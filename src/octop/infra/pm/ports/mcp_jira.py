"""Jira through a connected MCP server, instead of a stored REST credential.

Why this exists
---------------
The rest of this package talks to Jira with :class:`~octop.infra.pm.ports.jira.JiraClient`,
which needs a site URL plus a token kept in PM's own secret store. An operator
who has already wired Jira into an MCP connector (Atlassian MCP over stdio) ends
up entering the same credentials a second time, and rotating them in two places.

This module is the other transport: it drives the MCP server the connector
already owns and converts its JSON into the same issue shape the picker and
intake expect. Nothing here stores a credential — the server spec (command,
args, env) is read from the connector row at call time.

Two properties are load-bearing:

* **The session is long-lived.** Spawning ``mcp-atlassian`` costs about a
  second of interpreter start-up. The intake dialog re-queries on a 400 ms
  debounce, so a per-call subprocess would make typing feel broken. One stdio
  session is kept per connector and reused; it is dropped on error and rebuilt
  on the next call.
* **The facade is synchronous.** ``PMService`` is sync and is called from sync
  request handlers. The MCP SDK is asyncio-only, so the event loop lives on a
  background thread and every call hops onto it. See :class:`McpJiraSession`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from typing import Any

from octop.infra.pm.ports.base import PortError

_log = logging.getLogger(__name__)

# Jira's own status categories, as the values REST reports them. The MCP server
# reports the localised name instead ("处理中"), and ``status_category`` is part
# of the summary contract, so it is translated back.
_CATEGORY_KEYS = {
    "待办": "new",
    "处理中": "indeterminate",
    "完成": "done",
    "to do": "new",
    "in progress": "indeterminate",
    "done": "done",
}

# Fields asked for explicitly. ``*all`` alone is not enough: on the live site it
# returned forty keys including every custom field but omitted ``description``
# for an issue that had one. Asking for both covers the two shapes observed.
_ISSUE_FIELDS = (
    "*all,summary,description,attachment,environment,components,labels,"
    "status,issuetype,priority,project,assignee,reporter,comment,created,updated,resolution"
)

# Top-level keys the MCP server uses for things that are not custom fields.
_RESERVED_KEYS = frozenset(
    {
        "id",
        "key",
        "summary",
        "url",
        "browse_url",
        "status",
        "issue_type",
        "issuetype",
        "priority",
        "project",
        "worklog",
        "assignee",
        "reporter",
        "creator",
        "labels",
        "components",
        "epic_name",
        "epic_link",
        "created",
        "updated",
        "duedate",
        "resolutiondate",
        "resolution",
        "comments",
        "description",
        "attachment",
        "attachments",
        "environment",
        "watches",
        "votes",
        "subtasks",
        "issuelinks",
        "parent",
        "timeestimate",
        "timespent",
        "aggregatetimespent",
        "sprint",
        "story_points",
        "status_category",
        "changelogs",
        "transitions",
        "properties",
    }
)


def _text(value: Any) -> str:
    """Flatten whatever Jira puts in a field into display text.

    Custom fields arrive as ``{"value": "..."}``, ``{"name": "..."}``,
    ``{"displayName": "..."}`` or a list of any of those. Passing a dict through
    ``str()`` would put ``{'value': ...}`` in the requirement text, which reads
    like a bug rather than a field value.
    """

    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("value", "display_name", "displayName", "name", "text", "body"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        parts = [_text(v) for v in value.values()]
        return "、".join(item for item in parts if item)
    if isinstance(value, list):
        parts = [_text(item) for item in value if item not in (None, "", [], {})]
        return "、".join(item for item in parts if item)
    return str(value).strip()


def _person(node: Any) -> str:
    if isinstance(node, dict):
        for key in ("display_name", "displayName", "name", "email"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _text(node)


def mcp_issue_to_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise one MCP ``jira_get_issue`` payload into the picker shape.

    Mirrors :func:`octop.infra.pm.ports.jira.issue_to_summary` key for key, so
    every downstream step (intake composition, page location, evidence) is
    transport-agnostic.
    """

    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    project = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    resolution = raw.get("resolution") if isinstance(raw.get("resolution"), dict) else {}

    labels = [str(item) for item in (raw.get("labels") or []) if isinstance(item, str)]

    components: list[str] = []
    for item in raw.get("components") or []:
        if isinstance(item, dict):
            name = _text(item.get("name"))
            if name:
                components.append(name)
        elif isinstance(item, str) and item.strip():
            components.append(item.strip())

    # With ``use_display_names`` the custom fields arrive under their Chinese
    # display name as top-level keys, so anything not reserved is a custom field
    # and its name needs no separate catalogue lookup.
    custom: list[dict[str, str]] = []
    for key, value in raw.items():
        if key in _RESERVED_KEYS:
            continue
        text = _text(value)
        if not text:
            continue
        custom.append({"id": key, "name": key, "text": text})

    comments: list[dict[str, str]] = []
    for item in raw.get("comments") or []:
        if not isinstance(item, dict):
            continue
        body = _text(item.get("body"))
        if not body:
            continue
        comments.append(
            {
                "author": _person(item.get("author")),
                "text": body,
                "created": _text(item.get("created")),
            }
        )

    attachments: list[dict[str, str]] = []
    pool = raw.get("attachment") or raw.get("attachments") or []
    if isinstance(pool, dict):
        pool = [pool]
    for item in pool:
        if not isinstance(item, dict):
            continue
        filename = _text(item.get("filename")) or _text(item.get("name"))
        if not filename:
            continue
        attachments.append(
            {
                "filename": filename,
                "mime": _text(item.get("mime") or item.get("mimeType")),
                "size": str(item.get("size") or ""),
            }
        )

    category = _text(status.get("category")).lower()
    category_key = _text(status.get("statusCategory")) or _CATEGORY_KEYS.get(category, "")

    key = _text(raw.get("key"))
    return {
        "key": key,
        "title": _text(raw.get("summary")) or key,
        "description": _text(raw.get("description")),
        "status": _text(status.get("name")),
        "status_category": category_key,
        "assignee": _person(raw.get("assignee")),
        "reporter": _person(raw.get("reporter")),
        "priority": _text((raw.get("priority") or {}).get("name")),
        "issue_type": _text((raw.get("issue_type") or raw.get("issuetype") or {}).get("name")),
        "project": _text(project.get("key") or project.get("name")),
        "resolution": _text(resolution.get("name")) or _text(raw.get("resolution")),
        "labels": labels,
        "components": components,
        "environment": _text(raw.get("environment")),
        "custom_fields": custom,
        "comments": comments,
        "attachments": attachments,
        "created": _text(raw.get("created")),
        "updated": _text(raw.get("updated")),
    }


