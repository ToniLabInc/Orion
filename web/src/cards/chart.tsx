/**
 * Charts in the cards, drawn with visx: smooth monotone curves over a soft gradient of their colour, a few dashed
 * hairlines, the newest point lit. A live chart scrolls: each new reading slides the curve one step to the left over
 * the reading's interval (one transform per reading; still under reduced motion), so it moves as continuously as time.
 * The pointer over a chart shows a crosshair with every series' value and when it was.
 */
import { curveMonotoneX } from "@visx/curve"
import { localPoint } from "@visx/event"
import { scaleLinear } from "@visx/scale"
import { area, line } from "@visx/shape"
import { motion, useReducedMotion } from "motion/react"
import { type PointerEvent, useId, useMemo, useState } from "react"
import { useWidth } from "@/lib/hooks"
import { cn } from "@/lib/utils"

export type ChartTone = "rigel" | "nebula" | "aurora" | "betel" | "dusk" | "alert" | "ok"

/** One line of a chart. */
export interface Series {
  key: string
  label: string
  values: number[] // oldest first
  tone: ChartTone
}

const STROKES: Record<ChartTone, string> = {
  rigel: "var(--color-rigel-400)",
  nebula: "var(--color-nebula-400)",
  aurora: "var(--color-aurora-400)",
  betel: "var(--color-betel-400)",
  dusk: "var(--color-dusk-400)",
  alert: "var(--color-alert-400)",
  ok: "var(--color-ok-400)",
}

/**
 * A live chart: the last `points` readings of each series, scrolling as readings come.
 * @param props the `series`, how many `points` the window holds, the value range (`min`, `max`), its `height`, the
 *   reading's `interval` in seconds (the scroll's length), a `version` that changes with each reading, how a value
 *   reads (`format`) and when a point was (`when`, from its distance to the newest)
 * @returns the chart
 */
