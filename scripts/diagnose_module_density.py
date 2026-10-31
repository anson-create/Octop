"""Does normalising module heat by module size fix the big-module bias?

bpoTask scores 11.0 against a 3,000-entry module; contract-parsing scores 8.0
against a few hundred. Raw sums always favour the big module.
"""

import math
import re
from collections import Counter
from pathlib import Path

from octop.infra.pm.ports.i18n import I18nIndex

ROOT = Path("/opt/pm-mirror/cwx-platform-admin")
TEXTS = {
    "企业客户": "企业客户管理：企业列表支持按企业名称搜索、按客户状态筛选，展示企业编码与创建时间",
    "供应商": "派单供应商管理：在供应商列表增加就绪状态与路由就绪筛选，支持按供应商代码查询",
    "合同解析": "合同解析任务：上传合同文件后自动解析，展示解析任务列表与失败原因",
    "反馈审核": "反馈平台一审：运营对用户反馈做初审，标记分类与处理状态",
}


def kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


idx = I18nIndex.load(ROOT)
size = Counter(e.module for e in idx.entries)
views = ROOT / "src" / "views"

for label, text in TEXTS.items():
    heat = idx.module_heat(text)
    print(f"\n=== {label} ===")
    ranked = []
    for module, score in heat.items():
        n = max(size[module], 1)
        density = score / math.sqrt(n)
        slug = kebab(module)
        page = ""
        for probe in (views / slug, views / f"{slug}-management"):
            if probe.is_dir():
                idxs = sorted(p.relative_to(ROOT).as_posix() for p in probe.rglob("index.vue"))
                page = idxs[0] if idxs else ""
                break
        ranked.append((density, module, score, n, page))
    ranked.sort(reverse=True)
    for density, module, score, n, page in ranked[:6]:
        mark = "PAGE" if page else "    "
        print(f"  {mark} {density:5.3f}  {module:22s} raw={score:5.1f} n={n:5d}  {page}")
