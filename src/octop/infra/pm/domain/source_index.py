"""What the product already does, read from its own source, before writing a PRD.

A PRD written from the ticket alone invents endpoints. The steps that are
supposed to be factual — code probe, and the "现状" section of the PRD — had no
way to look anything up, so every statement about existing behaviour came from
the model's imagination and every gap list ended with "未接入代码库".

This module is the lookup. It answers one question: *which existing endpoints
and services does this requirement touch?* and it answers it with file paths and
line numbers, so a reader can check the claim.

Deliberately narrow:

* It reads only the repositories a project is mapped to (see ``repos.py``), never
  a keyword sweep across every checkout. Reading CargoWare's Java for a
  CargoWareX ticket would produce confident, wrong "current behaviour".
* It prefers the api layer, and falls back to the web layer when the api mirror
  is absent. CargoWareX's api tree is not mirrored on the host; its Vue app is,
  and "which page and which endpoint does this ticket touch" is answerable from
  the front end alone — a page that calls ``/mdm/airport/queryList`` is a fact
  about existing behaviour even without the Java behind it.
* It returns *structure* — mapping paths, method signatures, class names, route
  paths, endpoint calls — not whole files. A controller is 300 lines; the part
  that matters is four.
* It never claims more than it saw. No hits is a stated reason, not an empty
  success, and a layer that was skipped says why.

``grep`` does the file selection because it is one pass over 100 MB of Java with
the page cache doing the heavy lifting (measured on the deployment host: 0.1–3.7s
depending on whether the tree is warm). Python then reads only the shortlisted
files, which is where the summary comes from.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from octop.infra.pm.domain.repos import FRAMEWORK_JAVA, RepoSpec

# Continuous Chinese runs are the query terms. There is no segmenter here and
# there does not need to be: a 4-character window over a Chinese phrase lands on
# the word often enough, and precision comes from scoring rather than from
# segmentation.
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

# Words that appear in every ticket and distinguish nothing. Kept short on
# purpose: "列表" or "查询" do carry meaning in a title.
_STOPWORDS = frozenset(
    {
        "需求",
        "功能",
        "支持",
        "系统",
        "用户",
        "我们",
        "可以",
        "需要",
        "问题",
        "相关",
        "进行",
        "实现",
        "增加",
        "优化",
        "The",
        # Product names appear in every file of the product's own repository.
        # Matching "CargoWare" in a CargoWareX ticket selects the whole tree.
        "CargoWare",
        "CargoWareX",
        "WallTech",
        "walltech",
        "cargoware",
    }
)

_MAPPING = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(([^)]*)\)")
# The closing paren is deliberately not required: a real controller wraps its
# parameters onto the next line, and requiring ")" on the same line silently
# skipped every signature that mattered.
_METHOD_SIG = re.compile(r"^\s*(?:public|protected)\s+[\w<>\[\], ?.]+\s+(\w+)\s*\(")
_CLASS_DECL = re.compile(r"^\s*(?:public\s+)?(?:final\s+)?(?:class|interface|enum)\s+(\w+)")
_STRING_ARG = re.compile(r'"([^"]*)"')

# --- front end shapes, taken from the real mirror rather than guessed ---------
#
# ``src/api/basic-data/airport.ts`` declares a base path and calls
# ``http.post<PortRouteRelationVO[]>(`/mdm/airport/route/queryList`, { portCode })``
# under a one-line comment. "POST /mdm/airport/route/queryList -> queryRouteRelations"
# is the citable fact; the comment is the human label.
_API_BASE = re.compile(r"const\s+API_BASE\s*=\s*['\"]([^'\"]+)['\"]")
_HTTP_CALL = re.compile(
    # A generic can nest (``http.post<PageResultVO<AirportVO>>(...)``), so the
    # span between the verb and the paren is matched loosely instead of trying
    # to parse angle brackets. Missing the nested case silently dropped half the
    # calls in every api module.
    r"http\.(get|post|put|delete|patch)\b[^(]{0,120}?\(\s*[`'\"]([^`'\"]+)"
)
_COMMENT = re.compile(r"^\s*(?://+|/\*+|\*)\s*(.+?)\s*(?:\*/)?\s*$")
_METHOD_NAME = re.compile(
    r"^\s*(?:export\s+)?(?:const\s+)?(\w+)\s*[:=]\s*(?:async\s*)?\("
    r"|^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"
)
_ROUTE_PATH = re.compile(r"^\s*path:\s*['\"]([^'\"]*)['\"]")
_ROUTE_TITLE = re.compile(r"title:\s*['\"]([^'\"]+)['\"]")
_ROUTE_IMPORT = re.compile(r"import\(\s*['\"]([^'\"]+)['\"]")
_API_IMPORT = re.compile(r"from\s+['\"]@/api/([^'\"]+)['\"]")

_MAX_TERMS = 8
_MAX_READS = 60


@dataclass(frozen=True)
class SourceHit:
    """One place in the source that already does something related."""

    repo: str
    path: str
    lines: str
    kind: str  # endpoint | service | data | model | page | route | component
    excerpt: str
    score: int


@dataclass(frozen=True)
class SourceDigest:
    """The lookup's answer, including why it is empty when it is."""

    repo: str
    terms: tuple[str, ...] = ()
    hits: tuple[SourceHit, ...] = ()
    candidates: int = 0
    reason: str = ""
    layer: str = ""  # api | web
    # Layers tried before this one and what they said. Silently omitting them
    # would make "read the front end instead" look like "read everything".
    skipped: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.hits)

    @property
    def layer_label(self) -> str:
        if self.layer == "api":
            return "接口层源码"
        if self.layer == "web":
            return "前端源码"
        return "源码"

    def render(self, *, limit: int = 6, width: int = 2000) -> str:
        """The digest as prompt material — facts with paths, nothing else."""

        if not self.ok:
            return ""
        lines = [
            f"{self.repo}（{self.layer_label}）中与本次需求相关的既有实现"
            f"（静态检索，共 {self.candidates} 个候选文件）："
        ]
        for hit in self.hits[:limit]:
            lines.append(f"- {hit.excerpt}　[{hit.path}:{hit.lines}]")
        if self.skipped:
            lines.append(f"未检索：{'；'.join(self.skipped)}")
        text = "\n".join(lines)
        return text[:width]

    def as_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "layer": self.layer,
            "terms": list(self.terms),
            "candidates": self.candidates,
            "hits": [
                {
                    "path": hit.path,
                    "lines": hit.lines,
                    "kind": hit.kind,
                    "score": hit.score,
                    "excerpt": hit.excerpt,
                }
                for hit in self.hits
            ],
            "reason": self.reason,
            "skipped": list(self.skipped),
        }


def query_terms(text: str, *, limit: int = _MAX_TERMS) -> tuple[str, ...]:
    """The words worth grepping for, most specific first.

    Chinese has no segmenter here, so a long run is cut twice: four-character
    windows carry the phrase ("就绪状态"), and two-character windows catch the
    two-word business terms that never appear as a four-character phrase at all
    — "亿通账单需显示工作单号" only matches because of 亿通 / 账单 / 单号.

    The title is searched for terms before the body. A title is dense ("空港管理
    新增启用状态筛选"); the body is a paragraph, and its windows are mostly
    connective tissue ("也要带上", "导出也要"). Budgeting them equally is how the
    one word that identifies the feature gets pushed out of the query.
    """

    head, _, rest = (text or "").partition("\n")
    ordered = list(_window_terms(head, limit))
    if len(ordered) < limit:
        for term in _window_terms(rest, limit):
            if term not in ordered:
                ordered.append(term)
            if len(ordered) >= limit:
                break
    return tuple(ordered[:limit])


def _window_terms(text: str, limit: int) -> list[str]:
    """Four-character windows and two-character windows, interleaved.

    Not "long first, short last": a sentence-length ticket yields a dozen
    four-character windows and the two characters that name the subject — 空港,
    账单 — end up behind them and get cut. Measured on a real title: the file
    holding the answer matched only 空港 and was never in the query at all.
    """

    picked: list[str] = []
    for run in _CJK_RUN.findall(text or ""):
        if len(run) <= 6:
            picked.append(run)
        else:
            for start in range(0, len(run) - 3, 2):
                picked.append(run[start : start + 4])
        if len(run) >= 4:
            for start in range(0, len(run) - 1, 2):
                picked.append(run[start : start + 2])
    for ident in _IDENT.findall(text or ""):
        # Attachment ids and hashes are identifiers by the same pattern and say
        # nothing about the feature — "d63e7f9d621abc0eb118670493c9c4d" was the
        # eighth search term of a real ticket.
        if len(ident) >= 16 and sum(char.isdigit() for char in ident) >= 6:
            continue
        picked.append(ident)

    seen: dict[str, None] = {}
    for term in picked:
        cleaned = term.strip()
        if len(cleaned) < 2 or cleaned in _STOPWORDS:
            continue
        seen.setdefault(cleaned, None)

    # Stable sort by length: within one length, the order the words appear in
    # the title is kept, because the subject of a Chinese title comes first.
    long_terms = sorted((item for item in seen if len(item) >= 4), key=lambda item: -len(item))
    short_terms = sorted((item for item in seen if len(item) < 4), key=lambda item: -len(item))

    ordered: list[str] = []
    for index in range(max(len(long_terms), len(short_terms))):
        for bucket in (long_terms, short_terms):
            if index < len(bucket):
                ordered.append(bucket[index])
    return ordered[:limit]


