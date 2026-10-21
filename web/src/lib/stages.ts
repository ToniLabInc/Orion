// A turn's stages as the HUD names them, the same in a turn's strip and in Settings' speed panel: hearing you, his
// thinking, his tools, his voice. Their colours (stage/stages.tsx) are his states' families, a step darker so the four
// stay apart side by side (validated as a categorical set on the dark glass: lightness, chroma, colour-blind and
// normal-vision separation, contrast): your orange, nebula, dusk's gold, aurora. Pure, for the tests.
import type { StageTimes } from "./types"

/** The stages, in the order a turn goes through them. */
export const STAGES = [
  { key: "heard_ms", label: "heard", name: "Hearing you" },
  { key: "think_ms", label: "think", name: "Thinking" },
  { key: "tools_ms", label: "tools", name: "His tools" },
  { key: "speak_ms", label: "speak", name: "Speaking" },
] as const

export type StageKey = (typeof STAGES)[number]["key"]

/** One stage of a turn, with its share of the stages' sum (0 to 1). */
export interface StagePart {
  key: StageKey
  label: string
  name: string
  ms: number
  share: number
}

/**
 * The stages a turn went through (one that took no time is left out: a typed request isn't heard, a written answer
 * isn't spoken), each with its share of their sum.
 * @param times the turn's stages
 * @returns the parts, in order
 */
export function stageParts(times: StageTimes): StagePart[] {
  const shown = STAGES.filter((stage) => times[stage.key] > 0)
  const sum = shown.reduce((total, stage) => total + times[stage.key], 0)
  return shown.map((stage) => ({ ...stage, ms: times[stage.key], share: sum ? times[stage.key] / sum : 0 }))
}

/**
 * A duration as the stages say it: "840 ms" under a second, "2.4 s" under ten, then whole seconds.
 * @param ms the duration
 * @returns its words
 */
export function msText(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))} ms`
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.round(ms / 1000)} s`
}

/** What the server calls the brain of a turn an instant command answered (orion/timing.py). */
export const INSTANT = "instant"

/**
 * A brain's name in the speeds: the model's, and "Instant commands" for the turns that never reached one.
 * @param brain the `brain` of a timing
 * @returns its name
 */
export const brainLabel = (brain: string) => (brain === INSTANT ? "Instant commands" : brain)
