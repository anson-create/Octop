import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { jiraStatus, jiraMeta, listJiraIssues, getJiraIssue, createRequirement } = vi.hoisted(
  () => ({
    jiraStatus: vi.fn(),
    jiraMeta: vi.fn(),
    listJiraIssues: vi.fn(),
    getJiraIssue: vi.fn(),
    createRequirement: vi.fn(),
  }),
);

vi.mock("../../../api/modules/pm", () => ({
  pmApi: {
    jiraStatus,
    jiraMeta,
    listJiraIssues,
    getJiraIssue,
    createRequirement,
    saveJiraConfig: vi.fn(() => Promise.resolve({ configured: true })),
    probeJira: vi.fn(() => Promise.resolve({ user: "tester" })),
  },
}));

vi.mock("../../../utils/antdMessage", () => ({
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

import { message } from "../../../utils/antdMessage";
import NewRequirementModal from "./NewRequirementModal";

const ISSUE = {
  key: "CCTOWER-3597",
  title: "派单供应商列表新增就绪状态筛选",
  description: "按就绪状态筛选",
  status: "待设计",
  status_category: "new",
  assignee: "张三",
  reporter: "李四",
  priority: "P0",
  issue_type: "Story",
  project: "CCTOWER",
  resolution: "",
  labels: [],
  created: "2026-10-01T09:00:00.000+0800",
  updated: "2026-10-30T18:00:00.000+0800",
  components: ["派单供应商"],
  custom_fields: [{ id: "customfield_10101", name: "业务痛点", text: "供应商状态看不清" }],
  comments: [{ author: "产品经理", text: "就绪状态要和适配器配置对齐", created: "2026-10-30" }],
  attachments: [],
};

const PREVIEW_TEXT = [
  "## 需求描述",
  "按就绪状态筛选",
  "",
  "## 背景与补充",
  "### 业务痛点",
  "供应商状态看不清",
  "",
  "## 讨论记录",
  "1. 产品经理（2026-10-30）：就绪状态要和适配器配置对齐",
].join("\n");

const PREVIEW = {
  key: "CCTOWER-3597",
  issue: ISSUE,
  requirement_text: PREVIEW_TEXT,
  sections: ["需求描述", "背景与补充", "讨论记录"],
  truncated: [],
  blockers: [],
  existing: null as null | { id: number; public_id: string; title: string; status: string },
};

const META = {
  user: "测试员",
  statuses: ["待设计", "开发中", "已完成"],
  priorities: ["P0", "P1", "P2"],
  jql: 'assignee = currentUser() AND status in ("待设计")',
  status_hits: ["待设计"],
  priority_hits: ["P0", "P1"],
  ok: true,
  error: "",
};

function stubStatus(configured = true) {
  jiraStatus.mockResolvedValue({
    configured,
    base_url: configured ? "https://jira.example.com" : "",
    jql: "",
    verify_tls: true,
    token_set: configured,
    enabled: configured,
    last_probe_at: null,
    probe_status: null,
  });
}

function renderModal(
  overrides: {
    configured?: boolean;
    preview?: typeof PREVIEW;
    readError?: Error;
    meta?: Partial<typeof META>;
    items?: (typeof ISSUE)[];
    note?: string;
  } = {},
) {
  stubStatus(overrides.configured ?? true);
  jiraMeta.mockResolvedValue({ ...META, ...overrides.meta });
  listJiraIssues.mockResolvedValue({
    jql: META.jql,
    items: overrides.items ?? [ISSUE],
    total: (overrides.items ?? [ISSUE]).length,
    note: overrides.note ?? "",
  });
  // Set here, not in the tests: the read only happens when the operator clicks,
  // so a mock installed after render would be too late and one installed before
  // would be overwritten by this default.
  if (overrides.readError) {
    getJiraIssue.mockRejectedValue(overrides.readError);
  } else {
    getJiraIssue.mockResolvedValue(overrides.preview ?? { ...PREVIEW });
  }
  return render(
    <NewRequirementModal open workspaceId={1} onClose={vi.fn()} onCreated={vi.fn()} />,
  );
}

function primaryButton(): HTMLElement {
  const footer = document.querySelector(".ant-modal-footer");
  const button = footer?.querySelector(".ant-btn-primary");
  if (!button) throw new Error("modal OK button not found");
  return button as HTMLElement;
}

async function pickRow(user: ReturnType<typeof userEvent.setup>, key = "CCTOWER-3597") {
  const row = await waitFor(() => {
    const found = document.querySelector(`[data-issue-key="${key}"]`);
    if (!found) throw new Error(`row ${key} not rendered`);
    return found as HTMLElement;
  });
  await user.click(row);
}

describe("NewRequirementModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads the design queue on open instead of hiding it behind a toggle", async () => {
    renderModal();

    // The thing the operator came here for is their own queue; making them
    // click "browse" first turned the default into a blank dialog.
    await waitFor(() => expect(listJiraIssues).toHaveBeenCalledWith(1, undefined));
    expect(await screen.findByTestId("pm-jira-issues")).toBeInTheDocument();
  });

  it("says which queue it is showing", async () => {
    renderModal();

    const hint = await screen.findByTestId("pm-jira-queue-hint");
    expect(hint.textContent).toBe("pm.newRequirement.queueHint");
  });

  it("has one search box, not a key field plus a separate list", async () => {
    renderModal();
    await screen.findByTestId("pm-jira-search");

    // The old dialog had two inputs for one question, and only one of them
    // surfaced the queue.
    expect(document.querySelectorAll('input[type="text"]')).toHaveLength(1);
    expect(screen.queryByTestId("pm-jira-key-input")).toBeNull();
  });

  it("searches fuzzily after a pause, not on every keystroke", async () => {
    const user = userEvent.setup();
    renderModal();
    const input = await screen.findByTestId("pm-jira-search");

    await user.type(input, "报关单");

    // Debounced: the request carries the finished phrase, not each prefix.
    await waitFor(() => expect(listJiraIssues).toHaveBeenCalledWith(1, "报关单"), {
      timeout: 2000,
    });
    expect(listJiraIssues).not.toHaveBeenCalledWith(1, "报");
  });

  it("reads an issue in full only after one is picked", async () => {
    const user = userEvent.setup();
    renderModal();

    // Listing proves a ticket exists; it does not prove what it says.
    expect(getJiraIssue).not.toHaveBeenCalled();

    await pickRow(user);

    await waitFor(() => expect(getJiraIssue).toHaveBeenCalledWith(1, "CCTOWER-3597"));
    const body = await screen.findByTestId("pm-jira-preview-text");
    // Not just the description: the custom field and the comment used to be
    // dropped on the floor, and the analysis then wrote about a ticket it had
    // only half read.
    expect(body.textContent).toContain("按就绪状态筛选");
    expect(body.textContent).toContain("供应商状态看不清");
    expect(body.textContent).toContain("就绪状态要和适配器配置对齐");
  });

  it("keeps the footer disabled until a ticket has actually been read", async () => {
    const user = userEvent.setup();
    renderModal();
    await screen.findByTestId("pm-jira-issues");

    expect(primaryButton()).toBeDisabled();

    await user.click(primaryButton());
    // Clicking the disabled button does nothing, and nothing was created from a
    // row that merely appeared in a list.
    expect(createRequirement).not.toHaveBeenCalled();
    expect(message.warning).not.toHaveBeenCalled();
  });

  it("submits the key the server returned", async () => {
    const user = userEvent.setup();
    renderModal();

    await pickRow(user);
    await screen.findByTestId("pm-jira-preview");
    await user.click(primaryButton());

    await waitFor(() => expect(createRequirement).toHaveBeenCalled());
    expect(createRequirement).toHaveBeenCalledWith({
      workspace_id: 1,
      issue_key: "CCTOWER-3597",
    });
  });

  it("warns when the ticket is already a requirement instead of duplicating it", async () => {
    const user = userEvent.setup();
    renderModal({
      preview: {
        ...PREVIEW,
        existing: { id: 9, public_id: "REQ-0009", title: "派单供应商就绪状态", status: "drafting" },
      },
    });

    await pickRow(user);

    expect(await screen.findByText(/REQ-0009/)).toBeInTheDocument();
    expect(primaryButton().textContent).toBe("pm.newRequirement.openExisting");
  });

  it("shows a read failure where the preview would be, and cannot be confirmed", async () => {
    const user = userEvent.setup();
    renderModal({ readError: new Error("Jira 返回 404：issue 不存在或地址不对") });

    await pickRow(user);

    expect(await screen.findByTestId("pm-jira-read-error")).toBeInTheDocument();
    expect(screen.queryByTestId("pm-jira-preview")).toBeNull();
    expect(primaryButton()).toBeDisabled();
  });

  it("flags text the export blacklist will block, at intake rather than at export", async () => {
    const user = userEvent.setup();
    renderModal({ preview: { ...PREVIEW, blockers: ["内部工单号 1 处（CCTOWER-1111）"] } });

    await pickRow(user);

    expect(await screen.findByText(/CCTOWER-1111/)).toBeInTheDocument();
  });

  it("explains a queue filter the site does not recognise", async () => {
    renderModal({
      meta: { ok: false, status_hits: [], statuses: ["To Do", "In Progress"], error: "" },
    });

    // Naming the site's real statuses is the only version of this message an
    // operator can act on.
    expect(await screen.findByTestId("pm-jira-queue-mismatch")).toBeInTheDocument();
  });

  it("repeats the server's fallback note when the queue had to be widened", async () => {
    renderModal({ note: "默认筛选（状态/优先级）在本站不成立，已退回“我的未完成工单”。" });

    expect(await screen.findByTestId("pm-jira-list-note")).toBeInTheDocument();
  });

  it("asks for the Jira connection when none is configured", async () => {
    renderModal({ configured: false });

    await waitFor(() => expect(screen.getByTestId("pm-jira-config")).toBeInTheDocument());
    // No search box until the site is reachable — typing into it would fail with
    // a connection error that looks like a bad ticket number.
    expect(screen.queryByTestId("pm-jira-search")).toBeNull();
    expect(listJiraIssues).not.toHaveBeenCalled();
  });
});
