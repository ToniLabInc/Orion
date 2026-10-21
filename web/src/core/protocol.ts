/**
 * What the page tells Horizon's engine and what the engine tells back. client.ts encodes, engine.ts acts, in a worker
 * (worker.ts) or on the page. A shared leaf: it imports types only, so the worker bundles nothing of the HUD's.
 *
 * Points the page sends are CSS pixels from the canvas's top left (the limb's top, the talk button, a meteor's
 * target), or −1..1 for the pointer (x right, y down). The engine turns them into device pixels itself, at the
 * resolution it draws.
 */
import type { CoreHue, CoreLayout, CoreQuality, CoreState } from "./index"

/** A sound level's bands. */
export const BANDS = 16
/** Floats in a `level` message: the source (0 the microphone, 1 his voice), the level, the bands. */
export const LEVEL_FLOATS = 2 + BANDS
/** The longest text the pen writes, characters, and the longest a writing is held, ms. */
export const WRITE_MAX_CHARS = 12
export const WRITE_MAX_MS = 600_000
/** The most stars a recall brightens. */
export const RECALL_MAX = 8
/** Every character the pen can write (pen.ts has a glyph for each). */
export const WRITABLE = "0123456789:.,°%+-−/ hmsapCF"

export const HUES: readonly CoreHue[] = ["rigel", "nebula", "aurora", "betel", "alert", "ok"]
export const STATES: readonly CoreState[] = ["idle", "listening", "thinking", "speaking", "loading", "error"]
export type Shape = "question" | "check"
const SHAPES: readonly Shape[] = ["question", "check"]

/** Knobs for the demo and screenshots only; the HUD never sets them. */
export interface EngineDebug {
  /** Pin a quality tier (0–3) instead of adapting. */
  tier?: number
  /** The engine feeds itself speech-like levels: the microphone while listening, his voice while speaking. */
  synth?: boolean
}

/** How the engine starts. */
export interface EngineInit {
  /** The canvas in CSS pixels, and the screen's device pixels per CSS pixel. */
  width: number
  height: number
  dpr: number
  quality: CoreQuality
  reduced: boolean
  visible: boolean
  /** Where the star catalogue is (stars.bin, from our own origin). */
  stars: string
  debug: EngineDebug
}

/** A message from the page to the engine. */
export type ToEngine =
  | { type: "size"; width: number; height: number; dpr: number }
  | { type: "layout"; horizon: number; talkX: number; talkY: number }
  | { type: "state"; state: CoreState }
  | { type: "level"; data: Float32Array } // LEVEL_FLOATS: encodeLevel()
  | { type: "pointer"; x: number; y: number }
  | { type: "meteor"; hue: CoreHue; x: number; y: number }
  | { type: "recall"; count: number }
  | { type: "write"; text: string | null; shape: Shape | null; ms: number }
  | { type: "rest"; on: boolean }
  | { type: "quality"; quality: CoreQuality }
  | { type: "visible"; on: boolean }
  | { type: "motion"; reduced: boolean }
  | { type: "advance"; seconds: number } // debug: run the clock this far in fixed steps, then draw one still frame
  | { type: "bench"; frames: number } // debug: time `frames` whole frames at each tier, back to back
  | { type: "dispose" }

/** One tier's frame time in the bench: whole frames with the GPU waited for (`ms`), and the GPU's own time per frame by pass (`gpu`, ms; null where the browser has no timer queries). */
export interface BenchTier {
  tier: number
  ms: number
  gpu: { total: number; passes: Record<string, number> } | null
}

/** A message from the engine to the page. */
export type FromEngine =
  | { type: "ready"; tier: number }
  | { type: "failed"; error: string }
  | { type: "problem"; message: string } // something went wrong that the page should say (the catalogue didn't load)
  | { type: "stats"; fps: number; ms: number; cpu: number; gpu: number | null; tier: number } // ms: between frames;
  // cpu: the thread's own time per frame; gpu: the GPU's own (timer queries), null where the browser has none
  | { type: "bench"; size: [number, number]; tiers: BenchTier[] }
  | { type: "advanced" } // debug: the frame an `advance` asked for is drawn

const unit = (value: number) => (Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0)

