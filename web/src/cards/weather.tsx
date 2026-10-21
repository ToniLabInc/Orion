/**
 * Weather: now, the coming hours and the week for the home city (or the last place asked about), beside a globe that
 * shows where it is and where it is night. The temperature in Instrument Serif; under it a curve of the next hours
 * (with the chance of rain as bars) when Orion sends them, else the week's highs and lows as a band; then the week as
 * a strip. The curve draws itself in, and glides to a new forecast. While he fetches it every weather.refresh_s and
 * whenever you ask, it arrives as a "weather" event; bars stand in until the first one.
 */
import { curveMonotoneX } from "@visx/curve"
import { localPoint } from "@visx/event"
import { scaleLinear } from "@visx/scale"
import { area, line } from "@visx/shape"
import {
  Cloud,
  CloudDrizzle,
  CloudFog,
  CloudLightning,
  CloudRain,
  CloudSnow,
  CloudSun,
  Droplets,
  type LucideIcon,
  Sun,
  Thermometer,
  Wind,
} from "lucide-react"
import { motion } from "motion/react"
import { type PointerEvent, useEffect, useId, useMemo, useState } from "react"
import { useWidth } from "@/lib/hooks"
import { DUR, EXPO } from "@/lib/motion"
import { useLive } from "@/lib/store"
import type { Weather as WeatherData, WeatherDay } from "@/lib/types"
import { Skeleton } from "@/ui/states"
import type { CardProps } from "./card"
import { Readout } from "./chart"
import { Globe } from "./globe"

/**
 * The icon for a WMO weather code (what Open-Meteo reports).
 * @param code the code
 * @returns the icon
 */
export function iconFor(code: number): LucideIcon {
  if (code === 0) return Sun
  if (code <= 2) return CloudSun
  if (code === 3) return Cloud
  if (code <= 48) return CloudFog
  if (code <= 57) return CloudDrizzle
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return CloudSnow
  if (code >= 95) return CloudLightning
  return CloudRain
}

const weekday = (day: WeatherDay, short = true) =>
  new Date(`${day.date}T12:00`).toLocaleDateString("en-GB", { weekday: short ? "short" : "long" })

/**
 * The weather card.
 * @param props the card's
 * @returns the body
 */
export default function Weather({ setNote }: CardProps) {
  const weather = useLive((s) => s.weather)
  const online = useLive((s) => s.link === "online")
  useEffect(() => setNote(weather?.place ?? null), [weather?.place, setNote])
  if (!weather)
    return (
      <div className="px-4 pb-4">
        {online ? (
          <p className="text-sm text-steel-400">Ask “What's the weather?” and tell him your city.</p>
        ) : (
          <Skeleton lines={3} />
        )}
      </div>
    )
  const Now = iconFor(weather.code)
  const [today] = weather.days
  return (
    <div className="grid gap-4 px-4 pt-1 pb-4" data-testid="widget-weather">
      <div className="flex items-center gap-4">
        {weather.lat !== undefined && weather.lon !== undefined && (
          <Globe lat={weather.lat} lon={weather.lon} className="size-16" />
        )}
        <div className="min-w-0">
          <p className="font-serif text-4xl text-steel-50 tabular-nums leading-none">
            {Math.round(weather.temp)}
            <span className="text-steel-400">°</span>
          </p>
          <p className="mt-1.5 flex items-center gap-1.5 text-sm text-steel-200">
            <Now aria-hidden="true" className="size-4 shrink-0 text-aurora-300" />
            <span className="truncate first-letter:uppercase">{weather.condition}</span>
          </p>
        </div>
        {today && (
          <p className="ml-auto grid shrink-0 gap-0.5 text-right font-mono text-xs text-steel-500 tabular-nums">
            <span>
              H <span className="text-steel-100">{Math.round(today.max)}°</span>
            </span>
            <span>
              L <span className="text-steel-100">{Math.round(today.min)}°</span>
            </span>
          </p>
        )}
      </div>
      <Curve weather={weather} />
      <ol className="grid grid-cols-7 gap-1" aria-label="The week">
        {weather.days.slice(0, 7).map((day, i) => {
          const Icon = iconFor(day.code)
          return (
            <li
              key={day.date}
              title={`${weekday(day, false)}: ${day.condition}, ${day.rain_chance}% rain`}
              className="grid justify-items-center gap-1 rounded-control py-1.5 text-center"
            >
              <span className="text-micro text-steel-500">{i === 0 ? "Today" : weekday(day)}</span>
              <Icon aria-label={day.condition} className="size-4 text-steel-300" />
              <span className="font-mono text-xs text-steel-100 tabular-nums">{Math.round(day.max)}°</span>
              <span className="font-mono text-micro text-steel-500 tabular-nums">{Math.round(day.min)}°</span>
            </li>
          )
        })}
      </ol>
      <dl className="grid grid-cols-3 gap-2 border-t pt-3 text-xs">
        <Fact icon={Thermometer} label="Feels like" value={`${Math.round(weather.feels_like)}°`} />
        <Fact icon={Droplets} label="Humidity" value={`${Math.round(weather.humidity)}%`} />
        <Fact icon={Wind} label="Wind" value={`${Math.round(weather.wind)} km/h`} />
      </dl>
    </div>
  )
}

