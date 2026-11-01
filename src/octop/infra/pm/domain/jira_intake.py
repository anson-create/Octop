"""What a Jira ticket says, assembled into the text a requirement is made of.

Intake used to keep only ``description``. Everything else the ticket states — the
custom field the team happens to call 痛点, the environment, the components, the
attachments and the comments where a requirement usually gets refined — was read
for page location and then thrown away. The step that writes the requirement
analysis never saw any of it, so "从工单派生需求" was true of a subject line and
one paragraph.

This module is the single place that decides what that text contains. It is
pure: no HTTP, no database, no model. Two things follow from that. The rule is
testable without a Jira instance, and the preview shown in the intake dialog is
produced by the same function that fills the database — a preview that shows
something other than what gets stored is worse than no preview at all.

Two constraints shape the output, both learned the hard way:

* The issue key itself must not appear. Every export runs a blacklist that
  blocks ticket ids, and intake text reaches deliverables verbatim, so a key in
  here makes that requirement permanently un-exportable. The key lives in
  ``source_ref`` and ``baseline``, which are metadata.
* Comments *are* included, by request. They routinely mention other tickets,
  which the same blacklist catches, so the assembled text is scanned here and
  the blockers are reported at intake time rather than discovered at export
  time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# A ticket with a hundred comments would otherwise blow the model's context and
# silently push the description out of the prompt. The budget is on the whole
# assembled text, and every cut is reported.
MAX_TEXT_CHARS = 30000

# Section order is deliberate: what the ticket asks for first, the supplementary
# fields next, and the discussion last. A model reading top-down then sees the
# requirement before the conversation about it.
HEAD_DESCRIPTION = "需求描述"
HEAD_EXTRA = "背景与补充"
HEAD_ENVIRONMENT = "运行环境"
HEAD_SCOPE = "关联模块"
HEAD_COMMENTS = "讨论记录"
HEAD_ATTACHMENTS = "附件"

_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class RequirementText:
    """The text plus an honest account of how it was produced."""

    text: str
    sections: tuple[str, ...] = ()
    truncated: tuple[str, ...] = field(default_factory=tuple)
    # Content the ticket states that a deliverable may not carry verbatim.
    blockers: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "sections": list(self.sections),
            "truncated": list(self.truncated),
            "blockers": list(self.blockers),
            "chars": len(self.text),
        }


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _day(value: Any) -> str:
    text = _clean(value)
    match = _DATE_PREFIX.match(text)
    return match.group(1) if match else text[:10]


def _issue_key_token(key: str) -> str:
    return _clean(key).upper()


def _section(heading: str, body: str) -> str:
    body = body.strip()
    if not body:
        return ""
    return f"## {heading}\n{body}"


def _custom_block(custom: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in custom:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name")) or _clean(item.get("id"))
        text = _clean(item.get("text"))
        if not name or not text:
            continue
        parts.append(f"### {name}\n{text}")
    return "\n\n".join(parts)


def _annotation_block(issue: dict[str, Any]) -> str:
    """Bookkeeping that is still worth reading: status, type, priority, people.

    Kept out of the prose sections on purpose — a model that sees "状态：In
    Progress" inside 需求描述 starts writing about workflow state as if it were a
    requirement.
    """

    pairs = (
        ("工单类型", issue.get("issue_type")),
        ("状态", issue.get("status")),
        ("优先级", issue.get("priority")),
        ("经办人", issue.get("assignee")),
        ("报告人", issue.get("reporter")),
    )
    lines = [f"- {label}：{_clean(value)}" for label, value in pairs if _clean(value)]
    return "\n".join(lines)


def _scope_block(issue: dict[str, Any]) -> str:
    lines: list[str] = []
    components = [c for c in (_clean(item) for item in issue.get("components") or []) if c]
    if components:
        lines.append(f"- 组件：{'、'.join(components)}")
    labels = [c for c in (_clean(item) for item in issue.get("labels") or []) if c]
    if labels:
        lines.append(f"- 标签：{'、'.join(labels)}")
    return "\n".join(lines)


def _comment_block(comments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, item in enumerate(comments, start=1):
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("text"))
        if not text:
            continue
        author = _clean(item.get("author")) or "匿名"
        day = _day(item.get("created"))
        stamp = f"（{day}）" if day else ""
        lines.append(f"{index}. {author}{stamp}：{text}")
    return "\n".join(lines)


def _attachment_block(attachments: list[dict[str, Any]]) -> str:
    """Filenames only. Nothing here was opened, and the text must not imply it was.

    The name still matters: people name screenshots after the screen
    (``报关单列表-导入入口.png``), so a reader learns where this lives.
    """

    lines: list[str] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("filename"))
        if not name:
            continue
        lines.append(f"- {name}（仅文件名，内容未读取）")
    return "\n".join(lines)


def _export_blockers(text: str) -> tuple[str, ...]:
    """Tokens a deliverable may not contain, reported before the ticket is stored."""

    from octop.infra.pm.domain.export import scan  # noqa: PLC0415

    seen: dict[str, set[str]] = {}
    for hit in scan(text):
        seen.setdefault(hit.label, set()).add(hit.match)
    notes: list[str] = []
    for label, matches in seen.items():
        sample = "、".join(sorted(matches)[:3])
        more = "等" if len(matches) > 3 else ""
        notes.append(f"{label} {len(matches)} 处（{sample}{more}）")
    return tuple(notes)


def compose(issue: dict[str, Any] | None, *, max_chars: int = MAX_TEXT_CHARS) -> RequirementText:
    """Assemble the requirement text from one normalised issue.

    ``issue`` is what ``octop.infra.pm.ports.jira.issue_to_summary`` produces, so
    this works the same for the fake client in tests and a real DC instance.
    """

    if not isinstance(issue, dict):
        return RequirementText(text="")

    key = _issue_key_token(str(issue.get("key") or ""))
    description = _clean(issue.get("description"))

    planned: list[tuple[str, str]] = []
    if description:
        planned.append((HEAD_DESCRIPTION, description))
    else:
        # An explicit statement of absence. Leaving the section out lets every
        # later step describe a ticket that said nothing as if it had.
        planned.append((HEAD_DESCRIPTION, "（工单未填写描述，以下内容均来自其他字段）"))

    extra = _custom_block([item for item in issue.get("custom_fields") or [] if isinstance(item, dict)])
    annotation = _annotation_block(issue)
    if extra and annotation:
        planned.append((HEAD_EXTRA, f"{annotation}\n\n{extra}"))
    elif extra:
        planned.append((HEAD_EXTRA, extra))
    elif annotation:
        planned.append((HEAD_EXTRA, annotation))

    environment = _clean(issue.get("environment"))
    if environment:
        planned.append((HEAD_ENVIRONMENT, environment))

    scope = _scope_block(issue)
    if scope:
        planned.append((HEAD_SCOPE, scope))

    comments = [item for item in issue.get("comments") or [] if isinstance(item, dict)]
    comment_text = _comment_block(comments)
    if comment_text:
        planned.append((HEAD_COMMENTS, comment_text))

    attachments = [item for item in issue.get("attachments") or [] if isinstance(item, dict)]
    attachment_text = _attachment_block(attachments)
    if attachment_text:
        planned.append((HEAD_ATTACHMENTS, attachment_text))

    # Budget is spent in order, so a ticket with 200 comments loses its tail, not
    # its description. Each cut is named; a silent one produces a requirement
    # confidently missing the half that mattered.
    # The title is not repeated here: it is its own column and reaches the model
    # as ``ctx.requirement_title``. Duplicating it would spend budget on text the
    # prompt already has.
    blocks: list[str] = []
    kept: list[str] = []
    truncated: list[str] = []

    remaining = max_chars
    for heading, body in planned:
        rendered = _section(heading, body)
        if not rendered:
            continue
        if len(rendered) > remaining:
            if remaining < 200:
                truncated.append(f"「{heading}」未纳入：预算已用尽")
                continue
            cut = rendered[:remaining]
            # Cut on a line boundary when possible; a half sentence reads as a
            # typo rather than a truncation.
            if "\n" in cut:
                cut = cut[: cut.rfind("\n")]
            rendered = cut + f"\n（「{heading}」因长度限制截断，原文 {len(_section(heading, body))} 字）"
            truncated.append(f"「{heading}」从 {len(_section(heading, body))} 字截到 {len(cut)} 字")
        blocks.append(rendered)
        kept.append(heading)
        remaining -= len(rendered)
        if remaining <= 0:
            break

    text = "\n\n".join(blocks).strip()

    # Belt and braces: the key must never reach the body, even when a custom
    # field or the title happens to contain it.
    if key:
        text = text.replace(key, "").replace(key.lower(), "")
        text = re.sub(r"[ \t]{2,}", " ", text)

    return RequirementText(
        text=text,
        sections=tuple(kept),
        truncated=tuple(truncated),
        blockers=_export_blockers(text),
    )
