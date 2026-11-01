"""Whole pipeline for "derive this requirement's prototype from real source".

Three questions have to be answered in order, and each one can fail honestly:

1. **Which repository?** Answered before this module is called, from the Jira
   project code (``domain.repos``). A wrong answer here produces a prototype that
   is faithful to the wrong product, so it is never guessed.
2. **Which page in it?** ``locate_page`` — Chinese requirement text reaches the
   code's identifiers through the product's own i18n catalogue, then the
   tree-sitter graph maps identifiers to files.
3. **What is on it?** ``derive_from_source`` — reads the page's own SFC (template,
   script, style) so the prototype can be transcribed from it. Rendering the
   component is a separate, optional step (``verify=True``) that measures the
   result; the two differ in cost by an order of magnitude, and requiring the
   measurement meant a mirror without `node_modules` produced nothing at all.

Every failure returns a reason a human can act on ("mirror not synced", "no code
graph", "no i18n match", "the page has no template") instead of an empty string,
because the prototype step prints that reason into the artifact's fidelity note.
A prototype that could not be derived must say so; that is the whole point of the
step.

Indexes are cached per repository and invalidated by the index file's
mtime+size, so a requirement costs one graph scan rather than one per step, and
rebuilding the graph does not require a service restart.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from octop.infra.pm.domain.locate_evidence import LocateEvidence
from octop.infra.pm.domain.repos import RepoSpec
from octop.infra.pm.ports.graph import GraphHit, GraphIndex
from octop.infra.pm.ports.i18n import I18nIndex
from octop.infra.pm.ports.routes import RouteIndex
from octop.infra.pm.runtime.prototype_source import (
    DerivedPrototype,
    PrototypeDeriver,
    SourceBaseline,
    SsrRenderer,
    expand_baseline,
)

# The renderer ships inside the wheel so `pip install` alone enables derivation.
# It cannot run from there: `import 'vite'` resolves against the *script's*
# directory, and site-packages has no node_modules. It is therefore copied into
# a directory that has one — the mirror root for Vue 3, a service-owned
# toolchain directory for Vue 2 (which needs packages the product never had).
RENDER_SCRIPT_NAME = "render.mjs"
_ASSET_DIR = Path(__file__).parent / "assets"

# Where the Vue 2 renderer's own toolchain lives. Deliberately *outside* the
# mirrors: `ensure_render_script` writing a whole node_modules into a product's
# source tree would be wiped by the next sync, and polluting a checkout to make
# a read-only feature work is exactly the kind of side effect that later gets
# mistaken for a real dependency.
TOOLCHAIN_ROOT_ENV = "OCTOP_PM_SSR_TOOLCHAIN"
_DEFAULT_TOOLCHAIN_ROOT = "/opt/pm-mirror/.ssr"

# A page-level hit beats a component hit, but a component is still a legitimate
# answer (some products put the whole screen in one). Only files we can actually
# render are candidates.
_MIN_TERM_LEN = 3

# Files a browser can render into a prototype — the same set the graph port
# treats as derivable.
_RENDERABLE_SUFFIXES = (".vue", ".tsx", ".jsx", ".svelte")

# English identifiers embedded in Chinese tickets ("加个 vendorCode 校验").
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,40}")

# A routed .vue this small is an entrance shell that imports the real page one
# level down. Worth naming in the reasons: a reviewer comparing the prototype to
# the ticket is looking at a wrapper, not the screen.
_SHELL_BYTES = 1024

# Two candidates this close together means "we cannot tell which page". The
# caller must then treat the pick as a hypothesis, not a fact.
_AMBIGUITY_RATIO = 1.2


@dataclass(frozen=True)
class PageCandidate:
    source_file: str
    score: float
    matched_labels: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "score": round(self.score, 3),
            "matched_labels": list(self.matched_labels),
            "reasons": list(self.reasons),
        }


@dataclass
class PageLocation:
    """The answer to "which page", plus how sure we are and why."""

    candidates: tuple[PageCandidate, ...] = ()
    terms: tuple[str, ...] = ()
    method: str = "none"  # i18n+graph | graph | none
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.candidates)

    @property
    def page(self) -> str:
        return self.candidates[0].source_file if self.candidates else ""

    @property
    def ambiguous(self) -> bool:
        """True when the runner-up is close enough that the pick is a guess."""

        if len(self.candidates) < 2:
            return False
        top, second = self.candidates[0].score, self.candidates[1].score
        if top <= 0:
            return True
        return second > 0 and top / second < _AMBIGUITY_RATIO

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "method": self.method,
            "ambiguous": self.ambiguous,
            "terms": list(self.terms),
            "candidates": [item.as_dict() for item in self.candidates[:3]],
            "notes": list(self.notes),
        }


@dataclass
class DerivationOutcome:
    """What the prototype step gets back: a page and its source, or a reason.

    ``ok`` now means "the page was found and its source was read". It does not
    mean the page was rendered: rendering is an optional fidelity check
    (``verify=True``) that needs a full Node toolchain in the mirror, and making
    the step depend on it meant a mirror without `node_modules` produced no
    prototype at all — even though the source needed to write one was sitting
    right there.
    """

    ok: bool
    prototype: DerivedPrototype | None = None
    baseline: SourceBaseline | None = None
    location: PageLocation = field(default_factory=PageLocation)
    reason: str = ""

    @property
    def page(self) -> str:
        if self.baseline is not None:
            return self.baseline.page
        if self.prototype is not None:
            return self.prototype.source_page.lstrip("/")
        return self.location.page

    @property
    def class_overlap(self) -> float:
        return self.prototype.class_overlap if self.prototype is not None else 0.0

    @property
    def verified(self) -> bool:
        """Whether a render actually happened and produced a measurable overlap."""

        return self.prototype is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "page": self.page,
            "location": self.location.as_dict(),
            "verified": self.verified,
            "class_overlap": round(self.class_overlap, 3),
            "derived": bool(self.prototype is not None and self.prototype.derived),
            "baseline": (
                {
                    "page": self.baseline.page,
                    "lines": self.baseline.lines,
                    "classes": len(self.baseline.classes),
                    "components": list(self.baseline.components[:10]),
                    "note": self.baseline.note,
                }
                if self.baseline is not None
                else None
            ),
        }


# --- indexes ----------------------------------------------------------------

_CACHE: dict[str, tuple[tuple[int, int], Any]] = {}
_CACHE_LOCK = threading.Lock()


def _fingerprint(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _cached(key: str, path: Path, build: Any) -> Any:
    stamp = _fingerprint(path)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    value = build()
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, value)
    return value


def graph_index(root: Path) -> GraphIndex | None:
    """The repo's code graph, or ``None`` when it was never built."""

    path = GraphIndex.graph_path(Path(root))
    return _cached(
        f"graph::{root}", path, lambda: GraphIndex.load(Path(root))
    )


