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


def issue_to_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise one Jira issue into the shape the picker and intake want."""
    fields = raw.get("fields") or {}
    status = fields.get("status") or {}
    assignee = fields.get("assignee") or {}
    priority = fields.get("priority") or {}
    issue_type = fields.get("issuetype") or {}
    project = fields.get("project") or {}
    resolution = fields.get("resolution") or {}
    labels = fields.get("labels") or []
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
        "labels": [str(item) for item in labels if isinstance(item, str)],
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

    def issue(self, key: str) -> dict[str, Any]:
        cleaned = (key or "").strip()
        if not cleaned:
            raise PortError("issue key is empty")
        payload = self._request(
            "GET",
            f"/rest/api/2/issue/{cleaned}",
            params={"fields": ISSUE_FIELDS},
        )
        if not isinstance(payload, dict):
            raise JiraError("Jira issue 响应格式不对")
        return issue_to_summary(payload)

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
