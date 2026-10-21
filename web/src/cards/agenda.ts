// The Today card's agenda, kept pure for the tests: what is coming today and tomorrow in time order (timers,
// reminders, calendar events, one going on now first, and the next run of each job), grouped by day; and the days'
// names and an event's span, which the calendar card shares.
import type { CalendarEvent, Job, Reminder } from "@/lib/types"

export type AgendaItem =
  | { kind: "timer" | "reminder"; id: string; at: number; reminder: Reminder }
  | { kind: "event"; id: string; at: number; end: number; event: CalendarEvent; now: boolean }
  | { kind: "job"; id: string; at: number; job: Job }

/** A day of the agenda: its name and its items. */
interface Day {
  key: string // the day's date (YYYY-MM-DD)
  label: string // "Today", "Tomorrow", "Wednesday", "Mon 20 Oct"
  items: AgendaItem[]
}

/** How many days the agenda covers: today and tomorrow. */
const AGENDA_DAYS = 2

/**
 * When a calendar event starts and ends, in ms (an all-day event: the day's local midnight to the next).
 * @param event the event
 * @returns its start and end
 */
export function eventSpan(event: CalendarEvent): { start: number; end: number } {
  if (event.all_day) {
    const start = new Date(`${event.start.slice(0, 10)}T00:00`).getTime()
    const endDay = event.end ? new Date(`${event.end.slice(0, 10)}T00:00`).getTime() : start + 86_400_000
    return { start, end: Math.max(endDay, start + 86_400_000) }
  }
  const start = Date.parse(event.start)
  const end = event.end ? Date.parse(event.end) : start
  return { start, end: Number.isNaN(end) ? start : end }
}

/** A local date as YYYY-MM-DD. */
const dayKey = (at: Date) =>
  `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")}`

/**
 * A day's name as the agenda says it.
 * @param at a moment of the day
 * @param now the time now
 * @returns "Today", "Tomorrow", the weekday within a week, else a short date
 */
export function dayLabel(at: Date, now: Date): string {
  const days = Math.round(
    (new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime() -
      new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()) /
      86_400_000,
  )
  if (days <= 0) return "Today"
  if (days === 1) return "Tomorrow"
  if (days < 7) return at.toLocaleDateString("en-GB", { weekday: "long" })
  return at.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })
}

/**
 * The agenda: what is coming until the end of the last day it covers (a timer already due stays, on today).
 * @param input the timers and reminders, the calendars' events and the jobs
 * @param now the time now
 * @param days how many days, today included
 * @returns the days with their items, in time order
 */
export function agenda(
  input: { reminders: readonly Reminder[]; events: readonly CalendarEvent[]; jobs: readonly Job[] },
  now: Date,
  days = AGENDA_DAYS,
): Day[] {
  const t = now.getTime()
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate() + days).getTime()
  const coming: AgendaItem[] = []
  for (const reminder of input.reminders)
    if (reminder.due * 1000 < end)
      coming.push({ kind: reminder.kind, id: `r-${reminder.id}`, at: reminder.due * 1000, reminder })
  for (const event of input.events) {
    const span = eventSpan(event)
    if (Number.isNaN(span.start) || span.end <= t || span.start >= end) continue
    coming.push({
      kind: "event",
      id: `e-${event.id}`,
      at: Math.max(span.start, t),
      end: span.end,
      event,
      now: span.start <= t,
    })
  }
  for (const job of input.jobs)
    if (job.enabled && job.next_run && job.next_run * 1000 < end)
      coming.push({ kind: "job", id: `j-${job.id}`, at: job.next_run * 1000, job })
  coming.sort((a, b) => a.at - b.at || a.id.localeCompare(b.id))
  const out: Day[] = []
  for (const item of coming) {
    const when = new Date(Math.max(item.at, t))
    const key = dayKey(when)
    const day = out.at(-1)
    if (day?.key === key) day.items.push(item)
    else out.push({ key, label: dayLabel(when, now), items: [item] })
  }
  return out
}