def i18n_index(root: Path) -> I18nIndex | None:
    """The repo's Chinese catalogue, or ``None`` when it has none.

    Keyed on the root directory's mtime: a catalogue edit inside an existing
    file does not change it, so the service is restarted (or the graph rebuilt)
    to pick that up. Cheaper than stat-ing every catalogue file on every step,
    and the failure mode is stale search terms rather than wrong ones.
    """

    path = Path(root)
    return _cached(f"i18n::{root}", path, lambda: I18nIndex.load(path))


def route_index(root: Path) -> RouteIndex | None:
    """The repo's router, or ``None`` when it has none.

    Keyed like the other indexes, so adding a route needs a restart to be seen.
    That is a stale-answer failure, not a wrong one: a route that exists but is
    not indexed falls back to the graph, it never produces a page that is not
    in the router.

    The catalogue is handed in because a router may title a route with a key
    rather than a label — CargoWare's sea-export section is ``seaExport``, and
    only the catalogue knows that is 海运出口.
    """

    path = Path(root)
    return _cached(
        f"routes::{root}",
        path,
        lambda: RouteIndex.load(path, i18n=I18nIndex.load(path)),
    )


def clear_cache() -> None:
    """Drop every cached index — used by tests and by a manual resync."""

    with _CACHE_LOCK:
        _CACHE.clear()


# --- renderer placement -----------------------------------------------------


