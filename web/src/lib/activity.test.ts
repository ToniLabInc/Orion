import type { InfiniteData } from "@tanstack/react-query"
import { describe, expect, it } from "vitest"
import { activityParams, byDay, HIS_OWN, matches, outcomeTone, pastDayLabel, withLive } from "./activity"
import type { Activity, ActivityPage } from "./types"

const NOW = new Date(2026, 9, 11, 12, 0, 0) // Sunday 11 October 2026, noon
const at = (days: number, hours = 9) => new Date(2026, 9, 11 + days, hours).getTime() / 1000

const entry = (id: number, more: Partial<Activity> = {}): Activity => ({
  id,
  at: at(0),
  kind: "tool",
  source: "voice",
  actor: "you",
  connector: "web",
  tool: "get_weather",
  title: "Weather and forecast",
  detail: "location: Lisbon",
  outcome: "ok",
  turn: "t1",
  ms: 800,
  ...more,
})

const none = { kind: [], source: [], q: "" }

describe("the question a page of the log is asked with", () => {
  it("asks for a page of fifty, and names only the filters that are on", () => {
    expect(activityParams(none, null)).toBe("limit=50")
    expect(activityParams({ kind: ["deny", "block"], source: ["discord"], q: "  run command " }, 120)).toBe(
      "limit=50&before=120&kind=deny%2Cblock&source=discord&q=run+command",
    )
    expect(activityParams({ ...none, q: "   " }, null)).toBe("limit=50")
  })
})

describe("which entries a filter shows", () => {
  it("shows everything with no filter, and narrows by kind and by source", () => {
    expect(matches(entry(1), none)).toBe(true)
    expect(matches(entry(1), { ...none, kind: ["deny", "block"] })).toBe(false)
    expect(matches(entry(1, { kind: "deny" }), { ...none, kind: ["deny", "block"] })).toBe(true)
    expect(matches(entry(1), { ...none, source: ["job"] })).toBe(false)
    expect(matches(entry(1, { source: "job" }), { ...none, source: [...HIS_OWN] })).toBe(true)
  })
  it("needs every word you typed, anywhere in the entry, in any case", () => {
    expect(matches(entry(1), { ...none, q: "weather lisbon" })).toBe(true)
    expect(matches(entry(1), { ...none, q: "WEB get_weather" })).toBe(true)
    expect(matches(entry(1), { ...none, q: "weather paris" })).toBe(false)
    expect(matches(entry(1, { outcome: "denied" }), { ...none, q: "denied" })).toBe(true)
  })
})

describe("a live entry joining what is read", () => {
  const page = (...ids: number[]): ActivityPage => ({ items: ids.map((id) => entry(id)), next: null })
  const data = (...pages: ActivityPage[]): InfiniteData<ActivityPage, number | null> => ({
    pages,
    pageParams: pages.map(() => null),
  })
  it("goes on top of the first page", () => {
    const after = withLive(data(page(5, 4), page(3)), entry(6))
    expect(after.pages.map((p) => p.items.map((i) => i.id))).toEqual([[6, 5, 4], [3]])
  })
  it("is left out when the log has it already, and where nothing was read", () => {
    const before = data(page(5, 4))
    expect(withLive(before, entry(4))).toBe(before)
    const empty = data()
    expect(withLive(empty, entry(1))).toBe(empty)
  })
})

describe("words and days", () => {
  it("tells how an outcome ended", () => {
    for (const word of ["ok", "yes", "done"]) expect(outcomeTone(word)).toBe("ok")
    for (const word of ["failed", "denied", "no", "blocked"]) expect(outcomeTone(word)).toBe("alert")
    expect(outcomeTone("timeout")).toBe("dusk")
    expect(outcomeTone("")).toBe("neutral")
    expect(outcomeTone("Failed")).toBe("alert")
  })
  it("names a past day", () => {
    const day = (days: number) => new Date(2026, 9, 11 + days, 8)
    expect(pastDayLabel(day(0), NOW)).toBe("Today")
    expect(pastDayLabel(day(-1), NOW)).toBe("Yesterday")
    expect(pastDayLabel(day(-3), NOW)).toBe("Thursday")
    expect(pastDayLabel(day(-9), NOW)).toBe("Fri 2 Oct")
    expect(pastDayLabel(new Date(2025, 11, 20), NOW)).toBe("Sat 20 Dec 2025")
  })
  it("groups entries by day, newest first, keeping each day's order", () => {
    const days = byDay(
      [entry(5, { at: at(0, 11) }), entry(4, { at: at(0, 8) }), entry(3, { at: at(-1) }), entry(2, { at: at(-9) })],
      NOW,
    )
    expect(days.map((d) => [d.label, d.items.map((i) => i.id)])).toEqual([
      ["Today", [5, 4]],
      ["Yesterday", [3]],
      ["Fri 2 Oct", [2]],
    ])
  })
})
