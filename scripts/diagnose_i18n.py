"""Why did '企业' not become a search term?"""

from pathlib import Path

from octop.infra.pm.ports.i18n import I18nIndex

ROOT = Path("/opt/pm-mirror/cwx-platform-admin")
TEXT = "企业客户管理：企业列表支持按企业名称搜索、按客户状态筛选，展示企业编码与创建时间"

idx = I18nIndex.load(ROOT)
print(f"catalogue files: {len(idx.files_scanned)}  entries: {len(idx)}")

matches = idx.match(TEXT, limit=24)
print(f"\nmatches (limit=24): {len(matches)}")
for m in matches[:15]:
    print(f"  {m.score:5.2f}  {m.entry.module:28s} {m.entry.key:28s} {m.entry.value}")

print(f"\nterms(limit=8): {idx.terms(TEXT, limit=8)}")
print(f"module_heat: {list(idx.module_heat(TEXT).items())[:6]}")
