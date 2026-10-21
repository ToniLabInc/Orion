import { describe, expect, it } from "vitest"
import type { CalendarEvent, Job, Reminder } from "@/lib/types"
import { agenda, dayLabel, eventSpan } from "./agenda"

const NOW = new Date(2026, 9, 11, 12, 0, 0) // Sunday 11 October 2026, noon
const s = (date: Date) => date.getTime() / 1000
const at = (days: number, hours: number, minutes = 0) => new Date(2026, 9, 11 + days, hours, minutes)

const timer = (id: number, due: Date): Reminder => ({ id, text: "", due: s(due), kind: "timer", ts: s(NOW) - 60 })
const event = (id: string, start: Date, end: Date, more: Partial<CalendarEvent> = {}): CalendarEvent => ({
  id,
  source: "google",
  calendar: "Work",
  title: id,
  start: start.toISOString(),
  end: end.toISOString(),
  all_day: false,
  location: "",
  ...more,
})
const job = (id: number, next: Date | null, enabled = true) =>
  ({ id, name: `job ${id}`, enabled, next_run: next && s(next) }) as Job

describe("the Today card's agenda", () => {
  it("lists today and tomorrow by day, in time order, and nothing later", () => {
    const days = agenda(
      {
        reminders: [timer(1, at(0, 12, 10)), timer(2, at(1, 9)), timer(3, at(2, 9))],
        events: [event("standup", at(0, 14), at(0, 14, 30)), event("past", at(0, 9), at(0, 10))],
        jobs: [job(7, at(1, 7, 30)), job(8, null), job(9, at(0, 13), false), job(10, at(30, 9))],
      },
      NOW,
    )
    expect(days.map((d) => d.label)).toEqual(["Today", "Tomorrow"])
    expect(days[0].items.map((i) => i.id)).toEqual(["r-1", "e-standup"])
    expect(days[1].items.map((i) => i.id)).toEqual(["j-7", "r-2"])
  })

  it("keeps a timer already due on today, until his announcement takes it away", () => {
    const days = agenda({ reminders: [timer(4, at(0, 11, 59))], events: [], jobs: [] }, NOW)
    expect(days).toMatchObject([{ label: "Today", items: [{ id: "r-4" }] }])
  })

  it("shows an event going on now at the top of today, and an all-day one for its whole day", () => {
    const days = agenda(
      {
        reminders: [],
        events: [
          event("lunch", at(0, 11, 30), at(0, 12, 30)),
          { ...event("holiday", at(0, 0), at(1, 0)), all_day: true, start: "2026-10-11", end: "2026-10-12" },
        ],
        jobs: [],
      },
      NOW,
    )
    expect(days[0].items.map((i) => i.kind === "event" && i.now)).toEqual([true, true])
    expect(eventSpan({ all_day: true, start: "2026-10-11", end: "" } as CalendarEvent).end).toBe(
      new Date(2026, 9, 12).getTime(),
    )
  })

  it("names the days", () => {
    expect(dayLabel(at(0, 18), NOW)).toBe("Today")
    expect(dayLabel(at(1, 8), NOW)).toBe("Tomorrow")
    expect(dayLabel(at(3, 8), NOW)).toBe("Wednesday")
    expect(dayLabel(at(9, 8), NOW)).toBe("Tue 20 Oct")
  })
})