/**
 * A sound level as one small array: the source, the level and 16 bands, each clamped to 0..1 (missing bands are 0).
 * @param source the microphone (you) or his voice
 * @param value the level, 0..1
 * @param bands up to 16 bands, 0..1
 * @returns the level message's data
 */
export function encodeLevel(source: "mic" | "voice", value: number, bands: readonly number[]): Float32Array {
  const data = new Float32Array(LEVEL_FLOATS)
  data[0] = source === "mic" ? 0 : 1
  data[1] = unit(value)
  const n = Math.min(BANDS, bands.length)
  for (let i = 0; i < n; i++) data[2 + i] = unit(bands[i])
  return data
}

/**
 * Read a level message into the engine's own arrays (no allocation).
 * @param data the message's data
 * @param bands where the 16 bands go
 * @returns the source (0 the microphone, 1 his voice) and the level
 */
export function decodeLevel(data: Float32Array, bands: Float32Array): { source: 0 | 1; value: number } {
  for (let i = 0; i < BANDS; i++) bands[i] = data[2 + i] ?? 0
  return { source: data[0] === 0 ? 0 : 1, value: data[1] }
}

/**
 * The layout's message, moved by the canvas's own offset on the page.
 * @param layout the limb's top and the talk button, CSS px from the viewport's top left
 * @param left the canvas's left on the page, CSS px
 * @param top the canvas's top on the page, CSS px
 * @returns the message, or null when a coordinate isn't a number
 */
export function encodeLayout(layout: CoreLayout, left = 0, top = 0): Extract<ToEngine, { type: "layout" }> | null {
  const { horizon, talk } = layout
  if (![horizon, talk?.x, talk?.y].every(Number.isFinite)) return null
  return { type: "layout", horizon: horizon - top, talkX: talk.x - left, talkY: talk.y - top }
}

/**
 * A meteor's message: its target moved by the canvas's own offset, an unknown hue his own (rigel).
 * @param meteor the meteor as the HUD asks for it
 * @param left the canvas's left on the page, CSS px
 * @param top the canvas's top on the page, CSS px
 * @returns the message, or null when the target isn't a point
 */
export function encodeMeteor(
  meteor: { to: { x: number; y: number }; hue: CoreHue },
  left = 0,
  top = 0,
): Extract<ToEngine, { type: "meteor" }> | null {
  const { x, y } = meteor.to ?? {}
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null
  const hue = HUES.includes(meteor.hue) ? meteor.hue : "rigel"
  return { type: "meteor", hue, x: x - left, y: y - top }
}

/**
 * A writing's message, or why it can't be written: a text of WRITABLE characters (trimmed, at most
 * WRITE_MAX_CHARS) or a known shape (text wins), held 0..WRITE_MAX_MS. `ms: 0` with neither releases whatever is
 * written.
 * @param write the writing as the HUD asks for it
 * @returns the message, or the reason it was refused
 */
export function encodeWrite(write: {
  text?: string
  shape?: Shape
  ms: number
}): Extract<ToEngine, { type: "write" }> | { error: string } {
  if (!Number.isFinite(write.ms) || write.ms < 0)
    return { error: `a writing's ms must be a number ≥ 0, not ${write.ms}` }
  const ms = Math.min(WRITE_MAX_MS, write.ms)
  const text = typeof write.text === "string" ? write.text.trim() : ""
  if (text) {
    const chars = [...text]
    if (chars.length > WRITE_MAX_CHARS)
      return { error: `"${text}" is too long to write: ${chars.length} characters, ${WRITE_MAX_CHARS} at most` }
    const strange = [...new Set(chars.filter((c) => !WRITABLE.includes(c)))]
    if (strange.length) return { error: `the pen can't write ${strange.map((c) => `"${c}"`).join(", ")}` }
    return { type: "write", text, shape: null, ms }
  }
  if (write.shape !== undefined) {
    if (!SHAPES.includes(write.shape)) return { error: `the pen knows no shape "${write.shape}"` }
    return { type: "write", text: null, shape: write.shape, ms }
  }
  if (ms === 0) return { type: "write", text: null, shape: null, ms: 0 }
  return { error: "nothing to write: give a text or a shape" }
}
