"""Dump every Jira component, and draft the component -> page mapping.

Run where the code mirror lives. Answers, per project:

1. Which components does this project actually file issues against, and how many?
2. For each component name, which page in the mirror does it mean?

The second answer prefers the router over any text search: the product already
says "订单中心" in its own ``meta.title``, in the same Chinese the component uses.
Matching that vocabulary is stronger evidence than any keyword hit in source, so
it is tried first and the graph locator is only the fallback.

Every row is a draft marked ``suggested`` / ``needs_review`` / ``no_candidate``.
A title carried by two routes in two apps is genuinely two different pages, and
that ambiguity is reported rather than resolved by taking the top score.

No writes to Jira. Nothing is created, updated or deleted anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Where the deployed service keeps its virtualenv. Only used when this script is
# run from a checkout on the server; harmless elsewhere.
sys.path.insert(0, "/opt/octop/venv/lib/python3.12/site-packages")

# Below this, a routed .vue is an entrance shell that imports the real page one
# level down, not the screen itself.
_SHELL_BYTES = 1024


def _load_service():  # noqa: ANN202
    from octop.config import load_config
    from octop.infra.db.factory import open_database
    from octop.infra.db.repos.audit import AuditRepo
    from octop.infra.pm.service import PMService
    from octop.infra.utils.paths import PathLayout

    paths = PathLayout.from_env()
    config = load_config(paths.config)
    db = open_database(config, paths)
    return PMService(db, audit=AuditRepo(db))


def _pick_workspace(service) -> int | None:  # noqa: ANN001
    target = None
    for item in service.list_workspaces():
        status = service.jira_status(item["id"])
        print(f"  ws={item['id']} {item['name']:<20} jira_configured={status.get('configured')}")
        if status.get("configured") and target is None:
            target = item["id"]
    return target


def _jql_literal(value: str) -> str:
    """Quote a value so a component name cannot rewrite the query."""

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _draft_by_route(  # noqa: ANN001
    name: str, routes, prefer: tuple[str, ...] = ()
) -> dict[str, object] | None:
    """The product's own vocabulary. ``None`` means "not found this way"."""

    found = routes.match(name, prefer=prefer)
    if found.ok and found.entry is not None:
        entry = found.entry
        notes = ["路由标题唯一匹配"]
        if found.reason == "compound":
            notes = ["复合名按「板块-功能」拆解后唯一，且子页确在该板块内"]
        if prefer:
            notes.append(f"同名路由跨子应用时取 {'、'.join(prefer)}")
        if entry.section:
            notes.append("该标题是板块，组件只是它的默认子页")
        if 0 < entry.size < _SHELL_BYTES:
            notes.append(f"入口壳（{entry.size}B），真页面在它内部 import")
        return {
            "status": "suggested",
            # Repo-relative, which is what the deriver reads and what a reviewer
            # can open. ``entry.file`` is relative to the app's src instead.
            "page": routes.repo_path(entry),
            "app": entry.app,
            "method": f"route-title:{found.reason}",
            "section": entry.section,
            "size": entry.size,
            "candidates": [],
            "notes": notes,
        }
    if found.candidates:
        return {
            "status": "needs_review",
            "page": "",
            "method": "route-title",
            "candidates": [
                {"path": item.file, "app": item.app, "score": 0.0, "reasons": [f"路由 {item.path}"]}
                for item in found.candidates
            ],
            "notes": [
                f"标题 {name} 在 {len(found.candidates)} 条路由上重复，是不同子应用的页面，不自动选",
            ],
        }
    return None


