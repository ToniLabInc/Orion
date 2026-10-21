/**
 * Vitals: how hard the PC is working (a stats event every hud.stats_interval_s). The last minute of CPU, GPU and memory
 * as one live chart that scrolls with each reading, their figures above it as its legend (a temperature under each, in
 * red with a sign when it runs hot), then the disk and the battery as meters, and the busiest programs. A figure in the
 * warning range says so with an icon, never with colour alone.
 */
import { Meter as BaseMeter } from "@base-ui/react/meter"
import { BatteryCharging, TriangleAlert } from "lucide-react"
import { type ReactNode, useEffect } from "react"
import { useShallow } from "zustand/react/shallow"
import { useLive } from "@/lib/store"
import type { Stats } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Skeleton } from "@/ui/states"
import type { CardProps } from "./card"
import { type ChartTone, LiveChart, type Series, STROKES } from "./chart"

/** The chart's window: a minute. */
const WINDOW_S = 60

const uptime = (s: number) => `up ${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} min`
const temp = (c: number | null | undefined) => (c ? `${Math.round(c)}°C` : "")

/**
 * The vitals card.
 * @param props the card's
 * @returns the body
 */
export default function Vitals({ setNote }: CardProps) {
  const { stats, history, interval } = useLive(
    useShallow((s) => ({ stats: s.stats, history: s.vitals, interval: s.hud?.stats_interval_s })),
  )
  useEffect(() => setNote(stats ? uptime(stats.uptime) : null), [stats, setNote])
  if (!stats || !interval)
    return (
      <div className="px-4 pb-4">
        <Skeleton lines={4} />
      </div>
    )
  const points = Math.max(2, Math.round(WINDOW_S / interval))
  const gpu = stats.gpu
  const series: (Series & { now: string; note: string; hot: boolean })[] = [
    {
      key: "cpu",
      label: "CPU",
      tone: "rigel",
      values: history.map((s) => s.cpu),
      now: `${Math.round(stats.cpu)}%`,
      note: temp(stats.cpu_temp),
      hot: stats.cpu >= 90 || (stats.cpu_temp ?? 0) >= 90,
    },
    ...(gpu
      ? [
          {
            key: "gpu",
            label: "GPU",
            tone: "nebula" as ChartTone,
            values: history.map((s) => s.gpu?.util ?? 0),
            now: `${Math.round(gpu.util)}%`,
            note: temp(gpu.temp),
            hot: gpu.util >= 97 || gpu.temp >= 85,
          },
        ]
      : []),
    {
      key: "ram",
      label: "Memory",
      tone: "aurora",
      values: history.map((s) => s.ram),
      now: `${stats.ram_used.toFixed(1)} GB`,
      note: `of ${Math.round(stats.ram_total)} GB`,
      hot: stats.ram >= 90,
    },
  ]
  return (
    <div className="grid gap-4 px-4 pt-1 pb-4" data-testid="widget-vitals">
      <dl className={cn("grid gap-3", series.length === 3 ? "grid-cols-3" : "grid-cols-2")}>
        {series.map((s) => (
          <div key={s.key} className="min-w-0" data-testid={`vital-${s.key}`}>
            <dt className="flex items-center gap-1.5 text-xs text-steel-400">
              <span aria-hidden="true" className="size-1.5 rounded-full" style={{ background: STROKES[s.tone] }} />
              {s.label}
            </dt>
            <dd className={cn("mt-1 font-semibold text-xl tabular-nums", s.hot ? "text-alert-300" : "text-steel-50")}>
              {s.now}
            </dd>
            <dd
              className={cn(
                "flex items-center gap-1 text-micro tabular-nums",
                s.hot ? "text-alert-300" : "text-steel-500",
              )}
            >
              {s.hot && <TriangleAlert aria-label="High" className="size-3" />}
              {s.note}
            </dd>
          </div>
        ))}
      </dl>
      <LiveChart
        series={series}
        points={points}
        max={100}
        height={92}
        interval={interval}
        version={stats.uptime}
        format={(v) => `${Math.round(v)}%`}
        when={(n) => (n === 0 ? "Now" : `${Math.round(n * interval)} s ago`)}
      />
      <Meters stats={stats} />
      {stats.top && stats.top.length > 0 && <Programs stats={stats} />}
    </div>
  )
}