def ensure_render_script(
    base: Path, name: str = RENDER_SCRIPT_NAME
) -> Path | None:
    """Copy the packaged renderer into ``base`` if it is missing or stale.

    ``base`` is whichever directory has a ``node_modules`` the script can use —
    the mirror root for Vue 3, the toolchain directory for Vue 2. Returns the
    path to run, or ``None`` when the asset is not in the wheel (a source
    checkout that skipped the build, for instance).
    """

    asset = _ASSET_DIR / name
    if not asset.is_file():
        return None
    try:
        payload = asset.read_bytes()
    except OSError:
        return None

    target = Path(base) / name
    if target.is_file():
        try:
            if target.read_bytes() == payload:
                return target
        except OSError:
            # Unreadable copy: fall through and try to overwrite it.
            pass
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    except OSError:
        # A read-only directory that already carries a renderer still works — it
        # may just be an older one. Say so by returning it rather than failing.
        return target if target.is_file() else None
    return target


def toolchain_dir(name: str) -> Path:
    """Where one framework's out-of-tree SSR toolchain is installed."""

    root = os.environ.get(TOOLCHAIN_ROOT_ENV) or _DEFAULT_TOOLCHAIN_ROOT
    return Path(root) / name


def missing_packages(root: Path, names: tuple[str, ...]) -> list[str]:
    """Packages from ``names`` that node cannot resolve from ``root``.

    Node walks up from the importing file, so a hoisted ``node_modules`` one
    level above the mirror is a legitimate layout and is accepted. Anything
    deeper is not followed: at that distance the "package found" answer stops
    describing this repository and starts describing the host.
    """

    if not names:
        return []
    search = [Path(root), Path(root).parent]
    missing: list[str] = []
    for name in names:
        if not any((base / "node_modules" / name).is_dir() for base in search):
            missing.append(name)
    return missing


# --- locating ---------------------------------------------------------------


def _identifiers(text: str, limit: int = 8) -> list[str]:
    """English identifiers written directly into the ticket."""

    found: list[str] = []
    for match in _IDENTIFIER_RE.finditer(text or ""):
        token = match.group(0)
        if len(token) < _MIN_TERM_LEN or token in found:
            continue
        found.append(token)
        if len(found) >= limit:
            break
    return found


_PAGE_DIRS = ("/views/", "/pages/", "/screens/")
# Directories that hold a page-shaped file which is not a screen: tests, build
# output, and the " 2" duplicates an iCloud-synced checkout leaves behind.
_SKIP_MARKERS = ("/__tests__/", "/node_modules/", "/dist/", "/.git/")
_SKIP_PATTERN = re.compile(r" \d+/")


def _is_page(path: str) -> bool:
    """Whether a file is a screen we could render, rather than a module."""

    if not path.endswith(_RENDERABLE_SUFFIXES):
        return False
    lowered = f"/{path.lower()}"
    if any(marker in lowered for marker in _SKIP_MARKERS):
        return False
    if _SKIP_PATTERN.search(path):
        return False
    return any(hint in lowered for hint in _PAGE_DIRS)


def _page_candidates(hits: list[GraphHit], limit: int) -> tuple[PageCandidate, ...]:
    """Hits that are already pages *and* carry a strong signal.

    Weak hits are files that merely contain a common word; treating them as
    answers is how a supplier requirement once landed on the zip-code page.
    """

    return tuple(
        PageCandidate(
            source_file=hit.source_file,
            score=hit.score,
            matched_labels=hit.matched_labels,
            reasons=hit.reasons,
        )
        for hit in hits
        if _is_page(hit.source_file) and hit.strong
    )[:limit]


