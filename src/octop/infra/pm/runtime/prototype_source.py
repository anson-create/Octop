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
def block_body(text: str, tag: str) -> str | None:
    """Body of the outermost ``<tag>…</tag>``, counting nesting.

    A non-greedy regex stops at the *first* closing tag, and Vue slot templates
    nest: ``bookingTreeGrid.vue`` contains five ``<template>`` tags, so the naive
    pattern returned 19 699 of the element's 73 837 characters — 73% of the
    screen was dropped before anything downstream saw it. The same cut silently
    shrank the copy-anchor set to a seventh of its real size, which made a
    perfect transcription score near zero.

    Returns ``None`` when the tag is absent, so callers can still tell "no block"
    from "empty block".
    """

    if not text:
        return None
    token_re = re.compile(rf"<{tag}\b[^>]*>|</{tag}\s*>", re.IGNORECASE)
    depth = 0
    start = -1
    for match in token_re.finditer(text):
        token = match.group(0)
        if token.startswith("</"):
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : match.start()]
            continue
        if token.rstrip().endswith("/>"):
            # `<template />` opens nothing.
            if depth == 0 and start < 0:
                return ""
            continue
        depth += 1
        if depth == 1 and start < 0:
            start = match.end()
    if start < 0:
        return None
    # Unterminated block: hand back the tail rather than nothing.
    return text[start:]


def template_body(text: str) -> str | None:
    return block_body(text, "template")
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


# The SFC's three blocks. `<template #foo>` and `<script setup lang="ts">` both
# carry attributes, so the opening tag is matched loosely rather than as the
# bare word — otherwise a scoped or ts block reads as "no script" and the
# baseline silently loses the data the template binds to.
_SFC_SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.DOTALL)

# Namespaced component tags: `el-table`, `a-form-item`, `van-cell`. Native tags
# have no dash and are not interesting here — they carry no library semantics.
_COMPONENT_TAG_RE = re.compile(r"<([a-z][a-z0-9]*(?:-[a-z0-9]+)+)[\s/>]")

# UI-library tags. They are components too, but their source is in `node_modules`
# (or nowhere), so expanding them would only produce noise. Everything else with
# a dash is a project-local component and is a candidate for expansion.
_LIBRARY_PREFIXES = ("el-", "a-", "van-", "arco-", "ant-", "n-", "v-", "router-")

# `import bookingTreeGrid from './components/booking/bookingTreeGrid'`
# The default and named forms both appear in these repos; only the local path is
# interesting, so the specifier is captured loosely and filtered afterwards.
_IMPORT_RE = re.compile(
    r"""import\s+(?:(\w+)|\{([^}]*)\})\s+from\s+["']([^"']+)["']""",
)

# `$t('CargoMsg.Cargo_ARAP')` and `$t("...")` — the only way this product emits
# user-visible copy. There is no hard-coded Chinese in the CARGO pages at all.
_I18N_CALL_RE = re.compile(r"""\$t\(\s*["']([A-Za-z][\w.\-]+)["']""")

# A Chinese string literal in a lang pack: `Cargo_ARAP: '应收应付'`.
_LANG_ENTRY_RE = re.compile(r"""^\s*([A-Za-z_][\w]*)\s*:\s*['"]([^'"]*)['"]""", re.M)

# Runs of CJK characters — what a transcribed prototype must still contain if it
# really took its copy from the product.
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff][\u4e00-\u9fff，。、：；（）%\-/]*[\u4e00-\u9fff]|[\u4e00-\u9fff]")


def _kebab_to_camel(tag: str) -> str:
    """`booking-tree-grid` -> `bookingTreeGrid`, matching the import variable."""

    head, *rest = tag.split("-")
    return head + "".join(part[:1].upper() + part[1:] for part in rest if part)


def _is_library_tag(tag: str) -> bool:
    return tag.lower().startswith(_LIBRARY_PREFIXES)


