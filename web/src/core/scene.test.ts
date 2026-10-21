/// <reference types="node" />
import { readFileSync } from "node:fs"
import { expect, test } from "vitest"
import type { Lin, Palette } from "./colors"
import { AURORA_SEEN, METEOR_FADE_S, METEOR_MAX, RECALL_MAX, REST_DIM, Scene, WIPE_SPEED } from "./scene"
import { decode } from "./sky"
import { arcUnder } from "./world"

/** A palette of distinct plain colours (the real one reads palette.css, which Node doesn't parse here). */
function palette(): Palette {
  const c = (n: number): Lin => [n, n / 2, n / 4]
  const states = ["idle", "listening", "thinking", "speaking", "loading", "error"] as const
  const glow = Object.fromEntries(states.map((s, i) => [s, c(0.1 + i * 0.1)])) as Palette["glow"]
  const upper = Object.fromEntries(states.map((s, i) => [s, c(0.05 + i * 0.1)])) as Palette["upper"]
  const hues = Object.fromEntries(
    ["rigel", "nebula", "aurora", "betel", "alert", "ok"].map((h, i) => [h, c(0.5 + i * 0.05)]),
  ) as Palette["hues"]
  return {
    glow,
    upper,
    hues,
    sky: c(0.01),
    ground: c(0),
    dust: c(0.3),
    nebula: c(0.4),
    figure: c(0.6),
    auroraLow: c(0.7),
    auroraHigh: c(0.8),
  }
}

const file = readFileSync(new URL("./stars.bin", import.meta.url))
const catalogue = decode(file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength) as ArrayBuffer)

/** A scene on a 1600 × 900 screen with the limb at 600 and the talk button at the middle, the sky open. */
function make(sky = true): Scene {
  const s = new Scene(palette(), false)
  s.size(1600, 900)
  s.layout(600, 800, 606)
  if (sky) s.setSky(catalogue)
  return s
}
const run = (s: Scene, seconds: number, fps = 60) => {
  for (let i = 0; i < Math.round(seconds * fps); i++) s.update(1 / fps)
}

test("nothing is laid out before the first layout, and events before it are let go", () => {
  const s = new Scene(palette(), false)
  s.size(1600, 900)
  expect(s.laid).toBe(false)
  s.meteor("rigel", 100, 100)
  s.write("12:41", null, 1000)
  expect(s.meteors).toHaveLength(0)
  expect(s.writing).toBeNull()
  s.layout(600, 800, 606)
  expect(s.laid).toBe(true)
  expect(s.limb.horizon).toBe(600)
  expect(s.limb.r).toBeGreaterThan(1600)
})

test("his state's colour spreads out from the talk button at WIPE_SPEED, and when it has reached the ends it is the only one", () => {
  const s = make()
  const pal = s.palette
  expect(s.colours.a).toBe(pal.glow.idle)
  s.setState("listening")
  expect(s.wipeFront).toBe(0)
  expect(s.colours.a).toBe(pal.glow.idle)
  expect(s.colours.b).toBe(pal.glow.listening)
  run(s, 0.1)
  expect(s.wipeFront).toBeCloseTo(WIPE_SPEED * 0.1, 0)
  run(s, 2)
  expect(s.wipeFront).toBe(Number.POSITIVE_INFINITY)
  expect(s.colours.a).toBe(pal.glow.listening)
  expect(s.colours.upA).toBe(pal.upper.listening)
  // the same state again changes nothing
  s.setState("listening")
  expect(s.wipeFront).toBe(Number.POSITIVE_INFINITY)
})

test("thinking raises the aurora and it is gone, and costs nothing, a few seconds after he stops", () => {
  const s = make()
  expect(s.aurora).toBe(0)
  s.setState("thinking")
  run(s, 3)
  expect(s.aurora).toBeGreaterThan(0.9)
  expect(s.auroraDrift).toBeGreaterThan(30)
  expect(s.active()).toBe(true)
  s.setState("idle")
  run(s, 12)
  expect(s.aurora).toBe(0)
  expect(s.auroraDrift).toBe(0)
  expect(s.aurora).toBeLessThan(AURORA_SEEN)
  run(s, 3) // the string's last ripples are done, nothing else moves
  expect(s.active()).toBe(false)
  expect(s.auroraTop).toBeGreaterThan(130)
  expect(s.auroraTop).toBeLessThan(530)
})

test("rest dims the glow to REST_DIM and draws the figure; waking restores both, and quickly", () => {
  const s = make()
  s.rest(true)
  run(s, 8)
  expect(s.glow).toBeCloseTo(REST_DIM, 1)
  expect(s.figureAlpha).toBeGreaterThan(0.95)
  expect(s.figureClock).toBeGreaterThan(7)
  // the figure's lines start one after another, each taking a visible time
  expect(s.figureTimes).toHaveLength(42)
  for (let k = 1; k < 21; k++) expect(s.figureTimes[k * 2]).toBeGreaterThan(s.figureTimes[(k - 1) * 2])
  for (let k = 0; k < 21; k++) {
    expect(s.figureTimes[k * 2 + 1]).toBeGreaterThanOrEqual(0.7 - 1e-6) // stored as a float32
    expect(s.figureTimes[k * 2 + 1]).toBeLessThanOrEqual(2.6 + 1e-6)
  }
  s.rest(false)
  run(s, 2)
  expect(s.glow).toBeGreaterThan(0.99)
  expect(s.figureAlpha).toBe(0)
})