def _climb_to_pages(
    graph: GraphIndex, hits: list[GraphHit], terms: list[str], limit: int
) -> tuple[tuple[PageCandidate, ...], str]:
    """Follow one import hop up from module hits to the pages that use them.

    A page is only accepted when its own path carries one of the search terms.
    Without that check "some page imports this enum" would be read as "this is
    the screen" — e.g. a supplier requirement landing on the app-access page
    because both import a shared status enum. That failure is worse than no
    answer, because the rendered screen looks right and is about something else.

    The score is discounted because the evidence was gathered on the module, not
    on the screen: the page inherits the association, not the certainty.
    """

    wanted = [term.lower() for term in terms if term]
    best: dict[str, float] = {}
    via: dict[str, str] = {}
    for hit in hits[:6]:
        if _is_page(hit.source_file):
            continue
        for importer in graph.importers(hit.source_file, max_files=8):
            if not _is_page(importer):
                continue
            if wanted and not any(term in importer.lower() for term in wanted):
                continue
            score = hit.score * 0.6
            if score > best.get(importer, 0.0):
                best[importer] = score
                via[importer] = hit.source_file
    ordered = sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]
    candidates = tuple(
        PageCandidate(
            source_file=page,
            score=score,
            reasons=(f"imports {via[page]}",),
        )
        for page, score in ordered
    )
    return candidates, (candidates[0].source_file if candidates else "")


def _terms_from(
    catalogue: I18nIndex | None, text: str, notes: list[str], *, tag: str
) -> list[str]:
    """i18n keys for one source, plus identifiers typed by hand."""

    terms: list[str] = []
    if catalogue is not None:
        terms = [term for term in catalogue.terms(text, limit=8) if len(term) >= _MIN_TERM_LEN]
    for token in _identifiers(text):
        if token not in terms:
            terms.append(token)
    if not terms:
        notes.append(f"「{tag}」没有产出可用检索词")
    return terms


# How much a source's hits count relative to the title. A Jira component field
# names an area on purpose, so it outranks even the title; a comment is someone
# thinking out loud and must never outrank the ticket itself.
_SOURCE_FACTOR = {
    "component": 1.5,
    "summary": 1.0,
    "label": 1.0,
    "attachment": 0.75,
    "custom": 0.5,
    "description": 0.5,
    "environment": 0.4,
    "comment": 0.3,
}


# Sources that may create a page candidate on their own. They are short and are
# written to say what the ticket is about; a body or a comment is prose and its
# words ("优化", "反馈", "确认") bridge into the codebase's vocabulary as noise —
# a real measurement had a customs ticket land on the menu-settings page purely
# on words like `operatorAdmin` and `pending` that the description produced.
_ANCHOR_KINDS = frozenset({"component", "label", "attachment", "summary"})


def _hits_from_evidence(
    graph: GraphIndex,
    evidence: LocateEvidence,
    catalogue: I18nIndex | None,
    *,
    limit: int,
    notes: list[str],
) -> tuple[list[GraphHit], list[str]]:
    """Search each source separately, then merge.

    Anchoring sources decide *which* pages are candidates; supporting sources
    can only add weight to a candidate that an anchor already proposed. Without
    that split, a long description invents candidates of its own and the
    citation "found in the description" reads as evidence for a wrong page.

    When a ticket has no anchoring source at all (a manually written requirement
    is often just a title), every source is allowed to propose candidates —
    otherwise nothing could ever be located.
    """

    anchors = [item for item in evidence.ranked() if item.kind in _ANCHOR_KINDS]
    pool = anchors if anchors else list(evidence.ranked())

    all_terms: list[str] = []
    merged: dict[str, dict[str, Any]] = {}
    for source, may_create in (
        [(item, True) for item in pool]
        + [(item, False) for item in evidence.ranked() if item not in pool]
    ):
        factor = _SOURCE_FACTOR.get(source.kind, 0.4)
        terms = _terms_from(catalogue, source.text, notes, tag=source.field)
        if not terms:
            continue
        for term in terms:
            if term not in all_terms:
                all_terms.append(term)
        hits = graph.search(terms, limit=max(limit * 3, 9))
        for hit in hits:
            if not may_create and hit.source_file not in merged:
                continue
            entry = merged.setdefault(
                hit.source_file,
                {"best": 0.0, "labels": [], "reasons": [], "strong": False, "from": []},
            )
            scaled = hit.score * factor
            entry["best"] = max(entry["best"], scaled)
            for label in hit.matched_labels:
                if label not in entry["labels"]:
                    entry["labels"].append(label)
            for reason in hit.reasons:
                if reason not in entry["reasons"]:
                    entry["reasons"].append(reason)
            # Only an anchor can vouch for a page's identity. A description that
            # happens to mention a word must not make a file "strong".
            entry["strong"] = entry["strong"] or (hit.strong and may_create)
            if source.field not in entry["from"]:
                entry["from"].append(source.field)

    if anchors:
        notes.append(
            "以"
            + "、".join(dict.fromkeys(item.field for item in anchors))
            + "作为锚定依据，其余字段只用于佐证"
        )

    ordered: list[GraphHit] = []
    for source_file, entry in merged.items():
        # Corroboration: a second independent source that also points here is
        # real evidence, but capped so volume cannot manufacture confidence.
        extra = min(len(entry["from"]) - 1, 2) * 0.25
        reasons = [f"命中自{name}" for name in entry["from"][:3]] + entry["reasons"]
        ordered.append(
            GraphHit(
                source_file=source_file,
                score=entry["best"] * (1.0 + extra),
                matched_labels=tuple(entry["labels"][:5]),
                reasons=tuple(dict.fromkeys(reasons))[:5],
                strong=entry["strong"],
            )
        )
    ordered.sort(key=lambda hit: (-hit.score, hit.source_file))
    return ordered, all_terms


