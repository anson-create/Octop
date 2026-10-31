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


# UI nouns that end a screen's Chinese name. They say what kind of view it is, not
# which area it belongs to, so they are removed before matching against a
# catalogue whose own values ("管理", "订单管理") contain the same characters.
_STRUCTURE_SUFFIXES = (
    "管理",
    "列表",
    "查询",
    "筛选",
    "设置",
    "配置",
    "维护",
    "台账",
    "看板",
    "报表",
    "详情",
    "新增",
    "编辑",
    "页面",
    "模块",
    "功能",
    "中心",
)

# Names longer than this are treated as prose (a title or a description), where
# stripping a suffix would remove meaning instead of noise.
_SHORT_NAME_LEN = 10


def _strip_structure_suffix(text: str) -> str:
    """Drop one trailing UI noun so the domain word is what gets matched.

    Returns the input unchanged when nothing useful remains — "管理" on its own
    carries no area information and must not become an empty query.
    """

    cjk = _cjk_only(text)
    for suffix in _STRUCTURE_SUFFIXES:
        if not cjk.endswith(suffix):
            continue
        trimmed = cjk[: -len(suffix)]
        # Two characters are the shortest domain word worth matching (供应商, 费用).
        if len(trimmed) >= 2:
            return trimmed
    return text


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
    # How rare this entry's own wording is. Needed as a tie-breaker: every entry
    # the requirement fully contains scores 1.0, so without this the ranking fell
    # back to "longest value first" and `企业` lost its slot to `企业名称`.
    specificity: float = 0.0

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
            #
            # Containment does not exempt it. Chinese area names end in these
            # suffixes almost by grammar — 报关单管理 contains 管理 — and with the
            # containment bonus `management` scored a full 1.0, tying with the
            # only correct key (`customs`) and dragging a customs requirement
            # onto the menu-settings page through `menuManagement`.
            if len(value_cjk) == 2:
                score *= 0.55
            if score < threshold:
                continue
            matches.append(
                I18nMatch(
                    entry=entry,
                    score=min(score, 1.0),
                    specificity=sum(self._idf(b) for b in have) / max(len(have), 1),
                )
            )
        matches.sort(key=lambda m: (-m.score, -m.specificity, m.entry.key))
        return matches[:limit]

    def terms(self, text: str, *, limit: int = 8, relative: float = 0.55) -> list[str]:
        """The identifiers to hand a graph query, most specific first.

        A short area name is first stripped of its structural suffix. Chinese
        names for a screen are built as "<domain word><UI noun>" — 报关单管理,
        供应商列表, 费用查询 — and the UI noun repeats in every sibling name. Left
        in place, `管理` matches `订单管理` and `菜单管理` by two shared bigrams,
        outscoring the one entry that actually means "customs". Removing the
        suffix leaves the domain word, which is the part that identifies the area.

        The stripped pass is a preference, not a replacement: if it finds nothing,
        the original wording is tried, because plenty of catalogue values are
        themselves written with the suffix ("权限管理") and only the full name
        reaches them.

        ``relative`` then drops whatever still scores far below the best match.
        """

        cjk = _cjk_only(text)
        if len(cjk) <= _SHORT_NAME_LEN:
            stripped = _strip_structure_suffix(text)
            if stripped != text:
                picked = self._pick_keys(stripped, limit=limit, relative=max(relative, 0.7))
                if picked:
                    return picked
        return self._pick_keys(text, limit=limit, relative=relative)

    def _pick_keys(self, text: str, *, limit: int, relative: float) -> list[str]:
        matches = self.match(text, limit=limit * 6)
        if not matches:
            return []
        # A short area name admits only near-peers; long text is different — each
        # clause may legitimately match its own entry — so the ordinary threshold
        # applies there.
        floor_ratio = relative if len(_cjk_only(text)) > _SHORT_NAME_LEN else max(relative, 0.7)
        floor = matches[0].score * floor_ratio
        seen: list[str] = []
        # The same key usually appears in several modules (`companyName` in five),
        # so the window has to be wider than the number of keys wanted or the
        # duplicates consume every slot.
        for match in matches:
            if match.score < floor:
                continue
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
