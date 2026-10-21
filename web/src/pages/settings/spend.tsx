/**
 * What OpenRouter costs: today against the daily budget (a meter that warms as it fills; past it he sends no paid
 * request and the stand-in brain answers), the budget itself, the last 30 days as columns (the pointer or the arrow
 * keys read a day's figure), what the money went on, and the key's own limits as OpenRouter reports them. The budget's
 * day, and the columns', are your local day; the key's own figure for today is OpenRouter's, a UTC day, and says so.
 * Shown once there is a key, a budget or a cost to show.
 */
import { Meter } from "@base-ui/react/meter"
import { Wallet } from "lucide-react"
import { type KeyboardEvent, useState } from "react"
import { missing, useSpend } from "@/lib/api"
import { money, utcMidnight } from "@/lib/format"
import { useWidth } from "@/lib/hooks"
import { useLive } from "@/lib/store"
import type { SettingsResponse, SpendResponse } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Failed } from "@/ui/states"
import { Field, Note, Panel, Row } from "./fields"

/** What each purpose is called. */
const PURPOSES: Record<string, string> = {
  conversation: "Conversations",
  job: "Scheduled jobs",
  review: "Memory reviews",
  bench: "Benchmarks",
}

/**
 * The panel.
 * @param props the `settings` (whether an OpenRouter key is set)
 * @returns the panel, or nothing while there is nothing to show
 */
export function Spend({ settings }: { settings: SettingsResponse }) {
  const spend = useSpend(30)
  const live = useLive((s) => s.spend)
  if (missing(spend.error)) return null
  const data = spend.data
  const today = live?.today ?? data?.today ?? 0
  const budget = live?.budget ?? data?.budget ?? 0
  const keyed = Boolean(settings.secrets["openrouter.api_key"])
  const spent = today > 0 || Boolean(data?.by_day.some((d) => d.cost > 0))
  if (!keyed && !spent && !budget) return null
  return (
    <Panel title="What OpenRouter costs" icon={Wallet} description="By your local day" data-testid="brain-spend">
      <Today today={today} budget={budget} />
      <Field path="openrouter.daily_budget_usd" />
      {spend.isError ? (
        <div className="border-t px-5 py-4">
          <Failed title="The costs can't be read" error={spend.error} onRetry={() => void spend.refetch()} />
        </div>
      ) : data ? (
        <>
          <Days days={data.by_day} budget={budget} />
          {data.by_purpose.some((p) => p.cost > 0) && <Purposes purposes={data.by_purpose} />}
          <KeyLimits keyInfo={data.key} />
        </>
      ) : null}
    </Panel>
  )
}

/**
 * Today against the budget: the figure, and a meter whose fill warms as it nears the budget.
 * @param props `today`'s cost and the `budget` (0: none)
 * @returns the row
 */
function Today({ today, budget }: { today: number; budget: number }) {
  const share = budget ? today / budget : 0
  const fill = share >= 1 ? "bg-alert-500" : share >= 0.75 ? "bg-dusk-600" : "bg-rigel-500"
  const track = share >= 1 ? "bg-alert-500/18" : share >= 0.75 ? "bg-dusk-600/20" : "bg-rigel-500/18"
  return (
    <div className="grid gap-3 px-5 pb-4" data-testid="spend-today">
      <p className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-serif text-4xl text-steel-50 leading-none">{money(today)}</span>
        <span className="text-sm text-steel-400">
          {budget ? `today, of a ${money(budget)} daily budget` : "today · no daily budget"}
        </span>
      </p>
      {budget > 0 && (
        <Meter.Root value={Math.min(today, budget)} max={budget} aria-label="Today's cost against the daily budget">
          <Meter.Track className={cn("h-1.5 overflow-hidden rounded-full", track)}>
            <Meter.Indicator className={cn("h-full rounded-full transition-[width] duration-(--dur-slow)", fill)} />
          </Meter.Track>
        </Meter.Root>
      )}
      {share >= 1 && (
        <p className="text-sm text-alert-200">
          Over budget: he sends OpenRouter nothing more today, and his stand-in brain answers instead.
        </p>
      )}
    </div>
  )
}

