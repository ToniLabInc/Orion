// What Customize needs to know about connectors, skills and plugins, kept pure for the tests: a status in words, the
// groups the lists fall into, the words a search matches, what each channel's limits make of a connector, a settings
// form's draft (what is typed, what changed, what is wrong), and a custom connector's command line or URL as the
// server's entry.
import type { BadgeTone } from "@/ui/badge"
import type {
  ChannelRules,
  Connector,
  ConnectorField,
  ConnectorStatus,
  DirectoryItem,
  NewConnector,
  Plugin,
  PluginAuthor,
  Skill,
} from "./types"

/** A status as the pages say it: the word, and the badge's tone. */
export const STATUS: Readonly<Record<ConnectorStatus, { label: string; tone: BadgeTone }>> = {
  connected: { label: "Connected", tone: "ok" },
  needs_sign_in: { label: "Not connected", tone: "dusk" },
  needs_setup: { label: "Needs setup", tone: "dusk" },
  connecting: { label: "Connecting", tone: "rigel" },
  error: { label: "Error", tone: "alert" },
  off: { label: "Off", tone: "neutral" },
  unavailable: { label: "Not on this machine", tone: "neutral" },
}

/** What a connector's trust means, in a sentence. */
export const TRUST_WORDS: Readonly<Record<Connector["trust"], string>> = {
  orion: "Part of Orion: his own code.",
  reviewed: "Reviewed: a version we checked, pinned so it can't change under you.",
  unreviewed: "Not reviewed: its code is someone else's, and it gets only what you give it.",
}

/** The groups a list of connectors falls into, in the order they are shown. */
const CONNECTOR_GROUPS: readonly { kind: Connector["kind"][]; title: string; about: string }[] = [
  { kind: ["builtin"], title: "His own", about: "What he can do on his own" },
  { kind: ["catalog", "custom"], title: "Added", about: "Services and servers you connected" },
  { kind: ["plugin"], title: "From plugins", about: "Brought by the plugins you installed" },
]

/**
 * Whether a connector matches what was typed: every word is in its title, its description or its tools' names.
 * @param c the connector
 * @param query what was typed
 * @returns true when it matches (everything does for an empty query)
 */
export function matchesConnector(c: Connector, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (!words.length) return true
  const text = [c.title, c.description, c.id, ...c.tools.map((t) => t.title || t.name)].join(" ").toLowerCase()
  return words.every((word) => text.includes(word))
}

/** How soon a status wants a look: a failure first, then what waits for you, then what works, then what is off. */
const ATTENTION: Readonly<Record<ConnectorStatus, number>> = {
  error: 0,
  needs_sign_in: 1,
  needs_setup: 1,
  connecting: 2,
  connected: 3,
  off: 4,
  unavailable: 4,
}

/**
 * The connectors in their groups (an empty group left out), what needs a look first, then by title.
 * @param items the connectors
 * @param query what was typed to narrow them
 * @returns the groups
 */
export function groupConnectors(
  items: readonly Connector[],
  query = "",
): { title: string; about: string; items: Connector[] }[] {
  const shown = items.filter((c) => matchesConnector(c, query))
  return CONNECTOR_GROUPS.map((g) => ({
    title: g.title,
    about: g.about,
    items: shown
      .filter((c) => g.kind.includes(c.kind))
      .toSorted((a, b) => ATTENTION[a.status] - ATTENTION[b.status] || a.title.localeCompare(b.title)),
  })).filter((g) => g.items.length)
}

/**
 * What a connector's tools are worth in words: how many are on out of how many.
 * @param c the connector
 * @returns "12 tools", "3 of 12 tools on", "No tools yet"
 */
export function toolsWords(c: Pick<Connector, "tools">): string {
  const total = c.tools.length
  if (!total) return "No tools yet"
  const on = c.tools.filter((t) => t.enabled).length
  const noun = `tool${total === 1 ? "" : "s"}`
  return on === total ? `${total} ${noun}` : `${on} of ${total} ${noun} on`
}

/** A connector may be used from a channel, only after asking, or never. */
export type Stance = "allowed" | "asks" | "never"

/**
 * What a channel's limits make of a connector as a whole (`<id>:*`), and how many of its tools they name one by one.
 * @param c the connector
 * @param rules the channel's limits
 * @returns its stance, and the number of its own tools named alone
 */
export function stanceOf(c: Pick<Connector, "id" | "tools">, rules: ChannelRules): { stance: Stance; named: number } {
  const whole = `${c.id}:*`
  const stance: Stance = rules.never.includes(whole) ? "never" : rules.confirm.includes(whole) ? "asks" : "allowed"
  const named = c.tools.filter((t) => rules.never.includes(t.name) || rules.confirm.includes(t.name)).length
  return { stance, named }
}

