"""Live check: locate a page in the real mirror, then derive it.

Usage: verify_derive_live.py "<requirement text>"
Runs against the deployed venv so it exercises exactly what the service runs.
"""

import json
import sys
import time
from pathlib import Path

from octop.infra.pm.domain.repos import REPOS
from octop.infra.pm.runtime.prototype_derive import derive_from_source, locate_page

ROOT = Path("/opt/pm-mirror/cwx-platform-admin")
TEXT = sys.argv[1] if len(sys.argv) > 1 else (
    "派单供应商管理：在供应商列表增加就绪状态与路由就绪筛选，"
    "支持按供应商代码查询，列表展示供应商名称、状态、更新时间。"
)

print(f"=== QUERY: {TEXT[:60]} ===")
t0 = time.time()
location = locate_page(ROOT, TEXT)
elapsed = time.time() - t0
print(json.dumps(location.as_dict(), ensure_ascii=False, indent=2))
print(f"locate took {elapsed:.1f}s")

if not location.ok:
    print("\n=== NO PAGE: derive stops here, the step must say so ===")
    spec = REPOS["cwx-web"]
    outcome = derive_from_source(spec, TEXT, timeout_s=240)
    print(json.dumps({"ok": outcome.ok, "reason": outcome.reason}, ensure_ascii=False, indent=2))
    raise SystemExit(0)

spec = REPOS["cwx-web"]
print(f"\n=== DERIVE {spec.mirror} :: {location.page} ===")
t1 = time.time()
outcome = derive_from_source(spec, TEXT, timeout_s=240)
took = time.time() - t1

payload = {
    "ok": outcome.ok,
    "reason": outcome.reason,
    "page": outcome.page,
    "class_overlap": round(outcome.class_overlap, 3),
    "derived": bool(outcome.prototype and outcome.prototype.derived),
    "fidelity": outcome.prototype.fidelity if outcome.prototype else "",
    "note": outcome.prototype.fidelity_note if outcome.prototype else "",
    "html_len": len(outcome.prototype.html) if outcome.prototype else 0,
    "styles_inlined": outcome.prototype.styles_inlined if outcome.prototype else 0,
    "classes_source": outcome.prototype.classes_source if outcome.prototype else 0,
    "errors": list(outcome.prototype.errors) if outcome.prototype else [],
    "seconds": round(took, 1),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))

if outcome.prototype and outcome.prototype.html:
    out = Path("/tmp/derived-live.html")
    out.write_text(outcome.prototype.html, encoding="utf-8")
    print(f"\nwrote {out} ({out.stat().st_size} bytes)")