/** The plot's height and the band under it for the days' labels, in px. */
const PLOT = 96
const AXIS = 18

/**
 * The last days as columns of one colour on a shared scale, the budget as a line across them; the pointer, or the
 * arrow keys once the chart has the focus, read a day's figure (every figure is also in the table for screen readers).
 * @param props the `days` (oldest first) and the `budget`
 * @returns the chart
 */
function Days({ days, budget }: { days: SpendResponse["by_day"]; budget: number }) {
  const { ref, width } = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const top = niceTop(Math.max(budget, ...days.map((d) => d.cost)))
  const slot = days.length ? width / days.length : 0
  const bar = Math.max(1, Math.min(24, slot - 2))
  const y = (cost: number) => PLOT - (cost / top) * PLOT
  const shown = hover === null ? null : days[hover]
  const label = (day: string) =>
    new Date(`${day}T00:00`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })
  const keys = (e: KeyboardEvent) => {
    const last = days.length - 1
    const at = hover ?? last
    const next =
      e.key === "ArrowLeft"
        ? at - 1
        : e.key === "ArrowRight"
          ? at + 1
          : e.key === "Home"
            ? 0
            : e.key === "End"
              ? last
              : null
    if (next === null) return
    e.preventDefault()
    setHover(Math.min(last, Math.max(0, next)))
  }
  if (!days.length) return null
  return (
    <div className="grid gap-2 border-t px-5 pt-3.5 pb-4">
      <p className="text-sm text-steel-200">The last {days.length} days</p>
      <div
        ref={ref}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: the arrow keys read a day's figure
        tabIndex={0}
        role="img"
        aria-label={`What each of the last ${days.length} days cost; the arrow keys read one`}
        onKeyDown={keys}
        onFocus={() => setHover((h) => h ?? days.length - 1)}
        onBlur={() => setHover(null)}
        onPointerMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect()
          setHover(Math.min(days.length - 1, Math.max(0, Math.floor((e.clientX - box.left) / slot))))
        }}
        onPointerLeave={() => setHover(null)}
        className="relative mt-4 rounded-chip outline-none focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-rigel-400"
        style={{ height: PLOT + AXIS }}
        data-testid="spend-days"
      >
        {width > 0 && (
          <svg width={width} height={PLOT + AXIS} aria-hidden="true" className="absolute inset-0 overflow-visible">
            <line x1={0} x2={width} y1={PLOT + 0.5} y2={PLOT + 0.5} className="stroke-steel-50/10" />
            <line x1={0} x2={width} y1={0.5} y2={0.5} className="stroke-steel-50/6" />
            {days.map((d, i) => {
              if (d.cost <= 0) return null
              const h = Math.max(1, PLOT - y(d.cost))
              const x = i * slot + (slot - bar) / 2
              const r = Math.min(4, bar / 2, h)
              return (
                <path
                  key={d.day}
                  d={`M${x},${PLOT}V${PLOT - h + r}Q${x},${PLOT - h} ${x + r},${PLOT - h}H${x + bar - r}Q${x + bar},${PLOT - h} ${x + bar},${PLOT - h + r}V${PLOT}Z`}
                  className={cn(
                    "transition-[fill] duration-(--dur-instant)",
                    hover === i ? "fill-rigel-300" : "fill-rigel-500",
                  )}
                />
              )
            })}
            {budget > 0 && budget <= top && (
              <line
                x1={0}
                x2={width}
                y1={Math.round(y(budget)) + 0.5}
                y2={Math.round(y(budget)) + 0.5}
                className="stroke-dusk-400"
                strokeDasharray="4 3"
              />
            )}
          </svg>
        )}
        {budget > 0 && budget <= top ? (
          <span
            className="absolute right-0 -translate-y-full pb-1 font-mono text-micro text-dusk-200 tabular-nums"
            style={{ top: y(budget) }}
          >
            budget {money(budget)}
          </span>
        ) : (
          <span className="absolute top-1 left-0 font-mono text-micro text-steel-500 tabular-nums">{money(top)}</span>
        )}
        <span className="absolute bottom-0 left-0 font-mono text-micro text-steel-500">{label(days[0].day)}</span>
        <span className="absolute right-0 bottom-0 font-mono text-micro text-steel-500">Today</span>
        {shown && hover !== null && (
          <div
            className="glass-solid pointer-events-none absolute z-10 grid -translate-x-1/2 -translate-y-full gap-0.5 rounded-control px-2.5 py-1.5 text-micro"
            style={{
              left: Math.min(Math.max(hover * slot + slot / 2, 56), width - 56),
              top: Math.min(y(shown.cost), PLOT) - 6,
            }}
            role="status"
          >
            <span className="font-mono text-sm text-steel-50 tabular-nums">{money(shown.cost)}</span>
            <span className="text-steel-400">{label(shown.day)}</span>
          </div>
        )}
      </div>
      <table className="sr-only">
        <caption>What each day cost</caption>
        <tbody>
          {days.map((d) => (
            <tr key={d.day}>
              <th scope="row">{label(d.day)}</th>
              <td>{money(d.cost)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The top of the days' scale: a round figure just above the highest (a cent at least).
 * @param highest the highest cost or the budget
 * @returns the scale's top
 */
function niceTop(highest: number): number {
  if (highest <= 0.01) return 0.01
  const step = 10 ** Math.floor(Math.log10(highest))
  return Math.ceil(highest / step) * step
}

/**
 * What the money went on, as bars of one colour, the largest first, each with its figure at its end.
 * @param props the `purposes`
 * @returns the rows
 */
function Purposes({ purposes }: { purposes: SpendResponse["by_purpose"] }) {
  const sorted = purposes.filter((p) => p.cost > 0).toSorted((a, b) => b.cost - a.cost)
  const most = sorted[0]?.cost ?? 1
  return (
    <div className="grid gap-2.5 border-t px-5 py-4">
      <p className="text-sm text-steel-200">What it went on</p>
      <ul className="grid gap-2">
        {sorted.map((p) => (
          <li key={p.purpose} className="grid grid-cols-[8.5rem_minmax(0,1fr)] items-center gap-3 text-sm">
            <span className="truncate text-steel-300">{PURPOSES[p.purpose] ?? p.purpose}</span>
            <span className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className="h-2 rounded-r-[4px] bg-rigel-500"
                style={{ width: `${Math.max(2, (p.cost / most) * 80)}%` }}
              />
              <span className="shrink-0 font-mono text-xs text-steel-200 tabular-nums">{money(p.cost)}</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * The key's own limits, as OpenRouter reports them.
 * @param props the key's figures (`keyInfo`), or null when OpenRouter didn't say
 * @returns the row
 */
function KeyLimits({ keyInfo }: { keyInfo: SpendResponse["key"] }) {
  if (!keyInfo) return <Note>OpenRouter didn't say what the key's own limits are.</Note>
  const { limit, limit_remaining, usage_daily, limit_reset } = keyInfo
  return (
    <Row
      label="The key's own limit"
      hint={`Set on openrouter.ai, for every app that uses this key. It spent ${money(usage_daily)} since midnight UTC (${utcMidnight(new Date())} here).`}
    >
      <span className="font-mono text-sm text-steel-100 tabular-nums" data-testid="spend-key">
        {limit === null
          ? "No limit"
          : `${money(limit_remaining ?? 0)} left of ${money(limit)}${limit_reset ? `, resets ${limit_reset}` : ""}`}
      </span>
    </Row>
  )
}