/**
 * A channel's limits with a connector's stance changed (its tools named alone stay as they are).
 * @param id the connector's id
 * @param rules the channel's limits
 * @param stance the new stance
 * @returns the new limits
 */
export function withStance(id: string, rules: ChannelRules, stance: Stance): ChannelRules {
  const whole = `${id}:*`
  const now: Stance = rules.never.includes(whole) ? "never" : rules.confirm.includes(whole) ? "asks" : "allowed"
  if (now === stance) return rules
  const never = rules.never.filter((e) => e !== whole)
  const confirm = rules.confirm.filter((e) => e !== whole)
  return {
    never: stance === "never" ? [...never, whole] : never,
    confirm: stance === "asks" ? [...confirm, whole] : confirm,
  }
}

// ---------- a connector's settings form ----------
/** What is typed in a settings form, by field: text and secrets and numbers as typed, a switch as a boolean, a list as a
 *  list of strings. */
export type Draft = Record<string, string | boolean | string[]>

/**
 * The form as it opens: each field's saved value (a secret's is never sent: it opens empty).
 * @param fields the fields
 * @param values their saved values
 * @returns the draft
 */
export function openDraft(fields: readonly ConnectorField[], values: Record<string, unknown>): Draft {
  const draft: Draft = {}
  for (const f of fields) {
    const v = values[f.key]
    if (f.secret || f.kind === "secret") draft[f.key] = ""
    else if (f.kind === "switch") draft[f.key] = v === true
    else if (f.kind === "list") draft[f.key] = Array.isArray(v) ? v.map(String) : []
    else draft[f.key] = v === undefined || v === null ? "" : String(v)
  }
  return draft
}

/**
 * The fields that changed since the form opened (a secret only when something is typed in it).
 * @param fields the fields
 * @param values their saved values
 * @param draft what is typed
 * @returns their keys
 */
export function changedKeys(
  fields: readonly ConnectorField[],
  values: Record<string, unknown>,
  draft: Draft,
): string[] {
  const was = openDraft(fields, values)
  return fields.filter((f) => JSON.stringify(draft[f.key]) !== JSON.stringify(was[f.key])).map((f) => f.key)
}

/**
 * What is wrong in the form, by field: a required one left empty (a secret already set counts as filled), a number
 * that isn't one.
 * @param fields the fields
 * @param draft what is typed
 * @returns the problems, in words
 */
export function formProblems(fields: readonly ConnectorField[], draft: Draft): Record<string, string> {
  const out: Record<string, string> = {}
  for (const f of fields) {
    const v = draft[f.key]
    const empty = v === "" || v === undefined || (Array.isArray(v) && !v.length)
    if (f.kind === "number" && typeof v === "string" && v.trim() && !Number.isFinite(Number(v))) out[f.key] = "A number"
    else if (f.required && empty && !(f.secret && f.set)) out[f.key] = "Required"
  }
  return out
}

/**
 * The values to save: the fields that changed, a number as a number.
 * @param fields the fields
 * @param values their saved values
 * @param draft what is typed
 * @returns the values (only the changed fields)
 */
export function formValues(
  fields: readonly ConnectorField[],
  values: Record<string, unknown>,
  draft: Draft,
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  const changed = new Set(changedKeys(fields, values, draft))
  for (const f of fields) {
    if (!changed.has(f.key)) continue
    const v = draft[f.key]
    out[f.key] = f.kind === "number" && typeof v === "string" ? (v.trim() ? Number(v) : null) : v
  }
  return out
}

// ---------- adding a connector of your own ----------
/** A connector's name: lower-case letters, digits, - and _ (the server's own rule). */
const CONNECTOR_NAME = /^[a-z0-9][a-z0-9_-]{0,40}$/

/**
 * Split arguments as a shell would for simple cases: spaces, and quotes round one that has spaces.
 * @param text the command line
 * @returns its words
 */
export const splitArgs = (text: string): string[] =>
  [...text.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)].map((m) => m[1] ?? m[2] ?? m[3])

/**
 * KEY=VALUE lines (or `Key: Value`, the way headers are written) as a mapping.
 * @param text the lines
 * @returns the mapping, or the number of the first line that isn't one
 */
export function pairs(text: string): Record<string, string> | number {
  const out: Record<string, string> = {}
  for (const [i, line] of text.split("\n").entries()) {
    if (!line.trim()) continue
    const match = /^\s*([^=:\s]+)\s*[=:]\s*(.*?)\s*$/.exec(line)
    if (!match) return i + 1
    out[match[1]] = match[2]
  }
  return out
}

