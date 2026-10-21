/**
 * The weather card's globe: the Earth as a sphere of points, turned so the weather's place sits a little up and right of the
 * middle, lit where the sun is up now and dark where it is night (the terminator from the sun's real position for the
 * date and time; no map data), with the place marked in your orange. Static: it is drawn again when the place changes
 * and every ten minutes, as the night moves.
 */
import { useMemo } from "react"
import { useNow } from "@/lib/hooks"
import { cn } from "@/lib/utils"

const STEP = 9 // degrees between the points, along a meridian
const REDRAW_MS = 10 * 60_000
const RAD = Math.PI / 180

/**
 * Where the sun is overhead at a moment: the subsolar point.
 * @param at the moment
 * @returns its latitude and longitude, in degrees
 */
export function subsolar(at: Date): { lat: number; lon: number } {
  const start = Date.UTC(at.getUTCFullYear(), 0, 0)
  const day = (at.getTime() - start) / 86_400_000 // the day of the year, with its fraction
  const lat = -23.44 * Math.cos((2 * Math.PI * (day + 10)) / 365.24)
  const hours = at.getUTCHours() + at.getUTCMinutes() / 60 + at.getUTCSeconds() / 3600
  const lon = ((((-15 * (hours - 12)) % 360) + 540) % 360) - 180
  return { lat, lon }
}

/**
 * A point on the sphere as a unit vector (x towards 0°E on the equator, y north, z towards 90°E).
 * @param lat its latitude in degrees
 * @param lon its longitude in degrees
 * @returns the vector
 */
const unit = (lat: number, lon: number) => [
  Math.cos(lat * RAD) * Math.cos(lon * RAD),
  Math.sin(lat * RAD),
  Math.cos(lat * RAD) * Math.sin(lon * RAD),
]

interface Dot {
  key: string
  x: number
  y: number
  depth: number // 0 at the edge of the disc, 1 facing you
  light: number // -1 deep night, 1 full day
}

/**
 * The sphere's points that face you, projected (orthographic) round a centre, each with how lit it is.
 * @param centre the latitude and longitude that face you
 * @param sun where the sun is overhead
 * @returns the points, in units of the sphere's radius
 */
function dots(centre: { lat: number; lon: number }, sun: { lat: number; lon: number }): Dot[] {
  const [s0, c0] = [Math.sin(centre.lat * RAD), Math.cos(centre.lat * RAD)]
  const toSun = unit(sun.lat, sun.lon)
  const out: Dot[] = []
  for (let lat = -81; lat <= 81; lat += STEP) {
    const ring = Math.max(1, Math.round((360 / STEP) * Math.cos(lat * RAD)))
    for (let i = 0; i < ring; i++) {
      const lon = -180 + (360 * (i + (lat / STEP) * 0.5)) / ring
      const d = (lon - centre.lon) * RAD
      const [sl, cl] = [Math.sin(lat * RAD), Math.cos(lat * RAD)]
      const z = s0 * sl + c0 * cl * Math.cos(d)
      if (z <= 0.04) continue // the far side, and the very rim
      const p = unit(lat, lon)
      out.push({
        key: `${lat}:${i}`,
        x: cl * Math.sin(d),
        y: -(c0 * sl - s0 * cl * Math.cos(d)),
        depth: z,
        light: p[0] * toSun[0] + p[1] * toSun[1] + p[2] * toSun[2],
      })
    }
  }
  return out
}

/**
 * Project a place onto the disc.
 * @param centre what faces you
 * @param lat the place's latitude
 * @param lon its longitude
 * @returns where it falls, and whether it is on the near side
 */
function project(centre: { lat: number; lon: number }, lat: number, lon: number) {
  const [s0, c0] = [Math.sin(centre.lat * RAD), Math.cos(centre.lat * RAD)]
  const d = (lon - centre.lon) * RAD
  const [sl, cl] = [Math.sin(lat * RAD), Math.cos(lat * RAD)]
  return { x: cl * Math.sin(d), y: -(c0 * sl - s0 * cl * Math.cos(d)), near: s0 * sl + c0 * cl * Math.cos(d) > 0 }
}

/**
 * The globe.
 * @param props the place's `lat` and `lon`, and extra classes (its size comes from them)
 * @returns the globe, hidden from screen readers (the place is named in the plate's header)
 */
export function Globe({ lat, lon, className }: { lat: number; lon: number; className?: string }) {
  const now = useNow(REDRAW_MS)
  // the place up and right of the middle, so the sphere's curve and the night both show
  const centre = useMemo(() => ({ lat: Math.max(-60, Math.min(60, lat * 0.55)), lon: lon - 28 }), [lat, lon])
  const points = useMemo(() => dots(centre, subsolar(now)), [centre, now])
  const here = project(centre, lat, lon)
  return (
    <svg viewBox="-1.12 -1.12 2.24 2.24" aria-hidden="true" className={cn("shrink-0 overflow-visible", className)}>
      <circle r={1} fill="var(--color-void-950)" opacity={0.6} />
      <circle r={1.06} fill="none" stroke="var(--color-rigel-500)" strokeWidth={0.015} opacity={0.35} />
      <circle r={1} fill="none" stroke="var(--color-rigel-400)" strokeWidth={0.02} opacity={0.55} />
      {points.map((d) => {
        const day = d.light > 0.06
        const dusk = !day && d.light > -0.06
        return (
          <circle
            key={d.key}
            cx={d.x}
            cy={d.y}
            r={0.028 + 0.022 * d.depth}
            fill={day ? "var(--color-rigel-300)" : dusk ? "var(--color-betel-400)" : "var(--color-steel-500)"}
            opacity={(day ? 0.35 + 0.6 * d.depth : dusk ? 0.75 : 0.18 + 0.2 * d.depth) * (0.55 + 0.45 * d.depth)}
          />
        )
      })}
      {here.near && (
        <g transform={`translate(${here.x} ${here.y})`}>
          <circle r={0.16} fill="none" stroke="var(--color-betel-300)" strokeWidth={0.025} opacity={0.7} />
          <path d="M0 -0.08 0.08 0 0 0.08 -0.08 0Z" fill="var(--color-betel-200)" />
        </g>
      )}
    </svg>
  )
}
