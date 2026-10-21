import { expect, test } from "vitest"
import { altitude, arcUnder, limb, onLimb, onString, RADIUS_WIDTHS, STRING_MARGIN, sag } from "./world"

test("the limb is a circle whose top is at the screen's middle and whose radius is a few widths", () => {
  const l = limb(1920, 640)
  expect(l.r).toBe(RADIUS_WIDTHS * 1920)
  expect(l.cx).toBe(960)
  expect(l.cy).toBe(640 + l.r)
  expect(altitude(l, 960, 640)).toBeCloseTo(0, 9)
  expect(altitude(l, 960, 540)).toBeCloseTo(100, 6)
  expect(altitude(l, 960, 740)).toBeCloseTo(-100, 4)
  // it falls away to the sides by the circle's sagitta, a gentle arc
  const drop = sag(l, 960)
  expect(drop).toBeGreaterThan(60)
  expect(drop).toBeLessThan(90)
  expect(altitude(l, 0, 640 + drop)).toBeCloseTo(0, 6)
  expect(altitude(l, 1920, 640 + drop)).toBeCloseTo(0, 6)
  expect(sag(limb(100, 0), 0)).toBe(0)
  expect(sag({ ...l, r: 100 }, 60)).toBeCloseTo(20, 9)
})

test("altitude is the distance to the circle, exact where the numbers are huge", () => {
  for (const [w, h] of [
    [390, 480],
    [1920, 640],
    [7680, 2000],
  ]) {
    const l = limb(w, h)
    for (const [dx, dy] of [
      [0, -300],
      [w * 0.3, -40],
      [-w * 0.45, 12],
      [w * 0.1, 400],
    ]) {
      const x = l.cx + dx
      const y = l.horizon + dy
      const exact = Math.hypot(x - l.cx, y - l.cy) - l.r
      expect(altitude(l, x, y)).toBeCloseTo(exact, 6)
    }
  }
  // in single precision (a shader's) the difference of two large numbers loses the answer; the exact form keeps it
  const l = limb(7680, 2000)
  const x = l.cx + 0.1
  const y = l.horizon - 0.01
  const naive = Math.fround(Math.hypot(Math.fround(x - l.cx), Math.fround(y - l.cy))) - Math.fround(l.r)
  expect(Math.abs(naive - 0.01)).toBeGreaterThan(Math.abs(altitude(l, x, y) - 0.01))
})

test("a point on the limb: its arc length and its normal, and a step along the normal is that high", () => {
  const l = limb(1920, 640)
  for (const s of [-900, -300, 0, 450, 1000]) {
    const p = onLimb(l, s)
    expect(Math.hypot(p.nx, p.ny)).toBeCloseTo(1, 12)
    expect(altitude(l, p.x, p.y)).toBeCloseTo(0, 6)
    expect(arcUnder(l, p.x, p.y)).toBeCloseTo(s, 6)
    expect(altitude(l, p.x + p.nx * 25, p.y + p.ny * 25)).toBeCloseTo(25, 6)
    expect(arcUnder(l, p.x + p.nx * 25, p.y + p.ny * 25)).toBeCloseTo(s, 6) // up the radius: the same place along the limb
  }
})

test("the string runs past each side of the screen and maps arc length to 0..1", () => {
  const l = limb(1920, 640)
  expect(l.length).toBeGreaterThan(1920 + 2 * STRING_MARGIN)
  expect(l.start).toBeCloseTo(-l.length / 2, 9)
  expect(onString(l, l.start)).toBe(0)
  expect(onString(l, 0)).toBeCloseTo(0.5, 9)
  expect(onString(l, l.start + l.length)).toBe(1)
  expect(onString(l, 1e6)).toBe(1)
  expect(onString(l, -1e6)).toBe(0)
})