/**
 * One fact under the week.
 * @param props its `icon`, `label` and `value`
 * @returns the fact
 */
function Fact({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <div className="grid gap-0.5">
      <dt className="flex items-center gap-1 text-steel-500">
        <Icon aria-hidden="true" className="size-3" />
        {label}
      </dt>
      <dd className="font-mono text-steel-100 tabular-nums">{value}</dd>
    </div>
  )
}

interface Point {
  i: number
  hi: number
  lo: number // the same as hi for an hour
  rain: number
  label: string
}

/**
 * The curve: the next hours (with the rain's chance as bars), or the week's highs and lows as a band.
 * @param props the `weather`
 * @returns the chart
 */
function Curve({ weather }: { weather: WeatherData }) {
  const { ref, width } = useWidth<HTMLDivElement>()
  const fill = useId()
  const [hover, setHover] = useState<number | null>(null)
  const hourly = Boolean(weather.hours?.length)
  const data: Point[] = useMemo(
    () =>
      hourly
        ? (weather.hours ?? []).slice(0, 24).map((h, i) => ({
            i,
            hi: h.temp,
            lo: h.temp,
            rain: h.rain_chance,
            label:
              i === 0 && Math.abs(new Date(h.time).getTime() - Date.now()) < 3_600_000
                ? "Now"
                : new Date(h.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }),
          }))
        : weather.days.slice(0, 7).map((d, i) => ({
            i,
            hi: d.max,
            lo: d.min,
            rain: d.rain_chance,
            label: i === 0 ? "Today" : weekday(d, false),
          })),
    [weather, hourly],
  )
  // top to bottom: the curve, the rain's bars on their baseline, and (for hours) a time every six hours
  const height = hourly ? 116 : 92
  const base = height - (hourly ? 18 : 2)
  const floor = base - 18
  const low = Math.min(...data.map((d) => d.lo)) - 2
  const high = Math.max(...data.map((d) => d.hi)) + 2
  const x = scaleLinear({ domain: [0, Math.max(1, data.length - 1)], range: [4, Math.max(5, width - 4)] })
  const y = scaleLinear({ domain: [low, high], range: [floor - 2, 18] })
  const inside = (at: number) => Math.min(Math.max(at, 12), width - 12)
  // the warmest and the coldest hour, marked on the curve
  const marks = hourly
    ? [data.reduce((a, b) => (b.hi > a.hi ? b : a)), data.reduce((a, b) => (b.hi < a.hi ? b : a))].filter(
        (d, i, all) => all.findIndex((o) => o.i === d.i) === i,
      )
    : []
  const top = line<Point>({ x: (d) => x(d.i), y: (d) => y(d.hi), curve: curveMonotoneX })(data) ?? ""
  const bottom = line<Point>({ x: (d) => x(d.i), y: (d) => y(d.lo), curve: curveMonotoneX })(data) ?? ""
  const band =
    area<Point>({
      x: (d) => x(d.i),
      y0: hourly ? () => floor : (d) => y(d.lo),
      y1: (d) => y(d.hi),
      curve: curveMonotoneX,
    })(data) ?? ""
  const move = (e: PointerEvent<SVGSVGElement>) => {
    const at = localPoint(e)
    if (!at || data.length < 2) return
    const i = Math.round(Math.min(data.length - 1, Math.max(0, x.invert(at.x))))
    setHover((before) => (before === i ? before : i))
  }
  const shown = hover === null ? null : data[hover]
  return (
    <div ref={ref} className="relative" style={{ height }}>
      {width > 0 && data.length > 1 && (
        <svg
          width={width}
          height={height}
          className="absolute inset-0 overflow-visible"
          onPointerMove={move}
          onPointerLeave={() => setHover(null)}
          role="img"
          aria-label={hourly ? "Temperature over the next hours" : "The week's highs and lows"}
        >
          <defs>
            <linearGradient id={fill} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="var(--color-aurora-400)" stopOpacity={0.3} />
              <stop offset="1" stopColor="var(--color-aurora-400)" stopOpacity={0} />
            </linearGradient>
          </defs>
          {data.map((d) => (
            // the chance of rain, as a thin bar under each point
            <rect
              key={d.i}
              x={x(d.i) - 1.5}
              y={base - Math.max(1, (d.rain / 100) * 14)}
              width={3}
              height={Math.max(1, (d.rain / 100) * 14)}
              rx={1.5}
              className="fill-rigel-400/45"
            />
          ))}
          <motion.path
            d={band}
            fill={`url(#${fill})`}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1, d: band }}
            transition={{ duration: DUR.slow, ease: [...EXPO] }}
          />
          {!hourly && (
            <motion.path
              d={bottom}
              fill="none"
              stroke="var(--color-rigel-400)"
              strokeOpacity={0.6}
              strokeWidth={1.5}
              strokeDasharray="3 3"
              initial={{ pathLength: 0 }}
              animate={{ pathLength: 1, d: bottom }}
              transition={{ duration: DUR.ambient, ease: [...EXPO] }}
            />
          )}
          <motion.path
            d={top}
            fill="none"
            stroke="var(--color-aurora-400)"
            strokeWidth={1.75}
            strokeLinecap="round"
            initial={{ pathLength: 0 }}
            animate={{ pathLength: 1, d: top }}
            transition={{ duration: DUR.ambient, ease: [...EXPO] }}
          />
          {marks.map((d) => (
            <text
              key={`mark-${d.i}`}
              x={inside(x(d.i))}
              y={y(d.hi) - 7}
              textAnchor="middle"
              className="fill-steel-200 font-mono text-micro"
            >
              {Math.round(d.hi)}°
            </text>
          ))}
          {hourly &&
            data
              .filter((d) => d.i % 6 === 0)
              .map((d) => (
                <text
                  key={`time-${d.i}`}
                  x={d.i === 0 ? x(d.i) : inside(x(d.i))}
                  y={height - 3}
                  textAnchor={d.i === 0 ? "start" : "middle"}
                  className="fill-steel-500 font-mono text-micro"
                >
                  {d.label}
                </text>
              ))}
          {shown && (
            <g pointerEvents="none">
              <line
                x1={x(shown.i)}
                x2={x(shown.i)}
                y1={0}
                y2={floor}
                className="stroke-steel-50/30"
                strokeDasharray="3 3"
              />
              <circle
                cx={x(shown.i)}
                cy={y(shown.hi)}
                r={3}
                fill="var(--color-void-950)"
                stroke="var(--color-aurora-400)"
                strokeWidth={1.5}
              />
              {!hourly && (
                <circle
                  cx={x(shown.i)}
                  cy={y(shown.lo)}
                  r={3}
                  fill="var(--color-void-950)"
                  stroke="var(--color-rigel-400)"
                  strokeWidth={1.5}
                />
              )}
            </g>
          )}
        </svg>
      )}
      {shown && width > 0 && (
        <Readout
          left={Math.min(Math.max(x(shown.i), 70), width - 70)}
          title={shown.label}
          rows={[
            { key: "hi", label: hourly ? "Temperature" : "High", value: `${Math.round(shown.hi)}°`, tone: "aurora" },
            ...(hourly ? [] : [{ key: "lo", label: "Low", value: `${Math.round(shown.lo)}°`, tone: "rigel" as const }]),
            { key: "rain", label: "Rain", value: `${shown.rain}%`, tone: "rigel" },
          ]}
        />
      )}
    </div>
  )
}
