"""Derive a prototype from a real page instead of inventing one.

The old prototype step asked a model to draw a page from a sentence. The result
could not be 1:1 with the product by construction: the model had never seen the
product, so it invented class names, picked its own primary colour, and rendered
a bare ``<table>`` where the product renders an Arco ``a-table``.

This module inverts that. It renders the *actual* page component with the
repository's own Vite config and Vue SSR, then wraps the markup in a standalone
HTML document with the stylesheets the page needs. The model's job shrinks to
substituting business copy into that markup — it never gets to restructure it.

Fidelity is measured, not asserted. ``class_overlap`` is the share of the source
template's class names that survive into the rendered output; for a derived
prototype it is ~1.0 by construction, and for a model-written page it collapses
to near zero. That number is what the gate reads, so "looks similar" cannot pass
as "is derived".
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from octop.infra.pm.domain.repos import RepoSpec

# UI libraries that ship a prebuilt stylesheet. A prototype without it renders
# correct markup with no visuals, which reads as "broken" rather than "close".
_UI_CSS = {
    "arco": ("node_modules/@arco-design/web-vue/dist/arco.css",),
    "element-ui": ("node_modules/element-ui/lib/theme-chalk/index.css",),
}

# `class="..."` — the lookbehind keeps `:class="..."` out, otherwise an object
# binding would be picked up as if it were a literal class list.
_CLASS_ATTR_RE = re.compile(r"""(?<![-:\w])class\s*=\s*["']([^"']+)["']""")
_CLASS_BIND_RE = re.compile(r""":class\s*=\s*["']([^"']+)["']""")
# Tailwind utilities and project BEM-ish names both look like this. Rejecting
# anything outside it keeps `{{ }}` expressions and ternaries out of the set.
_CLASS_TOKEN_RE = re.compile(r"^[A-Za-z][\w\-:/\[\]\.%]{0,60}$")
_SFC_TEMPLATE_RE = re.compile(r"<template>(.*?)</template>", re.DOTALL)
_SFC_STYLE_RE = re.compile(r"<style([^>]*)>(.*?)</style>", re.DOTALL)


def extract_classes(markup: str) -> set[str]:
    """Static class names in a chunk of markup.

    Only literal ``class="..."`` / ``:class="'literal'"`` are collected. Dynamic
    bindings (``:class="{ active: x }"``) are skipped: they cannot be evaluated
    without a Vue instance, and counting their identifiers would inflate the
    overlap score with strings that never reach the DOM.
    """

    found: set[str] = set()
    for match in _CLASS_ATTR_RE.finditer(markup or ""):
        for token in re.split(r"\s+", match.group(1).strip()):
            if token and _CLASS_TOKEN_RE.match(token):
                found.add(token)
    for match in _CLASS_BIND_RE.finditer(markup or ""):
        value = match.group(1).strip()
        # An object or ternary binding is an expression, not a class list.
        if any(ch in value for ch in "{}?:"):
            continue
        for token in re.split(r"\s+", value):
            if token and _CLASS_TOKEN_RE.match(token):
                found.add(token)
    return found


def extract_tokens(css: str) -> dict[str, str]:
    """CSS custom properties declared anywhere in a stylesheet."""

    tokens: dict[str, str] = {}
    # The last declaration in a block has no trailing semicolon, so the terminator
    # is either ``;`` or the closing brace.
    for match in re.finditer(r"(--[\w-]+)\s*:\s*([^;{}]+?)\s*(?:;|\})", css or ""):
        tokens.setdefault(match.group(1), match.group(2).strip())
    return tokens


@dataclass
class RenderResult:
    ok: bool
    html: str = ""
    styles: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class DerivedPrototype:
    """A prototype plus the measurements that justify calling it 1:1."""

    html: str
    source_page: str
    repo_key: str
    git_ref: str
    class_overlap: float
    classes_source: int
    classes_rendered: int
    styles_inlined: int
    fidelity: str
    fidelity_note: str
    errors: tuple[str, ...] = ()

    @property
    def derived(self) -> bool:
        """True when the render came from the real component, not a rewrite."""

        return self.class_overlap >= 0.9 and self.classes_rendered > 0


class SsrRenderer:
    """Runs ``render.mjs`` in a subprocess and parses its JSON reply."""

    def __init__(
        self,
        script: Path,
        *,
        node: str = "node",
        timeout_s: int = 240,
    ) -> None:
        self._script = Path(script)
        self._node = node
        self._timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return self._script.is_file()

    def render(self, repo_root: Path, page_file: str) -> RenderResult:
        if not self.available:
            return RenderResult(ok=False, message=f"ssr script missing: {self._script}")
        root = Path(repo_root)
        if not root.is_dir():
            return RenderResult(ok=False, message=f"repo mirror missing: {root}")

        page = page_file if page_file.startswith("/") else f"/{page_file}"
        cmd = [self._node, str(self._script), "--root", str(root), "--page", page]
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                cwd=str(root),
            )
        except subprocess.TimeoutExpired:
            return RenderResult(ok=False, message=f"ssr timed out after {self._timeout_s}s")
        except OSError as exc:
            return RenderResult(ok=False, message=f"ssr failed to start: {exc}")

        payload = _last_json_object(proc.stdout) or _last_json_object(proc.stderr)
        if payload is None:
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            return RenderResult(ok=False, message=f"ssr produced no json: {detail}")

        html = str(payload.get("html") or "")
        errors = _str_list(payload.get("errors") or payload.get("error"))
        return RenderResult(
            ok=bool(html),
            html=html,
            styles=_str_list(payload.get("styles")),
            components=_str_list(payload.get("components")),
            errors=errors,
            message="" if html else str(payload.get("error") or "empty render"),
        )


def _str_list(value: object) -> list[str]:
    """Normalise ``str | list | None`` into a list of strings."""

    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    return []


def _last_json_object(raw: str) -> dict[str, object] | None:
    """Pull the last JSON object out of a noisy stdout.

    Vite and Vue both print warnings that are not JSON; the renderer prints its
    payload last, so scanning from the end is more robust than parsing the whole
    stream.
    """

    if not raw:
        return None
    for line in reversed(raw.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class PrototypeDeriver:
    """Turns (repo, page) into a standalone HTML prototype."""

    def __init__(self, renderer: SsrRenderer) -> None:
        self._renderer = renderer

    def derive(self, spec: RepoSpec, page_file: str) -> DerivedPrototype:
        root = spec.root()
        source_text = _read(root / page_file.lstrip("/"))
        source_classes = extract_classes(_template_of(source_text) or source_text)

        render = self._renderer.render(root, page_file)
        if not render.ok:
            return DerivedPrototype(
                html="",
                source_page=page_file,
                repo_key=spec.key,
                git_ref=spec.git_ref,
                class_overlap=0.0,
                classes_source=len(source_classes),
                classes_rendered=0,
                styles_inlined=0,
                fidelity="partial",
                fidelity_note=f"未能从源码派生：{render.message}",
                errors=tuple(render.errors[:5]),
            )

        rendered_classes = extract_classes(render.html)
        overlap = (
            len(rendered_classes & source_classes) / len(source_classes)
            if source_classes
            else 0.0
        )

        css_parts, css_sources, css_notes = _collect_css(root, spec, page_file, source_text)
        tokens = extract_tokens("\n".join(css_parts))
        html = _assemble(
            body=render.html,
            css="\n".join(css_parts),
            title=f"{spec.system} · {Path(page_file).stem}",
            page_file=page_file,
            tokens=tokens,
            spec=spec,
        )

        fidelity = "exact" if overlap >= 0.9 else "partial"
        note = (
            f"由 {spec.key} 的 {page_file} 经 Vue SSR 渲染生成，"
            f"class 同源率 {overlap:.0%}（{len(rendered_classes & source_classes)}/{len(source_classes)}），"
            f"内联样式 {len(css_sources)} 份。数据为空态，业务文案可替换。"
            + (f" {'；'.join(css_notes)}。" if css_notes else "")
            if fidelity == "exact"
            else f"渲染与原页面结构同源率仅 {overlap:.0%}，不可宣称 1:1。"
        )
        return DerivedPrototype(
            html=html,
            source_page=page_file,
            repo_key=spec.key,
            git_ref=spec.git_ref,
            class_overlap=overlap,
            classes_source=len(source_classes),
            classes_rendered=len(rendered_classes),
            styles_inlined=len(css_sources),
            fidelity=fidelity,
            fidelity_note=note,
            errors=tuple(render.errors[:5]),
        )


# --- helpers ----------------------------------------------------------------


def _read(path: Path, limit: int = 400_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _template_of(sfc: str) -> str:
    match = _SFC_TEMPLATE_RE.search(sfc or "")
    return match.group(1) if match else ""


def _collect_css(
    root: Path, spec: RepoSpec, page_file: str, source_text: str
) -> tuple[list[str], list[str], list[str]]:
    """Stylesheets the prototype needs, in preference order.

    A production build is the only source that is *exactly* right: it has already
    run Tailwind, compiled the SCSS variables and bundled the UI library in the
    same order the real app uses. Inlining raw ``.scss`` instead would put
    ``$color-primary: #165DFF`` into a ``<style>`` tag, where the browser drops it
    as invalid and the page silently loses its palette.

    Falls back to the UI library stylesheet when the mirror has never been built,
    and reports which of the two happened so the caller can say so.
    """

    parts: list[str] = []
    sources: list[str] = []
    notes: list[str] = []

    # Preferred: the stylesheet bundle built specifically for prototypes. It is
    # arco.css + compiled Tailwind + compiled SCSS in the app's own order, so the
    # palette and spacing come from the product rather than from a guess.
    # Note that this bundle does *not* contain the page's own <style> block —
    # that is appended below, and skipping it would drop the page's grid layout
    # while still looking plausible, which is the failure mode to avoid.
    dedicated = root / "dist" / "pm-prototype.css"
    dedicated_css = _read(dedicated, limit=8_000_000) if dedicated.is_file() else ""
    if dedicated_css:
        parts.append(dedicated_css)
        sources.append("dist/pm-prototype.css")
        notes.append("样式来自 dist/pm-prototype.css（组件库 + 编译后的 Tailwind/SCSS）")
    else:
        bundles = (
            sorted((root / "dist" / "assets").glob("*.css")) if (root / "dist").is_dir() else []
        )
        for path in bundles:
            css = _read(path, limit=8_000_000)
            if css:
                parts.append(css)
                sources.append(str(path.relative_to(root)))
        if parts:
            notes.append(f"样式来自构建产物（{len(parts)} 个 CSS 包），与线上一致")
        else:
            if bundles:
                notes.append("dist 存在但没有可读的 CSS 包")
            for rel in _UI_CSS.get(spec.ui_lib, ()):
                css = _read(root / rel, limit=4_000_000)
                if css:
                    parts.append(css)
                    sources.append(rel)
            if parts:
                notes.append("样式来自组件库发布包，未含 Tailwind/SCSS 编译结果")
            else:
                notes.append("未能取到任何组件库样式，原型将缺少视觉")

    # Page-level <style> blocks. `scoped` is dropped because the rendered markup
    # carries no `data-v-*` attributes; `lang="scss"` is skipped because raw SCSS
    # in a <style> tag is not CSS and would be discarded by the browser.
    for match in _SFC_STYLE_RE.finditer(source_text or ""):
        attrs = match.group(1) or ""
        css = match.group(2).strip()
        if not css:
            continue
        if "scss" in attrs.lower() or "sass" in attrs.lower():
            notes.append(f"{page_file} 的样式是 SCSS，需构建后才能内联")
            continue
        parts.append(css)
        sources.append(f"{page_file}#style")

    return parts, sources, notes


def _assemble(
    *,
    body: str,
    css: str,
    title: str,
    page_file: str,
    tokens: dict[str, str],
    spec: RepoSpec | None = None,
) -> str:
    token_block = ""
    if tokens:
        # Rendering the token table makes the prototype's palette auditable: a
        # reviewer can see #165DFF came from the product, not from the model.
        declared = "\n".join(f"  {name}: {value};" for name, value in tokens.items())
        token_block = f":root {{\n{declared}\n}}"
    body_class = "arco-theme-default" if spec is None or spec.ui_lib == "arco" else ""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"<style>\n{token_block}\n{css}\n</style>\n"
        "</head>\n"
        f'<body class="{body_class}">\n'
        f"<!-- derived from {page_file} -->\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )
