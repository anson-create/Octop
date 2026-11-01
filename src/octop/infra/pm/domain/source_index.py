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
* It returns *structure* — mapping paths, method signatures, class names — not
  whole files. A controller is 300 lines; the part that matters is four.
* It never claims more than it saw. No hits is a stated reason, not an empty
  success.

``grep`` does the file selection because it is one pass over 100 MB of Java with
the page cache doing the heavy lifting (measured on the deployment host: 0.1–3.7s
depending on whether the tree is warm). Python then reads only the shortlisted
files, which is where the summary comes from.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
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
    }
)

_MAPPING = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(([^)]*)\)")
# The closing paren is deliberately not required: a real controller wraps its
# parameters onto the next line, and requiring ")" on the same line silently
# skipped every signature that mattered.
_METHOD_SIG = re.compile(r"^\s*(?:public|protected)\s+[\w<>\[\], ?.]+\s+(\w+)\s*\(")
_CLASS_DECL = re.compile(r"^\s*(?:public\s+)?(?:final\s+)?(?:class|interface|enum)\s+(\w+)")
_STRING_ARG = re.compile(r'"([^"]*)"')

_MAX_TERMS = 8
_MAX_READS = 60


@dataclass(frozen=True)
class SourceHit:
    """One place in the source that already does something related."""

    repo: str
    path: str
    lines: str
    kind: str  # endpoint | service | data | model
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

    @property
    def ok(self) -> bool:
        return bool(self.hits)

    def render(self, *, limit: int = 6, width: int = 2000) -> str:
        """The digest as prompt material — facts with paths, nothing else."""

        if not self.ok:
            return ""
        lines = [f"{self.repo} 中与本次需求相关的既有实现（静态检索，共 {self.candidates} 个候选文件）："]
        for hit in self.hits[:limit]:
            lines.append(f"- {hit.excerpt}　[{hit.path}:{hit.lines}]")
        text = "\n".join(lines)
        return text[:width]

    def as_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
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
        }


def query_terms(text: str, *, limit: int = _MAX_TERMS) -> tuple[str, ...]:
    """The words worth grepping for, most specific first.

    Chinese has no segmenter here, so a long run is cut twice: four-character
    windows carry the phrase ("就绪状态"), and two-character windows catch the
    two-word business terms that never appear as a four-character phrase at all
    — "亿通账单需显示工作单号" only matches because of 亿通 / 账单 / 单号.

    Short terms are weighted down at scoring time rather than dropped here: one
    of them is often the only thing that matches, and the scoring can tell a
    real hit from a coincidence.
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
        picked.append(ident)

    seen: dict[str, None] = {}
    for term in picked:
        cleaned = term.strip()
        if len(cleaned) < 2 or cleaned in _STOPWORDS:
            continue
        seen.setdefault(cleaned, None)
    # Four-to-six characters first: that is the length of a real business term.
    # Shortest last, because they are the ones that match everything.
    ordered = sorted(seen, key=lambda item: (0 if 4 <= len(item) <= 6 else 1, -len(item), item))
    return tuple(ordered[:limit])


def _grep_files(root: Path, terms: tuple[str, ...], suffixes: tuple[str, ...], timeout_s: float) -> list[Path]:
    """Files containing any term. Fixed-string grep: the terms are ticket prose."""

    if not terms:
        return []
    args = ["grep", "-rlF"]
    for suffix in suffixes:
        args += ["--include", suffix]
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
        if "/target/" in str(path) or "/test/" in str(path):
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
    score = sum(3 if len(term) >= 4 else 1 for term in hits) + len(hits)
    lowered = path.lower()
    if "controller" in lowered:
        score += 4
    if any(token in lowered for token in _LOW_VALUE_PATH):
        score -= 8
    name = lowered.rsplit("/", 1)[-1]
    score += 4 * sum(1 for term in terms if len(term) >= 3 and term.lower() in name)
    return max(score, 0)


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
            reason=f"镜像未同步：{root} 不存在（先同步源码再重新取证）",
        )
    terms = query_terms(query)
    if not terms:
        return SourceDigest(repo=spec.key, reason="需求原文里没有可用于检索的业务词")

    if spec.framework == FRAMEWORK_JAVA:
        suffixes = ("*.java",)
    elif spec.layer == "web":
        suffixes = ("*.vue", "*.js", "*.ts")
    else:
        suffixes = ("*.java",)

    files = _grep_files(root, terms, suffixes, timeout_s)
    if not files:
        return SourceDigest(
            repo=spec.key,
            terms=terms,
            reason=f"{root} 里没有命中「{'、'.join(terms[:4])}」的文件",
        )

    # Read the shortlist only. 600 files can match "供应商"; reading all of them
    # would cost more than the answer is worth.
    ranked = sorted(files, key=lambda item: -_path_score(str(item), terms))[:_MAX_READS]
    hits: list[SourceHit] = []
    for path in ranked:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        relative = str(path).replace(str(root).rstrip("/") + "/", "")
        excerpt, span = _java_excerpt(text, terms, name=path.stem)
        if not excerpt:
            continue
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

    if not hits:
        return SourceDigest(
            repo=spec.key,
            terms=terms,
            candidates=len(files),
            reason="命中的文件里没有可读的结构（可能是空文件或纯资源）",
        )

    # Endpoints first: a PRD talks about what the API already exposes far more
    # often than about which VO carries the field.
    hits.sort(key=lambda hit: (-hit.score, hit.kind != "endpoint", hit.path))
    return SourceDigest(
        repo=spec.key,
        terms=terms,
        hits=tuple(hits[:limit]),
        candidates=len(files),
    )
