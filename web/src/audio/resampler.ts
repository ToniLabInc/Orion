// A streaming resampler at a fixed ratio: input arrives in pieces of any length (an AudioWorklet's 128-sample quanta),
// output comes out as soon as the kernel has the samples it needs, the same whatever the pieces were. Its delay is the
// kernel's half width (under 2 ms at 48 → 16 kHz).
import { interpolate, type KernelOptions, type SincKernel, sincKernel } from "./kernel"

export class StreamResampler {
  readonly kernel: SincKernel
  /** Input samples per output sample. */
  readonly step: number
  private buf: Float32Array
  private len = 0
  private pos = 0

  /**
   * @param inRate - the input's sample rate (Hz)
   * @param outRate - the output's sample rate (Hz)
   * @param options - the kernel's shape
   */
  constructor(inRate: number, outRate: number, options?: KernelOptions) {
    this.kernel = sincKernel(inRate, outRate, options)
    this.step = inRate / outRate
    this.buf = new Float32Array(2 * this.kernel.taps + 1024)
    this.reset()
  }

  /** Forget the past: the next input starts from silence. */
  reset(): void {
    this.buf.fill(0)
    // the first output sits on the first input sample, with silence before it for the kernel's left side
    this.len = this.kernel.half - 1
    this.pos = this.len
  }

  /**
   * The most output samples `n` more input samples can give (to size `out`).
   * @param n - input samples
   * @returns the bound
   */
  maxOutput(n: number): number {
    return Math.ceil(n / this.step) + 2
  }

  /**
   * Take `input` and write the output it completes into `out` from `at`.
   * @param input - the next input samples
   * @param out - where the output goes (room for `maxOutput(input.length)` from `at`)
   * @param at - the first index of `out` to write
   * @returns how many output samples were written
   */
  process(input: Float32Array, out: Float32Array, at = 0): number {
    if (this.len + input.length > this.buf.length) {
      const bigger = new Float32Array(2 * (this.len + input.length))
      bigger.set(this.buf.subarray(0, this.len))
      this.buf = bigger
    }
    this.buf.set(input, this.len)
    this.len += input.length
    const { half } = this.kernel
    const { buf, step } = this
    let pos = this.pos
    let n = at
    for (let i = Math.floor(pos); i + half < this.len; i = Math.floor(pos)) {
      out[n++] = interpolate(this.kernel, buf, i - half + 1, pos - i)
      pos += step
    }
    const drop = Math.floor(pos) - half + 1 // what no later output reads
    if (drop > 0) {
      buf.copyWithin(0, drop, this.len)
      this.len -= drop
      pos -= drop
    }
    this.pos = pos
    return n - at
  }
}
