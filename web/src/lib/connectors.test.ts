import { describe, expect, it } from "vitest"
import {
  authorName,
  changedKeys,
  customConnector,
  directoryNeeds,
  formProblems,
  formValues,
  groupConnectors,
  groupSkills,
  isUrl,
  matchesConnector,
  openDraft,
  pairs,
  pluginParts,
  secretUser,
  splitArgs,
  stanceOf,
  toolsWords,
  withStance,
} from "./connectors"
import type { ChannelRules, Connector, ConnectorField, Plugin, Skill } from "./types"

const tool = (name: string, enabled = true) => ({
  name,
  title: name,
  description: "",
  risk: "read" as const,
  open_world: false,
  enabled,
})

const connector = (id: string, more: Partial<Connector> = {}): Connector => ({
  id,
  title: id,
  description: `The ${id} connector`,
  icon: "Plug",
  kind: "builtin",
  trust: "orion",
  sandbox: "none",
  status: "connected",
  error: "",
  needs: [],
  auth: "none",
  signed_in: false,
  local: true,
  tools: [tool(`${id}_one`), tool(`${id}_two`)],
  settings: { kind: "none" },
  secrets: [],
  removable: false,
  calls: 0,
  ...more,
})

describe("the connectors in their groups", () => {
  const all = [
    connector("web"),
    connector("system", { status: "off" }),
    connector("notion", { kind: "catalog", status: "needs_sign_in" }),
    connector("hass", { kind: "catalog", status: "error" }),
    connector("my-notes", { kind: "custom" }),
    connector("context7", { kind: "plugin", plugin: "context7" }),
    connector("calendar"),
  ]
  it("puts his own, the ones you added and the plugins' apart, in that order", () => {
    expect(groupConnectors(all).map((g) => [g.title, g.items.map((c) => c.id)])).toEqual([
      ["His own", ["calendar", "web", "system"]],
      ["Added", ["hass", "notion", "my-notes"]],
      ["From plugins", ["context7"]],
    ])
  })
  it("shows what needs a look first, then by title, and what is off last", () => {
    const added = groupConnectors(all).find((g) => g.title === "Added")
    expect(added?.items.map((c) => c.status)).toEqual(["error", "needs_sign_in", "connected"])
  })
  it("narrows by every word you typed, in a title, a description or a tool, and leaves out an empty group", () => {
    expect(groupConnectors(all, "notion").map((g) => g.title)).toEqual(["Added"])
    expect(matchesConnector(connector("web"), "the web")).toBe(true)
    expect(matchesConnector(connector("web"), "web_two")).toBe(true)
    expect(matchesConnector(connector("web"), "web paris")).toBe(false)
    expect(groupConnectors(all, "nothing like it")).toEqual([])
  })
  it("says how many tools are on", () => {
    expect(toolsWords(connector("a"))).toBe("2 tools")
    expect(toolsWords(connector("a", { tools: [tool("x")] }))).toBe("1 tool")
    expect(toolsWords(connector("a", { tools: [tool("x"), tool("y", false), tool("z")] }))).toBe("2 of 3 tools on")
    expect(toolsWords(connector("a", { tools: [] }))).toBe("No tools yet")
  })
})

describe("what a channel's limits make of a connector", () => {
  const rules: ChannelRules = { never: ["notion:*", "web_one"], confirm: ["web:*", "@destroy"] }
  it("reads the whole connector's stance, and counts the tools named alone", () => {
    expect(stanceOf(connector("notion"), rules)).toEqual({ stance: "never", named: 0 })
    expect(stanceOf(connector("web"), rules)).toEqual({ stance: "asks", named: 1 })
    expect(stanceOf(connector("system"), rules)).toEqual({ stance: "allowed", named: 0 })
  })
  it("changes only that connector's entry, whichever list held it", () => {
    expect(withStance("web", rules, "never")).toEqual({
      never: ["notion:*", "web_one", "web:*"],
      confirm: ["@destroy"],
    })
    expect(withStance("notion", rules, "allowed")).toEqual({ never: ["web_one"], confirm: ["web:*", "@destroy"] })
    expect(withStance("system", rules, "asks")).toEqual({
      never: ["notion:*", "web_one"],
      confirm: ["web:*", "@destroy", "system:*"],
    })
    expect(withStance("web", rules, "asks")).toEqual(rules)
  })
})

