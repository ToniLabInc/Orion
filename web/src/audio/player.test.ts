import { describe, expect, it } from "vitest"
import { PLAYER_TIMING } from "./constants"
import { JitterPlayer } from "./player"

const OUT = 48_000
const QUANTUM = 128
const BLOCK = 960 // 40 ms at 24 kHz, as the server sends
const FREQ = 440
const AMP = 0.5
/** The most a 440 Hz sine of amplitude 0.5 moves between two samples at 48 kHz (a click jumps far more). */
const SMOOTH = AMP * 2 * Math.PI * (FREQ / OUT)

/** A stand-in server: blocks of one long sine, continuous across blocks. */
function voice() {
  let n = 0
  return () => {
    const pcm = new Int16Array(BLOCK)
    for (let i = 0; i < BLOCK; i++, n++) pcm[i] = Math.round(32767 * AMP * Math.sin((2 * Math.PI * FREQ * n) / 24_000))
    return pcm
  }
}

/** Render `ms` of output in quanta, calling `each(quantum index)` before each one; returns everything rendered. */
function render(player: JitterPlayer, ms: number, each: (q: number) => void = () => {}) {
  const quanta = Math.round((ms * OUT) / 1000 / QUANTUM)
  const out = new Float32Array(quanta * QUANTUM)
  for (let q = 0; q < quanta; q++) {
    each(q)
    player.render(out.subarray(q * QUANTUM, (q + 1) * QUANTUM))
  }
  return out
}

/** The biggest jump between two neighbouring samples. */
function biggestStep(x: Float32Array): number {
  let worst = 0
  for (let k = 1; k < x.length; k++) worst = Math.max(worst, Math.abs(x[k] - x[k - 1]))
  return worst
}

const PER_BLOCK = (40 * OUT) / 1000 / QUANTUM // quanta per 40 ms block (15)

/** The server's pace: four blocks at once, then one every 40 ms. */
function paced(player: JitterPlayer, next: () => Int16Array, blocks: number) {
  let sent = 0
  return (q: number) => {
    if (q === 0)
      while (sent < Math.min(4, blocks)) {
        player.push(next())
        sent++
      }
    else if (q % PER_BLOCK === 0 && sent < blocks) {
      player.push(next())
      sent++
    }
  }
}