def _draft_by_graph(name: str, root: Path | None) -> dict[str, object]:
    """Fallback: locate by source text against the code graph."""

    if root is None:
        return {"status": "no_repo", "note": "该项目未映射到前端仓，只列组件不定位"}

    from octop.infra.pm.runtime.prototype_derive import locate_page

    location = locate_page(root, name, limit=6)
    candidates = [
        {"path": item.source_file, "score": round(item.score, 2), "reasons": list(item.reasons[:3])}
        for item in location.candidates
    ]
    if location.ok:
        status = "suggested"
    elif location.ambiguous:
        status = "needs_review"
    else:
        status = "no_candidate"
    return {
        "status": status,
        "page": location.page,
        "method": location.method,
        "candidates": candidates,
        "notes": list(location.notes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump Jira components and draft a page mapping")
    parser.add_argument("--json-out", default="jira_components.json", help="where to write the draft")
    parser.add_argument("--project", default="", help="only this project key")
    parser.add_argument(
        "--repo-root", default="", help="覆盖项目映射，直接指向前端仓根目录（本地调试用）"
    )
    parser.add_argument("--skip-counts", action="store_true", help="skip per-component issue counts")
    args = parser.parse_args()

    from octop.infra.pm.domain.repos import resolve_project
    from octop.infra.pm.ports.routes import RouteIndex

    service = _load_service()
    print("=== workspaces ===")
    workspace_id = _pick_workspace(service)
    if workspace_id is None:
        print("\nNO_JIRA_CONFIGURED：该实例没有任何 workspace 配好地址与凭据。")
        print("先跑 scripts/configure_jira.py，再跑本脚本。")
        return 1

    forced_root = Path(args.repo_root).expanduser() if args.repo_root else None
    route_cache: dict[str, object] = {}

    def routes_for(root: Path | None):  # noqa: ANN202
        if root is None:
            return None
        key = str(root)
        if key not in route_cache:
            route_cache[key] = RouteIndex.load(root)
        return route_cache[key]

    client = service.jira_client(workspace_id)
    try:
        probe = client.probe()
        print(f"\nprobe: {json.dumps(probe, ensure_ascii=False)[:300]}")

        projects = client.projects()
        if args.project:
            projects = [item for item in projects if item["key"] == args.project]
        print(f"\n=== 可见项目 {len(projects)} 个 ===")

        payload: dict[str, object] = {
            "base_url": probe.get("base_url", ""),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "projects": [],
        }
        rows: list[dict[str, object]] = []

        for project in projects:
            key = project["key"]
            binding = resolve_project(key, {})
            root = forced_root
            if root is None and binding is not None and binding.web is not None:
                candidate_root = Path(binding.web.mirror)
                if candidate_root.is_dir():
                    root = candidate_root
                else:
                    print(f"  ! {key} 镜像不存在：{candidate_root}")

            routes = routes_for(root)
            components = client.components(key)
            route_note = f"路由 {len(routes)} 条" if routes is not None else "无路由表"
            print(
                f"\n--- {key} {project['name']} · 组件 {len(components)} 个 · "
                f"仓 {root or '未映射'} · {route_note} ---"
            )
            if not components:
                print("    （该团队不用组件字段）")

            items: list[dict[str, object]] = []
            # The product fact, not a tie-break: a CCTOWER ticket belongs to the
            # tenant app, where the same menu name also exists in the admin app.
            prefer = (binding.web.preferred_app,) if binding and binding.web else ()
            if prefer and prefer[0]:
                print(f"    子应用优先：{prefer[0]}（同名菜单优先取它）")
            for component in components:
                name = component["name"]
                usage = -1
                if not args.skip_counts:
                    try:
                        usage = client.count(
                            f"project = {_jql_literal(key)} AND component = {_jql_literal(name)}"
                        )
                    except Exception as exc:  # noqa: BLE001 - a count must not kill the dump
                        print(f"    ! {name} 计数失败：{str(exc)[:80]}")

                draft = _draft_by_route(name, routes, prefer) if routes is not None else None
                if draft is None:
                    draft = _draft_by_graph(name, root)

                items.append({"name": name, "issues": usage, **draft})
                mark = {"suggested": "✓", "needs_review": "?", "no_candidate": "×"}.get(
                    str(draft.get("status")), "-"
                )
                where = draft.get("page") or (
                    f"{len(draft.get('candidates') or [])} 个候选"
                    if draft.get("candidates")
                    else ""
                )
                print(f"    {mark} {name:<20} issues={usage:<6} {where}")
                rows.append(
                    {
                        "project": key,
                        "component": name,
                        "status": draft.get("status"),
                        "page": draft.get("page") or "",
                        "method": draft.get("method") or "",
                        "candidates": draft.get("candidates") or [],
                    }
                )

            payload["projects"].append(  # type: ignore[index]
                {
                    "key": key,
                    "name": project["name"],
                    "system": binding.system if binding else "",
                    "repo": str(root) if root else "",
                    "routes": len(routes) if routes is not None else 0,
                    "components": items,
                }
            )
    finally:
        client.close()

    out = Path(args.json_out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出：{out.resolve()}")

    suggested = [row for row in rows if row["status"] == "suggested"]
    review = [row for row in rows if row["status"] == "needs_review"]
    print(
        f"\n合计 {len(rows)} 个组件：suggested {len(suggested)} · "
        f"needs_review {len(review)} · 其余无候选"
    )
    if review:
        print("\n需要人工拍板的（候选不唯一）：")
        for row in review[:25]:
            tops = " | ".join(
                f"{item.get('app', '')}:{item['path']}({item.get('score', 0)})"
                for item in (row["candidates"] or [])[:3]  # type: ignore[index]
            )
            print(f"  {row['project']} / {row['component']}: {tops}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
