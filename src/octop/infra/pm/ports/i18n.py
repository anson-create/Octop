"""i18n port: turn Chinese prose into the identifiers the code actually uses.

A Jira ticket is written in Chinese ("派单供应商列表加就绪状态筛选"); the code is
written in identifiers (``vendor``, ``readyState``, ``AdvancedFilter``). Nothing
in a tree-sitter graph bridges that gap on its own, so a naive graph query for
"供应商" returns nothing and the prototype step falls back to inventing a page.

The bridge already exists in the product: every user-facing Chinese string is an
i18n value with an English key, and the module the key lives in names the feature
area. ``vendor: '供应商'`` in ``advanced-filter.ts`` is both a search term and a
pointer at the component that owns the filter UI.

Matching is deliberately literal (character bigrams, no embeddings): a wrong
translation must show up as "no match", not as a confident wrong file.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Where Chinese catalogues live across the front-ends we support.
_CATALOGUE_DIRS = (
    "src/i18n/modules/zh-CN",
    "src/i18n/modules/zh",
    "src/i18n/locales/zh-CN",
    "src/locales/zh-CN",
    "src/i18n/zh-CN",
    "src/i18n/zh",
    "src/lang/zh-CN",
)
_CATALOGUE_FILES = (
    "src/i18n/zh-CN.ts",
    "src/i18n/zh.ts",
    "src/locales/zh-CN.ts",
    "src/locales/zh.ts",
)
_CATALOGUE_SUFFIXES = (".ts", ".js", ".json")

# `key: '中文'` / `"key": "中文"`. Values without a CJK character are skipped:
# an English-only catalogue cannot help a Chinese requirement.
_ENTRY_RE = re.compile(
    r"""(?P<key>[A-Za-z_$][\w$]*)\s*:\s*(?P<quote>['"`])(?P<value>(?:\\.|(?!(?P=quote)).)*)(?P=quote)""",
    re.DOTALL,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _cjk_only(text: str) -> str:
    return "".join(_CJK_RE.findall(text or ""))


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


@dataclass(frozen=True)
class I18nEntry:
    key: str
    module: str
    value: str
    file: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "module": self.module, "value": self.value, "file": self.file}


@dataclass(frozen=True)
class I18nMatch:
    entry: I18nEntry
    score: float

    def as_dict(self) -> dict[str, object]:
        return {"score": round(self.score, 3), **self.entry.as_dict()}


@dataclass
class I18nIndex:
    """A read-only index of one repository's Chinese catalogue."""

    repo_root: Path
    entries: list[I18nEntry] = field(default_factory=list)
    files_scanned: tuple[str, ...] = ()
    _df: Counter[str] = field(default_factory=Counter, repr=False)

    @classmethod
    def load(cls, repo_root: Path) -> I18nIndex | None:
        root = Path(repo_root)
        if not root.is_dir():
            return None
        catalogue_files = cls._catalogue_files(root)
        if not catalogue_files:
            return None
        index = cls(repo_root=root, files_scanned=tuple(str(p.relative_to(root)) for p in catalogue_files))
        for path in catalogue_files:
            index._ingest(path)
        if not index.entries:
            return None
        index._build_df()
        return index

    @staticmethod
    def _catalogue_files(root: Path) -> list[Path]:
        found: list[Path] = []
        for rel in _CATALOGUE_DIRS:
            directory = root / rel
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix in _CATALOGUE_SUFFIXES:
                    found.append(path)
        for rel in _CATALOGUE_FILES:
            path = root / rel
            if path.is_file():
                found.append(path)
        return found

    def _ingest(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        module = path.stem
        rel = str(path.relative_to(self.repo_root))
        for match in _ENTRY_RE.finditer(text):
            value = match.group("value")
            if not _CJK_RE.search(value):
                continue
            self.entries.append(
                I18nEntry(
                    key=match.group("key"),
                    module=module,
                    value=value[:200],
                    file=rel,
                )
            )

    # --- queries ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    def _build_df(self) -> None:
        """Count how many entries each Chinese bigram appears in.

        Without this, `状态` / `列表` / `原因` match a quarter of the catalogue
        and outrank the one entry that actually matters. Rarity is the signal.
        """

        df: Counter[str] = Counter()
        for entry in self.entries:
            for bigram in _bigrams(_cjk_only(entry.value)):
                df[bigram] += 1
        self._df = df

    def _idf(self, bigram: str) -> float:
        total = max(len(self.entries), 1)
        seen = self._df.get(bigram, 0)
        return math.log(1 + total / (1 + seen))

    def match(self, text: str, *, limit: int = 12, threshold: float = 0.5) -> list[I18nMatch]:
        """Find catalogue entries whose Chinese overlaps the requirement text.

        Score is the share of the *entry's* rarity-weighted bigrams that the
        requirement covers — normalising by the requirement instead would bury
        every answer under a long ticket body. Rarity does the disambiguation:
        `供应商` outranks `状态` even when both are fully contained, and a
        two-character value needs more than containment to be trusted.
        """

        cjk = _cjk_only(text)
        if len(cjk) < 2:
            return []
        wanted = _bigrams(cjk)
        matches: list[I18nMatch] = []
        for entry in self.entries:
            value_cjk = _cjk_only(entry.value)
            if len(value_cjk) < 2:
                continue
            have = _bigrams(value_cjk)
            overlap = wanted & have
            if not overlap:
                continue
            budget = sum(self._idf(b) for b in have) or 1.0
            score = sum(self._idf(b) for b in overlap) / budget
            contained = value_cjk in cjk
            if contained:
                score *= 1.15
            # A two-character value is a single bigram: `状态`, `列表`, `原因`
            # all satisfy it, so it must clear a higher bar to be reported.
            if len(value_cjk) == 2 and not contained:
                score *= 0.55
            if score < threshold:
                continue
            matches.append(I18nMatch(entry=entry, score=min(score, 1.0)))
        matches.sort(key=lambda m: (-m.score, -len(_cjk_only(m.entry.value)), m.entry.key))
        return matches[:limit]

    def terms(self, text: str, *, limit: int = 8) -> list[str]:
        """The identifiers to hand a graph query, most specific first."""

        seen: list[str] = []
        for match in self.match(text, limit=limit * 3):
            key = match.entry.key
            # Single- and two-letter keys (`in`, `to`, `of`) are catalogue
            # plumbing, not search terms; feeding them to a graph query only
            # adds noise.
            if len(key) < 3 or key in seen:
                continue
            seen.append(key)
            if len(seen) >= limit:
                break
        return seen

    def module_heat(self, text: str) -> dict[str, float]:
        """Which feature areas the requirement text is closest to."""

        heat: dict[str, float] = {}
        for match in self.match(text, limit=40):
            heat[match.entry.module] = heat.get(match.entry.module, 0.0) + match.score
        return dict(sorted(heat.items(), key=lambda kv: -kv[1]))