class McpJiraSession:
    """A long-lived stdio MCP session behind a synchronous ``call``.

    The SDK is asyncio-only and ``PMService`` is synchronous, so the loop runs
    on a daemon thread: ``_boot`` enters the stdio and client context managers,
    then ``run_forever`` keeps them open while calls are submitted with
    ``run_coroutine_threadsafe``.

    Any failure drops the session instead of retrying in place — an stdio
    server that died mid-call cannot be revived, and a stale handle turns every
    later request into the same timeout.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        call_timeout: float = 60.0,
        boot_timeout: float = 30.0,
        label: str = "",
    ) -> None:
        self._command = command
        self._args = list(args or [])
        self._env = dict(env or {})
        self._call_timeout = call_timeout
        self._boot_timeout = boot_timeout
        self._label = label or command
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any = None
        self._stack: Any = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._last_used = 0.0

    # --- lifecycle ------------------------------------------------------

    async def _boot(self) -> None:
        from contextlib import AsyncExitStack  # noqa: PLC0415

        try:
            from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
            from mcp.client.stdio import stdio_client  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise PortError("MCP 依赖未安装，无法通过连接器读 Jira") from exc

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
        )
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        self._session = await stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        self._stack = stack

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._boot())
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            self._failure = exc
            self._ready.set()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                if self._stack is not None:
                    loop.run_until_complete(self._stack.aclose())
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
            loop.close()

    def _start(self) -> None:
        self._ready.clear()
        self._failure = None
        thread = threading.Thread(
            target=self._run, name=f"mcp-jira-{self._label}", daemon=True
        )
        thread.start()
        self._thread = thread
        if not self._ready.wait(self._boot_timeout):
            self._thread = None
            raise PortError(f"MCP 服务 {self._label} 启动超时（{self._boot_timeout:.0f}s）")
        if self._failure is not None:
            self._thread = None
            raise PortError(f"MCP 服务 {self._label} 启动失败：{self._failure}")

    def _alive(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._session is not None
            and self._loop is not None
        )

    def _discard(self) -> None:
        loop = self._loop
        self._session = None
        self._stack = None
        self._loop = None
        self._thread = None
        if loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)

    def close(self) -> None:
        with self._lock:
            loop = self._loop
            if loop is not None:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(loop.stop)
            thread = self._thread
            self._discard()
            if thread is not None:
                thread.join(timeout=5)

    # --- one call -------------------------------------------------------

    def call(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Invoke one MCP tool and return its JSON payload."""

        with self._lock:
            if not self._alive():
                self._continue_idle_session()
                if not self._alive():
                    self._start()
            loop = self._loop
            session = self._session
            assert loop is not None and session is not None  # noqa: S101
            try:
                future = asyncio.run_coroutine_threadsafe(
                    session.call_tool(tool, payload), loop
                )
                result = future.result(timeout=self._call_timeout)
            except Exception as exc:  # noqa: BLE001 - transport failure
                self._discard()
                raise PortError(f"调用 MCP 工具 {tool} 失败：{exc}") from exc
            self._last_used = time.monotonic()
        return _payload_of(result, tool)

    def _continue_idle_session(self) -> None:
        """Nothing to resume — kept as a hook for a future idle-eviction policy."""

        _ = self._last_used


