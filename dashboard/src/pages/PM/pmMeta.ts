/** Pure PM helpers — no React, no fetch. Unit-tested in ``pmMeta.test.ts``. */

import type {
  PmDecision,
  PmGateResult,
  PmLevel,
  PmStep,
} from "../../api/types";

/** Backend ``NOTE_MIN_LEN`` — the answer form enforces the same floor. */
export const NOTE_MIN_LEN = 10;

export const STAGE_ORDER = ["P1", "P2", "P3", "P4", "P5"] as const;

/** Step keys in pipeline order; labels live under ``pm.steps.*`` in the locale bundle. */
export const STEP_ORDER = [
  "intake",
  "link_expand",
  "requirement_analysis",
  "code",
  "datadict",
  "dedup",
  "capability_fit",
  "decision",
  "prototype_gate",
  "prototype",
  "prd",
  "review",
  "deliver",
] as const;

export const STEP_INDEX: Record<string, number> = Object.fromEntries(
  STEP_ORDER.map((key, index) => [key, index]),
);

export const EVIDENCE_SOURCES = [
  "jira",
  "code",
  "datadict",
  "kb",
  "human",
  "web",
] as const;

/** Tag colour per backend enum value; unknown values fall back to ``default``. */
const TONES: Record<string, string> = {
  // task
  queued: "default",
  running: "processing",
  waiting_human: "warning",
  blocked: "error",
  paused: "default",
  delivered: "success",
  archived: "default",
  // step
  pending: "default",
  passed: "success",
  failed: "error",
  waiting_card: "warning",
  skipped: "default",
  // decision
  answered: "success",
  auto_answered: "warning",
  expired: "error",
  void: "default",
  // conclusion
  evidenced: "success",
  uncovered: "warning",
  undetermined: "default",
  // requirement
  open: "default",
  in_progress: "processing",
  closed: "default",
  // review verdict
  pass: "success",
  reject: "error",
};

export function statusTone(status: string | null | undefined): string {
  if (!status) return "default";
  return TONES[status] ?? "default";
}

export function stepLabelKey(stepKey: string): string {
  return STEP_INDEX[stepKey] === undefined
    ? stepKey
    : `pm.steps.${stepKey}`;
}

export function stageLabelKey(stageKey: string): string {
  return `pm.stages.${stageKey}`;
}

export function levelLabelKey(level: string): string {
  return `pm.levels.${level}`;
}

/** A gate passes when it says so, or when it reports no failures. */
export function gatePassed(gate: PmGateResult | null | undefined): boolean {
  if (!gate) return false;
  if (typeof gate.ok === "boolean") return gate.ok;
  if (typeof gate.passed === "boolean") return gate.passed;
  const issues = gate.issues ?? gate.failures ?? [];
  return issues.length === 0;
}

export function gateIssues(gate: PmGateResult | null | undefined) {
  if (!gate) return [];
  return gate.issues ?? gate.failures ?? [];
}

/** Progress of the 13 steps, 0–1. ``skipped`` counts as done. */
export function stepProgress(steps: PmStep[]): number {
  if (steps.length === 0) return 0;
  const done = steps.filter(
    (step) => step.status === "passed" || step.status === "skipped",
  ).length;
  return done / steps.length;
}

export function currentStep(steps: PmStep[]): PmStep | null {
  const bySeq = [...steps].sort((a, b) => a.seq - b.seq);
  return (
    bySeq.find((step) => step.status === "running") ??
    bySeq.find((step) => step.status === "waiting_card") ??
    bySeq.find((step) => step.status === "failed") ??
    bySeq.find((step) => step.status === "pending") ??
    null
  );
}

const MINUTE = 60 * 1000;

/** Minutes left before a card times out; negative when overdue; null when no deadline. */
export function remainingMinutes(
  iso: string | null | undefined,
  now: number = Date.now(),
): number | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  return Math.round((at - now) / MINUTE);
}

/**
 * L2/L3 cards are the human's queue, so the UI sorts them by urgency:
 * overdue first, then nearest deadline, then newest.
 */
export function sortByUrgency(
  cards: PmDecision[],
  now: number = Date.now(),
): PmDecision[] {
  const weight = (card: PmDecision): number => {
    const left = remainingMinutes(card.timeout_at, now);
    if (left === null) return Number.MAX_SAFE_INTEGER / 2;
    return left;
  };
  return [...cards].sort((a, b) => {
    const diff = weight(a) - weight(b);
    if (diff !== 0) return diff;
    return (b.created_at ?? "").localeCompare(a.created_at ?? "");
  });
}

export function isOverdue(
  iso: string | null | undefined,
  now: number = Date.now(),
): boolean {
  const left = remainingMinutes(iso, now);
  return left !== null && left < 0;
}

