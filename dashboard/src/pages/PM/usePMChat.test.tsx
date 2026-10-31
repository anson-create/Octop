import { renderHook, waitFor } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  create: vi.fn(() => Promise.resolve({ thread_id: "t-new" })),
  sendMessage: vi.fn(),
  cancelStream: vi.fn(),
  loadHistory: vi.fn(() => Promise.resolve()),
  useChatArgs: [] as Array<[string | null, string | null]>,
}));

vi.mock("../../api/modules/octopThreads", () => ({
  octopThreadsApi: { create: h.create },
}));

vi.mock("../Chat/hooks/useChat", () => ({
  useChat: (threadId: string | null, agentId: string | null) => {
    h.useChatArgs.push([threadId, agentId]);
    return {
      messages: [],
      isStreaming: false,
      sendMessage: h.sendMessage,
      cancelStream: h.cancelStream,
      loadHistory: h.loadHistory,
    };
  },
}));

import { collabThreadKey, usePMChat } from "./usePMChat";

beforeEach(() => {
  window.localStorage.clear();
  h.create.mockClear();
  h.sendMessage.mockClear();
  h.loadHistory.mockClear();
  h.useChatArgs.length = 0;
});

describe("usePMChat", () => {
  it("reuses the stored thread and never creates or rebinds another", async () => {
    window.localStorage.setItem(collabThreadKey("a1"), "t-old");
    const { result } = renderHook(() => usePMChat("a1"));

    await waitFor(() => expect(result.current.threadId).toBe("t-old"));
    expect(h.create).not.toHaveBeenCalled();
    // Rebinding is what would hijack /chat's preferred thread — it must not happen.
    expect(h.sendMessage).not.toHaveBeenCalled();
  });

  it("creates and stores a thread when this agent has none", async () => {
    const { result } = renderHook(() => usePMChat("a1"));

    await waitFor(() => expect(result.current.threadId).toBe("t-new"));
    expect(h.create).toHaveBeenCalledWith("a1");
    expect(window.localStorage.getItem(collabThreadKey("a1"))).toBe("t-new");
  });

  it("sends through the shared chat runtime with this panel's thread", async () => {
    window.localStorage.setItem(collabThreadKey("a1"), "t-old");
    const { result } = renderHook(() => usePMChat("a1"));
    await waitFor(() => expect(result.current.threadId).toBe("t-old"));

    act(() => result.current.send("  hello  "));
    expect(h.sendMessage).toHaveBeenCalledWith("hello", "", "a1", undefined, "t-old");
  });

  it("stays idle without an agent", async () => {
    const { result } = renderHook(() => usePMChat(null));
    expect(result.current.threadId).toBeNull();
    expect(h.create).not.toHaveBeenCalled();
  });

  it("replaces the stored thread on demand", async () => {
    window.localStorage.setItem(collabThreadKey("a1"), "t-old");
    h.create.mockResolvedValueOnce({ thread_id: "t-fresh" });
    const { result } = renderHook(() => usePMChat("a1"));
    await waitFor(() => expect(result.current.threadId).toBe("t-old"));

    act(() => result.current.startNewThread());
    await waitFor(() => expect(result.current.threadId).toBe("t-fresh"));
    expect(window.localStorage.getItem(collabThreadKey("a1"))).toBe("t-fresh");
  });
});