def component_imports(script: str) -> dict[str, str]:
    """Local component imports, keyed by the variable the template binds to.

    Only ``./`` and ``@/`` specifiers are kept: a package import has no readable
    source in the mirror, so it cannot be expanded no matter how much budget is
    left. ``@`` is the repo's ``src`` alias, which is what the Vue CLI default
    configures and what both CARGO families use.
    """

    found: dict[str, str] = {}
    for match in _IMPORT_RE.finditer(script or ""):
        default_name, named, spec = match.group(1), match.group(2) or "", match.group(3)
        if not (spec.startswith("./") or spec.startswith("@/")):
            continue
        if default_name:
            found[default_name] = spec
        for part in named.split(","):
            part = part.strip()
            if not part:
                continue
            local = part.split(" as ")[-1].strip()
            if local:
                found.setdefault(local, spec)
    return found


def _resolve_component(root: Path, importer: str, spec: str) -> str | None:
    """Turn an import specifier into a repo-relative file that actually exists.

    The extension is usually omitted, and a directory import resolves to its
    ``index``. Both are tried before giving up, and the answer is a relative
    path so it can be re-read and reported without leaking absolute paths.
    """

    importer_dir = Path(importer).parent
    if spec.startswith("@/"):
        base = Path("src") / spec[2:]
    else:
        base = importer_dir / spec[2:] if spec.startswith("./") else importer_dir / spec
    base = Path(str(base).replace("\\", "/"))
    candidates = [base]
    if base.suffix == "":
        candidates += [base.with_suffix(".vue"), base.with_suffix(".jsx"), base.with_suffix(".tsx")]
        candidates += [base / "index.vue", base / "index.js"]
    for candidate in candidates:
        try:
            rel = candidate.as_posix().lstrip("./")
        except (AttributeError, ValueError):  # pragma: no cover - defensive
            continue
        if (Path(root) / rel).is_file():
            return rel
    return None


def read_i18n_table(root: Path) -> dict[str, str]:
    """Flatten ``src/lang/zh*`` into ``{leaf_key: chinese_copy}``.

    The CARGO pages carry no hard-coded copy at all — one page had 851 ``$t()``
    calls and zero literal Chinese — so without this table a baseline has no
    readable text and no way to check whether a transcription kept it. Values are
    keyed by the *leaf* of the dotted key because the pages qualify keys with a
    namespace (``CargoMsg.Cargo_ARAP``) that the pack file already encodes.
    """

    table: dict[str, str] = {}
    lang_dir = Path(root) / "src" / "lang"
    if not lang_dir.is_dir():
        return table
    files = [lang_dir / "zh.js"]
    zh_dir = lang_dir / "zh_CN"
    if zh_dir.is_dir():
        files += sorted(zh_dir.glob("*.js"))
    for path in files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable pack is not fatal
            continue
        for match in _LANG_ENTRY_RE.finditer(text):
            leaf, value = match.group(1), match.group(2)
            # An empty value means "not translated yet" and must not count as an
            # anchor, otherwise the fidelity score would be measured against
            # blanks.
            if value.strip() and leaf not in table:
                table[leaf] = value
    return table


def extract_anchor_keys(markup: str) -> set[str]:
    """i18n keys referenced by a chunk of template."""

    return set(_I18N_CALL_RE.findall(markup or ""))


def extract_anchors(markup: str, table: dict[str, str]) -> set[str]:
    """Chinese copy the markup is expected to show, resolved through the pack.

    A key that the pack does not know is dropped rather than counted as missing:
    an unresolved key is a gap in the language pack, not evidence that the
    transcription lost copy.
    """

    copy: set[str] = set()
    for key in extract_anchor_keys(markup):
        value = table.get(key.split(".")[-1])
        if value:
            copy.add(value.strip())
    # Copy that is written literally (older pages, and the admin console) is an
    # anchor too — otherwise the whole mechanism silently measures nothing there.
    # Comments are stripped first: the expansion markers this module writes into
    # the template are Chinese prose, and counting them as copy inflated the
    # anchor set with strings that never appear on a screen.
    for run in _CJK_RUN_RE.findall(_strip_i18n_calls(_COMMENT_RE.sub(" ", markup or ""))):
        run = run.strip()
        if len(run) >= 2:
            copy.add(run)
    return copy