export function noteTooShort(note: string): boolean {
  return note.trim().length < NOTE_MIN_LEN;
}

export type Countdown =
  | { state: "none" }
  | { state: "overdue" | "soon" | "later"; minutes: number };

/** Deadline bucket for a card: overdue, under an hour, or later. */
export function countdown(
  iso: string | null | undefined,
  now: number = Date.now(),
): Countdown {
  const minutes = remainingMinutes(iso, now);
  if (minutes === null) return { state: "none" };
  if (minutes < 0) return { state: "overdue", minutes: -minutes };
  if (minutes < 60) return { state: "soon", minutes };
  return { state: "later", minutes };
}

/** Level ordering for grouping: L3 (irreversible) first. */
export function levelRank(level: PmLevel | string): number {
  if (level === "L3") return 0;
  if (level === "L2") return 1;
  return 2;
}

const LOCATOR_KINDS = [
  "jira_field",
  "code_span",
  "dict_field",
  "kb_section",
  "url",
  "pm_record",
] as const;

export type LocatorKind = (typeof LOCATOR_KINDS)[number];

export interface LocatorSummary {
  kind: LocatorKind | "unknown";
  /** Short human-readable address, e.g. ``CSM-1024 · 优先级``. */
  text: string;
  /** In-app route when the locator points inside PM, or an external URL. */
  href: string | null;
  external: boolean;
}

function pick(locator: Record<string, unknown>, key: string): string {
  const value = locator[key];
  if (value === undefined || value === null) return "";
  return String(value);
}

/**
 * Turn a locator into something clickable. A quote without an address is not
 * evidence, so an unknown/blank locator renders as "无定位" rather than a link
 * that goes nowhere.
 */
export function summarizeLocator(
  locator: Record<string, unknown> | null | undefined,
): LocatorSummary {
  if (!locator) return { kind: "unknown", text: "", href: null, external: false };
  for (const kind of LOCATOR_KINDS) {
    if (!(kind in locator)) continue;
    if (kind === "jira_field") {
      const issue = pick(locator, "issue");
      const field = pick(locator, "field");
      return {
        kind,
        text: [issue, field].filter(Boolean).join(" · "),
        href: pick(locator, "url") || null,
        external: Boolean(pick(locator, "url")),
      };
    }
    if (kind === "code_span") {
      const path = pick(locator, "path");
      const line = pick(locator, "line_start") || pick(locator, "line");
      return {
        kind,
        text: [path, line].filter(Boolean).join(":"),
        href: null,
        external: false,
      };
    }
    if (kind === "dict_field") {
      return {
        kind,
        text: [pick(locator, "table"), pick(locator, "column")]
          .filter(Boolean)
          .join(" · "),
        href: null,
        external: false,
      };
    }
    if (kind === "kb_section") {
      return {
        kind,
        text: [pick(locator, "kb_id"), pick(locator, "doc_id"), pick(locator, "anchor")]
          .filter(Boolean)
          .join(" / "),
        href: null,
        external: false,
      };
    }
    if (kind === "url") {
      const url = pick(locator, "url");
      return { kind, text: url, href: url || null, external: Boolean(url) };
    }
    // pm_record → an in-app route
    const entity = pick(locator, "entity");
    const ref = pick(locator, "ref");
    const href =
      entity === "requirement" && ref ? `/pm/requirements/${ref}` : null;
    return {
      kind: "pm_record",
      text: [entity, ref].filter(Boolean).join(" #"),
      href,
      external: false,
    };
  }
  return { kind: "unknown", text: "", href: null, external: false };
}

/** Distinct source types present in a ``source_breakdown`` payload. */
export function sourceEntries(
  breakdown: Record<string, number> | null | undefined,
): Array<{ source: string; count: number }> {
  if (!breakdown) return [];
  return Object.entries(breakdown)
    .filter(([, count]) => count > 0)
    .map(([source, count]) => ({ source, count }))
    .sort((a, b) => b.count - a.count);
}

/**
 * ``prototype_html`` artifacts are a page, not a document. Rendering them
 * through the markdown renderer shows a wall of tags, so they get an iframe.
 * The check also catches HTML that was stored under a generic kind.
 */
export function isHtmlArtifact(
  kind: string | null | undefined,
  body: string | null | undefined,
): boolean {
  if (kind === "prototype_html" || kind === "html") return true;
  const head = (body ?? "").trimStart().slice(0, 200).toLowerCase();
  return head.startsWith("<!doctype html") || head.startsWith("<html");
}

/**
 * Data artifacts store JSON, documents store markdown. Pretty-printing the JSON
 * keeps a raw one-liner from looking like a broken document.
 */
export function tryFormatJson(text: string | null | undefined): string | null {
  const trimmed = (text ?? "").trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return null;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return null;
  }
}
