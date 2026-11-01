"""The prototype must be derived from source, and its fidelity measurable.

These cover the two ways the old prototype lied: it invented class names (so
``class_overlap`` collapsed) and it claimed a palette the product never used (so
the token table had to come from the product's own CSS, not from the model).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from octop.infra.pm.domain.repos import REPOS
from octop.infra.pm.runtime import prototype_source
from octop.infra.pm.runtime.prototype_source import (
    PrototypeDeriver,
    SsrRenderer,
    _collect_css,
    extract_classes,
    extract_tokens,
)

PAGE = "/src/views/customer/company-management/index.vue"

PAGE_SFC = """
<template>
  <div class="page-container">
    <a-card class="search-card">
      <a-input class="filter-input" />
    </a-card>
    <div :class="{ active: isActive }" class="stats-grid">x</div>
  </div>
</template>

<style scoped lang="scss">
.page-container {
  padding: $spacing-lg;
  color: $color-text;
}
</style>
"""


def _repo(tmp_path: Path, *, built: bool) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "views" / "customer" / "company-management").mkdir(parents=True)
    (root / PAGE.lstrip("/")).write_text(PAGE_SFC, encoding="utf-8")
    root.joinpath("src", "styles").mkdir(parents=True, exist_ok=True)
    (root / "src" / "styles" / "variables.scss").write_text(
        "$color-primary: #165DFF;\n", encoding="utf-8"
    )
    if built:
        (root / "dist" / "assets").mkdir(parents=True)
        (root / "dist" / "assets" / "index-abc.css").write_text(
            ":root{--color-primary:#165DFF}.page-container{padding:24px}", encoding="utf-8"
        )
    return root


def test_static_classes_are_collected_and_dynamic_ones_are_not() -> None:
    classes = extract_classes(PAGE_SFC)

    assert {"page-container", "search-card", "filter-input", "stats-grid"} <= classes
    # `{ active: isActive }` is a binding, not a name; counting it would inflate
    # the overlap score with strings that never appear in the DOM.
    assert "active" not in classes
    assert "isActive" not in classes


def test_tokens_come_from_real_css() -> None:
    tokens = extract_tokens(":root{--color-primary:#165DFF;--radius:8px}")

    assert tokens == {"--color-primary": "#165DFF", "--radius": "8px"}


def test_built_styles_win_over_raw_scss(tmp_path: Path) -> None:
    root = _repo(tmp_path, built=True)
    parts, sources, notes = _collect_css(root, REPOS["cwx-web"], PAGE, PAGE_SFC)

    assert sources == ["dist/assets/index-abc.css"]
    assert "#165DFF" in parts[0]
    assert any("构建产物" in note for note in notes)


def test_raw_scss_is_never_inlined_as_css(tmp_path: Path) -> None:
    """`$color-primary: #165DFF` in a <style> tag is discarded by the browser."""

    root = _repo(tmp_path, built=False)
    parts, _sources, notes = _collect_css(root, REPOS["cwx-web"], PAGE, PAGE_SFC)
    joined = "\n".join(parts)

    assert "$color-primary" not in joined
    assert "$spacing-lg" not in joined
    assert any("SCSS" in note for note in notes)


def test_plain_css_style_block_is_kept(tmp_path: Path) -> None:
    root = _repo(tmp_path, built=False)
    sfc = '<template><div class="a" /></template><style>.a { color: red }</style>'
    parts, _sources, _notes = _collect_css(root, REPOS["cwx-web"], PAGE, sfc)

    assert any(".a { color: red }" in part for part in parts)


def test_page_style_survives_the_dedicated_bundle(tmp_path: Path) -> None:
    """The dedicated bundle has the library and Tailwind, but not this page's grid."""

    root = _repo(tmp_path, built=False)
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "pm-prototype.css").write_text(".arco-card{padding:8px}", encoding="utf-8")
    sfc = '<template><div class="a" /></template><style>.stats-grid { display: grid }</style>'

    parts, sources, notes = _collect_css(root, REPOS["cwx-web"], PAGE, sfc)
    joined = "\n".join(parts)

    assert "dist/pm-prototype.css" in sources
    assert ".stats-grid { display: grid }" in joined
    assert any("构建产物" in note or "pm-prototype.css" in note for note in notes)


def test_renderer_reports_a_missing_script_instead_of_rendering(tmp_path: Path) -> None:
    renderer = SsrRenderer(tmp_path / "nope.mjs")

    assert renderer.available is False
    result = renderer.render(tmp_path, PAGE)
    assert result.ok is False
    assert "missing" in result.message


def test_renderer_reports_a_missing_mirror_before_spawning_node(tmp_path: Path) -> None:
    script = tmp_path / "render.mjs"
    script.write_text("// noop\n", encoding="utf-8")
    renderer = SsrRenderer(script)

    result = renderer.render(tmp_path / "absent", PAGE)
    assert result.ok is False
    assert "mirror missing" in result.message


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_vue2_renderer_fails_in_the_shape_the_caller_parses(tmp_path: Path) -> None:
    """A renderer that crashes unparsed is indistinguishable from a timeout.

    This machine has no node_modules for CargoWare and no network to install
    them, so the render itself cannot be exercised here. What *can* be pinned is
    the contract the Python side depends on: whatever happens, the script exits
    with one JSON object and a message naming the missing piece — not a stack
    trace that leaves ``SsrRenderer`` reporting "ssr produced no json".
    """

    assets = Path(prototype_source.__file__).parent / "assets"
    renderer = SsrRenderer(assets / "render.vue2.mjs", timeout_s=60)

    result = renderer.render(tmp_path, PAGE)

    assert result.ok is False
    assert result.html == ""
    assert "no json" not in result.message
    assert any(
        name in result.message for name in ("vite", "vue-server-renderer", "Vue 2 SFC plugin")
    )


