"""Graph port: answer "which source file is this requirement about?".

The prototype step used to guess a page from the requirement text alone, which
produced a generic admin shell with invented class names. A prototype can only be
1:1 with the product if it is *derived* from the real page, and that needs a
deterministic answer to "which file".

This port reads the graph ``graphify extract --code-only`` writes
(``graphify-out/graph.json``). That extraction is pure tree-sitter: no model, no
API key, so locating a file is free and repeatable. The graph gives us two things
the file system alone cannot:

* ``node.source_file`` — every symbol already knows which file defines it;
* ``node.label`` — the identifier a Chinese requirement maps onto via i18n.

Dependency walking is deliberately *not* graph-based. Measured on a real repo,
``graphify`` emits ``imports_from`` edges whose target id is the resolved module
path (``octop_infra_pm_ports_base``) while the node itself is keyed ``base``, so
following those edges silently returns nothing. ``closure()`` therefore reads the
import statements out of the file directly and resolves them against the tree,
which is exact and does not depend on the extractor's symbol resolution.

Nothing here invents a hit. When the graph is missing or nothing matches, callers
get ``None``/an empty list and must say so; the gates reject a prototype that
claims fidelity it cannot back with a file.
"""

from __future__ import annotations

import json
import math
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GRAPH_DIRNAME = "graphify-out"
GRAPH_FILENAME = "graph.json"

# Pages live under these directories in the front-ends we support. A hit whose
# file sits in one of them is a better answer than a hit in `utils/`.
_PAGE_DIR_HINTS = ("/views/", "/pages/", "/screens/", "/routes/")
_COMPONENT_DIR_HINTS = ("/components/",)

# Extension of the artefact the prototype step can actually derive from.
_DERIVABLE_SUFFIXES = (".vue", ".tsx", ".jsx", ".svelte")

# `graphify` slices module docstrings into their own nodes (`base_rationale_1`),
# which makes prose look like a symbol. They keep their `source_file`, so the
# path still matches — they are only excluded from label matching.
_RATIONALE_MARKER = "_rationale_"

