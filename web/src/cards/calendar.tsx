/**
 * Calendar: your agenda for the week, day by day, from the calendars he reads (his built-in calendar group: GET
 * /api/calendar). Each event with its time (or "All day"), its title, where, and its calendar's colour; one going on
 * now is marked. A calendar that couldn't be read says why; none set up says where to add one.
 */
import { CalendarX2, MapPin } from "lucide-react"
import { motion } from "motion/react"
import { useEffect, useMemo } from "react"
import { Link } from "wouter"
import { useCalendar } from "@/lib/api"
import { useNow } from "@/lib/hooks"
import { stagger } from "@/lib/motion"
import type { CalendarEvent } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { Empty, Failed, Skeleton } from "@/ui/states"
import { dayLabel, eventSpan } from "./agenda"
import type { CardProps } from "./card"

const DOTS = ["bg-aurora-400", "bg-rigel-400", "bg-nebula-400", "bg-betel-400", "bg-dusk-400", "bg-ok-400"]

/**
 * A calendar's colour: the same for the same name.
 * @param name the calendar's name
 * @returns a background class
 */
function dotOf(name: string): string {
  let hash = 0
  for (const c of name) hash = (hash * 31 + c.charCodeAt(0)) | 0
  return DOTS[Math.abs(hash) % DOTS.length] as string
}

const time = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })

/**
 * The calendar card.
 * @param props the card's
 * @returns the body
 */
export default function Calendar({ setNote }: CardProps) {
  const calendar = useCalendar()
  const now = useNow(60_000)
  const events = calendar.data?.events
  const days = useMemo(() => byDay(events ?? [], now), [events, now])
  const count = events?.length ?? 0
  useEffect(() => setNote(count ? `${count} this week` : null), [count, setNote])

  if (calendar.isError)
    return (
      <Failed
        title="The calendars can't be read"
        error={calendar.error}
        onRetry={() => void calendar.refetch()}
        className="px-4 pb-4"
      />
    )
  if (!calendar.data)
    return (
      <div className="px-4 pb-4">
        <Skeleton lines={4} />
      </div>
    )
  const broken = calendar.data.sources.filter((s) => !s.ok)
  if (!calendar.data.sources.length)
    return (
      <Empty
        icon={<CalendarX2 />}
        action={
          <Link
            href="/customize/connectors/calendar"
            className="text-sm text-rigel-300 underline-offset-2 hover:underline"
          >
            Set up the Calendar connector
          </Link>
        }
      >
        No calendar is set up yet.
      </Empty>
    )
  return (
    <div className="grid gap-3 px-3 pt-1 pb-4" data-testid="widget-calendar">
      {broken.map((s) => (
        <p key={s.name} className="rounded-control bg-alert-400/8 px-3 py-2 text-xs text-alert-200">
          {s.name}: {s.error}
        </p>
      ))}
      {days.length ? (
        days.map((day) => (
          <section key={day.label} aria-label={day.label}>
            <h3 className="caps mb-1 px-1 text-steel-500">{day.label}</h3>
            <motion.ol
              initial="hidden"
              animate="shown"
              variants={{ shown: { transition: stagger(0.03) } }}
              className="grid gap-1"
            >
              {day.events.map(({ event, start, end, now: going }) => (
                <motion.li
                  key={event.id}
                  variants={{ hidden: { opacity: 0, x: -6 }, shown: { opacity: 1, x: 0 } }}
                  className={cn(
                    "relative grid grid-cols-[3.25rem_minmax(0,1fr)] gap-x-3 rounded-control py-2 pr-2 pl-2.5",
                    going ? "bg-ok-400/7 shadow-[inset_2px_0_0_var(--color-ok-400)]" : "hover:bg-steel-50/4",
                  )}
                >
                  <span className="font-mono text-xs text-steel-300 tabular-nums">
                    {event.all_day ? "All day" : time(start)}
                    {!event.all_day && <span className="block text-steel-500">{time(end)}</span>}
                  </span>
                  <span className="min-w-0">
                    <span className="flex items-center gap-2">
                      <span
                        aria-hidden="true"
                        className={cn("size-1.5 shrink-0 rounded-full", dotOf(event.calendar))}
                      />
                      <span className="truncate text-sm text-steel-100">{event.title}</span>
                      {going && (
                        <Badge tone="ok" className="ml-auto">
                          Now
                        </Badge>
                      )}
                    </span>
                    <span className="mt-0.5 flex items-center gap-1 truncate text-micro text-steel-500">
                      {event.location && <MapPin aria-hidden="true" className="size-3 shrink-0" />}
                      <span className="truncate">{event.location || event.calendar}</span>
                    </span>
                  </span>
                </motion.li>
              ))}
            </motion.ol>
          </section>
        ))
      ) : (
        <Empty icon={<CalendarX2 />}>Nothing on your calendars this week.</Empty>
      )}
    </div>
  )
}

/**
 * The events still to come (or going on), by day.
 * @param events the calendars' events
 * @param now the time now
 * @returns the days with their events
 */
function byDay(events: CalendarEvent[], now: Date) {
  const t = now.getTime()
  const days: { label: string; events: { event: CalendarEvent; start: number; end: number; now: boolean }[] }[] = []
  const shown = events
    .map((event) => ({ event, ...eventSpan(event) }))
    .filter(({ start, end }) => !Number.isNaN(start) && end > t)
    .toSorted((a, b) => a.start - b.start)
  for (const { event, start, end } of shown) {
    const label = dayLabel(new Date(Math.max(start, t)), now)
    const day = days.at(-1)
    const entry = { event, start, end, now: start <= t }
    if (day?.label === label) day.events.push(entry)
    else days.push({ label, events: [entry] })
  }
  return days
}
