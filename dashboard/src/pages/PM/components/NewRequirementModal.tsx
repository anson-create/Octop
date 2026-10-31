import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Input, Modal, Spin, Tag } from "antd";
import { Link2, RefreshCw, Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmJiraIssue, PmJiraStatus, PmRequirement } from "../../../api/types";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";

interface NewRequirementModalProps {
  open: boolean;
  workspaceId: number | null;
  onClose: () => void;
  onCreated: (requirement: PmRequirement) => void;
}

/**
 * Intake is by reference, never by typing.
 *
 * There used to be two text boxes here (title + raw text). That made "jira" a
 * label on freehand prose: nothing checked that the text matched a real ticket,
 * and every downstream step inherited unverified input. Now the browser can only
 * name an issue; the server fetches it.
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

  const [issues, setIssues] = useState<PmJiraIssue[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");

  const [selected, setSelected] = useState<string | null>(null);
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

  useEffect(() => {
    if (!open) return;
    setSelected(null);
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
    if (!open || !status?.configured) return;
    void refreshIssues(query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, status?.configured, query, workspaceId]);

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
    if (!workspaceId || !selected) {
      message.warning(t("pm.newRequirement.selectFirst"));
      return;
    }
    setSubmitting(true);
    try {
      const created = await pmApi.createRequirement({
        workspace_id: workspaceId,
        issue_key: selected,
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
  const okDisabled = !workspaceId || !configured || !selected || submitting;

  return (
    <Modal
      open={open}
      title={t("pm.newRequirement.title")}
      onCancel={onClose}
      onOk={() => void submit()}
      okButtonProps={{ disabled: okDisabled, loading: submitting }}
      okText={t("pm.actions.create")}
      cancelText={t("pm.actions.cancel")}
      width={720}
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

            <div style={{ display: "flex", gap: 8, margin: "8px 0", alignItems: "center" }}>
              {status?.base_url ? <Tag color="blue">{status.base_url}</Tag> : null}
              <span style={{ fontSize: 11, opacity: 0.6 }}>{t("pm.jira.configured")}</span>
              <Button type="link" size="small" onClick={() => setShowConfig(true)}>
                {t("pm.jira.reconfigure")}
              </Button>
            </div>

            {listError ? <Alert type="error" showIcon message={listError} /> : null}

            <div
              data-testid="pm-jira-issues"
              style={{
                border: "1px solid rgba(0,0,0,0.08)",
                borderRadius: 6,
                maxHeight: 340,
                overflow: "auto",
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
                const active = issue.key === selected;
                return (
                  <button
                    key={issue.key}
                    type="button"
                    aria-pressed={active}
                    data-issue-key={issue.key}
                    onClick={() => setSelected(issue.key)}
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

            {selected ? (
              <div style={{ marginTop: 8, fontSize: 12 }}>
                {t("pm.newRequirement.selected", { key: selected })}
              </div>
            ) : null}
          </>
        )}
      </div>
    </Modal>
  );
}