describe("a settings form", () => {
  const fields: ConnectorField[] = [
    {
      key: "url",
      label: "Address",
      kind: "text",
      help: "",
      placeholder: "",
      options: [],
      required: true,
      secret: false,
    },
    {
      key: "token",
      label: "Token",
      kind: "secret",
      help: "",
      placeholder: "",
      options: [],
      required: true,
      secret: true,
      set: true,
    },
    {
      key: "port",
      label: "Port",
      kind: "number",
      help: "",
      placeholder: "",
      options: [],
      required: false,
      secret: false,
    },
    { key: "on", label: "On", kind: "switch", help: "", placeholder: "", options: [], required: false, secret: false },
    {
      key: "feeds",
      label: "Feeds",
      kind: "list",
      help: "",
      placeholder: "",
      options: [],
      required: false,
      secret: false,
    },
  ]
  const values = { url: "http://x", port: 8123, on: true, feeds: ["a", "b"] }
  it("opens with what is saved, a secret's box empty", () => {
    expect(openDraft(fields, values)).toEqual({ url: "http://x", token: "", port: "8123", on: true, feeds: ["a", "b"] })
    expect(openDraft(fields, {})).toEqual({ url: "", token: "", port: "", on: false, feeds: [] })
  })
  it("finds what changed: a secret only once something is typed in it", () => {
    const draft = openDraft(fields, values)
    expect(changedKeys(fields, values, draft)).toEqual([])
    expect(changedKeys(fields, values, { ...draft, url: "http://y", token: "abc", feeds: ["a"] })).toEqual([
      "url",
      "token",
      "feeds",
    ])
  })
  it("saves only the changes, a number as a number", () => {
    const draft = { ...openDraft(fields, values), port: "9000", on: false }
    expect(formValues(fields, values, draft)).toEqual({ port: 9000, on: false })
    expect(formValues(fields, values, { ...draft, port: "" })).toEqual({ port: null, on: false })
  })
  it("says what is wrong: a required field left empty (a secret already set counts as filled), a number that isn't one", () => {
    const draft = openDraft(fields, values)
    expect(formProblems(fields, draft)).toEqual({})
    expect(formProblems(fields, { ...draft, url: "", port: "eight" })).toEqual({ url: "Required", port: "A number" })
    const unset = fields.map((f) => (f.key === "token" ? { ...f, set: false } : f))
    expect(formProblems(unset, draft)).toEqual({ token: "Required" })
  })
})

describe("a connector of your own", () => {
  it("splits a command line as a shell would for the simple cases", () => {
    expect(splitArgs(`uvx mcp-server-time --tz "Europe/Lisbon" 'a b'`)).toEqual([
      "uvx",
      "mcp-server-time",
      "--tz",
      "Europe/Lisbon",
      "a b",
    ])
  })
  it("reads KEY=VALUE and Header: value lines, and names the first line that is neither", () => {
    expect(pairs("API_KEY=abc\n\nAuthorization: Bearer x y")).toEqual({ API_KEY: "abc", Authorization: "Bearer x y" })
    expect(pairs("fine=1\nnot a pair")).toBe(2)
  })
  it("makes a command's entry with its environment, or a URL's with its headers", () => {
    expect(customConnector({ name: "my-notes", where: "uvx mcp-server-notes /notes", extra: "TOKEN=1" }, [])).toEqual({
      body: {
        from: "custom",
        name: "my-notes",
        command: "uvx",
        args: ["mcp-server-notes", "/notes"],
        env: { TOKEN: "1" },
      },
    })
    expect(
      customConnector({ name: "remote", where: "https://x.example/mcp", extra: "Authorization: Bearer 1" }, []),
    ).toEqual({
      body: { from: "custom", name: "remote", url: "https://x.example/mcp", headers: { Authorization: "Bearer 1" } },
    })
    expect(isUrl(" http://localhost:3000 ")).toBe(true)
    expect(isUrl("uvx thing")).toBe(false)
  })
  it("refuses what the server would: a bad or taken name, nothing to run, a line that isn't a pair", () => {
    const problem = (form: Parameters<typeof customConnector>[0], taken: string[] = []) => {
      const made = customConnector(form, taken)
      return "problem" in made ? made.problem : ""
    }
    expect(problem({ name: "My Notes", where: "x", extra: "" })).toMatch(/lower-case/)
    expect(problem({ name: "notes", where: "x", extra: "" }, ["notes"])).toMatch(/already a connector called notes/)
    expect(problem({ name: "notes", where: "  ", extra: "" })).toMatch(/command that starts it/)
    expect(problem({ name: "notes", where: "uvx x", extra: "oops" })).toMatch(/Line 1 of the environment/)
    expect(problem({ name: "notes", where: "https://x", extra: "oops" })).toMatch(/Line 1 of the headers/)
  })
})

