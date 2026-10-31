import { useState } from "react";
import { Alert, Button, Empty, Pagination, Select, Skeleton } from "antd";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import { pmApi } from "../../api/modules/pm";
import type { PmDecision, PmPaged } from "../../api/types";
import PageShell from "../../layouts/PageShell";
import DecisionCard from "./components/DecisionCard";
import { sortByUrgency } from "./pmMeta";
import { usePMResource } from "./usePMResource";
import styles from "./pm.module.less";

const PAGE_SIZE = 50;
const STATUSES = ["pending", "answered", "auto_answered", "expired"];
const LEVELS = ["L1", "L2", "L3"];

/**
 * The human queue. Cards are ordered by urgency (overdue first) rather than by
 * creation time, because the thing that breaks a delivery is the card nobody
 * saw before it expired.
 */
export default function DecisionCenter() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const taskFilter = Number(params.get("task")) || undefined;

  const [status, setStatus] = useState("pending");
  const [level, setLevel] = useState("");
  const [mine, setMine] = useState(false);
  const [page, setPage] = useState(1);

  const { data, loading, error, refresh } = usePMResource<PmPaged<PmDecision>>(
    { items: [], total: 0, page: 1, page_size: PAGE_SIZE },
    () =>
      pmApi.listDecisions({
        status: status || undefined,
        level: level || undefined,
        mine: mine || undefined,
        task_id: taskFilter,
        page,
        page_size: PAGE_SIZE,
      }),
    [status, level, mine, taskFilter, page],
  );

  const cards = sortByUrgency(data.items);

  return (
    <PageShell
      title={t("pm.decision.centerTitle")}
      subtitle={t("pm.decision.centerSubtitle")}
      actions={
        <div style={{ display: "flex", gap: 8 }}>
          <Button size="small" icon={<ArrowLeft size={13} />} onClick={() => navigate("/pm")}>
            {t("pm.actions.back")}
          </Button>
          <Button
            size="small"
            icon={<RefreshCw size={13} />}
            loading={loading}
            onClick={() => void refresh()}
          >
            {t("pm.actions.refresh")}
          </Button>
        </div>
      }
    >
      <div className={styles.page}>
        <div className={styles.toolbar}>
          <Select
            size="small"
            value={status}
            onChange={(value) => {
              setStatus(value);
              setPage(1);
            }}
            style={{ minWidth: 140 }}
            options={[
              { value: "", label: t("pm.filters.allStatus") },
              ...STATUSES.map((value) => ({
                value,
                label: t(`pm.status.${value}`, { defaultValue: value }),
              })),
            ]}
          />
          <Select
            size="small"
            value={level}
            onChange={(value) => {
              setLevel(value);
              setPage(1);
            }}
            style={{ minWidth: 150 }}
            options={[
              { value: "", label: t("pm.filters.allLevels") },
              ...LEVELS.map((value) => ({
                value,
                label: `${value} · ${t(`pm.levels.${value}`)}`,
              })),
            ]}
          />
          <Button
            size="small"
            type={mine ? "primary" : "default"}
            onClick={() => {
              setMine((value) => !value);
              setPage(1);
            }}
          >
            {t("pm.filters.mine")}
          </Button>
          <span className={styles.toolbarSpacer} />
          <span style={{ fontSize: 12, color: "var(--fn-text-tertiary)" }}>
            {t("pm.decision.total", { count: data.total })}
          </span>
        </div>

        {error ? <Alert type="error" showIcon message={error} /> : null}
        {loading && cards.length === 0 ? <Skeleton active paragraph={{ rows: 5 }} /> : null}

        {!loading && cards.length === 0 && !error ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t("pm.empty.decisions")}
          />
        ) : null}

        {cards.map((card) => (
          <DecisionCard
            key={card.id}
            decision={card}
            context={`${t("pm.task.label")} #${card.task_id}`}
            onChanged={() => void refresh()}
          />
        ))}

        {data.total > PAGE_SIZE ? (
          <Pagination
            size="small"
            current={page}
            pageSize={PAGE_SIZE}
            total={data.total}
            onChange={setPage}
            style={{ marginTop: 8, textAlign: "center" }}
          />
        ) : null}

        <div style={{ marginTop: 8 }}>
          <Button
            size="small"
            type="link"
            onClick={() => navigate("/pm?tab=tasks")}
          >
            {t("pm.decision.backToTasks")}
          </Button>
        </div>
      </div>
    </PageShell>
  );
}
