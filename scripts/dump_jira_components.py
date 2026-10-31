"""Dump every Jira component, and draft the component -> page mapping from source.

Run on the server, where the Jira credentials live. Answers, per project:

1. Which components does this project actually file issues against, and how many?
2. For each component name, which page in the mirror does it plausibly mean?

The second answer is a *draft*, never a fact: a component name is Chinese prose
("费用管理"), the mirror is file paths ("charge-management.vue"), and the bridge
between them is a heuristic. So every row carries the evidence it was decided
on and is marked `suggested` / `needs_review` / `no_candidate`. Only rows a human
confirms should end up in the mapping table.

No writes to Jira. Nothing is created, updated or deleted anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/opt/octop/venv/lib/python3.12/site-packages")


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
    """Quote a value so a component name cannot rewrite the query.

    Component names are free text an operator types, and one stray quote would
    turn a count into a different query. Cheap to get right, expensive to debug.
    """

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _draft(component_name: str, root: Path | None) -> dict[str, object]:
    """Locate a page for one component name, and say how sure we are."""

    if root is None:
        return {"status": "no_repo", "note": "该项目未映射到前端仓，只列组件不定位"}

    from octop.infra.pm.runtime.prototype_derive import locate_page

    location = locate_page(root, component_name, limit=6)
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
        "terms": list(location.terms),
        "candidates": candidates,
        "notes": list(location.notes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump Jira components and draft a page mapping")
    parser.add_argument("--json-out", default="jira_components.json", help="where to write the draft")
    parser.add_argument("--project", default="", help="only this project key")
    parser.add_argument("--skip-counts", action="store_true", help="skip per-component issue counts")
    args = parser.parse_args()

    from octop.infra.pm.domain.repos import resolve_project

    service = _load_service()
    print("=== workspaces ===")
    workspace_id = _pick_workspace(service)
    if workspace_id is None:
        print("\nNO_JIRA_CONFIGURED：该实例没有任何 workspace 配好 base_url + token。")
        print("先在 PM Studio 的 Jira 集成页填好，再跑本脚本。")
        return 1

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
            root = None
            if binding is not None and binding.web is not None:
                candidate_root = Path(binding.web.mirror)
                if candidate_root.is_dir():
                    root = candidate_root
                else:
                    print(f"  ! {key} 镜像不存在：{candidate_root}")

            components = client.components(key)
            print(f"\n--- {key} {project['name']} · 组件 {len(components)} 个 · 仓 {root or '未映射'} ---")
            if not components:
                print("    （该团队不用组件字段）")

            items: list[dict[str, object]] = []
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
                draft = _draft(name, root)
                items.append({"name": name, "issues": usage, **draft})
                mark = {"suggested": "✓", "needs_review": "?", "no_candidate": "×"}.get(
                    str(draft.get("status")), "-"
                )
                print(f"    {mark} {name:<20} issues={usage:<6} {draft.get('page') or ''}")
                rows.append(
                    {
                        "project": key,
                        "component": name,
                        "status": draft.get("status"),
                        "page": draft.get("page") or "",
                        "candidates": draft.get("candidates") or [],
                    }
                )

            payload["projects"].append(  # type: ignore[index]
                {
                    "key": key,
                    "name": project["name"],
                    "system": binding.system if binding else "",
                    "repo": str(root) if root else "",
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
    print(f"\n合计 {len(rows)} 个组件：suggested {len(suggested)} · needs_review {len(review)} · 其余无候选")
    if review:
        print("\n需要人工拍板的（候选不唯一）：")
        for row in review[:20]:
            tops = " | ".join(
                f"{item['path']}({item['score']})" for item in (row["candidates"] or [])[:3]  # type: ignore[index]
            )
            print(f"  {row['project']} / {row['component']}: {tops}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
