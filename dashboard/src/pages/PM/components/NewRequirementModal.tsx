import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Divider, Input, Modal, Spin, Tag } from "antd";
import { FileSearch, Link2, RefreshCw, Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type {
  PmJiraIssue,
  PmJiraIssuePreview,
  PmJiraStatus,
  PmRequirement,
} from "../../../api/types";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";

interface NewRequirementModalProps {
  open: boolean;
  workspaceId: number | null;
  onClose: () => void;
  onCreated: (requirement: PmRequirement) => void;
}

/**
 * Intake is by reference, never by typing prose.
 *
 * There used to be two text boxes here (title + raw text). That made "jira" a
 * label on freehand prose: nothing checked that the text matched a real ticket,
 * and every downstream step inherited unverified input.
 *
 * The ticket *number*, though, is the fastest way in — the operator already has
 * it open in Jira. So the field at the top takes a key, reads the issue in full
 * (description, custom fields, comments, attachments) and shows exactly what
 * will be stored before anything is created. The list below is a convenience for
 * "what is assigned to me", not the only way in.
 */
export default function NewRequirementModal({
  open,
  workspaceId,
  onClose,
  onCreated,
}: NewRequirementModalProps) {
  const { t } = useTranslation();

  const [status, setStatus] = useState<PmJiraStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);

  const [issueKey, setIssueKey] = useState("");
  const [preview, setPreview] = useState<PmJiraIssuePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [showList, setShowList] = useState(false);
  const [issues, setIssues] = useState<PmJiraIssue[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");

  const [submitting, setSubmitting] = useState(false);

  const [showConfig, setShowConfig] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [jql, setJql] = useState("");
  const [verifyTls, setVerifyTls] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

  // Plain functions, not useCallback: effects below key on ids only. A callback
  // that depends on `t` gets a fresh identity whenever i18next re-renders, which
  // turns "load status" into an infinite request loop.
  const refreshStatus = async () => {
    if (!workspaceId) return;
    setStatusLoading(true);
    try {
      const next = await pmApi.jiraStatus(workspaceId);
      setStatus(next);
      setBaseUrl(next.base_url || "");
      setJql(next.jql || "");
      setVerifyTls(next.verify_tls);
      setShowConfig(!next.configured);
    } catch (err) {
      setConfigError(apiErrorMessage(err, t("pm.newRequirement.loadFailed"), t));
    } finally {
      setStatusLoading(false);
    }
  };

  const refreshIssues = async (needle: string) => {
    if (!workspaceId) return;
    setListLoading(true);
    setListError(null);
    try {
      const data = await pmApi.listJiraIssues(workspaceId, needle || undefined);
      setIssues(data.items);
    } catch (err) {
      setIssues([]);
      setListError(apiErrorMessage(err, t("pm.newRequirement.loadFailed"), t));
    } finally {
      setListLoading(false);
    }
  };

  /**
   * Read one issue and show what intake will store.
   *
   * Both entry points funnel through here — typing a key and clicking a row —
   * so the confirm button always acts on a ticket that was actually read, never
   * on a row that merely exists in a list.
   */
  const readIssue = async (raw: string) => {
    const key = raw.trim().toUpperCase();
    if (!workspaceId || !key) return;
    setPreviewLoading(true);
    setPreviewError(null);
    setPreview(null);
    try {
      const data = await pmApi.getJiraIssue(workspaceId, key);
      setPreview(data);
      setIssueKey(data.key);
    } catch (err) {
      setPreviewError(apiErrorMessage(err, t("pm.newRequirement.readFailed"), t));
    } finally {
      setPreviewLoading(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    setIssueKey("");
    setPreview(null);
    setPreviewError(null);
    setShowList(false);
    setSearch("");
    setQuery("");
    setIssues([]);
    setListError(null);
    setConfigError(null);
    setToken("");
    void refreshStatus();
    // Keyed on identity, never on `t` or on the function itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, workspaceId]);

  useEffect(() => {
    // Only when the operator opens the list. Loading it up front made the dialog
    // look like a picker with a search box, which is the behaviour being fixed.
    if (!open || !status?.configured || !showList) return;
    void refreshIssues(query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, status?.configured, showList, query, workspaceId]);

  const connect = async () => {
    if (!workspaceId) return;
    setConnecting(true);
    setConfigError(null);
    try {
      await pmApi.saveJiraConfig(workspaceId, {
        base_url: baseUrl.trim(),
        token: token.trim() || undefined,
        jql: jql.trim() || undefined,
        verify_tls: verifyTls,
      });
      const probe = await pmApi.probeJira(workspaceId);
      message.success(t("pm.jira.connected", { user: probe.user || probe.account || "-" }));
      await refreshStatus();
    } catch (err) {
      setConfigError(apiErrorMessage(err, t("pm.jira.connectFailed"), t));
    } finally {
      setConnecting(false);
    }
  };

  const submit = async () => {
    if (!workspaceId || !preview) {
      message.warning(t("pm.newRequirement.readFirst"));
      return;
    }
    setSubmitting(true);
    try {
      const created = await pmApi.createRequirement({
        workspace_id: workspaceId,
        issue_key: preview.key,
      });
      message.success(t("pm.newRequirement.created"));
      onCreated(created);
      onClose();
    } catch (err) {
      message.error(apiErrorMessage(err, t("pm.newRequirement.loadFailed"), t));
    } finally {
      setSubmitting(false);
    }
  };

  const configured = Boolean(status?.configured);
  const okDisabled = !workspaceId || !configured || !preview || submitting;

  const issue = preview?.issue;
  const components = issue?.components ?? [];
  const comments = issue?.comments ?? [];
  const attachments = issue?.attachments ?? [];

  return (
    <Modal
      open={open}
      title={t("pm.newRequirement.title")}
      onCancel={onClose}
      onOk={() => void submit()}
      okButtonProps={{ disabled: okDisabled, loading: submitting }}
      okText={preview?.existing ? t("pm.newRequirement.openExisting") : t("pm.actions.create")}
      cancelText={t("pm.actions.cancel")}
      width={760}
      destroyOnClose
    >
      <div style={{ marginTop: 4 }}>
        <div style={{ fontSize: 12, opacity: 0.7, marginBottom: 12 }}>
          {t("pm.newRequirement.subtitle")}
        </div>

        {statusLoading ? <Spin size="small" /> : null}

        {showConfig ? (
          <div data-testid="pm-jira-config" style={{ display: "grid", gap: 10 }}>
            <Alert type="info" showIcon message={t("pm.jira.intro")} />
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 12 }}>{t("pm.jira.baseUrl")}</span>
              <Input
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder="https://jira.example.com"
                autoComplete="off"
              />
              <span style={{ fontSize: 11, opacity: 0.6 }}>{t("pm.jira.baseUrlHint")}</span>
            </label>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 12 }}>{t("pm.jira.token")}</span>
              <Input.Password
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder={status?.token_set ? t("pm.jira.tokenKeep") : t("pm.jira.tokenHint")}
                autoComplete="new-password"
              />
              <span style={{ fontSize: 11, opacity: 0.6 }}>{t("pm.jira.tokenHint")}</span>
            </label>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 12 }}>{t("pm.jira.jql")}</span>
              <Input
                value={jql}
                onChange={(event) => setJql(event.target.value)}
                placeholder="assignee = currentUser() AND resolution = Unresolved"
              />
              <span style={{ fontSize: 11, opacity: 0.6 }}>{t("pm.jira.jqlHint")}</span>
            </label>
            <Checkbox
              checked={verifyTls}
              onChange={(event) => setVerifyTls(event.target.checked)}
            >
              {t("pm.jira.verifyTls")}
            </Checkbox>
            {configError ? <Alert type="error" showIcon message={configError} /> : null}
            <div>
              <Button
                type="primary"
                size="small"
                icon={<Link2 size={13} />}
                loading={connecting}
                disabled={!baseUrl.trim() || (!token.trim() && !status?.token_set)}
                onClick={() => void connect()}
              >
                {t("pm.jira.connect")}
              </Button>
              {configured ? (
                <Button size="small" style={{ marginLeft: 8 }} onClick={() => setShowConfig(false)}>
                  {t("pm.actions.cancel")}
                </Button>
              ) : null}
            </div>
          </div>
        ) : (
          <>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Input
                value={issueKey}
                onChange={(event) => setIssueKey(event.target.value)}
                onPressEnter={() => void readIssue(issueKey)}
                placeholder={t("pm.newRequirement.keyPlaceholder")}
                allowClear
                autoFocus
                data-testid="pm-jira-key-input"
              />
              <Button
                type="primary"
                size="small"
                icon={<FileSearch size={13} />}
                loading={previewLoading}
                disabled={!issueKey.trim()}
                onClick={() => void readIssue(issueKey)}
              >
                {t("pm.newRequirement.read")}
              </Button>
            </div>

            <div style={{ display: "flex", gap: 8, margin: "8px 0", alignItems: "center" }}>
              {status?.base_url ? <Tag color="blue">{status.base_url}</Tag> : null}
              <span style={{ fontSize: 11, opacity: 0.6 }}>{t("pm.jira.configured")}</span>
              <Button type="link" size="small" onClick={() => setShowConfig(true)}>
                {t("pm.jira.reconfigure")}
              </Button>
            </div>

            {previewError ? (
              <div data-testid="pm-jira-read-error">
                <Alert type="error" showIcon message={previewError} />
              </div>
            ) : null}

            {previewLoading ? (
              <div style={{ padding: 16, textAlign: "center" }}>
                <Spin size="small" />
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 6 }}>
                  {t("pm.newRequirement.reading", { key: issueKey.trim().toUpperCase() })}
                </div>
              </div>
            ) : null}

            {preview ? (
              <div data-testid="pm-jira-preview" style={{ display: "grid", gap: 8 }}>
                {preview.existing ? (
                  <Alert
                    type="info"
                    showIcon
                    message={t("pm.newRequirement.existing")}
                    // The identifier is rendered as its own node: a translated
                    // string with an interpolated list wraps badly once the list
                    // is longer than a line.
                    description={`${preview.existing.public_id} · ${preview.existing.title}`}
                  />
                ) : null}

                <div
                  style={{
                    border: "1px solid rgba(0,0,0,0.08)",
                    borderRadius: 6,
                    padding: "10px 12px",
                    display: "grid",
                    gap: 6,
                  }}
                >
                  <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                    <code style={{ fontSize: 12 }}>{preview.key}</code>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>{issue?.title}</span>
                    {issue?.status ? <Tag style={{ fontSize: 10 }}>{issue.status}</Tag> : null}
                    {issue?.issue_type ? <Tag style={{ fontSize: 10 }}>{issue.issue_type}</Tag> : null}
                    {issue?.priority ? <Tag style={{ fontSize: 10 }}>{issue.priority}</Tag> : null}
                  </div>

                  <div style={{ fontSize: 11, opacity: 0.6 }}>
                    {[
                      issue?.reporter ? t("pm.newRequirement.reporter", { name: issue.reporter }) : "",
                      issue?.assignee ? t("pm.newRequirement.assignee", { name: issue.assignee }) : "",
                      issue?.updated ? t("pm.newRequirement.updated", { at: issue.updated.slice(0, 10) }) : "",
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>

                  {components.length || attachments.length || comments.length ? (
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                      {components.map((name) => (
                        <Tag key={name} color="geekblue" style={{ fontSize: 10 }}>
                          {name}
                        </Tag>
                      ))}
                      {comments.length ? (
                        <span style={{ fontSize: 11, opacity: 0.6 }}>
                          {t("pm.newRequirement.commentCount", { count: comments.length })}
                        </span>
                      ) : null}
                      {attachments.length ? (
                        <span style={{ fontSize: 11, opacity: 0.6 }}>
                          {t("pm.newRequirement.attachmentCount", { count: attachments.length })}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                </div>

                {preview.blockers.length ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={t("pm.newRequirement.blockers")}
                    description={preview.blockers.join("；")}
                  />
                ) : null}

                {preview.truncated.length ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={t("pm.newRequirement.truncated")}
                    description={preview.truncated.join("；")}
                  />
                ) : null}

                <div>
                  <div style={{ fontSize: 12, marginBottom: 4 }}>
                    {t("pm.newRequirement.previewTitle")}
                    <span style={{ opacity: 0.5, marginLeft: 6 }}>
                      {t("pm.newRequirement.chars", { count: preview.requirement_text.length })}
                    </span>
                  </div>
                  <div
                    data-testid="pm-jira-preview-text"
                    style={{
                      whiteSpace: "pre-wrap",
                      fontSize: 12,
                      lineHeight: 1.6,
                      maxHeight: 240,
                      overflow: "auto",
                      background: "rgba(0,0,0,0.02)",
                      border: "1px solid rgba(0,0,0,0.06)",
                      borderRadius: 6,
                      padding: "8px 10px",
                    }}
                  >
                    {preview.requirement_text}
                  </div>
                  <div style={{ fontSize: 11, opacity: 0.55, marginTop: 4 }}>
                    {t("pm.newRequirement.sections", {
                      list: preview.sections.join("、"),
                    })}
                  </div>
                </div>
              </div>
            ) : null}

            <Divider style={{ margin: "12px 0 8px" }} />

            <Button
              type="link"
              size="small"
              style={{ paddingLeft: 0 }}
              onClick={() => setShowList((value) => !value)}
            >
              {showList ? t("pm.newRequirement.browseHide") : t("pm.newRequirement.browse")}
            </Button>

            {showList ? (
              <>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
                  <Input
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    onPressEnter={() => setQuery(search.trim())}
                    placeholder={t("pm.newRequirement.searchPlaceholder")}
                    allowClear
                    prefix={<Search size={13} />}
                  />
                  <Button size="small" onClick={() => setQuery(search.trim())}>
                    {t("pm.newRequirement.search")}
                  </Button>
                  <Button
                    size="small"
                    icon={<RefreshCw size={13} />}
                    loading={listLoading}
                    onClick={() => void refreshIssues(query)}
                  />
                </div>

                {listError ? <Alert type="error" showIcon message={listError} /> : null}

                <div
                  data-testid="pm-jira-issues"
                  style={{
                    border: "1px solid rgba(0,0,0,0.08)",
                    borderRadius: 6,
                    maxHeight: 240,
                    overflow: "auto",
                    marginTop: 8,
                  }}
                >
                  {listLoading && issues.length === 0 ? (
                    <div style={{ padding: 16, textAlign: "center" }}>
                      <Spin size="small" />
                    </div>
                  ) : null}
                  {!listLoading && issues.length === 0 && !listError ? (
                    <div style={{ padding: 16, textAlign: "center", opacity: 0.6, fontSize: 12 }}>
                      {t("pm.newRequirement.empty")}
                    </div>
                  ) : null}
                  {issues.map((issue) => {
                    const active = issue.key === preview?.key;
                    return (
                      <button
                        key={issue.key}
                        type="button"
                        aria-pressed={active}
                        data-issue-key={issue.key}
                        onClick={() => void readIssue(issue.key)}
                        style={{
                          display: "block",
                          width: "100%",
                          textAlign: "left",
                          padding: "8px 10px",
                          border: "none",
                          borderBottom: "1px solid rgba(0,0,0,0.06)",
                          background: active ? "rgba(22,119,255,0.10)" : "transparent",
                          cursor: "pointer",
                        }}
                      >
                        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                          <code style={{ fontSize: 12 }}>{issue.key}</code>
                          <span style={{ fontSize: 12, fontWeight: active ? 600 : 400 }}>
                            {issue.title}
                          </span>
                          {issue.status ? (
                            <Tag style={{ marginLeft: "auto", fontSize: 10 }}>{issue.status}</Tag>
                          ) : null}
                        </div>
                        <div style={{ fontSize: 11, opacity: 0.6, marginTop: 2 }}>
                          {issue.issue_type || "-"}
                          {issue.assignee ? ` · ${issue.assignee}` : ""}
                          {issue.priority ? ` · ${issue.priority}` : ""}
                          {issue.updated ? ` · ${issue.updated}` : ""}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </>
            ) : null}
          </>
        )}
      </div>
    </Modal>
  );
}
