/**
 * Today: your day and tomorrow at a glance, in time order: timers counting down (a ring emptying), reminders, calendar
 * events (one going on now marked), and the next run of each job. A row opens the card it comes from. "Show me today"
 * summons it (by voice, or the palette).
 */
import { Bell, CalendarClock, CalendarDays, Hourglass, Sunrise } from "lucide-react"
import { motion } from "motion/react"
import { type ReactNode, useEffect, useMemo } from "react"
import { useShallow } from "zustand/react/shallow"
import { useCalendar } from "@/lib/api"
import { clockTime, countdown, timeLeft, timerLabel, untilInWords } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { stagger } from "@/lib/motion"
import { type CardId, present } from "@/lib/stage"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { ProgressRing } from "@/ui/progress-ring"
import { Empty } from "@/ui/states"
import { type AgendaItem, agenda } from "./agenda"
import type { CardProps } from "./card"

/**
 * The Today card.
 * @param props the card's
 * @returns the body
 */
export default function Today({ setNote }: CardProps) {
  const { reminders, jobs } = useLive(useShallow((s) => ({ reminders: s.reminders, jobs: s.jobs })))
  const events = useCalendar().data?.events
  const soon = reminders.some((r) => r.kind === "timer" && r.due * 1000 - Date.now() < 3_600_000)
  const now = useNow(soon ? 1000 : 30_000)
  const days = useMemo(
    () => agenda({ reminders, events: events ?? [], jobs: jobs ?? [] }, now),
    [reminders, events, jobs, now],
  )
  const label = now.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })
  useEffect(() => setNote(label), [label, setNote])
  if (!days.length)
    return <Empty icon={<Sunrise />}>Nothing on today or tomorrow: no events, reminders, timers or jobs.</Empty>
  return (
    <div className="grid gap-3 px-3 pt-1 pb-3" data-testid="widget-today">
      {days.map((day) => (
        <section key={day.key} aria-label={day.label}>
          <h3 className="caps mb-1 px-1 text-steel-500">{day.label}</h3>
          <motion.ol
            initial="hidden"
            animate="shown"
            variants={{ shown: { transition: stagger(0.03) } }}
            className="grid gap-0.5"
          >
            {day.items.map((item) => (
              <motion.li key={item.id} variants={{ hidden: { opacity: 0, x: -6 }, shown: { opacity: 1, x: 0 } }}>
                <Row item={item} now={now} />
              </motion.li>
            ))}
          </motion.ol>
        </section>
      ))}
    </div>
  )
}

/** What a row shows: its time, its mark, its words, a figure on the right, and the card it opens. */
interface RowLook {
  time: string
  mark: ReactNode
  title: string
  detail?: string
  end?: ReactNode
  card: CardId
}

/**
 * A row's look.
 * @param item the agenda's item
 * @param now the time now
 * @returns how it shows
 */
function look(item: AgendaItem, now: Date): RowLook {
  const time = clockTime(new Date(item.at), false)
  switch (item.kind) {
    case "timer":
      return {
        time,
        mark: (
          <ProgressRing value={timeLeft(item.reminder, now)} size={24} stroke={2.25} tone="dusk" glide={0}>
            <Hourglass aria-hidden="true" className="size-3 text-dusk-300" />
          </ProgressRing>
        ),
        title: timerLabel(item.reminder),
        end: <span className="font-mono text-sm text-dusk-200 tabular-nums">{countdown(item.reminder.due, now)}</span>,
        card: "timers",
      }
    case "reminder":
      return {
        time,
        mark: <Bell aria-hidden="true" className="size-4 text-rigel-300" />,
        title: item.reminder.text,
        detail: untilInWords(item.reminder.due, now),
        card: "timers",
      }
    case "event": {
      const { event } = item
      return {
        time: event.all_day ? "All day" : clockTime(new Date(Date.parse(event.start)), false),
        mark: (
          <CalendarDays aria-hidden="true" className={cn("size-4", item.now ? "text-ok-400" : "text-aurora-300")} />
        ),
        title: event.title,
        detail: event.location || event.calendar,
        end: item.now ? <Badge tone="ok">Now</Badge> : undefined,
        card: "calendar",
      }
    }
    case "job":
      return {
        time,
        mark: <CalendarClock aria-hidden="true" className="size-4 text-nebula-300" />,
        title: item.job.name,
        detail: item.job.explain,
        card: "jobs",
      }
  }
}

/**
 * One row: a button that opens its card.
 * @param props the `item` and the time `now`
 * @returns the row
 */
function Row({ item, now }: { item: AgendaItem; now: Date }) {
  const l = look(item, now)
  return (
    <button
      type="button"
      onClick={() => present(l.card)}
      className="grid w-full grid-cols-[3rem_1.5rem_minmax(0,1fr)_auto] items-center gap-x-2.5 rounded-control px-1.5 py-1.5 text-left outline-none transition-colors duration-(--dur-instant) hover:bg-steel-50/5 focus-visible:outline-2 focus-visible:outline-rigel-400"
      data-testid={`today-${item.kind}`}
    >
      <span className="font-mono text-xs text-steel-400 tabular-nums">{l.time}</span>
      <span className="grid place-items-center">{l.mark}</span>
      <span className="min-w-0">
        <span className="block truncate text-sm text-steel-100">{l.title}</span>
        {l.detail && <span className="block truncate text-xs text-steel-500">{l.detail}</span>}
      </span>
      {l.end ?? <span />}
    </button>
  )
}
