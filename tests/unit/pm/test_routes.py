"""Route-title lookup: the product's own words for a screen, not our guess.

The failure this guards against is the one that looks right: a route object
contains nested braces, so a lazy regex pairs this route's component with the
*next* route's title, and every row is then confidently wrong.
"""

from __future__ import annotations

from pathlib import Path

from octop.infra.pm.ports.routes import RouteIndex

ROUTES = """
export const routes = [
  { path: '/home', name: 'Home', component: () => import('@/views/home/index.vue'),
    meta: { title: '首页', icon: 'icon-home', requiresAuth: false } },
  { path: '/bi-analytics', name: 'Bi', component: () => import('@/views/bi-analytics/index.vue'),
    meta: { title: 'BI统计分析', permission: 'module:bi:bi' } },
  { path: '/order', name: 'Order', component: () => import('@/views/order/index.vue'),
    meta: { title: '订单中心' } },
]
"""


def _repo(tmp_path: Path, *, apps: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    payload = apps or {"control-tower": ROUTES}
    for app, body in payload.items():
        router = root / app / "src" / "router"
        router.mkdir(parents=True, exist_ok=True)
        (router / "routes.ts").write_text(body, encoding="utf-8")
    return root


def test_titles_are_paired_with_their_own_component(tmp_path: Path) -> None:
    index = RouteIndex.load(_repo(tmp_path))
    assert index is not None

    entry, candidates = index.resolve("订单中心")
    assert entry is not None and len(candidates) == 1
    assert entry.file == "views/order/index.vue"
    assert entry.app == "control-tower"
    # The neighbouring route's title must not leak in.
    assert index.lookup("订单中心")[0].file != "views/home/index.vue"


def test_a_title_in_two_apps_is_not_resolved(tmp_path: Path) -> None:
    """Same menu name in the tenant app and the admin app are different pages."""

    tenant = ROUTES.replace("'订单中心'", "'系统设置'")
    admin = tenant.replace("views/order", "views/settings")
    index = RouteIndex.load(_repo(tmp_path, apps={"control-tower": tenant, "platform-admin": admin}))
    assert index is not None

    entry, candidates = index.resolve("系统设置")
    assert entry is None
    assert {item.app for item in candidates} == {"control-tower", "platform-admin"}


def test_a_title_that_prefixes_other_paths_is_flagged_as_a_section(tmp_path: Path) -> None:
    """``/basic-data`` renders its first child; it is a section, not a page."""

    body = """
export const routes = [
  { path: '/basic-data', component: () => import('@/views/basic-data/ports/index.vue'),
    meta: { title: '基础资料维护' },
    children: [
      { path: '/basic-data/ports', component: () => import('@/views/basic-data/ports/index.vue'),
        meta: { title: '港口' } },
    ] },
]
"""
    index = RouteIndex.load(_repo(tmp_path, apps={"control-tower": body}))
    assert index is not None

    entry, _ = index.resolve("基础资料维护")
    assert entry is not None and entry.section is True
    leaf, _ = index.resolve("港口")
    assert leaf is not None and leaf.section is False


def test_duplicate_copies_of_a_router_are_ignored(tmp_path: Path) -> None:
    """macOS leaves "routes 2.ts" behind; no running app mounts it."""

    root = _repo(tmp_path)
    router = root / "control-tower" / "src" / "router"
    (router / "routes 2.ts").write_text(
        ROUTES.replace("订单中心", "订单中心").replace("views/order", "views/order-copy"),
        encoding="utf-8",
    )

    index = RouteIndex.load(root)
    assert index is not None
    entry, candidates = index.resolve("订单中心")
    assert entry is not None and len(candidates) == 1
    assert "copy" not in entry.file


def test_a_repo_without_a_router_returns_nothing(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert RouteIndex.load(tmp_path / "empty") is None


def test_titles_expose_what_the_mirror_calls_things(tmp_path: Path) -> None:
    index = RouteIndex.load(_repo(tmp_path))
    assert index is not None
    assert {"首页", "BI统计分析", "订单中心"} <= index.titles


def test_a_section_dash_function_component_resolves_within_its_section(tmp_path: Path) -> None:
    """``超级运价-运价维护`` is the Jira name for the rate-maintenance screen."""

    body = """
export const routes = [
  { path: '/saas', component: () => import('@/views/saas/rate-management/index.vue'),
    meta: { title: '超级运价' } },
  { path: '/saas/rate', component: () => import('@/views/saas/rate-management/index.vue'),
    meta: { title: '运价维护' } },
]
"""
    index = RouteIndex.load(_repo(tmp_path, apps={"control-tower": body}))
    assert index is not None

    found = index.match("超级运价-运价维护")
    assert found.reason == "compound"
    assert found.entry is not None
    assert found.entry.file == "views/saas/rate-management/index.vue"


def test_a_compound_name_does_not_reach_outside_its_section(tmp_path: Path) -> None:
    """A same-named menu in another part of the product is not the answer."""

    body = """
export const routes = [
  { path: '/saas', component: () => import('@/views/saas/rate-management/index.vue'),
    meta: { title: '超级运价' } },
  { path: '/shipping', component: () => import('@/views/shipping/rate/index.vue'),
    meta: { title: '运价维护' } },
]
"""
    index = RouteIndex.load(_repo(tmp_path, apps={"control-tower": body}))
    assert index is not None

    found = index.match("超级运价-运价维护")
    assert found.entry is None
    assert found.reason == "none"


def test_a_name_that_is_not_a_route_stays_unresolved(tmp_path: Path) -> None:
    """``全局`` marks a scope, not a screen; inventing a page would be worse."""

    index = RouteIndex.load(_repo(tmp_path))
    assert index is not None
    found = index.match("全局")
    assert found.entry is None
    assert found.reason == "none"


def test_the_typo_form_at_views_is_normalised_and_size_is_recorded(tmp_path: Path) -> None:
    """The mirror really contains ``@views/...`` — a strict ``@/`` strip breaks it."""

    body = """
export const routes = [
  { path: '/customer', component: () => import('@views/customer/index.vue'),
    meta: { title: '客商中心' } },
]
"""
    root = tmp_path / "repo"
    router = root / "control-tower" / "src" / "router"
    router.mkdir(parents=True)
    (router / "routes.ts").write_text(body, encoding="utf-8")
    view = root / "control-tower" / "src" / "views" / "customer" / "index.vue"
    view.parent.mkdir(parents=True)
    view.write_text("<template><div/></template>", encoding="utf-8")

    index = RouteIndex.load(root)
    assert index is not None
    entry, _ = index.resolve("客商中心")
    assert entry is not None
    assert entry.file == "views/customer/index.vue"
    assert entry.size == view.stat().st_size


def test_the_preferred_app_settles_a_tenant_versus_admin_duplicate(tmp_path: Path) -> None:
    """A CargoWareX ticket is a tenant ticket, so the tenant page is the answer."""

    tenant = ROUTES.replace("'订单中心'", "'系统设置'")
    admin = tenant.replace("views/order", "views/settings")
    index = RouteIndex.load(
        _repo(tmp_path, apps={"control-tower": tenant, "platform-admin": admin})
    )
    assert index is not None

    entry, candidates = index.resolve("系统设置", prefer=("control-tower",))
    assert entry is not None
    assert entry.app == "control-tower"
    # The other app's route is still reported, so the choice stays auditable.
    assert {item.app for item in candidates} == {"control-tower", "platform-admin"}


def test_the_preferred_app_cannot_settle_two_routes_inside_itself(tmp_path: Path) -> None:
    """Preference picks between apps. It must never pick between two pages."""

    body = ROUTES.replace("views/home", "views/home/mobile").replace(
        "订单中心", "系统设置"
    ) + """
export const extra = [
  { path: '/settings', component: () => import('@/views/settings/index.vue'),
    meta: { title: '系统设置' } },
]
"""
    index = RouteIndex.load(_repo(tmp_path, apps={"control-tower": body}))
    assert index is not None

    entry, _ = index.resolve("系统设置", prefer=("control-tower",))
    assert entry is None


def test_a_layout_routed_as_home_does_not_beat_the_real_home_page(tmp_path: Path) -> None:
    """``/`` renders MainLayout and is often titled 首页; it is a frame, not a screen."""

    body = """
export const routes = [
  { path: '/', component: () => import('@/layouts/MainLayout.vue'),
    meta: { title: '首页' } },
  { path: '/home', component: () => import('@/views/home/index.vue'),
    meta: { title: '首页' } },
]
"""
    index = RouteIndex.load(_repo(tmp_path, apps={"control-tower": body}))
    assert index is not None

    entry, candidates = index.resolve("首页")
    assert entry is not None and entry.file == "views/home/index.vue"
    assert len(candidates) == 2, "the layout stays in the evidence, not in the answer"


VUE2_SEA = """
import Layout from '@/layout'
export default {
  path: '/SeaExport',
  component: Layout,
  meta: { title: 'seaExport' },
  children: [
    { path: 'BookingNew',
      component: () => import('@/views/cargo/v0/SeaExport/bookingNew/index.vue'),
      meta: { title: '订舱中心' } },
  ]
}
"""

VUE2_FBA = """
export default {
  path: '/FBA',
  component: Layout,
  meta: { title: 'FBA' },
  children: [
    { path: 'Warehousing',
      component: () => import('@/views/cargo/v0/FBA/Warehousing/index.vue'),
      meta: { title: '国内仓' } },
  ]
}
"""

VUE2_PRICE = """
export default {
  path: '/Price',
  component: Layout,
  meta: { title: 'price' },
  children: [
    { path: 'Quotation',
      component: () => import('@/views/cargo/v0/Price/Quotation/index.vue'),
      meta: { title: '报价管理' } },
  ]
}
"""


def _vue2_repo(tmp_path: Path) -> Path:
    root = tmp_path / "cargoware-h5"
    base = root / "src" / "router" / "modules" / "cargo" / "v0"
    for module, body in (("SeaExport", VUE2_SEA), ("FBA", VUE2_FBA), ("Price", VUE2_PRICE)):
        directory = base / module
        directory.mkdir(parents=True)
        (directory / "index.js").write_text(body, encoding="utf-8")
    return root


def test_a_single_app_repo_is_read_from_a_nested_module_router(tmp_path: Path) -> None:
    """Vue 2 keeps one router file per module; reading only the first level finds nothing."""

    index = RouteIndex.load(_vue2_repo(tmp_path))
    assert index is not None
    assert "订舱中心" in index.titles
    entry, _ = index.resolve("国内仓")
    assert entry is not None
    assert entry.scope == "FBA"


def test_a_vue2_compound_name_resolves_inside_its_own_module(tmp_path: Path) -> None:
    index = RouteIndex.load(_vue2_repo(tmp_path))
    assert index is not None

    found = index.match("FBA-国内仓")
    assert found.reason == "compound"
    assert found.entry is not None
    assert found.entry.file == "views/cargo/v0/FBA/Warehousing/index.vue"


def test_a_vue2_compound_name_does_not_borrow_another_module(tmp_path: Path) -> None:
    """报价管理 lives in Price, so ``FBA-报价管理`` is not that page."""

    index = RouteIndex.load(_vue2_repo(tmp_path))
    assert index is not None

    found = index.match("FBA-报价管理")
    assert found.entry is None


def test_repo_path_is_relative_to_the_repo_root(tmp_path: Path) -> None:
    """The deriver reads ``root / page`` and hands it to Vite; ``@/`` means nothing."""

    monorepo = RouteIndex.load(_repo(tmp_path))
    assert monorepo is not None
    entry, _ = monorepo.resolve("订单中心")
    assert entry is not None
    assert monorepo.repo_path(entry) == "control-tower/src/views/order/index.vue"

    single = RouteIndex.load(_vue2_repo(tmp_path))
    assert single is not None
    leaf, _ = single.resolve("国内仓")
    assert leaf is not None
    assert single.repo_path(leaf) == "src/views/cargo/v0/FBA/Warehousing/index.vue"


def test_two_routes_onto_the_same_file_are_an_alias_not_a_tie(tmp_path: Path) -> None:
    """CargoWare routes 首页 twice, both to dashboard/index.vue. That is one screen."""

    body = """
export const routes = [
  { path: '/', component: () => import('@/views/dashboard/index.vue'),
    meta: { title: '首页' } },
  { path: '/dashboard', component: () => import('@/views/dashboard/index.vue'),
    meta: { title: '首页' } },
]
"""
    index = RouteIndex.load(_repo(tmp_path, apps={"control-tower": body}))
    assert index is not None

    entry, candidates = index.resolve("首页")
    assert entry is not None and entry.file == "views/dashboard/index.vue"
    assert len(candidates) == 2, "both routes stay visible as evidence"
