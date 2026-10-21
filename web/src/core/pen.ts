/**
 * Light-writing: a short text, or a question or check mark, written above the limb in single strokes of light, the
 * way a pen of light would leave it on a long exposure.
 *
 * The glyphs are Horizon's own: monoline paths in an em box (y up, the baseline at 0, digits and capitals 1 tall,
 * the x-height 0.7), each a single stroke wherever a pen can make it one (a 4 from its crossbar round to its stem;
 * an 8 as one figure of eight), dots as tiny closed loops. A path is SVG-like: `M x y`, `L x y`, `C x1 y1 x2 y2 x y`
 * and `A cx cy rx ry from to` (an elliptical arc, degrees, anticlockwise).
 *
 * The pen moves as a hand does: its speed follows the two-thirds power law of handwriting (Lacquaniti, Terzuolo
 * and Viviani 1983), v = K·κ^(−1/3), slower round tight curves, quick on the straights, easing in where it lands and
 * out where it lifts, and travelling between strokes with the light off. A long exposure keeps more light where the
 * pen dwells, so each point's ink grows as its speed falls (√, kept within INK_RANGE): corners and stroke ends glow
 * a little brighter. `write` lays the text out and returns the strokes as segments, each end with the moment the pen
 * passes it and its ink, for the shader. Plain numbers: tested in Node.
 */

/** A glyph: how far the pen moves on after it (em), and its strokes. */
interface Glyph {
  advance: number
  strokes: readonly string[]
}

