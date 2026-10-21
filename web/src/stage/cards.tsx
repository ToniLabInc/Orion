/**
 * The cards on the stage: a column of glass on the right (a sheet on a tablet or a phone). A card arrives with a meteor
 * from the limb and settles into the stack, pushing the others down; pinned cards stay on top (and come back after a
 * reload), transient ones leave after a minute and a half, a thin line along their top edge running out the time (the
 * pointer or the focus on a card holds it). Esc sends the newest away.
 */
import { Pin, PinOff, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { lazy, type ReactNode, type RefObject, Suspense, useEffect, useRef, useState } from "react"
import type { CoreHue } from "@/core"
import { cardTarget, coreActions, useMeteorTarget } from "@/lib/core"
import { DUR, EXPO, SPRING } from "@/lib/motion"
import { CARD_INFO } from "@/lib/nav"
import { type Card, type CardId, dismiss, pin, TRANSIENT_MS, useStage } from "@/lib/stage"
import { cn } from "@/lib/utils"
import { IconButton } from "@/ui/button"
import { ErrorBoundary, Pending } from "@/ui/states"

/** Each card's body: its own chunk (visx, the hands' view), loaded the first time it is summoned. */
const BODIES: Record<CardId, ReturnType<typeof lazy>> = {
  today: lazy(() => import("@/cards/today")),
  weather: lazy(() => import("@/cards/weather")),
  vitals: lazy(() => import("@/cards/vitals")),
  calendar: lazy(() => import("@/cards/calendar")),
  timers: lazy(() => import("@/cards/timers")),
  tasks: lazy(() => import("@/cards/tasks")),
  news: lazy(() => import("@/cards/news")),
  jobs: lazy(() => import("@/cards/jobs")),
  hands: lazy(() => import("@/cards/hands")),
  eyes: lazy(() => import("@/cards/eyes")),
  devices: lazy(() => import("@/cards/devices")),
}

/** The colour of the meteor a card arrives with. */
const HUES: Record<CardId, CoreHue> = {
  today: "rigel",
  weather: "aurora",
  vitals: "rigel",
  calendar: "aurora",
  timers: "betel",
  tasks: "rigel",
  news: "rigel",
  jobs: "nebula",
  hands: "betel",
  eyes: "betel",
  devices: "rigel",
}

/** The cards in the order they stack: pinned ones on top, then the newest first. */
export const stacked = (cards: Card[]) => cards.toSorted((a, b) => Number(b.pinned) - Number(a.pinned) || b.at - a.at)

/**
 * The stack.
 * @param props whether it is inside a sheet, and classes
 * @returns the cards
 */
export function CardStack({ inSheet = false, className }: { inSheet?: boolean; className?: string }) {
  const cards = useStage((s) => s.cards)
  const column = useRef<HTMLDivElement>(null)
  const below = useMoreBelow(column)
  return (
    <div className={cn("relative flex min-h-0 flex-col", className)}>
      <div
        ref={column}
        className="no-scrollbar flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto overscroll-contain"
        data-testid="cards"
      >
        <AnimatePresence initial={false} mode="popLayout">
          {stacked(cards).map((card) => (
            <CardFrame key={card.id} card={card} inSheet={inSheet} />
          ))}
        </AnimatePresence>
      </div>
      {!inSheet && (
        // more cards below the fold: the column fades into the sky there
        <div
          aria-hidden="true"
          className={cn(
            "pointer-events-none absolute inset-x-0 bottom-0 h-20 bg-linear-to-t from-void-950 to-transparent transition-opacity duration-(--dur-base)",
            below ? "opacity-100" : "opacity-0",
          )}
        />
      )}
    </div>
  )
}

/**
 * Whether a scrolling column has more below what shows, followed as it scrolls, resizes or its children change.
 * @param ref the column
 * @returns whether there is more below
 */
function useMoreBelow(ref: RefObject<HTMLElement | null>): boolean {
  const [below, setBelow] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const check = () => setBelow(el.scrollHeight - el.scrollTop - el.clientHeight > 8)
    const sizes = new ResizeObserver(check)
    const watch = () => {
      sizes.disconnect()
      sizes.observe(el)
      for (const child of el.children) sizes.observe(child)
      check()
    }
    const children = new MutationObserver(watch)
    watch()
    children.observe(el, { childList: true })
    el.addEventListener("scroll", check, { passive: true })
    return () => {
      sizes.disconnect()
      children.disconnect()
      el.removeEventListener("scroll", check)
    }
  }, [ref])
  return below
}

