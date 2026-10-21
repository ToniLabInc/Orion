// The activity log's rules, pure for the tests: its kinds and sources in words, the question a page of it is asked
// with, which entries a filter shows (the server filters the pages it sends; an entry arriving live is checked here the
// same way), how a live entry joins the pages already read, how an outcome reads, and the days the log is grouped by.
import type { InfiniteData } from "@tanstack/react-query"
import type { Activity, ActivityKind, ActivityPage, ActivitySource } from "./types"

/** What the activity log shows: some kinds and sources (none: all), and words to find. */
export interface ActivityFilter {
  kind: readonly string[]
  source: readonly string[]
  q: string
}

/** How many entries a page of the log brings. */
const ACTIVITY_PAGE = 50

/**
 * The query string of one page of the log.
 * @param filter what it shows
 * @param before the id the page ends before (null: the newest)
 * @returns the parameters
 */
export function activityParams(filter: ActivityFilter, before: number | null): string {
  const params = new URLSearchParams({ limit: String(ACTIVITY_PAGE) })
  if (before !== null) params.set("before", String(before))
  if (filter.kind.length) params.set("kind", filter.kind.join(","))
  if (filter.source.length) params.set("source", filter.source.join(","))
  if (filter.q.trim()) params.set("q", filter.q.trim())
  return params.toString()
}

/** Each kind of entry, as the log names it (in the order its filter lists them). */
export const KINDS: Readonly<Record<ActivityKind, string>> = {
  tool: "Tools",
  ask: "Questions",
  deny: "Refused",
  block: "Blocked",
  alert: "Alerts",
  job: "Jobs",
  instant: "Instant",
  backup: "Backups",
  connector: "Connectors",
  plugin: "Plugins",
  skill: "Skills",
  secret: "Secrets",
  login: "Sign-ins",
  bench: "Benchmarks",
  system: "System",
}

/** Each source, as the log names it. */
export const SOURCES: Readonly<Record<ActivitySource, string>> = {
  voice: "Voice",
  hud: "HUD",
  discord: "Discord",
  job: "Jobs",
  watcher: "Watchers",
  gesture: "Gestures",
  app: "Other apps",
  user: "You",
  system: "System",
}

/** "His own": what he did without you in front of him (scheduled jobs, the watchers, requests from Discord). */
export const HIS_OWN: readonly ActivitySource[] = ["job", "watcher", "discord"]

/**
 * Whether an entry belongs in a filtered log: one of its kinds and sources (when it names any), and every word looked
 * for somewhere in it.
 * @param item the entry
 * @param filter the log's filter
 * @returns true when it shows
 */
export function matches(item: Activity, filter: ActivityFilter): boolean {
  if (filter.kind.length && !filter.kind.includes(item.kind)) return false
  if (filter.source.length && !filter.source.includes(item.source)) return false
  const words = filter.q.toLowerCase().split(/\s+/).filter(Boolean)
  if (!words.length) return true
  const text = [item.title, item.detail, item.tool, item.connector, item.actor, item.outcome].join(" ").toLowerCase()
  return words.every((word) => text.includes(word))
}

/**
 * The pages already read, with a live entry on top (once: an entry already there is left alone).
 * @param data the pages
 * @param item the new entry
 * @returns the pages with it, or the same pages when it was there
 */
export function withLive(data: InfiniteData<ActivityPage, number | null>, item: Activity) {
  if (data.pages.some((page) => page.items.some((x) => x.id === item.id))) return data
  const [first, ...rest] = data.pages
  if (!first) return data
  return { ...data, pages: [{ ...first, items: [item, ...first.items] }, ...rest] }
}

/** How an outcome reads: done, failed or refused, run out, or nothing to say. */
export type OutcomeTone = "ok" | "alert" | "dusk" | "neutral"

const GOOD = new Set(["ok", "done", "yes", "allowed", "connected", "signed in", "installed", "saved"])
const BAD = new Set(["failed", "error", "no", "denied", "refused", "blocked"])

/**
 * The tone of an outcome.
 * @param outcome its word
 * @returns the tone
 */
export function outcomeTone(outcome: string): OutcomeTone {
  const word = outcome.trim().toLowerCase()
  if (GOOD.has(word)) return "ok"
  if (BAD.has(word)) return "alert"
  if (word === "timeout") return "dusk"
  return "neutral"
}

/**
 * A past day's name, as the log heads it.
 * @param at a moment of the day
 * @param now the time now
 * @returns "Today", "Yesterday", the weekday within a week, else the date ("Mon 5 Oct", with the year if not this one)
 */
export function pastDayLabel(at: Date, now: Date): string {
  const midnight = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const days = Math.round((midnight(now) - midnight(at)) / 86_400_000)
  if (days <= 0) return "Today"
  if (days === 1) return "Yesterday"
  if (days < 7) return at.toLocaleDateString("en-GB", { weekday: "long" })
  const date = at.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })
  return at.getFullYear() === now.getFullYear() ? date : `${date} ${at.getFullYear()}` // (en-GB adds a comma with a year)
}

/**
 * Entries by day, newest first, as the log shows them.
 * @param items the entries, newest first
 * @param now the time now
 * @returns each day's name and entries
 */
export function byDay(items: readonly Activity[], now: Date): { label: string; items: Activity[] }[] {
  const days: { label: string; items: Activity[] }[] = []
  for (const item of items) {
    const label = pastDayLabel(new Date(item.at * 1000), now)
    const day = days.at(-1)
    if (day?.label === label) day.items.push(item)
    else days.push({ label, items: [item] })
  }
  return days
}