def _grep_files(root: Path, terms: tuple[str, ...], suffixes: tuple[str, ...], timeout_s: float) -> list[Path]:
    """Files containing any term. Fixed-string grep: the terms are ticket prose."""

    if not terms:
        return []
    args = ["grep", "-rlF"]
    for suffix in suffixes:
        args += ["--include", suffix]
    # Excluded at the grep level, not only when filtering results: a front-end
    # mirror carries its node_modules (hundreds of MB), and walking it costs more
    # than the whole query. These directories never hold product source.
    for excluded in (
        "node_modules",
        "dist",
        ".git",
        "target",
        "__tests__",
        "__mocks__",
        "coverage",
        "graphify-out",
        "docs",
    ):
        args.append(f"--exclude-dir={excluded}")
    for term in terms:
        args += ["-e", term]
    args.append(str(root))
    try:
        # noqa justification: fixed argv, no shell, terms are passed as separate
        # arguments so a title containing quotes cannot become an option.
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        # 1 means "no match", which is an answer; anything else is a broken grep.
        return []
    out: list[Path] = []
    for line in proc.stdout.splitlines():
        path = Path(line.strip())
        name = path.name
        # A front-end mirror ships its build output and installed packages; both
        # contain the same business vocabulary thousands of times and nothing a
        # PRD should quote. Test files describe behaviour without being it.
        if any(
            token in str(path)
            for token in ("/target/", "/test/", "/node_modules/", "/dist/", "/.git/")
        ):
            continue
        if ".test." in name or ".spec." in name or name.endswith(".d.ts"):
            continue
        out.append(path)
    return out


def _kind_of(path: str) -> str:
    lowered = path.lower()
    if "controller" in lowered:
        return "endpoint"
    if "service" in lowered or "impl" in lowered:
        return "service"
    if any(token in lowered for token in ("mapper", "dao", "repository", "repo")):
        return "data"
    if lowered.endswith(".vue") or "/views/" in lowered:
        return "page"
    if "router" in lowered:
        return "route"
    if "/api/" in lowered:
        return "endpoint"
    return "model"


def _path_score(path: str, terms: tuple[str, ...]) -> int:
    """Cheap pre-filter: file and directory names that mention a term."""

    lowered = path.lower()
    score = 0
    for term in terms:
        if term.lower() in lowered:
            score += 6
    if "controller" in lowered:
        score += 2
    # A page or an api module is where the behaviour is; a shared component or a
    # type file may only mention the word.
    if "/views/" in lowered or "/api/" in lowered or "router" in lowered:
        score += 3
    if "/components/" in lowered:
        score -= 2
    return score


# Directories that read like implementation but are not: a constant table can
# mention 代收垫付 on forty lines without doing anything with it.
_LOW_VALUE_PATH = ("constant", "enums", "enum/", "exception", "/docs/", "i18n")


def _keyword_score(text: str, path: str, terms: tuple[str, ...]) -> int:
    """How much of *this requirement's* vocabulary the file actually speaks.

    Distinct terms, not raw occurrence counts: a controller that handles 代收垫付
    beats a constants file that lists the phrase on every other line.
    """

    hits = [term for term in terms if term in text]
    if not hits:
        return 0
    # A phrase window ("询价表单") says far more than a two-character word
    # ("表单"), which appears in half the repository. Six to one, not three to
    # one: the ranking has to separate them, not merely prefer one.
    score = sum(6 if len(term) >= 4 else 1 for term in hits) + len(hits)
    lowered = path.lower()
    if "controller" in lowered:
        score += 4
    if any(token in lowered for token in _LOW_VALUE_PATH):
        score -= 8
    if "/views/" in lowered or "/api/" in lowered or "router" in lowered:
        score += 4
    name = lowered.rsplit("/", 1)[-1]
    score += 4 * sum(1 for term in terms if len(term) >= 3 and term.lower() in name)
    return max(score, 0)


