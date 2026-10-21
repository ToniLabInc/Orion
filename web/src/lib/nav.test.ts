import { describe, expect, it } from "vitest"
import { GESTURES, isCustomizeTab, isSettingsTab, movedTo, pageOf, SETTINGS_TABS } from "./nav"

describe("which page an address shows", () => {
  it("knows every page of the HUD", () => {
    expect(pageOf("/")).toBe("stage")
    expect(pageOf("/memory")).toBe("memory")
    expect(pageOf("/automations")).toBe("automations")
    expect(pageOf("/automations/watchers")).toBe("automations")
    expect(pageOf("/activity")).toBe("activity")
    expect(pageOf("/customize")).toBe("customize")
    expect(pageOf("/customize/")).toBe("customize")
    expect(pageOf("/customize/connectors")).toBe("customize")
    expect(pageOf("/customize/connectors/home-assistant/settings")).toBe("customize")
    expect(pageOf("/customize/skills/pc-checkup")).toBe("customize")
    expect(pageOf("/customize/plugins/daily-digest/unused")).toBe("customize")
    expect(pageOf("/settings")).toBe("settings")
    for (const tab of SETTINGS_TABS) expect(pageOf(`/settings/${tab.id}`)).toBe("settings")
  })
  it("knows no other address, and no tab that isn't one", () => {
    expect(pageOf("/nothing")).toBeNull()
    expect(pageOf("/customize/widgets")).toBeNull()
    expect(pageOf("/settings/abilities")).toBeNull()
    expect(pageOf("/activity/extra")).toBeNull()
    expect(isCustomizeTab("plugins")).toBe(true)
    expect(isCustomizeTab("abilities")).toBe(false)
    expect(isSettingsTab("safety")).toBe(true)
    expect(isSettingsTab("gestures")).toBe(false)
    expect(isSettingsTab(undefined)).toBe(false)
  })
})

describe("where an address that moved lives now", () => {
  it("sends the old tabs to the page they became", () => {
    expect(movedTo("/neural")).toBe("/memory")
    expect(movedTo("/customize")).toBe("/customize/connectors")
    expect(movedTo("/customize/")).toBe("/customize/connectors")
    expect(movedTo("/settings/abilities")).toBe("/customize/connectors")
    expect(movedTo("/settings/eyes")).toBe("/settings/safety")
    expect(movedTo("/automations/skills")).toBe("/customize/skills")
  })
  it("leaves the gestures to the server, which knows the first remote's name", () => {
    expect(movedTo("/settings/gestures")).toBe(GESTURES)
  })
  it("leaves an address that hasn't moved alone", () => {
    for (const path of ["/", "/memory", "/settings/safety", "/customize/skills", "/activity"])
      expect(movedTo(path)).toBeNull()
  })
})
