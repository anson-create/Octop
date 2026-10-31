import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Drawer, Segmented, Spin, Tag } from "antd";
import { Copy, Download, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmExportPlan } from "../../../api/types";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";
import { copyText } from "../../../utils/copyText";
import { saveBlobAsFile } from "../../../utils/saveBlobAsFile";
import StatusTag from "./StatusTag";
import styles from "../pm.module.less";

interface ExportDrawerProps {
  artifactId: number | null;
  onClose: () => void;
}

/**
 * The four export gates, made visible: trim, blacklist scan, explicit second
 * confirm, audit trail. Preview always renders first — the confirm button is the
 * only path that produces content.
 */
export default function ExportDrawer({ artifactId, onClose }: ExportDrawerProps) {
  const { t } = useTranslation();
  const [audience, setAudience] = useState<"internal" | "customer">("internal");
  const [plan, setPlan] = useState<PmExportPlan | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (target: "internal" | "customer") => {
      if (!artifactId) return;
      setLoading(true);
      setError(null);
      setContent(null);
      try {
        setPlan(await pmApi.exportArtifact(artifactId, { audience: target }));
      } catch (err) {
        setError(apiErrorMessage(err, t("pm.error.load"), t));
      } finally {
        setLoading(false);
      }
    },
    [artifactId, t],
  );

  useEffect(() => {
    if (artifactId !== null) void load(audience);
  }, [artifactId, audience, load]);

  const confirm = async () => {
    if (!artifactId) return;
    setConfirming(true);
    try {
      const result = await pmApi.exportArtifact(artifactId, {
        audience,
        confirm: true,
      });
      setPlan(result);
      setContent(result.content);
      message.success(t("pm.export.confirmed"));
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setConfirming(false);
    }
  };

  const filename = `${plan?.title || `artifact-${artifactId ?? ""}`}-${audience}.md`;

  return (
    <Drawer
      open={artifactId !== null}
      onClose={onClose}
      width={560}
      title={t("pm.export.title")}
      destroyOnClose
    >
      <Segmented
        value={audience}
        onChange={(value) => setAudience(value as "internal" | "customer")}
        options={[
          { value: "internal", label: t("pm.audience.internal") },
          { value: "customer", label: t("pm.audience.customer") },
        ]}
        style={{ marginBottom: 12 }}
      />

      {loading ? (
        <div style={{ textAlign: "center", padding: 24 }}>
          <Spin size="small" />
        </div>
      ) : null}
      {error ? <Alert type="error" showIcon message={error} /> : null}

      {plan ? (
        <>
          {plan.ok ? (
            <Alert
              type="success"
              showIcon
              message={t("pm.export.clear")}
              description={
                <div style={{ fontSize: 12 }}>
                  <div>
                    {t("pm.export.downgradedCount", { count: plan.downgraded })}
                  </div>
                  <div>
                    {t("pm.export.droppedCount", { count: plan.dropped_evidence })}
                  </div>
                </div>
              }
              style={{ marginBottom: 12 }}
            />
          ) : (
            <Alert
              type="error"
              showIcon
              message={t("pm.export.blocked", { count: plan.hits.length })}
              description={
                <ul style={{ margin: 0, paddingInlineStart: 18, fontSize: 12 }}>
                  {plan.hits.slice(0, 12).map((hit, index) => (
                    <li key={`${hit.rule}-${index}`}>
                      <Tag color="red">{hit.rule}</Tag>
                      {hit.label}: <code>{hit.match}</code>
                    </li>
                  ))}
                </ul>
              }
              style={{ marginBottom: 12 }}
            />
          )}

          {plan.notes.length > 0 ? (
            <div style={{ fontSize: 12, color: "var(--fn-text-secondary)", marginBottom: 12 }}>
              {plan.notes.map((note, index) => (
                <div key={index}>· {note}</div>
              ))}
            </div>
          ) : null}

          {plan.conclusions.length > 0 ? (
            <>
              <div className={styles.sectionTitle}>{t("pm.conclusions.title")}</div>
              {plan.conclusions.map((item) => (
                <div key={item.seq} className={styles.conclusion}>
                  <div className={styles.conclusionText}>
                    {item.seq}. {item.customer_text || item.text}
                  </div>
                  <div className={styles.conclusionMeta}>
                    <StatusTag status={item.status} />
                    {item.downgraded ? (
                      <span className={styles.warnText}>{t("pm.export.rowDowngraded")}</span>
                    ) : null}
                    <span>{t("pm.conclusions.evidence", { count: item.evidence_ids.length })}</span>
                  </div>
                </div>
              ))}
            </>
          ) : null}

          <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
            <Button
              type="primary"
              icon={<ShieldCheck size={14} />}
              disabled={!plan.ok}
              loading={confirming}
              onClick={() => void confirm()}
            >
              {t("pm.export.confirm")}
            </Button>
            {plan.preview_only ? (
              <span className={styles.warnText} style={{ alignSelf: "center" }}>
                {t("pm.export.previewOnly")}
              </span>
            ) : null}
          </div>

          {content ? (
            <>
              <div className={styles.sectionTitle}>
                <span>{t("pm.export.content")}</span>
                <span style={{ display: "flex", gap: 4 }}>
                  <Button
                    size="small"
                    type="text"
                    icon={<Copy size={12} />}
                    onClick={() => {
                      void copyText(content).then(() =>
                        message.success(t("pm.actions.copied")),
                      );
                    }}
                  >
                    {t("pm.actions.copy")}
                  </Button>
                  <Button
                    size="small"
                    type="text"
                    icon={<Download size={12} />}
                    onClick={() =>
                      void saveBlobAsFile(
                        new Blob([content], { type: "text/markdown;charset=utf-8" }),
                        filename,
                      )
                    }
                  >
                    {t("pm.actions.download")}
                  </Button>
                </span>
              </div>
              <pre className={styles.excerpt}>{content}</pre>
            </>
          ) : null}
        </>
      ) : null}
    </Drawer>
  );
}
