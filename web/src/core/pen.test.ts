import { expect, test } from "vitest"
import { flatten, GLYPHS, INK_RANGE, inkAt, LAND_SPEED, POWER_K, speeds, TOP_SPEED, write } from "./pen"
import { WRITABLE } from "./protocol"

test("the pen has a glyph for every character it is asked to write, and for the two marks", () => {
  for (const c of WRITABLE) expect(GLYPHS[c], `"${c}"`).toBeDefined()
  expect(GLYPHS.question).toBeDefined()
  expect(GLYPHS.check).toBeDefined()
  expect(() => write(["q"], 100)).toThrow(/no glyph for "q"/)
})

test("every glyph is a well-formed single stroke or a few, inside its em box", () => {
  for (const [name, glyph] of Object.entries(GLYPHS)) {
    expect(glyph.advance, name).toBeGreaterThan(0.2)
    for (const stroke of glyph.strokes) {
      const points = flatten(stroke)
      expect(points.length, name).toBeGreaterThan(0)
      for (const [x, y] of points) {
        expect(x, name).toBeGreaterThan(-0.05)
        expect(x, name).toBeLessThan(glyph.advance + 0.05)
        expect(y, name).toBeGreaterThan(-0.35) // a comma and a p go below the line
        expect(y, name).toBeLessThan(1.1)
      }
    }
  }
  // digits are one stroke each, as a pen writes them (a 4 round from its bar to its stem; an 8 as one figure of eight)
  for (const d of "0123456789") expect(GLYPHS[d].strokes, d).toHaveLength(1)
})

test("curves are cut into chords that stray no further than the tolerance from the true curve", () => {
  const r = 0.32
  const points = flatten(`A 0.5 0.5 ${r} ${r} 0 360`)
  for (let i = 0; i < points.length - 1; i++) {
    const [ax, ay] = points[i]
    const [bx, by] = points[i + 1]
    const mid = Math.hypot((ax + bx) / 2 - 0.5, (ay + by) / 2 - 0.5)
    expect(r - mid).toBeLessThan(0.003)
  }
  expect(() => flatten("L 1 1")).toThrow(/starts with M or A/)
  expect(() => flatten("M 0 0 M 1 1")).toThrow(/moves only at its start/)
  expect(() => flatten("M 0 0 Q 1 1")).toThrow(/unknown command/)
  expect(() => flatten("M x 0")).toThrow(/number expected/)
})

test("the pen's speed follows the two-thirds power law: v = K·κ^(−1/3), slower on a tighter curve, up to its top speed", () => {
  // a long arc of radius r, written at the speed its curvature asks for (away from where the pen lands and lifts)
  for (const r of [0.1, 0.2, 0.4]) {
    const points = flatten(`A 0 0 ${r} ${r} 0 1000`)
    const v = speeds(points)
    const mid = v[Math.floor(v.length / 2)]
    expect(mid).toBeCloseTo(Math.min(TOP_SPEED, POWER_K * r ** (1 / 3)), 0)
  }
  const v = speeds(flatten("M 0 0 L 1 0"))
  expect(v[0]).toBeCloseTo(LAND_SPEED, 6) // lands slowly
  expect(v[v.length - 1]).toBeLessThan(TOP_SPEED * 0.5) // and lifts slowly
  expect(Math.max(...v)).toBeLessThanOrEqual(TOP_SPEED + 1e-9)
})

test("a long exposure keeps more light where the pen dwells: ink grows as speed falls, within its range", () => {
  expect(inkAt(1)).toBe(INK_RANGE[1])
  expect(inkAt(1000)).toBe(INK_RANGE[0])
  expect(inkAt(8)).toBeGreaterThan(inkAt(16))
})

test("writing: segments follow one another in time, the text is centred, and it takes a hand's time", () => {
  const w = write([..."12:41"], 100)
  expect(w.segments.length).toBeGreaterThan(30)
  let t = 0
  for (const s of w.segments) {
    expect(s.t0).toBeGreaterThanOrEqual(t - 1e-9)
    expect(s.t1).toBeGreaterThan(s.t0)
    t = s.t1
    expect(s.ink0).toBeGreaterThanOrEqual(INK_RANGE[0])
    expect(s.ink1).toBeLessThanOrEqual(INK_RANGE[1])
  }
  expect(w.duration).toBeCloseTo(t, 9)
  expect(w.duration).toBeGreaterThan(0.8) // a quarter of a second a digit, with the pen lifting between strokes
  expect(w.duration).toBeLessThan(2.2)
  const xs = w.segments.flatMap((s) => [s.x0, s.x1])
  expect(Math.min(...xs) + Math.max(...xs)).toBeCloseTo(0, 6)
  expect(Math.max(...xs) - Math.min(...xs)).toBeCloseTo(w.width, 6)
  // the digits are as tall as asked: the baseline at y = em/2, the top at −em/2
  const ys = w.segments.flatMap((s) => [s.y0, s.y1])
  expect(Math.max(...ys)).toBeCloseTo(50, 0)
  expect(Math.min(...ys)).toBeCloseTo(-50, 0)
})

test("a slower pace takes proportionally longer; the same text at another size takes as long", () => {
  const quick = write(["check"], 100, 1)
  const slow = write(["check"], 100, 0.5)
  expect(slow.duration).toBeCloseTo(quick.duration * 2, 9)
  expect(write([..."21°"], 50).duration).toBeCloseTo(write([..."21°"], 200).duration, 9)
  expect(write([" "], 100).segments).toHaveLength(0)
})
