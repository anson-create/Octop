"""Does the i18n module name point at a page directory?

If `company.ts` implies `src/views/company-management/`, that is a far stronger
signal than matching a shared function name.
"""

import re
from pathlib import Path

from octop.infra.pm.ports.i18n import I18nIndex

ROOT = Path("/opt/pm-mirror/cwx-platform-admin")
TEXTS = {
    "企业客户": "企业客户管理：企业列表支持按企业名称搜索、按客户状态筛选，展示企业编码与创建时间",
    "供应商": "派单供应商管理：在供应商列表增加就绪状态与路由就绪筛选，支持按供应商代码查询",
    "合同解析": "合同解析任务：上传合同文件后自动解析，展示解析任务列表与失败原因",
}


def kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


idx = I18nIndex.load(ROOT)
views = ROOT / "src" / "views"

for label, text in TEXTS.items():
    heat = idx.module_heat(text)
    print(f"\n=== {label} ===")
    print(f"  heat top: {list(heat.items())[:8]}")
    hits = []
    for module, score in list(heat.items())[:12]:
        slug = kebab(module)
        for probe in (views / slug, views / f"{slug}-management"):
            if probe.is_dir():
                pages = sorted(p.relative_to(ROOT).as_posix() for p in probe.rglob("index.vue"))
                hits.append((probe.relative_to(ROOT).as_posix(), score, pages[:2]))
    for item in hits[:6]:
        print(f"  HIT {item[0]} (score {item[1]:.1f}) -> {item[2]}")
    if not hits:
        print("  no module matched a page directory")
