/**
 * The top bar, over every page: his mark and name with his state in words (shell/state-label.tsx); in the middle the
 * command palette's field (⌘K); on the right what is live (the next timer counting down, his questions waiting, the
 * watchers' alerts: each a chip that opens it), a restart waiting, the ways to the other pages, and the time. On a
 * phone it keeps the mark, the state, the live chips, the palette's button, and the pages in a menu.
 */
import { Menu as BaseMenu } from "@base-ui/react/menu"
import { ArrowLeft, BellRing, Hourglass, Menu, RotateCw, Search, ShieldAlert } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import type { ReactNode } from "react"
import { Link, useLocation } from "wouter"
import { useShallow } from "zustand/react/shallow"
import { useRestart } from "@/lib/api"
import { countdown, timeLeft, timerLabel } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { POP } from "@/lib/motion"
import { PAGES, type PageId, pageOf, preload } from "@/lib/nav"
import { present, setPalette } from "@/lib/stage"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { showConfirmations } from "@/stage/confirmation"
import { Mark, Wordmark } from "@/ui/brand"
import { Button, buttonClass } from "@/ui/button"
import { Kbd, modKey } from "@/ui/kbd"
import { MENU, MENU_ITEM } from "@/ui/menu"
import { ProgressRing } from "@/ui/progress-ring"
import { Tooltip } from "@/ui/tooltip"
import { StateLabel } from "./state-label"

/** The pages the bar leads to (the stage is the mark). */
const LINKED = PAGES.filter((p) => p.id !== "stage")

/**
 * The bar.
 * @returns the bar
 */
