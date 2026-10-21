import { Building2, CalendarDays, Plug } from "lucide-react"
import { describe, expect, it } from "vitest"
import { CONNECTOR_ICONS, connectorIcon, iconKey } from "./icons"

describe("a connector's icon", () => {
  it("is found by lucide's name in any spelling", () => {
    expect(connectorIcon("CalendarDays")).toBe(CalendarDays)
    expect(connectorIcon("calendar-days")).toBe(CalendarDays)
    expect(connectorIcon("calendar_days")).toBe(CalendarDays)
    expect(connectorIcon("Building2")).toBe(Building2)
    expect(iconKey("Calendar-Days")).toBe("calendardays")
  })
  it("has an icon of its own for every name orion/connectors.py and orion/catalog.py send", () => {
    const sent = [
      "activity",
      "box",
      "brain",
      "briefcase",
      "calendar",
      "clipboard",
      "clock",
      "cloud",
      "eye",
      "folder-search",
      "git-branch",
      "globe",
      "house",
      "kanban",
      "list-checks",
      "mail",
      "mails",
      "message-circle",
      "message-square",
      "monitor",
      "notebook-pen",
      "orbit",
      "palette",
      "blocks",
      "hand",
      "search",
      "sparkles",
      "square-kanban",
      "timer",
      "zap",
    ]
    for (const name of sent) expect(connectorIcon(name), name).not.toBe(Plug)
    expect(connectorIcon("plug")).toBe(Plug)
  })
  it("is a plug for a name nothing here knows", () => {
    expect(connectorIcon("NoSuchIcon")).toBe(Plug)
    expect(connectorIcon("")).toBe(Plug)
  })
  it("has every listed icon reachable by its own name and by lucide's displayName", () => {
    for (const [name, icon] of Object.entries(CONNECTOR_ICONS)) {
      expect(connectorIcon(name), name).toBe(icon)
      expect(icon.displayName, `${name} has no displayName`).toBeTruthy()
      expect(connectorIcon(icon.displayName ?? ""), `${name}'s displayName`).toBe(icon)
    }
  })
})
