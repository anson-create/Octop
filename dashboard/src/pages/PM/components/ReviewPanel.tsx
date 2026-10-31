import { useState } from "react";
import { Button, Input, Segmented, Select } from "antd";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmReviewList } from "../../../api/types";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";
import { NOTE_MIN_LEN, noteTooShort } from "../pmMeta";
import StatusTag from "./StatusTag";
import styles from "../pm.module.less";

const PERSPECTIVES = ["business", "tech", "customer"] as const;
type Perspective = (typeof PERSPECTIVES)[number];

interface ReviewPanelProps {
  taskId: number;
  reviews: PmReviewList | null;
  onChanged: () => void;
}

/**
 * P5 needs three verdicts. The panel shows which are missing instead of a bare
 * "submit" form, because "one of three" is the state that silently blocks a
 * delivery.
 */
export default function ReviewPanel({ taskId, reviews, onChanged }: ReviewPanelProps) {
  const { t } = useTranslation();
  const [perspective, setPerspective] = useState<Perspective>("business");
  const [verdict, setVerdict] = useState<"pass" | "reject">("pass");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  const existing = new Map(
    (reviews?.items ?? []).map((item) => [item.perspective, item]),
  );
  const missing = PERSPECTIVES.filter((key) => !existing.has(key));

  const submit = async () => {
    setBusy(true);
    try {
      await pmApi.submitReview({
        task_id: taskId,
        perspective,
        verdict,
        comment: comment.trim() || undefined,
      });
      message.success(t("pm.review.submitted"));
      setComment("");
      onChanged();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className={styles.sectionTitle}>
        <span>{t("pm.review.title")}</span>
        <span>
          {reviews?.complete
            ? t("pm.review.complete")
            : t("pm.review.incomplete", { count: missing.length })}
        </span>
      </div>

      {PERSPECTIVES.map((key) => {
        const item = existing.get(key);
        return (
          <div
            key={key}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              fontSize: 12.5,
              padding: "4px 0",
            }}
          >
            <span style={{ width: 56, color: "var(--fn-text-secondary)" }}>
              {t(`pm.review.${key}`)}
            </span>
            {item ? (
              <>
                <StatusTag status={item.verdict} />
                <span style={{ color: "var(--fn-text-tertiary)", fontSize: 11.5 }}>
                  {item.comment || ""}
                </span>
              </>
            ) : (
              <span style={{ color: "var(--fn-text-tertiary)" }}>
                {t("pm.review.missing")}
              </span>
            )}
          </div>
        );
      })}

      <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
        <Select
          size="small"
          value={perspective}
          onChange={(value) => setPerspective(value as Perspective)}
          options={PERSPECTIVES.map((key) => ({
            value: key,
            label: t(`pm.review.${key}`),
          }))}
          style={{ width: 96 }}
        />
        <Segmented
          size="small"
          value={verdict}
          onChange={(value) => setVerdict(value as "pass" | "reject")}
          options={[
            { value: "pass", label: t("pm.review.pass") },
            { value: "reject", label: t("pm.review.reject") },
          ]}
        />
        <Button
          size="small"
          type="primary"
          loading={busy}
          disabled={verdict === "reject" && noteTooShort(comment)}
          onClick={() => void submit()}
        >
          {t("pm.actions.submit")}
        </Button>
      </div>
      <Input.TextArea
        value={comment}
        onChange={(event) => setComment(event.target.value)}
        placeholder={t("pm.review.commentPlaceholder", { min: NOTE_MIN_LEN })}
        autoSize={{ minRows: 2, maxRows: 4 }}
        style={{ marginTop: 8 }}
      />
    </div>
  );
}
