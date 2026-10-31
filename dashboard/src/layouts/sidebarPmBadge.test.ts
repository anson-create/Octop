import { describe, expect, it } from "vitest";
import { buildNavSections } from "./sidebarNav";
import type { OctopUser } from "../api/modules/auth";

const admin = { id: 1, username: "anson", role: "admin" } as unknown as OctopUser;

function pmItem(opts?: { pmInboxBadge?: string }) {
  return buildNavSections(admin, opts)
    .flatMap((section) => section.items)
    .find((item) => item.key === "pm");
}

describe("sidebar PM badge", () => {
  it("shows how many cards are waiting on a human", () => {
    expect(pmItem({ pmInboxBadge: "3 待拍" })?.badge).toBe("3 待拍");
  });

  it("stays clean when nothing is waiting", () => {
    expect(pmItem({ pmInboxBadge: undefined })?.badge).toBeUndefined();
  });

  it("keeps every other nav item badge-free", () => {
    const sections = buildNavSections(admin, { pmInboxBadge: "3 待拍" });
    const badged = sections
      .flatMap((section) => section.items)
      .filter((item) => item.badge);
    expect(badged.map((item) => item.key)).toEqual(["pm"]);
  });
});
