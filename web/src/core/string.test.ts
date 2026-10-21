import { expect, test } from "vitest"
import { CROSS_S, MODES, NODES, ONSET_PX, SOFT_LIMIT_PX, SOURCE_PX, STEP, Strand, VOICE_FALL, VOICE_PX } from "./string"

const h = 1 / (NODES - 1)
/** A band vector with one band at a level. */
const band = (k: number, level = 1) => Array.from({ length: 16 }, (_, i) => (i === k ? level : 0))
/** How much of a mode's shape sin(nπξ) is in the string now (its projection, in CSS px). */
const projection = (s: Strand, n: number) => {
  let sum = 0
  for (let j = 0; j < NODES; j++) sum += s.u[j] * Math.sin(n * Math.PI * j * h)
  return (2 * sum) / NODES
}
/** Advance by whole frames, so two frame rates end at the very same moment (a frame's time is the loop's, not a float's). */
const run = (s: Strand, seconds: number, fps = 60, each?: (t: number) => void) => {
  const frames = Math.round(seconds * fps)
  for (let i = 0; i < frames; i++) {
    s.advance(1 / fps)
    each?.(i / fps)
  }
}

test("a string starts still, with fixed ends, and stays still without a drive", () => {
  const s = new Strand()
  expect(s.still).toBe(true)
  run(s, 1)
  expect(s.peak()).toBe(0)
  s.voice(band(3))
  expect(s.still).toBe(false)
  let peak = 0
  run(s, 2, 60, () => {
    peak = Math.max(peak, s.peak())
    expect(s.u[0]).toBe(0)
    expect(s.u[NODES - 1]).toBe(0)
  })
  expect(peak).toBeGreaterThan(0.5)
})

test("a band at full level drives its own standing wave: that mode's shape, at about VOICE_PX·(2/n)^VOICE_FALL", () => {
  for (const k of [0, 3, 9]) {
    const s = new Strand()
    const n = MODES[k]
    s.voice(band(k))
    let peak = 0
    let other = 0
    run(s, 8, 60, (t) => {
      if (t < 6) return
      peak = Math.max(peak, Math.abs(projection(s, n)))
      for (const m of [n - 1, n + 1]) other = Math.max(other, Math.abs(projection(s, m)))
    })
    const expected = VOICE_PX * (MODES[0] / n) ** VOICE_FALL
    expect(peak, `mode ${n}`).toBeGreaterThan(expected * 0.7)
    expect(peak, `mode ${n}`).toBeLessThan(expected * 1.3)
    expect(other, `mode ${n}'s neighbours`).toBeLessThan(peak * 0.15)
  }
})

test("it is stable under the loudest voice, and the same at any frame rate", () => {
  const loud = new Strand()
  loud.voice(new Array(16).fill(1))
  loud.listen(1)
  run(loud, 30)
  expect(Number.isFinite(loud.peak())).toBe(true)
  expect(loud.peak()).toBeLessThan(80)
  const a = new Strand()
  const b = new Strand()
  const bands = Array.from({ length: 16 }, (_, i) => 0.2 + 0.05 * i)
  a.voice(bands)
  b.voice(bands)
  // the same energy, whatever the frame rate: the string's own step is fixed, only the drive is sampled by frames
  const rms = (s: Strand, fps: number) => {
    let sum = 0
    let n = 0
    run(s, 4, fps, (t) => {
      if (t < 2.5) return
      for (let j = 0; j < NODES; j += 4) {
        sum += s.u[j] * s.u[j]
        n++
      }
    })
    return Math.sqrt(sum / n)
  }
  const [x, y] = [rms(a, 60), rms(b, 144)]
  expect(x).toBeGreaterThan(1)
  expect(Math.abs(x - y)).toBeLessThan(x * 0.06)
})

test("it rings down when the voice stops, and comes to rest exactly (nothing more to draw)", () => {
  const s = new Strand()
  s.voice(band(2))
  run(s, 4)
  const before = s.peak()
  expect(before).toBeGreaterThan(2)
  s.voice([])
  run(s, 1.5)
  expect(s.peak()).toBeLessThan(before * 0.2)
  run(s, 6)
  expect(s.still).toBe(true)
  expect(s.peak()).toBe(0)
})

test("a pluck sends ripples both ways at the wave speed: the far side sees them about CROSS_S·distance later", () => {
  const s = new Strand()
  s.pluck(0.3, 4)
  const at = Math.round(0.65 * (NODES - 1))
  let first = -1
  let peak = 0
  run(s, 1.4, 240, (t) => {
    const v = Math.abs(s.u[at])
    if (v > peak) peak = v
    if (first < 0 && v > 0.12) first = t
  })
  expect(peak).toBeGreaterThan(0.3)
  const expected = (0.65 - 0.3) * CROSS_S
  expect(first).toBeGreaterThan(expected * 0.5)
  expect(first).toBeLessThan(expected * 1.4)
  // and the other way, equally far
  const t = new Strand()
  t.pluck(0.3, 4)
  const back = Math.round(-0.05 * (NODES - 1) + 0.3 * (NODES - 1)) // nodes 0.25 string lengths to the left
  let leftPeak = 0
  run(t, 1.4, 240, () => {
    leftPeak = Math.max(leftPeak, Math.abs(t.u[back]))
  })
  expect(leftPeak).toBeGreaterThan(0.3)
})

test("your voice at the talk button makes ripples that run outward on both sides alike", () => {
  const s = new Strand()
  s.talk = 0.5
  const left = Math.round(0.25 * (NODES - 1))
  const right = Math.round(0.75 * (NODES - 1))
  let l = 0
  let r = 0
  let diff = 0
  run(s, 3, 60, (t) => {
    s.listen(t < 0.1 ? 0.2 : 0.6 + 0.3 * Math.sin(t * 25))
    if (t > 1) {
      l = Math.max(l, Math.abs(s.u[left]))
      r = Math.max(r, Math.abs(s.u[right]))
      diff = Math.max(diff, Math.abs(s.u[left] - s.u[right]))
    }
  })
  expect(l).toBeGreaterThan(SOURCE_PX * 0.2)
  expect(l).toBeLessThan(ONSET_PX * 3)
  expect(Math.abs(l - r)).toBeLessThan(Math.max(l, r) * 0.05 + 1e-3)
  expect(diff).toBeLessThan(Math.max(l, r) * 0.05 + 1e-3) // mirror images at every moment
})

test("what the shader reads: the displacement, its slope along the limb, and a smear that grows with speed", () => {
  const s = new Strand()
  s.voice(band(1))
  run(s, 3)
  const out = new Float32Array(NODES * 4)
  s.write(out, 2000)
  const ds = 2000 / (NODES - 1)
  const shown = (x: number) => SOFT_LIMIT_PX * Math.tanh(x / SOFT_LIMIT_PX)
  for (const j of [10, 100, 256, 400]) {
    expect(out[j * 4]).toBeCloseTo(shown(s.u[j]), 5)
    expect(out[j * 4 + 1]).toBeCloseTo((shown(s.u[j + 1]) - shown(s.u[j - 1])) / (2 * ds), 5)
    expect(out[j * 4 + 2]).toBeGreaterThanOrEqual(0)
  }
  // a soft limit: small displacements pass as they are, huge ones can't go past it
  expect(shown(0.5)).toBeCloseTo(0.5, 2)
  expect(shown(1e6)).toBeCloseTo(SOFT_LIMIT_PX, 6)
  // still: no motion, so no smear
  const quiet = new Strand()
  quiet.write(out, 2000)
  expect(Math.max(...out)).toBe(0)
  expect(STEP).toBeLessThan(0.002)
})