export function LiveChart({
  series,
  points,
  min = 0,
  max,
  height = 84,
  interval,
  version,
  format,
  when,
  className,
}: {
  series: Series[]
  points: number
  min?: number
  max: number
  height?: number
  interval: number
  version: number
  format: (value: number) => string
  when: (fromNewest: number) => string
  className?: string
}) {
  const { ref, width } = useWidth<HTMLDivElement>()
  const still = useReducedMotion() ?? false
  const gradient = useId()
  const clip = useId()
  const [hover, setHover] = useState<number | null>(null)
  const pad = 4 // room for the newest point's ring
  const w = Math.max(0, width - pad)
  const step = w / Math.max(1, points - 1)
  const x = useMemo(() => scaleLinear({ domain: [0, points - 1], range: [0, w] }), [points, w])
  const y = useMemo(() => scaleLinear({ domain: [min, max], range: [height - 3, 3], clamp: true }), [min, max, height])
  // the window, plus the reading before it (it slides out on the left as the newest slides in)
  const shown = series.map((s) => {
    const window = s.values.slice(-(points + 1))
    const from = points - window.length // a short history starts further right
    return { ...s, data: window.map((v, i) => ({ i: from + i, v })) }
  })
  const pathOf = line<{ i: number; v: number }>({ x: (d) => x(d.i), y: (d) => y(d.v), curve: curveMonotoneX })
  const fillOf = area<{ i: number; v: number }>({
    x: (d) => x(d.i),
    y0: height,
    y1: (d) => y(d.v),
    curve: curveMonotoneX,
  })
  const newest = shown[0]?.data.at(-1)

  const move = (e: PointerEvent<SVGSVGElement>) => {
    const at = localPoint(e)
    if (!at || !w) return
    const i = Math.round(Math.min(points - 1, Math.max(0, at.x / step)))
    setHover((before) => (before === i ? before : i))
  }

  return (
    <div ref={ref} className={cn("relative", className)} style={{ height }}>
      {width > 0 && (
        <svg
          width={width}
          height={height}
          className="absolute inset-0 overflow-visible"
          onPointerMove={move}
          onPointerLeave={() => setHover(null)}
          role="img"
          aria-label={series.map((s) => `${s.label} ${format(s.values.at(-1) ?? 0)}`).join(", ")}
        >
          <defs>
            {shown.map((s) => (
              <linearGradient key={s.key} id={`${gradient}${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor={STROKES[s.tone]} stopOpacity={0.28} />
                <stop offset="1" stopColor={STROKES[s.tone]} stopOpacity={0} />
              </linearGradient>
            ))}
            <clipPath id={clip}>
              <rect x={0} y={-pad} width={w + pad} height={height + 2 * pad} />
            </clipPath>
          </defs>
          {[0.25, 0.5, 0.75].map((t) => (
            <line
              key={t}
              x1={0}
              x2={w}
              y1={Math.round(height * t) + 0.5}
              y2={Math.round(height * t) + 0.5}
              className="stroke-steel-50/6"
              strokeDasharray="2 4"
            />
          ))}
          <g clipPath={`url(#${clip})`}>
            <motion.g
              key={version}
              initial={{ x: still ? 0 : step }}
              animate={{ x: 0 }}
              transition={{ duration: still ? 0 : interval, ease: "linear" }}
            >
              {shown.map((s) => (
                <g key={s.key}>
                  <path d={fillOf(s.data) ?? ""} fill={`url(#${gradient}${s.key})`} />
                  <path
                    d={pathOf(s.data) ?? ""}
                    fill="none"
                    stroke={STROKES[s.tone]}
                    strokeWidth={1.5}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </g>
              ))}
              {newest && shown[0] && (
                <g transform={`translate(${x(newest.i)} ${y(newest.v)})`}>
                  <circle r={6} fill={STROKES[shown[0].tone]} opacity={0.18} />
                  <circle r={2.5} fill={STROKES[shown[0].tone]} />
                </g>
              )}
            </motion.g>
          </g>
          {hover !== null && (
            <g pointerEvents="none">
              <line
                x1={x(hover)}
                x2={x(hover)}
                y1={0}
                y2={height}
                className="stroke-steel-50/30"
                strokeDasharray="3 3"
              />
              {shown.map((s) => {
                const d = s.data.find((p) => p.i === hover)
                return d ? (
                  <circle
                    key={s.key}
                    cx={x(hover)}
                    cy={y(d.v)}
                    r={3}
                    fill="var(--color-void-950)"
                    stroke={STROKES[s.tone]}
                    strokeWidth={1.5}
                  />
                ) : null
              })}
            </g>
          )}
        </svg>
      )}
      {hover !== null && width > 0 && (
        <Readout
          left={Math.min(Math.max(x(hover), 70), width - 70)}
          title={when(points - 1 - hover)}
          rows={shown.flatMap((s) => {
            const d = s.data.find((p) => p.i === hover)
            return d ? [{ key: s.key, label: s.label, value: format(d.v), tone: s.tone }] : []
          })}
        />
      )}
    </div>
  )
}

/**
 * The crosshair's label: when, and each series' value.
 * @param props where it stands (`left`), its `title` and `rows`
 * @returns the label
 */
export function Readout({
  left,
  title,
  rows,
}: {
  left: number
  title: string
  rows: { key: string; label: string; value: string; tone: ChartTone }[]
}) {
  return (
    <div
      className="glass-solid pointer-events-none absolute bottom-[calc(100%+6px)] z-10 grid min-w-28 -translate-x-1/2 gap-0.5 rounded-control px-2.5 py-1.5 text-micro"
      style={{ left }}
    >
      <span className="text-steel-500">{title}</span>
      {rows.map((r) => (
        <span key={r.key} className="flex items-center gap-1.5 text-steel-200">
          <span aria-hidden="true" className="size-1.5 rounded-full" style={{ background: STROKES[r.tone] }} />
          {r.label}
          <span className="ml-auto pl-3 font-mono text-steel-50 tabular-nums">{r.value}</span>
        </span>
      ))}
    </div>
  )
}

export { STROKES }
