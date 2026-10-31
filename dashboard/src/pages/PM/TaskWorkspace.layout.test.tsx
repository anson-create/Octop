import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PmTaskDetail } from "../../api/types";

/**
 * Layout contract (decision from the design review): the middle column is
 * chat + the human decision queue, the right column is the deliverable.
 * jsdom has no layout engine, so this test pins the *structure* — which column
 * holds what, and in which order — which is the part that was wrong before.
 */

const h = vi.hoisted(() => ({
  task: { current: null as unknown },
  isMobile: { current: false },
  search: { current: "" },
  /**
   * One stable ``t`` for the whole file. ``usePMResource`` keeps ``t`` in its
   * ``refresh`` deps, so a mock that returns a new function per render turns
   * the page into an endless refetch loop — invisible while nothing flushes,
   * a hard hang as soon as ``user.click`` awaits.
   */
  translate: (key: string, fallback?: unknown): string => {
    if (typeof fallback === "string") return fallback;
    if (fallback && typeof fallback === "object") {
      const opts = fallback as { defaultValue?: string };
      return opts.defaultValue ?? key;
    }
    return key;
  },
}));

vi.mock("react-i18next", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-i18next")>();
  return {
    ...actual,
    useTranslation: () => ({ t: h.translate }),
  };
});

vi.mock("react-router-dom", () => ({
  useParams: () => ({ taskId: "1" }),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [
    new URLSearchParams(h.search.current),
    vi.fn(),
  ] as unknown as ReturnType<typeof import("react-router-dom").useSearchParams>,
}));

vi.mock("../../api/modules/pm", () => ({
  pmApi: {
    getTask: () => Promise.resolve(h.task.current),
    listReviews: () => Promise.resolve({ items: [], perspectives: [] }),
  },
}));

vi.mock("../../layouts/PageShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock("../../hooks/useIsMobile", () => ({ useIsMobile: () => h.isMobile.current }));
vi.mock("../../utils/antdMessage", () => ({
  message: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock("./components/CollabChatPanel", () => ({
  default: () => <div data-testid="collab-chat" />,
}));
vi.mock("./components/DecisionCard", () => ({
  default: () => <div data-testid="decision-card" />,
}));
vi.mock("./components/ArtifactPanel", () => ({
  default: () => <div data-testid="artifact-panel" />,
}));
vi.mock("./components/ReviewPanel", () => ({
  default: () => <div data-testid="review-panel" />,
}));
vi.mock("./components/StageRail", () => ({
  default: () => <div data-testid="stage-rail" />,
}));
vi.mock("./components/EvidenceDrawer", () => ({ default: () => null }));
vi.mock("./components/ExportDrawer", () => ({ default: () => null }));
vi.mock("./components/TraceDrawer", () => ({ default: () => null }));
vi.mock("./components/ReflowModal", () => ({ default: () => null }));

import TaskWorkspace from "./TaskWorkspace";

function taskWith(pendingCount: number, status?: string): PmTaskDetail {
  return {
    id: 1,
    public_id: "TASK-1",
    status: status ?? (pendingCount > 0 ? "waiting_human" : "running"),
    stage: "P2",
    requirement: null,
    steps: [],
    stages: [],
    artifacts: [],
    evidence_count: 3,
    evidence_sources: { code: 2, jira: 1 },
    conclusions: { total: 2, evidenced: 1, uncovered: 1, undetermined: 0 },
    pending_decisions: Array.from({ length: pendingCount }, (_, index) => ({
      id: index + 1,
      step_id: null,
      level: "L2",
      question: `Q${index + 1}`,
      options: [],
      status: "pending",
      timeout_at: null,
    })),
  } as unknown as PmTaskDetail;
}

beforeEach(() => {
  h.task.current = taskWith(2);
  h.isMobile.current = false;
  h.search.current = "";
});

describe("TaskWorkspace column layout", () => {
  it("puts chat and the decision queue in the same (middle) column", async () => {
    const { container } = render(<TaskWorkspace />);
    await screen.findByTestId("collab-chat");

    const sections = container.querySelectorAll("section");
    expect(sections).toHaveLength(3);

    const middle = sections[1];
    expect(middle).toContainElement(screen.getByTestId("collab-chat"));
    expect(middle.textContent).toContain("pm.decision.title");
    expect(middle.querySelectorAll('[data-testid="decision-card"]')).toHaveLength(2);
  });

  it("keeps the deliverable out of the middle column", async () => {
    const { container } = render(<TaskWorkspace />);
    await screen.findByTestId("artifact-panel");

    const sections = container.querySelectorAll("section");
    const middle = sections[1];
    const right = sections[2];

    expect(right).toContainElement(screen.getByTestId("artifact-panel"));
    expect(right).toContainElement(screen.getByTestId("review-panel"));
    expect(middle).not.toContainElement(screen.getByTestId("artifact-panel"));
    expect(screen.getByTestId("stage-rail")).toBeInTheDocument();
  });

  it("shows the cards above the chat so a pending decision is not below the fold", async () => {
    render(<TaskWorkspace />);
    const chat = await screen.findByTestId("collab-chat");
    const zoneTitle = screen.getByText("pm.decision.title");

    // DOCUMENT_POSITION_FOLLOWING => the chat comes after the decision zone.
    expect(
      zoneTitle.compareDocumentPosition(chat) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("collapses the decision zone when nothing is pending", async () => {
    h.task.current = taskWith(0);
    render(<TaskWorkspace />);
    await screen.findByTestId("collab-chat");

    expect(screen.queryAllByTestId("decision-card")).toHaveLength(0);
    expect(screen.getByText("pm.decision.empty")).toBeInTheDocument();
  });
});

describe("TaskWorkspace on a phone", () => {
  beforeEach(() => {
    h.isMobile.current = true;
  });

  /**
   * The pane is URL-backed, so the test drives it the same way a pasted link
   * does. Clicking antd's Segmented under jsdom hangs the worker, so the
   * switcher itself is left to the eye.
   */
  async function openCollabPane() {
    h.search.current = "pane=collab";
    const user = userEvent.setup();
    render(<TaskWorkspace />);
    await screen.findByTestId("collab-chat");
    return user;
  }

  it("shows one pane at a time instead of three squeezed columns", async () => {
    const { container } = render(<TaskWorkspace />);
    await screen.findByTestId("stage-rail");
    expect(container.querySelectorAll("section")).toHaveLength(1);
  });

  it("keeps the cards collapsed so the composer stays reachable", async () => {
    h.task.current = taskWith(2, "running");
    const user = await openCollabPane();

    expect(screen.queryAllByTestId("decision-card")).toHaveLength(0);
    const toggle = screen.getByLabelText("pm.decision.toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);
    expect(screen.getAllByTestId("decision-card")).toHaveLength(2);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  it("opens the cards straight away when the task is blocked on a human", async () => {
    h.task.current = taskWith(2, "waiting_human");
    await openCollabPane();

    expect(screen.getAllByTestId("decision-card")).toHaveLength(2);
    expect(screen.getByLabelText("pm.decision.toggle")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("has no collapse handle on desktop", async () => {
    h.isMobile.current = false;
    render(<TaskWorkspace />);
    await screen.findByTestId("collab-chat");
    expect(screen.queryByLabelText("pm.decision.toggle")).toBeNull();
  });
});
