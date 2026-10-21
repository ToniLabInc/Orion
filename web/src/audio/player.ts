// His voice's side of the player worklet, as plain logic: a jitter buffer over a ring of 24 kHz samples, read through
// the sinc kernel at the context's rate.
//
// - A sound starts once `targetMs` is held (the server sends its first 160 ms at once), or, shorter than that, once
//   nothing more came for `startAfterMs`; it fades in.
// - Running dry (the end of a sound, or the network late), it fades out over what is left, and back in if more comes.
// - Holding too much (a burst after a stall), it plays up to `maxSpeedUp` faster until it is back near the target,
//   and past `skipAboveMs` it jumps ahead to the target.
// - `flush` (a new generation, or the server's `speaker` event) drops everything held.
// Every jump fades the old position out while the new one fades in (a short crossfade): no clicks.
import { PLAYER_TIMING, type PlayerTiming, VOICE_RATE } from "./constants"
import { interpolate, type SincKernel, sincKernel } from "./kernel"

export interface PlayerStats {
  /** Audio held, not played yet. */
  heldMs: number
  /** The most it held since the last stats. */
  maxHeldMs: number
  playing: boolean
  /** The playback speed (1 = real time; above while catching up). */
  speed: number
  /** Samples (24 kHz) received. */
  received: number
  /** Times it ran dry while sounding (ends of sounds included). */
  drains: number
  /** Times it skipped ahead (it held too much). */
  skips: number
  /** Times it dropped what it held (a new generation). */
  flushes: number
  /** RMS of what it played since the last stats. */
  rms: number
}

const CAPACITY = 1 << 16 // 2.7 s at 24 kHz
const MASK = CAPACITY - 1
const REBASE = 1 << 29 // positions come back below this (a multiple of the capacity) to stay exact integers

export class JitterPlayer {
  private readonly kernel: SincKernel
  private readonly baseStep: number
  private readonly inRate: number
  /** The ring, then a copy of its first `taps` samples, so the kernel always reads one contiguous run. */
  private readonly ring: Float32Array
  /** Samples kept at zero past the last one written (what the kernel and a fade-out read beyond the end). */
  private readonly ahead: number
  private readonly fade: number // output samples
  private readonly drainAt: number // input samples
  private readonly target: number // input samples
  private readonly startAfter: number // output samples
  private readonly skipAbove: number // input samples
  private readonly catchUpAbove: number // input samples
  private readonly catchUpSpan: number // input samples
  private readonly maxSpeedUp: number
  private readonly tail: Float32Array
  private tailLen = 0
  private tailPos = 0
  private written = 0 // samples written, from the start (rebased)
  private read = 0 // the next output's position, in input samples (rebased with `written`)
  private playing = false
  private sounding = false // the gain reached 1 since the sound started
  private gain = 0
  private gainStep = 0
  private speed = 1
  private clock = 0 // output samples rendered
  private lastPush = 0
  private received = 0
  private drains = 0
  private skips = 0
  private flushes = 0
  private maxHeld = 0
  private squares = 0
  private rendered = 0

  /**
   * @param outRate - the context's sample rate (Hz)
   * @param timing - the jitter buffer's timing
   * @param inRate - his voice's sample rate (Hz)
   */
  constructor(outRate: number, timing: PlayerTiming = PLAYER_TIMING, inRate = VOICE_RATE) {
    this.kernel = sincKernel(inRate, outRate)
    this.inRate = inRate
    this.baseStep = inRate / outRate
    const ms = (value: number, rate: number) => Math.round((value * rate) / 1000)
    this.fade = Math.max(1, ms(timing.fadeMs, outRate))
    this.drainAt = this.fade * this.baseStep
    this.target = ms(timing.targetMs, inRate)
    this.startAfter = ms(timing.startAfterMs, outRate)
    this.skipAbove = Math.min(ms(timing.skipAboveMs, inRate), CAPACITY / 2)
    this.catchUpAbove = ms(timing.catchUpAboveMs, inRate)
    this.catchUpSpan = Math.max(1, ms(timing.catchUpSpanMs, inRate))
    this.maxSpeedUp = timing.maxSpeedUp
    this.ahead = this.kernel.taps + Math.ceil(this.fade * this.baseStep * (1 + this.maxSpeedUp)) + 2
    this.ring = new Float32Array(CAPACITY + this.kernel.taps)
    this.tail = new Float32Array(this.fade)
  }

