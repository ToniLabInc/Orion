/**
 * Timers & reminders, run straight through his organizer's tools (no model involved; the list comes back as a
 * "reminders" event). A timer is a ring emptying clockwise as it counts down (it glides between the seconds), its
 * digits in the middle; at zero it fills once and reads Done until his announcement arrives. A reminder can be set
 * here in your own words ("in 20 minutes", "tomorrow at 9"), and a quick timer started with one press. Rows slide as
 * they re-sort; one cancelled folds away.
 */
import { Bell, BellPlus, Hourglass, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { type FormEvent, useEffect, useState } from "react"
import { useOrganizer } from "@/lib/api"
import { countdown, timeLeft, timerLabel, untilInWords } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { FOLD, SPRING } from "@/lib/motion"
import { useLive } from "@/lib/store"
import type { Reminder } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Button, IconButton } from "@/ui/button"
import { Input } from "@/ui/input"
import { ProgressRing } from "@/ui/progress-ring"
import { Empty } from "@/ui/states"
import type { CardProps } from "./card"

/**
 * The timers card.
 * @param props the card's
 * @returns the body
 */
export default function Timers({ setNote }: CardProps) {
  const reminders = useLive((s) => s.reminders)
  const online = useLive((s) => s.link === "online")
  const quick = useLive((s) => s.hud?.quick_timers)
  const run = useOrganizer()
  const [busy, setBusy] = useState<string[]>([])
  const [adding, setAdding] = useState(false)
  const soonest = reminders.toSorted((a, b) => a.due - b.due)
  const live = soonest.some((r) => r.due * 1000 - Date.now() < 3_600_000)
  const now = useNow(1000, live || soonest.length > 0)
  useEffect(() => setNote(reminders.length ? `${reminders.length} running` : null), [reminders.length, setNote])

  const act = async (key: string, tool: string, args: Record<string, unknown>) => {
    setBusy((b) => [...b, key])
    const done = await run(tool, args, "Timers")
    setBusy((b) => b.filter((k) => k !== key))
    return done
  }

  return (
    <div className="grid gap-3 px-4 pt-1 pb-4" data-testid="widget-timers">
      <AnimatePresence initial={false}>
        {adding && (
          <motion.div {...FOLD} className="overflow-hidden">
            <ReminderForm
              onSet={async (message, when) => {
                const done = await act("remind", "set_reminder", { message, when })
                if (done) setAdding(false)
                return done
              }}
            />
          </motion.div>
        )}
      </AnimatePresence>
      <ul className="grid">
        <AnimatePresence initial={false}>
          {soonest.map((r) => (
            <Row
              key={r.id}
              reminder={r}
              now={now}
              cancelling={busy.includes(`cancel-${r.id}`)}
              online={online}
              onCancel={() => act(`cancel-${r.id}`, "cancel_timer_or_reminder", { which: `#${r.id}` })}
            />
          ))}
        </AnimatePresence>
        {!soonest.length && (
          <li>
            <Empty icon={<Hourglass />}>Nothing counting down.</Empty>
          </li>
        )}
      </ul>
      <div className="flex flex-wrap items-center gap-1.5 border-t pt-3">
        {quick?.map((minutes) => (
          <Button
            key={minutes}
            size="xs"
            className="rounded-full font-mono"
            disabled={busy.includes(`quick-${minutes}`) || !online}
            onClick={() => act(`quick-${minutes}`, "set_timer", { duration: `${minutes} minutes` })}
            aria-label={`Start a ${minutes} minute timer`}
            data-testid={`quick-timer-${minutes}`}
          >
            {minutes}m
          </Button>
        ))}
        <IconButton
          label="Set a reminder"
          size="xs"
          className="ml-auto"
          aria-expanded={adding}
          disabled={!online}
          onClick={() => setAdding((a) => !a)}
          data-testid="reminder-new"
        >
          <BellPlus />
        </IconButton>
      </div>
    </div>
  )
}

/**
 * A reminder in your own words: what, and when.
 * @param props `onSet`, which resolves true once he has set it
 * @returns the form
 */
function ReminderForm({ onSet }: { onSet: (message: string, when: string) => Promise<boolean> }) {
  const [message, setMessage] = useState("")
  const [when, setWhen] = useState("")
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (message.trim() && when.trim() && (await onSet(message.trim(), when.trim()))) {
      setMessage("")
      setWhen("")
    }
  }
  return (
    <form onSubmit={submit} className="grid grid-cols-[1fr_auto] gap-2 pb-1" data-testid="reminder-form">
      <Input
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder="Remind me to…"
        aria-label="What to remind you about"
        maxLength={200}
        className="col-span-2"
        autoFocus
      />
      <Input
        value={when}
        onChange={(e) => setWhen(e.target.value)}
        placeholder="When: in 20 minutes, tomorrow at 9…"
        aria-label="When"
        maxLength={80}
      />
      <Button type="submit" tone="primary" disabled={!message.trim() || !when.trim()}>
        Set
      </Button>
    </form>
  )
}

/**
 * One timer or reminder.
 * @param props the `reminder`, the time `now`, whether it is being cancelled, whether he is online, and `onCancel`
 * @returns the row
 */
function Row({
  reminder: r,
  now,
  cancelling,
  online,
  onCancel,
}: {
  reminder: Reminder
  now: Date
  cancelling: boolean
  online: boolean
  onCancel: () => void
}) {
  const timer = r.kind === "timer"
  const label = timer ? timerLabel(r) : r.text
  const left = Math.ceil(r.due - now.getTime() / 1000)
  const done = left <= 0
  const soon = left < 3600
  return (
    <motion.li
      layout
      {...FOLD}
      transition={{ ...FOLD.transition, layout: SPRING.gentle }}
      className={cn("group/row overflow-hidden", cancelling && "opacity-40")}
      data-testid="reminder"
    >
      <div className="flex items-center gap-3 py-2">
        {timer ? (
          <ProgressRing
            value={done ? 1 : timeLeft(r, now)}
            size={44}
            stroke={3}
            tone={done ? "ok" : "dusk"}
            glide={1000}
            linear
          >
            <Hourglass aria-hidden="true" className={cn("size-4", done ? "text-ok-300" : "text-dusk-300")} />
          </ProgressRing>
        ) : (
          <span className="grid size-11 place-items-center rounded-full bg-steel-50/5 shadow-[inset_0_0_0_1px_var(--hairline)]">
            <Bell aria-label="Reminder" className="size-4 text-rigel-300" />
          </span>
        )}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-steel-100">{label}</span>
          <span className="block text-xs text-steel-500">{timer ? "Timer" : untilInWords(r.due, now)}</span>
        </span>
        <span
          className={cn(
            "font-serif text-2xl tabular-nums leading-none",
            done ? "text-ok-300" : timer && soon ? "text-steel-50" : "text-steel-300",
          )}
        >
          {done ? "Done" : timer && left < 60 ? `0:${String(left).padStart(2, "0")}` : countdown(r.due, now)}
        </span>
        <IconButton
          label={`Cancel ${label}`}
          tip={false}
          size="xs"
          disabled={cancelling || !online}
          onClick={onCancel}
          className="opacity-0 transition-opacity group-hover/row:opacity-100 focus-visible:opacity-100 max-desk:opacity-100"
        >
          <X />
        </IconButton>
      </div>
    </motion.li>
  )
}