def _payload_of(result: Any, tool: str) -> dict[str, Any]:
    """Extract the JSON object a tool returned.

    The Atlassian server answers with one ``TextContent`` holding JSON. A tool
    that fails still comes back as a normal result with ``isError`` set, so an
    unchecked ``content[0].text`` would be parsed as an error page and read as
    data.
    """

    text = ""
    for chunk in getattr(result, "content", None) or []:
        piece = getattr(chunk, "text", None)
        if isinstance(piece, str):
            text += piece
    if getattr(result, "isError", False):
        raise PortError(f"MCP 工具 {tool} 返回错误：{text[:300] or '（无内容）'}")
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        # Some tools answer in markdown. Nothing downstream can use that, and
        # silently treating prose as zero results would look like "no tickets".
        raise PortError(f"MCP 工具 {tool} 返回的不是 JSON：{stripped[:200]}") from None
    return data if isinstance(data, dict) else {"items": data}


class McpJiraClient:
    """Same surface as ``JiraClient``, backed by a connector-owned MCP server.

    Only the methods the PM flow calls are implemented. ``statuses`` and
    ``priorities`` return empty tuples: the Atlassian server exposes no global
    status catalogue, and an empty answer makes ``jira_meta`` say "this site
    did not report status names" instead of inventing a list.
    """

    def __init__(
        self,
        session: McpJiraSession,
        *,
        base_url: str = "",
        username: str = "",
    ) -> None:
        self._session = session
        self.base_url = base_url
        self._username = username

    # The session outlives any one request; closing here would break the next
    # call for every other user of the same connector.
    def __enter__(self) -> McpJiraClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def close(self) -> None:
        return None

    @property
    def transport(self) -> str:
        return "mcp"

    # --- reads ----------------------------------------------------------

    def myself(self) -> dict[str, Any]:
        """Who the connector is acting as.

        Keys are the REST spelling (``displayName``/``name``) because that is
        what ``jira_meta`` and ``probe`` already read; the transport should not
        leak into callers.
        """

        profile: dict[str, Any] = {}
        if self._username:
            try:
                profile = self._session.call(
                    "jira_get_user_profile", {"user_identifier": self._username}
                )
            except PortError:
                profile = {}
        display = (
            _text(profile.get("display_name"))
            or _text(profile.get("displayName"))
            or _text(profile.get("name"))
            or self._username
        )
        # Deliberately not falling back to ``_display_name``: that is the
        # connector's label ("Jira + Confluence (WallTech)"), and putting it
        # where a person's name belongs makes ``jira_meta`` report the wrong
        # owner for the default queue.
        return {
            "displayName": display,
            "name": _text(profile.get("name")) or self._username,
            "emailAddress": _text(profile.get("email")),
        }

    def projects(self) -> list[dict[str, Any]]:
        payload = self._session.call("jira_get_all_projects", {"include_archived": False})
        items = payload.get("projects") or payload.get("values") or payload.get("items") or []
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "key": _text(item.get("key")),
                    "name": _text(item.get("name")),
                    "id": _text(item.get("id")),
                }
            )
        return out

    def components(self, project_key: str) -> list[dict[str, Any]]:
        payload = self._session.call(
            "jira_get_project_components", {"project_key": project_key}
        )
        items = payload.get("components") or payload.get("items") or []
        return [
            {"name": _text(item.get("name")), "id": _text(item.get("id"))}
            for item in items
            if isinstance(item, dict) and _text(item.get("name"))
        ]

    def statuses(self) -> tuple[str, ...]:
        return ()

    def priorities(self) -> tuple[str, ...]:
        return ()

    def count(self, jql: str) -> int:
        payload = self._session.call(
            "jira_search", {"jql": jql, "fields": "summary", "limit": 1}
        )
        try:
            return int(payload.get("total") or 0)
        except (TypeError, ValueError):
            return 0

    def search(self, jql: str, *, limit: int = 20) -> list[dict[str, Any]]:
        payload = self._session.call(
            "jira_search",
            {
                "jql": jql,
                "fields": _ISSUE_FIELDS,
                "limit": max(1, min(int(limit or 20), 50)),
                "use_display_names": True,
            },
        )
        issues = payload.get("issues") or payload.get("items") or []
        return [
            mcp_issue_to_summary(item) for item in issues if isinstance(item, dict)
        ]

    def locate_fields(self) -> list[dict[str, str]]:
        """Custom field names come back inline, so no catalogue call is needed."""

        return []

    def issue(self, key: str, *, with_context: bool = False) -> dict[str, Any]:
        cleaned = (key or "").strip()
        if not cleaned:
            raise PortError("issue key is empty")
        # ``with_context`` is about the REST path, where comments and custom
        # fields need a separate catalogue call and a wider field list. Here one
        # call already returns everything, so both branches are the same.
        _ = with_context
        payload = self._session.call(
            "jira_get_issue",
            {
                "issue_key": cleaned.upper(),
                "fields": _ISSUE_FIELDS,
                "use_display_names": True,
                "comment_limit": 100,
            },
        )
        if not payload.get("key"):
            raise PortError(f"MCP 未返回工单 {cleaned} 的内容")
        return mcp_issue_to_summary(payload)

    def probe(self) -> dict[str, Any]:
        total: int | None = None
        try:
            total = self.count('assignee = currentUser() AND resolution = Unresolved')
        except PortError:
            total = None
        return {
            "base_url": self.base_url,
            "user": self.myself().get("displayName") or self._username,
            "account": self._username,
            "default_jql": "assignee = currentUser() AND resolution = Unresolved",
            "open_assigned": total,
            "transport": "mcp",
        }


# --- one session per connector ------------------------------------------

_SESSIONS: dict[str, McpJiraSession] = {}
_SESSIONS_LOCK = threading.Lock()


def session_for(
    instance_id: str,
    command: str,
    args: list[str],
    env: dict[str, str],
) -> McpJiraSession:
    """Reuse the stdio session bound to one connector instance.

    Keyed by connector instance: two workspaces pointed at the same connector
    share it, and a second connector with different credentials gets its own.
    """

    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(instance_id)
        if existing is None:
            existing = McpJiraSession(command, args, env, label=instance_id[-8:])
            _SESSIONS[instance_id] = existing
        return existing


def close_all() -> None:
    """Drop every cached session. Used by tests and by shutdown hooks."""

    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        session.close()
