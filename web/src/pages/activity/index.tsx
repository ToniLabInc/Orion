/**
 * Activity: what he did, and who allowed it, newest first and grouped by day, each day heading the log as it scrolls:
 * the tools he ran and how they ended, the questions he asked and who answered, what was refused or blocked, the
 * alerts, his jobs, the instant commands, backups, connector, plugin and skill changes, sign-ins and secrets (their
 * names only). Narrow it by kind and by where it came from, by words (typed, asked for once you stop), or with "His
 * own": what he did without you in front of him (jobs, watchers, Discord). The filters are in the address
 * (/activity?kind=alert, which the top bar's alert chip opens); entries arrive live at the top, and older ones come as
 * the page nears its end. An entry opens to everything recorded about it.
 */
import { Popover } from "@base-ui/react/popover"
import { Check, Filter, LoaderCircle, ScrollText } from "lucide-react"
import { motion } from "motion/react"
import { type RefObject, useEffect, useMemo, useRef, useState } from "react"
import { useSearchParams } from "wouter"
import { byDay, HIS_OWN, KINDS, SOURCES } from "@/lib/activity"
import { missing, useActivity } from "@/lib/api"
import { useNow, useSettled } from "@/lib/hooks"
import { stagger } from "@/lib/motion"
import { useSmoothScroll } from "@/lib/scroll"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { MENU } from "@/ui/menu"
import { SearchField } from "@/ui/search"
import { Empty, Failed, Pending } from "@/ui/states"
import { ARRIVE } from "../frame"
import { Entry } from "./entry"

/** The comma-separated list a parameter holds. */
const listOf = (value: string | null) => (value ? value.split(",").filter(Boolean) : [])

/**
 * The activity page.
 * @returns the page
 */
export default function ActivityPage() {
  const [params, setParams] = useSearchParams()
  const kind = listOf(params.get("kind"))
  const source = listOf(params.get("source"))
  const [typed, setTyped] = useState(params.get("q") ?? "")
  const q = useSettled(typed)
  const log = useActivity({ kind, source, q })
  const scroller = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  useSmoothScroll(scroller, content)
  const filtered = kind.length > 0 || source.length > 0 || q.trim().length > 0

  const set = (change: { kind?: string[]; source?: string[] }) =>
    setParams(
      (before) => {
        const next = new URLSearchParams(before)
        for (const [key, list] of Object.entries(change)) {
          if (list.length) next.set(key, list.join(","))
          else next.delete(key)
        }
        return next
      },
      { replace: true },
    )
  const own = source.length === HIS_OWN.length && HIS_OWN.every((s) => source.includes(s))
  const clear = () => {
    setTyped("")
    setParams({}, { replace: true })
  }

  return (
    <div ref={scroller} className="no-scrollbar h-full overflow-y-auto overscroll-contain" data-testid="activity">
      <div ref={content} className="mx-auto grid max-w-4xl gap-4 px-3 pt-[calc(var(--bar-h)+1.25rem)] pb-16 tab:px-6">
        <motion.header
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="grid gap-1.5 px-1"
          data-testid="page-header"
        >
          <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
            <h1 className="font-serif text-4xl text-steel-50 leading-none">Activity</h1>
            <Live />
          </div>
          <p className="max-w-2xl text-sm text-steel-400">
            What he did, and who allowed it. Secrets are left out before anything is written here.
          </p>
        </motion.header>

        <search className="flex flex-wrap items-center gap-2" aria-label="Filter the activity">
          <SearchField
            value={typed}
            onChange={setTyped}
            label="Search the activity"
            placeholder="Search what he did…"
            className="min-w-48 flex-1 tab:max-w-sm"
            data-testid="activity-search"
          />
          <button
            type="button"
            aria-pressed={own}
            onClick={() => set({ source: own ? [] : [...HIS_OWN] })}
            title="Jobs, watchers and Discord: what he did without you in front of him"
            className={cn(
              "h-9 shrink-0 rounded-full px-3.5 text-sm outline-none transition-colors focus-visible:outline-2 focus-visible:outline-rigel-400",
              own
                ? "bg-rigel-400/16 text-rigel-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_45%,transparent)]"
                : "bg-steel-50/5 text-steel-300 shadow-[inset_0_0_0_1px_var(--hairline)] hover:text-steel-100",
            )}
            data-testid="activity-own"
          >
            His own
          </button>
          <FilterMenu label="Kind" options={KINDS} selected={kind} onChange={(list) => set({ kind: list })} />
          <FilterMenu label="From" options={SOURCES} selected={source} onChange={(list) => set({ source: list })} />
          {filtered && (
            <Button tone="ghost" size="sm" onClick={clear} data-testid="activity-clear">
              Clear
            </Button>
          )}
        </search>

        <Log log={log} filtered={filtered} onClear={clear} scroller={scroller} />
      </div>
    </div>
  )
}

/**
 * Whether the log is live: entries arrive as he does things.
 * @returns the badge
 */
function Live() {
  const online = useLive((s) => s.link === "online")
  return online ? (
    <Badge tone="ok" dot pulse className="h-6 px-2.5 text-xs">
      Live
    </Badge>
  ) : (
    <Badge className="h-6 px-2.5 text-xs">Offline: not live</Badge>
  )
}

/**
 * A filter: a button with how many it holds, and a popover of choices to switch on and off.
 * @param props the filter's `label`, its `options` (value → words), those `selected`, and `onChange`
 * @returns the filter
 */
