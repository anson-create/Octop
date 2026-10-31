import { describe, expect, it } from "vitest";
import { buildNavSections, SIDEBAR_NAV_KEYS } from "../../layouts/sidebarNav";
import { navAllowed, pathPermissionKeys } from "../../utils/permissions";
import type { OctopUser } from "../../api/modules/auth";

const pmUser = { id: 2, username: "pm", role: "user", permissions: ["pm"] } as OctopUser;
const plainUser = { id: 3, username: "dev", role: "user", permissions: [] } as OctopUser;

describe("PM access wiring", () => {
  it("guards every PM route with the pm permission", () => {
    expect(pathPermissionKeys("/pm")).toEqual(["pm"]);
    expect(pathPermissionKeys("/pm/tasks/12")).toEqual(["pm"]);
    expect(pathPermissionKeys("/pm/requirements/3")).toEqual(["pm"]);
    expect(pathPermissionKeys("/pm/decisions")).toEqual(["pm"]);
  });

  it("only lists the nav entry for holders", () => {
    expect(navAllowed(pmUser, "pm")).toBe(true);
    expect(navAllowed(plainUser, "pm")).toBe(false);
    expect(SIDEBAR_NAV_KEYS).toContain("pm");

    const keys = buildNavSections(pmUser)
      .flatMap((section) => section.items.map((item) => item.key));
    expect(keys).toContain("pm");
    expect(keys).not.toContain("admin-users");
  });

  it("points the nav entry at the list route", () => {
    const item = buildNavSections(pmUser)
      .flatMap((section) => section.items)
      .find((entry) => entry.key === "pm");
    expect(item?.path).toBe("/pm");
  });
});
