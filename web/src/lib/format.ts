// Words and numbers as the HUD writes them: clocks and countdowns, the trace's targets, durations and summaries, and
// the rule that turns an image in a reply into a link. Pure functions (format.test.ts).
import type { Reminder } from "./types"

/** 14:05:09, always 24-hour. */
export const clockTime = (date: Date, seconds = true) =>
  date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds && { second: "2-digit" }),
    hourCycle: "h23",
  })

/** Wed 1 Oct */
export const shortDate = (date: Date) =>
  date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })

// ---------- timers and reminders (due times in epoch seconds, as the organizer keeps them) ----------
/** What a countdown shows: 4:05 under an hour; after that the time it's due, with the weekday if not today. */
export function countdown(due: number, now: Date): string {
  const left = Math.ceil(due - now.getTime() / 1000)
  if (left <= 0) return "now"
  if (left < 3600) return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`
  const when = new Date(due * 1000)
  const time = clockTime(when, false)
  return when.toDateString() === now.toDateString()
    ? time
    : `${when.toLocaleDateString([], { weekday: "short" })} ${time}`
}

/** How long until it's due, in words: "in 2 h 59 min", "in 40 min", "in 3 days". */
export function untilInWords(due: number, now: Date): string {
  const minutes = Math.round((due - now.getTime() / 1000) / 60)
  if (minutes < 1) return "any moment"
  if (minutes < 60) return `in ${minutes} min`
  if (minutes < 48 * 60) {
    const h = Math.floor(minutes / 60)
    const m = minutes % 60
    return m ? `in ${h} h ${m} min` : `in ${h} h`
  }
  return `in ${Math.round(minutes / 1440)} days`
}

/** How long ago, in words: "just now", "12 min ago", "at 09:41" (today), else the day ("Wed 1 Oct"). */
export function agoInWords(at: number, now: Date): string {
  const minutes = Math.floor((now.getTime() / 1000 - at) / 60)
  if (minutes < 1) return "just now"
  if (minutes < 60) return `${minutes} min ago`
  const when = new Date(at * 1000)
  return when.toDateString() === now.toDateString() ? `at ${clockTime(when, false)}` : shortDate(when)
}

/** A timer's name, or its length when it has none: "pasta", "10 min timer", "90 s timer", "1.5 h timer". */
export function timerLabel(r: Reminder): string {
  if (r.text) return r.text
  const s = Math.round(r.due - r.ts)
  if (s < 60) return `${s} s timer`
  if (s < 3600) return `${Math.round(s / 60)} min timer`
  return `${(s / 3600).toFixed(1).replace(/\.0$/, "")} h timer`
}

/** How much of a timer is left: 1 when it starts, 0 when it's due. */
export function timeLeft(r: Reminder, now: Date): number {
  const total = r.due - r.ts
  return total > 0 ? Math.min(1, Math.max(0, (r.due - now.getTime() / 1000) / total)) : 0
}

// ---------- the OPERATIONS trace ----------
/** The words of an operation's arguments the trace shows ("weather Lisbon", "10 min"), at most 80 characters. */
export function stepTarget(args: Record<string, unknown>): string {
  const clip = (text: string) => {
    const flat = text.replace(/\s+/g, " ").trim()
    return flat.length <= TARGET_CHARS ? flat : `${flat.slice(0, TARGET_CHARS - 1)}…`
  }
  for (const key of TARGET_KEYS) {
    const value = args[key]
    if (typeof value === "string" && value.trim()) return clip(value)
  }
  for (const [key, unit] of UNIT_KEYS) {
    const value = args[key]
    if (typeof value === "number") return `${value} ${unit}`
  }
  const first = Object.values(args).find((value) => value !== null && value !== undefined && value !== "")
  if (first === undefined) return ""
  return clip(typeof first === "object" ? JSON.stringify(first) : String(first))
}

const TARGET_CHARS = 80
const TARGET_KEYS = ["query", "q", "url", "text", "title", "name", "city", "location", "path", "memories", "duration"]
const UNIT_KEYS: [string, string][] = [
  ["minutes", "min"],
  ["seconds", "s"],
  ["hours", "h"],
  ["changes", "changes"],
]

/** How long an operation took: "0.8 s" below ten seconds, "12 s" below a minute, then "1 m 04 s". */
export function stepDuration(ms: number): string {
  if (ms < 10_000) return `${(Math.max(0, ms) / 1000).toFixed(1)} s`
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s} s`
  return `${Math.floor(s / 60)} m ${String(s % 60).padStart(2, "0")} s`
}