  /**
   * More of his voice, in order.
   * @param pcm - 16-bit samples at his rate
   */
  push(pcm: Int16Array | Float32Array): void {
    let n = pcm.length
    if (!n) return
    const scale = pcm instanceof Int16Array ? 1 / 32768 : 1
    let from = 0
    if (n > CAPACITY / 4) {
      from = n - CAPACITY / 4 // longer than the ring can hold beside the rest: its end is what matters
      n = CAPACITY / 4
    }
    for (let j = 0; j < n; j++) this.put(this.written + j, pcm[from + j] * scale)
    this.written += n
    for (let j = 0; j < this.ahead; j++) this.put(this.written + j, 0)
    this.received += pcm.length
    this.lastPush = this.clock
    if (this.playing && this.gainStep < 0) this.gainStep = (1 - this.gain) / this.fade // it came in time: back up
    const held = this.written - this.read
    if (held > this.maxHeld) this.maxHeld = held
    if (held > this.skipAbove) this.skipTo(this.written - this.target)
    if (this.written >= REBASE) {
      this.written -= REBASE
      this.read -= REBASE
    }
  }

  /** Drop everything held (fading out what plays): the next sound starts from silence. */
  flush(): void {
    if (this.playing) this.fadeOutTail()
    for (let j = 1; j <= this.kernel.half; j++) this.put(this.written - j, 0)
    this.read = this.written
    this.playing = false
    this.sounding = false
    this.gain = 0
    this.gainStep = 0
    this.flushes++
  }

  /**
   * Fill one render quantum.
   * @param out - the output samples, at the context's rate
   */
  render(out: Float32Array): void {
    const { ring, kernel } = this
    const half = kernel.half
    this.steer()
    const step = this.baseStep * this.speed
    for (let k = 0; k < out.length; k++) {
      if (!this.playing) this.maybeStart()
      let s = 0
      if (this.playing) {
        const held = this.written - this.read
        if (held <= 0) this.stop()
        else {
          if (this.gainStep >= 0 && held < this.drainAt) this.gainStep = (-this.gain * step) / held // fade over what's left
          const i = Math.floor(this.read)
          s = interpolate(kernel, ring, (i - half + 1) & MASK, this.read - i) * this.gain
          this.read += step
          if (this.gainStep !== 0) {
            this.gain += this.gainStep
            if (this.gain >= 1) {
              this.gain = 1
              this.gainStep = 0
              this.sounding = true
            } else if (this.gain <= 0) this.stop()
          }
        }
      }
      if (this.tailPos < this.tailLen) s += this.tail[this.tailPos++]
      out[k] = s
      this.squares += s * s
    }
    this.clock += out.length
    this.rendered += out.length
  }

  /** How it is doing; the maximum held and the RMS start again from here. */
  stats(): PlayerStats {
    const ms = (samples: number) => Math.round((samples / this.inRate) * 10_000) / 10
    const stats = {
      heldMs: ms(this.written - this.read),
      maxHeldMs: ms(this.maxHeld),
      playing: this.playing,
      speed: Math.round(this.speed * 10_000) / 10_000,
      received: this.received,
      drains: this.drains,
      skips: this.skips,
      flushes: this.flushes,
      rms: this.rendered ? Math.sqrt(this.squares / this.rendered) : 0,
    }
    this.maxHeld = this.written - this.read
    this.squares = 0
    this.rendered = 0
    return stats
  }

  private put(index: number, value: number): void {
    const i = index & MASK
    this.ring[i] = value
    if (i < this.kernel.taps) this.ring[i + CAPACITY] = value
  }

  private maybeStart(): void {
    const held = this.written - this.read
    if (held <= 0 || (held < this.target && this.clock - this.lastPush < this.startAfter)) return
    this.playing = true
    this.sounding = false
    this.gain = 0
    this.gainStep = 1 / this.fade
  }

  private stop(): void {
    if (this.sounding) this.drains++
    this.playing = false
    this.sounding = false
    this.gain = 0
    this.gainStep = 0
    if (this.read > this.written) this.read = this.written
  }

  /** The speed for this quantum: a little faster while holding well over the target, eased in and out. */
  private steer(): void {
    let goal = 1
    if (this.playing) {
      const over = (this.written - this.read - this.catchUpAbove) / this.catchUpSpan
      goal = 1 + this.maxSpeedUp * Math.min(1, Math.max(0, over))
    }
    this.speed += (goal - this.speed) * 0.05
    if (Math.abs(this.speed - goal) < 1e-5) this.speed = goal
  }

  /** Jump to `to` (ahead): the old position fades out while the new one fades in. */
  private skipTo(to: number): void {
    if (this.playing) {
      this.fadeOutTail()
      this.gain = 0
      this.gainStep = 1 / this.fade
    }
    this.read = to
    this.skips++
  }

  /** The next `fade` samples from the current position, fading out, mixed into what the tail still holds. */
  private fadeOutTail(): void {
    const { ring, kernel, fade, tail } = this
    const half = kernel.half
    const step = this.baseStep * this.speed
    const rest = this.tailLen - this.tailPos
    let pos = this.read
    for (let t = 0; t < fade; t++) {
      const i = Math.floor(pos)
      const v = pos < this.written ? interpolate(kernel, ring, (i - half + 1) & MASK, pos - i) : 0
      tail[t] = v * this.gain * (1 - t / fade) + (t < rest ? tail[this.tailPos + t] : 0)
      pos += step
    }
    this.tailLen = fade
    this.tailPos = 0
  }
}