def _is_specific(text: str, terms: tuple[str, ...]) -> bool:
    """Did this file match a phrase, or only one generic two-character word?"""

    matched = [term for term in terms if term in text]
    return any(len(term) >= 4 for term in matched) or len(matched) >= 2


def _join_path(base: str, target: str) -> str:
    """Class-level prefix + method path, the way Spring resolves it."""

    left = (base or "").rstrip("/")
    right = (target or "").lstrip("/")
    if not left:
        return f"/{right}" if right else "(类级路径)"
    joined = f"{left}/{right}" if right else left
    return joined if joined.startswith("/") else f"/{joined}"


def _java_excerpt(
    text: str, terms: tuple[str, ...], *, name: str, max_pairs: int = 4
) -> tuple[str, str]:
    """Mappings paired with their method signature, plus the class name.

    This is the shape a reader wants: ``POST /chargeConfirmed/supplierCharge/
    advancedSearch -> supplierChargeAdvancedSearch``. Returning the whole
    controller instead would bury it.
    """

    lines = text.splitlines()
    first_line = 0
    last_line = 0
    pairs: list[str] = []
    pending: tuple[str, str, int] | None = None

    class_index = 0
    class_name = ""
    for index, line in enumerate(lines):
        declared = _CLASS_DECL.match(line)
        if declared:
            class_index = index
            class_name = declared.group(1)
            break

    # The class-level mapping is the prefix of every method path. Reporting
    # "POST list" instead of "POST /billing/list" is not a shorter path, it is a
    # path nobody can call or look up.
    base_path = ""
    if class_index:
        for line in reversed(lines[max(0, class_index - 8) : class_index]):
            prefix = _MAPPING.search(line)
            if prefix and prefix.group(1) == "Request":
                strings = _STRING_ARG.findall(prefix.group(2))
                if strings:
                    base_path = strings[0]
                break

    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        match = _MAPPING.search(stripped)
        if match:
            verb = "ANY" if match.group(1) == "Request" else match.group(1).upper()
            strings = _STRING_ARG.findall(match.group(2))
            pending = (verb, strings[0] if strings else "", index)
            first_line = first_line or index
            continue
        if pending is None:
            continue
        signature = _METHOD_SIG.match(line)
        if signature:
            verb, target, _at = pending
            path = _join_path(base_path, target)
            # Parameters often wrap onto the next line, and the line after that
            # is method body. Stop at the closing paren, or the excerpt starts
            # quoting implementation instead of the signature.
            parts: list[str] = []
            for part in lines[index - 1 : index + 2]:
                parts.append(part.strip())
                if ")" in part:
                    break
            tail = " ".join(parts)
            head = tail.split(")", 1)[0] + ")" if ")" in tail else tail
            # Drop the "public Result<...> queryBillingList" part: the method
            # name is already in the pair, so only the parameters are new.
            if "(" in head:
                head = head.split("(", 1)[1]
            params = " ".join(head.split())[:60]
            pairs.append(f"{verb} {path} -> {signature.group(1)} {params}")
            last_line = max(last_line, index)
            pending = None
            if len(pairs) >= max_pairs:
                break

    if not pairs:
        # No Spring annotations (a service, a VO): quote the first place the
        # vocabulary appears, with a line of context either side. Quoting every
        # occurrence made the span run to 900 lines, which is not citable.
        for index, line in enumerate(lines):
            if any(term in line for term in terms) and line.strip():
                start = max(0, index - 1)
                stop = min(len(lines), index + 2)
                for offset in range(start, stop):
                    text_line = lines[offset].strip()
                    if text_line:
                        pairs.append(text_line[:120])
                first_line = start + 1
                last_line = stop
                break

    label = class_name or name
    body = "；".join(pairs[:max_pairs]) if pairs else class_name
    excerpt = f"{label}: {body}" if body else label
    span = f"{first_line}-{last_line}" if first_line else ""
    return excerpt[:280], span