def _locate_by_route(
    routes: RouteIndex,
    evidence: LocateEvidence | None,
    *,
    prefer: tuple[str, ...],
) -> tuple[PageLocation | None, tuple[str, ...]]:
    """Ask the router first: a Jira component and a route title are the same word.

    ``None`` means "the router did not answer" and the caller must still try the
    graph — it does not mean there is no page. Only the component field is
    consulted, because it is the one structured field a ticket uses to name a
    functional area; the rest of the ticket is prose, and prose produces
    candidates the router never had.
    """

    if evidence is None:
        return None, ()
    names = [
        source.text.strip()
        for source in evidence.ranked()
        if source.kind == "component" and source.text.strip()
    ]
    if not names:
        return None, ()

    picked: list[tuple[str, PageCandidate]] = []
    misses: list[str] = []
    for name in dict.fromkeys(names):
        match = routes.match(name, prefer=prefer)
        if match.entry is not None:
            entry = match.entry
            page = routes.repo_path(entry)
            if not page:
                misses.append(f"组件「{name}」命中的路由没有页面文件")
                continue
            reasons = [f"Jira 组件「{name}」= 路由标题「{entry.title}」（{match.reason}）"]
            if entry.path:
                reasons.append(f"路由 {entry.path}")
            if entry.section:
                reasons.append("标题是板块，该文件只是板块的默认子页")
            if 0 < entry.size < _SHELL_BYTES:
                reasons.append(f"入口壳 {entry.size}B，真页面在它内部 import")
            picked.append(
                (
                    name,
                    PageCandidate(
                        source_file=page,
                        score=10.0,
                        matched_labels=(entry.title,),
                        reasons=tuple(reasons),
                    ),
                )
            )
            continue
        if match.candidates:
            apps = "、".join(dict.fromkeys(item.app for item in match.candidates))
            misses.append(
                f"组件「{name}」在 {len(match.candidates)} 条同名路由上重复（{apps}），不自动选"
            )
        else:
            misses.append(f"组件「{name}」在路由表里没有同名页面")

    if not picked:
        return None, tuple(misses)

    pages = {candidate.source_file for _, candidate in picked}
    if len(pages) > 1:
        listing = " | ".join(f"{name}→{candidate.source_file}" for name, candidate in picked)
        return (
            PageLocation(
                method="route",
                notes=(
                    f"组件的路由指向不同页面，不自动派生：{listing}",
                    *misses,
                ),
            ),
            (),
        )

    consensus = len(picked) > 1
    return (
        PageLocation(
            candidates=(picked[0][1],),
            terms=tuple(name for name, _ in picked),
            method="route",
            notes=(
                "按 Jira 组件名在路由表里定位"
                + (f"（{len(picked)} 个组件指向同一页面）" if consensus else ""),
                *misses,
            ),
        ),
        (),
    )


