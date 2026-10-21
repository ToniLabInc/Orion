import { describe, expect, it } from "vitest"
import { MicCapture } from "./capture"

/** Feed `seconds` of channels made by `make(n)` to a capture at `rate`, in 128-sample quanta. */
function run(rate: number, seconds: number, make: (n: number) => number[]) {
  const capture = new MicCapture(rate)
  const blocks: Float32Array[] = []
  const levels: number[] = []
  const sink = { block: (b: Float32Array) => blocks.push(b), level: (v: number) => levels.push(v) }
  const total = Math.round(rate * seconds)
  for (let start = 0; start < total; start += 128) {
    const first = make(start)
    const channels = first.map(() => new Float32Array(128))
    for (let i = 0; i < 128; i++) {
      const values = make(start + i)
      for (let c = 0; c < channels.length; c++) channels[c][i] = values[c]
    }
    capture.push(channels, sink)
  }
  return { blocks, levels, capture }
}

describe("the microphone's capture", () => {
  it("sends whole 40 ms blocks of 16 kHz", () => {
    const { blocks } = run(48_000, 2, (n) => [Math.sin(n / 10)])
    expect(blocks.every((b) => b.length === 640)).toBe(true)
    expect(blocks.length).toBe(Math.floor((2 * 16_000 - 100) / 640)) // all but the kernel's few samples of delay
  })

  it("mixes channels down to mono", () => {
    const tone = (n: number) => 0.4 * Math.sin((2 * Math.PI * 500 * n) / 44_100)
    const same = run(44_100, 1, (n) => [tone(n), tone(n)])
    const opposite = run(44_100, 1, (n) => [tone(n), -tone(n)])
    const peak = (blocks: Float32Array[]) => Math.max(...blocks.slice(2).map((b) => Math.max(...b.map(Math.abs))))
    expect(peak(same.blocks)).toBeCloseTo(0.4, 2)
    expect(peak(opposite.blocks)).toBeLessThan(1e-6)
  })

  it("measures the level about 30 times a second, as the RMS", () => {
    const { levels } = run(48_000, 3, (n) => [0.5 * Math.sin((2 * Math.PI * 1000 * n) / 48_000)])
    expect(levels.length).toBeGreaterThanOrEqual(89)
    expect(levels.length).toBeLessThanOrEqual(90)
    for (const level of levels.slice(1)) expect(level).toBeCloseTo(0.5 / Math.SQRT2, 2)
  })

  it("ignores a quantum without channels (the input not connected yet)", () => {
    const capture = new MicCapture(48_000)
    const sink = { block: () => expect.unreachable(), level: () => expect.unreachable() }
    capture.push([], sink)
  })
})
