// Which card a tool fills: his weather tool fills the weather card, a timer the timers card, and so on. The stage
// sends a meteor from the limb to that card while the tool runs, and summons it (transient) when the answer arrives,
// so an answer materializes where it belongs. Pure, for the tests.
import type { CardId } from "./stage"

/** His own tools by name. */
const BY_NAME: Readonly<Record<string, CardId>> = {
  get_weather: "weather",
  get_news: "news",
  set_timer: "timers",
  set_reminder: "timers",
  cancel_timer_or_reminder: "timers",
  list_reminders: "timers",
  todo: "tasks",
  system_status: "vitals",
  top_processes: "vitals",
  storage_report: "vitals",
}

/** Whole servers that fill one card. */
const BY_SERVER: Readonly<Record<string, CardId>> = {
  automation: "jobs",
  jobs: "jobs",
  calendar: "calendar",
  eyes: "eyes",
  hands: "hands",
}

/** A tool another server lends (its name has the server in front) still fills a card by what it is called. */
const BY_WORDS: readonly [RegExp, CardId][] = [
  [/weather|forecast/, "weather"],
  [/calendar|agenda|event/, "calendar"],
  [/timer|reminder/, "timers"],
  [/\bjobs?\b|schedule/, "jobs"],
  [/screen|camera|frame/, "eyes"],
]

/**
 * The card a tool's answer belongs on.
 * @param name the tool's model-visible name
 * @param server its server
 * @returns the card, or null for a tool no card shows
 */
export function cardFor(name: string, server: string): CardId | null {
  const own = BY_NAME[name]
  if (own) return own
  const whole = BY_SERVER[server]
  if (whole) return whole
  const words = name.replaceAll("_", " ").toLowerCase()
  return BY_WORDS.find(([pattern]) => pattern.test(words))?.[1] ?? null
}

/** Cards whose content a tool's answer is: they are summoned when it arrives. (Hands and eyes follow you instead.) */
export const ANSWER_CARDS: ReadonlySet<CardId> = new Set([
  "weather",
  "news",
  "timers",
  "tasks",
  "vitals",
  "calendar",
  "jobs",
])

/**
 * Whether you asked him the time ("what time is it?"): he writes the clock in light for a moment.
 * @param text what you said
 * @returns true for a question about the time
 */
export const asksTheTime = (text: string) =>
  /\b(what(?:'s| is)? the time|what time is it|time is it|tell me the time)\b/i.test(text)
