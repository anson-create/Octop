import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  agent: { current: { agent_id: "a1" } as { agent_id: string } | null },
  send: vi.fn(),
  cancelStream: vi.fn(),
  startNewThread: vi.fn(),
  chat: { current: null as Record<string, unknown> | null },
}));

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>();
  return {
    ...actual,
    useTranslation: () => ({
      t: (key: string, fallback?: unknown): string => {
        if (typeof fallback === "string") return fallback;
        if (fallback && typeof fallback === "object") {
          const opts = fallback as { defaultValue?: string };
          return opts.defaultValue ?? key;
        }
        return key;
      },
    }),
  };
});

vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => ({ activeAgent: h.agent.current }),
}));

vi.mock("../usePMChat", () => ({
  usePMChat: () => h.chat.current,
}));

vi.mock("../../../components/AgentSelector", () => ({
  default: () => <div data-testid="agent-selector" />,
}));

vi.mock("../../Chat/components/MessageList", () => ({
  default: () => <div data-testid="message-list" />,
}));

import CollabChatPanel, { buildContextLine } from "./CollabChatPanel";

const TEMPLATE = "【PM 上下文】任务 {{task}} · 环节 {{step}} · 待拍板 {{cards}} 张";

beforeEach(() => {
  h.agent.current = { agent_id: "a1" };
  h.send.mockClear();
  h.startNewThread.mockClear();
  h.chat.current = {
    threadId: "t-1",
    booting: false,
    error: null,
    messages: [],
    isStreaming: false,
    send: h.send,
    cancelStream: h.cancelStream,
    startNewThread: h.startNewThread,
  };
});

describe("buildContextLine", () => {
  it("fills all three slots", () => {
    expect(
      buildContextLine(TEMPLATE, {
        taskPublicId: "TASK-9",
        stepLabel: "需求分析",
        pendingCards: 2,
      }),
    ).toBe("【PM 上下文】任务 TASK-9 · 环节 需求分析 · 待拍板 2 张");
  });

  it("uses an em dash rather than 'null' when a slot is unknown", () => {
    const line = buildContextLine(TEMPLATE, {
      taskPublicId: null,
      stepLabel: null,
      pendingCards: 0,
    });
    expect(line).not.toContain("null");
    expect(line).toContain("—");
  });
});

describe("CollabChatPanel", () => {
  it("shows the context it will send, and sends text with that line prepended", async () => {
    const user = userEvent.setup();
    render(
      <CollabChatPanel
        taskPublicId="TASK-9"
        stepLabel="需求分析"
        pendingCards={2}
      />,
    );

    expect(
      screen.getByText("【PM 上下文】任务 TASK-9 · 环节 需求分析 · 待拍板 2 张"),
    ).toBeInTheDocument();

    await user.type(screen.getByRole("textbox"), "帮我看看结论");
    await user.click(screen.getByTitle("pm.collab.send"));

    expect(h.send).toHaveBeenCalledWith(
      "【PM 上下文】任务 TASK-9 · 环节 需求分析 · 待拍板 2 张\n\n帮我看看结论",
    );
  });

  it("renders the host message list rather than its own", () => {
    render(<CollabChatPanel taskPublicId="T" stepLabel="S" pendingCards={0} />);
    expect(screen.getByTestId("message-list")).toBeInTheDocument();
  });

  it("starts a new thread through the shared chat runtime", async () => {
    const user = userEvent.setup();
    render(<CollabChatPanel taskPublicId="T" stepLabel="S" pendingCards={0} />);

    await user.click(screen.getByLabelText("pm.collab.newThread"));
    expect(h.startNewThread).toHaveBeenCalledTimes(1);
  });

  it("asks for an agent instead of rendering a dead composer", () => {
    h.agent.current = null;
    render(<CollabChatPanel taskPublicId="T" stepLabel="S" pendingCards={0} />);

    expect(screen.getByText("pm.collab.noAgent")).toBeInTheDocument();
    expect(screen.getByTestId("agent-selector")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
  });
});
