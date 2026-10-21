import { describe, expect, it } from "vitest"
import { sincKernel } from "./kernel"
import { StreamResampler } from "./resampler"

/** A sine at `rate`, `seconds` long. */
function sine(rate: number, freq: number, seconds: number, amp = 0.5, phase = 0.3): Float32Array {
  const out = new Float32Array(Math.round(rate * seconds))
  for (let n = 0; n < out.length; n++) out[n] = amp * Math.sin((2 * Math.PI * freq * n) / rate + phase)
  return out
}

/** Run `input` through a fresh resampler in pieces of `piece` samples. */
function resample(inRate: number, outRate: number, input: Float32Array, piece = 128): Float32Array {
  const r = new StreamResampler(inRate, outRate)
  const out = new Float32Array(r.maxOutput(input.length) + 64)
  let n = 0
  for (let i = 0; i < input.length; i += piece) n += r.process(input.subarray(i, i + piece), out, n)
  return out.subarray(0, n)
}

/** Signal-to-error ratio (dB) of `out` against the same sine sampled at the output's rate, past the edges. */
function snr(out: Float32Array, outRate: number, freq: number, amp = 0.5, phase = 0.3, skip = 400): number {
  let signal = 0
  let error = 0
  for (let k = skip; k < out.length - skip; k++) {
    const want = amp * Math.sin((2 * Math.PI * freq * k) / outRate + phase)
    signal += want * want
    error += (out[k] - want) ** 2
  }
  return 10 * Math.log10(signal / error)
}

/** RMS past the edges. */
function rms(x: Float32Array, skip = 400): number {
  let sum = 0
  for (let k = skip; k < x.length - skip; k++) sum += x[k] * x[k]
  return Math.sqrt(sum / (x.length - 2 * skip))
}

/** The amplitude of `freq` in `x` (a single-bin DFT), past the edges. */
function amplitudeAt(x: Float32Array, rate: number, freq: number, skip = 400): number {
  let re = 0
  let im = 0
  const n = x.length - 2 * skip
  for (let k = skip; k < x.length - skip; k++) {
    re += x[k] * Math.cos((2 * Math.PI * freq * k) / rate)
    im += x[k] * Math.sin((2 * Math.PI * freq * k) / rate)
  }
  return (2 * Math.hypot(re, im)) / n
}

describe("the kernel", () => {
  it("passes DC unchanged at every fractional position", () => {
    const k = sincKernel(44_100, 16_000)
    for (let p = 0; p < k.phases; p++) {
      let sum = 0
      for (let j = 0; j < k.taps; j++) sum += k.table[p * k.taps + j]
      expect(sum).toBeCloseTo(1, 5)
    }
  })

  it("is longer where the band is narrower (downsampling by 3 needs about 3× the taps)", () => {
    expect(sincKernel(48_000, 16_000).taps).toBeGreaterThan(2.5 * sincKernel(24_000, 48_000).taps)
  })
})

describe("the microphone's resampling to 16 kHz", () => {
  it.each([
    [48_000, 1000],
    [48_000, 6000],
    [44_100, 3000],
    [96_000, 440],
    [16_000, 2000],
  ])("keeps a sine (%i Hz in, %i Hz tone) clean and in time", (inRate, freq) => {
    const out = resample(inRate, 16_000, sine(inRate, freq, 1))
    expect(out.length).toBeGreaterThan(16_000 - 200)
    expect(snr(out, 16_000, freq)).toBeGreaterThan(60)
    expect(rms(out)).toBeCloseTo(0.5 / Math.SQRT2, 3)
  })

  it.each([
    [48_000, 9000],
    [48_000, 12_000],
    [48_000, 20_000],
    [44_100, 10_000],
  ])("filters out what would alias (%i Hz in, a %i Hz tone)", (inRate, freq) => {
    const out = resample(inRate, 16_000, sine(inRate, freq, 1))
    expect(20 * Math.log10(rms(out) / (0.5 / Math.SQRT2))).toBeLessThan(-60)
  })

  it("is nothing like naive decimation: a 9 kHz tone doesn't come back as 7 kHz", () => {
    const input = sine(48_000, 9000, 1)
    const naive = new Float32Array(16_000)
    for (let k = 0; k < naive.length; k++) naive[k] = input[3 * k]
    expect(amplitudeAt(naive, 16_000, 7000)).toBeGreaterThan(0.4)
    expect(amplitudeAt(resample(48_000, 16_000, input), 16_000, 7000)).toBeLessThan(0.0005)
  })

  it("gives the same output whatever pieces the input comes in", () => {
    const input = sine(44_100, 1234, 0.5)
    const whole = resample(44_100, 16_000, input, input.length)
    for (const piece of [1, 7, 128, 1000]) {
      const pieces = resample(44_100, 16_000, input, piece)
      expect(pieces.length).toBe(whole.length)
      let worst = 0
      for (let k = 0; k < whole.length; k++) worst = Math.max(worst, Math.abs(pieces[k] - whole[k]))
      expect(worst).toBeLessThan(1e-6)
    }
  })

  it("passes a constant through unchanged", () => {
    const out = resample(48_000, 16_000, new Float32Array(48_000).fill(0.3))
    for (let k = 200; k < out.length - 10; k++) expect(out[k]).toBeCloseTo(0.3, 5)
  })
})

describe("his voice's resampling to the context's rate", () => {
  it.each([
    [48_000, 5000],
    [48_000, 9500],
    [44_100, 3000],
    [96_000, 1000],
  ])("keeps a sine clean and in time (24 kHz → %i Hz, a %i Hz tone)", (outRate, freq) => {
    const out = resample(24_000, outRate, sine(24_000, freq, 1))
    expect(snr(out, outRate, freq)).toBeGreaterThan(60)
  })

  it("leaves no image above his band (a 10 kHz tone makes no 14 kHz one at 48 kHz)", () => {
    const out = resample(24_000, 48_000, sine(24_000, 10_000, 1))
    expect(amplitudeAt(out, 48_000, 10_000)).toBeGreaterThan(0.49)
    expect(amplitudeAt(out, 48_000, 14_000)).toBeLessThan(0.0005) // under −60 dB
  })
})
