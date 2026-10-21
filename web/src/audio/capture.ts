// The microphone's side of the capture worklet, as plain logic: each render quantum (one Float32Array per channel at
// the context's rate) is mixed down to mono, low-passed and resampled to 16 kHz, cut into 40 ms blocks for the server,
// and measured (RMS) about 30 times a second for the level.
import { MIC_BLOCK_MS, MIC_LEVEL_HZ, MIC_RATE } from "./constants"
import { StreamResampler } from "./resampler"

export interface CaptureSink {
  /** A whole block of 16 kHz mono samples (a fresh array: it may be transferred). */
  block(samples: Float32Array): void
  /** The RMS of the last ~33 ms. */
  level(rms: number): void
}

export class MicCapture {
  /** Samples (16 kHz) per block sent. */
  readonly blockSamples: number
  /** Samples (16 kHz) per level measured. */
  readonly levelSamples: number
  private readonly resampler: StreamResampler
  private mono = new Float32Array(128)
  private pending: Float32Array
  private fill = 0
  private squares = 0
  private counted = 0

  /** @param inRate - the context's sample rate (Hz) */
  constructor(inRate: number) {
    this.resampler = new StreamResampler(inRate, MIC_RATE)
    this.blockSamples = Math.round((MIC_RATE * MIC_BLOCK_MS) / 1000)
    this.levelSamples = Math.round(MIC_RATE / MIC_LEVEL_HZ)
    this.pending = new Float32Array(this.blockSamples + this.resampler.maxOutput(1024))
  }

  /**
   * One quantum of the microphone.
   * @param channels - its channels (all the same length; none while the input isn't connected)
   * @param sink - where blocks and levels go
   */
  push(channels: readonly Float32Array[], sink: CaptureSink): void {
    if (!channels.length || !channels[0].length) return
    const n = channels[0].length
    let mono = channels[0]
    if (channels.length > 1) {
      if (this.mono.length < n) this.mono = new Float32Array(n)
      mono = this.mono.subarray(0, n)
      mono.fill(0)
      for (const channel of channels) for (let i = 0; i < n; i++) mono[i] += channel[i]
      const scale = 1 / channels.length
      for (let i = 0; i < n; i++) mono[i] *= scale
    }
    const room = this.fill + this.resampler.maxOutput(n)
    if (room > this.pending.length) {
      const bigger = new Float32Array(room + this.blockSamples)
      bigger.set(this.pending.subarray(0, this.fill))
      this.pending = bigger
    }
    const got = this.resampler.process(mono, this.pending, this.fill)
    for (let i = this.fill; i < this.fill + got; i++) {
      this.squares += this.pending[i] * this.pending[i]
      if (++this.counted === this.levelSamples) {
        sink.level(Math.sqrt(this.squares / this.counted))
        this.squares = 0
        this.counted = 0
      }
    }
    this.fill += got
    while (this.fill >= this.blockSamples) {
      sink.block(this.pending.slice(0, this.blockSamples))
      this.pending.copyWithin(0, this.blockSamples, this.fill)
      this.fill -= this.blockSamples
    }
  }
}
