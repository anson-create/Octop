"""Jira Server / Data Center port — the only way an issue becomes a requirement.

Credentials never live in this module's state beyond the client instance: the PAT
is handed in by the caller, which reads it from the host secret store. Every
failure here is reported, never papered over — an unreachable Jira must not
silently produce a requirement with an invented description.

Sync on purpose. ``tick`` and the card sweeper already run off the event loop
(the sweeper in its own thread), so an async client would buy nothing and cost a
second connection-pool lifecycle to get wrong.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from octop.infra.pm.ports.base import PortError, PortResult, PortUnavailable

# Jira Server/DC caps search paging at 100 by default but 50 keeps a single tick
# responsive and is plenty for a picker UI.
MAX_RESULTS = 50

# Only what the picker and the intake step actually need. Asking for ``*all``
# pulls changelogs in some DC versions and blows up payload size.
ISSUE_FIELDS = (
    "summary,description,status,assignee,reporter,priority,labels,"
    "issuetype,project,created,updated,resolution"
)

# Fields that say *which part of the product* an issue is about. `components`
# is the only structural one — it is literally a functional-area picker in the
# Jira UI — so it is fetched even though the picker list does not show it.
LOCATE_FIELDS = "components,environment,attachment,comment"

# Names (Chinese and English) that mark a custom field as carrying functional
# context. Matched as substrings against the field's display name, because a
# "痛点" that a team writes today is `customfield_10231` tomorrow.
_LOCATE_NAME_HINTS = (
    "模块",
    "功能",
    "页面",
    "菜单",
    "系统",
    "板块",
    "业务",
    "产品",
    "痛点",
    "问题",
    "背景",
    "现状",
    "场景",
    "建议",
    "方案",
    "期望",
    "目标",
    "需求",
    "流程",
    "影响",
    "范围",
    "说明",
    "描述",
    "module",
    "feature",
    "page",
    "menu",
    "screen",
    "pain",
    "problem",
    "background",
    "context",
    "scenario",
    "proposal",
    "solution",
    "suggestion",
    "expect",
    "goal",
    "scope",
    "story",
    "requirement",
)

# Name fragments that mean the field is bookkeeping, not product context. A
# field called "需求优先级" would otherwise match 需求 above.
_LOCATE_NAME_VETO = (
    "优先级",
    "状态",
    "经办人",
    "报告人",
    "创建",
    "更新",
    "截止",
    "工时",
    "工作量",
    "估算",
    "编号",
    "版本号",
    "priority",
    "status",
    "assignee",
    "reporter",
    "created",
    "updated",
    "due",
    "estimate",
    "sprint",
    "rank",
    "vote",
    "watch",
    "resolution",
)

DEFAULT_JQL = (
    "assignee = currentUser() AND resolution = Unresolved "
    "ORDER BY updated DESC"
)


class JiraError(PortError):
    """A Jira call failed in a way the caller must surface."""


def _clean_text(value: Any) -> str:
    """Jira DC returns plain strings; Cloud returns ADF documents.

    Anything structured is flattened to text rather than dumped as JSON, because
    the downstream model reads this field as prose.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts: list[str] = []
        _flatten_adf(value, parts)
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, list):
        return "\n".join(_clean_text(item) for item in value).strip()
    return str(value).strip()


def _flatten_adf(node: dict[str, Any], out: list[str]) -> None:
    if node.get("type") == "text":
        text = node.get("text")
        if isinstance(text, str):
            out.append(text)
        return
    for child in node.get("content") or []:
        if isinstance(child, dict):
            _flatten_adf(child, out)
    if node.get("type") in ("paragraph", "heading", "listItem"):
        out.append("\n")


def _looks_like_prose(schema: dict[str, Any]) -> bool:
    """Is this custom field free text a human typed?

    Deliberately narrow: dates, numbers, selects and user pickers cannot carry
    a feature name, and asking for them only bloats the payload.
    """

    kind = str(schema.get("type") or "").lower()
    if kind in ("string", "text"):
        return True
    # ADF documents (Jira Cloud renders "paragraph" rich text) come back as
    # objects; ``any`` is the schema type Cloud uses for rich text fields.
    return kind in ("any", "doc")


