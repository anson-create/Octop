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
    SourceBaseline,
    SsrRenderer,
    _collect_css,
    anchor_overlap,
    expand_baseline,
    extract_classes,
    extract_tokens,
    read_baseline,
    read_i18n_table,
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


# --- reading the source a prototype is transcribed from -----------------------

SFC = """<template>
  <div class="vendor-list">
    <el-form class="filter-bar"><el-input v-model="kw" /></el-form>
    <el-table class="grid" :data="rows" />
  </div>
</template>

<script>
export default {
  data() {
    return { kw: '', rows: [] }
  },
}
</script>

<style lang="scss" scoped>
.vendor-list { padding: 12px; }
</style>
"""


def test_a_page_is_read_as_three_separate_blocks(tmp_path: Path) -> None:
    """Template, script and style answer different questions and stay apart.

    The template is what gets transcribed, the script says which state it binds
    to, the style decides spacing. Flattening them into one blob makes the model
    treat CSS declarations as structure.
    """

    (tmp_path / "views").mkdir()
    (tmp_path / "views" / "vendor.vue").write_text(SFC, encoding="utf-8")

    baseline = read_baseline(tmp_path, "views/vendor.vue", "cwx-web")

    assert baseline.usable is True
    assert "vendor-list" in baseline.template
    assert "kw: ''" in baseline.script
    assert "padding: 12px" in baseline.style
    # Scoped and lang attributes must not hide the blocks.
    assert baseline.style
    assert baseline.classes == ("filter-bar", "grid", "vendor-list")
    assert baseline.components == ("el-form", "el-input", "el-table")
    assert baseline.note == ""


def test_a_page_without_a_template_falls_back_to_the_whole_file(tmp_path: Path) -> None:
    """A .tsx page has no SFC blocks; its JSX *is* the template."""

    (tmp_path / "page.tsx").write_text(
        'export default () => <div className="grid"><table /></div>', encoding="utf-8"
    )

    baseline = read_baseline(tmp_path, "page.tsx", "cwx-web")

    assert baseline.usable is True
    assert "className" in baseline.template
    assert "没有 <template> 块" in baseline.note


def test_a_huge_template_is_truncated_and_says_so(tmp_path: Path) -> None:
    """A silently cut template yields a prototype missing its lower half.

    Worse than a short one, because nothing on the artifact hints that the source
    was clipped — so the cut is recorded in the note.
    """

    body = '<div class="a">' + ("<p>x</p>" * 2000) + "</div>"
    (tmp_path / "big.vue").write_text(f"<template>{body}</template>", encoding="utf-8")

    baseline = read_baseline(tmp_path, "big.vue", "cwx-web", max_chars=500)

    assert len(baseline.template) == 500
    assert "截到 500 字" in baseline.note


def test_an_unreadable_page_is_not_a_baseline(tmp_path: Path) -> None:
    baseline = read_baseline(tmp_path, "nope.vue", "cwx-web")

    assert isinstance(baseline, SourceBaseline)
    assert baseline.usable is False
    assert "读取失败" in baseline.note


# --- flattening a shell page into the components that draw it ----------------
#
# In CARGO the located file is usually a frame: bookingNew.vue is 770 lines and
# its <template> is 1361 characters holding one class and four custom tags. The
# screen is drawn by booking-tree-grid (456 KB) and booking-workspace (352 KB),
# and the grid inside that is another component again. A baseline taken from the
# frame alone transcribes to an empty box — so these cover the flattening, the
# budget it has to respect, and the copy anchors that make fidelity measurable.


