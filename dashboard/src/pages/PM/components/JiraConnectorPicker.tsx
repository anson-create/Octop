import { useEffect, useState } from "react";
import { Alert, Button, Spin, Tag } from "antd";
import { CheckCircle2, PlugZap, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { pmApi } from "../../../api/modules/pm";
import type { PmJiraConnector, PmJiraStatus } from "../../../api/types";
import { apiErrorMessage } from "../../../utils/apiError";
import { message } from "../../../utils/antdMessage";

interface JiraConnectorPickerProps {
  workspaceId: number | null;
  status: PmJiraStatus | null;
  onBound: (status: PmJiraStatus) => void | Promise<void>;
  /** Fall back to the older "site URL + token" form. */
  onUseRest: () => void;
  /**
   * Where "install one first" sends the operator. Passed in rather than read
   * from a router hook so the picker stays renderable outside a `<Router>` —
   * which is how it ends up tested.
   */
  onOpenConnectors?: () => void;
}

function keyOf(item: PmJiraConnector): string {
  return `${item.instance_id}:${item.server_key}`;
}

/**
 * Pick the connector that already talks to Jira, instead of pasting a token.
 *
 * The credential is the point: the operator configured it once when installing
 * the connector, and binding stores only *which* connector to ask. Rotating the
 * token there rotates it here, and a database dump contains no secret to leak.
 *
 * Only connectors carrying a Jira server are offered — the server filters the
 * list, because picking a mail connector fails later with "unknown tool", a
 * message that points at the tool rather than at the choice that caused it.
 */
export default function JiraConnectorPicker({
  workspaceId,
  status,
  onBound,
  onUseRest,
  onOpenConnectors,
}: JiraConnectorPickerProps) {
  const { t } = useTranslation();

  const [items, setItems] = useState<PmJiraConnector[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [picked, setPicked] = useState("");
  const [binding, setBinding] = useState(false);
  const [bindError, setBindError] = useState<string | null>(null);

  useEffect(() => {
    if (!workspaceId) return;
    let alive = true;
    setLoading(true);
    setError(null);
    pmApi
      .jiraConnectors()
      .then((data) => {
        if (!alive) return;
        setItems(data.items);
        // Pre-select what is already bound, so "which one am I on" is answered
        // before the operator has to read the list.
        const current = data.items.find(
          (item) =>
            item.instance_id === status?.connector_instance_id &&
            (!status?.connector_server || item.server_key === status.connector_server),
        );
        setPicked(current ? keyOf(current) : "");
      })
      .catch((err) => {
        if (!alive) return;
        setItems([]);
        setError(apiErrorMessage(err, t("pm.jira.connectorLoadFailed"), t));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // `status` is read for the pre-selection only; re-running on every status
    // object identity change would refetch the list on each keystroke upstream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, reloadKey]);

  const bind = async () => {
    if (!workspaceId || !picked) return;
    const [instanceId, serverKey] = picked.split(":");
    setBinding(true);
    setBindError(null);
    try {
      const next = await pmApi.bindJiraConnector(workspaceId, {
        connector_instance_id: instanceId,
        server_key: serverKey || undefined,
      });
      message.success(t("pm.jira.connectorBound"));
      await onBound(next);
    } catch (err) {
      setBindError(apiErrorMessage(err, t("pm.jira.connectorBindFailed"), t));
    } finally {
      setBinding(false);
    }
  };

  if (loading && items.length === 0) {
    return (
      <div style={{ padding: 16, textAlign: "center" }}>
        <Spin size="small" />
      </div>
    );
  }

  const boundKey =
    status?.transport === "mcp" && status.connector_instance_id
      ? `${status.connector_instance_id}:${status.connector_server || ""}`
      : "";

  return (
    <div data-testid="pm-jira-connectors" style={{ display: "grid", gap: 10 }}>
      {status?.transport === "mcp" && status.connector_display ? (
        <Alert
          type="success"
          showIcon
          message={t("pm.jira.usingConnector", { name: status.connector_display })}
        />
      ) : null}

      {error ? <Alert type="error" showIcon message={error} /> : null}

      {!error && items.length === 0 ? (
        <div data-testid="pm-jira-connectors-empty">
          <Alert
            type="warning"
            showIcon
            message={t("pm.jira.noConnector")}
            description={t("pm.jira.noConnectorHint")}
            action={
              <Button size="small" onClick={() => onOpenConnectors?.()}>
                {t("pm.jira.openConnectors")}
              </Button>
            }
          />
        </div>
      ) : null}

      {items.map((item) => {
        const key = keyOf(item);
        const active = key === picked;
        const isBound = key === boundKey;
        return (
          <button
            key={key}
            type="button"
            aria-pressed={active}
            data-connector-key={key}
            onClick={() => setPicked(key)}
            style={{
              display: "grid",
              gap: 3,
              textAlign: "left",
              padding: "9px 11px",
              borderRadius: 6,
              border: active
                ? "1px solid rgba(22,119,255,0.65)"
                : "1px solid rgba(0,0,0,0.10)",
              background: active ? "rgba(22,119,255,0.06)" : "transparent",
              cursor: "pointer",
            }}
          >
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <PlugZap size={13} />
              <span style={{ fontSize: 13, fontWeight: active ? 600 : 400 }}>
                {item.display_name || item.instance_id}
              </span>
              {isBound ? (
                <Tag color="green" style={{ fontSize: 10, marginLeft: "auto" }}>
                  <CheckCircle2 size={10} style={{ marginRight: 3 }} />
                  {t("pm.jira.currentConnector")}
                </Tag>
              ) : null}
            </div>
            <div style={{ fontSize: 11, opacity: 0.65 }}>
              {[
                item.server_display || item.server_key,
                item.base_url,
                item.username,
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
          </button>
        );
      })}

      {bindError ? <Alert type="error" showIcon message={bindError} /> : null}

      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Button
          type="primary"
          size="small"
          icon={<PlugZap size={13} />}
          loading={binding}
          disabled={!picked || picked === boundKey}
          onClick={() => void bind()}
          data-testid="pm-jira-bind"
        >
          {t("pm.jira.bindConnector")}
        </Button>
        <Button
          size="small"
          icon={<RefreshCw size={13} />}
          onClick={() => setReloadKey((value) => value + 1)}
        >
          {t("pm.actions.refresh")}
        </Button>
        <Button type="link" size="small" onClick={onUseRest}>
          {t("pm.jira.useRest")}
        </Button>
      </div>

      <div style={{ fontSize: 11, opacity: 0.55 }}>{t("pm.jira.connectorHint")}</div>
    </div>
  );
}