/** The glyphs: every character protocol.ts's WRITABLE lets through, and the two shapes. */
export const GLYPHS: Readonly<Record<string, Glyph>> = {
  "0": { advance: 0.64, strokes: ["A 0.32 0.5 0.235 0.5 95 460"] },
  "1": { advance: 0.64, strokes: ["M 0.15 0.8 C 0.24 0.86 0.31 0.93 0.36 1 L 0.36 0"] },
  "2": {
    advance: 0.64,
    strokes: [
      "M 0.1 0.74 C 0.1 0.92 0.22 1 0.32 1 C 0.44 1 0.54 0.92 0.54 0.76 C 0.54 0.62 0.46 0.52 0.36 0.42 L 0.08 0 L 0.56 0",
    ],
  },
  "3": {
    advance: 0.64,
    strokes: [
      "M 0.1 0.84 C 0.16 0.95 0.24 1 0.33 1 C 0.45 1 0.53 0.92 0.53 0.79 C 0.53 0.65 0.43 0.57 0.28 0.57 C 0.45 0.57 0.55 0.46 0.55 0.3 C 0.55 0.12 0.45 0 0.31 0 C 0.21 0 0.13 0.05 0.08 0.15",
    ],
  },
  "4": { advance: 0.64, strokes: ["M 0.56 0.3 L 0.06 0.3 L 0.41 1 L 0.41 0"] },
  "5": {
    advance: 0.64,
    strokes: [
      "M 0.52 1 L 0.15 1 L 0.11 0.57 C 0.18 0.62 0.25 0.645 0.32 0.645 C 0.46 0.645 0.555 0.52 0.555 0.33 C 0.555 0.13 0.45 0 0.3 0 C 0.2 0 0.12 0.05 0.07 0.14",
    ],
  },
  "6": {
    advance: 0.64,
    strokes: [
      "M 0.5 0.93 C 0.45 0.98 0.39 1 0.33 1 C 0.17 1 0.08 0.8 0.08 0.48 C 0.08 0.17 0.18 0 0.32 0 C 0.46 0 0.555 0.12 0.555 0.3 C 0.555 0.49 0.45 0.6 0.32 0.6 C 0.2 0.6 0.11 0.51 0.085 0.37",
    ],
  },
  "7": { advance: 0.64, strokes: ["M 0.08 1 L 0.56 1 C 0.42 0.74 0.3 0.42 0.24 0"] },
  "8": {
    advance: 0.64,
    strokes: [
      "M 0.32 0.56 C 0.18 0.6 0.115 0.68 0.115 0.785 C 0.115 0.92 0.205 1 0.32 1 C 0.435 1 0.525 0.92 0.525 0.785 C 0.525 0.68 0.46 0.6 0.32 0.56 C 0.165 0.515 0.085 0.42 0.085 0.28 C 0.085 0.11 0.19 0 0.32 0 C 0.45 0 0.555 0.11 0.555 0.28 C 0.555 0.42 0.475 0.515 0.32 0.56",
    ],
  },
  "9": {
    advance: 0.64,
    strokes: [
      "M 0.545 0.66 C 0.51 0.52 0.42 0.42 0.3 0.42 C 0.17 0.42 0.085 0.53 0.085 0.7 C 0.085 0.88 0.185 1 0.315 1 C 0.465 1 0.555 0.86 0.555 0.6 C 0.555 0.24 0.44 0 0.26 0 C 0.18 0 0.12 0.03 0.085 0.08",
    ],
  },
  ":": { advance: 0.3, strokes: ["A 0.15 0.68 0.03 0.03 90 450", "A 0.15 0.16 0.03 0.03 90 450"] },
  ".": { advance: 0.26, strokes: ["A 0.13 0.035 0.03 0.03 90 450"] },
  ",": { advance: 0.26, strokes: ["M 0.14 0.07 C 0.15 0.02 0.13 -0.07 0.08 -0.14"] },
  "°": { advance: 0.4, strokes: ["A 0.2 0.86 0.11 0.11 90 455"] },
  "%": {
    advance: 0.66,
    strokes: ["A 0.16 0.82 0.1 0.13 90 455", "M 0.53 1 L 0.11 0", "A 0.48 0.18 0.1 0.13 90 455"],
  },
  "+": { advance: 0.64, strokes: ["M 0.32 0.22 L 0.32 0.74", "M 0.08 0.48 L 0.56 0.48"] },
  "-": { advance: 0.48, strokes: ["M 0.08 0.46 L 0.4 0.46"] },
  "−": { advance: 0.64, strokes: ["M 0.08 0.46 L 0.56 0.46"] },
  "/": { advance: 0.54, strokes: ["M 0.46 1.04 L 0.08 -0.04"] },
  " ": { advance: 0.32, strokes: [] },
  h: {
    advance: 0.63,
    strokes: ["M 0.1 1 L 0.1 0 L 0.1 0.42 C 0.15 0.6 0.24 0.7 0.34 0.7 C 0.46 0.7 0.53 0.62 0.53 0.46 L 0.53 0"],
  },
  m: {
    advance: 0.82,
    strokes: [
      "M 0.09 0.7 L 0.09 0 L 0.09 0.44 C 0.12 0.61 0.19 0.7 0.27 0.7 C 0.36 0.7 0.41 0.63 0.41 0.5 L 0.41 0 L 0.41 0.5 C 0.44 0.63 0.5 0.7 0.59 0.7 C 0.68 0.7 0.73 0.63 0.73 0.5 L 0.73 0",
    ],
  },
  s: {
    advance: 0.56,
    strokes: [
      "M 0.47 0.61 C 0.42 0.67 0.35 0.7 0.28 0.7 C 0.17 0.7 0.1 0.64 0.1 0.54 C 0.1 0.43 0.19 0.4 0.29 0.37 C 0.41 0.34 0.48 0.29 0.48 0.18 C 0.48 0.07 0.39 0 0.28 0 C 0.19 0 0.12 0.03 0.07 0.1",
    ],
  },
  a: {
    advance: 0.6,
    strokes: [
      "M 0.5 0.56 C 0.46 0.65 0.39 0.7 0.3 0.7 C 0.17 0.7 0.08 0.57 0.08 0.35 C 0.08 0.13 0.17 0 0.3 0 C 0.39 0 0.46 0.05 0.5 0.14 L 0.5 0.7 L 0.5 0",
    ],
  },
  p: {
    advance: 0.62,
    strokes: [
      "M 0.1 0.7 L 0.1 -0.3 L 0.1 0.36 C 0.1 0.58 0.2 0.7 0.32 0.7 C 0.46 0.7 0.54 0.56 0.54 0.35 C 0.54 0.14 0.45 0 0.32 0 C 0.2 0 0.1 0.12 0.1 0.32",
    ],
  },
  C: {
    advance: 0.65,
    strokes: [
      "M 0.57 0.85 C 0.51 0.95 0.42 1 0.33 1 C 0.18 1 0.08 0.8 0.08 0.5 C 0.08 0.2 0.18 0 0.33 0 C 0.42 0 0.51 0.05 0.57 0.15",
    ],
  },
  F: { advance: 0.58, strokes: ["M 0.53 1 L 0.1 1 L 0.1 0", "M 0.1 0.53 L 0.45 0.53"] },
  question: {
    advance: 0.62,
    strokes: [
      "M 0.1 0.79 C 0.12 0.94 0.22 1 0.32 1 C 0.45 1 0.54 0.91 0.54 0.78 C 0.54 0.64 0.45 0.58 0.37 0.52 C 0.31 0.48 0.3 0.42 0.3 0.3",
      "A 0.3 0.04 0.035 0.035 90 450",
    ],
  },
  check: { advance: 0.84, strokes: ["M 0.06 0.44 L 0.27 0.16 C 0.38 0.4 0.52 0.68 0.76 0.96"] },
}

