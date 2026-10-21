// Horizon: Orion's body on the stage. Not an object: the night side of a world seen from orbit, curving across the
// lower part of the screen, the thin airglow along its limb (his voice: a vibrating string), aurora curtains rising
// from it while he thinks, meteors for his actions, light written above the limb for short answers, and above it all
// the real sky with the constellation Orion. WebGL 2 in a worker behind the DOM; a static SVG still of the same scene
// where it can't draw or is switched off.
//
// This file is the contract between the engine (src/core/*, its owner) and the rest of the HUD, which only ever calls
// these methods. client.ts implements them; the engine and its parts live beside it.
import { connect } from "./client"

export type CoreState = "idle" | "listening" | "thinking" | "speaking" | "loading" | "error"
export type CoreQuality = "auto" | "high" | "low" | "off"
export type CoreHue = "rigel" | "nebula" | "aurora" | "betel" | "alert" | "ok"

export interface CoreOptions {
  quality: CoreQuality
  reducedMotion: boolean
}

/**
 * Where the world is, in CSS pixels from the viewport's top left. The limb is a circle whose top is at
 * (the viewport's horizontal centre, `horizon`), its radius 3.2 viewport widths: the world fills everything below it
 * and falls away at the sides (75 px at each edge of a 1920 px screen). `talk` is the middle of the talk button, where
 * the limb's string meets you: your voice plucks it there and ripples run outward, a new state's colour spreads from
 * there along the limb, meteors leave the limb near it, and light is written above it, centred on its x.
 *
 * What Horizon draws in the sky, for a layout to leave room for: a writing's middle is 42% of the way down from the
 * top to the limb, its digits 16% of `horizon` tall (56 to 150 px; shrunk to fit a narrow screen); the aurora's rays
 * stand up to 60% of `horizon` above the limb (most are far shorter); the Orion figure fills the sky from 11% to 83%
 * of `horizon` while resting. Behind glass panels all of it stays legible and soft.
 */
export interface CoreLayout {
  horizon: number
  talk: { x: number; y: number }
}

export interface Core {
  /** His state: the airglow's colour (rigel at rest, betel listening, nebula thinking, aurora speaking, dusk
   *  loading, alert on an error) and what moves: the aurora while he thinks, the string while he speaks. */
  state(state: CoreState): void
  /** A sound level, at most 30 times a second: his voice drives the string's standing waves (its bands, low to
   *  high, the long waves to the short), your microphone plucks it at the talk button. `value` and each band are
   *  0..1; `bands` holds up to 16 (fewer, or none, is fine: the missing ones are silent). */
  level(source: "mic" | "voice", value: number, bands: readonly number[]): void
  /** The pointer, −1..1 with y down (x: clientX / innerWidth * 2 − 1; y the same): a whisper of parallax. */
  pointer(x: number, y: number): void
  /** Where the limb's top is and where the talk button is. Nothing is drawn before the first layout; call it again
   *  whenever either moves (a resize, the composer growing). */
  layout(layout: CoreLayout): void
  /** One of his actions reaching a point (a tool's result arriving at a card): a meteor streaks from the limb to
   *  `to`, CSS pixels from the viewport's top left, its light in `hue`. */
  meteor(meteor: { to: { x: number; y: number }; hue: CoreHue }): void
  /** Memories recalled: `count` stars (1 to 8) brighten in turn. */
  recall(count: number): void
  /**
   * Light written above the limb in one stroke, like a pen: a short `text` (up to 12 characters of
   * `0123456789 : . , ° % + - − / h m s a p C F` and spaces: a time, a temperature, a timer) or a `shape` (a
   * question mark while a question waits, a check mark when something is done). It is held `ms` after it is
   * written (up to 10 minutes), then fades. A new writing replaces the one shown. `ms: 0` releases a held one:
   * with a text or a shape, only when that is what is written now (a stale release leaves a newer writing alone);
   * with neither, whatever is written.
   */
  write(write: { text?: string; shape?: "question" | "check"; ms: number }): void
  /** Rest after a long idle: the airglow dims and the Orion figure draws itself among the stars. `false` wakes him
   *  (any activity should). */
  rest(on: boolean): void
  /** The quality tier (Settings, This screen): `off` swaps the engine for a static SVG still of the same scene. */
  quality(quality: CoreQuality): void
  /** Whether the canvas is seen (false behind a page that covers it): no frames while hidden. A hidden tab stops it
   *  by itself. */
  visibility(visible: boolean): void
  dispose(): void
}

/**
 * Horizon on a canvas: a full-viewport canvas fixed behind the DOM (draw nothing else on it), handed to a worker
 * where the browser allows it. Where WebGL 2 is missing, or with quality "off", a static SVG of the same scene is put
 * beside the canvas in its place (same box, aria-hidden, no pointer events).
 */
export function createCore(canvas: HTMLCanvasElement, options: CoreOptions): Core {
  return connect(canvas, options)
}