def _web_endpoint_pairs(
    lines: list[str], terms: tuple[str, ...] = (), *, max_pairs: int = 4
) -> tuple[list[str], int, int]:
    """``http.post(path)`` calls paired with the method that wraps them.

    Without the fallback to the front end, a project whose api layer is not
    mirrored gets nothing at all. This is the other half of "what already
    exists": the page already calls this endpoint, so the ticket is a change to
    it rather than a new feature.

    Calls whose surrounding comment mentions the ticket's words come first; an
    api module of forty endpoints quoted at random says nothing about this
    ticket.
    """

    base = ""
    for line in lines:
        found = _API_BASE.search(line)
        if found:
            base = found.group(1)
            break

    pairs: list[tuple[str, int, int, bool]] = []
    label = ""
    label_at = 0
    method = ""
    for index, line in enumerate(lines, start=1):
        call = _HTTP_CALL.search(line)
        if call:
            target = call.group(2)
            if "${" in target and base:
                target = re.sub(
                    r"\$\{(?:API_BASE|BASE|BASE_URL)\}",
                    base.rstrip("/"),
                    target,
                )
            parts = [f"{call.group(1).upper()} {target}"]
            if method:
                parts.append(f"-> {method}")
            # The comment describes the endpoint ("查询空港与航线关联列表"), and a
            # comment from twenty lines up describes the file, not this call.
            relevant = False
            if label and index - label_at <= 4:
                parts.append(f"（{label[:40]}）")
                relevant = any(term in label for term in terms)
            relevant = relevant or any(term.lower() in target.lower() for term in terms)
            pairs.append((" ".join(parts), index, index, relevant))
            method = ""
            continue
        comment = _COMMENT.match(line)
        if comment:
            text = comment.group(1).strip()
            # "/*" opens a doc block, "*/" closes it; neither is a label.
            if text and text not in ("*/", "/**") and not text.startswith("*"):
                label, label_at = text, index
            continue
        named = _METHOD_NAME.match(line)
        if named:
            method = (named.group(1) or named.group(2) or "").strip()

    picked = [item for item in pairs if item[3]] or pairs
    chosen = picked[:max_pairs]
    if not chosen:
        return [], 0, 0
    return (
        [item[0] for item in chosen],
        chosen[0][1],
        max(item[2] for item in chosen),
    )


def _route_targets(
    lines: list[str], *, terms: tuple[str, ...] = (), max_pairs: int = 4
) -> list[tuple[str, str, str, int, int]]:
    """``(path, view module, title, first line, last line)`` per mounted route.

    Kept as data rather than as strings because the view it names is the next
    thing worth reading: the page says which api modules it calls, and that is
    the answer to "where does this ticket land".

    Matching looks at the route's ``meta.title`` as well as its path and module.
    A router file holds every page in the product, so quoting its first four
    entries answers nothing; when no route mentions the ticket, the file simply
    is not a hit.
    """

    found: list[tuple[str, str, str, int, int]] = []
    pending: tuple[str, int] | None = None
    for index, line in enumerate(lines, start=1):
        declared = _ROUTE_PATH.search(line)
        if declared:
            pending = (declared.group(1), index)
            continue
        if pending is None:
            continue
        mounted = _ROUTE_IMPORT.search(line)
        if not mounted:
            continue
        path, at = pending
        title = ""
        for nearby in lines[index - 1 : index + 6]:
            named = _ROUTE_TITLE.search(nearby)
            if named:
                title = named.group(1)
                break
        found.append((path or "/", mounted.group(1), title, at, index))
        pending = None

    def relevance(item: tuple[str, str, str, int, int]) -> int:
        haystack = f"{item[0]} {item[1]} {item[2]}".lower()
        return sum(1 for term in terms if term.lower() in haystack)

    ranked = sorted(found, key=lambda item: (-relevance(item), item[3]))
    matched = [item for item in ranked if relevance(item) > 0]
    if terms and matched:
        # With terms at hand, an unrelated route is worse than no route.
        return matched[:max_pairs]
    if terms:
        return []
    return ranked[:max_pairs]


def _web_route_pairs(
    lines: list[str], terms: tuple[str, ...] = (), *, max_pairs: int = 4
) -> tuple[list[str], int, int]:
    """``path`` plus the view it mounts: the page a ticket is actually about."""

    routes = _route_targets(lines, terms=terms, max_pairs=max_pairs)
    pairs = [
        f"路由 {path} -> {target.replace('@/', '')}" + (f"（{title}）" if title else "")
        for path, target, title, _at, _end in routes
    ]
    if not routes:
        return [], 0, 0
    return pairs, routes[0][3], max(route[4] for route in routes)


