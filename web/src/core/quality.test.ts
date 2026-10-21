import { expect, test } from "vitest"
import {
  ACTIVE_FPS,
  DOWN_AFTER_S,
  effectiveMs,
  Governor,
  IDLE_FPS,
  Pacer,
  startTier,
  TIERS,
  TOP,
  UP_AFTER_S,
} from "./quality"

/** How many frames a pacer draws in `seconds` of a display calling back `hz` times a second, wanting `fps`. */
function paced(hz: number, fps: number, seconds = 10): number {
  const pacer = new Pacer()
  let drawn = 0
  for (let i = 0; i < hz * seconds; i++) if (pacer.due(1000 / hz, fps)) drawn++
  return drawn / seconds
}

test("pacing: 24 frames a second while only the stars twinkle and 60 while anything moves, whatever the display's rate", () => {
  expect(paced(60, IDLE_FPS)).toBeCloseTo(24, 0)
  expect(paced(60, ACTIVE_FPS)).toBeCloseTo(60, 0)
  expect(paced(120, ACTIVE_FPS)).toBeCloseTo(60, 0)
  expect(paced(120, IDLE_FPS)).toBeCloseTo(24, 0)
  // 144 Hz can't land on 60 exactly: never fewer than 60, never every callback
  const fast = paced(144, ACTIVE_FPS)
  expect(fast).toBeGreaterThanOrEqual(59.5)
  expect(fast).toBeLessThan(80)
  expect(paced(144, IDLE_FPS)).toBeCloseTo(24, 0)
  // a slow display draws every frame it gets, and a stall doesn't make it race to catch up
  expect(paced(30, ACTIVE_FPS)).toBeCloseTo(30, 0)
  const pacer = new Pacer()
  expect(pacer.due(1000, ACTIVE_FPS)).toBe(true)
  expect(pacer.due(1000 / 60, ACTIVE_FPS)).toBe(true)
  expect(pacer.due(1000 / 60, ACTIVE_FPS)).toBe(true)
})

/** Feed a governor `seconds` of frames `ms` apart; the changes it made. */
function run(g: Governor, ms: number, seconds: number): number[] {
  const changes: number[] = []
  for (let t = 0; t < seconds * 1000; t += ms) {
    const change = g.frame(ms)
    if (change) changes.push(change)
  }
  return changes
}

test("the tiers: more pixels, aurora resolution, sheets and stars as they go up, never fewer", () => {
  expect(TIERS).toHaveLength(4)
  expect(TOP).toBe(3)
  for (let i = 1; i < TIERS.length; i++) {
    expect(TIERS[i].dpr).toBeGreaterThanOrEqual(TIERS[i - 1].dpr)
    expect(TIERS[i].aurora).toBeGreaterThanOrEqual(TIERS[i - 1].aurora)
    expect(TIERS[i].sheets).toBeGreaterThanOrEqual(TIERS[i - 1].sheets)
    expect(TIERS[i].faintest).toBeGreaterThanOrEqual(TIERS[i - 1].faintest)
  }
  // every tier draws the whole naked-eye sky but the lowest, which leaves out the faintest stars; none goes past the catalogue's limit
  expect(TIERS[0].faintest).toBeLessThan(6.5)
  for (const t of TIERS) expect(t.faintest).toBeLessThanOrEqual(6.5)
  // the screen's own density is drawn at on `auto`'s start tier, so lines and stars are crisp on a 2× display
  expect(TIERS[startTier("auto")].dpr).toBeGreaterThanOrEqual(2)
  expect(TIERS[TOP].dpr).toBeGreaterThanOrEqual(3)
})

test("auto: slow frames for 2 s step down once; a shorter stall changes nothing", () => {
  const g = new Governor("auto")
  expect(g.tier).toBe(2)
  expect(run(g, 25, DOWN_AFTER_S * 0.7)).toEqual([])
  expect(run(g, 16.7, 1)).toEqual([])
  expect(g.tier).toBe(2)
  expect(run(g, 25, DOWN_AFTER_S + 0.4)).toEqual([-1])
  expect(g.tier).toBe(1)
  // and on down to the floor, never below it
  expect(run(g, 40, 30)).toEqual([-1])
  expect(g.tier).toBe(0)
})

test("auto: 10 s on time steps up; a step up that doesn't hold makes the next one wait twice as long", () => {
  const g = new Governor("auto")
  expect(run(g, 16.7, UP_AFTER_S - 0.5)).toEqual([])
  expect(run(g, 16.7, 1)).toEqual([1])
  expect(g.tier).toBe(TOP)
  // too much for this machine: back down within seconds
  expect(run(g, 22, DOWN_AFTER_S + 0.5)).toEqual([-1])
  expect(run(g, 16.7, UP_AFTER_S + 1)).toEqual([])
  expect(run(g, 16.7, UP_AFTER_S)).toEqual([1])
})

test("the GPU's own time counts against a tier: what the governor hears is the worse of the interval and four times it", () => {
  expect(effectiveMs(16.7, null)).toBe(16.7)
  expect(effectiveMs(16.7, 1)).toBe(16.7)
  expect(effectiveMs(16.7, 6)).toBeCloseTo(24, 6)
  expect(effectiveMs(30, 2)).toBe(30)
  // a display locked to 60 Hz with a GPU at 7 ms a frame never shows a slow interval; the governor still steps down
  const g = new Governor("auto")
  const slow: number[] = []
  for (let t = 0; t < 5000; t += 16.7) {
    const change = g.frame(effectiveMs(16.7, 7))
    if (change) slow.push(change)
  }
  expect(slow).toContain(-1)
  // and one at 2 ms is left alone on the tier it is on
  const light = new Governor("auto")
  const changes: number[] = []
  for (let t = 0; t < 30_000; t += 16.7) {
    const change = light.frame(effectiveMs(16.7, 2))
    if (change === -1) changes.push(change)
  }
  expect(changes).toEqual([])
})

test("high and low hold their tier whatever the frames; a pinned tier holds too", () => {
  const high = new Governor("high")
  expect(high.tier).toBe(TOP)
  expect(run(high, 40, 10)).toEqual([])
  const low = new Governor("low")
  expect(low.tier).toBe(1)
  expect(run(low, 10, 30)).toEqual([])
  const pinned = new Governor("auto", 0)
  expect(run(pinned, 10, 30)).toEqual([])
  expect(pinned.tier).toBe(0)
  expect(startTier("auto")).toBe(2)
  expect(startTier("off")).toBe(1)
  // a new preference re-aims it
  const g = new Governor("auto")
  g.prefer("low")
  expect(g.tier).toBe(1)
  g.prefer("auto")
  expect(g.tier).toBe(2)
})