def locate_field_ids(catalog: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Pick the custom fields worth reading when locating a page in source.

    Returns ``[{"id", "name"}]``. This is intentionally permissive on *which*
    fields it reads and strict on *nothing* downstream: what comes back is
    treated as one weighted signal among several, never as an answer on its own.
    """

    picked: list[dict[str, str]] = []
    for item in catalog:
        if not isinstance(item, dict):
            continue
        field_id = str(item.get("id") or "")
        name = _clean_text(item.get("name"))
        if not field_id.startswith("customfield_") or not name:
            continue
        name_lc = name.lower()
        if any(veto in name_lc for veto in _LOCATE_NAME_VETO):
            continue
        if not any(hint in name_lc for hint in _LOCATE_NAME_HINTS):
            continue
        schema = item.get("schema") or {}
        if not _looks_like_prose(schema):
            continue
        picked.append({"id": field_id, "name": name})
    return picked


def issue_to_summary(
    raw: dict[str, Any], *, custom_names: dict[str, str] | None = None
) -> dict[str, Any]:
    """Normalise one Jira issue into the shape the picker and intake want.

    ``custom_names`` maps ``customfield_10999`` to its display name. Without it
    the custom values are dropped: an unnamed field would reach the model as an
    opaque key, and the model would treat the key itself as content.
    """
    fields = raw.get("fields") or {}
    status = fields.get("status") or {}
    assignee = fields.get("assignee") or {}
    priority = fields.get("priority") or {}
    issue_type = fields.get("issuetype") or {}
    project = fields.get("project") or {}
    resolution = fields.get("resolution") or {}

    labels = [str(item) for item in (fields.get("labels") or []) if isinstance(item, str)]
    components = [
        _clean_text((item or {}).get("name"))
        for item in (fields.get("components") or [])
        if isinstance(item, dict)
    ]

    custom: list[dict[str, str]] = []
    for field_id, display in (custom_names or {}).items():
        value = fields.get(field_id)
        if value is None:
            continue
        text = _clean_text(value)
        if not text:
            continue
        custom.append({"id": field_id, "name": display, "text": text})

    comments: list[dict[str, str]] = []
    comment_block = fields.get("comment") or {}
    if isinstance(comment_block, dict):
        for item in comment_block.get("comments") or []:
            if not isinstance(item, dict):
                continue
            body = _clean_text(item.get("body"))
            if not body:
                continue
            comments.append(
                {
                    "author": _clean_text((item.get("author") or {}).get("displayName")),
                    "text": body,
                    "created": _clean_text(item.get("created")),
                }
            )

    attachments: list[dict[str, str]] = []
    for item in fields.get("attachment") or []:
        if not isinstance(item, dict):
            continue
        filename = _clean_text(item.get("filename"))
        if not filename:
            continue
        attachments.append(
            {
                "filename": filename,
                "mime": _clean_text(item.get("mimeType")),
                "size": str(item.get("size") or ""),
            }
        )

    return {
        "key": str(raw.get("key") or ""),
        "title": _clean_text(fields.get("summary")) or str(raw.get("key") or ""),
        "description": _clean_text(fields.get("description")),
        "status": _clean_text(status.get("name")),
        "status_category": str((status.get("statusCategory") or {}).get("key") or ""),
        "assignee": _clean_text(assignee.get("displayName")),
        "reporter": _clean_text((fields.get("reporter") or {}).get("displayName")),
        "priority": _clean_text(priority.get("name")),
        "issue_type": _clean_text(issue_type.get("name")),
        "project": _clean_text(project.get("key")),
        "resolution": _clean_text(resolution.get("name")),
        "labels": labels,
        "components": [item for item in components if item],
        "environment": _clean_text(fields.get("environment")),
        "custom_fields": custom,
        "comments": comments,
        "attachments": attachments,
        "created": _clean_text(fields.get("created")),
        "updated": _clean_text(fields.get("updated")),
    }


class JiraClient:
    """Thin, synchronous Jira REST v2 client.

    One client instance owns one connection pool; build it once per request batch
    rather than per call, or the TLS handshake dominates the latency.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 15.0,
        verify_tls: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        cleaned = (base_url or "").strip().rstrip("/")
        if not cleaned:
            raise PortUnavailable("Jira base_url is not configured")
        if not token:
            raise PortUnavailable("Jira token is not configured")
        self.base_url = cleaned
        self._client = httpx.Client(
            base_url=cleaned,
            timeout=timeout,
            verify=verify_tls,
            headers={
                # Server/DC PATs are bearer tokens; Basic auth is Cloud-only and
                # would fail here with a 401 that looks like a bad password.
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            transport=transport,
            follow_redirects=True,
        )
        # Field catalog is instance-wide and changes rarely; one fetch per
        # client beats re-asking on every intake.
        self._locate_fields: list[dict[str, str]] | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # --- transport --------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PortUnavailable(f"Jira 请求超时（{self.base_url}）") from exc
        except httpx.HTTPError as exc:
            raise PortUnavailable(f"Jira 不可达：{exc}") from exc

        if response.status_code in (401, 403):
            raise PortUnavailable(
                f"Jira 拒绝访问（HTTP {response.status_code}）：token 无效、已过期或无该 issue 的权限"
            )
        if response.status_code == 404:
            raise JiraError("Jira 返回 404：issue 不存在或地址不对")
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = str(
                    body.get("errorMessages")
                    or body.get("message")
                    or body
                )[:300]
            except ValueError:
                detail = response.text[:300]
            raise JiraError(f"Jira 返回 {response.status_code}：{detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise JiraError("Jira 返回的不是 JSON，可能被网关/登录页拦截") from exc

    # --- API --------------------------------------------------------------

    def myself(self) -> dict[str, Any]:
        data = self._request("GET", "/rest/api/2/myself")
        return data if isinstance(data, dict) else {}

    def projects(self) -> list[dict[str, Any]]:
        """The project keys this token can see.

        The mapping from project to repository has to be built against the real
        key names, not against what we assume the instance uses: a project key is
        chosen per instance and can be anything. Without this call the operator
        would be typing keys from memory, and a single typo would silently route
        a requirement to the wrong product's source.
        """

        payload = self._request("GET", "/rest/api/2/project")
        if not isinstance(payload, list):
            raise JiraError("Jira 项目列表响应格式不对")
        result: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            key = _clean_text(item.get("key"))
            if not key:
                continue
            result.append(
                {
                    "key": key,
                    "name": _clean_text(item.get("name")),
                    "type": _clean_text(item.get("projectTypeKey")),
                    "archived": bool(item.get("archived", False)),
                }
            )
        result.sort(key=lambda item: item["key"])
        return result

    def components(self, project_key: str) -> list[dict[str, Any]]:
        """The functional areas this project actually files issues against.

        ``components`` is the one Jira field that is structurally a functional-area
        picker rather than free text, which makes it the best anchor we have for
        locating a page. An empty list is a real answer — the team does not use
        components — and the caller must say so instead of inferring a mapping.
        """

        key = _clean_text(project_key)
        if not key:
            return []
        payload = self._request("GET", f"/rest/api/2/project/{quote(key, safe='')}/components")
        if not isinstance(payload, list):
            raise JiraError("Jira 组件列表响应格式不对")
        result: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = _clean_text(item.get("name"))
            if not name:
                continue
            result.append(
                {
                    "id": _clean_text(item.get("id")),
                    "name": name,
                    "lead": _clean_text((item.get("lead") or {}).get("displayName")),
                    "description": _clean_text(item.get("description"))[:200],
                }
            )
        result.sort(key=lambda item: item["name"])
        return result

    def count(self, jql: str) -> int:
        """How many issues match — separates a live component from a dead one.

        A component nobody files against is a leftover from a rename; mapping it
        to a page would add a locator signal that never fires. ``maxResults=1``
        keeps the payload to one issue while still returning ``total``.
        """

        payload = self._request(
            "GET", "/rest/api/2/search", params={"jql": jql, "maxResults": 1}
        )
        total = payload.get("total") if isinstance(payload, dict) else None
        return int(total) if isinstance(total, int) else 0

    def search(self, jql: str, *, limit: int = 20) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), MAX_RESULTS))
        payload = self._request(
            "GET",
            "/rest/api/2/search",
            params={
                "jql": jql,
                "maxResults": capped,
                "fields": ISSUE_FIELDS,
            },
        )
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list):
            raise JiraError("Jira 搜索结果缺少 issues 字段")
        return [issue_to_summary(item) for item in issues if isinstance(item, dict)]

    def locate_fields(self) -> list[dict[str, str]]:
        """Custom fields on this instance that can name a functional area.

        Cached per client. A failure here is not fatal for intake: the issue is
        still readable, it just gets fewer signals, and the caller says so.
        """

        if self._locate_fields is not None:
            return self._locate_fields
        try:
            payload = self._request("GET", "/rest/api/2/field")
        except PortError:
            self._locate_fields = []
            return []
        catalog = payload if isinstance(payload, list) else []
        self._locate_fields = locate_field_ids(catalog)
        return self._locate_fields

    def issue(self, key: str, *, with_context: bool = False) -> dict[str, Any]:
        """Fetch one issue.

        ``with_context`` additionally pulls components, environment, attachments,
        comments and the locate-relevant custom fields. It costs one extra call
        (the field catalog) and a larger payload, so the picker list does not
        use it — only intake does, where "which page is this about" matters.
        """

        cleaned = (key or "").strip()
        if not cleaned:
            raise PortError("issue key is empty")
        fields = ISSUE_FIELDS
        custom_names: dict[str, str] = {}
        if with_context:
            located = self.locate_fields()
            custom_names = {item["id"]: item["name"] for item in located}
            extra = list(LOCATE_FIELDS.split(",")) + list(custom_names)
            fields = ",".join([ISSUE_FIELDS, *extra])
        payload = self._request(
            "GET",
            f"/rest/api/2/issue/{cleaned}",
            params={"fields": fields},
        )
        if not isinstance(payload, dict):
            raise JiraError("Jira issue 响应格式不对")
        return issue_to_summary(payload, custom_names=custom_names)

    def probe(self) -> dict[str, Any]:
        """Cheap reachability check: who am I, and how many issues do I see."""
        me = self.myself()
        jql = DEFAULT_JQL
        total = None
        try:
            payload = self._request(
                "GET",
                "/rest/api/2/search",
                params={"jql": jql, "maxResults": 1, "fields": "summary"},
            )
            if isinstance(payload, dict):
                total = payload.get("total")
        except JiraError:
            # A search failure still proves the endpoint authenticates; report it
            # as "connected" with no count instead of failing the whole probe.
            total = None
        return {
            "base_url": self.base_url,
            "user": _clean_text(me.get("displayName")) or _clean_text(me.get("name")),
            "account": _clean_text(me.get("name")),
            "default_jql": jql,
            "open_assigned": total,
        }


