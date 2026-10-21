import { describe, expect, it } from "vitest"
import { asOptions, CHANNELS, offered, ruleGroups, sandboxState } from "./rules"
import type { SafetyState } from "./types"

const choices: SafetyState["choices"] = {
  classes: ["@read", "@change", "@destroy", "@open_world"],
  tools: [
    { name: "run_command", title: "Run a shell command", connector: "system", risk: "destroy" },
    { name: "volume", title: "The volume", connector: "system", risk: "change" },
    { name: "get_weather", title: "", connector: "web", risk: "read" },
  ],
}
const titles = { system: "This machine", web: "The web" }

describe("what a channel's limit can name", () => {
  it("lists the kinds of tool, each connector whole, then each tool, in words", () => {
    const groups = ruleGroups(choices, titles)
    expect(groups.map((g) => g.value)).toEqual(["Kinds of tool", "Whole connectors", "Tools"])
    expect(groups[0].items[2]).toMatchObject({ value: "@destroy", label: "Anything that deletes" })
    expect(groups[1].items.map((o) => [o.value, o.label])).toEqual([
      ["system:*", "All of This machine"],
      ["web:*", "All of The web"],
    ])
    expect(groups[2].items[0]).toEqual({
      value: "run_command",
      label: "Run a shell command",
      detail: "This machine · deletes",
    })
    expect(groups[2].items[2]).toMatchObject({ label: "get_weather", detail: "The web · reads" }) // no title: its name
  })
  it("says a connector as its id when it has no title, and leaves out a group with nothing in it", () => {
    const groups = ruleGroups({ classes: [], tools: [] }, titles)
    expect(groups).toEqual([])
    expect(ruleGroups({ classes: ["@read"], tools: [] }, {}).map((g) => g.value)).toEqual(["Kinds of tool"])
    expect(ruleGroups({ ...choices, classes: [] }, {})[0].items[0].label).toBe("All of system")
  })
})

describe("a list's entries", () => {
  const groups = ruleGroups(choices, titles)
  it("are said in words, in the list's order, and an entry nothing names any more stays as it is", () => {
    expect(asOptions(["@destroy", "power", "web:*"], groups).map((o) => [o.value, o.label, o.detail])).toEqual([
      ["@destroy", "Anything that deletes", "Tools whose work can't be undone"],
      ["power", "power", "Not offered any more"],
      ["web:*", "All of The web", "Every tool of The web"],
    ])
  })
  it("leave out of the other list what the channel's first list holds", () => {
    const rest = offered(groups, ["run_command", "system:*"])
    expect(rest[1].items.map((o) => o.value)).toEqual(["web:*"])
    expect(rest[2].items.map((o) => o.value)).toEqual(["volume", "get_weather"])
    expect(offered(groups, [])).toEqual(groups)
    expect(offered(groups, ["@read", "@change", "@destroy", "@open_world"]).map((g) => g.value)).toEqual([
      "Whole connectors",
      "Tools",
    ])
  })
})

describe("the channels", () => {
  it("are the five ways a request reaches him, each with who it is", () => {
    expect(CHANNELS.map((c) => c.id)).toEqual(["voice", "hud", "discord", "job", "app"])
    for (const c of CHANNELS) expect(c.about).not.toBe("")
  })
})

describe("how the sandbox stands", () => {
  const box = (over: Partial<SafetyState["sandbox"]> = {}): SafetyState["sandbox"] => ({
    available: "bwrap",
    commands: true,
    network: true,
    network_ok: true,
    hidden: [],
    writable: [],
    why: "",
    ...over,
  })
  it("works when commands run in a sandbox the machine can make", () => {
    expect(sandboxState(box())).toBe("works")
    expect(sandboxState(box({ available: "container" }))).toBe("works")
  })
  it("is off when commands run without one, whatever the machine can make", () => {
    expect(sandboxState(box({ commands: false }))).toBe("off")
    expect(sandboxState(box({ commands: false, available: "" }))).toBe("off")
  })
  it("is missing when commands need a sandbox the machine can't make: they are refused", () => {
    expect(sandboxState(box({ available: "", why: "bubblewrap isn't installed" }))).toBe("missing")
  })
  it("can't give commands the network when the kernel is too old, and only says so when it knows", () => {
    expect(sandboxState(box({ network_ok: false }))).toBe("no-network")
    expect(sandboxState(box({ network_ok: false, network: false }))).toBe("works")
    expect(sandboxState(box({ network_ok: false, available: "container" }))).toBe("works")
    expect(sandboxState(box({ network_ok: undefined }))).toBe("works")
  })
})
