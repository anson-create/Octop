"""Does a Jira component name actually locate a page in the real mirror?

This tests the premise behind routing on ``components``, separately from whether
any one ticket fills that field. Runs against the live cwx-platform-admin mirror
and its real code graph — no fakes.

Each case supplies only what a component field would supply (a short area name)
and asks where the locator lands. A refusal is a valid, informative outcome; a
confident wrong page is the failure we are measuring for.
"""

from __future__ import annotations

import sys
from pathlib import Path

MIRROR = Path("/opt/pm-mirror/cwx-platform-admin")

sys.path.insert(0, "/opt/octop/venv/lib/python3.12/site-packages")

CASES = [
    ("企业客户管理", "expect company-management"),
    ("派单供应商管理", "no such page in this repo"),
    ("运价指数", "unknown"),
    ("报关单管理", "unknown"),
    ("单证管理", "unknown"),
    ("用户权限管理", "unknown"),
    ("费用管理", "unknown"),
]


def main() -> int:
    from octop.infra.pm.domain.locate_evidence import (
        WEIGHT_COMPONENT,
        WEIGHT_DESCRIPTION,
        EvidenceSource,
        LocateEvidence,
    )
    from octop.infra.pm.runtime.prototype_derive import locate_page

    if not (MIRROR / "graphify-out" / "graph.json").is_file():
        print(f"NO_GRAPH at {MIRROR}")
        return 1

    for component, note in CASES:
        evidence = LocateEvidence(
            sources=(
                EvidenceSource("component", "Jira 组件", component, WEIGHT_COMPONENT),
                # A body that says nothing about where — the common real case.
                EvidenceSource(
                    "description",
                    "描述",
                    "运营反馈目前流程不便，希望能优化，具体交互待确认。",
                    WEIGHT_DESCRIPTION,
                ),
            )
        )
        location = locate_page(MIRROR, "", evidence=evidence)
        first = location.candidates[0] if location.candidates else None
        print(f"\n### 组件「{component}」  （{note}）")
        print(f"    ok={location.ok}  page={location.page or '(拒绝)'}")
        if first is not None:
            print(f"    score={first.score:.1f}  labels={list(first.matched_labels)[:3]}")
        for candidate in location.candidates[1:4]:
            print(f"    次选 {candidate.source_file}({candidate.score:.1f})")
        for text in location.notes[:3]:
            print(f"    · {text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