class JiraPort:
    """Evidence port over Jira: a query returns issue envelopes, not prose."""

    name = "jira"

    def __init__(
        self,
        factory: Any,
        *,
        jql: str | None = None,
    ) -> None:
        # ``factory`` builds a JiraClient on demand so a rotated token takes effect
        # without restarting the process.
        self._factory = factory
        self._jql = jql or DEFAULT_JQL

    def available(self) -> bool:
        try:
            with self._factory() as client:
                return bool(client.base_url)
        except PortError:
            return False

    def fetch(self, *, query: str, task_ref: str, limit: int = 10) -> PortResult:
        needle = (query or "").strip()
        if not needle:
            return PortResult(port=self.name, unavailable_reason="query is empty")
        # Quote the needle so a title containing JQL syntax cannot rewrite the query.
        escaped = needle.replace('"', '\\"')
        jql = f'text ~ "{escaped}" ORDER BY updated DESC'
        try:
            with self._factory() as client:
                issues = client.search(jql, limit=limit)
                base_url = client.base_url
        except PortUnavailable as exc:
            return PortResult(port=self.name, unavailable_reason=str(exc))
        except PortError as exc:
            return PortResult(port=self.name, unavailable_reason=str(exc))

        envelopes: list[dict[str, Any]] = []
        for item in issues:
            envelopes.append(
                {
                    "source_type": "jira",
                    # The only Jira locator the evidence schema accepts is
                    # jira_field, and it insists on issue + field. The excerpt
                    # carries status/assignee as context around that field.
                    "locator": {
                        "kind": "jira_field",
                        "issue": item["key"],
                        "field": "summary",
                        "url": f"{base_url}/browse/{item['key']}" if base_url else "",
                    },
                    "excerpt": (
                        f"{item['key']} [{item['status'] or '-'}] {item['title']}"
                        + (f"｜指派人 {item['assignee']}" if item["assignee"] else "")
                    )[:500],
                    "confidence": 1.0,
                }
            )
        return PortResult(port=self.name, envelopes=envelopes)
