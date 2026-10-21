import { expect, test } from "vitest"
import { makeNoise, makeRays, NOISE_SIZE, RAY_SIZE } from "./noise"

const at = (p: Uint8Array, x: number, y: number, c: number) =>
  p[(((y + NOISE_SIZE) % NOISE_SIZE) * NOISE_SIZE + ((x + NOISE_SIZE) % NOISE_SIZE)) * 4 + c]

test("the noise is deterministic for a seed, different for another, and RGBA8", () => {
  const a = makeNoise(5)
  const b = makeNoise(5)
  const c = makeNoise(6)
  expect(a).toHaveLength(NOISE_SIZE * NOISE_SIZE * 4)
  expect(a).toEqual(b)
  expect(a).not.toEqual(c)
})

test("each channel is stretched to its full range, so a threshold means the same everywhere", () => {
  const p = makeNoise()
  for (let c = 0; c < 4; c++) {
    let lo = 255
    let hi = 0
    let sum = 0
    for (let i = 0; i < NOISE_SIZE * NOISE_SIZE; i++) {
      const v = p[i * 4 + c]
      lo = Math.min(lo, v)
      hi = Math.max(hi, v)
      sum += v
    }
    expect(lo, `channel ${c}`).toBe(0)
    expect(hi, `channel ${c}`).toBe(255)
    const mean = sum / (NOISE_SIZE * NOISE_SIZE)
    expect(mean, `channel ${c}`).toBeGreaterThan(90)
    expect(mean, `channel ${c}`).toBeLessThan(165)
  }
})

test("it tiles: the texel past an edge continues the field smoothly (the coarse channels change little per texel)", () => {
  const p = makeNoise()
  for (const c of [0, 1]) {
    let worst = 0
    for (let y = 0; y < NOISE_SIZE; y += 7) {
      worst = Math.max(
        worst,
        Math.abs(at(p, NOISE_SIZE - 1, y, c) - at(p, 0, y, c)),
        Math.abs(at(p, y, NOISE_SIZE - 1, c) - at(p, y, 0, c)),
      )
    }
    expect(worst, `channel ${c}`).toBeLessThan(c === 0 ? 4 : 14)
  }
})

test("the rays' texture: deterministic, one line of four noises at four scales, full range, tiling without a seam", () => {
  const a = makeRays(3)
  expect(a).toHaveLength(RAY_SIZE * 4)
  expect(a).toEqual(makeRays(3))
  expect(a).not.toEqual(makeRays(4))
  // the first channel has the finest structure (1024 cells), the last the broadest (54): the rougher, the more it changes per texel
  const roughness = (c: number) => {
    let sum = 0
    for (let x = 0; x < RAY_SIZE; x++) sum += Math.abs(a[((x + 1) % RAY_SIZE) * 4 + c] - a[x * 4 + c])
    return sum / RAY_SIZE
  }
  expect(roughness(0)).toBeGreaterThan(roughness(1))
  expect(roughness(1)).toBeGreaterThan(roughness(2))
  expect(roughness(2)).toBeGreaterThan(roughness(3))
  for (let c = 0; c < 4; c++) {
    let lo = 255
    let hi = 0
    for (let x = 0; x < RAY_SIZE; x++) {
      lo = Math.min(lo, a[x * 4 + c])
      hi = Math.max(hi, a[x * 4 + c])
    }
    expect([lo, hi], `channel ${c}`).toEqual([0, 255])
    // no seam: the step over the wrap is like any other
    expect(Math.abs(a[c] - a[(RAY_SIZE - 1) * 4 + c]), `channel ${c}`).toBeLessThanOrEqual(
      Math.max(8, roughness(c) * 6),
    )
  }
})

test("the channels are different fields: no two are the same noise", () => {
  const p = makeNoise()
  for (const [a, b] of [
    [0, 1],
    [1, 2],
    [2, 3],
    [0, 3],
  ]) {
    let same = 0
    for (let i = 0; i < NOISE_SIZE * NOISE_SIZE; i += 13) if (Math.abs(p[i * 4 + a] - p[i * 4 + b]) < 8) same++
    expect(same / ((NOISE_SIZE * NOISE_SIZE) / 13)).toBeLessThan(0.25)
  }
})
