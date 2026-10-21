/**
 * One message of the conversation as a card of flat glass over the sky: yours on the right with Betelgeuse's hairline,
 * the pictures you sent above your words; his on the left with Rigel's, his trace of operations above his answer, which
 * streams in with a soft caret, and under it the turn's stages as they were measured (an instant command's reply is
 * marked so); an alert (a timer or reminder come due) with its bell; another app's operations or background work as
 * one compact line that opens to its chips; and the line where a new conversation began.
 */
import { AlertTriangle, Bell, ChevronDown, Workflow, Zap } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { memo, type ReactNode, useRef, useState } from "react"
import { messageTarget, useMeteorTarget } from "@/lib/core"
import { clockTime, traceSpan } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { FOLD, RISE } from "@/lib/motion"
import type { ChatMessage } from "@/lib/store"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { Mark } from "@/ui/brand"
import { CopyButton } from "@/ui/copy-button"
import { Pictures } from "@/ui/pictures"
import { Markdown } from "./markdown"
import { StageStrip } from "./stages"
import { Trace } from "./trace"

/**
 * A message; only one whose message changed renders again.
 * @param props the `message`
 * @returns the row
 */
export const Message = memo(function Message({ message: m }: { message: ChatMessage }) {
  const time = clockTime(new Date(m.at), false)
  if (m.role === "divider")
    return (
      <motion.li {...RISE} data-role="divider" className="flex items-center gap-3 py-2 text-steel-500">
        <span aria-hidden="true" className="h-px flex-1 bg-linear-to-r from-transparent to-steel-50/12" />
        <span className="caps">New conversation · {time}</span>
        <span aria-hidden="true" className="h-px flex-1 bg-linear-to-l from-transparent to-steel-50/12" />
      </motion.li>
    )
  if (m.role === "ops") return <OpsRow message={m} />
  if (m.role === "user") return <Yours message={m} time={time} />
  return <His message={m} time={time} />
})

/**
 * A message's card: flat glass, a denser tint over the sky (the list fades out at its top, where no blur reaches),
 * that meteors reach by its message's id.
 * @param props the message `id`, classes and the content
 * @returns the card
 */