test("levels that stop coming are silence; his voice and yours light the glow", () => {
  const s = make()
  const bands = new Float32Array(16).fill(0.8)
  s.level(1, 0.9, bands)
  run(s, 0.2)
  expect(s.voice).toBeGreaterThan(0.6)
  expect(s.strand.still).toBe(false)
  run(s, 0.5)
  expect(s.voice).toBeLessThan(0.3) // nothing came: it fades, quickly
  run(s, 3)
  expect(s.voice).toBeLessThan(0.01)
  s.level(0, 0.7, bands)
  run(s, 0.1)
  expect(s.mic).toBeGreaterThan(0.3)
})

test("a meteor leaves the limb near the talk button for the point, takes longer the further it goes, and is gone after its fade", () => {
  const s = make()
  s.meteor("aurora", 1200, 200)
  expect(s.meteors).toHaveLength(1)
  const m = s.meteors[0]
  expect(Math.abs(arcUnder(s.limb, m.ox, m.oy + 1))).toBeLessThan(0.05 * s.width)
  expect([m.tx, m.ty]).toEqual([1200, 200])
  s.meteor("aurora", 810, 580)
  expect(s.meteors[1].dur).toBeLessThan(m.dur)
  const out = new Float32Array(METEOR_MAX * 9)
  expect(s.meteorsForShader(out)).toBe(2)
  expect(out[4]).toBe(0) // age
  run(s, m.dur + METEOR_FADE_S + 0.2)
  expect(s.meteors).toHaveLength(0)
  for (let i = 0; i < 20; i++) s.meteor("rigel", 100 + i, 100)
  expect(s.meteors).toHaveLength(METEOR_MAX)
  // a meteor flares the glow where it leaves, and the flare dies away
  const flares = Array.from(s.bumpsForShader())
  expect(Math.max(flares[2], flares[6], flares[10], flares[14])).toBeGreaterThan(0.5)
})

test("a recall lights up to eight stars in turn, from the sky's middle, clear of each other", () => {
  const s = make()
  s.recall(5)
  run(s, 0.05)
  const out = new Float32Array(RECALL_MAX * 8)
  expect(s.recallsForShader(out)).toBe(1) // the first has started, the next ones follow
  run(s, 0.5)
  expect(s.recallsForShader(out)).toBeGreaterThan(2)
  const lit = s.recallsForShader(out)
  for (let i = 0; i < lit; i++) {
    expect(out[i * 8 + 7]).toBeGreaterThan(0)
    expect(out[i * 8 + 3]).toBeLessThanOrEqual(4.6) // bright enough to be a star he knows
  }
  s.recall(50)
  run(s, 0.1)
  expect(s.recallsForShader(out)).toBeLessThanOrEqual(RECALL_MAX)
  run(s, 11)
  expect(s.recallsForShader(out)).toBe(0)
  expect(make(false).recallsForShader(out)).toBe(0) // no sky yet: nothing to light
})

test("writing: held for its time after the pen is done, then it fades and goes; a release brings that forward", () => {
  const s = make()
  s.write("12:41", null, 3000)
  const w = s.writing
  expect(w).not.toBeNull()
  const writing = w as NonNullable<typeof w>
  expect(writing.fadeAt).toBeCloseTo(s.time + writing.duration + 3, 6)
  expect(s.writeGain()).toBe(1)
  run(s, writing.duration + 3.5)
  expect(s.writeGain()).toBeLessThan(1)
  expect(s.writeGain()).toBeGreaterThan(0)
  run(s, 1)
  expect(s.writing).toBeNull()
  expect(s.writeGain()).toBe(0)

  // a held question stays and breathes; a release of something else leaves it; its own release lets it go
  s.write(null, "question", 60_000)
  run(s, 1)
  const gains = new Set<number>()
  for (let i = 0; i < 90; i++) {
    s.update(1 / 60)
    gains.add(Math.round(s.writeGain() * 100))
  }
  expect(gains.size).toBeGreaterThan(5)
  s.write("12:41", null, 0)
  s.write(null, "check", 0)
  expect(s.writing).not.toBeNull()
  expect((s.writing as NonNullable<typeof s.writing>).fadeAt).toBeGreaterThan(s.time + 30) // its 60 s hold stands
  s.write(null, "question", 0)
  run(s, 2)
  expect(s.writing).toBeNull()
  s.write(null, "check", 5000)
  s.write(null, null, 0) // neither: whatever is written
  run(s, 2)
  expect(s.writing).toBeNull()
})