def _cargo_mirror(tmp_path: Path) -> Path:
    """A miniature CARGO: shell page, deep component, language pack, forwarder."""

    root = tmp_path / "cargo"
    views = root / "src" / "views" / "cargo" / "v0" / "SeaExport"
    booking = views / "components" / "booking"
    booking.mkdir(parents=True)

    (views / "bookingNew.vue").write_text(
        """<template>
  <div class="app-main-content">
    <booking-tree-grid v-if="!openMultiple" />
    <el-button @click="save">{{ $t('CargoMsg.Cargo_Save') }}</el-button>
  </div>
</template>

<script>
import bookingTreeGrid from './components/booking/bookingTreeGrid'
export default { name: 'BookingNew', components: { bookingTreeGrid } }
</script>
""",
        encoding="utf-8",
    )
    (booking / "bookingTreeGrid.vue").write_text(
        """<template>
  <div class="tree-grid">
    <el-form label-width="90px">
      <el-form-item :label="$t('CargoMsg.Cargo_AccountPeriod')">
        <el-input v-model="form.period" />
      </el-form-item>
      <el-form-item :label="$t('CargoMsg.Cargo_AirlineCode')">
        <el-input v-model="form.airline" />
      </el-form-item>
    </el-form>
    <cargo-ag-grid :columns="columns" />
  </div>
</template>

<script>
import cargoAgGrid from './cargoAgGrid'
export default { name: 'BookingTreeGrid', components: { cargoAgGrid } }
</script>
""",
        encoding="utf-8",
    )
    (booking / "cargoAgGrid.vue").write_text(
        '<template><div class="ag-host"><ag-grid-vue :columnDefs="cols" /></div></template>',
        encoding="utf-8",
    )
    fba = root / "src" / "views" / "cargo" / "v0" / "FBA"
    fba.mkdir(parents=True)
    (fba / "fbaBookingNew.vue").write_text(
        """<script>
import bookingNew from '@/views/cargo/v0/SeaExport/bookingNew'

export default { name: 'FBABookingNew', extends: bookingNew }
</script>
""",
        encoding="utf-8",
    )
    lang = root / "src" / "lang" / "zh_CN"
    lang.mkdir(parents=True)
    (lang / "CargoMsg.js").write_text(
        "export default {\n"
        "  Cargo_Save: '保存',\n"
        "  Cargo_AccountPeriod: '账期',\n"
        "  Cargo_AirlineCode: '航线代码',\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def test_a_shell_page_expands_into_the_components_that_draw_it(tmp_path: Path) -> None:
    """The frame alone is not the screen, so the baseline carries both."""

    root = _cargo_mirror(tmp_path)

    plain = read_baseline(root, "src/views/cargo/v0/SeaExport/bookingNew.vue", "cw-web")
    # The frame names the component but contains none of what it draws.
    assert "ag-host" not in plain.template
    assert plain.thin is True

    baseline = expand_baseline(root, "src/views/cargo/v0/SeaExport/bookingNew.vue", "cw-web")

    assert baseline.usable is True
    assert baseline.thin is False
    assert "tree-grid" in baseline.template
    assert "ag-host" in baseline.template
    assert len(baseline.expanded) == 2
    assert baseline.expanded[0].endswith("bookingTreeGrid.vue")
    assert "子组件展开" in baseline.template
    assert "已展开 2 个子组件" in baseline.note


def test_a_forwarding_shell_is_followed_to_the_page_it_re_exports(tmp_path: Path) -> None:
    """14 lines that import another page have no template of their own."""

    root = _cargo_mirror(tmp_path)

    baseline = expand_baseline(root, "src/views/cargo/v0/FBA/fbaBookingNew.vue", "cw-web")

    assert baseline.shell is True
    assert baseline.page.endswith("SeaExport/bookingNew.vue")
    assert "app-main-content" in baseline.template
    assert "转发壳" in baseline.note
    # The old code handed the forwarder's <script> back as a template and still
    # reported it usable; that page scored 0% and blamed the model for it.
    assert "<script>" not in baseline.template


def test_library_tags_are_not_expanded_but_local_ones_are(tmp_path: Path) -> None:
    """`el-form` has no readable source; a project component does."""

    root = _cargo_mirror(tmp_path)
    baseline = expand_baseline(root, "src/views/cargo/v0/SeaExport/bookingNew.vue", "cw-web")

    assert all("el-" not in rel for rel in baseline.expanded)
    assert any("bookingTreeGrid" in rel for rel in baseline.expanded)


def test_expansion_respects_the_budget_and_reports_every_cut(tmp_path: Path) -> None:
    """A silent cut yields a prototype confidently missing its lower half."""

    root = _cargo_mirror(tmp_path)
    # A real grid component is 456 KB; the fixture has to outgrow the budget for
    # the cut to happen at all.
    grid = root / "src/views/cargo/v0/SeaExport/components/booking/bookingTreeGrid.vue"
    filler = "\n".join(
        f'<div class="row-{i}"><span>{{{{ $t(\'CargoMsg.Cargo_AccountPeriod\') }}}}</span></div>'
        for i in range(200)
    )
    grid.write_text(f'<template><div class="tree-grid">{filler}</div></template>', encoding="utf-8")

    full = expand_baseline(root, "src/views/cargo/v0/SeaExport/bookingNew.vue", "cw-web")
    tight = expand_baseline(
        root, "src/views/cargo/v0/SeaExport/bookingNew.vue", "cw-web", max_chars=2000
    )

    assert len(tight.template) < len(full.template)
    assert "截断" in tight.note


def test_i18n_keys_become_copy_anchors_through_the_language_pack(tmp_path: Path) -> None:
    """The pages carry no hard-coded Chinese; the pack is the only source of it."""

    root = _cargo_mirror(tmp_path)
    table = read_i18n_table(root)
    assert table["Cargo_AccountPeriod"] == "账期"

    baseline = expand_baseline(root, "src/views/cargo/v0/SeaExport/bookingNew.vue", "cw-web")

    assert "账期" in baseline.anchors
    assert "航线代码" in baseline.anchors
    assert "保存" in baseline.anchors
    # The key itself must not leak into the anchor set as if it were copy.
    assert not any(anchor.startswith("CargoMsg.") for anchor in baseline.anchors)


def test_an_unresolvable_key_adds_no_anchor_and_no_false_penalty(tmp_path: Path) -> None:
    """A missing translation is a gap in the pack, not lost copy."""

    root = tmp_path / "mini"
    (root / "src").mkdir(parents=True)
    page = root / "src" / "page.vue"
    page.write_text(
        '<template><div class="x">{{ $t(\'Nope.Missing\') }}</div></template>',
        encoding="utf-8",
    )

    baseline = expand_baseline(root, "src/page.vue", "cw-web")

    assert "Nope.Missing" not in baseline.anchors
    assert baseline.anchors == ()


def test_anchor_overlap_measures_copy_not_markup() -> None:
    """A rewrite can keep the classes and still lose every word on the screen."""

    anchors = ("账期", "航线代码", "保存")

    kept = '<div class="a"><span>账期</span><span>航线代码</span><button>保存</button></div>'
    lost = '<div class="a"><span>Period</span><span>Airline</span><button>OK</button></div>'

    assert anchor_overlap(anchors, kept)[:2] == (3, 3)
    assert anchor_overlap(anchors, kept)[2] == pytest.approx(1.0)
    assert anchor_overlap(anchors, lost)[2] == pytest.approx(0.0)


def test_script_and_style_contents_never_count_as_visible_copy() -> None:
    """A `<style>` selector that happens to be Chinese is not on the screen."""

    markup = "<style>.账期{color:red}</style><div>账期</div>"

    assert anchor_overlap(("账期",), markup)[0] == 1
    assert anchor_overlap(("航线代码",), markup)[0] == 0


def test_a_nested_slot_template_is_not_cut_at_the_first_closing_tag() -> None:
    """Vue slots nest, and a non-greedy match stops at the first `</template>`.

    The real bookingTreeGrid.vue has five `<template>` tags: one root and four
    slots. Reading it with `<template>(.*?)</template>` returned 19 699 of its
    73 837 characters, so 73% of the screen never reached the model — and the
    copy anchors taken from that fragment made a faithful transcription score
    near zero for a reason that had nothing to do with the transcription.
    """

    sfc = """<template>
  <div class="root">
    <first-block />
    <el-table :data="rows">
      <template #status="{ record }"><span>{{ record.status }}</span></template>
      <template #actions="{ record }"><a @click="edit(record)">编辑</a></template>
    </el-table>
    <last-block />
  </div>
</template>

<script>
export default { name: 'Nested' }
</script>
"""

    from octop.infra.pm.runtime.prototype_source import template_body

    body = template_body(sfc)

    assert body is not None
    assert "last-block" in body
    assert "actions" in body
    assert body.count("<template") == 2  # the two slot openers stay inside
    assert "</script>" not in body


def test_the_language_pack_is_read_once_per_mirror(tmp_path: Path) -> None:
    """One page references hundreds of keys; re-reading the pack each time is waste."""

    root = _cargo_mirror(tmp_path)
    prototype_source._I18N_CACHE.clear()

    expand_baseline(root, "src/views/cargo/v0/SeaExport/bookingNew.vue", "cw-web")
    cached = prototype_source._I18N_CACHE[str(root)]

    expand_baseline(root, "src/views/cargo/v0/SeaExport/bookingNew.vue", "cw-web")

    assert prototype_source._I18N_CACHE[str(root)] is cached