function Card({ id, className, children }: { id: string; className?: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useMeteorTarget(ref, messageTarget(id))
  return (
    <div
      ref={ref}
      data-flat=""
      className={cn("glass [--glass:color-mix(in_oklch,var(--color-void-850)_74%,transparent)]", className)}
    >
      {children}
    </div>
  )
}

/**
 * Your message.
 * @param props the `message` and its `time`
 * @returns the row
 */
function Yours({ message: m, time }: { message: ChatMessage; time: string }) {
  return (
    <motion.li {...RISE} data-role="user" className="flex flex-col items-end gap-1.5 pl-[12%]">
      <Card
        id={m.id}
        className="rounded-card rounded-br-[8px] px-4 py-3 shadow-[var(--shadow-float),inset_0_1px_0_0_var(--highlight),inset_0_0_0_1px_color-mix(in_oklch,var(--color-betel-400)_30%,transparent)]"
      >
        {m.images && m.images.length > 0 && (
          <Pictures images={m.images} label="Pictures you sent" className="mb-2.5 justify-end" />
        )}
        {m.text && <p className="whitespace-pre-wrap break-words text-base text-steel-50">{m.text}</p>}
      </Card>
      <p className="flex items-center gap-1.5 px-1 text-micro text-steel-500">
        <span className="text-betel-300/80">{m.source === "voice" ? "You, aloud" : "You"}</span>
        <span aria-hidden="true">·</span>
        <time>{time}</time>
      </p>
    </motion.li>
  )
}

/**
 * His message: an answer (its trace above its words) or an alert.
 * @param props the `message` and its `time`
 * @returns the row
 */
function His({ message: m, time }: { message: ChatMessage; time: string }) {
  const alert = m.source === "announcement"
  const steps = m.steps ?? []
  const waiting = m.streaming && !m.text && !steps.some((st) => st.status === "running")
  const showText = Boolean(m.text) || m.error || waiting || (!steps.length && !m.streaming)
  return (
    <motion.li {...RISE} data-role="assistant" className="group/answer pr-[4%]">
      <Card
        id={m.id}
        className={cn(
          "rounded-card rounded-tl-[8px] px-5 pt-3.5 pb-4",
          m.error
            ? "shadow-[var(--shadow-float),inset_0_1px_0_0_var(--highlight),inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_40%,transparent)]"
            : alert
              ? "shadow-[var(--shadow-float),inset_0_1px_0_0_var(--highlight),inset_0_0_0_1px_color-mix(in_oklch,var(--color-dusk-400)_32%,transparent)]"
              : "shadow-[var(--shadow-float),inset_0_1px_0_0_var(--highlight),inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_18%,transparent)]",
        )}
      >
        <header className="mb-2 flex min-h-6 items-center gap-2">
          {alert ? (
            <Bell aria-hidden="true" className="size-3.5 text-dusk-400" />
          ) : (
            <Mark className="size-4 text-rigel-300" />
          )}
          <span className={cn("font-medium text-xs", alert ? "text-dusk-200" : "text-steel-200")}>
            {alert ? (m.title ?? "Alert") : "Orion"}
          </span>
          <time className="text-micro text-steel-500">{time}</time>
          {m.instant && (
            <Badge title="Answered at once, without his brain">
              <Zap aria-hidden="true" className="text-dusk-300" />
              Instant
            </Badge>
          )}
          {m.text && !m.streaming && (
            <CopyButton
              text={m.text}
              label="Copy his answer"
              className="-my-1 ml-auto opacity-0 transition-opacity group-focus-within/answer:opacity-100 group-hover/answer:opacity-100 max-desk:opacity-100"
            />
          )}
        </header>
        {steps.length > 0 && (
          <div className={cn(showText && "mb-3")}>
            <Trace steps={steps} streaming={m.streaming} />
          </div>
        )}
        {showText && (
          <div className="text-base text-steel-100 leading-relaxed">
            {m.error ? (
              <div className="flex gap-2.5 text-alert-200">
                <AlertTriangle aria-label="Error" className="mt-0.5 size-4 shrink-0 text-alert-400" />
                <Markdown text={m.text} />
              </div>
            ) : (
              <Markdown text={m.text} streaming={m.streaming} />
            )}
            {m.interrupted && <p className="mt-2 text-xs text-steel-500">Interrupted</p>}
          </div>
        )}
        {!m.streaming && !alert && <StageStrip turn={m.id} />}
      </Card>
    </motion.li>
  )
}

/**
 * Another app's operations, or background work, as one line that opens to its chips.
 * @param props the ops `message`
 * @returns the row
 */
export function OpsRow({ message: m }: { message: ChatMessage }) {
  const [open, setOpen] = useState(false)
  const steps = m.steps ?? []
  const running = steps.some((st) => st.status === "running")
  const now = useNow(1000, running).getTime()
  const last = steps.at(-1)
  const external = m.source === "app"
  const who = external ? (m.client ?? "Another app") : (m.client ?? "Background")
  const failed = steps.some((st) => st.status === "failed")
  return (
    <motion.li {...RISE} data-role="ops" className="flex flex-col items-center">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        data-flat=""
        className="glass flex h-8 max-w-full items-center gap-2 rounded-full px-3 text-xs text-steel-300 outline-none transition-colors hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400"
      >
        <Workflow
          aria-hidden="true"
          className={cn("size-3.5 shrink-0", failed ? "text-alert-400" : "text-steel-500")}
        />
        <span className="shrink-0 font-medium text-steel-200">{who}</span>
        <span className="truncate">
          {last?.title}
          {steps.length > 1 ? ` · ${steps.length} operations` : ""}
        </span>
        <span className="shrink-0 font-mono text-micro text-steel-500 tabular-nums">{traceSpan(steps, now)}</span>
        <ChevronDown
          aria-hidden="true"
          className={cn("size-3.5 shrink-0 transition-transform duration-(--dur-quick)", open && "rotate-180")}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div {...FOLD} className="w-full overflow-hidden">
            <div className="pt-2.5">
              <Trace steps={steps} streaming={running} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.li>
  )
}
