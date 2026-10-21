/**
 * How hard Horizon may work. Four tiers, lowest first: the most device pixels per CSS pixel it draws at (the lines
 * and stars stay crisp up to the screen's own density), the aurora's target in pixels per CSS pixel (it is drawn at
 * that resolution while it shows, so its fine rays stay resolved at any screen density) and how many curtain sheets
 * it has, and the faintest star drawn (the catalogue is brightest first, so that is a count). `auto` starts at the
 * third and adapts to how frames keep up: an average frame slower than 18 ms for 2 s steps down; 10 s on time steps
 * up (and a step up that doesn't hold makes the next one wait twice as long). `high` and `low` pin a tier; `off` is
 * the still (no engine). Plain numbers: no WebGL here, so it is tested in Node.
 */
import type { CoreQuality } from "./index"

export interface Tier {
  /** The most device pixels per CSS pixel. */
  dpr: number
  /** The aurora target's most pixels per CSS pixel. */
  aurora: number
  /** Curtain sheets, each with its own folds and rays. */
  sheets: 1 | 2
  /** The faintest star drawn, magnitude. */
  faintest: number
}

export const TIERS: readonly Tier[] = [
  { dpr: 1, aurora: 0.5, sheets: 1, faintest: 5.5 },
  { dpr: 1.5, aurora: 1, sheets: 2, faintest: 6.5 }, // low
  { dpr: 2, aurora: 1, sheets: 2, faintest: 6.5 }, // auto starts here
  { dpr: 3, aurora: 1.5, sheets: 2, faintest: 6.5 }, // high
]
export const TOP = TIERS.length - 1

/** A frame slower than this on average, for DOWN_AFTER_S, steps the quality down. */
export const SLOW_MS = 18
/** Frames this quick on average count as headroom. */
export const EASY_MS = 17.5
export const DOWN_AFTER_S = 2
export const UP_AFTER_S = 10
/** The longest a step up may be made to wait after failed ones. */
export const UP_AFTER_MAX_S = 160

/**
 * The tier a preference starts at.
 * @param preference the quality chosen in Settings
 * @returns the tier, 0..TOP
 */
export function startTier(preference: CoreQuality): number {
  if (preference === "high") return TOP
  if (preference === "low" || preference === "off") return 1
  return 2
}

/** Watches frame times and moves the tier (only for `auto`). */
export class Governor {
  tier: number
  min: number
  max: number
  private avg = 1000 / 60
  private slow = 0
  private easy = 0
  private hold = UP_AFTER_S
  private sinceUp = Number.POSITIVE_INFINITY

  constructor(preference: CoreQuality, pinned?: number) {
    this.tier = this.min = this.max = 0
    this.prefer(preference, pinned)
  }

  /**
   * A new preference (or a pinned tier, for the demo): `auto` adapts over every tier, the others hold one.
   * @param preference the quality chosen in Settings
   * @param pinned a tier to hold whatever the preference
   */
  prefer(preference: CoreQuality, pinned?: number): void {
    if (pinned !== undefined) {
      this.tier = this.min = this.max = Math.min(TOP, Math.max(0, Math.round(pinned)))
    } else if (preference === "auto") {
      this.min = 0
      this.max = TOP
      this.tier = startTier(preference)
    } else {
      this.tier = this.min = this.max = startTier(preference)
    }
    this.hold = UP_AFTER_S
    this.reset()
  }

  /** Forget the frames so far (after a pause, a resize or a tier change: the next interval spans it). */
  reset(): void {
    this.avg = 1000 / 60
    this.slow = 0
    this.easy = 0
  }

  /**
   * One frame drawn while the loop aims at 60 a second.
   * @param ms the time since the frame before it
   * @returns -1 when the tier just stepped down, 1 when it stepped up, 0 otherwise
   */
  frame(ms: number): -1 | 0 | 1 {
    if (this.min === this.max || !(ms > 0)) return 0
    const s = Math.min(ms, 250) / 1000
    this.avg += (Math.min(ms, 250) - this.avg) * 0.1
    this.sinceUp += s
    if (this.avg > SLOW_MS) {
      this.slow += s
      this.easy = 0
    } else {
      this.slow = 0
      this.easy = this.avg < EASY_MS ? this.easy + s : 0
    }
    if (this.slow >= DOWN_AFTER_S && this.tier > this.min) {
      // the step up just before didn't hold: the next one waits twice as long
      if (this.sinceUp < DOWN_AFTER_S * 3) this.hold = Math.min(UP_AFTER_MAX_S, this.hold * 2)
      this.tier--
      this.sinceUp = Number.POSITIVE_INFINITY
      this.reset()
      return -1
    }
    if (this.easy >= this.hold && this.tier < this.max) {
      this.tier++
      this.sinceUp = 0
      this.reset()
      return 1
    }
    return 0
  }
}

/**
 * How many times the GPU's own time per frame counts against the tier: a tier is kept while its frames cost less GPU
 * than a fourth of a 60 Hz frame (4.4 ms), whatever the interval between frames says (a display locked to 60 Hz never
 * shows a GPU at a third of its time, and the machine's other work wants the rest).
 */
export const GPU_SLOWNESS = 4

/**
 * What the governor is told a frame took: the interval since the last frame, or the GPU's time scaled by GPU_SLOWNESS,
 * whichever is worse.
 * @param gap ms since the frame before
 * @param gpu the GPU's smoothed time per frame, ms; null where the browser can't measure it
 * @returns ms
 */
export function effectiveMs(gap: number, gpu: number | null): number {
  return gpu === null ? gap : Math.max(gap, gpu * GPU_SLOWNESS)
}

/** The frame rate while only the stars low over the limb twinkle; while anything else moves, ACTIVE_FPS. */
export const IDLE_FPS = 24
export const ACTIVE_FPS = 60

/**
 * Which animation frames to draw: the display calls back at its own rate (60, 120, 144 Hz…), Horizon wants 60 a
 * second while something moves and 24 while only the stars twinkle. The time owed carries over, so the average lands
 * on the rate wanted whatever the display's (24 on 60 Hz: two callbacks, then three).
 */
export class Pacer {
  private debt = 0
  /** The display's interval, ms: the shortest seen lately. */
  vsync = 1000 / 60

  /** Start over (after a pause: the first interval spans it). */
  reset(): void {
    this.debt = 0
  }

  /**
   * An animation frame came, `delta` ms after the one before.
   * @param delta the time since the last callback
   * @param fps the rate wanted now
   * @returns whether to draw this one
   */
  due(delta: number, fps: number): boolean {
    this.vsync = delta < this.vsync ? delta : this.vsync + (delta - this.vsync) * 0.02
    const interval = 1000 / fps
    this.debt += delta
    if (this.debt + this.vsync * 0.5 < interval) return false
    this.debt = Math.min(this.debt - interval, interval)
    return true
  }
}
