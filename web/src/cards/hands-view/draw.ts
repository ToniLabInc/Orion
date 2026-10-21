// The live view's geometry, kept pure for the tests: where the camera's field sits in the canvas, where a landmark
// lands, how the hand's bones join, how far a held shape is from firing, the fingertips' trails, and the tether
// between two hands.
import type { GestureBinding } from "@/lib/types"

/** The 21-point hand: which landmarks a bone joins (MediaPipe's numbering). */
export const BONES: readonly (readonly [number, number])[] = [
  [0, 1],
  [1, 2],
  [2, 3],
  [3, 4], // thumb
  [0, 5],
  [5, 6],
  [6, 7],
  [7, 8], // index
  [5, 9],
  [9, 10],
  [10, 11],
  [11, 12], // middle
  [9, 13],
  [13, 14],
  [14, 15],
  [15, 16], // ring
  [13, 17],
  [17, 18],
  [18, 19],
  [19, 20], // little finger
  [0, 17], // the heel of the palm
]
/** The fingertips (drawn a little larger). */
export const TIPS: ReadonlySet<number> = new Set([4, 8, 12, 16, 20])
/** The palm's points: its centre is their mean (where a held shape's ring goes). */
export const PALM: readonly number[] = [0, 5, 9, 13, 17]

export interface Box {
  x: number
  y: number
  w: number
  h: number
}

/**
 * Where the camera's field goes in a canvas: as large as fits, keeping its proportions, centred.
 * @param width the canvas's width
 * @param height its height
 * @param aspect the camera's width / height (4:3 when unknown)
 * @param pad room left round it
 * @returns the field's box
 */
export function fieldBox(width: number, height: number, aspect = 4 / 3, pad = 0): Box {
  const room = { w: Math.max(0, width - 2 * pad), h: Math.max(0, height - 2 * pad) }
  const w = Math.min(room.w, room.h * aspect)
  const h = w / aspect
  return { x: (width - w) / 2, y: (height - h) / 2, w, h }
}

/**
 * Where a landmark lands in the canvas.
 * @param point x and y from 0 to 1 in the camera's (mirrored) image
 * @param box the camera's field in the canvas
 * @returns the canvas point
 */
export function place(point: readonly [number, number], box: Box): [number, number] {
  return [box.x + point[0] * box.w, box.y + point[1] * box.h]
}

/**
 * The centre of a hand's palm.
 * @param pts its landmarks (canvas or image points)
 * @returns the centre
 */
export function palmCentre(pts: readonly (readonly [number, number])[]): [number, number] {
  let x = 0
  let y = 0
  for (const i of PALM) {
    x += pts[i]?.[0] ?? 0
    y += pts[i]?.[1] ?? 0
  }
  return [x / PALM.length, y / PALM.length]
}

/**
 * How long a hand shape must be held to fire what it is bound to, in seconds: until the tracker counts it, or the
 * binding's own hold when that is longer; null for a shape bound to nothing.
 * @param binding what the shape does
 * @param counts how long the tracker waits before it counts a shape (its static_hold; 0 when it doesn't say)
 * @returns the seconds, or null
 */
export function holdNeeded(binding: GestureBinding | undefined, counts: number): number | null {
  if (!binding) return null
  return Math.max(counts, typeof binding.hold === "number" ? binding.hold : 0)
}

/**
 * How far a held shape is from firing, 0 to 1.
 * @param held how long it has been held
 * @param needed how long it must be (holdNeeded)
 * @returns the progress
 */
export function progress(held: number, needed: number | null): number {
  if (needed === null || needed <= 0) return 0
  return Math.min(1, Math.max(0, held / needed))
}

/**
 * Ease a hand's points towards where the camera now sees them (smooth movement between frames).
 * @param from where they are drawn now (null: not yet: they jump there)
 * @param to where they are
 * @param k how far to go this step, 0 to 1
 * @returns the points to draw
 */
export function ease(
  from: readonly (readonly [number, number])[] | null,
  to: readonly (readonly [number, number])[],
  k: number,
): [number, number][] {
  if (!from || from.length !== to.length) return to.map(([x, y]) => [x, y])
  return to.map(([x, y], i) => {
    const [fx, fy] = from[i] as readonly [number, number]
    return [fx + (x - fx) * k, fy + (y - fy) * k]
  })
}

/** How long a fingertip's trail lasts behind it, in ms. */
export const TRAIL_MS = 220

/** A fingertip's recent places (image points), oldest first, each with when it was there (performance.now()). */
export type Trail = { x: number; y: number; at: number }[]

/**
 * A fingertip's trail, with where it is now and without what is older than its length.
 * @param trail the trail so far
 * @param point where the tip is now (an image point)
 * @param at now (performance.now())
 * @returns the trail
 */
export function extend(trail: Trail, point: readonly [number, number], at: number): Trail {
  const kept = trail.filter((p) => at - p.at < TRAIL_MS)
  const last = kept.at(-1)
  if (!last || Math.hypot(point[0] - last.x, point[1] - last.y) > 0.0008) kept.push({ x: point[0], y: point[1], at })
  return kept
}

/** Which of the two-hand gestures the hands' distance counts towards now. */
export type Pair = "together" | "between" | "apart"

/**
 * The tether between two hands: where, along the line between their palms, hands_together and hands_apart begin (on
 * either side of the middle, half of each distance away), and which of the two the distance counts as now.
 * @param a one palm's centre (canvas point)
 * @param b the other's
 * @param apart how far apart they are, in palm lengths (the tracker's own measure)
 * @param counts the distances, in palm lengths, that make them together and apart
 * @returns the line's direction, the marks (pairs of canvas points) and the state; null when it can't be drawn
 */
export function tether(
  a: readonly [number, number],
  b: readonly [number, number],
  apart: number,
  counts: { together: number; apart: number },
): { dir: [number, number]; together: [number, number][]; apart: [number, number][]; state: Pair } | null {
  const length = Math.hypot(b[0] - a[0], b[1] - a[1])
  if (length < 1 || !(apart > 0)) return null
  const perPalm = length / apart
  const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
  const dir: [number, number] = [(b[0] - a[0]) / length, (b[1] - a[1]) / length]
  const marks = (palms: number): [number, number][] =>
    [-1, 1].map((side) => [
      mid[0] + (side * dir[0] * palms * perPalm) / 2,
      mid[1] + (side * dir[1] * palms * perPalm) / 2,
    ])
  const state: Pair = apart <= counts.together ? "together" : apart >= counts.apart ? "apart" : "between"
  return { dir, together: marks(counts.together), apart: marks(counts.apart), state }
}
