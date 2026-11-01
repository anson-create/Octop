import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { jiraStatus, listJiraIssues, getJiraIssue, createRequirement } = vi.hoisted(() => ({
  jiraStatus: vi.fn(),
  listJiraIssues: vi.fn(),
  getJiraIssue: vi.fn(),
  createRequirement: vi.fn(),
}));

vi.mock("../../../api/modules/pm", () => ({
  pmApi: {
    jiraStatus,
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

import NewRequirementModal from "./NewRequirementModal";

const ISSUE = {
  key: "CCTOWER-3597",
  title: "派单供应商列表新增就绪状态筛选",
  description: "按就绪状态筛选",
  status: "In Progress",
  status_category: "indeterminate",
  assignee: "张三",
  reporter: "李四",
  priority: "High",
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
  overrides: { configured?: boolean; preview?: typeof PREVIEW; readError?: Error } = {},
) {
  stubStatus(overrides.configured ?? true);
  listJiraIssues.mockResolvedValue({ jql: "x", items: [ISSUE], total: 1 });
  // Set here, not in the tests: the read only happens when the operator clicks,
  // so a mock installed after render would be too late and one installed before
  // would be overwritten by this default.
  if (overrides.readError) {
    getJiraIssue.mockRejectedValue(overrides.readError);
  } else {
    getJiraIssue.mockResolvedValue(overrides.preview ?? { ...PREVIEW });
  }
  return render(
    <NewRequirementModal
      open
      workspaceId={1}
      onClose={vi.fn()}
      onCreated={vi.fn()}
    />,
  );
}

function primaryButton(): HTMLElement {
  const footer = document.querySelector(".ant-modal-footer");
  const button = footer?.querySelector(".ant-btn-primary");
  if (!button) throw new Error("modal OK button not found");
  return button as HTMLElement;
}

async function readKey(user: ReturnType<typeof userEvent.setup>, value: string) {
  const input = await screen.findByTestId("pm-jira-key-input");
  await user.clear(input);
  await user.type(input, value);
  await user.click(screen.getByRole("button", { name: "pm.newRequirement.read" }));
}

describe("NewRequirementModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("has no free-text intake any more", async () => {
    renderModal();
    await waitFor(() => expect(screen.getByTestId("pm-jira-key-input")).toBeInTheDocument());

    // The old form had a title input and a raw-text textarea. Intake by typing
    // prose is exactly what was removed; an issue key is a reference, not prose.
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
    expect(screen.queryByText("pm.fields.rawText")).toBeNull();
  });

  it("reads the issue behind a typed key and shows the text that will be stored", async () => {
    const user = userEvent.setup();
    renderModal();

    await readKey(user, "cctower-3597");

    // The key is canonicalised before it leaves the browser.
    await waitFor(() => expect(getJiraIssue).toHaveBeenCalledWith(1, "CCTOWER-3597"));

    const body = await screen.findByTestId("pm-jira-preview-text");
    // Not just the description: the custom field and the comment used to be
    // dropped on the floor, and the analysis then wrote about a ticket it had
    // only half read.
    expect(body.textContent).toContain("按就绪状态筛选");
    expect(body.textContent).toContain("供应商状态看不清");
    expect(body.textContent).toContain("就绪状态要和适配器配置对齐");
  });

  it("keeps OK disabled until an issue has actually been read", async () => {
    renderModal();
    await screen.findByTestId("pm-jira-key-input");

    expect(primaryButton()).toBeDisabled();

    // A key typed but not read is not a reference yet.
    const user = userEvent.setup();
    await user.type(screen.getByTestId("pm-jira-key-input"), "CCTOWER-3597");
    expect(primaryButton()).toBeDisabled();
  });

  it("submits the key the server returned, not what was typed", async () => {
    const user = userEvent.setup();
    renderModal();

    await readKey(user, "cctower-3597");
    await screen.findByTestId("pm-jira-preview");
    await user.click(primaryButton());

    await waitFor(() => expect(createRequirement).toHaveBeenCalled());
    expect(createRequirement).toHaveBeenCalledWith({
      workspace_id: 1,
      issue_key: "CCTOWER-3597",
    });
  });

  it("does not load the queue until it is asked for", async () => {
    const user = userEvent.setup();
    renderModal();
    await screen.findByTestId("pm-jira-key-input");

    // Opening straight into a picker is the behaviour being replaced: it reads
    // as "choose from what we found" rather than "name the ticket you mean".
    expect(listJiraIssues).not.toHaveBeenCalled();
    expect(screen.queryByTestId("pm-jira-issues")).toBeNull();

    await user.click(screen.getByRole("button", { name: "pm.newRequirement.browse" }));

    await waitFor(() => expect(listJiraIssues).toHaveBeenCalled());
    expect(await screen.findByTestId("pm-jira-issues")).toBeInTheDocument();
  });

  it("reads an issue picked from the queue through the same path", async () => {
    const user = userEvent.setup();
    renderModal();
    await screen.findByTestId("pm-jira-key-input");
    await user.click(screen.getByRole("button", { name: "pm.newRequirement.browse" }));

    const row = await screen.findByText("CCTOWER-3597");
    await user.click(row);

    await waitFor(() => expect(getJiraIssue).toHaveBeenCalledWith(1, "CCTOWER-3597"));
    expect(await screen.findByTestId("pm-jira-preview")).toBeInTheDocument();
  });

  it("warns when the ticket is already a requirement instead of duplicating it", async () => {
    const user = userEvent.setup();
    renderModal({
      preview: {
        ...PREVIEW,
        existing: { id: 9, public_id: "REQ-0009", title: "派单供应商就绪状态", status: "drafting" },
      },
    });

    await readKey(user, "CCTOWER-3597");

    expect(await screen.findByText(/REQ-0009/)).toBeInTheDocument();
    // The footer button no longer claims to create anything.
    expect(primaryButton().textContent).toBe("pm.newRequirement.openExisting");
  });

  it("shows a read failure where the preview would be, and cannot be confirmed", async () => {
    const user = userEvent.setup();
    renderModal({ readError: new Error("Jira 返回 404：issue 不存在或地址不对") });

    await readKey(user, "CCTOWER-3597");

    expect(await screen.findByTestId("pm-jira-read-error")).toBeInTheDocument();
    expect(screen.queryByTestId("pm-jira-preview")).toBeNull();
    expect(primaryButton()).toBeDisabled();
  });

  it("flags text the export blacklist will block, at intake rather than at export", async () => {
    const user = userEvent.setup();
    renderModal({
      preview: { ...PREVIEW, blockers: ["内部工单号 1 处（CCTOWER-1111）"] },
    });

    await readKey(user, "CCTOWER-3597");

    expect(await screen.findByText(/CCTOWER-1111/)).toBeInTheDocument();
  });

  it("asks for the Jira connection when none is configured", async () => {
    renderModal({ configured: false });

    await waitFor(() =>
      expect(screen.getByTestId("pm-jira-config")).toBeInTheDocument(),
    );
    // No key field until the site is reachable — typing into it would fail with
    // a connection error that looks like a bad ticket number.
    expect(screen.queryByTestId("pm-jira-key-input")).toBeNull();
    expect(getJiraIssue).not.toHaveBeenCalled();
  });
});