/** A point of a flattened stroke, em (y up). */
type Point = [number, number]

/** How far a flattened curve may stray from the true one, em. */
const TOLERANCE = 0.0025

/**
 * A stroke's path as a polyline: curves and arcs cut into chords no further than TOLERANCE from them.
 * @param path the stroke (M, L, C, A commands)
 * @returns its points, em, y up
 * @throws Error on a command it doesn't know, or a path that doesn't start with M or A
 */
export function flatten(path: string): Point[] {
  const tokens = path.trim().split(/\s+/)
  const points: Point[] = []
  let i = 0
  const num = () => {
    const v = Number(tokens[i++])
    if (!Number.isFinite(v)) throw new Error(`a number expected in "${path}"`)
    return v
  }
  let at: Point | null = null
  while (i < tokens.length) {
    const command = tokens[i++]
    if (command === "M") {
      if (at) throw new Error(`a stroke moves only at its start: "${path}"`)
      at = [num(), num()]
      points.push(at)
    } else if (command === "L") {
      if (!at) throw new Error(`a stroke starts with M or A: "${path}"`)
      at = [num(), num()]
      points.push(at)
    } else if (command === "C") {
      if (!at) throw new Error(`a stroke starts with M or A: "${path}"`)
      const p0 = at
      const p1: Point = [num(), num()]
      const p2: Point = [num(), num()]
      const p3: Point = [num(), num()]
      // enough chords that each strays at most TOLERANCE: the control polygon's bend bounds the curve's
      const bend = Math.max(
        Math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
        Math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]),
      )
      const n = Math.max(2, Math.ceil(Math.sqrt((3 * bend) / (4 * TOLERANCE))))
      for (let s = 1; s <= n; s++) {
        const t = s / n
        const u = 1 - t
        points.push([
          u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
          u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
        ])
      }
      at = p3
    } else if (command === "A") {
      const [cx, cy, rx, ry, from, to] = [num(), num(), num(), num(), num(), num()]
      const sweep = ((to - from) * Math.PI) / 180
      // a chord over an angle Δ on a radius r strays r(1 − cos Δ/2)
      const step = 2 * Math.acos(1 - TOLERANCE / Math.max(rx, ry))
      const n = Math.max(6, Math.ceil(Math.abs(sweep) / step))
      const start = (from * Math.PI) / 180
      for (let s = at ? 1 : 0; s <= n; s++) {
        const a = start + (sweep * s) / n
        points.push([cx + rx * Math.cos(a), cy + ry * Math.sin(a)])
      }
      at = points[points.length - 1]
    } else throw new Error(`unknown command "${command}" in "${path}"`)
  }
  if (!points.length) throw new Error(`an empty stroke: "${path}"`)
  return points
}

// ---------------------------------------------------------------------------------------------------- the pen
/** K of the power law, em^(2/3)/s: a curve of radius 0.25 em is written at 12 em/s. */
export const POWER_K = 12 * 4 ** (1 / 3)
/** The fastest the pen goes (a straight), em/s, and how slowly it lands and lifts. */
export const TOP_SPEED = 26
export const LAND_SPEED = 3.5
/** How far it takes to get up to speed after landing, and to slow before lifting, em. */
const LANDING = 0.09
const LIFTING = 0.07
/** Between strokes: the pen lifts for this long, then travels this fast, em/s. */
export const LIFT_S = 0.05
const TRAVEL_SPEED = 30
/** The ink's range: what the slowest and the quickest parts of a stroke keep. */
export const INK_RANGE = [0.8, 1.45] as const
/** The speed whose ink is 1, em/s. */
const INK_SPEED = 13

/** A segment of the written light, CSS px (y down), with when the pen passes each end (s) and the ink there. */
export interface Segment {
  x0: number
  y0: number
  x1: number
  y1: number
  t0: number
  t1: number
  ink0: number
  ink1: number
}

/** A writing laid out: its segments, how long the pen takes, and its box round (0, 0), CSS px. */
export interface Writing {
  segments: Segment[]
  duration: number
  width: number
  height: number
}

