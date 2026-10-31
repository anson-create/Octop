import { useEffect, useState } from "react";
import { Alert, Button, Empty, Input, Modal, Select, Table, Tabs } from "antd";
import type { TableColumnsType } from "antd";
import { Gavel, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { pmApi } from "../../api/modules/pm";
import type { PmPaged, PmRequirement, PmTask, PmWorkspace } from "../../api/types";
import { apiErrorMessage } from "../../utils/apiError";
import { message } from "../../utils/antdMessage";
import PageShell from "../../layouts/PageShell";
import HealthBar from "./components/HealthBar";
import NewRequirementModal from "./components/NewRequirementModal";
import StatusTag from "./components/StatusTag";
import { usePMResource } from "./usePMResource";
import styles from "./pm.module.less";

const PAGE_SIZE = 20;

const REQUIREMENT_STATUSES = ["open", "in_progress", "delivered", "closed"];
const TASK_STATUSES = [
  "queued",
  "running",
  "waiting_human",
  "blocked",
  "paused",
  "delivered",
];

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "—";
}

/** Zero-state helper: PM needs at least one workspace before intake works. */
function WorkspaceCreator({ onCreated }: { onCreated: () => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      await pmApi.createWorkspace({ name: name.trim() });
      message.success(t("pm.workspace.created"));
      setOpen(false);
      setName("");
      onCreated();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.error.load"), t));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button type="primary" size="small" onClick={() => setOpen(true)}>
        {t("pm.workspace.create")}
      </Button>
      <Modal
        open={open}
        title={t("pm.workspace.create")}
        onCancel={() => setOpen(false)}
        onOk={() => void submit()}
        okButtonProps={{ loading: busy, disabled: !name.trim() }}
        okText={t("pm.actions.create")}
        cancelText={t("pm.actions.cancel")}
        destroyOnClose
      >
        <Input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={t("pm.workspace.namePlaceholder")}
          maxLength={80}
          onPressEnter={() => void submit()}
        />
      </Modal>
    </>
  );
}

