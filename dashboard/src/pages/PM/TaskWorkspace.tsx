import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Segmented,
  Skeleton,
  Tag,
} from "antd";
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  Download,
  Pause,
  Play,
  RotateCcw,
  StepForward,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { pmApi } from "../../api/modules/pm";
import type { PmReviewList, PmStep, PmTaskDetail } from "../../api/types";
import { apiErrorMessage } from "../../utils/apiError";
import { message } from "../../utils/antdMessage";
import { saveBlobAsFile } from "../../utils/saveBlobAsFile";
import PageShell from "../../layouts/PageShell";
import { useIsMobile } from "../../hooks/useIsMobile";
import ArtifactPanel from "./components/ArtifactPanel";
import CollabChatPanel from "./components/CollabChatPanel";
import DecisionCard from "./components/DecisionCard";
import EvidenceDrawer from "./components/EvidenceDrawer";
import ExportDrawer from "./components/ExportDrawer";
import ReflowModal from "./components/ReflowModal";
import ReviewPanel from "./components/ReviewPanel";
import StageRail from "./components/StageRail";
import StatusTag from "./components/StatusTag";
import TraceDrawer from "./components/TraceDrawer";
import { currentStep, sourceEntries, stepLabelKey } from "./pmMeta";
import { usePMResource } from "./usePMResource";
import styles from "./pm.module.less";

type Column = "rail" | "collab" | "artifact";

const COLUMNS: readonly Column[] = ["rail", "collab", "artifact"];

function paneFrom(value: string | null): Column {
  return COLUMNS.includes(value as Column) ? (value as Column) : "rail";
}

/**
 * The three-column workspace as per the design: chain on the left,
 * chat + the human decision queue in the middle (that is where work actually
 * happens), the deliverable on the right. Artefact and evidence selection live
 * in the URL so a citation can be pasted into chat and land on the same spot.
 */
