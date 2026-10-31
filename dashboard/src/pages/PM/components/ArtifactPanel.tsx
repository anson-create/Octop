import { useMemo } from "react";
import { Alert, Button, Segmented, Spin, Tag, Tooltip } from "antd";
import { Copy, Download } from "lucide-react";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmArtifact, PmConclusion } from "../../../api/types";
import Markdown from "../../../components/Markdown";
import { copyText } from "../../../utils/copyText";
import { message } from "../../../utils/antdMessage";
import { isHtmlArtifact, tryFormatJson } from "../pmMeta";
import StatusTag from "./StatusTag";
import { usePMResource } from "../usePMResource";
import styles from "../pm.module.less";

interface ArtifactPanelProps {
  artifacts: PmArtifact[];
  selectedId: number | null;
  onSelect: (artifactId: number) => void;
  onOpenEvidence: (evidenceId: number) => void;
  onExport: (artifactId: number) => void;
}

interface ArtifactBundle {
  artifact: PmArtifact | null;
  conclusions: PmConclusion[];
}

/**
 * Middle column: the artifact body plus its conclusions. Every conclusion shows
 * the evidence it cites — an ``evidenced`` row with no chip would be a bug, so
 * it is rendered loudly instead of silently.
 */
export default function ArtifactPanel({
  artifacts,
  selectedId,
  onSelect,
  onOpenEvidence,
  onExport,
}: ArtifactPanelProps) {
  const { t } = useTranslation();

  const { data, loading, error } = usePMResource<ArtifactBundle>(
    { artifact: null, conclusions: [] },
    async () => {
      if (!selectedId) return { artifact: null, conclusions: [] };
      const [artifact, bundle] = await Promise.all([
        pmApi.getArtifact(selectedId),
        pmApi.artifactConclusions(selectedId),
      ]);
      return { artifact, conclusions: bundle.items };
    },
    [selectedId],
    { enabled: selectedId !== null },
  );

  const options = useMemo(
    () =>
      artifacts.map((item) => ({
        value: item.id,
        label: `${item.title || item.kind} · v${item.version}`,
      })),
    [artifacts],
  );

  if (artifacts.length === 0) {
    return <div className={styles.emptyHint}>{t("pm.artifacts.empty")}</div>;
  }

  const artifact = data.artifact ?? null;
  const body = artifact?.body_inline ?? "";
  const html = artifact ? isHtmlArtifact(artifact.kind, body) : false;
  const json = html ? null : tryFormatJson(body);

  return (
    <div>
      <div style={{ overflowX: "auto", marginBottom: 10 }}>
        <Segmented
          size="small"
          value={selectedId ?? undefined}
          options={options}
          onChange={(value) => onSelect(Number(value))}
        />
      </div>

      {error ? <Alert type="error" showIcon message={error} /> : null}
      {loading && !artifact ? (
        <div style={{ padding: 24, textAlign: "center" }}>
          <Spin size="small" />
        </div>
      ) : null}

      {artifact ? (
        <>
          <div className={styles.artifactMeta}>
            <Tag>{artifact.kind}</Tag>
            <Tag color={artifact.audience === "customer" ? "blue" : "default"}>
              {t(`pm.audience.${artifact.audience}`, {
                defaultValue: artifact.audience,
              })}
            </Tag>
            <span>
              {t("pm.artifacts.fidelity", {
                value: artifact.fidelity || "—",
              })}
            </span>
            {artifact.fidelity_note ? (
              <span>· {artifact.fidelity_note}</span>
            ) : null}
            <span style={{ flex: 1 }} />
            {body ? (
              <Button
                size="small"
                type="text"
                icon={<Copy size={13} />}
                onClick={() => {
                  void copyText(body).then(() =>
                    message.success(t("pm.actions.copied")),
                  );
                }}
              >
                {t("pm.actions.copy")}
              </Button>
            ) : null}
            <Button
              size="small"
              type="text"
              icon={<Download size={13} />}
              onClick={() => onExport(artifact.id)}
            >
              {t("pm.actions.export")}
            </Button>
          </div>

          {body ? (
            html ? (
              // Prototypes are pages. Sandboxed with allow-scripts only: the
              // artifact must run its own JS but never reach the dashboard.
              <iframe
                className={styles.htmlPreview}
                title={artifact.title || artifact.kind}
                srcDoc={body}
                sandbox="allow-scripts"
              />
            ) : json ? (
              <pre className={styles.traceJson}>{json}</pre>
            ) : (
              <div className={styles.markdown}>
                <Markdown content={body} />
              </div>
            )
          ) : (
            <div className={styles.emptyHint}>
              {t("pm.artifacts.bodyMissing", {
                path: artifact.body_path || "—",
              })}
            </div>
          )}

          <div className={styles.sectionTitle}>
            <span>{t("pm.conclusions.title")}</span>
            <span>
              {t("pm.conclusions.summary", {
                evidenced: data.conclusions.filter(
                  (item) => item.status === "evidenced",
                ).length,
                total: data.conclusions.length,
              })}
            </span>
          </div>

          {data.conclusions.length === 0 ? (
            <div className={styles.emptyHint}>{t("pm.conclusions.empty")}</div>
          ) : (
            data.conclusions.map((item) => (
              <div key={item.id} className={styles.conclusion}>
                <div className={styles.conclusionText}>
                  {item.seq}. {item.text}
                </div>
                <div className={styles.conclusionMeta}>
                  <StatusTag status={item.status} />
                  {item.customer_text ? (
                    <Tooltip title={item.customer_text}>
                      <span>{t("pm.conclusions.customerText")}</span>
                    </Tooltip>
                  ) : null}
                  {(item.evidence ?? []).length === 0 ? (
                    <span className={styles.dangerText}>
                      {t("pm.conclusions.noEvidence")}
                    </span>
                  ) : (
                    (item.evidence ?? []).map((evidence) => (
                      <button
                        key={evidence.id}
                        type="button"
                        className={styles.evidenceChip}
                        onClick={() => onOpenEvidence(evidence.id)}
                      >
                        #{evidence.id} {evidence.source_type}
                      </button>
                    ))
                  )}
                </div>
              </div>
            ))
          )}
        </>
      ) : null}
    </div>
  );
}