test("a writing is as large as is pleasing and never wider than the screen; marks are drawn more slowly than words", () => {
  const wide = make()
  wide.write("12:41", null, 1000)
  const narrow = new Scene(palette(), false)
  narrow.size(300, 600)
  narrow.layout(400, 150, 406)
  narrow.write("12:41 pm 2 h", null, 1000)
  const bounds = (s: Scene) => {
    const w = s.writing as NonNullable<typeof s.writing>
    const xs = w.segments.flatMap((g) => [g.x0, g.x1])
    return [w.origin[0] + Math.min(...xs), w.origin[0] + Math.max(...xs)]
  }
  const [l, r] = bounds(wide)
  expect(l).toBeGreaterThan(0)
  expect(r).toBeLessThan(1600)
  const [nl, nr] = bounds(narrow)
  expect(nl).toBeGreaterThanOrEqual(15)
  expect(nr).toBeLessThanOrEqual(285)
  expect(wide.writing?.origin[1]).toBeCloseTo(600 * 0.42, 6)
  wide.write(null, "check", 1000)
  expect((wide.writing as NonNullable<typeof wide.writing>).duration).toBeGreaterThan(0.2)
})

test("for reduced motion the scene settles where it would come to rest: the colour spread, the curtains standing, a writing complete", () => {
  const s = make()
  s.setState("thinking")
  s.rest(true)
  s.write("21°", null, 2000)
  s.meteor("rigel", 900, 200)
  s.settle()
  expect(s.wipeFront).toBe(Number.POSITIVE_INFINITY)
  expect(s.colours.a).toBe(s.palette.glow.thinking)
  expect(s.aurora).toBe(1)
  expect(s.glow).toBeCloseTo(REST_DIM, 6)
  expect(s.figureAlpha).toBe(1)
  expect(s.meteors).toHaveLength(0)
  const w = s.writing as NonNullable<typeof s.writing>
  expect(s.time - w.t0).toBeGreaterThan(w.duration)
  expect(s.writeGain()).toBe(1)
  expect(s.shift).toEqual([0, 0])
  s.endWriting()
  expect(s.writing).toBeNull()
})

test("the sky drifts very slowly and leans a whisper away from the pointer", () => {
  const s = make()
  run(s, 120)
  expect(Math.hypot(s.shift[0], s.shift[1])).toBeLessThan(25)
  expect(Math.abs(s.shift[0])).toBeGreaterThan(0.5)
  s.pointer(1, 1)
  run(s, 6)
  const leaned = [...s.shift]
  s.pointer(-1, -1)
  run(s, 6)
  expect(leaned[0]).toBeLessThan(s.shift[0] + 18 * 2) // it moved the other way (within the drift)
  expect(s.shift[0] - leaned[0]).toBeGreaterThan(5)
})

test("M42 is placed in the sky's plane near the belt's sword, and the stars fade in once the sky has come", () => {
  const s = make(false)
  expect(s.skyOpen).toBe(0)
  s.setSky(catalogue)
  run(s, 2)
  expect(s.skyOpen).toBe(1)
  const [x, y, sx, sy] = s.m42
  expect(Math.abs(x)).toBeLessThan(0.1)
  expect(y).toBeLessThan(0) // below the belt, where the sword hangs
  expect(y).toBeGreaterThan(-0.15)
  expect(sy).toBeGreaterThan(sx)
})

test("the background is still when nothing in it moves, and only then: that is when it is drawn once and kept", () => {
  const s = make()
  run(s, 6)
  expect(s.backgroundStill()).toBe(true) // twinkle and drift are the stars', not the background's
  // each thing that moves it makes it move, and when that is over it is still again
  s.level(1, 0.8, new Float32Array(16).fill(0.5))
  run(s, 0.1)
  expect(s.backgroundStill()).toBe(false) // his voice lights the glow and rings the string
  run(s, 12)
  expect(s.backgroundStill()).toBe(true)
  s.setState("listening")
  expect(s.backgroundStill()).toBe(false) // the colour spreads, and the string is plucked
  run(s, 12)
  expect(s.backgroundStill()).toBe(true)
  s.meteor("rigel", 900, 200)
  expect(s.backgroundStill()).toBe(false) // a flare on the limb
  run(s, 6)
  expect(s.backgroundStill()).toBe(true)
  s.setState("thinking")
  run(s, 2)
  expect(s.backgroundStill()).toBe(false) // the aurora
  s.setState("loading")
  run(s, 20)
  expect(s.backgroundStill()).toBe(false) // the sweep
  s.setState("idle")
  run(s, 20)
  expect(s.backgroundStill()).toBe(true)
})

test("only what moves fast counts as active: stars twinkling and the sky's drift do not", () => {
  const s = make()
  run(s, 10)
  expect(s.active()).toBe(false)
  s.meteor("rigel", 700, 300)
  expect(s.active()).toBe(true)
  run(s, 3)
  expect(s.active()).toBe(false)
  s.setState("loading")
  expect(s.active()).toBe(true)
  run(s, 0.1)
  expect(s.sweep).toBeGreaterThan(-1e3)
  s.setState("idle")
  run(s, 0.1)
  expect(s.sweep).toBeLessThan(-1e3)
})