/**
 * The pen's speed at each point of a polyline (em/s): the power law from the curvature there, eased where it lands
 * and where it lifts.
 * @param points the polyline, em
 * @returns one speed per point
 */
export function speeds(points: readonly Point[]): number[] {
  const n = points.length
  const length = (i: number) => Math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
  const out: number[] = []
  let travelled = 0
  const total = points.slice(1).reduce((sum, _, i) => sum + length(i), 0)
  for (let i = 0; i < n; i++) {
    let curvature = 0
    if (i > 0 && i < n - 1) {
      const a = Math.atan2(points[i][1] - points[i - 1][1], points[i][0] - points[i - 1][0])
      const b = Math.atan2(points[i + 1][1] - points[i][1], points[i + 1][0] - points[i][0])
      let turn = Math.abs(b - a)
      if (turn > Math.PI) turn = 2 * Math.PI - turn
      const span = (length(i - 1) + length(i)) / 2
      curvature = span > 0 ? turn / span : 0
    }
    let v = curvature > 0 ? Math.min(TOP_SPEED, POWER_K * curvature ** (-1 / 3)) : TOP_SPEED
    // landing and lifting: up to speed over LANDING, down over LIFTING
    const ease = Math.min(1, travelled / LANDING, (total - travelled) / LIFTING)
    v = LAND_SPEED + (v - LAND_SPEED) * Math.max(0, ease)
    out.push(v)
    if (i < n - 1) travelled += length(i)
  }
  // a corner is a moment, not a place: let the slowness spread to its neighbours
  return out.map((v, i) => Math.min(v, ((out[i - 1] ?? v) + v + (out[i + 1] ?? v)) / 3))
}

/**
 * The ink a pen leaves at a speed: more where it dwells.
 * @param v em/s
 * @returns the ink, within INK_RANGE
 */
export const inkAt = (v: number): number => Math.min(INK_RANGE[1], Math.max(INK_RANGE[0], Math.sqrt(INK_SPEED / v)))

/**
 * Lay out a text (or a shape: "question", "check") and time the pen over it, centred on (0, 0) with its baseline
 * through y = height / 2 (y down).
 * @param glyphs the characters, or one shape's name
 * @param em the digits' height, CSS px
 * @param pace the pen's speed, as a share of a hand's (a lone mark is drawn more deliberately than a word)
 * @returns the segments and the time they take
 * @throws Error for a character without a glyph
 */
export function write(glyphs: readonly string[], em: number, pace = 1): Writing {
  const strokes: Point[][] = []
  let x = 0
  for (const char of glyphs) {
    const glyph = GLYPHS[char]
    if (!glyph) throw new Error(`the pen has no glyph for "${char}"`)
    for (const path of glyph.strokes) strokes.push(flatten(path).map(([px, py]) => [x + px, py]))
    x += glyph.advance
  }
  // the ink's own box, without the side bearings of the first and last glyph
  let minX = Number.POSITIVE_INFINITY
  let maxX = Number.NEGATIVE_INFINITY
  for (const stroke of strokes)
    for (const [px] of stroke) {
      minX = Math.min(minX, px)
      maxX = Math.max(maxX, px)
    }
  if (!strokes.length) return { segments: [], duration: 0, width: 0, height: em }
  const cx = (minX + maxX) / 2
  const segments: Segment[] = []
  let t = 0
  let last: Point | null = null
  for (const stroke of strokes) {
    if (last) t += (LIFT_S + Math.hypot(stroke[0][0] - last[0], stroke[0][1] - last[1]) / TRAVEL_SPEED) / pace
    const v = speeds(stroke).map((speed) => speed * pace)
    for (let i = 0; i < stroke.length - 1; i++) {
      const [ax, ay] = stroke[i]
      const [bx, by] = stroke[i + 1]
      const dt = Math.hypot(bx - ax, by - ay) / ((v[i] + v[i + 1]) / 2)
      segments.push({
        x0: (ax - cx) * em,
        y0: (0.5 - ay) * em,
        x1: (bx - cx) * em,
        y1: (0.5 - by) * em,
        t0: t,
        t1: t + dt,
        ink0: inkAt(v[i] / pace),
        ink1: inkAt(v[i + 1] / pace),
      })
      t += dt
    }
    last = stroke[stroke.length - 1]
  }
  return { segments, duration: t, width: (maxX - minX) * em, height: em }
}
