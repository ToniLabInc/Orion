// What a remote's gesture settings need to edit a binding, kept pure for the tests: the gestures in the order they
// are listed, a fresh binding when the action changes, the holds a hand shape can wait for, the choices of an option
// (with one written by hand in config.yaml kept), and a tool's simple arguments. The words, the choices and what a
// new binding starts with all come from the server (GET /api/gestures).
import type { ActionChoice, GestureAction, GestureBinding, GestureInfo, GestureRemote, GestureTool } from "@/lib/types"

/** Which kinds of gesture are listed, and under what. */
export const KINDS: { kind: string; title: string; hint: string }[] = [
  {
    kind: "shape",
    title: "Hand shapes",
    hint: "Hold a shape still for a moment: it counts once, then again only if you show it again.",
  },
  { kind: "motion", title: "Motions", hint: "One quick movement of one hand." },
  { kind: "two_hands", title: "Both hands", hint: "Both hands up in front of the camera." },
]

/** A tool as one select's value: its server and its name. */
export const toolKey = (server: string, tool: string) => `${server}/${tool}`

/**
 * The gestures of a remote in the listed order, by kind: what it says it recognises, then any gesture only its
 * bindings name (a remote that has never said yet still lists those, as other gestures).
 * @param gestures what it recognises
 * @param bindings what its gestures do
 * @returns each kind's gestures
 */
export function byKind(
  gestures: GestureInfo[],
  bindings: Record<string, GestureBinding>,
): { kind: string; gestures: GestureInfo[] }[] {
  const known = new Set(gestures.map((g) => g.name))
  const extra = Object.keys(bindings)
    .filter((name) => !known.has(name))
    .map((name) => ({ name, title: name.replaceAll("_", " "), kind: "", how: "" }))
  const all = [...gestures, ...extra]
  const groups = KINDS.map(({ kind }) => ({ kind, gestures: all.filter((g) => g.kind === kind) }))
  const other = all.filter((g) => !KINDS.some((k) => k.kind === g.kind))
  if (other.length) groups.push({ kind: "", gestures: other })
  return groups.filter((group) => group.gestures.length)
}

/**
 * A fresh binding for a newly chosen action: what the server starts it with, its options kept from the binding before
 * when that had the same action, and the hold of the binding before; repeating on for an action that repeats.
 * @param action the action
 * @param spec what the server says of it (its options, what it starts with)
 * @param before what the gesture did until now
 * @param shape whether the gesture is a hand shape (hold and repeat apply only to those)
 * @returns the binding
 */
export function freshBinding(
  action: string,
  spec: GestureAction | undefined,
  before: GestureBinding | undefined,
  shape: boolean,
): GestureBinding {
  const binding: GestureBinding = structuredClone(spec?.start ?? { do: action })
  if (before?.do === action)
    for (const param of spec?.params ?? [])
      if (before[param.name] !== undefined) binding[param.name] = before[param.name]
  if (shape) {
    if (before?.hold) binding.hold = before.hold
    if (spec?.repeats) binding.repeat = true
  }
  return binding
}

/**
 * How long a hand shape may be asked to be held: as soon as it counts, or one of the steps the remote reports a held
 * shape at (and the binding's own, when config.yaml says another).
 * @param remote the gesture remote
 * @param hold the binding's hold now
 * @returns the holds in seconds, 0 first
 */
export function holds(remote: Pick<GestureRemote, "hold_steps">, hold: number | undefined): number[] {
  const steps = [0, ...remote.hold_steps]
  return hold && !steps.includes(hold) ? [...steps, hold].toSorted((a, b) => a - b) : steps
}

/**
 * An option's choices, with its value now among them even when config.yaml has one the server doesn't offer.
 * @param choices what the server offers
 * @param value the option's value now
 * @returns the choices
 */
export function withValue(choices: ActionChoice[], value: unknown): ActionChoice[] {
  if (typeof value !== "string" && typeof value !== "number") return choices
  return choices.some((c) => c.value === value) ? choices : [...choices, { value, label: String(value) }]
}

/** One argument of a tool with a plain control. */
export interface SimpleArg {
  name: string
  type: "string" | "number" | "integer" | "boolean"
  choices?: string[]
  required: boolean
  description: string
}

/**
 * A tool's arguments, when every one is plain (text, a number, true or false, or one of a few texts): each is then
 * edited with its own control; otherwise (null) they are edited as JSON.
 * @param tool the tool
 * @returns its arguments, or null
 */
export function simpleArgs(tool: GestureTool | undefined): SimpleArg[] | null {
  const schema = tool?.schema as
    | { properties?: Record<string, Record<string, unknown>>; required?: string[] }
    | undefined
  if (!schema) return null
  const required = new Set(schema.required ?? [])
  const out: SimpleArg[] = []
  for (const [name, prop] of Object.entries(schema.properties ?? {})) {
    const choices =
      Array.isArray(prop.enum) && prop.enum.every((v) => typeof v === "string") ? (prop.enum as string[]) : undefined
    const type = prop.type
    if (type !== "string" && type !== "number" && type !== "integer" && type !== "boolean") return null
    out.push({ name, type, choices, required: required.has(name), description: String(prop.description ?? "") })
  }
  return out
}