describe("the player", () => {
  it("waits for the jitter target before it starts", () => {
    const player = new JitterPlayer(OUT)
    const next = voice()
    player.push(next())
    player.push(next())
    expect(biggestStep(render(player, 20))).toBe(0) // 80 ms held, more expected: silence
    player.push(next())
    const out = render(player, 20)
    expect(Math.max(...out.map(Math.abs))).toBeGreaterThan(0.4)
    expect(player.stats().playing).toBe(true)
  })

  it("plays a stream at the server's pace without a gap or a click, about 120–160 ms behind", () => {
    const player = new JitterPlayer(OUT)
    const next = voice()
    const held: number[] = []
    const out = render(player, 5000, (q) => {
      paced(player, next, 1000)(q)
      if (q % 50 === 25) held.push(player.stats().heldMs)
    })
    expect(biggestStep(out)).toBeLessThan(SMOOTH * 1.05)
    const stats = player.stats()
    expect(stats.drains).toBe(0)
    expect(stats.skips).toBe(0)
    expect(Math.min(...held)).toBeGreaterThanOrEqual(80)
    expect(Math.max(...held)).toBeLessThanOrEqual(170)
    // after its fade-in, the output is the sine itself (resampled)
    let peak = 0
    for (let k = 4800; k < out.length; k++) peak = Math.max(peak, Math.abs(out[k]))
    expect(peak).toBeCloseTo(AMP, 2)
  })

  it("fades out when the network is late, and back in when it catches up, without a click", () => {
    const player = new JitterPlayer(OUT)
    const next = voice()
    let sent = 0
    const out = render(player, 2000, (q) => {
      // the server's pace, but nothing between 400 and 900 ms; then what was late comes at once
      const ms = (q * QUANTUM * 1000) / OUT
      const due = q === 0 ? 4 : 4 + Math.floor(ms / 40)
      if (ms >= 400 && ms < 900) return
      while (sent < due) {
        player.push(next())
        sent++
      }
    })
    expect(biggestStep(out)).toBeLessThan(SMOOTH * 1.3)
    expect(player.stats().drains).toBe(1)
    let silent = 0
    for (let k = 0; k < out.length; k++) if (k > 4800 && out[k] === 0) silent++
    expect(silent).toBeGreaterThan(0) // it went quiet…
    expect(Math.max(...out.subarray(out.length - 4800).map(Math.abs))).toBeGreaterThan(0.45) // …and came back
  })

  it("drops what it holds on a flush, fading out over 5 ms", () => {
    const player = new JitterPlayer(OUT)
    const next = voice()
    render(player, 500, paced(player, next, 30))
    player.flush()
    const out = render(player, 100)
    expect(biggestStep(out)).toBeLessThan(SMOOTH * 1.3)
    const fade = Math.round(PLAYER_TIMING.fadeMs * 48)
    for (let k = fade + 1; k < out.length; k++) expect(out[k]).toBe(0)
    const stats = player.stats()
    expect(stats.heldMs).toBe(0)
    expect(stats.flushes).toBe(1)
  })

  it("crossfades into the next generation's first sound", () => {
    const player = new JitterPlayer(OUT)
    const old = voice()
    render(player, 300, paced(player, old, 30))
    player.flush()
    const next = voice()
    const out = render(player, 400, paced(player, next, 10)) // its first blocks come at once, mid-fade
    expect(biggestStep(out)).toBeLessThan(SMOOTH * 2.2)
    expect(Math.max(...out.subarray(4800).map(Math.abs))).toBeGreaterThan(0.45)
  })

  it("plays a sound shorter than the target once nothing more comes", () => {
    const player = new JitterPlayer(OUT)
    const next = voice()
    player.push(next())
    player.push(next()) // 80 ms: all there is
    const out = render(player, 400)
    let loud = 0
    for (const v of out) if (Math.abs(v) > 0.05) loud++
    expect(loud / OUT).toBeGreaterThan(0.06) // most of its 80 ms
    expect(biggestStep(out)).toBeLessThan(SMOOTH * 1.05)
    expect(player.stats().drains).toBe(1) // its end
  })

  it("catches up gently after a burst: a little faster, then back to real time", () => {
    const player = new JitterPlayer(OUT)
    const next = voice()
    for (let i = 0; i < 15; i++) player.push(next()) // 600 ms at once
    let fastest = 1
    const out = render(player, 15_000, (q) => {
      if (q > 0 && q % PER_BLOCK === 0) player.push(next())
      fastest = Math.max(fastest, player.stats().speed)
    })
    expect(fastest).toBeGreaterThan(1.005)
    expect(fastest).toBeLessThanOrEqual(1 + PLAYER_TIMING.maxSpeedUp)
    const stats = player.stats()
    expect(stats.skips).toBe(0)
    expect(stats.heldMs).toBeLessThan(PLAYER_TIMING.catchUpAboveMs + 50)
    expect(biggestStep(out)).toBeLessThan(SMOOTH * 1.1)
  })

  it("skips ahead to the target when it holds far too much, crossfading", () => {
    const player = new JitterPlayer(OUT)
    const next = voice()
    render(player, 200, paced(player, next, 5))
    for (let i = 0; i < 30; i++) player.push(next()) // 1.2 s more at once
    const out = render(player, 300)
    const stats = player.stats()
    expect(stats.skips).toBe(1)
    expect(stats.heldMs).toBeLessThan(PLAYER_TIMING.targetMs + 40)
    expect(biggestStep(out)).toBeLessThan(SMOOTH * 2.2)
  })

  it("survives a frame longer than it can hold", () => {
    const player = new JitterPlayer(OUT)
    player.push(new Int16Array(24_000 * 5).fill(1000))
    render(player, 100)
    expect(player.stats().heldMs).toBeLessThanOrEqual(PLAYER_TIMING.skipAboveMs)
  })

  it("plays at 44.1 kHz just as well", () => {
    const player = new JitterPlayer(44_100)
    const next = voice()
    const out = new Float32Array(44_100)
    for (let i = 0; i < 6; i++) player.push(next())
    for (let q = 0; q < out.length / 147; q++) player.render(out.subarray(q * 147, (q + 1) * 147))
    let peak = 0
    for (let k = 2000; k < 9000; k++) peak = Math.max(peak, Math.abs(out[k]))
    expect(peak).toBeCloseTo(AMP, 2)
    expect(biggestStep(out)).toBeLessThan(AMP * 2 * Math.PI * (FREQ / 44_100) * 1.05)
  })
})