interface Timed {
  at: number // ms
  ms?: number
  kind?: string
  status?: string
  result?: string
}

/** The wall-clock span of some operations, first start to last end (parallel calls aren't counted twice). */
export function traceSpan(steps: Timed[], now = Date.now()): string {
  if (!steps.length) return stepDuration(0)
  const start = Math.min(...steps.map((st) => st.at))
  const end = Math.max(...steps.map((st) => st.at + (st.ms ?? now - st.at)))
  return stepDuration(end - start)
}

/** A folded trace in one line: "4 operations · 3.1 s · 1 memory saved · 2 recalled · 1 failed" (zeros left out). */
export function traceSummary(steps: Timed[], now = Date.now()): string {
  const saved = steps.filter((st) => st.kind === "memory" && st.status === "ok").length
  const recalled = steps
    .filter((st) => st.kind === "recall")
    .reduce((n, st) => n + Math.max(1, (st.result ?? "").split("\n").filter((line) => line.startsWith("•")).length), 0)
  const failed = steps.filter((st) => st.status === "failed").length
  return [
    `${steps.length} operation${steps.length === 1 ? "" : "s"}`,
    traceSpan(steps, now),
    saved && `${saved} ${saved === 1 ? "memory" : "memories"} saved`,
    recalled && `${recalled} recalled`,
    failed && `${failed} failed`,
  ]
    .filter(Boolean)
    .join(" · ")
}

// ---------- money and sizes ----------
/**
 * Dollars as Settings writes them: "$0" for nothing, "<$0.01" for a fraction of a cent, cents below a hundred
 * dollars, whole dollars above.
 * @param usd the amount
 * @returns its words
 */
export function money(usd: number): string {
  if (usd <= 0) return "$0"
  if (usd < 0.01) return "<$0.01"
  if (usd < 100) return `$${usd.toFixed(2)}`
  return `$${Math.round(usd).toLocaleString("en")}`
}

/**
 * Dollars to a hundredth of a cent, for what a benchmark cost (a run is often a few cents): "$0", "<$0.0001",
 * "$0.0123", and as `money` from a dollar up.
 * @param usd the amount
 * @returns its words
 */
export function fineMoney(usd: number): string {
  if (usd <= 0) return "$0"
  if (usd < 0.0001) return "<$0.0001"
  return usd < 1 ? `$${usd.toFixed(4)}` : money(usd)
}

/**
 * When OpenRouter's day starts (midnight UTC) on this PC's clock: "03:00" in Helsinki in summer, "00:00" in London in
 * winter.
 * @param now now
 * @returns the time on the wall clock here
 */
export function utcMidnight(now: Date): string {
  const midnight = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))
  return clockTime(midnight, false)
}

/**
 * A file's size: "820 B", "34 KB", "1.2 MB", "4.7 GB" (powers of 1000, as disks are sold).
 * @param size bytes
 * @returns its words
 */
export function bytes(size: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"]
  let value = Math.max(0, size)
  let unit = 0
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000
    unit++
  }
  return `${unit && value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`
}

// ---------- images in replies ----------
/** An image in a reply, never fetched: a link saying what it is and where (`label` = its words and its server), or
 * just its words when it is inside a link or its address isn't a web page's. */
export type ImageLink = { href: string; label: string; words: string; host: string } | { text: string }

/** How to show an image a reply names (see ImageLink). */
export function imageLink(src: string | undefined, alt: string | undefined, base: string, inLink = false): ImageLink {
  const words = alt?.trim() || "Image"
  if (inLink || !src) return { text: words }
  const url = URL.parse(src, base)
  if (!url || (url.protocol !== "https:" && url.protocol !== "http:") || !url.host) return { text: words }
  return { href: url.href, label: `${words} ${url.host}`, words, host: url.host }
}
