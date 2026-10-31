import { useState } from "react";
import { Button, Tooltip } from "antd";
import { RefreshCw, ScanLine } from "lucide-react";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmHealth } from "../../../api/types";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";
import { usePMResource } from "../usePMResource";
import styles from "../pm.module.less";

/**
 * Subsystem health strip: migration state, queue depth, and the manual sweep
 * hook. The sweep endpoint exists because the 30s scheduler is not wired yet —
 * without this button an expired card would sit there until someone noticed.
 */
export default function HealthBar() {
  const { t } = useTranslation();
  const [sweeping, setSweeping] = useState(false);
  const { data, loading, error, refresh } = usePMResource<PmHealth | null>(
    null,
    () => pmApi.health(),
    [],
  );

  const dotClass = error
    ? `${styles.dot} ${styles.dotError}`
    : data?.enabled === false
      ? `${styles.dot} ${styles.dotWarn}`
      : data?.status === "ok"
        ? styles.dot
        : `${styles.dot} ${styles.dotWarn}`;

  const onSweep = async () => {
    setSweeping(true);
    try {
      const result = await pmApi.sweep();
      message.success(
        t("pm.health.sweepDone", {
          auto: result.auto_answered ?? 0,
          expired: result.expired ?? 0,
        }),
      );
      await refresh();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setSweeping(false);
    }
  };

  const counts = data?.tasks ?? {};
  const inflight =
    (counts.queued ?? 0) + (counts.running ?? 0) + (counts.waiting_human ?? 0);

  return (
    <div className={styles.health}>
      <span className={styles.healthTitle}>
        <span className={dotClass} aria-hidden />
        {error
          ? t("pm.health.unavailable")
          : data?.enabled === false
            ? t("pm.health.disabled")
            : data?.status === "ok"
              ? t("pm.health.ok")
              : t("pm.health.degraded")}
      </span>

      {data?.migration?.version ? (
        <span>{t("pm.health.migration", { version: data.migration.version })}</span>
      ) : null}
      {!error && data ? (
        <>
          <span>· {t("pm.health.inflight", { count: inflight })}</span>
          <span>· {t("pm.health.pendingDecisions", { count: data.decisions_pending })}</span>
          {data.decisions_due > 0 ? (
            <span className={styles.dangerText}>
              · {t("pm.health.due", { count: data.decisions_due })}
            </span>
          ) : null}
        </>
      ) : null}

      {error ? (
        <Tooltip title={error}>
          <span className={styles.warnText}>{t("pm.error.unavailable")}</span>
        </Tooltip>
      ) : null}

      <span className={styles.healthSpacer} />

      <Button
        size="small"
        type="text"
        icon={<RefreshCw size={13} />}
        loading={loading}
        onClick={() => void refresh()}
      >
        {t("pm.actions.refresh")}
      </Button>
      <Tooltip title={t("pm.health.sweepHint")}>
        <Button
          size="small"
          type="text"
          icon={<ScanLine size={13} />}
          loading={sweeping}
          onClick={() => void onSweep()}
        >
          {t("pm.health.sweep")}
        </Button>
      </Tooltip>
    </div>
  );
}