/** Whether the line a connector is added with is a URL (else it is a command). */
export const isUrl = (where: string) => /^https?:\/\//i.test(where.trim())

/**
 * The form of a custom connector as the server's entry: a URL (the pairs are its headers), or a command with its
 * arguments (the pairs are its environment).
 * @param form the name, the command or URL, and the KEY=VALUE lines
 * @param taken the names already used
 * @returns the entry, or what is wrong in words
 */
export function customConnector(
  form: { name: string; where: string; extra: string },
  taken: readonly string[],
): { body: NewConnector } | { problem: string } {
  const name = form.name.trim()
  const where = form.where.trim()
  if (!CONNECTOR_NAME.test(name)) return { problem: "A name of lower-case letters, digits, - and _, up to 41 of them." }
  if (taken.includes(name)) return { problem: `There's already a connector called ${name}.` }
  if (!where) return { problem: "Type the command that starts it, or its URL." }
  const more = pairs(form.extra)
  const url = isUrl(where)
  if (typeof more === "number")
    return { problem: `Line ${more} of ${url ? "the headers" : "the environment"}: write KEY=VALUE.` }
  const some = Object.keys(more).length > 0
  if (url) return { body: { from: "custom", name, url: where, ...(some && { headers: more }) } }
  const [command, ...args] = splitArgs(where)
  return { body: { from: "custom", name, command, args, ...(some && { env: more }) } }
}

// ---------- skills and plugins ----------
/** The groups skills fall into, in the order they are shown. */
const SKILL_GROUPS: readonly { source: Skill["source"]; title: string }[] = [
  { source: "yours", title: "Yours" },
  { source: "learned", title: "Learned" },
  { source: "plugin", title: "From plugins" },
  { source: "builtin", title: "His own" },
]

/**
 * Whether a skill matches what was typed: every word is in its name, its description or its triggers.
 * @param s the skill
 * @param query what was typed
 * @returns true when it matches
 */
function matchesSkill(s: Skill, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  const text = [s.name, s.description, ...s.triggers].join(" ").toLowerCase()
  return words.every((word) => text.includes(word))
}

/**
 * The skills in their groups (an empty group left out), each by name.
 * @param items the skills
 * @param query what was typed to narrow them
 * @returns the groups
 */
export function groupSkills(items: readonly Skill[], query = ""): { title: string; items: Skill[] }[] {
  const shown = items.filter((s) => matchesSkill(s, query))
  return SKILL_GROUPS.map((g) => ({
    title: g.title,
    items: shown.filter((s) => s.source === g.source).toSorted((a, b) => a.name.localeCompare(b.name)),
  })).filter((g) => g.items.length)
}

/**
 * What a directory connector asks of you, in a few words: the fields it requires (names as they are written: they hold
 * proper nouns), or a sign-in, which for a service that gives no sign-in to unknown apps comes after an app of your own.
 * @param item the directory item
 * @returns its words
 */
export function directoryNeeds(item: Pick<DirectoryItem, "auth" | "fields">): string {
  const required = item.fields.filter((f) => f.required).map((f) => f.label)
  if (item.auth === "oauth")
    return required.length ? "Sign in, after you make an app of your own" : "Sign in with your account"
  return required.length ? `Needs ${required.join(" · ")}` : "Nothing required"
}

/**
 * What uses a secret, in words: the server names a place by its id (a connector's, `openrouter.api_key`, or
 * `calendar:<source>`).
 * @param place what the server says uses it
 * @param titles each connector's title, by id
 * @returns its name for people
 */
export function secretUser(place: string, titles: ReadonlyMap<string, string>): string {
  if (place === "openrouter.api_key") return "OpenRouter"
  if (place.startsWith("calendar:")) return `the ${place.slice("calendar:".length)} calendar`
  return titles.get(place) ?? place
}

/**
 * A plugin's author in words.
 * @param author a name, or plugin.json's object
 * @returns the name ("" for none)
 */
export const authorName = (author: PluginAuthor | undefined) =>
  typeof author === "string" ? author : (author?.name ?? "")

/**
 * A plugin's parts in a line.
 * @param p the plugin
 * @returns "3 skills · 1 connector", or "Nothing Orion uses"
 */
export function pluginParts(p: Pick<Plugin, "components">): string {
  const n = (count: number, one: string, many = `${one}s`) => (count ? `${count} ${count === 1 ? one : many}` : "")
  const parts = [
    n(p.components.skills.length + p.components.commands.length, "skill"),
    n(p.components.connectors.length, "connector"),
  ].filter(Boolean)
  return parts.length ? parts.join(" · ") : "Nothing Orion uses"
}
