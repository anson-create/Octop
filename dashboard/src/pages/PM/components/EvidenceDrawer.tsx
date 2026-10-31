import { Alert, Button, Drawer, Spin, Tag } from "antd";
import { Copy, ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { pmApi } from "../../../api/modules/pm";
import type { PmEvidence } from "../../../api/types";
import { copyText } from "../../../utils/copyText";
import { message } from "../../../utils/antdMessage";
import { isOverdue, summarizeLocator } from "../pmMeta";
import { usePMResource } from "../usePMResource";
import styles from "../pm.module.less";

interface EvidenceDrawerProps {
  evidenceId: number | null;
  onClose: () => void;
}

function formatConfidence(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

/**
 * Evidence deep-link target: the quote plus its address. A locator that cannot
 * be turned into something clickable says "无定位" — it never fakes a link.
 */
export default function EvidenceDrawer({
  evidenceId,
  onClose,
}: EvidenceDrawerProps) {
  const { t } = useTranslation();
  const { data, loading, error } = usePMResource<PmEvidence | null>(
    null,
    () => pmApi.getEvidence(Number(evidenceId)),
    [evidenceId],
    { enabled: evidenceId !== null },
  );

  const locator = summarizeLocator(data?.locator);
  const stale = isOverdue(data?.stale_at);

  return (
    <Drawer
      open={evidenceId !== null}
      onClose={onClose}
      width={520}
      title={t("pm.evidence.drawerTitle", { id: evidenceId ?? "" })}
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
            <span className={styles.kvKey}>{t("pm.fields.source")}</span>
            <span className={styles.kvValue}>
              <Tag>{data.source_type}</Tag>
              {data.port && data.port !== data.source_type ? (
                <span style={{ marginLeft: 6 }}>{data.port}</span>
              ) : null}
            </span>

            <span className={styles.kvKey}>{t("pm.evidence.visibility")}</span>
            <span className={styles.kvValue}>
              {t(`pm.audience.${data.visibility}`, { defaultValue: data.visibility })}
            </span>

            <span className={styles.kvKey}>{t("pm.evidence.confidence")}</span>
            <span className={styles.kvValue}>{formatConfidence(data.confidence)}</span>

            <span className={styles.kvKey}>{t("pm.evidence.retrievedAt")}</span>
            <span className={styles.kvValue}>
              {data.retrieved_at
                ? new Date(data.retrieved_at).toLocaleString()
                : "—"}
            </span>

            <span className={styles.kvKey}>{t("pm.evidence.locator")}</span>
            <span className={styles.kvValue}>
              {locator.kind === "unknown" ? (
                <span className={styles.dangerText}>{t("pm.evidence.noLocator")}</span>
              ) : (
                <>
                  <Tag>{locator.kind}</Tag>
                  {locator.href && locator.external ? (
                    <a href={locator.href} target="_blank" rel="noreferrer">
                      {locator.text} <ExternalLink size={11} />
                    </a>
                  ) : locator.href ? (
                    <Link to={locator.href}>{locator.text}</Link>
                  ) : (
                    <span style={{ fontFamily: "var(--fn-font-mono)" }}>
                      {locator.text || "—"}
                    </span>
                  )}
                  <Button
                    size="small"
                    type="text"
                    icon={<Copy size={12} />}
                    onClick={() => {
                      const text = JSON.stringify(data.locator ?? {}, null, 2);
                      void copyText(text).then(() =>
                        message.success(t("pm.actions.copied")),
                      );
                    }}
                  />
                </>
              )}
            </span>

            <span className={styles.kvKey}>{t("pm.evidence.staleAt")}</span>
            <span className={styles.kvValue}>
              {data.stale_at ? (
                <span className={stale ? styles.warnText : undefined}>
                  {new Date(data.stale_at).toLocaleString()}
                  {stale ? ` · ${t("pm.evidence.stale")}` : ""}
                </span>
              ) : (
                "—"
              )}
            </span>

            <span className={styles.kvKey}>{t("pm.evidence.hash")}</span>
            <span className={styles.kvValue} style={{ fontFamily: "var(--fn-font-mono)" }}>
              {data.content_hash ? `${data.content_hash.slice(0, 16)}…` : "—"}
            </span>
          </div>

          <div className={styles.sectionTitle}>
            <span>{t("pm.evidence.excerpt")}</span>
            <Button
              size="small"
              type="text"
              icon={<Copy size={12} />}
              onClick={() => {
                void copyText(data.excerpt ?? "").then(() =>
                  message.success(t("pm.actions.copied")),
                );
              }}
            >
              {t("pm.actions.copy")}
            </Button>
          </div>
          <pre className={styles.excerpt}>{data.excerpt || "—"}</pre>
        </div>
      ) : null}
    </Drawer>
  );
}