function FilterMenu({
  label,
  options,
  selected,
  onChange,
}: {
  label: string
  options: Readonly<Record<string, string>>
  selected: string[]
  onChange: (list: string[]) => void
}) {
  return (
    <Popover.Root>
      <Popover.Trigger
        className={cn(
          "flex h-9 shrink-0 items-center gap-2 rounded-full px-3.5 text-sm outline-none transition-colors focus-visible:outline-2 focus-visible:outline-rigel-400",
          selected.length
            ? "bg-rigel-400/16 text-rigel-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_45%,transparent)]"
            : "bg-steel-50/5 text-steel-300 shadow-[inset_0_0_0_1px_var(--hairline)] hover:text-steel-100",
        )}
        data-testid={`activity-filter-${label.toLowerCase()}`}
      >
        <Filter aria-hidden="true" className="size-3.5" />
        {label}
        {selected.length > 0 && <span className="font-mono text-xs tabular-nums">{selected.length}</span>}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner side="bottom" align="start" sideOffset={8} className="isolate z-[60]">
          <Popover.Popup className={cn(MENU, "grid max-h-80 min-w-52 gap-0.5 p-1.5")}>
            {Object.entries(options).map(([value, words]) => {
              const on = selected.includes(value)
              return (
                <button
                  key={value}
                  type="button"
                  aria-pressed={on}
                  onClick={() => onChange(on ? selected.filter((s) => s !== value) : [...selected, value])}
                  className="flex min-h-9 items-center gap-2.5 rounded-control px-2.5 text-left text-sm text-steel-200 outline-none transition-colors hover:bg-steel-50/8 hover:text-steel-50 focus-visible:bg-steel-50/8 aria-pressed:text-steel-50"
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      "grid size-4 place-items-center rounded-[5px] shadow-[inset_0_0_0_1px_var(--hairline-strong)]",
                      on && "bg-rigel-400 text-void-950 shadow-none",
                    )}
                  >
                    {on && <Check className="size-3" strokeWidth={3} />}
                  </span>
                  {words}
                </button>
              )
            })}
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  )
}

/**
 * The entries by day, with the next page asked for as the end comes into view.
 * @param props the `log` query, whether a filter is on (`filtered`), `onClear`, and the page's `scroller`
 * @returns the log, or why there is none
 */
function Log({
  log,
  filtered,
  onClear,
  scroller,
}: {
  log: ReturnType<typeof useActivity>
  filtered: boolean
  onClear: () => void
  scroller: RefObject<HTMLDivElement | null>
}) {
  const now = useNow(60_000)
  const items = useMemo(() => log.data?.pages.flatMap((page) => page.items) ?? [], [log.data])
  const days = useMemo(() => byDay(items, now), [items, now])
  const end = useRef<HTMLDivElement>(null)
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = log

  useEffect(() => {
    const sentinel = end.current
    if (!sentinel || !hasNextPage) return
    const seen = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting && !isFetchingNextPage) void fetchNextPage()
      },
      { root: scroller.current, rootMargin: "400px 0px" },
    )
    seen.observe(sentinel)
    return () => seen.disconnect()
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, scroller])

  if (log.isError && !log.data)
    return missing(log.error) ? (
      <Failed title="The activity log" error={log.error} />
    ) : (
      <Failed title="The activity can't be read" error={log.error} onRetry={() => void log.refetch()} />
    )
  if (log.isPending) return <Pending label="Reading the activity" delay={150} page />
  if (!items.length)
    return (
      <div className="glass">
        <Empty
          icon={<ScrollText />}
          action={
            filtered && (
              <Button size="sm" onClick={onClear}>
                Clear the filters
              </Button>
            )
          }
        >
          {filtered ? "Nothing matches these filters." : "Nothing recorded yet: what he does shows here as he does it."}
        </Empty>
      </div>
    )
  return (
    <motion.div
      initial="hidden"
      animate="shown"
      variants={{ shown: { transition: stagger(0.05) } }}
      className={cn("grid gap-5 transition-opacity duration-(--dur-quick)", log.isPlaceholderData && "opacity-60")}
    >
      {days.map((day) => (
        <motion.section key={day.label} variants={ARRIVE} aria-label={day.label} data-testid="activity-day">
          <h2 className="caps sticky top-(--bar-h) z-10 mb-1 w-fit rounded-full bg-void-950/85 px-3 py-1 text-steel-300 backdrop-blur-md">
            {day.label}
          </h2>
          <ol className="relative before:absolute before:top-3 before:bottom-3 before:left-[calc(2.75rem+0.375rem+1rem+0.5rem)] before:w-px before:bg-steel-50/8 tab:before:left-[calc(2.75rem+0.375rem+1rem+0.75rem)]">
            {day.items.map((item) => (
              <Entry key={item.id} item={item} />
            ))}
          </ol>
        </motion.section>
      ))}
      <div ref={end} className="grid justify-items-center py-2">
        {hasNextPage ? (
          <Button size="sm" disabled={isFetchingNextPage} onClick={() => void fetchNextPage()}>
            {isFetchingNextPage && <LoaderCircle aria-hidden="true" className="animate-spin" />}
            Older
          </Button>
        ) : (
          <p className="text-xs text-steel-500">That's all of it.</p>
        )}
      </div>
    </motion.div>
  )
}