# `import X from './X.vue'` / `require('@/utils/a')` / dynamic `import('...')`.
_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:type\s+)?(?:[^'"]*?\s+from\s+)?|require\s*\(\s*|import\s*\(\s*|export\s+[^'"]*?\s+from\s+)['"]([^'"]+)['"]"""
)
# `src/` is the default root in every front-end here; `@`/`~` are the aliases
# they use for it. A repo that maps them elsewhere still resolves through the
# relative-import path, and a miss shows up as an empty closure rather than a
# wrong file.
_ALIAS_ROOTS = {"@": "src", "~": "src", "~@": "src", "@@": "src"}
_INDEX_FILES = (
    "",
    "/index.vue",
    "/index.ts",
    "/index.tsx",
    "/index.js",
)
_EXTENSIONS = ("", ".vue", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".svelte")


# `code` inside `zipcode` is a string coincidence, not a signal. A term has to
# be long enough to be a word before an exact hit on it means anything.
_STRONG_TERM_MIN_LEN = 5
# Share of the repo a term may touch and still be allowed to anchor a page.
# Measured on cwx-platform-admin: `management` reaches roughly this share of the
# view tree while `permission` reaches two files, so the cutoff separates them.
_STRONG_SPREAD_RATIO = 0.02


@dataclass(frozen=True)
class GraphHit:
    """One candidate file for a requirement, with the reason it surfaced."""

    source_file: str
    score: float
    matched_labels: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    # A term either names a symbol exactly or appears in the file path. Anything
    # weaker is "this file mentions some word in common", which is how a supplier
    # requirement ended up on the zip-code page.
    strong: bool = False

    @property
    def derivable(self) -> bool:
        return self.source_file.endswith(_DERIVABLE_SUFFIXES)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "score": round(self.score, 3),
            "matched_labels": list(self.matched_labels),
            "reasons": list(self.reasons),
            "strong": self.strong,
            "derivable": self.derivable,
        }


@dataclass
class _Node:
    id: str
    label: str
    source_file: str
    location: str = ""


@dataclass
class GraphIndex:
    """An in-memory, read-only view over one repository's ``graph.json``."""

    repo_root: Path
    nodes: dict[str, _Node] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    _by_file: dict[str, list[_Node]] = field(default_factory=dict, repr=False)
    _degree: dict[str, int] = field(default_factory=dict, repr=False)
    _reverse: dict[str, list[str]] | None = field(default=None, repr=False)

    # --- construction -------------------------------------------------------

    @classmethod
    def graph_path(cls, repo_root: Path) -> Path:
        return Path(repo_root) / GRAPH_DIRNAME / GRAPH_FILENAME

    @classmethod
    def load(cls, repo_root: Path) -> GraphIndex | None:
        """Return the index, or ``None`` when no graph has been built yet."""

        root = Path(repo_root)
        path = cls.graph_path(root)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A truncated graph (extraction killed mid-write) must read as
            # "no graph", never as "no matches" — the caller reports the
            # difference to the user.
            return None
        index = cls(repo_root=root)
        index._ingest(payload)
        return index

    def _ingest(self, payload: dict[str, Any]) -> None:
        for raw in payload.get("nodes") or []:
            node_id = str(raw.get("id") or "")
            if not node_id:
                continue
            node = _Node(
                id=node_id,
                label=str(raw.get("label") or node_id),
                source_file=str(raw.get("source_file") or ""),
                location=str(raw.get("source_location") or ""),
            )
            self.nodes[node_id] = node
            if node.source_file:
                self._by_file.setdefault(node.source_file, []).append(node)

        for raw in payload.get("edges") or []:
            source = str(raw.get("source") or "")
            target = str(raw.get("target") or "")
            if not source or not target:
                continue
            self.edges.append(raw)
            self._degree[source] = self._degree.get(source, 0) + 1
            self._degree[target] = self._degree.get(target, 0) + 1

    # --- queries ------------------------------------------------------------

    @property
    def file_count(self) -> int:
        return len(self._by_file)

    def files(self) -> list[str]:
        return sorted(self._by_file)

    def search(self, terms: list[str], *, limit: int = 5) -> list[GraphHit]:
        """Rank files by how well their symbols match ``terms``.

        Matching is literal and case-insensitive on purpose: the caller is
        expected to have already turned Chinese prose into identifiers (see
        ``ports.i18n``), and a fuzzy match here would hide a bad translation
        behind a plausible-looking wrong file.
        """

        normalised = [t.strip().lower() for t in terms if t and t.strip()]
        if not normalised or not self._by_file:
            return []

        # Pass 1: how many files each term touches. A term that matches a fifth
        # of the repo ("code", "time", "status") is not evidence of anything, yet
        # summing every symbol it matches made a 700-symbol file score 166 and
        # win outright. Rarity is the signal; volume is the noise.
        spread = dict.fromkeys(normalised, 0)
        for source_file, nodes in self._by_file.items():
            file_lc = source_file.lower()
            labels = [n.label.lower() for n in nodes if _RATIONALE_MARKER not in n.id]
            for term in normalised:
                if term in file_lc or any(term in label for label in labels):
                    spread[term] += 1
        weight = {
            term: 1.0 / (1.0 + math.log1p(spread[term])) for term in normalised
        }
        # A term that appears in a noticeable share of the repo cannot *anchor* a
        # page, however exactly it matches. Chinese area names almost always end
        # in a structural suffix — 管理/列表/查询 — and each maps to one identifier
        # (`management`, `list`, `query`) that every sibling page also carries. So
        # "用户权限管理" scored `permission` and `management` alike, every
        # `*management.vue` tied, and the locator refused a question it had
        # actually answered correctly. Rarity is what makes a match mean something.
        rare_enough = max(3, int(self.file_count * _STRONG_SPREAD_RATIO))
        can_anchor = {term: spread[term] <= rare_enough for term in normalised}

        # Pass 2: one score per (term, file) — the best symbol match, not the
        # sum over all of them. Otherwise a file with many similarly-named
        # helpers outranks the file that actually is about the term.
        per_file: dict[str, dict[str, Any]] = {}
        for term in normalised:
            factor = weight[term]
            for source_file, nodes in self._by_file.items():
                file_lc = source_file.lower()
                # `contract-parsing` must answer a query for `contractParsing`:
                # separators are a spelling difference, not a different word.
                path_flat = file_lc.replace("-", "").replace("_", "").replace("/", "")
                best = 0.0
                best_label = ""
                best_degree = 0
                reasons: list[str] = []
                exact_label = False
                for node in nodes:
                    if _RATIONALE_MARKER in node.id:
                        continue
                    label_lc = node.label.lower()
                    score = 0.0
                    if label_lc == term:
                        score += 10.0
                        exact_label = True
                    elif term in label_lc:
                        # Prefer the shortest label containing the term: asking
                        # for "supplier" should rank `SupplierTable` above
                        # `SupplierTableColumnConfigDialogHelper`.
                        score += 6.0 * (len(term) / max(len(label_lc), 1))
                    if score > best:
                        best, best_label, best_degree = score, node.label, self._degree.get(node.id, 0)
                in_path = term in file_lc or term in path_flat
                if in_path:
                    best += 3.0
                    reasons.append(f"path~{term}")
                if best <= 0:
                    continue
                if exact_label:
                    reasons.append(f"label=={term}")
                elif best_label and not reasons:
                    reasons.append(f"label~{term}")
                # A symbol nobody imports is usually dead code or a type alias;
                # require some connectivity before trusting it.
                best += min(math.log1p(best_degree), 2.0)

                entry = per_file.setdefault(
                    source_file, {"score": 0.0, "labels": [], "reasons": [], "strong": False}
                )
                entry["score"] += best * factor
                if (
                    len(term) >= _STRONG_TERM_MIN_LEN
                    and can_anchor[term]
                    and (exact_label or in_path)
                ):
                    entry["strong"] = True
                if best_label and best_label not in entry["labels"]:
                    entry["labels"].append(best_label)
                entry["reasons"].extend(r for r in reasons if r not in entry["reasons"])

        hits: list[GraphHit] = []
        for source_file, entry in per_file.items():
            score = float(entry["score"])
            lowered = f"/{source_file.lower()}"
            if any(hint in lowered for hint in _PAGE_DIR_HINTS):
                score += 4.0
            elif any(hint in lowered for hint in _COMPONENT_DIR_HINTS):
                score += 2.0
            if source_file.endswith(_DERIVABLE_SUFFIXES):
                score += 2.0
            hits.append(
                GraphHit(
                    source_file=source_file,
                    score=score,
                    matched_labels=tuple(entry["labels"][:5]),
                    reasons=tuple(entry["reasons"][:5]),
                    strong=bool(entry["strong"]),
                )
            )

        hits.sort(key=lambda h: (-h.score, h.source_file))
        return hits[:limit]

    def imports_of(self, source_file: str) -> list[str]:
        """Repo-relative files that ``source_file`` imports, in source order."""

        text = self.read(source_file)
        if not text:
            return []
        found: list[str] = []
        for match in _IMPORT_RE.finditer(text):
            resolved = self._resolve_import(match.group(1), source_file)
            if resolved and resolved not in found:
                found.append(resolved)
        return found

    def closure(
        self,
        source_file: str,
        *,
        depth: int = 2,
        max_files: int = 12,
    ) -> list[str]:
        """Files a page pulls in, breadth-first.

        This is the set the prototype must reuse rather than reinvent: if the
        real page renders ``AdvancedTable``, the derived prototype has to render
        it too, or the two are not 1:1. Resolution is by reading imports, not by
        trusting graph edges (see the module docstring for why).
        """

        seen: set[str] = {source_file}
        ordered: list[str] = []
        frontier = [source_file]
        for _ in range(max(depth, 1)):
            nxt: list[str] = []
            for current in frontier:
                for imported in self.imports_of(current):
                    if imported in seen:
                        continue
                    seen.add(imported)
                    ordered.append(imported)
                    nxt.append(imported)
                    if len(ordered) >= max_files:
                        return ordered
            if not nxt:
                break
            frontier = nxt
        return ordered

    def importers(self, source_file: str, *, max_files: int = 8) -> list[str]:
        """Which files import ``source_file`` — used to find the page behind a module.

        Backed by a reverse index built in one pass, because the alternative
        (re-reading every source file per query) made this O(files) per call and
        the page locator calls it once per candidate.
        """

        target = source_file.lstrip("./")
        return self.reverse_index().get(target, [])[:max_files]

    def reverse_index(self) -> dict[str, list[str]]:
        """``imported file -> [importers]``, built once per index instance.

        Reading 700-odd files costs a second or two on first use; the locator
        then answers "who imports this?" in constant time. Without this a
        requirement whose terms land in ``src/api/*.ts`` could never reach the
        page that renders them.
        """

        if self._reverse is not None:
            return self._reverse
        reverse: dict[str, list[str]] = {}
        for source_file in self._by_file:
            for imported in self.imports_of(source_file):
                bucket = reverse.setdefault(imported, [])
                if source_file not in bucket:
                    bucket.append(source_file)
        self._reverse = reverse
        return reverse

    # --- file access --------------------------------------------------------

    def read(self, source_file: str, *, limit: int = 60_000) -> str:
        path = self.repo_root / source_file
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            return ""

    def _resolve_import(self, spec: str, from_file: str) -> str | None:
        """Turn an import specifier into a repo-relative file, if it is local."""

        spec = (spec or "").strip()
        if not spec or spec.startswith(("http://", "https://", "//")):
            return None
        base = Path(from_file).parent
        if spec.startswith("."):
            candidate = (base / spec).as_posix()
        elif spec.startswith(("~", "@")):
            head, _, rest = spec.partition("/")
            root = _ALIAS_ROOTS.get(head)
            if not root or not rest:
                return None
            candidate = f"{root}/{rest}"
        else:
            return None  # bare package specifier: node_modules, not our source

        normalised = _normalise(candidate)
        for suffix in _EXTENSIONS:
            for index in _INDEX_FILES:
                probe = f"{normalised}{suffix}{index}"
                if (self.repo_root / probe).is_file():
                    return probe
        return None


def _normalise(path: str) -> str:
    """Collapse ``.``/``..`` and leading slashes into a repo-relative path."""

    return posixpath.normpath(path).lstrip("/")