def locate_page(
    root: Path,
    text: str,
    *,
    evidence: LocateEvidence | None = None,
    limit: int = 6,
    prefer: tuple[str, ...] = (),
) -> PageLocation:
    """Find the source file a requirement is about.

    Two anchors, tried in order of how much they are worth:

    1. **The router.** A Jira component named 订单中心 and a route whose
       ``meta.title`` is 订单中心 are the same words chosen by the product, not
       a resemblance. On the real CargoWareX mirror this resolves 14 of 15
       CCTOWER components; the graph resolves 1 of 7 and cannot run at all
       without ``graph.json``.
    2. **The code graph**, reached through the product's own i18n catalogue.

    When ``evidence`` carries the structured ticket fields, each source is
    searched on its own and weighted. Otherwise the plain ``text`` is treated as
    one description-level source, which is what a manual requirement has.
    """

    repo_root = Path(root)
    routes = route_index(repo_root)
    route_notes: tuple[str, ...] = ()
    if routes is not None:
        located, route_notes = _locate_by_route(routes, evidence, prefer=prefer)
        if located is not None:
            return located

    graph = graph_index(repo_root)
    if graph is None:
        notes = ["源码仓库还没有代码图谱（graphify-out/graph.json 缺失），无法定位页面"]
        if routes is None:
            notes.append("源码仓库也没有路由表（src/router 不存在），组件名无从锚定")
        return PageLocation(method="none", notes=(*notes, *route_notes))

    notes: list[str] = list(route_notes)
    catalogue = i18n_index(repo_root)
    if catalogue is None:
        notes.append("源码仓库没有中文 i18n 词表，只能用文中的英文标识符检索")

    if evidence is not None and evidence.has_content:
        method = "i18n+graph(多源)" if catalogue is not None else "graph(多源)"
        hits, terms = _hits_from_evidence(graph, evidence, catalogue, limit=limit, notes=notes)
        notes.append(
            f"按 {len(evidence.strong_sources())} 个强信号字段分别检索后合并"
            f"（来源：{'、'.join(dict.fromkeys(s.field for s in evidence.ranked()[:4]))}）"
        )
        if evidence.unread:
            notes.extend(evidence.unread)
    else:
        terms = _terms_from(catalogue, text, notes, tag="需求正文")
        method = "i18n+graph" if catalogue is not None and terms else "graph"
        hits = graph.search(terms, limit=max(limit * 3, 9)) if terms else []

    if not terms:
        return PageLocation(method="none", notes=(*notes, "需求里没有可用于检索的标识符"))

    candidates = _page_candidates(hits, limit)
    if not candidates:
        # A requirement's terms land in ``src/api/*.ts`` far more often than in
        # the .vue file: the API module names every field, the page uses them.
        # Climbing one import hop recovers the page that renders them.
        candidates, climbed = _climb_to_pages(graph, hits, terms, limit)
        if climbed:
            notes = (*notes, f"检索命中模块文件，沿 import 关系向上定位到页面 {climbed}")
    if not candidates:
        weak = [hit for hit in hits if _is_page(hit.source_file)]
        if weak:
            shared = "、".join(weak[0].reasons[:2]) or "弱匹配"
            notes = (
                *notes,
                f"命中 {len(weak)} 个页面，但检索词只与其共享通用字段名（{shared}），"
                "不足以认定是目标页面",
            )
        return PageLocation(
            terms=tuple(terms),
            method=method,
            notes=(*notes, f"图谱里没有可渲染的匹配文件（检索词：{'、'.join(terms[:5])}）"),
        )

    location = PageLocation(candidates=candidates, terms=tuple(terms), method=method, notes=tuple(notes))
    if location.ambiguous:
        # Measured on the real mirror: a supplier requirement hit the zip-code
        # page, then a booking page, then a template list — each by sharing one
        # generic field name. Picking the winner and saying "may be wrong" still
        # hands the reviewer a confident, wrong screen, so this refuses instead.
        shortlist = " | ".join(
            f"{item.source_file}({item.score:.1f})" for item in candidates[:3]
        )
        return PageLocation(
            method=method,
            terms=tuple(terms),
            notes=(
                f"页面候选不唯一，不自动派生。候选：{shortlist}",
                "请在需求里指明页面路径，或为该项目补充 Jira 组件字段后再跑",
                *notes,
            ),
        )
    return location


