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