# --- the Vue 2 renderer's own wiring ----------------------------------------
#
# The real toolchain (vite + vite-plugin-vue2 + vue-server-renderer) cannot be
# installed here: the mirror has no node_modules and there is no network. These
# stand-ins are not a substitute for rendering a real page — they are a check
# that the *script* calls things in the right order and speaks the JSON its
# caller parses, which is the part that would otherwise be pure assertion.

_TOOLCHAIN = {
    "vite/package.json": '{"name":"vite","type":"module","main":"index.js"}',
    "vite/index.js": """
const Page = { name: 'FakePage', render: (h) => h('div', { class: 'page-container' }, 'hi') }

function FakeVue(options) { this.$options = options }
FakeVue.version = '2.6.10'
FakeVue.config = {}
FakeVue.use = () => {}

export async function createServer() {
  return {
    async ssrLoadModule(spec) {
      if (spec === 'vue') return { default: FakeVue }
      if (spec === 'vue-template-compiler') return { default: { version: '2.6.10' } }
      if (spec === 'element-ui') return { default: { install() {} } }
      if (spec === 'vue-router') return { default: class Router { constructor() {} } }
      if (spec === '/src/views/x.vue') return { default: Page }
      throw new Error('not a module: ' + spec)
    },
    async close() {},
  }
}
""",
    "vite-plugin-vue2/package.json": '{"name":"vite-plugin-vue2","type":"module","main":"index.js"}',
    "vite-plugin-vue2/index.js": "export default function vue2() { return { name: 'vite:vue2' } }\n",
    "vue-server-renderer/package.json": (
        '{"name":"vue-server-renderer","type":"module","main":"index.js"}'
    ),
    "vue-server-renderer/index.js": """
export function createRenderer() {
  return {
    async renderToString() {
      return '<div class="page-container" data-ssr="1">hi</div>'
    },
  }
}
""",
}


def _write_tree(base: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_vue2_renderer_renders_once_a_toolchain_is_present(tmp_path: Path) -> None:
    """The orchestration is testable even when Vue itself is not installed.

    Places the real script with the real `ensure_render_script`, gives it a
    toolchain that answers the way Vite does, and checks the page's own markup
    comes back. What this does *not* prove is that Vite and vue-server-renderer
    behave like these stand-ins — only that the script asks them for the right
    things in the right order.
    """

    from octop.infra.pm.runtime.prototype_derive import ensure_render_script

    tool = tmp_path / "tool"
    script = ensure_render_script(tool, "render.vue2.mjs")
    assert script is not None
    _write_tree(tool / "node_modules", _TOOLCHAIN)

    mirror = tmp_path / "mirror"
    (mirror / "src").mkdir(parents=True)

    result = SsrRenderer(script, timeout_s=60).render(mirror, "src/views/x.vue")

    assert result.ok is True
    assert "page-container" in result.html
    assert "vue:2.6.10" in result.components
    # This mirror has no `node_modules`, so Element UI can only arrive through
    # the module graph — the fallback route, labelled as such.
    assert "element-ui:ssr" in result.components
    assert "plugin:vite-plugin-vue2" in result.components


def test_element_ui_skips_vite_when_the_mirror_carries_a_real_package(tmp_path: Path) -> None:
    """The production route has to be the native one.

    Vite 2 evaluates every module as ESM, so a CommonJS `element-ui` loaded
    through it dies with "module is not defined" and every `el-*` tag renders as
    an unknown element — a prototype that looks structurally right and is
    quietly wrong. This pins the preference: when the mirror has the package,
    it is required by Node, and the result says so.
    """

    from octop.infra.pm.runtime.prototype_derive import ensure_render_script

    tool = tmp_path / "toolchain"
    tool.mkdir()
    script = ensure_render_script(tool, "render.vue2.mjs")
    assert script is not None
    _write_tree(tool / "node_modules", _TOOLCHAIN)

    mirror = tmp_path / "mirror-with-deps"
    (mirror / "src/views").mkdir(parents=True)
    (mirror / "package.json").write_text('{"name":"mirror"}', encoding="utf-8")
    _write_tree(
        mirror / "node_modules",
        {
            "element-ui/package.json": '{"name":"element-ui","main":"index.js"}',
            "element-ui/index.js": (
                "module.exports = { install: function (Vue) { Vue.__elementUi = true } };\n"
            ),
        },
    )

    result = SsrRenderer(script, timeout_s=60).render(mirror, "src/views/x.vue")

    assert result.ok is True
    assert "element-ui:native" in result.components
    assert "element-ui:ssr" not in result.components


def test_derivation_failure_never_claims_exact_fidelity(tmp_path: Path) -> None:
    """A failed render must not be dressed up as a 1:1 prototype."""

    root = _repo(tmp_path, built=True)
    spec = REPOS["cwx-web"]
    object.__setattr__(spec, "mirror", str(root))
    try:
        deriver = PrototypeDeriver(SsrRenderer(tmp_path / "absent.mjs"))
        derived = deriver.derive(spec, PAGE)
    finally:
        object.__setattr__(spec, "mirror", "/opt/pm-mirror/cw-web")

    assert derived.derived is False
    assert derived.fidelity == "partial"
    assert derived.html == ""
    assert "未能从源码派生" in derived.fidelity_note