# --- deriving ---------------------------------------------------------------


def derive_from_source(
    spec: RepoSpec,
    text: str,
    *,
    evidence: LocateEvidence | None = None,
    timeout_s: int = 240,
    renderer: SsrRenderer | None = None,
    verify: bool = False,
) -> DerivationOutcome:
    """Locate this requirement's page and read its source. Never invents a page.

    Rendering is off by default. The prototype is transcribed from the page's
    own SFC — block order, spacing, button groups, dialog skeleton — which is
    what the product's prototyping rules ask for and what a reviewer can check
    against the file. A render only adds a *measurement* (``class_overlap``) on
    top of that, so it runs only when ``verify`` is set, and its absence is not
    a failure: a mirror without `node_modules` still yields a usable baseline,
    it just cannot back the fidelity claim with a number.
    """

    if not spec.renderable:
        return DerivationOutcome(
            ok=False,
            reason=f"{spec.key} 不是前端仓库（{spec.framework}），没有可复刻的页面源码",
        )
    root = spec.root()
    if not root.is_dir():
        return DerivationOutcome(
            ok=False, reason=f"源码镜像未同步：{root} 不存在，先跑同步脚本"
        )

    prefer = (spec.preferred_app,) if spec.preferred_app else ()
    location = locate_page(root, text, evidence=evidence, prefer=prefer)
    if not location.ok:
        return DerivationOutcome(
            ok=False,
            location=location,
            reason=location.notes[0] if location.notes else "未能在源码仓库中定位到对应页面",
        )

    # Read the page *and* the components that draw it. One SFC is not enough in
    # this product: the located file is frequently a 1.3 KB frame around a 450 KB
    # grid, and a baseline taken from the frame alone transcribes to an empty
    # screen while still claiming to be derived.
    baseline = expand_baseline(root, location.page, spec.key)
    if not baseline.usable:
        return DerivationOutcome(
            ok=False,
            location=location,
            reason=f"页面 {location.page} 没有可复刻的内容：{baseline.note or '模板为空'}",
        )
    if baseline.thin:
        return DerivationOutcome(
            ok=False,
            location=location,
            reason=(
                f"页面 {location.page} 展开 {len(baseline.expanded)} 个子组件后仍只有 "
                f"{len(baseline.template)} 字模板、{len(baseline.classes)} 个 class，"
                "不足以复刻界面（界面可能由运行时配置渲染）"
            ),
        )

    if not verify:
        return DerivationOutcome(ok=True, baseline=baseline, location=location)

    # From here on everything is optional measurement. A missing toolchain, a
    # missing dependency or a crashed render downgrades the evidence, it does
    # not throw away the baseline that was already read.
    plan = spec.ssr
    if not plan.script:
        return DerivationOutcome(
            ok=True,
            baseline=baseline,
            location=location,
            reason=f"{spec.key} 的框架 {spec.framework} 没有配套的 SSR 校验器，已跳过校验",
        )

    absent = missing_packages(root, plan.mirror_requirements)
    base = root
    if plan.toolchain:
        base = toolchain_dir(plan.toolchain)
        absent = absent or missing_packages(base, plan.toolchain_requirements)
    if absent:
        return DerivationOutcome(
            ok=True,
            baseline=baseline,
            location=location,
            reason=f"未做渲染校验（缺 {'、'.join(absent)}），保真度未经机器测量",
        )

    script = ensure_render_script(base, plan.script)
    if script is None:
        return DerivationOutcome(
            ok=True,
            baseline=baseline,
            location=location,
            reason=f"未做渲染校验（包内缺少 {plan.script}）",
        )

    renderer = renderer or SsrRenderer(script, timeout_s=timeout_s)
    prototype = PrototypeDeriver(renderer).derive(spec, f"/{location.page}")
    if not prototype.html:
        return DerivationOutcome(
            ok=True,
            baseline=baseline,
            location=location,
            reason=f"未做渲染校验（{location.page} 渲染失败：{prototype.fidelity_note}）",
        )
    return DerivationOutcome(
        ok=True, prototype=prototype, baseline=baseline, location=location
    )
