/**
 * PM's own thread bootstrap for the collaboration panel.
 *
 * It drives the *same* chat runtime as ``/chat`` (``useChat`` → chatStore → SSE
 * → ``MessageList``); the only thing it adds is a pinned thread per agent, so
 * the PM panel can never repoint (``rebind``) the user's preferred chat thread
 * at itself. The shared ``useAgentThreadChat`` hook is deliberately left
 * untouched: `/chat`, Remote Phone and Terminal AI all depend on its
 * pick-and-rebind behaviour, and PM has no business changing it.
 */
import { useCallback, useEffect, useState } from "react";
import { octopThreadsApi } from "../../api/modules/octopThreads";
import { useChat } from "../Chat/hooks/useChat";

const THREAD_KEY_PREFIX = "pm.collab.thread.";

export function collabThreadKey(agentId: string): string {
  return `${THREAD_KEY_PREFIX}${agentId}`;
}

export interface PMChatState {
  threadId: string | null;
  booting: boolean;
  error: string | null;
  messages: ReturnType<typeof useChat>["messages"];
  isStreaming: boolean;
  send: (text: string) => void;
  cancelStream: () => void;
  startNewThread: () => void;
}

export function usePMChat(agentId: string | null): PMChatState {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [booting, setBooting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { messages, isStreaming, sendMessage, cancelStream, loadHistory } =
    useChat(threadId, agentId);

  const createThread = useCallback(
    async (agent: string, replace: boolean) => {
      const key = collabThreadKey(agent);
      if (replace) window.localStorage.removeItem(key);
      const created = await octopThreadsApi.create(agent);
      window.localStorage.setItem(key, created.thread_id);
      return created.thread_id;
    },
    [],
  );

  useEffect(() => {
    if (!agentId) {
      setThreadId(null);
      setError(null);
      return;
    }
    const stored = window.localStorage.getItem(collabThreadKey(agentId));
    if (stored) {
      setThreadId(stored);
      setError(null);
      return;
    }
    let cancelled = false;
    setBooting(true);
    void createThread(agentId, false)
      .then((id) => {
        if (!cancelled) {
          setThreadId(id);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setBooting(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, createThread]);

  useEffect(() => {
    if (threadId && agentId) void loadHistory(threadId);
  }, [threadId, agentId, loadHistory]);

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!agentId || !threadId || !trimmed) return;
      sendMessage(trimmed, "", agentId, undefined, threadId);
    },
    [agentId, sendMessage, threadId],
  );

  const startNewThread = useCallback(() => {
    if (!agentId) return;
    setBooting(true);
    setError(null);
    void createThread(agentId, true)
      .then((id) => setThreadId(id))
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setBooting(false));
  }, [agentId, createThread]);

  return {
    threadId,
    booting,
    error,
    messages,
    isStreaming,
    send,
    cancelStream,
    startNewThread,
  };
}
