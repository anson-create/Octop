"""The prototype must be derived from source, and its fidelity measurable.

These cover the two ways the old prototype lied: it invented class names (so
``class_overlap`` collapsed) and it claimed a palette the product never used (so
the token table had to come from the product's own CSS, not from the model).
"""

from __future__ import annotations

from pathlib import Path

from octop.infra.pm.domain.repos import REPOS
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