describe("skills and plugins in words", () => {
  const skill = (name: string, source: Skill["source"], more: Partial<Skill> = {}): Skill => ({
    name,
    description: `${name} does a thing`,
    source,
    enabled: true,
    triggers: [],
    tools: [],
    files: ["SKILL.md"],
    editable: source === "yours" || source === "learned",
    ...more,
  })
  it("groups skills by where they come from, each by name", () => {
    const skills = [
      skill("zeta", "yours"),
      skill("alpha", "yours", { triggers: ["movie night"] }),
      skill("weather", "builtin"),
      skill("digest", "plugin"),
      skill("focus", "learned"),
    ]
    expect(groupSkills(skills).map((g) => [g.title, g.items.map((s) => s.name)])).toEqual([
      ["Yours", ["alpha", "zeta"]],
      ["Learned", ["focus"]],
      ["From plugins", ["digest"]],
      ["His own", ["weather"]],
    ])
    expect(groupSkills(skills, "movie").map((g) => g.items.map((s) => s.name))).toEqual([["alpha"]])
  })
  it("names a plugin's author and what it brings", () => {
    expect(authorName("Anthropic")).toBe("Anthropic")
    expect(authorName({ name: "Upstash", url: "https://upstash.com" })).toBe("Upstash")
    expect(authorName(undefined)).toBe("")
    const parts = (skills: number, commands: number, connectors: number) =>
      pluginParts({
        components: {
          skills: Array(skills).fill("s"),
          commands: Array(commands).fill("c"),
          connectors: Array(connectors).fill("k"),
          unused: ["hooks"],
        },
      } as Pick<Plugin, "components">)
    expect(parts(1, 2, 1)).toBe("3 skills · 1 connector")
    expect(parts(0, 1, 0)).toBe("1 skill")
    expect(parts(0, 0, 2)).toBe("2 connectors")
    expect(parts(0, 0, 0)).toBe("Nothing Orion uses")
  })
})

describe("what a directory connector asks of you", () => {
  const field = (label: string, required = true) => ({
    key: label,
    label,
    kind: "text" as const,
    help: "",
    placeholder: "",
    options: [],
    required,
    secret: false,
  })
  it("lists the fields it requires, as they are written, and leaves out the optional ones", () => {
    expect(
      directoryNeeds({ auth: "fields", fields: [field("Home Assistant's address"), field("Long-lived access token")] }),
    ).toBe("Needs Home Assistant's address · Long-lived access token")
    expect(directoryNeeds({ auth: "fields", fields: [field("Exa API key", false)] })).toBe("Nothing required")
    expect(directoryNeeds({ auth: "none", fields: [] })).toBe("Nothing required")
  })
  it("says a sign-in, and that a service that gives none to unknown apps comes after an app of your own", () => {
    expect(directoryNeeds({ auth: "oauth", fields: [] })).toBe("Sign in with your account")
    expect(directoryNeeds({ auth: "oauth", fields: [field("OAuth client ID"), field("OAuth client secret")] })).toBe(
      "Sign in, after you make an app of your own",
    )
  })
})

describe("what uses a secret", () => {
  const titles = new Map([["home-assistant", "Home Assistant"]])
  it("names the place the server gives: the brain's key, a calendar, a connector by its title", () => {
    expect(secretUser("openrouter.api_key", titles)).toBe("OpenRouter")
    expect(secretUser("calendar:home", titles)).toBe("the home calendar")
    expect(secretUser("home-assistant", titles)).toBe("Home Assistant")
    expect(secretUser("gone-connector", titles)).toBe("gone-connector")
  })
})
