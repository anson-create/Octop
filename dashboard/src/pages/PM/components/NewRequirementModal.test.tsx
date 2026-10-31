import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { jiraStatus, listJiraIssues, createRequirement } = vi.hoisted(() => ({
  jiraStatus: vi.fn(),
  listJiraIssues: vi.fn(),
  createRequirement: vi.fn(),
  saveJiraConfig: vi.fn(),
  probeJira: vi.fn(),
}));

vi.mock("../../../api/modules/pm", () => ({
  pmApi: {
    jiraStatus,
    listJiraIssues,
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
};

function renderModal(overrides: { configured: boolean }) {
  jiraStatus.mockResolvedValue({
    configured: overrides.configured,
    base_url: overrides.configured ? "https://jira.example.com" : "",
    jql: "",
    verify_tls: true,
    token_set: overrides.configured,
    enabled: overrides.configured,
    last_probe_at: null,
    probe_status: null,
  });
  listJiraIssues.mockResolvedValue({ jql: "x", items: [ISSUE], total: 1 });
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

describe("NewRequirementModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("has no free-text intake any more", async () => {
    renderModal({ configured: true });
    await waitFor(() => expect(listJiraIssues).toHaveBeenCalled());

    // The old form had a title input and a raw-text textarea. Intake by typing is
    // exactly what was removed, so neither may come back.
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
    expect(screen.queryByText("pm.fields.rawText")).toBeNull();
  });

  it("lists issues and submits the chosen key, not a title", async () => {
    const user = userEvent.setup();
    renderModal({ configured: true });

    const row = await screen.findByText("CCTOWER-3597");
    await user.click(row);

    await user.click(primaryButton());

    await waitFor(() => expect(createRequirement).toHaveBeenCalled());
    expect(createRequirement).toHaveBeenCalledWith({
      workspace_id: 1,
      issue_key: "CCTOWER-3597",
    });
  });

  it("keeps OK disabled until an issue is picked", async () => {
    renderModal({ configured: true });
    await screen.findByText("CCTOWER-3597");

    expect(primaryButton()).toBeDisabled();
  });

  it("asks for the Jira connection when none is configured", async () => {
    renderModal({ configured: false });

    await waitFor(() =>
      expect(screen.getByTestId("pm-jira-config")).toBeInTheDocument(),
    );
    // No picker until the site is reachable — an empty list would look like
    // "you have no tickets", which is a different and wrong message.
    expect(screen.queryByTestId("pm-jira-issues")).toBeNull();
    expect(listJiraIssues).not.toHaveBeenCalled();
  });
});
