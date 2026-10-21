// The messages between the page and the voice's AudioWorklet processors (voice.worklet.ts).
import type { PlayerStats } from "./player"

/** The capture processor's name ("orion-capture"): the microphone in, 16 kHz blocks and levels out. */
export const CAPTURE_PROCESSOR = "orion-capture"
/** The player processor's name ("orion-player"): his voice in, the speakers out. */
export const PLAYER_PROCESSOR = "orion-player"

/** Capture → page. */
export type CaptureReply = { type: "block"; samples: Float32Array } | { type: "level"; rms: number }

/** Page → either processor. */
export type StopMessage = { type: "stop" }

/** Page → player. */
export type PlayerMessage = { type: "pcm"; pcm: Int16Array } | { type: "flush" } | { type: "stats" } | StopMessage

/** Player → page. */
export type PlayerReply = { type: "stats"; stats: PlayerStats }
