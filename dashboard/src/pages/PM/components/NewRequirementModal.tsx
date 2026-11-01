import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Divider, Input, Modal, Spin, Tag } from "antd";
import { Link2, RefreshCw, Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type {
  PmJiraIssue,
  PmJiraIssuePreview,
  PmJiraMeta,
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
 * There used to be two boxes here — a key field and, behind a toggle, a separate
 * search list. Both asked the same question ("which ticket?") at different
 * levels of precision, so the operator had to know which one to use, and a
 * mistyped key produced a 404 rather than "nothing matched". There is now one
 * fuzzy box: it opens on the design queue (mine, 待设计, P0/P1), a key matches
 * exactly *and* as text, a phrase matches the title and description.
 *
 * Nothing is created until a ticket has been read in full and shown back. A
 * typed key alone is never enough — that is the difference between "I typed
 * something" and "this ticket exists and here is what we will store".
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
  const [meta, setMeta] = useState<PmJiraMeta | null>(null);

  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  const [issues, setIssues] = useState<PmJiraIssue[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [listNote, setListNote] = useState("");

  const [selectedKey, setSelectedKey] = useState("");
  const [preview, setPreview] = useState<PmJiraIssuePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);

  const [showConfig, setShowConfig] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [jql, setJql] = useState("");
  const [verifyTls, setVerifyTls] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

  // Typing fires a request per keystroke otherwise, and out-of-order replies
  // paint the list with results for a prefix the input no longer shows.
  const listSeq = useRef(0);
  const previewSeq = useRef(0);

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

  /**
   * Statuses and priorities as this site spells them.
   *
   * The queue defaults to "待设计 / P0,P1", which is this product's guess about
   * someone else's vocabulary. If that guess is wrong the list still loads (the
   * server falls back), and this call supplies the names to say *why* — which is
   * the only version of that message an operator can act on.
   */
  const loadMeta = async () => {
    if (!workspaceId) return;
    try {
      setMeta(await pmApi.jiraMeta(workspaceId));
    } catch {
      // A missing catalogue must not stop intake; the fallback note still shows.
      setMeta(null);
    }
  };

  const refreshIssues = async (needle: string) => {
    if (!workspaceId) return;
    const seq = ++listSeq.current;
    setListLoading(true);
    setListError(null);
    try {
      const data = await pmApi.listJiraIssues(workspaceId, needle || undefined);
      if (seq !== listSeq.current) return;
      setIssues(data.items);
      setListNote(data.note || "");
    } catch (err) {
      if (seq !== listSeq.current) return;
      setIssues([]);
      setListNote("");
      setListError(apiErrorMessage(err, t("pm.newRequirement.loadFailed"), t));
    } finally {
      if (seq === listSeq.current) setListLoading(false);
    }
  };

  /**
   * Read one issue in full and show what intake will store.
   *
   * Every path — a row click, and a paste-enter on the search box — ends here,
   * so the confirm button only ever acts on a ticket that was actually read.
   */
  const readIssue = async (raw: string) => {
    const key = raw.trim().toUpperCase();
    if (!workspaceId || !key) return;
    const seq = ++previewSeq.current;
    setSelectedKey(key);
    setPreviewLoading(true);
    setPreviewError(null);
    setPreview(null);
    try {
      const data = await pmApi.getJiraIssue(workspaceId, key);
      if (seq !== previewSeq.current) return;
      setPreview(data);
    } catch (err) {
      if (seq !== previewSeq.current) return;
      setPreviewError(apiErrorMessage(err, t("pm.newRequirement.readFailed"), t));
    } finally {
      if (seq === previewSeq.current) setPreviewLoading(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    setSearch("");
    setQuery("");
    setSelectedKey("");
    setPreview(null);
    setPreviewError(null);
    setIssues([]);
    setListError(null);
    setListNote("");
    setMeta(null);
    setConfigError(null);
    setToken("");
    setReloadKey((value) => value + 1);
    void refreshStatus();
    void loadMeta();
    // Keyed on identity, never on `t` or on the function itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, workspaceId]);

  useEffect(() => {
    // Debounced so a title of twenty characters is one request, not twenty.
    if (!open || !status?.configured) return;
    if (search.trim() === query) return;
    const timer = window.setTimeout(() => setQuery(search.trim()), 400);
    return () => window.clearTimeout(timer);
  }, [search, open, status?.configured, query]);

  useEffect(() => {
    // The queue loads on open. It used to sit behind a "browse" toggle, which
    // meant the thing the operator came for was hidden by default.
    if (!open || !status?.configured) return;
    void refreshIssues(query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, status?.configured, query, reloadKey, workspaceId]);

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
      await loadMeta();
    } catch (err) {
      setConfigError(apiErrorMessage(err, t("pm.jira.connectFailed"), t));
    } finally {
      setConnecting(false);
    }
  };

  const submit = async () => {
    if (!workspaceId || !preview) {
      message.warning(t("pm.newRequirement.pickFirst"));
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

  // A mismatch the operator can act on: name the site's real statuses.
  const queueMismatch = Boolean(meta && !meta.error && !meta.ok);

  return (
    <Modal
      open={open}
      title={t("pm.newRequirement.title")}
      onCancel={onClose}
      onOk={() => void submit()}
      okButtonProps={{ disabled: okDisabled, loading: submitting }}
      okText={preview?.existing ? t("pm.newRequirement.openExisting") : t("pm.newRequirement.start")}
      cancelText={t("pm.actions.cancel")}
      width={780}
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
                placeholder={meta?.jql || "assignee = currentUser() AND status in (\"待设计\")"}
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
                onPressEnter={() => {
                  const needle = search.trim();
                  if (needle === query) setReloadKey((value) => value + 1);
                  else setQuery(needle);
                }}
                placeholder={t("pm.newRequirement.searchPlaceholder")}
                allowClear
                autoFocus
                prefix={<Search size={13} />}
                data-testid="pm-jira-search"
              />
              <Button
                size="small"
                icon={<RefreshCw size={13} />}
                loading={listLoading}
                onClick={() => setReloadKey((value) => value + 1)}
              />
            </div>

            <div
              style={{
                display: "flex",
                gap: 8,
                alignItems: "center",
                flexWrap: "wrap",
                margin: "8px 0",
                fontSize: 11,
                opacity: 0.65,
              }}
            >
              <span data-testid="pm-jira-queue-hint">
                {query ? t("pm.newRequirement.searching", { q: query }) : t("pm.newRequirement.queueHint")}
              </span>
              {meta?.user ? <Tag color="blue" style={{ fontSize: 10 }}>{meta.user}</Tag> : null}
              {status?.base_url ? <Tag style={{ fontSize: 10 }}>{status.base_url}</Tag> : null}
              <Button type="link" size="small" onClick={() => setShowConfig(true)}>
                {t("pm.jira.reconfigure")}
              </Button>
            </div>

            {queueMismatch ? (
              <div data-testid="pm-jira-queue-mismatch">
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message={t("pm.newRequirement.queueMismatch")}
                  description={t("pm.newRequirement.queueNames", {
                    statuses: meta?.statuses.slice(0, 8).join("、") || "-",
                    priorities: meta?.priorities.join("、") || "-",
                  })}
                />
              </div>
            ) : null}

            {listNote ? (
              <div data-testid="pm-jira-list-note">
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message={listNote}
                />
              </div>
            ) : null}

            {listError ? (
              <div data-testid="pm-jira-list-error">
                <Alert type="error" showIcon message={listError} />
              </div>
            ) : null}

            <div
              data-testid="pm-jira-issues"
              style={{
                border: "1px solid rgba(0,0,0,0.08)",
                borderRadius: 6,
                maxHeight: 210,
                overflow: "auto",
              }}
            >
              {listLoading && issues.length === 0 ? (
                <div style={{ padding: 16, textAlign: "center" }}>
                  <Spin size="small" />
                </div>
              ) : null}
              {!listLoading && issues.length === 0 && !listError ? (
                <div
                  data-testid="pm-jira-empty"
                  style={{ padding: 16, textAlign: "center", opacity: 0.6, fontSize: 12 }}
                >
                  {t("pm.newRequirement.empty")}
                </div>
              ) : null}
              {issues.map((item) => {
                const active = item.key === selectedKey;
                return (
                  <button
                    key={item.key}
                    type="button"
                    aria-pressed={active}
                    data-issue-key={item.key}
                    onClick={() => void readIssue(item.key)}
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
                      <code style={{ fontSize: 12 }}>{item.key}</code>
                      <span
                        style={{
                          fontSize: 12,
                          fontWeight: active ? 600 : 400,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {item.title}
                      </span>
                      {item.priority ? (
                        <Tag
                          color={/^p0$/i.test(item.priority) ? "red" : undefined}
                          style={{ marginLeft: "auto", fontSize: 10 }}
                        >
                          {item.priority}
                        </Tag>
                      ) : null}
                      {item.status ? <Tag style={{ fontSize: 10 }}>{item.status}</Tag> : null}
                    </div>
                    <div style={{ fontSize: 11, opacity: 0.6, marginTop: 2 }}>
                      {item.issue_type || "-"}
                      {item.assignee ? ` · ${item.assignee}` : ""}
                      {item.updated ? ` · ${item.updated.slice(0, 10)}` : ""}
                    </div>
                  </button>
                );
              })}
            </div>

            {previewError ? (
              <div data-testid="pm-jira-read-error" style={{ marginTop: 10 }}>
                <Alert type="error" showIcon message={previewError} />
              </div>
            ) : null}

            {previewLoading ? (
              <div style={{ padding: 12, textAlign: "center" }}>
                <Spin size="small" />
                <div style={{ fontSize: 12, opacity: 0.6, marginTop: 6 }}>
                  {t("pm.newRequirement.reading", { key: selectedKey })}
                </div>
              </div>
            ) : null}

            {preview ? (
              <div data-testid="pm-jira-preview" style={{ display: "grid", gap: 8, marginTop: 10 }}>
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
                      maxHeight: 200,
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
            ) : !previewLoading && !previewError ? (
              <Divider style={{ margin: "10px 0 0" }} />
            ) : null}
          </>
        )}
      </div>
    </Modal>
  );
}