export default function TaskWorkspace() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const { taskId } = useParams();
  const id = Number(taskId);
  const [params, setParams] = useSearchParams();

  const evidenceParam = Number(params.get("evidence"));
  const evidenceId =
    Number.isFinite(evidenceParam) && evidenceParam > 0 ? evidenceParam : null;
  const artifactParam = Number(params.get("artifact"));

  const [traceStep, setTraceStep] = useState<PmStep | null>(null);
  const [exportId, setExportId] = useState<number | null>(null);
  const [reflowOpen, setReflowOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [column, setColumnState] = useState<Column>(() =>
    paneFrom(params.get("pane")),
  );
  const paneParam = params.get("pane");

  /**
   * Mobile pane selection is mirrored into the URL (``?pane=collab``): a
   * pasted link and the back gesture both land on the same pane instead of
   * dumping the user out of the task.
   */
  const setColumn = (value: Column) => {
    setColumnState(value);
    const next = new URLSearchParams(params);
    if (value === "rail") next.delete("pane");
    else next.set("pane", value);
    setParams(next, { replace: true });
  };
  /**
   * Mobile only: a phone cannot show cards and a conversation at once, so the
   * decision zone collapses to its header. ``null`` follows the task state —
   * a task blocked on a human opens the cards, everything else keeps them
   * stashed — and an explicit tap overrides it until the page is left.
   */
  const [zoneOpen, setZoneOpen] = useState<boolean | null>(null);

  // Switching tasks must not inherit the previous task's collapsed state.
  useEffect(() => {
    setZoneOpen(null);
  }, [id]);

  // Keep the pane in step with the URL (back gesture, pasted link).
  useEffect(() => {
    setColumnState(paneFrom(paneParam));
  }, [paneParam]);

  const { data, loading, error, refresh } = usePMResource<PmTaskDetail | null>(
    null,
    () => pmApi.getTask(id),
    [id],
    { enabled: Number.isFinite(id) },
  );
  const reviews = usePMResource<PmReviewList | null>(
    null,
    () => pmApi.listReviews(id),
    [id],
    { enabled: Number.isFinite(id) },
  );

  const selectedArtifactId = useMemo(() => {
    const list = data?.artifacts ?? [];
    if (list.length === 0) return null;
    if (Number.isFinite(artifactParam) && list.some((item) => item.id === artifactParam)) {
      return artifactParam;
    }
    return list[0].id;
  }, [data, artifactParam]);

  const openEvidence = (value: number | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete("evidence");
    else next.set("evidence", String(value));
    setParams(next, { replace: true });
  };

  const selectArtifact = (artifactId: number) => {
    const next = new URLSearchParams(params);
    next.set("artifact", String(artifactId));
    setParams(next, { replace: true });
  };

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    try {
      await fn();
      await refresh();
      await reviews.refresh();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setBusy(null);
    }
  };

  const tick = () =>
    run("tick", async () => {
      const result = await pmApi.tick(id);
      message.success(
        t("pm.tick.result", {
          step: result.step ? t(stepLabelKey(String(result.step))) : "—",
          status: t(`pm.status.${result.status ?? "running"}`, {
            defaultValue: String(result.status ?? ""),
          }),
        }),
      );
      if (result.reason) message.info(String(result.reason));
    });

  const togglePause = () =>
    run("pause", async () => {
      if (data?.status === "paused") {
        await pmApi.resume(id);
        message.success(t("pm.tick.resumed"));
      } else {
        await pmApi.pause(id);
        message.success(t("pm.tick.paused"));
      }
    });

  const exportBundle = () =>
    run("bundle", async () => {
      const bundle = await pmApi.exportBundle(id);
      await saveBlobAsFile(
        new Blob([bundle.content], { type: "text/markdown;charset=utf-8" }),
        `${data?.public_id ?? `task-${id}`}-bundle.md`,
      );
      message.success(t("pm.export.bundleDone"));
    });

  const stepFor = (stepId: number | null): PmStep | undefined =>
    (data?.steps ?? []).find((step) => step.id === stepId);

  const activeStep = data ? currentStep(data.steps) : null;
  const requirement = data?.requirement ?? null;
  const zoneVisible =
    !isMobile || (zoneOpen ?? data?.status === "waiting_human");
  const pendingCount = data?.pending_decisions.length ?? 0;

  const actions = (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      <Button size="small" icon={<ArrowLeft size={13} />} onClick={() => navigate("/pm")}>
        {t("pm.actions.back")}
      </Button>
      <Button
        size="small"
        type="primary"
        icon={<StepForward size={13} />}
        loading={busy === "tick"}
        disabled={!data || data.status === "waiting_human" || data.status === "delivered"}
        onClick={() => void tick()}
      >
        {t("pm.actions.tick")}
      </Button>
      <Button
        size="small"
        icon={data?.status === "paused" ? <Play size={13} /> : <Pause size={13} />}
        loading={busy === "pause"}
        disabled={!data || data.status === "delivered"}
        onClick={() => void togglePause()}
      >
        {data?.status === "paused" ? t("pm.actions.resume") : t("pm.actions.pause")}
      </Button>
      <Button
        size="small"
        icon={<RotateCcw size={13} />}
        disabled={!data}
        onClick={() => setReflowOpen(true)}
      >
        {t("pm.actions.reflow")}
      </Button>
      <Button
        size="small"
        icon={<Download size={13} />}
        loading={busy === "bundle"}
        disabled={!data}
        onClick={() => void exportBundle()}
      >
        {t("pm.export.bundle")}
      </Button>
    </div>
  );

  return (
    <PageShell
      title={data?.public_id ?? t("pm.task.title")}
      subtitle={
        data?.requirement
          ? `${data.requirement.public_id} · ${data.requirement.title}`
          : undefined
      }
      actions={actions}
    >
      <div className={styles.page}>
        {error ? <Alert type="error" showIcon message={error} /> : null}
        {loading && !data ? <Skeleton active paragraph={{ rows: 8 }} /> : null}

        {data ? (
          <>
            <div className={styles.health}>
              <span className={styles.healthTitle}>
                <StatusTag status={data.status} />
              </span>
              <span>· {t("pm.task.stage", { stage: data.stage })}</span>
              <span>
                ·{" "}
                {t("pm.task.currentStep", {
                  step: activeStep ? t(stepLabelKey(activeStep.step_key)) : "—",
                })}
              </span>
              <span>· {t("pm.task.evidence", { count: data.evidence_count })}</span>
              <span>
                ·{" "}
                {t("pm.task.conclusions", {
                  evidenced: data.conclusions.evidenced,
                  total: data.conclusions.total,
                })}
              </span>
              <span>· {t("pm.task.pendingCards", { count: data.pending_decisions.length })}</span>
              <span className={styles.healthSpacer} />
              {requirement ? (
                <Button
                  size="small"
                  type="text"
                  onClick={() => navigate(`/pm/requirements/${requirement.id}`)}
                >
                  {t("pm.task.openRequirement")}
                </Button>
              ) : null}
            </div>

            {isMobile ? (
              <Segmented
                block
                className={styles.mobileCols}
                value={column}
                onChange={(value) => setColumn(value as Column)}
                options={[
                  { value: "rail", label: t("pm.task.colRail") },
                  { value: "collab", label: t("pm.task.colCollab") },
                  { value: "artifact", label: t("pm.task.colArtifact") },
                ]}
              />
            ) : null}

            <div className={`${styles.grid} ${isMobile ? styles.gridNarrow : ""}`}>
              {!isMobile || column === "rail" ? (
                <section className={styles.col}>
                  <div className={styles.colHead}>
                    <span>{t("pm.task.chain")}</span>
                    <span style={{ fontWeight: 400, fontSize: 11.5 }}>
                      {t("pm.task.stepsHint")}
                    </span>
                  </div>
                  <div className={styles.colBody}>
                    <StageRail
                      steps={data.steps}
                      stages={data.stages}
                      activeStepId={traceStep?.id ?? null}
                      onOpenStep={(step) => setTraceStep(step)}
                    />
                  </div>
                </section>
              ) : null}

              {!isMobile || column === "collab" ? (
                <section className={styles.col}>
                  <div className={styles.colHead}>
                    <span>{t("pm.task.colCollab")}</span>
                    <span style={{ fontWeight: 400, fontSize: 11.5 }}>
                      {t("pm.collab.hint")}
                    </span>
                  </div>
                  <div className={`${styles.colBody} ${styles.colBodySplit}`}>
                    <div
                      className={styles.decideZone}
                      data-empty={pendingCount === 0}
                      data-collapsed={isMobile && !zoneVisible}
                    >
                      <div className={styles.zoneHead}>
                        {isMobile ? (
                          <button
                            type="button"
                            className={styles.zoneToggle}
                            aria-expanded={zoneVisible}
                            aria-label={t("pm.decision.toggle")}
                            onClick={() => setZoneOpen(!zoneVisible)}
                          >
                            <span>{t("pm.decision.title")}</span>
                            {pendingCount > 0 ? (
                              <span className={styles.zoneCount}>{pendingCount}</span>
                            ) : null}
                            {zoneVisible ? (
                              <ChevronDown size={13} />
                            ) : (
                              <ChevronRight size={13} />
                            )}
                          </button>
                        ) : (
                          <>
                            <span>{t("pm.decision.title")}</span>
                            {pendingCount > 0 ? (
                              <span className={styles.zoneCount}>
                                {pendingCount}
                              </span>
                            ) : null}
                          </>
                        )}
                      </div>
                      {!zoneVisible ? null : pendingCount === 0 ? (
                        <div className={styles.zoneEmpty}>{t("pm.decision.empty")}</div>
                      ) : (
                        <div className={styles.zoneScroll}>
                          {data.pending_decisions.map((decision) => {
                            const step = stepFor(decision.step_id);
                            return (
                              <DecisionCard
                                key={decision.id}
                                decision={decision}
                                context={
                                  step ? t(stepLabelKey(step.step_key)) : undefined
                                }
                                onChanged={() => void refresh()}
                              />
                            );
                          })}
                        </div>
                      )}
                    </div>

                    <CollabChatPanel
                      fill
                      taskPublicId={data.public_id}
                      stepLabel={
                        activeStep ? t(stepLabelKey(activeStep.step_key)) : null
                      }
                      pendingCards={data.pending_decisions.length}
                    />
                  </div>
                </section>
              ) : null}

              {!isMobile || column === "artifact" ? (
                <section className={styles.col}>
                  <div className={styles.colHead}>
                    <span>{t("pm.artifacts.title")}</span>
                    <span style={{ fontWeight: 400, fontSize: 11.5 }}>
                      {t("pm.artifacts.count", { count: data.artifacts.length })}
                    </span>
                  </div>
                  <div className={styles.colBody}>
                    <ArtifactPanel
                      artifacts={data.artifacts}
                      selectedId={selectedArtifactId}
                      onSelect={selectArtifact}
                      onOpenEvidence={openEvidence}
                      onExport={(artifactId) => setExportId(artifactId)}
                    />

                    <ReviewPanel
                      taskId={id}
                      reviews={reviews.data}
                      onChanged={() => void reviews.refresh()}
                    />

                    <div className={styles.sectionTitle}>
                      <span>{t("pm.task.evidenceOverview")}</span>
                      <span>{data.evidence_count}</span>
                    </div>
                    {sourceEntries(data.evidence_sources).length === 0 ? (
                      <div className={styles.emptyHint}>{t("pm.evidence.empty")}</div>
                    ) : (
                      sourceEntries(data.evidence_sources).map((item) => (
                        <Tag key={item.source} style={{ marginBottom: 4 }}>
                          {t(`pm.sources.${item.source}`, {
                            defaultValue: item.source,
                          })}{" "}
                          · {item.count}
                        </Tag>
                      ))
                    )}
                    <div
                      style={{
                        marginTop: 6,
                        fontSize: 12,
                        color: "var(--fn-text-secondary)",
                      }}
                    >
                      {t("pm.conclusions.breakdown", {
                        evidenced: data.conclusions.evidenced,
                        uncovered: data.conclusions.uncovered,
                        undetermined: data.conclusions.undetermined,
                      })}
                    </div>
                    <div className={styles.emptyHint} style={{ paddingTop: 8 }}>
                      {t("pm.evidence.chipHint")}
                    </div>
                  </div>
                </section>
              ) : null}
            </div>
          </>
        ) : null}

        {!loading && !data && !error ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("pm.error.notFound")} />
        ) : null}
      </div>

      <EvidenceDrawer evidenceId={evidenceId} onClose={() => openEvidence(null)} />
      <ExportDrawer artifactId={exportId} onClose={() => setExportId(null)} />
      <TraceDrawer
        stepId={traceStep?.id ?? null}
        stepLabel={traceStep ? t(stepLabelKey(traceStep.step_key)) : ""}
        onClose={() => setTraceStep(null)}
        onOpenEvidence={(value) => {
          setTraceStep(null);
          openEvidence(value);
        }}
        onChanged={() => void refresh()}
      />
      <ReflowModal
        open={reflowOpen}
        taskId={id}
        onClose={() => setReflowOpen(false)}
        onDone={() => void refresh()}
      />

      {data && data.status === "waiting_human" ? (
        <div className={styles.desktopOnly}>
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 10 }}
            message={t("pm.task.waitingHuman")}
            description={t("pm.task.waitingHumanHint")}
          />
        </div>
      ) : null}
    </PageShell>
  );
}
