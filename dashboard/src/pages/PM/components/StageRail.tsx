import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";
import type { PmStageProgress, PmStep } from "../../../api/types";
import {
  STAGE_ORDER,
  gateIssues,
  gatePassed,
  stageLabelKey,
  stepLabelKey,
  stepProgress,
} from "../pmMeta";
import styles from "../pm.module.less";

interface StageRailProps {
  steps: PmStep[];
  stages: PmStageProgress[];
  activeStepId: number | null;
  onOpenStep: (step: PmStep) => void;
}

/**
 * The 13 steps grouped by their 5 stages. Clicking a row opens the gate +
 * trace drawer — the rail is the only place a failed gate is discoverable.
 */
export default function StageRail({
  steps,
  stages,
  activeStepId,
  onOpenStep,
}: StageRailProps) {
  const { t } = useTranslation();
  const ordered = [...steps].sort((a, b) => a.seq - b.seq);
  const progress = Math.round(stepProgress(ordered) * 100);

  return (
    <div>
      <div className={styles.stageChips}>
        {STAGE_ORDER.map((key) => {
          const stage = stages.find((item) => item.key === key);
          return (
            <span
              key={key}
              className={styles.stageChip}
              data-state={stage?.state ?? "pending"}
            >
              {t(stageLabelKey(key))}
            </span>
          );
        })}
      </div>
      <div className={styles.progressBar} role="progressbar" aria-valuenow={progress}>
        <div className={styles.progressFill} style={{ width: `${progress}%` }} />
      </div>

      {STAGE_ORDER.map((stageKey) => {
        const group = ordered.filter((step) => step.stage === stageKey);
        if (group.length === 0) return null;
        return (
          <div key={stageKey} className={styles.stageGroup}>
            <div className={styles.stageTitle}>
              <span>{t(stageLabelKey(stageKey))}</span>
              <span>
                {group.filter((step) => step.status === "passed").length}/
                {group.length}
              </span>
            </div>
            {group.map((step) => {
              const failed = step.gate ? !gatePassed(step.gate) : false;
              const issueCount = gateIssues(step.gate).length;
              return (
                <button
                  key={step.id}
                  type="button"
                  className={`${styles.stepRow} ${
                    activeStepId === step.id ? styles.stepRowActive : ""
                  }`}
                  onClick={() => onOpenStep(step)}
                >
                  <span
                    className={styles.stepDot}
                    data-status={step.status}
                    aria-hidden
                  />
                  <span style={{ minWidth: 0 }}>
                    <span className={styles.stepName}>
                      {step.seq}. {t(stepLabelKey(step.step_key))}
                    </span>
                    <span className={styles.stepMeta}>
                      <Tooltip title={t(`pm.status.${step.status}`, { defaultValue: step.status })}>
                        <span>{t(`pm.status.${step.status}`, { defaultValue: step.status })}</span>
                      </Tooltip>
                      {step.attempt > 1 ? (
                        <span>{t("pm.stepsUi.attempt", { count: step.attempt })}</span>
                      ) : null}
                      {step.error_code ? (
                        <span className={styles.dangerText}>{step.error_code}</span>
                      ) : null}
                      {failed ? (
                        <span className={styles.warnText}>
                          {t("pm.stepsUi.gateFailed", { count: issueCount })}
                        </span>
                      ) : null}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
