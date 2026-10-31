import { useMemo, useState } from "react";
import { Button, InputNumber, Tag, Tooltip } from "antd";
import { ArrowRightLeft, Undo2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmDecision } from "../../../api/types";
import type { AskQuestion } from "../../../api/types/hitl";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";
import AskQuestionCard from "../../Chat/components/AskQuestionCard";
import {
  NOTE_MIN_LEN,
  countdown,
  levelLabelKey,
  noteTooShort,
} from "../pmMeta";
import StatusTag from "./StatusTag";
import styles from "../pm.module.less";

/**
 * One judgement card.
 *
 * The question/options/reason UI is the host's own ``AskQuestionCard`` — the
 * same component a shot at ``/chat`` uses for ``ask_user_question`` — so people
 * meet one card design, not two. PM only adds the frame it needs: the L-level,
 * the deadline, transfer and appeal. The card speaks in plain answers, so the
 * option and the reason travel as its two questions.
 */

interface DecisionCardProps {
  decision: PmDecision;
  /** Extra context line (task public id, step name) rendered above the question. */
  context?: string;
  onChanged?: () => void;
  /** Hide the transfer form in read-only embeds. */
  allowTransfer?: boolean;
  now?: number;
}

function deadlineText(t: TFunction, decision: PmDecision, now: number): string {
  const state = countdown(decision.timeout_at, now);
  if (state.state === "none") return t("pm.decision.noDeadline");
  const human =
    state.minutes < 60
      ? t("pm.time.minutes", { count: state.minutes })
      : t("pm.time.hours", { count: Math.round(state.minutes / 60) });
  return state.state === "overdue"
    ? t("pm.decision.overdue", { when: human })
    : t("pm.decision.deadline", { when: human });
}

/** ``AskQuestionCard`` joins answers as ``header: value`` lines. Split them back. */
export function parseCardAnswer(
  message: string,
  headers: string[],
): string[] {
  return message.split("\n").map((line, index) => {
    const prefix = `${headers[index] ?? ""}: `;
    if (prefix !== ": " && line.startsWith(prefix)) {
      return line.slice(prefix.length).trim();
    }
    const cut = line.indexOf(": ");
    return (cut >= 0 ? line.slice(cut + 2) : line).trim();
  });
}

