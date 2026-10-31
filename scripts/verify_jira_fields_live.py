"""Live check: does the ticket actually carry the fields we now rely on?

Run on the server, where the Jira credentials live. Answers three questions in
order, and refuses to guess at any of them:

1. Which custom fields on this instance look like they name a functional area?
2. For a real issue, which of those fields are actually filled in?
3. With that evidence, where does the locator land?

No writes. Nothing is created, updated or deleted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/octop/venv/lib/python3.12/site-packages")


def main() -> int:
    from octop.config import load_config
    from octop.infra.db.factory import open_database
    from octop.infra.db.repos.audit import AuditRepo
    from octop.infra.pm.domain.locate_evidence import LocateEvidence
    from octop.infra.pm.domain.repos import resolve_project
    from octop.infra.pm.service import PMService
    from octop.infra.utils.paths import PathLayout

    paths = PathLayout.from_env()
    config = load_config(paths.config)
    db = open_database(config, paths)
    service = PMService(db, audit=AuditRepo(db))

    workspaces = service.list_workspaces()
    print(f"workspaces: {len(workspaces)}")
    if not workspaces:
        print("NO_WORKSPACE")
        return 1

    target = None
    for item in workspaces:
        status = service.jira_status(item["id"])
        print(f"  ws={item['id']} {item['name']} jira_configured={status.get('configured')}")
        if status.get("configured") and target is None:
            target = item["id"]
    if target is None:
        print("NO_JIRA_CONFIGURED")
        return 1

    client = service.jira_client(target)
    probe = client.probe()
    print(f"\nprobe: {json.dumps(probe, ensure_ascii=False)[:300]}")

    # 1. What does this instance call its functional-context fields?
    located = client.locate_fields()
    print(f"\n=== 识别到的定位字段 {len(located)} 个 ===")
    for item in located:
        print(f"  {item['id']:<22} {item['name']}")

    # 2. A real issue: which of those carry text?
    issues = client.search(probe.get("default_jql") or "resolution = Unresolved ORDER BY updated DESC", limit=8)
    print(f"\n=== 候选工单 {len(issues)} 条 ===")
    for item in issues[:8]:
        print(f"  {item['key']:<16} {item['title'][:40]}  components={item.get('components')}")

    if not issues:
        print("NO_ISSUE")
        return 0

    key = issues[0]["key"]
    issue = client.issue(key, with_context=True)
    evidence = LocateEvidence.from_issue(issue)

    print(f"\n=== 工单 {key} 的定位证据 ===")
    print(f"  组件: {issue.get('components')}")
    print(f"  标签: {issue.get('labels')}")
    print(f"  自定义字段: {[(i['name'], i['text'][:40]) for i in issue.get('custom_fields') or []]}")
    print(f"  评论数: {len(issue.get('comments') or [])}")
    print(f"  附件: {[i['filename'] for i in issue.get('attachments') or []]}")
    print("\n  权重排序（前 6）:")
    for source in evidence.ranked()[:6]:
        print(f"    {source.weight:>4}  [{source.kind:<12}] {source.field[:30]:<32} {source.text[:50]!r}")
    if evidence.unread:
        print("  无法读取:")
        for note in evidence.unread:
            print(f"    - {note}")

    # 3. Locate against the mirror this requirement would route to.
    project = str(issue.get("project") or "")
    binding = resolve_project(project, {}) if project else None
    if binding is None or binding.web is None:
        print(f"\n  项目 {project} 未映射到前端仓，跳过定位")
        return 0
    root = Path(binding.web.mirror)
    print(f"\n=== 用这组证据定位（{binding.system} / {binding.web.key}）===")
    if not root.is_dir():
        print(f"  镜像不存在：{root}")
        return 0

    from octop.infra.pm.runtime.prototype_derive import locate_page

    with_evidence = locate_page(root, "", evidence=evidence)
    print(f"  ok={with_evidence.ok} page={with_evidence.page} method={with_evidence.method}")
    for note in with_evidence.notes:
        print(f"    · {note}")
    for candidate in with_evidence.candidates[:5]:
        print(f"    候选 {candidate.source_file}({candidate.score:.1f}) {candidate.reasons[:2]}")

    plain = locate_page(root, f"{issue.get('title')}\n{issue.get('description')}")
    print(f"\n  对照：只用标题+描述 -> ok={plain.ok} page={plain.page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
