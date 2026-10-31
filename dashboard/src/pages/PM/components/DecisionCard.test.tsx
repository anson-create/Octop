import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PmDecision } from "../../../api/types";

vi.mock("../../../api/modules/pm", () => ({
  pmApi: {
    answerDecision: vi.fn(() => Promise.resolve({})),
    appealDecision: vi.fn(() => Promise.resolve({})),
    transferDecision: vi.fn(() => Promise.resolve({})),
  },
}));

vi.mock("../../../utils/antdMessage", () => ({
  message: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

import { pmApi } from "../../../api/modules/pm";
import { message } from "../../../utils/antdMessage";
import DecisionCard, { parseCardAnswer } from "./DecisionCard";

const NOW = Date.parse("2026-10-31T00:00:00Z");

function build(overrides: Partial<PmDecision> = {}): PmDecision {
  return {
    id: 42,
    task_id: 7,
    step_id: 9,
    level: "L2",
    question: "是否产出可点击原型？",
    options: [
      { key: "yes", label: "产出原型", pros: "评审更直观", cons: "多 0.5 人日" },
      { key: "no", label: "跳过原型", pros: "更快" },
    ],
    recommended_key: "yes",
    status: "pending",
    selected_key: null,
    note: null,
    decided_by: null,
    decided_at: null,
    timeout_at: "2026-10-31T01:00:00Z",
    auto_decided: false,
    appeal_state: null,
    assignee_user_id: null,
    created_at: "2026-10-30T00:00:00Z",
    ...overrides,
  };
}

/** Walk the host card: pick an option, write the reason, review, submit. */
async function answerCard(optionLabel: string, reason: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: new RegExp(optionLabel) }));
  await user.type(screen.getByRole("textbox"), reason);
  await user.click(screen.getByRole("button", { name: "chat.ask.review" }));
  await user.click(screen.getByRole("button", { name: "chat.ask.submit" }));
  return user;
}

describe("parseCardAnswer", () => {
  it("splits the host card's 'header: value' lines back into answers", () => {
    expect(
      parseCardAnswer("L2 · 拍板: 跳过原型\n理由: 客户不需要", ["L2 · 拍板", "理由"]),
    ).toEqual(["跳过原型", "客户不需要"]);
  });

  it("keeps the value when the header contains a colon", () => {
    expect(parseCardAnswer("为什么: 因为 A: B", ["为什么"])).toEqual(["因为 A: B"]);
  });
});

describe("DecisionCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders through the host ask-question card", () => {
    render(<DecisionCard decision={build()} now={NOW} />);
    // The host card's own chrome is the proof of reuse: title + progress.
    expect(screen.getByText("chat.ask.title")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /产出原型/ })).toBeInTheDocument();
  });

  it("submits the chosen option key with the trimmed reason", async () => {
    render(<DecisionCard decision={build()} now={NOW} />);

    await answerCard("跳过原型", "  客户只要结论不看原型  ");

    await waitFor(() =>
      expect(pmApi.answerDecision).toHaveBeenCalledWith(
        42,
        "no",
        "客户只要结论不看原型",
      ),
    );
  });

  it("refuses a short reason without calling the API", async () => {
    render(<DecisionCard decision={build()} now={NOW} />);

    await answerCard("跳过原型", "太短");

    await waitFor(() => expect(message.warning).toHaveBeenCalled());
    expect(pmApi.answerDecision).not.toHaveBeenCalled();
  });

  it("renders a settled card read-only with its outcome", () => {
    render(
      <DecisionCard
        decision={build({
          status: "auto_answered",
          selected_key: "yes",
          note: "超时自动决策：系统选择 yes",
          decided_at: "2026-10-31T01:00:00Z",
          auto_decided: true,
        })}
        now={NOW}
      />,
    );

    expect(screen.queryByRole("button", { name: "chat.ask.submit" })).toBeNull();
    expect(screen.getByText(/超时自动决策：系统选择 yes/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /pm.decision.appeal/ }),
    ).toBeInTheDocument();
  });

  it("shows an overdue deadline instead of a countdown", () => {
    render(
      <DecisionCard
        decision={build({ timeout_at: "2026-10-30T23:00:00Z" })}
        now={NOW}
      />,
    );
    expect(screen.getByText(/pm\.decision\.overdue/)).toBeInTheDocument();
  });
});
