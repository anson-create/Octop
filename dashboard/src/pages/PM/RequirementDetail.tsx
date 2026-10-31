import { useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Input,
  Modal,
  Skeleton,
  Tag,
} from "antd";
import { ArrowLeft, ArrowRight, FilePlus2, PlayCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router-dom";
import { pmApi } from "../../api/modules/pm";
import type { PmRequirementDetail } from "../../api/types";
import { apiErrorMessage } from "../../utils/apiError";
import { message } from "../../utils/antdMessage";
import PageShell from "../../layouts/PageShell";
import StatusTag from "./components/StatusTag";
import { usePMResource } from "./usePMResource";
import styles from "./pm.module.less";

/**
 * Requirement detail: the intake text, its change history, and the 1:1 task
 * handoff. Requirement ↔ task is 1:1 by DB constraint, so there is no "create
 * another task" affordance once one exists.
 */
export default function RequirementDetail() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { requirementId } = useParams();
  const id = Number(requirementId);

  const [changeOpen, setChangeOpen] = useState(false);
  const [changeText, setChangeText] = useState("");
  const [busy, setBusy] = useState(false);

  const { data, loading, error, refresh } = usePMResource<PmRequirementDetail | null>(
    null,
    () => pmApi.getRequirement(id),
    [id],
    { enabled: Number.isFinite(id) },
  );
  const task = data?.task ?? null;

  const submitChange = async () => {
    if (!data || !changeText.trim()) return;
    setBusy(true);
    try {
      await pmApi.addChange(data.id, changeText.trim());
      message.success(t("pm.requirement.changeAdded"));
      setChangeText("");
      setChangeOpen(false);
      await refresh();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setBusy(false);
    }
  };

  const createTask = async () => {
    if (!data) return;
    setBusy(true);
    try {
      const task = await pmApi.createTask({ requirement_id: data.id });
      message.success(t("pm.requirement.taskCreated"));
      navigate(`/pm/tasks/${task.id}`);
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setBusy(false);
    }
  };

  const actions = (
    <div style={{ display: "flex", gap: 8 }}>
      <Button size="small" icon={<ArrowLeft size={13} />} onClick={() => navigate("/pm")}>
        {t("pm.actions.back")}
      </Button>
      <Button
        size="small"
        icon={<FilePlus2 size={13} />}
        onClick={() => setChangeOpen(true)}
        disabled={!data}
      >
        {t("pm.actions.addChange")}
      </Button>
      {task ? (
        <Button
          size="small"
          type="primary"
          icon={<PlayCircle size={13} />}
          onClick={() => navigate(`/pm/tasks/${task.id}`)}
        >
          {t("pm.actions.openTask")}
        </Button>
      ) : (
        <Button
          size="small"
          type="primary"
          icon={<ArrowRight size={13} />}
          loading={busy}
          disabled={!data}
          onClick={() => void createTask()}
        >
          {t("pm.actions.createTask")}
        </Button>
      )}
    </div>
  );

  return (
    <PageShell
      title={data?.public_id ?? t("pm.requirement.title")}
      subtitle={data?.title}
      actions={actions}
    >
      {error ? <Alert type="error" showIcon message={error} /> : null}
      {loading && !data ? <Skeleton active paragraph={{ rows: 6 }} /> : null}

      {data ? (
        <div className={styles.evidenceBlock}>
          <Descriptions
            size="small"
            column={{ xs: 1, sm: 2, md: 3 }}
            items={[
              {
                key: "status",
                label: t("pm.fields.status"),
                children: <StatusTag status={data.status} />,
              },
              {
                key: "source",
                label: t("pm.fields.source"),
                children: (
                  <>
                    <Tag>{t(`pm.sources.${data.source}`, { defaultValue: data.source })}</Tag>
                    {data.source_ref ? <span>{data.source_ref}</span> : null}
                  </>
                ),
              },
              {
                key: "proposer",
                label: t("pm.fields.proposer"),
                children: data.proposer || "—",
              },
              {
                key: "priority",
                label: t("pm.fields.priority"),
                children: data.priority ?? "—",
              },
              {
                key: "created",
                label: t("pm.fields.createdAt"),
                children: data.created_at
                  ? new Date(data.created_at).toLocaleString()
                  : "—",
              },
              {
                key: "workspace",
                label: t("pm.fields.workspace"),
                children: `#${data.workspace_id}`,
              },
            ]}
          />

          <div className={styles.sectionTitle}>{t("pm.fields.rawText")}</div>
          <pre className={styles.excerpt}>{data.raw_text}</pre>

          <div className={styles.sectionTitle}>
            <span>{t("pm.requirement.task")}</span>
          </div>
          {task ? (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                flexWrap: "wrap",
                fontSize: 13,
              }}
            >
              <span style={{ fontFamily: "var(--fn-font-mono)" }}>{task.public_id}</span>
              <StatusTag status={task.status} />
              <Tag>{task.stage}</Tag>
              <Button
                size="small"
                type="link"
                onClick={() => navigate(`/pm/tasks/${task.id}`)}
              >
                {t("pm.actions.openTask")}
              </Button>
            </div>
          ) : (
            <div className={styles.emptyHint}>{t("pm.requirement.noTask")}</div>
          )}

          <div className={styles.sectionTitle}>
            <span>{t("pm.requirement.changes")}</span>
            <span>{data.changes.length}</span>
          </div>
          {data.changes.length === 0 ? (
            <div className={styles.emptyHint}>{t("pm.requirement.noChanges")}</div>
          ) : (
            data.changes.map((change) => (
              <div key={change.id} className={styles.conclusion}>
                <div className={styles.conclusionText}>{change.change_text}</div>
                <div className={styles.conclusionMeta}>
                  <Tag color={change.accepted ? "green" : "orange"}>
                    {change.accepted
                      ? t("pm.requirement.accepted")
                      : t("pm.requirement.pendingChange")}
                  </Tag>
                  {change.created_at
                    ? new Date(change.created_at).toLocaleString()
                    : null}
                </div>
              </div>
            ))
          )}
        </div>
      ) : null}

      <Modal
        open={changeOpen}
        title={t("pm.actions.addChange")}
        onCancel={() => setChangeOpen(false)}
        onOk={() => void submitChange()}
        okButtonProps={{ loading: busy, disabled: !changeText.trim() }}
        okText={t("pm.actions.submit")}
        cancelText={t("pm.actions.cancel")}
        destroyOnClose
      >
        <Input.TextArea
          value={changeText}
          onChange={(event) => setChangeText(event.target.value)}
          placeholder={t("pm.requirement.changePlaceholder")}
          autoSize={{ minRows: 3, maxRows: 8 }}
          maxLength={4000}
        />
      </Modal>
    </PageShell>
  );
}