export default function DecisionCard({
  decision,
  context,
  onChanged,
  allowTransfer = true,
  now = Date.now(),
}: DecisionCardProps) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState<null | "answer" | "appeal" | "transfer">(null);
  const [transferOpen, setTransferOpen] = useState(false);
  const [assignee, setAssignee] = useState<number | null>(null);

  const pending = decision.status === "pending";
  const state = countdown(decision.timeout_at, now);
  const options = useMemo(() => decision.options ?? [], [decision.options]);

  const choiceHeader = [decision.level, context].filter(Boolean).join(" · ");
  const reasonHeader = t("pm.decision.reasonHeader");

  const questions = useMemo<AskQuestion[]>(
    () => [
      {
        question: decision.question,
        header: choiceHeader,
        options: options.map((option) => {
          const detail = [
            option.pros ? `+ ${option.pros}` : "",
            option.cons ? `− ${option.cons}` : "",
            option.effort ? t("pm.decision.effort", { value: option.effort }) : "",
          ]
            .filter(Boolean)
            .join(" · ");
          return {
            label: option.label || option.key,
            description: detail || undefined,
          };
        }),
      },
      // No options → the host card renders a free-text answer, which is exactly
      // what a rationale field is.
      { question: t("pm.decision.reasonQuestion"), header: reasonHeader },
    ],
    [choiceHeader, decision.question, options, reasonHeader, t],
  );

  const run = async (
    kind: "answer" | "appeal" | "transfer",
    fn: () => Promise<unknown>,
    success: string,
  ) => {
    setBusy(kind);
    try {
      await fn();
      message.success(success);
      onChanged?.();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setBusy(null);
    }
  };

  const handleSubmit = (body: string) => {
    const [choice, reason] = parseCardAnswer(body, [choiceHeader, reasonHeader]);
    const picked =
      options.find((option) => (option.label || option.key) === choice) ??
      options.find((option) => option.key === choice);
    if (!picked) {
      message.error(t("pm.decision.pickRequired"));
      return;
    }
    if (noteTooShort(reason ?? "")) {
      message.warning(t("pm.decision.noteTooShort", { min: NOTE_MIN_LEN }));
      return;
    }
    void run(
      "answer",
      () => pmApi.answerDecision(decision.id, picked.key, (reason ?? "").trim()),
      t("pm.decision.answered"),
    );
  };

  return (
    <div className={styles.decisionCard} data-level={decision.level}>
      <div className={styles.decisionHead}>
        <Tag
          color={
            decision.level === "L3"
              ? "red"
              : decision.level === "L2"
                ? "orange"
                : "default"
          }
        >
          {decision.level} · {t(levelLabelKey(decision.level))}
        </Tag>
        <StatusTag status={decision.status} />
        {decision.auto_decided ? (
          <Tag color="orange">{t("pm.decision.auto")}</Tag>
        ) : null}
        <span className={state.state === "overdue" ? styles.dangerText : undefined}>
          {deadlineText(t, decision, now)}
        </span>
        {context ? <span>· {context}</span> : null}
      </div>

      <AskQuestionCard
        questions={questions}
        status={pending ? "pending" : decision.status === "expired" ? "rejected" : "approved"}
        onSubmit={pending ? handleSubmit : undefined}
      />

      {pending ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginTop: 8,
            flexWrap: "wrap",
          }}
        >
          <span style={{ flex: 1 }} />
          {allowTransfer ? (
            <Tooltip title={t("pm.decision.transfer")}>
              <Button
                size="small"
                type="text"
                icon={<ArrowRightLeft size={13} />}
                onClick={() => setTransferOpen((value) => !value)}
              >
                {t("pm.decision.transfer")}
              </Button>
            </Tooltip>
          ) : null}
        </div>
      ) : (
        <div className={styles.optionMeta}>
          {decision.selected_key ? (
            <div>
              {t("pm.decision.selected")}：
              {options.find((option) => option.key === decision.selected_key)?.label ||
                decision.selected_key}
            </div>
          ) : null}
          {decision.note ? (
            <div>
              {t("pm.decision.noteLabel")}：{decision.note}
            </div>
          ) : null}
          {decision.decided_at ? (
            <div>
              {t("pm.decision.decidedAt", {
                at: new Date(decision.decided_at).toLocaleString(),
              })}
            </div>
          ) : null}
          {decision.appeal_state ? (
            <div>
              {t("pm.decision.appealState", { state: decision.appeal_state })}
            </div>
          ) : null}
          {decision.auto_decided && decision.appeal_state !== "upheld" ? (
            <Button
              size="small"
              type="link"
              icon={<Undo2 size={13} />}
              loading={busy === "appeal"}
              onClick={() =>
                void run(
                  "appeal",
                  () => pmApi.appealDecision(decision.id),
                  t("pm.decision.appealed"),
                )
              }
            >
              {t("pm.decision.appeal")}
            </Button>
          ) : null}
        </div>
      )}

      {transferOpen && pending ? (
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <InputNumber
            min={1}
            value={assignee}
            onChange={(value) => setAssignee(value ?? null)}
            placeholder={t("pm.fields.assignee")}
            style={{ flex: 1 }}
          />
          <Button
            size="small"
            disabled={!assignee}
            loading={busy === "transfer"}
            onClick={() =>
              void run(
                "transfer",
                () => pmApi.transferDecision(decision.id, Number(assignee)),
                t("pm.decision.transferred"),
              )
            }
          >
            {t("pm.actions.confirm")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
