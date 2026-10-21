// The voice on the wire, as Orion's server has it (orion/voice, orion/server.py), and the timing of this browser's
// side of it. One place, so the worklets, the device link and the tests agree.

/** The microphone's samples go to the server at this rate (orion.voice.MIC_RATE), mono int16. */
export const MIC_RATE = 16_000
/** His voice comes at this rate (orion.voice.TTS_RATE), mono int16. */
export const VOICE_RATE = 24_000
/** First byte of a binary frame of the microphone (orion/server.py MIC_INT16_KIND), then int16 LE samples: half the
 * bytes of float32 for a phone on mobile data, and plenty for speech recognition. */
export const MIC_FRAME_KIND = 0x03
/** First byte of a binary frame of his voice (orion/voice/tts.py AUDIO_FRAME), then u32 LE generation, int16 LE. */
export const VOICE_FRAME_KIND = 0x02
/** The microphone is sent in blocks this long. */
export const MIC_BLOCK_MS = 40
/** The microphone's level is told this often. */
export const MIC_LEVEL_HZ = 30
/** The level's RMS is scaled as the server scales its own microphone's (orion/voice/listener.py LEVEL_GAIN). */
export const MIC_LEVEL_GAIN = 6

/** How the player holds his voice: a jitter buffer that crossfades whatever it starts, stops or skips. */
export interface PlayerTiming {
  /** Held before a sound starts playing (the server sends 160 ms ahead, then one 40 ms block every 40 ms). */
  targetMs: number
  /** A sound shorter than the target starts once nothing more came for this long. */
  startAfterMs: number
  /** Every start, stop, skip and flush fades over this long (no clicks). */
  fadeMs: number
  /** Holding more than this (a burst after the network stalled), it plays a little faster to catch up… */
  catchUpAboveMs: number
  /** …up to this much faster (0.04 = 4 %, under a semitone)… */
  maxSpeedUp: number
  /** …reached this far above `catchUpAboveMs`. */
  catchUpSpanMs: number
  /** Holding more than this, it skips ahead to the target (crossfaded): late speech is worse than a gap. */
  skipAboveMs: number
}

export const PLAYER_TIMING: PlayerTiming = {
  targetMs: 120,
  startAfterMs: 80,
  fadeMs: 5,
  catchUpAboveMs: 200,
  maxSpeedUp: 0.04,
  catchUpSpanMs: 200,
  skipAboveMs: 1000,
}
