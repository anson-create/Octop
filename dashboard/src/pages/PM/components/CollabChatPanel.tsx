import { useCallback, useRef, useState } from "react";
import { Button, Spin, Tooltip } from "antd";
import { MessageSquarePlus, Send, Square } from "lucide-react";
import { useTranslation } from "react-i18next";
import AgentSelector from "../../../components/AgentSelector";
import { useAgent } from "../../../context/AgentContext";
import MessageList from "../../Chat/components/MessageList";
import chatStyles from "../../Chat/index.module.less";
import { usePMChat } from "../usePMChat";
import styles from "../pm.module.less";

/**
 * Collaboration chat for a task.
 *
 * Reuse, not a second chat: ``usePMChat`` drives the same chatStore/SSE runtime
 * as ``/chat`` and ``MessageList`` is the same renderer (thinking, tool cards,
 * citations, code blocks). Only the thread scoping and the one-line task
 * context are PM-specific. It never rebinds the user's preferred chat thread,
 * so opening this panel cannot change what ``/chat`` shows.
 */

export interface TaskContextArgs {
  taskPublicId: string | null;
  stepLabel: string | null;
  pendingCards: number;
}

/** The exact line that gets prepended — shown in the UI, never hidden. */
export function buildContextLine(
  template: string,
  { taskPublicId, stepLabel, pendingCards }: TaskContextArgs,
): string {
  return template
    .replace("{{task}}", taskPublicId ?? "—")
    .replace("{{step}}", stepLabel ?? "—")
    .replace("{{cards}}", String(pendingCards));
}

interface CollabChatPanelProps extends TaskContextArgs {
  className?: string;
  /**
   * Stretch to the parent's height. The standalone panel carries a 420px
   * floor so it never collapses in a scrolling column; inside the split
   * workspace it shares height with the decision zone instead.
   */
  fill?: boolean;
}

export default function CollabChatPanel({
  taskPublicId,
  stepLabel,
  pendingCards,
  className,
  fill = false,
}: CollabChatPanelProps) {
  const { t } = useTranslation();
  const { activeAgent } = useAgent();
  const agentId = activeAgent?.agent_id ?? null;

  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const imeComposingRef = useRef(false);

  const {
    threadId,
    booting,
    error,
    messages,
    isStreaming,
    send,
    cancelStream,
    startNewThread,
  } = usePMChat(agentId);

  const template = t("pm.collab.contextLine", {
    defaultValue: "【PM 上下文】任务 {{task}} · 环节 {{step}} · 待拍板 {{cards}} 张",
  });
  const contextLine = buildContextLine(template, {
    taskPublicId,
    stepLabel,
    pendingCards,
  });

  const handleSend = useCallback(() => {
    const text = input.trim();
    if (!text || isStreaming || !agentId) return;
    setInput("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    send(`${contextLine}\n\n${text}`);
  }, [agentId, contextLine, input, isStreaming, send]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key !== "Enter" || e.shiftKey) return;
      if (imeComposingRef.current || e.nativeEvent.isComposing) return;
      e.preventDefault();
      handleSend();
    },
    [handleSend],
  );

  return (
    <div
      className={[
        styles.chatPanel,
        fill ? styles.chatPanelFill : "",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className={styles.chatHead}>
        <span>{t("pm.collab.title")}</span>
        <Tooltip title={t("pm.collab.newThread")}>
          <Button
            size="small"
            type="text"
            aria-label={t("pm.collab.newThread")}
            icon={<MessageSquarePlus size={13} />}
            disabled={!agentId || booting || isStreaming}
            onClick={startNewThread}
          />
        </Tooltip>
      </div>

      <div className={styles.chatContext} title={contextLine}>
        {contextLine}
      </div>

      {!agentId ? (
        <div className={styles.chatBlank}>
          <div>{t("pm.collab.noAgent")}</div>
          <div className={styles.chatBlankHint}>{t("pm.collab.noAgentHint")}</div>
          <div style={{ marginTop: 10 }}>
            <AgentSelector variant="select" showLabel={false} />
          </div>
        </div>
      ) : (
        <>
          <div
            className={[
              styles.chatBody,
              fill ? styles.chatBodyFill : "",
              chatStyles.messageListWrapper,
            ]
              .filter(Boolean)
              .join(" ")}
          >
            {booting ? (
              <div className={styles.chatBlank}>
                <Spin size="small" />
              </div>
            ) : error ? (
              <div className={styles.chatError}>{error}</div>
            ) : (
              <MessageList
                messages={messages}
                isStreaming={isStreaming}
                sessionKey={threadId ?? undefined}
              />
            )}
          </div>

          <div className={styles.chatComposer}>
            <textarea
              ref={textareaRef}
              className={styles.chatTextarea}
              value={input}
              onCompositionStart={() => {
                imeComposingRef.current = true;
              }}
              onCompositionEnd={() => {
                imeComposingRef.current = false;
              }}
              onChange={(e) => {
                setInput(e.target.value);
                const el = e.currentTarget;
                el.style.height = "auto";
                el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
              }}
              onKeyDown={handleKeyDown}
              placeholder={t("pm.collab.placeholder")}
              rows={2}
              disabled={booting}
            />
            <button
              type="button"
              className={styles.chatSend}
              disabled={booting || (!isStreaming && !input.trim())}
              onClick={isStreaming ? cancelStream : handleSend}
              title={isStreaming ? t("pm.collab.stop") : t("pm.collab.send")}
            >
              {isStreaming ? <Square size={14} /> : <Send size={14} />}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