def _strip_i18n_calls(markup: str) -> str:
    return _I18N_CALL_RE.sub(" ", markup or "")


# Everything that is markup rather than copy. `script`/`style` are removed whole
# first, because their contents would otherwise leak identifiers into the text.
_BLOCK_RE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_ONLY_RE = re.compile(r"<[^>]+>")
# Comments are prose, not copy. This module writes Chinese markers between the
# spliced component templates, and a `<style>` selector or a `<!-- 新增点 -->`
# annotation must never be counted as something the user sees.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Attributes whose value a user reads. Anchors are collected from the raw markup,
# so they include `placeholder="业务编号"` and `title="删除订舱"`; if the scorer
# only looked at text nodes it would mark those as lost on a faithful
# transcription, and the loss would be a measuring artefact rather than a defect.
_VISIBLE_ATTR_RE = re.compile(
    r"""(?:placeholder|title|aria-label|alt|label|empty-text)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def visible_text(markup: str) -> str:
    """The characters a reader would see, whitespace removed.

    Whitespace is stripped rather than collapsed because a transcription wraps
    lines differently from the source; comparing with the spaces kept made every
    long label look lost.
    """

    text = _COMMENT_RE.sub(" ", markup or "")
    text = _BLOCK_RE.sub(" ", text)
    attributes = " ".join(_VISIBLE_ATTR_RE.findall(text))
    text = _TAG_ONLY_RE.sub(" ", text)
    return re.sub(r"\s+", "", text + attributes)


def anchor_overlap(anchors: tuple[str, ...] | list[str], markup: str) -> tuple[int, int, float]:
    """How much of the page's own copy survived into a transcription.

    ``class_overlap`` cannot judge this product: a CARGO page exposes one or two
    class names because the interface is drawn by ag-Grid and project components,
    so the score is either 0% or 100% depending on a single string. The copy is
    the part that is both dense (hundreds of labels per page) and checkable —
    every string traces back to ``src/lang/zh_CN`` through a ``$t()`` key.
    """

    pool = [a for a in (anchors or ()) if a.strip()]
    text = visible_text(markup)
    if not pool or not text:
        return 0, len(pool), 0.0
    hits = sum(1 for anchor in pool if re.sub(r"\s+", "", anchor) in text)
    return hits, len(pool), hits / len(pool)


@dataclass
class SourceBaseline:
    """The real page source a prototype has to be transcribed from.

    This is what the product's own prototyping rules ask for: read the existing
    page, keep its block order, spacing, button groups and dialog skeleton, then
    write the logic HTML out of that. Rendering the component reaches the same
    goal by a much heavier route — a working Node toolchain and every runtime
    dependency installed — so it is kept as an optional fidelity *check* rather
    than the way the prototype is produced.

    The three blocks are kept separate rather than as one blob: the template is
    what gets transcribed, the script explains which state the template binds
    to, and the style decides spacing. A caller that only has room for one of
    them should take the template.
    """

    page: str
    repo_key: str
    template: str = ""
    script: str = ""
    style: str = ""
    size: int = 0
    lines: int = 0
    classes: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    note: str = ""
    # Sub-components whose templates were spliced into ``template``. A page whose
    # real interface lives one level down (the usual shape in CARGO: a 1.3 KB
    # shell wrapping a 450 KB grid) is only transcribable after this expansion,
    # so it is recorded rather than left implicit.
    expanded: tuple[str, ...] = ()
    # Chinese copy the page must show, resolved through the language pack.
    anchors: tuple[str, ...] = ()
    # True when the located file only re-exports another page.
    shell: bool = False

    @property
    def usable(self) -> bool:
        """A baseline with no template cannot be transcribed from."""

        return bool(self.template.strip())

    @property
    def thin(self) -> bool:
        """True when the template is too small to describe a screen by itself.

        A 1.3 KB template with one class and no copy is a container, not a page.
        Saying so keeps the caller from labelling the result 1:1 — which is the
        one thing the prototyping rules forbid outright.
        """

        return len(self.template) < 600 and len(self.classes) < 3 and not self.anchors


def read_baseline(
    repo_root: Path,
    page_rel: str,
    repo_key: str,
    *,
    max_chars: int = 24000,
) -> SourceBaseline:
    """Read one page's SFC blocks, bounded so a huge page cannot flood a prompt.

    ``max_chars`` applies to the template, which is the block that gets
    transcribed. The script and style are capped lower and truncation is always
    stated in ``note`` — a silently cut template produces a prototype that is
    confidently missing its lower half, which is worse than a short one that
    says it was short.
    """

    rel = (page_rel or "").lstrip("/")
    path = Path(repo_root) / rel
    if not rel:
        return SourceBaseline(page="", repo_key=repo_key, note="没有页面路径")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return SourceBaseline(page=rel, repo_key=repo_key, note=f"读取失败：{exc}")

    notes: list[str] = []
    template_match = template_body(text)
    if template_match is None:
        # A .tsx/.jsx page has no SFC blocks; there the JSX *is* the template.
        # A .vue with no <template> is a forwarding shell — 14 lines that import
        # another page and re-export it — and handing its `<script>` back as a
        # template produced a baseline that scored 0% and claimed to be usable.
        if path.suffix in {".tsx", ".jsx"}:
            notes.append("没有 <template> 块，按整文件作为基线（.tsx/.jsx 页面）")
            template = text
        else:
            notes.append("没有 <template> 块（转发壳），需沿 import 下钻到真实页面")
            template = ""
    else:
        template = template_match

    script_match = _SFC_SCRIPT_RE.search(text)
    style_match = _SFC_STYLE_RE.search(text)

    if len(template) > max_chars:
        notes.append(f"template 从 {len(template)} 字截到 {max_chars} 字")
        template = template[:max_chars]
    script = script_match.group(2).strip() if script_match else ""
    if len(script) > 8000:
        notes.append(f"script 从 {len(script)} 字截到 8000 字")
        script = script[:8000]
    style = style_match.group(2).strip() if style_match else ""
    if len(style) > 8000:
        notes.append(f"style 从 {len(style)} 字截到 8000 字")
        style = style[:8000]

    return SourceBaseline(
        page=rel,
        repo_key=repo_key,
        template=template,
        script=script,
        style=style,
        size=len(text.encode("utf-8", errors="replace")),
        lines=text.count("\n") + 1,
        classes=tuple(sorted(extract_classes(template))),
        components=tuple(sorted(set(_COMPONENT_TAG_RE.findall(template)))),
        note="；".join(notes),
    )


@dataclass
class _ComponentSlice:
    """One sub-component whose template was spliced into the page baseline."""

    tag: str
    rel: str
    parent: str
    template: str
    depth: int


_I18N_CACHE: dict[str, dict[str, str]] = {}


def _cached_i18n(root: Path) -> dict[str, str]:
    """Language pack lookup, cached per mirror root.

    One page references hundreds of keys; re-reading the whole ``zh_CN`` pack for
    every page turned a 30 ms read into a 300 ms one for no gain.
    """

    key = str(root)
    if key not in _I18N_CACHE:
        _I18N_CACHE[key] = read_i18n_table(root)
    return _I18N_CACHE[key]


def _read_text(path: Path, limit: int = 2_000_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _sfc_blocks(text: str) -> tuple[str, str, str]:
    """(template, script, style); template is "" when the file has no block."""

    tpl = template_body(text)
    scr = _SFC_SCRIPT_RE.search(text or "")
    sty = _SFC_STYLE_RE.search(text or "")
    return (
        tpl if tpl is not None else "",
        scr.group(2) if scr else "",
        sty.group(2) if sty else "",
    )


def _forwarding_target(root: Path, rel: str, text: str) -> str | None:
    """The page a template-less shell re-exports.

    ``fbaBookingNew.vue`` is 14 lines: import ``bookingNew``, re-export it. Read
    literally it has no template at all, and the previous code handed its
    ``<script>`` back as one — a baseline that scored 0% while reporting itself
    usable. Following the single import finds the page the user actually sees.
    """

    imports = component_imports(_sfc_blocks(text)[1])
    if len(imports) != 1:
        return None
    spec = next(iter(imports.values()))
    return _resolve_component(root, rel, spec)


def expand_baseline(
    repo_root: Path,
    page_rel: str,
    repo_key: str,
    *,
    max_chars: int = 40000,
    depth: int = 3,
    max_components: int = 40,
) -> SourceBaseline:
    """Read a page *and the components that actually draw it*.

    Reading one SFC is not enough in this product. ``SeaExport/bookingNew.vue``
    is 770 lines and 28 KB, but its ``<template>`` is 1361 characters holding a
    single class and four custom tags — the screen itself lives in
    ``booking-tree-grid`` (456 KB, 73 837 template characters) and
    ``booking-workspace`` (352 KB), and the grid inside *that* is another
    component again. A transcription built from the shell alone is an empty
    frame labelled 1:1, which is exactly the fabrication the prototyping rules
    forbid.

    So the template is flattened here: local component imports are resolved
    against the repo, their templates are read, and each one is appended behind
    the parent with a marker naming the tag it replaces. A budget covers the
    whole flattened result and every cut is reported — a silently truncated
    template produces a prototype confidently missing its lower half.

    The default budget is 40 000 characters rather than 24 000 because the first
    screen of a dense page does not fit in the smaller one: at 24 000 the booking
    grid kept a quarter of its template and yielded 30 copy anchors, which is too
    coarse to tell a good transcription from a mediocre one.
    """

    root = Path(repo_root)
    rel = (page_rel or "").lstrip("/")
    if not rel:
        return SourceBaseline(page="", repo_key=repo_key, note="没有页面路径")

    notes: list[str] = []
    text = _read_text(root / rel)
    if not text:
        return SourceBaseline(page=rel, repo_key=repo_key, note=f"读取失败：{rel} 不存在或不可读")

    shell = False
    if template_body(text) is None:
        target = _forwarding_target(root, rel, text)
        if target is None:
            # Not a shell and not an SFC: hand it to the plain reader so the
            # caller still gets the .tsx整文件 fallback and its own note.
            return read_baseline(root, rel, repo_key, max_chars=max_chars)
        notes.append(f"{rel} 是转发壳，已下钻到 {target}")
        rel, shell = target, True
        text = _read_text(root / rel)

    template, script, style = _sfc_blocks(text)

    nodes = [_ComponentSlice(tag="", rel=rel, parent="", template=template, depth=1)]
    seen = {rel}
    frontier: list[tuple[str, str, str, int]] = [(rel, template, script, 1)]
    while frontier and len(nodes) < max_components:
        cur_rel, cur_tpl, cur_script, cur_depth = frontier.pop(0)
        if cur_depth >= depth:
            continue
        imports = component_imports(cur_script)
        if not imports:
            continue
        for tag in sorted(set(_COMPONENT_TAG_RE.findall(cur_tpl))):
            if _is_library_tag(tag):
                continue
            spec = imports.get(_kebab_to_camel(tag))
            if spec is None:
                continue
            target = _resolve_component(root, cur_rel, spec)
            if target is None or target in seen:
                continue
            child_tpl, child_script, _ = _sfc_blocks(_read_text(root / target))
            if not child_tpl.strip():
                continue
            seen.add(target)
            nodes.append(
                _ComponentSlice(
                    tag=tag, rel=target, parent=cur_rel, template=child_tpl, depth=cur_depth + 1
                )
            )
            frontier.append((target, child_tpl, child_script, cur_depth + 1))
            if len(nodes) >= max_components:
                notes.append(f"子组件展开到上限 {max_components} 个，其余未展开")
                break

    cut: list[str] = []
    # The screen a reviewer sees is the page plus its direct children. Everything
    # deeper is dialogs and tab panels that are hidden until clicked, so it is
    # expanded for the model's benefit but must not sit in the fidelity
    # denominator: counting copy from a closed dialog made even a perfect
    # transcription score 53%, which measures the fixture rather than the work.
    core = [node for node in nodes[1:] if node.depth <= 2]
    deep = [node for node in nodes[1:] if node.depth > 2]
    core_text: list[str] = []
    if len(nodes) == 1:
        body = template
        if len(body) > max_chars:
            notes.append(f"template 从 {len(body)} 字截到 {max_chars} 字")
            body = body[:max_chars]
        flattened = body
        core_text.append(body)
    else:
        reserve = min(len(template), max(1500, max_chars // 4))
        rest = max(max_chars - reserve, 0)
        # First-screen components get the lion's share of what is left; the
        # hidden ones take what remains so they still inform the structure.
        budgets = {id(node): 0 for node in nodes[1:]}
        for group, budget in ((core, int(rest * 0.72)), (deep, rest - int(rest * 0.72))):
            total_g = sum(len(node.template) for node in group) or 1
            for node in group:
                budgets[id(node)] = min(
                    len(node.template), max(300, int(budget * len(node.template) / total_g))
                )
        if len(template) > reserve:
            notes.append(f"主模板从 {len(template)} 字截到 {reserve} 字")
        parts = [template[:reserve]]
        core_text.append(template[:reserve])
        for node in nodes[1:]:
            size = len(node.template)
            share = budgets[id(node)]
            if share < size:
                cut.append(node.rel)
            block = (
                f"\n<!-- [子组件展开] <{node.tag}> → {node.rel}（该组件模板 {size} 字"
                f"{'，已按预算截断' if share < size else ''}"
                f"{'，位于弹窗/标签页内，非首屏' if node.depth > 2 else ''}）"
                f"\n     主模板里出现 <{node.tag}> 的位置，真实页面渲染的就是下面这段结构： -->\n"
                f"{node.template[:share]}\n<!-- [/子组件展开] <{node.tag}> -->\n"
            )
            parts.append(block)
            if node.depth <= 2:
                core_text.append(node.template[:share])
        flattened = "".join(parts)
        if cut:
            notes.append(f"{len(cut)} 个子组件模板按预算截断（例如 {cut[0]}）")

    # Anchors come from the first screen only, so the score answers "did the
    # transcription keep the screen" rather than "did it also reproduce every
    # dialog behind a click".
    anchors = extract_anchors("\n".join(core_text), _cached_i18n(root))
    if len(nodes) > 1:
        notes.append(f"已展开 {len(nodes) - 1} 个子组件：{'、'.join(n.tag for n in nodes[1:5])}"
                     + ("…" if len(nodes) > 5 else ""))
        notes.append(f"保真度只按首屏 {len(core)} 个子组件计分，弹窗/标签页内的文案不计入分母")
    if anchors:
        notes.append(f"首屏业务文案锚点 {len(anchors)} 条（经语言包反查）")

    return SourceBaseline(
        page=rel,
        repo_key=repo_key,
        template=flattened,
        script=script[:8000],
        style=style[:8000],
        size=len(text.encode("utf-8", errors="replace")),
        lines=text.count("\n") + 1,
        classes=tuple(sorted(extract_classes(flattened))),
        components=tuple(sorted({n.tag for n in nodes if n.tag})),
        note="；".join(notes),
        expanded=tuple(n.rel for n in nodes[1:]),
        anchors=tuple(sorted(anchors)),
        shell=shell,
    )


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
    body = template_body(sfc or "")
    return body if body is not None else ""


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
