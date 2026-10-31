"""Diagnose why a search hit no renderable file: what kinds of file do the terms land in?"""

import json
from collections import Counter
from pathlib import Path

from octop.infra.pm.ports.graph import GraphIndex
from octop.infra.pm.ports.i18n import I18nIndex

ROOT = Path("/opt/pm-mirror/cwx-platform-admin")
TEXT = (
    "派单供应商管理：在供应商列表增加就绪状态与路由就绪筛选，"
    "支持按供应商代码查询，列表展示供应商名称、状态、更新时间。"
)

graph = GraphIndex.load(ROOT)
i18n = I18nIndex.load(ROOT)
print(f"graph: {graph.file_count} files, {len(graph.nodes)} nodes")

terms = i18n.terms(TEXT, limit=8)
print(f"terms: {terms}")

hits = graph.search(terms, limit=40)
print(f"\nhits: {len(hits)}")
for hit in hits[:12]:
    print(f"  {hit.score:6.2f}  {hit.source_file}")

print("\nsuffix distribution of all hits:")
print(Counter(Path(h.source_file).suffix for h in hits).most_common())

# Do the term-bearing symbols live in .ts, and does a page import them?
print("\nfirst 3 hit files: who imports them?")
for hit in hits[:3]:
    importers = graph.importers(hit.source_file, max_files=5)
    print(f"  {hit.source_file}")
    for imp in importers:
        print(f"      <- {imp}")
