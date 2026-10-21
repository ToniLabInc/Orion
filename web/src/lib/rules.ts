// The words of a channel's limits, pure for the tests. An entry of a channel's never or always-asks list names a kind
// of tool (`@destroy`), a whole connector (`home-assistant:*`) or one tool (`run_command`); the HUD says each in
// words, offers what a list can still take (never what the other list of the channel holds: a tool is never or asks
// first, not both), and keeps an entry it doesn't know (a tool gone since) as it is.
import type { Channel, SafetyState } from "./types"

/** One entry a limit can name, in words. */
export interface RuleOption {
  value: string
  label: string
  detail: string
}

/** The options in groups, as the picker lists them. */
export interface RuleGroup {
  value: string
  items: RuleOption[]
}

/** The channels, in the order Settings lists them, with who they are. */
export const CHANNELS: readonly { id: Channel; label: string; about: string }[] = [
  { id: "voice", label: "Voice", about: "anyone in the room with him" },
  { id: "hud", label: "HUD", about: "this PC, and the devices you paired" },
  { id: "discord", label: "Discord", about: "your account on Discord" },
  { id: "job", label: "Jobs", about: "his scheduled jobs, with nobody watching" },
  { id: "app", label: "Other apps", about: "AI apps that use his tools over MCP" },
]

/** The kinds of tool, in words. */
const KIND_WORDS: Readonly<Record<string, { label: string; detail: string }>> = {
  "@read": { label: "Anything that only reads", detail: "Tools that look and change nothing" },
  "@change": { label: "Anything that changes things", detail: "Tools that set, write, send or move" },
  "@destroy": { label: "Anything that deletes", detail: "Tools whose work can't be undone" },
  "@open_world": { label: "Anything that reaches the internet", detail: "Tools that read or send outside" },
}

const RISK_WORDS = { read: "reads", change: "changes things", destroy: "deletes" } as const

/**
 * Every entry a limit can name, in groups: the kinds, the connectors whole, then each tool.
 * @param choices what the server says a limit can name
 * @param titles each connector's title by its id (an id without one is said as it is)
 * @returns the groups (an empty one left out)
 */
export function ruleGroups(choices: SafetyState["choices"], titles: Readonly<Record<string, string>>): RuleGroup[] {
  const kinds = choices.classes.map((value) => ({
    value,
    label: KIND_WORDS[value]?.label ?? value,
    detail: KIND_WORDS[value]?.detail ?? "",
  }))
  const connectors = [...new Set(choices.tools.map((t) => t.connector))].map((id) => ({
    value: `${id}:*`,
    label: `All of ${titles[id] ?? id}`,
    detail: `Every tool of ${titles[id] ?? id}`,
  }))
  const tools = choices.tools.map((t) => ({
    value: t.name,
    label: t.title || t.name,
    detail: `${titles[t.connector] ?? t.connector} · ${RISK_WORDS[t.risk] ?? t.risk}`,
  }))
  return [
    { value: "Kinds of tool", items: kinds },
    { value: "Whole connectors", items: connectors },
    { value: "Tools", items: tools },
  ].filter((group) => group.items.length)
}

/**
 * A list's entries as options: each in words, or as it is when nothing names it any more.
 * @param entries the list
 * @param groups every option
 * @returns the options, in the list's order
 */
export function asOptions(entries: readonly string[], groups: readonly RuleGroup[]): RuleOption[] {
  const known = new Map(groups.flatMap((g) => g.items).map((o) => [o.value, o]))
  return entries.map((value) => known.get(value) ?? { value, label: value, detail: "Not offered any more" })
}

/**
 * The options one list may take: all of them but what the channel's other list holds.
 * @param groups every option
 * @param other the other list of the same channel
 * @returns the groups without those entries (an emptied one left out)
 */
export function offered(groups: readonly RuleGroup[], other: readonly string[]): RuleGroup[] {
  const taken = new Set(other)
  return groups.map((g) => ({ ...g, items: g.items.filter((o) => !taken.has(o.value)) })).filter((g) => g.items.length)
}

/** How the sandbox stands: working, switched off (commands run with his full access), unavailable here (commands are
 *  refused until it is mended or switched off) or unable to give commands the network (the kernel is too old). */
export type SandboxState = "works" | "off" | "missing" | "no-network"

/**
 * How the sandbox stands, from what the server says of it.
 * @param sandbox the server's view
 * @returns its state
 */
export function sandboxState(sandbox: SafetyState["sandbox"]): SandboxState {
  if (!sandbox.commands) return "off"
  if (!sandbox.available) return "missing"
  return sandbox.available === "bwrap" && sandbox.network && sandbox.network_ok === false ? "no-network" : "works"
}