export function TopBar() {
  const [location] = useLocation()
  const page = pageOf(location)
  return (
    <header
      className="fixed inset-x-0 top-0 z-40 flex h-(--bar-h) items-center gap-2 px-3 tab:gap-3 tab:px-5 desk:px-6"
      data-testid="top-bar"
    >
      {/* a soft fade so the bar reads over the sky without a hard edge */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -bottom-4 bg-linear-to-b from-void-950/85 via-void-950/40 to-transparent"
      />
      <div className="relative flex min-w-0 flex-1 items-center gap-2 tab:gap-3">
        {page !== "stage" && (
          <Link
            href="/"
            className={buttonClass({ tone: "ghost", size: "sm", icon: true })}
            aria-label="Back to the stage"
          >
            <ArrowLeft aria-hidden="true" />
          </Link>
        )}
        <Link
          href="/"
          className="flex shrink-0 items-center gap-2.5 rounded-control px-1 py-1 outline-none focus-visible:outline-2 focus-visible:outline-rigel-400"
          aria-label="Orion: the stage"
        >
          <Mark className="size-6 text-rigel-300" />
          <Wordmark className="max-tab:hidden" />
        </Link>
        <span aria-hidden="true" className="h-4 w-px shrink-0 bg-steel-50/12 max-tab:hidden" />
        <StateLabel className="min-w-0 text-sm" />
      </div>
      <PaletteField />
      <div className="relative flex min-w-0 flex-1 items-center justify-end gap-1 tab:gap-1.5">
        <LiveChips />
        <RestartChip />
        <nav aria-label="Pages" className="flex items-center max-tab:hidden">
          {LINKED.map((p) => (
            <PageLink key={p.id} page={p} here={page === p.id} />
          ))}
        </nav>
        <PagesMenu page={page} />
        <Clock />
      </div>
    </header>
  )
}

/**
 * The palette's field (a button that opens it): a glass pill on a wide screen, a magnifier otherwise.
 * @returns the field
 */
function PaletteField() {
  const mod = modKey()
  return (
    <>
      <button
        type="button"
        onClick={() => setPalette(true)}
        className="glass relative hidden h-9 w-[min(26rem,28vw)] shrink-0 items-center gap-2.5 rounded-full px-3.5 text-sm text-steel-400 outline-none transition-[color,box-shadow] duration-(--dur-quick) hover:text-steel-200 focus-visible:outline-2 focus-visible:outline-rigel-400 desk:flex"
        data-testid="palette-open"
      >
        <Search aria-hidden="true" className="size-4 shrink-0" />
        <span className="truncate">Search, or tell the HUD what to do…</span>
        <span className="ml-auto flex shrink-0 gap-1">
          <Kbd>{mod}</Kbd>
          <Kbd>K</Kbd>
        </span>
      </button>
      <Button
        tone="ghost"
        size="sm"
        className="relative shrink-0 desk:hidden"
        aria-label="Search and commands"
        onClick={() => setPalette(true)}
      >
        <Search aria-hidden="true" />
      </Button>
    </>
  )
}

/**
 * One of the other pages: an icon, lit while it shows (a press there goes back to the stage).
 * @param props the `page` and whether it is the one shown (`here`)
 * @returns the link
 */
function PageLink({ page, here }: { page: (typeof LINKED)[number]; here: boolean }) {
  return (
    <Tooltip
      content={page.label}
      side="bottom"
      render={
        <Link
          href={here ? "/" : page.href}
          aria-label={here ? `${page.label}: back to the stage` : page.label}
          aria-current={here ? "page" : undefined}
          onPointerEnter={() => preload(page.id)}
          onFocus={() => preload(page.id)}
          className={buttonClass({
            tone: "ghost",
            size: "sm",
            icon: true,
            className: "aria-[current=page]:bg-steel-50/9 aria-[current=page]:text-steel-50",
          })}
          data-testid={`page-${page.id}`}
        />
      }
    >
      <page.icon />
    </Tooltip>
  )
}

/**
 * The pages, in a menu: a phone's way to them.
 * @param props the `page` shown
 * @returns the menu
 */
function PagesMenu({ page }: { page: PageId | null }) {
  const [, navigate] = useLocation()
  return (
    <BaseMenu.Root>
      <BaseMenu.Trigger
        className={buttonClass({ tone: "ghost", size: "sm", icon: true, className: "tab:hidden" })}
        aria-label="Pages"
        data-testid="pages-menu"
      >
        <Menu aria-hidden="true" />
      </BaseMenu.Trigger>
      <BaseMenu.Portal>
        <BaseMenu.Positioner side="bottom" align="end" sideOffset={8} className="isolate z-[60]">
          <BaseMenu.Popup className={cn(MENU, "min-w-52")}>
            {PAGES.map((p) => (
              <BaseMenu.Item
                key={p.id}
                onClick={() => navigate(p.href)}
                className={cn(MENU_ITEM, page === p.id && "text-steel-50")}
              >
                <p.icon aria-hidden="true" className={cn(page === p.id ? "text-rigel-300" : "text-steel-400")} />
                {p.label}
              </BaseMenu.Item>
            ))}
          </BaseMenu.Popup>
        </BaseMenu.Positioner>
      </BaseMenu.Portal>
    </BaseMenu.Root>
  )
}

/**
 * The time (Instrument Serif's figures), once a minute; a tablet and wider.
 * @returns the clock
 */
function Clock() {
  const now = useNow(60_000)
  return (
    <time
      className="shrink-0 pl-1.5 font-serif text-lg text-steel-200 tabular-nums leading-none max-tab:hidden"
      dateTime={now.toISOString()}
      title={now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" })}
    >
      {now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}
    </time>
  )
}

// ---------- what is live ----------
const CHIP =
  "flex h-8 shrink-0 items-center gap-1.5 rounded-full px-2 text-xs outline-none transition-[background-color,color] duration-(--dur-quick) focus-visible:outline-2 focus-visible:outline-rigel-400 tab:px-2.5"

/**
 * What is live now, each a chip that opens it: the next timer, his questions waiting, the watchers' alerts.
 * @returns the chips (nothing while nothing is live)
 */
function LiveChips() {
  const { timers, questions, alerts, critical } = useLive(
    useShallow((s) => ({
      timers: s.reminders.filter((r) => r.kind === "timer").length,
      questions: s.confirms.length,
      alerts: s.alerts.length,
      critical: s.alerts.some((a) => a.level !== "info"),
    })),
  )
  const [, navigate] = useLocation()
  return (
    <div className="flex items-center gap-1" data-testid="live-chips">
      <AnimatePresence initial={false}>
        {timers > 0 && (
          <motion.span key="timers" {...POP} className="flex">
            <TimerChip />
          </motion.span>
        )}
        {questions > 0 && (
          <motion.span key="questions" {...POP} className="flex">
            <Chip
              label={`${questions === 1 ? "A question waits" : `${questions} questions wait`} for your answer`}
              onClick={showConfirmations}
              className="bg-alert-400/14 text-alert-100 hover:bg-alert-400/22"
              testId="chip-questions"
            >
              <ShieldAlert aria-hidden="true" className="size-4 text-alert-300" />
              <span className="font-mono tabular-nums">{questions}</span>
            </Chip>
          </motion.span>
        )}
        {alerts > 0 && (
          <motion.span key="alerts" {...POP} className="flex">
            <Chip
              label={`${alerts === 1 ? "An alert" : `${alerts} alerts`}: see them in Activity`}
              onClick={() => navigate("/activity?kind=alert")}
              className={cn(
                critical
                  ? "bg-alert-400/12 text-alert-100 hover:bg-alert-400/20"
                  : "bg-steel-50/6 text-steel-100 hover:bg-steel-50/10",
              )}
              testId="chip-alerts"
            >
              <BellRing aria-hidden="true" className={cn("size-4", critical ? "text-alert-300" : "text-rigel-300")} />
              <span className="font-mono tabular-nums">{alerts}</span>
            </Chip>
          </motion.span>
        )}
      </AnimatePresence>
    </div>
  )
}

/**
 * A live chip: a button with its tooltip.
 * @param props its `label`, `onClick`, classes, test id and content
 * @returns the chip
 */
function Chip({
  label,
  onClick,
  className,
  testId,
  children,
}: {
  label: string
  onClick: () => void
  className?: string
  testId: string
  children: ReactNode
}) {
  return (
    <Tooltip
      content={label}
      side="bottom"
      render={
        <button
          type="button"
          aria-label={label}
          onClick={onClick}
          className={cn(CHIP, className)}
          data-testid={testId}
        />
      }
    >
      {children}
    </Tooltip>
  )
}

/**
 * The next timer to end, counting down (and how many more run): a press opens the timers card.
 * @returns the chip
 */
function TimerChip() {
  const timers = useLive(useShallow((s) => s.reminders.filter((r) => r.kind === "timer")))
  const now = useNow(1000)
  const next = timers.toSorted((a, b) => a.due - b.due)[0]
  if (!next) return null
  const left = countdown(next.due, now)
  const more = timers.length - 1
  return (
    <Chip
      label={`${timerLabel(next)}: ${left === "now" ? "done" : `${left} left`}${more ? `, and ${more} more` : ""}`}
      onClick={() => present("timers", { go: true })}
      className="bg-dusk-400/10 text-dusk-100 hover:bg-dusk-400/18"
      testId="chip-timer"
    >
      <ProgressRing value={timeLeft(next, now)} size={18} stroke={2.25} tone="dusk" glide={0}>
        <Hourglass aria-hidden="true" className="size-2.5 text-dusk-300" />
      </ProgressRing>
      <span className="font-mono tabular-nums">{left}</span>
      {more > 0 && <span className="font-mono text-dusk-300/80 tabular-nums max-tab:hidden">+{more}</span>}
    </Chip>
  )
}

/**
 * A saved setting waits for a restart: say so, with the button.
 * @returns the chip, or nothing
 */
function RestartChip() {
  const waiting = useLive((s) => s.restart.length > 0)
  const restart = useRestart()
  if (!waiting) return null
  return (
    <Button
      size="xs"
      tone="secondary"
      className="rounded-full text-dusk-200"
      disabled={restart.isPending || restart.isSuccess}
      onClick={() => restart.mutate()}
      data-testid="restart-now"
      title="A setting waits for a restart"
    >
      <RotateCw aria-hidden="true" />
      <span className="max-desk:sr-only">Restart to apply</span>
    </Button>
  )
}