def _web_excerpt(
    text: str, terms: tuple[str, ...], *, name: str, path: str
) -> tuple[str, str]:
    """What a front-end file contributes: a route, a page, or an endpoint call."""

    lines = text.splitlines()
    lowered = path.lower()
    if "router" in lowered:
        pairs, first_line, last_line = _web_route_pairs(lines, terms)
        label = name
    elif lowered.endswith(".vue") or "/views/" in lowered:
        modules: list[str] = []
        snippet = ""
        at = 0
        best = 0
        for index, line in enumerate(lines, start=1):
            imported = _API_IMPORT.search(line)
            if imported and len(modules) < 3 and imported.group(1) not in modules:
                modules.append(imported.group(1))
            # The best matching line, not the first: "表单" appears in a dozen
            # comments, and the one that also says 询价 is the useful quote.
            matched = [term for term in terms if term in line]
            if not matched or not line.strip():
                continue
            weight = len(matched) * 10 + max(len(term) for term in matched)
            if weight > best:
                best, snippet, at = weight, line.strip(), index
        pairs = []
        if modules:
            pairs.append("引用接口模块 " + "、".join(f"@/api/{item}" for item in modules))
        if snippet:
            pairs.append(f"相关代码 {snippet[:110]}")
        first_line = at
        last_line = at
        label = name
    else:
        pairs, first_line, last_line = _web_endpoint_pairs(lines, terms)
        label = name

    body = "；".join(pairs) if pairs else ""
    if not body:
        return "", ""
    excerpt = f"{label}: {body}"
    span = f"{first_line}-{last_line}" if first_line else ""
    return excerpt[:280], span


def _excerpt_for(
    spec: RepoSpec, text: str, terms: tuple[str, ...], *, name: str, path: str
) -> tuple[str, str]:
    if spec.framework == FRAMEWORK_JAVA:
        return _java_excerpt(text, terms, name=name)
    return _web_excerpt(text, terms, name=name, path=path)


def _display_name(relative: str) -> str:
    """``views/basic-data/airport/index.vue`` -> ``airport``.

    "index" names nothing; the directory above it names the feature.
    """

    stem = Path(relative).stem
    if stem in ("index", "__init__", "mod"):
        return Path(relative).parent.name or stem
    return stem


def _resolve_module(root: Path, target: str) -> Path | None:
    """``@/views/x/index.vue`` -> the file, with or without a ``src`` prefix."""

    cleaned = target.strip().replace("@/", "").lstrip("/")
    for candidate in (root / "src" / cleaned, root / cleaned):
        if candidate.is_file():
            return candidate
    return None


def _follow_routes(
    root: Path,
    hits: list[SourceHit],
    texts: dict[str, str],
    terms: tuple[str, ...],
    *,
    max_pages: int = 3,
) -> list[SourceHit]:
    """Pages behind the matched routes.

    Labels come from i18n, so a page file often contains none of the ticket's
    Chinese words and grep never reaches it. The route does reach it, and the
    page is what a reader wants: which page, and which api modules it calls.
    """

    seen = {hit.path for hit in hits}
    pages: list[SourceHit] = []
    for route_hit in [item for item in hits if item.kind == "route"][:2]:
        text = texts.get(route_hit.path)
        if not text:
            continue
        for _route_path, target, _title, _at, _end in _route_targets(
            text.splitlines(), terms=terms
        ):
            if len(pages) >= max_pages:
                return pages
            resolved = _resolve_module(root, target)
            if resolved is None:
                continue
            relative = str(resolved).replace(str(root).rstrip("/") + "/", "")
            if relative in seen:
                continue
            try:
                body = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # ``index.vue`` says nothing; its directory names the feature.
            name = _display_name(relative)
            excerpt, span = _web_excerpt(body, terms, name=name, path=relative)
            if not excerpt:
                continue
            seen.add(relative)
            pages.append(
                SourceHit(
                    repo=route_hit.repo,
                    path=relative,
                    lines=span,
                    kind="page",
                    excerpt=excerpt,
                    # Slightly below the route that led here, still above the
                    # generic files: the page is the point of the route.
                    score=max(route_hit.score - 1, 1),
                )
            )
    return pages


