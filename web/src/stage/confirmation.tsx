/**
 * His question before a risky action (`confirm`), pinned at the top of the screen with a red rim: what he asks, what he
 * would do (the call's details, scrollable), why he asks (the request read content from somewhere, you asked to be
 * asked about this tool, the channel it came from always asks first, or it would run outside the sandbox), a ring
 * counting down to the moment no answer becomes no, and Yes / No (keys Y and N). Later (or Esc) tucks it into the top
 * bar's question chip, which opens it again. Whichever answer comes first (here, another HUD, his voice, Discord)
 * settles it everywhere (`confirm_done`). Horizon writes a "?" while it waits (shell/core-host.tsx).
 */
import { ShieldAlert } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useRef, useState } from "react"
import { create } from "zustand"
import { fieldOf, useNow } from "@/lib/hooks"
import { SPRING } from "@/lib/motion"
import { play } from "@/lib/sound"
import { useLive } from "@/lib/store"
import type { ConfirmRequest } from "@/lib/types"
import { cn } from "@/lib/utils"
import { answerConfirm, onEvent } from "@/lib/ws"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { Kbd } from "@/ui/kbd"
import { ProgressRing } from "@/ui/progress-ring"

/** The questions tucked away (Later), and those answered here (waiting for the server to close them). */
const useTucked = create<{ tucked: string[]; sent: string[] }>(() => ({ tucked: [], sent: [] }))

/** When each question first showed here (its ring runs from then to its end). */
const shownAt = new Map<string, number>()

/**
 * The part of a question's time left, 1 when it showed, 0 at its end.
 * @param c the question
 * @param now the time (ms)
 * @returns the part left
 */
export function confirmLeft(c: ConfirmRequest, now: number): number {
  const end = c.expires * 1000
  if (!shownAt.has(c.id)) shownAt.set(c.id, Math.min(now, end - 1000))
  const start = shownAt.get(c.id) ?? now
  return Math.min(1, Math.max(0, (end - now) / Math.max(1, end - start)))
}

/** Show the questions tucked away again (the top bar's question chip). */
export function showConfirmations(): void {
  useTucked.setState({ tucked: [] })
}

/** Why he asks, in words, for each reason the server gives. */
const WHY: Record<ConfirmRequest["reason"], (q: ConfirmRequest) => string> = {
  content: (q) =>
    `This request read outside content${q.source ? ` (${q.source})` : ""}: he checks with you before anything that changes or sends something.`,
  always: () => "You asked him to always check before this.",
  channel: () => "Where this request came from, he always checks with you before this (Settings, Safety).",
  outside_sandbox: () => "This would run outside the sandbox, with his full access to this machine.",
}

const KINDS: Record<string, string> = {
  voice: "Asked aloud",
  hud: "Typed here",
  discord: "From Discord",
  job: "From a scheduled job",
  background: "From his own work",
}

/**
 * The confirmation: the oldest question waiting, unless tucked away.
 * @returns the dialog
 */
export function Confirmation() {
  const confirms = useLive((s) => s.confirms)
  const { tucked, sent } = useTucked()
  const shown = confirms.filter((c) => !tucked.includes(c.id)).toSorted((a, b) => a.expires - b.expires)[0]
  const more = confirms.length - 1

  // a soft double tick as a question arrives
  useEffect(() => onEvent("confirm", () => play("ask")), [])
  // forget what was tucked or sent once it is settled
  useEffect(() => {
    const ids = new Set(confirms.map((c) => c.id))
    useTucked.setState((s) => ({
      tucked: s.tucked.filter((id) => ids.has(id)),
      sent: s.sent.filter((id) => ids.has(id)),
    }))
    for (const id of shownAt.keys()) if (!ids.has(id)) shownAt.delete(id)
  }, [confirms])

  return (
    <div className="pointer-events-none fixed inset-x-0 top-[calc(var(--bar-h)+0.75rem)] z-[65] flex justify-center px-3">
      <AnimatePresence mode="wait">
        {shown && <Question key={shown.id} question={shown} more={more} sent={sent.includes(shown.id)} />}
      </AnimatePresence>
    </div>
  )
}

/**
 * One question.
 * @param props the `question`, how many `more` wait, and whether an answer was `sent`
 * @returns the card
 */