export default function PMPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [workspaceId, setWorkspaceId] = useState<number | null>(null);
  const [tab, setTab] = useState("requirements");
  const [status, setStatus] = useState<string>("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [mine, setMine] = useState(false);
  const [newOpen, setNewOpen] = useState(false);

  const workspaces = usePMResource<PmWorkspace[]>(
    [],
    () => pmApi.listWorkspaces().then((result) => result.items),
    [],
  );

  useEffect(() => {
    if (workspaceId === null && workspaces.data.length > 0) {
      setWorkspaceId(workspaces.data[0].id);
    }
  }, [workspaces.data, workspaceId]);

  const requirements = usePMResource<PmPaged<PmRequirement>>(
    { items: [], total: 0, page: 1, page_size: PAGE_SIZE },
    () =>
      pmApi.listRequirements({
        workspace_id: workspaceId ?? undefined,
        status: status || undefined,
        q: query || undefined,
        page,
        page_size: PAGE_SIZE,
      }),
    [workspaceId, status, query, page],
    { enabled: workspaceId !== null },
  );

  const tasks = usePMResource<PmPaged<PmTask>>(
    { items: [], total: 0, page: 1, page_size: PAGE_SIZE },
    () =>
      pmApi.listTasks({
        workspace_id: workspaceId ?? undefined,
        status: status || undefined,
        owner: mine ? "me" : undefined,
        page,
        page_size: PAGE_SIZE,
      }),
    [workspaceId, status, mine, page],
    { enabled: workspaceId !== null },
  );

  const requirementColumns: TableColumnsType<PmRequirement> = [
    {
      title: t("pm.fields.id"),
      dataIndex: "public_id",
      width: 120,
      render: (value: string) => (
        <span style={{ fontFamily: "var(--fn-font-mono)", fontSize: 12 }}>{value}</span>
      ),
    },
    { title: t("pm.fields.title"), dataIndex: "title", ellipsis: true },
    {
      title: t("pm.fields.status"),
      dataIndex: "status",
      width: 110,
      render: (value: string) => <StatusTag status={value} />,
    },
    {
      title: t("pm.fields.priority"),
      dataIndex: "priority",
      width: 80,
      render: (value: number | null) => value ?? "—",
    },
    {
      title: t("pm.fields.proposer"),
      dataIndex: "proposer",
      width: 120,
      render: (value: string | null) => value || "—",
    },
    {
      title: t("pm.fields.createdAt"),
      dataIndex: "created_at",
      width: 170,
      render: (value: string | null) => formatTime(value),
    },
    {
      title: t("pm.fields.actions"),
      width: 90,
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          onClick={(event) => {
            event.stopPropagation();
            navigate(`/pm/requirements/${row.id}`);
          }}
        >
          {t("pm.actions.open")}
        </Button>
      ),
    },
  ];

  const taskColumns: TableColumnsType<PmTask> = [
    {
      title: t("pm.fields.id"),
      dataIndex: "public_id",
      width: 120,
      render: (value: string) => (
        <span style={{ fontFamily: "var(--fn-font-mono)", fontSize: 12 }}>{value}</span>
      ),
    },
    { title: t("pm.fields.stage"), dataIndex: "stage", width: 90 },
    {
      title: t("pm.fields.status"),
      dataIndex: "status",
      width: 120,
      render: (value: string) => <StatusTag status={value} />,
    },
    {
      title: t("pm.fields.owner"),
      dataIndex: "owner_user_id",
      width: 100,
      render: (value: number) => `#${value}`,
    },
    {
      title: t("pm.fields.updatedAt"),
      dataIndex: "updated_at",
      width: 170,
      render: (value: string | null) => formatTime(value),
    },
    {
      title: t("pm.fields.actions"),
      width: 120,
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          onClick={(event) => {
            event.stopPropagation();
            navigate(`/pm/tasks/${row.id}`);
          }}
        >
          {t("pm.actions.openTask")}
        </Button>
      ),
    },
  ];

  const hasWorkspace = workspaces.data.length > 0;
  const activeLoading =
    tab === "requirements" ? requirements.loading : tasks.loading;
  const activeError =
    tab === "requirements" ? requirements.error : tasks.error;

  const resetFilters = () => {
    setStatus("");
    setQuery("");
    setSearch("");
    setPage(1);
  };

  const filterBar = (
    <div className={styles.toolbar} style={{ marginBottom: 10 }}>
      <Select
        size="small"
        value={workspaceId ?? undefined}
        onChange={(value) => {
          setWorkspaceId(Number(value));
          resetFilters();
        }}
        options={workspaces.data.map((item) => ({
          value: item.id,
          label: item.name,
        }))}
        placeholder={t("pm.fields.workspace")}
        style={{ minWidth: 160 }}
        disabled={!hasWorkspace}
      />
      <Select
        size="small"
        value={status}
        onChange={(value) => {
          setStatus(value);
          setPage(1);
        }}
        options={[
          { value: "", label: t("pm.filters.allStatus") },
          ...(tab === "requirements" ? REQUIREMENT_STATUSES : TASK_STATUSES).map(
            (value) => ({
              value,
              label: t(`pm.status.${value}`, { defaultValue: value }),
            }),
          ),
        ]}
        style={{ minWidth: 130 }}
      />
      {tab === "requirements" ? (
        <Input.Search
          size="small"
          allowClear
          value={search}
          placeholder={t("pm.fields.search")}
          onChange={(event) => setSearch(event.target.value)}
          onSearch={(value) => {
            setQuery(value.trim());
            setPage(1);
          }}
          style={{ width: 240 }}
        />
      ) : (
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
      )}
      <span className={styles.toolbarSpacer} />
      <Button
        size="small"
        icon={<Gavel size={13} />}
        onClick={() => navigate("/pm/decisions")}
      >
        {t("pm.decision.center")}
      </Button>
      {tab === "requirements" ? (
        <Button
          size="small"
          type="primary"
          icon={<Plus size={13} />}
          disabled={!hasWorkspace}
          onClick={() => setNewOpen(true)}
        >
          {t("pm.actions.newRequirement")}
        </Button>
      ) : null}
    </div>
  );

  return (
    <PageShell title={t("pm.title")} subtitle={t("pm.subtitle")}>
      <div className={styles.page}>
        <HealthBar />

        {!hasWorkspace && !workspaces.loading ? (
          <Alert
            type="info"
            showIcon
            message={t("pm.workspace.none")}
            description={t("pm.workspace.noneHint")}
            action={<WorkspaceCreator onCreated={() => void workspaces.refresh()} />}
          />
        ) : null}

        {activeError ? (
          <Alert type="error" showIcon message={activeError} />
        ) : null}

        <Tabs
          activeKey={tab}
          onChange={(key) => {
            setTab(key);
            resetFilters();
          }}
          items={[
            {
              key: "requirements",
              label: t("pm.tabs.requirements"),
              children: (
                <>
                  {filterBar}
                  <Table<PmRequirement>
                    rowKey="id"
                    size="small"
                    loading={activeLoading}
                    columns={requirementColumns}
                    dataSource={requirements.data.items}
                    scroll={{ x: 860 }}
                    onRow={(row) => ({
                      onClick: () => navigate(`/pm/requirements/${row.id}`),
                      style: { cursor: "pointer" },
                    })}
                    locale={{
                      emptyText: (
                        <Empty
                          image={Empty.PRESENTED_IMAGE_SIMPLE}
                          description={t("pm.empty.requirements")}
                        />
                      ),
                    }}
                    pagination={{
                      current: page,
                      pageSize: PAGE_SIZE,
                      total: requirements.data.total,
                      showSizeChanger: false,
                      size: "small",
                      onChange: setPage,
                    }}
                  />
                </>
              ),
            },
            {
              key: "tasks",
              label: t("pm.tabs.tasks"),
              children: (
                <>
                  {filterBar}
                  <Table<PmTask>
                    rowKey="id"
                    size="small"
                    loading={activeLoading}
                    columns={taskColumns}
                    dataSource={tasks.data.items}
                    scroll={{ x: 760 }}
                    onRow={(row) => ({
                      onClick: () => navigate(`/pm/tasks/${row.id}`),
                      style: { cursor: "pointer" },
                    })}
                    locale={{
                      emptyText: (
                        <Empty
                          image={Empty.PRESENTED_IMAGE_SIMPLE}
                          description={t("pm.empty.tasks")}
                        />
                      ),
                    }}
                    pagination={{
                      current: page,
                      pageSize: PAGE_SIZE,
                      total: tasks.data.total,
                      showSizeChanger: false,
                      size: "small",
                      onChange: setPage,
                    }}
                  />
                </>
              ),
            },
          ]}
        />
      </div>

      <NewRequirementModal
        open={newOpen}
        workspaceId={workspaceId}
        onClose={() => setNewOpen(false)}
        onCreated={(requirement) => {
          void requirements.refresh();
          navigate(`/pm/requirements/${requirement.id}`);
        }}
      />
    </PageShell>
  );
}
