import { useState } from "react";
import { Alert, Button, Drawer, Input, Spin, Tag } from "antd";
import { RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmStepTrace } from "../../../api/types";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";
import { NOTE_MIN_LEN, gateIssues, gatePassed, noteTooShort } from "../pmMeta";
import StatusTag from "./StatusTag";
import { usePMResource } from "../usePMResource";
import styles from "../pm.module.less";

interface TraceDrawerProps {
  stepId: number | null;
  stepLabel: string;
  onClose: () => void;
  onOpenEvidence: (evidenceId: number) => void;
  onChanged: () => void;
}

/**
 * Gate verdict + retrieval trace for one step. This is where "why did P2 stop"
 * is answered: failing rule ids, gaps, and the evidence actually cited.
 */
export default function TraceDrawer({
  stepId,
  stepLabel,
  onClose,
  onOpenEvidence,
  onChanged,
}: TraceDrawerProps) {
  const { t } = useTranslation();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const { data, loading, error } = usePMResource<PmStepTrace | null>(
    null,
    () => pmApi.stepTrace(Number(stepId)),
    [stepId],
    { enabled: stepId !== null },
  );

  const rejected = () => {
    setNote("");
    onChanged();
    onClose();
  };

  const onReject = async () => {
    if (!stepId) return;
    setBusy(true);
    try {
      await pmApi.rejectStep(stepId, note.trim());
      message.success(t("pm.trace.rejected"));
      rejected();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setBusy(false);
    }
  };

  const gate = data?.gate ?? null;
  const issues = gateIssues(gate);
  const passed = gate ? gatePassed(gate) : null;
  const trace = data?.trace ?? {};
  const gaps: string[] = Array.isArray(trace.gaps) ? trace.gaps : [];

  return (
    <Drawer
      open={stepId !== null}
      onClose={onClose}
      width={560}
      title={`${t("pm.trace.title")} · ${stepLabel}`}
      destroyOnClose
    >
      {loading ? (
        <div style={{ textAlign: "center", padding: 24 }}>
          <Spin size="small" />
        </div>
      ) : null}
      {error ? <Alert type="error" showIcon message={error} /> : null}

      {data ? (
        <div className={styles.evidenceBlock}>
          <div className={styles.kv}>
            <span className={styles.kvKey}>{t("pm.fields.status")}</span>
            <span className={styles.kvValue}>
              <StatusTag status={data.status} />
            </span>
            <span className={styles.kvKey}>{t("pm.trace.gate")}</span>
            <span className={styles.kvValue}>
              {passed === null ? (
                "—"
              ) : passed ? (
                <Tag color="green">{t("pm.trace.gateOk")}</Tag>
              ) : (
                <Tag color="red">{t("pm.trace.gateFail")}</Tag>
              )}
              {gate?.rules?.length ? (
                <span style={{ marginLeft: 6, fontSize: 11 }}>
                  {gate.rules.join(", ")}
                </span>
              ) : null}
            </span>
          </div>

          {issues.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message={t("pm.trace.issues", { count: issues.length })}
              description={
                <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                  {issues.map((issue, index) => (
                    <li key={`${issue.rule_id ?? "issue"}-${index}`}>
                      {issue.rule_id ? `${issue.rule_id}: ` : ""}
                      {issue.message ?? ""}
                    </li>
                  ))}
                </ul>
              }
            />
          ) : null}

          {gaps.length > 0 ? (
            <>
              <div className={styles.sectionTitle}>{t("pm.trace.gaps")}</div>
              <ul style={{ margin: 0, paddingInlineStart: 18, fontSize: 12.5 }}>
                {gaps.map((gap, index) => (
                  <li key={index}>{String(gap)}</li>
                ))}
              </ul>
            </>
          ) : null}

          <div className={styles.sectionTitle}>
            <span>{t("pm.evidence.title")}</span>
            <span>{data.evidence.length}</span>
          </div>
          {data.evidence.length === 0 ? (
            <div className={styles.emptyHint}>{t("pm.evidence.empty")}</div>
          ) : (
            <div>
              {data.evidence.map((evidence) => (
                <button
                  key={evidence.id}
                  type="button"
                  className={styles.evidenceChip}
                  onClick={() => onOpenEvidence(evidence.id)}
                >
                  #{evidence.id} {evidence.source_type}
                </button>
              ))}
            </div>
          )}

          <div className={styles.sectionTitle}>{t("pm.trace.raw")}</div>
          <pre className={styles.traceJson}>
            {JSON.stringify(data.trace ?? {}, null, 2)}
          </pre>

          <div className={styles.sectionTitle}>{t("pm.trace.reject")}</div>
          <Input.TextArea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder={t("pm.trace.rejectPrompt", { min: NOTE_MIN_LEN })}
            autoSize={{ minRows: 2, maxRows: 4 }}
          />
          <Button
            danger
            size="small"
            style={{ marginTop: 8 }}
            icon={<RotateCcw size={13} />}
            loading={busy}
            disabled={noteTooShort(note)}
            onClick={() => void onReject()}
          >
            {t("pm.trace.reject")}
          </Button>
        </div>
      ) : null}
    </Drawer>
  );
}