function Question({ question: q, more, sent }: { question: ConfirmRequest; more: number; sent: boolean }) {
  const box = useRef<HTMLDivElement>(null)
  const now = useNow(1000).getTime()
  const left = Math.max(0, Math.ceil((q.expires * 1000 - now) / 1000))
  const [focused, setFocused] = useState(false)

  const answer = (yes: boolean) => {
    if (sent) return
    if (answerConfirm(q.id, yes)) useTucked.setState((s) => ({ sent: [...s.sent, q.id] }))
  }
  const tuck = () => useTucked.setState((s) => ({ tucked: [...s.tucked, q.id] }))

  // Y and N answer, Esc tucks it away (unless you are typing somewhere)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return
      if (fieldOf(e.target) && !box.current?.contains(e.target as Node)) return
      const key = e.key.toLowerCase()
      if (key === "y" || key === "n") {
        e.preventDefault()
        answer(key === "y")
      } else if (e.key === "Escape") {
        e.preventDefault()
        tuck()
      }
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  })

  // the focus comes here, unless you are typing
  useEffect(() => {
    if (!fieldOf(document.activeElement)) box.current?.focus({ preventScroll: true })
  }, [])

  const why = WHY[q.reason]?.(q) ?? WHY.always(q)
  return (
    <motion.div
      ref={box}
      role="alertdialog"
      aria-modal="false"
      aria-labelledby={`q-${q.id}`}
      aria-describedby={`qd-${q.id}`}
      tabIndex={-1}
      onFocus={() => setFocused(true)}
      onBlur={(e) => !e.currentTarget.contains(e.relatedTarget as Node | null) && setFocused(false)}
      initial={{ opacity: 0, y: -18 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -12, transition: { duration: 0.18 } }}
      transition={SPRING.gentle}
      className={cn(
        "glass-solid pointer-events-auto grid w-full max-w-xl gap-4 rounded-card p-5 outline-none",
        "shadow-[var(--shadow-elevated),0_0_60px_-18px_var(--color-alert-400),inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_48%,transparent),inset_0_1px_0_0_var(--highlight)]",
      )}
      data-testid="confirmation"
      data-focused={focused || undefined}
    >
      <div className="flex items-start gap-4">
        <ProgressRing
          value={confirmLeft(q, now)}
          size={46}
          stroke={3}
          tone="alert"
          glide={1000}
          linear
          label={`${left} seconds to answer`}
        >
          <span className="font-mono text-sm text-alert-100 tabular-nums">{left}</span>
        </ProgressRing>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-2">
            <span className="caps flex items-center gap-1.5 text-alert-300">
              <ShieldAlert aria-hidden="true" className="size-3.5" />
              Shall I?
            </span>
            {KINDS[q.source_kind] && <Badge>{KINDS[q.source_kind]}</Badge>}
            {more > 0 && <Badge tone="alert">+{more} waiting</Badge>}
          </p>
          <h2
            id={`q-${q.id}`}
            className="mt-1.5 text-balance font-medium text-base text-steel-50 leading-snug tab:text-lg"
          >
            {q.text}
          </h2>
        </div>
      </div>
      <div className="grid gap-2 rounded-control bg-void-950/55 p-3 shadow-[inset_0_0_0_1px_var(--hairline)]">
        <p className="flex items-center gap-2 text-sm text-steel-200">
          <span className="font-medium text-steel-100">{q.title}</span>
          {q.server && <span className="font-mono text-micro text-steel-500">{q.server}</span>}
        </p>
        {q.detail && (
          <pre className="max-h-36 overflow-y-auto whitespace-pre-wrap break-words font-mono text-xs text-steel-300 leading-relaxed">
            {q.detail}
          </pre>
        )}
      </div>
      <p id={`qd-${q.id}`} className="text-xs text-steel-400">
        {why}
      </p>
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button tone="ghost" size="sm" onClick={tuck} className="mr-auto">
          Later
          <Kbd>Esc</Kbd>
        </Button>
        <Button size="md" disabled={sent} onClick={() => answer(false)} data-testid="confirm-no">
          No
          <Kbd>N</Kbd>
        </Button>
        <Button tone="danger" size="md" disabled={sent} onClick={() => answer(true)} data-testid="confirm-yes">
          Yes, do it
          <Kbd className="bg-alert-400/16 text-alert-100">Y</Kbd>
        </Button>
      </div>
    </motion.div>
  )
}