def search_repo(
    spec: RepoSpec,
    query: str,
    *,
    limit: int = 6,
    timeout_s: float = 20.0,
) -> SourceDigest:
    """Existing implementations that touch this requirement, or a stated reason."""

    root = spec.root()
    if not root.is_dir():
        return SourceDigest(
            repo=spec.key,
            layer=spec.layer,
            reason=f"镜像未同步：{root} 不存在（先同步源码再重新取证）",
        )
    terms = query_terms(query)
    if not terms:
        return SourceDigest(
            repo=spec.key, layer=spec.layer, reason="需求原文里没有可用于检索的业务词"
        )

    if spec.framework == FRAMEWORK_JAVA:
        suffixes = ("*.java",)
    elif spec.layer == "web":
        suffixes = ("*.vue", "*.ts", "*.js")
    else:
        suffixes = ("*.java",)

    files = _grep_files(root, terms, suffixes, timeout_s)
    if not files:
        return SourceDigest(
            repo=spec.key,
            layer=spec.layer,
            terms=terms,
            reason=f"{root} 里没有命中「{'、'.join(terms[:4])}」的文件",
        )

    # Read the shortlist only. 600 files can match "供应商"; reading all of them
    # would cost more than the answer is worth.
    ranked = sorted(files, key=lambda item: -_path_score(str(item), terms))[:_MAX_READS]
    hits: list[SourceHit] = []
    texts: dict[str, str] = {}
    for path in ranked:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        relative = str(path).replace(str(root).rstrip("/") + "/", "")
        excerpt, span = _excerpt_for(
            spec, text, terms, name=_display_name(relative), path=relative
        )
        if not excerpt:
            continue
        texts[relative] = text
        score = _keyword_score(text, relative, terms)
        hits.append(
            SourceHit(
                repo=spec.key,
                path=relative,
                lines=span,
                kind=_kind_of(relative),
                excerpt=excerpt,
                score=score,
            )
        )

    if spec.layer == "web":
        # A page that matched only 表单 is not evidence of anything — every form
        # in the product matches it. Drop those when something better exists,
        # rather than padding the list to six.
        specific = [hit for hit in hits if _is_specific(texts.get(hit.path, ""), terms)]
        if specific:
            hits = specific
        hits.extend(_follow_routes(root, hits, texts, terms))

    if not hits:
        return SourceDigest(
            repo=spec.key,
            layer=spec.layer,
            terms=terms,
            candidates=len(files),
            reason="命中的文件里没有可读的结构（可能是空文件或纯资源）",
        )

    # Endpoints first: a PRD talks about what the API already exposes far more
    # often than about which VO carries the field. Then pages: a route tells the
    # reader where the work happens.
    order = {"endpoint": 0, "page": 1, "route": 2, "service": 3, "data": 4}
    hits.sort(key=lambda hit: (-hit.score, order.get(hit.kind, 5), hit.path))
    return SourceDigest(
        repo=spec.key,
        layer=spec.layer,
        terms=terms,
        hits=tuple(hits[:limit]),
        candidates=len(files),
    )


def search_requirement(
    specs: Sequence[RepoSpec],
    query: str,
    *,
    limit: int = 6,
    timeout_s: float = 20.0,
) -> SourceDigest | None:
    """First layer that has something to say, in the order given.

    ``None`` means the requirement is mapped to no repository at all, which is a
    different fact from "mapped, but every layer came back empty" — the caller
    words its gap differently, so the two must not be flattened here.

    The api layer goes first because a mapping path is more precise than a page
    name. When its mirror is absent the front end still answers "which page and
    which endpoint", and the digest carries the skipped layer so a reader knows
    the Java behind it was never read.
    """

    if not specs:
        return None
    notes: list[str] = []
    first: SourceDigest | None = None
    for spec in specs:
        digest = search_repo(spec, query, limit=limit, timeout_s=timeout_s)
        if digest.ok:
            return replace(digest, skipped=tuple(notes))
        notes.append(f"{spec.key}：{digest.reason}")
        if first is None:
            first = digest
    if first is None:  # pragma: no cover - specs was non-empty
        return None
    # Every layer failed: the reason has to mention all of them, or the reader
    # will "fix" the one visible mirror and be surprised again.
    return replace(first, reason="；".join(notes), skipped=tuple(notes[1:]))
