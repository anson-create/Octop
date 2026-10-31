import { describe, expect, it } from "vitest";
import type { PmDecision, PmStep } from "../../api/types";
import {
  NOTE_MIN_LEN,
  countdown,
  currentStep,
  gatePassed,
  isHtmlArtifact,
  noteTooShort,
  remainingMinutes,
  sortByUrgency,
  sourceEntries,
  stepProgress,
  summarizeLocator,
  tryFormatJson,
} from "./pmMeta";

function step(seq: number, status: string): PmStep {
  return {
    id: seq,
    seq,
    step_key: `s${seq}`,
    stage: "P1",
    status,
    attempt: 1,
    gate: null,
    error_code: null,
  };
}

function card(id: number, timeoutAt: string | null): PmDecision {
  return {
    id,
    task_id: 1,
    step_id: 2,
    level: "L2",
    question: `q${id}`,
    options: [],
    recommended_key: null,
    status: "pending",
    selected_key: null,
    note: null,
    decided_by: null,
    decided_at: null,
    timeout_at: timeoutAt,
    auto_decided: false,
    appeal_state: null,
    assignee_user_id: null,
    created_at: null,
  };
}

const NOW = Date.parse("2026-10-31T00:00:00Z");

describe("pmMeta", () => {
  it("treats a gate with no issues as passed", () => {
    expect(gatePassed({ ok: true })).toBe(true);
    expect(gatePassed({ issues: [] })).toBe(true);
    expect(gatePassed({ issues: [{ rule_id: "G-P1-01" }] })).toBe(false);
    expect(gatePassed(null)).toBe(false);
  });

  it("counts skipped steps as done and finds the live step", () => {
    const steps = [
      step(1, "passed"),
      step(2, "skipped"),
      step(3, "waiting_card"),
      step(4, "pending"),
    ];
    expect(stepProgress(steps)).toBeCloseTo(0.5);
    expect(currentStep(steps)?.seq).toBe(3);
    expect(currentStep([step(1, "passed")])).toBeNull();
  });

  it("buckets deadlines into overdue / soon / later", () => {
    expect(countdown(null, NOW).state).toBe("none");
    expect(remainingMinutes("2026-10-31T01:00:00Z", NOW)).toBe(60);
    expect(countdown("2026-10-30T23:00:00Z", NOW)).toEqual({
      state: "overdue",
      minutes: 60,
    });
    expect(countdown("2026-10-31T00:30:00Z", NOW).state).toBe("soon");
    expect(countdown("2026-10-31T06:00:00Z", NOW).state).toBe("later");
  });

  it("orders cards by urgency with overdue first", () => {
    const cards = [
      card(1, "2026-11-02T00:00:00Z"),
      card(2, "2026-10-30T00:00:00Z"), // overdue
      card(3, "2026-10-31T02:00:00Z"),
    ];
    expect(sortByUrgency(cards, NOW).map((item) => item.id)).toEqual([2, 3, 1]);
  });

  it("enforces the same note floor as the backend", () => {
    expect(NOTE_MIN_LEN).toBe(10);
    expect(noteTooShort("九个字符的理由不够")).toBe(true);
    expect(noteTooShort("刚好十个字符的理由啊")).toBe(false);
  });

  it("turns every locator kind into an address", () => {
    expect(
      summarizeLocator({ jira_field: true, issue: "CSM-1", field: "优先级" }),
    ).toMatchObject({ kind: "jira_field", text: "CSM-1 · 优先级", external: false });

    expect(
      summarizeLocator({ code_span: true, repo: "octop", path: "a.py", line_start: 12 }),
    ).toMatchObject({ kind: "code_span", text: "a.py:12", href: null });

    expect(summarizeLocator({ dict_field: true, table: "orders" })).toMatchObject({
      kind: "dict_field",
      text: "orders",
    });

    expect(
      summarizeLocator({ kb_section: true, kb_id: "kb1", doc_id: "d2" }),
    ).toMatchObject({ kind: "kb_section", text: "kb1 / d2" });

    expect(
      summarizeLocator({ url: "https://example.com/x" }),
    ).toMatchObject({ kind: "url", href: "https://example.com/x", external: true });

    expect(
      summarizeLocator({ pm_record: true, entity: "requirement", ref: 7 }),
    ).toMatchObject({ kind: "pm_record", href: "/pm/requirements/7", external: false });

    expect(summarizeLocator(null)).toMatchObject({ kind: "unknown", href: null });
  });

  it("pretty-prints JSON bodies and leaves markdown alone", () => {
    expect(tryFormatJson('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(tryFormatJson("# 标题")).toBeNull();
    expect(tryFormatJson("{broken")).toBeNull();
  });

  it("routes prototype artifacts to the html renderer", () => {
    expect(isHtmlArtifact("prototype_html", "<div/>")).toBe(true);
    // Kind is authoritative even before the body is loaded.
    expect(isHtmlArtifact("prototype_html", "")).toBe(true);
    expect(isHtmlArtifact("data", "<!DOCTYPE html><html></html>")).toBe(true);
    expect(isHtmlArtifact("data", '{"a":1}')).toBe(false);
    expect(isHtmlArtifact("document", "# PRD")).toBe(false);
    expect(isHtmlArtifact(null, null)).toBe(false);
  });

  it("drops empty sources from a breakdown", () => {
    expect(sourceEntries({ code: 2, jira: 0, kb: 1 })).toEqual([
      { source: "code", count: 2 },
      { source: "kb", count: 1 },
    ]);
    expect(sourceEntries(null)).toEqual([]);
  });
});