/**
 * One card: its header (icon, title, pin, close), its body, and a transient card's time running out.
 * @param props the `card`, and whether it is inside a sheet
 * @returns the card
 */
function CardFrame({ card, inSheet }: { card: Card; inSheet: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  const id = cardTarget(card.id)
  useMeteorTarget(ref, inSheet ? null : id)
  const info = CARD_INFO[card.id]
  const Body = BODIES[card.id]
  const [held, setHeld] = useState(false)
  const [note, setNote] = useState<ReactNode>(null)

  // it arrives with a meteor from the limb (none in a sheet: the sky isn't behind it)
  useEffect(() => {
    if (!card.comet || inSheet) return
    const frame = requestAnimationFrame(() => coreActions.meteor(id, HUES[card.id]))
    return () => cancelAnimationFrame(frame)
  }, [card.comet, card.id, id, inSheet])

  // a transient card leaves after its time, held while the pointer or the focus is on it
  const left = useRef(TRANSIENT_MS)
  useEffect(() => {
    if (card.pinned || held) return
    const started = Date.now()
    const timer = setTimeout(() => dismiss(card.id), left.current)
    return () => {
      clearTimeout(timer)
      left.current = Math.max(4000, left.current - (Date.now() - started))
    }
  }, [card.pinned, held, card.id])
  // biome-ignore lint/correctness/useExhaustiveDependencies: summoned again (a new time stamp): a fresh time
  useEffect(() => {
    left.current = TRANSIENT_MS
  }, [card.at])

  return (
    <motion.section
      ref={ref}
      layout="position"
      aria-labelledby={`${id}-title`}
      initial={{ opacity: 0, y: -14, x: inSheet ? 0 : 18 }}
      animate={{ opacity: 1, y: 0, x: 0 }}
      exit={{ opacity: 0, x: inSheet ? 0 : 24, transition: { duration: DUR.base, ease: [...EXPO] } }}
      transition={SPRING.gentle}
      onPointerEnter={() => setHeld(true)}
      onPointerLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={(e) => !e.currentTarget.contains(e.relatedTarget as Node | null) && setHeld(false)}
      className="glass flex shrink-0 flex-col overflow-hidden"
      data-testid={`card-${card.id}`}
    >
      {!card.pinned && (
        <span
          aria-hidden="true"
          key={card.at}
          className="absolute inset-x-5 top-0 h-px origin-left bg-linear-to-r from-transparent via-rigel-300/60 to-transparent"
          style={{
            animation: `card-time ${left.current}ms linear forwards`,
            animationPlayState: held ? "paused" : "running",
          }}
        />
      )}
      <header className="flex min-h-11 items-center gap-2.5 pt-2.5 pr-2 pb-0.5 pl-4">
        <info.icon aria-hidden="true" className="size-4 shrink-0 text-steel-400" />
        <h2 id={`${id}-title`} className="min-w-0 truncate font-medium text-sm text-steel-100">
          {info.title}
        </h2>
        {note && <span className="min-w-0 truncate text-xs text-steel-500">{note}</span>}
        <div className="ml-auto flex shrink-0 items-center">
          <IconButton
            label={card.pinned ? "Unpin: it leaves after a while" : "Pin: it stays"}
            size="xs"
            aria-pressed={card.pinned}
            onClick={() => pin(card.id, !card.pinned)}
            data-testid="card-pin"
          >
            {card.pinned ? <PinOff /> : <Pin />}
          </IconButton>
          <IconButton
            label="Send away"
            shortcut="Esc"
            size="xs"
            onClick={() => dismiss(card.id)}
            data-testid="card-close"
          >
            <X />
          </IconButton>
        </div>
      </header>
      <ErrorBoundary>
        <Suspense fallback={<Pending delay={120} className="px-4 pb-4" />}>
          <Body setNote={setNote} />
        </Suspense>
      </ErrorBoundary>
    </motion.section>
  )
}