/**
 * The disk and, when there is one, the battery.
 * @param props the latest `stats`
 * @returns the meters
 */
function Meters({ stats }: { stats: Stats }) {
  const battery = stats.battery
  return (
    <div className="grid gap-2.5 border-t pt-3.5">
      <Meter label="Disk" value={stats.disk} note={`${Math.round(stats.disk_free)} GB free`} warn={stats.disk >= 90} />
      {battery && (
        <Meter
          label="Battery"
          value={battery.percent}
          warn={battery.percent < 15 && !battery.plugged}
          note={
            <>
              {battery.plugged && <BatteryCharging aria-label="Charging" className="size-3.5 text-ok-400" />}
              {Math.round(battery.percent)}%
            </>
          }
        />
      )}
    </div>
  )
}

/**
 * A meter: a thin track, filled.
 * @param props its `label`, `value` (percent), `note`, and whether it is in the warning range
 * @returns the meter
 */
function Meter({ label, value, note, warn }: { label: string; value: number; note: ReactNode; warn: boolean }) {
  const filled = Math.min(100, Math.max(0, value))
  return (
    <div className="grid grid-cols-[4.5rem_1fr_auto] items-center gap-3 text-xs">
      <span className="text-steel-400">{label}</span>
      <BaseMeter.Root value={filled} aria-label={label}>
        <BaseMeter.Track className="h-1.5 overflow-hidden rounded-full bg-steel-50/8">
          <BaseMeter.Indicator
            className={cn(
              "block h-full rounded-full transition-[width] duration-(--dur-slow) ease-(--ease-expo)",
              warn ? "bg-alert-400" : "bg-linear-to-r from-rigel-600 to-rigel-400",
            )}
          />
        </BaseMeter.Track>
      </BaseMeter.Root>
      <span
        className={cn("flex items-center gap-1 font-mono tabular-nums", warn ? "text-alert-300" : "text-steel-300")}
      >
        {warn && <TriangleAlert aria-label="Warning" className="size-3" />}
        {note}
      </span>
    </div>
  )
}

/**
 * The busiest programs: by CPU (a share of the whole CPU), then memory.
 * @param props the latest `stats`
 * @returns the list
 */
function Programs({ stats }: { stats: Stats }) {
  const top = stats.top ?? []
  const most = Math.max(1, ...top.map((p) => p.cpu))
  return (
    <div className="grid gap-1.5 border-t pt-3.5" data-testid="vital-programs">
      <p className="caps text-steel-500">Busiest programs</p>
      <ol className="grid gap-1">
        {top.map((p) => (
          <li
            key={p.name}
            className="relative grid grid-cols-[1fr_auto_4.5rem] items-center gap-3 rounded-chip px-1.5 py-1 text-xs"
          >
            <span
              aria-hidden="true"
              className="absolute inset-y-0 left-0 rounded-chip bg-rigel-400/7"
              style={{ width: `${(p.cpu / most) * 100}%` }}
            />
            <span className="relative truncate text-steel-200">{p.name}</span>
            <span className={cn("relative font-mono tabular-nums", p.cpu >= 80 ? "text-alert-300" : "text-steel-300")}>
              {p.cpu.toFixed(p.cpu < 10 ? 1 : 0)}%
            </span>
            <span className="relative text-right font-mono text-steel-500 tabular-nums">
              {p.mem.toFixed(p.mem < 10 ? 2 : 1)} GB
            </span>
          </li>
        ))}
      </ol>
    </div>
  )
}
