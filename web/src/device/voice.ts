// Carrying Orion's voice from this tab: when the microphone's blocks go to the server, and what happens to his voice.
//
//   off ──takeVoice()──▶ opening ──(audio open, voice_take sent)──▶ waiting ──holder(true)──▶ carrying
//                                                                     │                         │
//          (no word from the server in time: let go) ◀────────────────┘    holder(false): the voice moved elsewhere,
//                                                                          let go here (the server already did)
//   carrying ──a block couldn't be sent (the connection dropped)──▶ lost
//   lost ──takeVoice() (the fresh init: nobody has the voice, so it came home when we dropped)──▶ waiting
//   lost ──holder(false) (someone else took it meanwhile), or no init in time──▶ off
//
// The microphone's blocks go out only while carrying: before the server agrees it would drop them, and after it moves
// the voice elsewhere they aren't ours to send. While waiting, the last second is kept and sent (after a pending
// `talk`) once it agrees, so the first words said right after the click aren't lost. His voice plays from the moment
// the take is sent: the server's frames go out ahead of its events, so they can arrive before its `voice_holder`.
// Lost still counts as carrying for the HUD (this tab means to carry the voice; nothing is sent): on the fresh init,
// the HUD takes the voice back when nobody has it, and only it knows (`holder` says just whether it is this tab).
import { MIC_LEVEL_GAIN, MIC_RATE } from "@/audio/constants"
import type { VoiceAudio, VoiceAudioEvents } from "@/audio/engine"
import { decodeVoiceFrame, encodeMicFrame } from "@/audio/frames"
import { Generations } from "@/audio/generations"
import type { DeviceSend } from "./index"

/** Opens the audio (the microphone and the player); called inside the user's click, before any await. */
export type OpenVoiceAudio = (events: VoiceAudioEvents) => Promise<VoiceAudio>

export interface VoiceTiming {
  /** How long the server's word on a take is awaited; then the take is given up (the server said why in a toast). */
  takeWaitMs: number
  /** How long the microphone stays open after the connection dropped, for it to come back and take the voice again. */
  lostWaitMs: number
  /** While the server's word is awaited, this much of the microphone is kept, to send once it agrees. */
  earlyKeepMs: number
}

export const VOICE_TIMING: VoiceTiming = { takeWaitMs: 30_000, lostWaitMs: 30_000, earlyKeepMs: 1000 }

export type VoiceState = "off" | "opening" | "waiting" | "carrying" | "lost"

export class VoiceCarrier {
  private readonly send: DeviceSend
  private readonly open: OpenVoiceAudio
  private readonly why: () => string | null
  private readonly timing: VoiceTiming
  private current: VoiceState = "off"
  private audio: VoiceAudio | null = null
  private opening: Promise<void> | null = null
  private session = 0 // each opening and letting go: a late completion of an older one is ignored
  private gens = new Generations()
  private pendingTalk = false
  private early: Float32Array[] = []
  private earlySamples = 0
  private timer: ReturnType<typeof setTimeout> | undefined
  private level = 0
  private readonly levelListeners = new Set<(value: number) => void>()

  /**
   * @param send - the connection to the server
   * @param open - opens the audio
   * @param why - why this browser can't carry the voice (null when it can)
   * @param timing - how long to wait for what
   */
  constructor(send: DeviceSend, open: OpenVoiceAudio, why: () => string | null, timing: VoiceTiming = VOICE_TIMING) {
    this.send = send
    this.open = open
    this.why = why
    this.timing = timing
  }

  get state(): VoiceState {
    return this.current
  }

  /** Carrying the voice, or meaning to while the connection is down (see the module's notes). */
  get carrying(): boolean {
    return this.current === "carrying" || this.current === "lost"
  }

  /** Take the voice (see the module's notes); resolves once the audio is open and the server asked. */
  take(): Promise<void> {
    switch (this.current) {
      case "waiting":
      case "carrying":
        this.audio?.resume()
        return Promise.resolve()
      case "opening":
        return this.opening ?? Promise.resolve()
      case "lost":
        this.ask()
        return Promise.resolve()
    }
    const why = this.why()
    if (why) return Promise.reject(new Error(why))
    const session = ++this.session
    this.current = "opening"
    let opened: Promise<VoiceAudio>
    try {
      opened = this.open(this.events(session)) // synchronously: the context starts inside the click
    } catch (error) {
      this.current = "off"
      return Promise.reject(error)
    }
    const opening = opened.then(
      (audio) => {
        if (session !== this.session) {
          audio.close()
          throw letGo()
        }
        this.audio = audio
        this.opening = null
        if (!this.ask()) {
          this.shutdown()
          throw new Error("Orion isn't connected right now: try again in a moment.")
        }
      },
      (error: unknown) => {
        if (session === this.session) {
          this.current = "off"
          this.opening = null
        }
        throw error
      },
    )
    this.opening = opening
    return opening
  }

  /** Let go of the voice: the microphone and the player close, and the server is told. */
  release(): void {
    if (this.current === "off") return
    if (this.current !== "opening") this.send.json({ type: "voice_take", on: false })
    this.shutdown()
  }

  /**
   * The server said who carries the voice (its `voice_holder`, or `init`'s).
   * @param isThisTab - this tab
   */
  holder(isThisTab: boolean): void {
    switch (this.current) {
      case "waiting":
        // not us: an older word, or our take is still under way (it can take a while to load speech recognition)
        if (isThisTab) this.agreed()
        break
      case "carrying":
        if (!isThisTab) this.shutdown() // the voice moved elsewhere: the server already let go of us
        break
      case "lost":
        // the connection is back, and the HUD didn't take the voice back first: someone else has it now
        if (isThisTab) this.agreed()
        else this.shutdown()
        break
      case "off":
        if (isThisTab) this.send.json({ type: "voice_take", on: false }) // a take we gave up on went through late
        break
    }
  }

  /** Ask to talk now (the talk button): sent at once while carrying, or as soon as the server agrees. */
  talk(): void {
    this.audio?.resume() // a click: the moment a suspended context may start again
    if (this.current === "carrying") {
      if (!this.send.json({ type: "talk" })) this.lose()
    } else if (this.current === "opening" || this.current === "waiting") this.pendingTalk = true
  }

  /**
   * A binary frame of his voice.
   * @param frame - the frame
   */
  play(frame: ArrayBuffer): void {
    const audio = this.audio
    if (!audio) return
    const decoded = decodeVoiceFrame(frame)
    if (!decoded) return
    const action = this.gens.frame(decoded.generation)
    if (action === "drop") return
    if (action === "flush-play") audio.flush()
    audio.play(decoded.pcm)
  }

  /**
   * The server stopped his voice (`speaker {gen}`).
   * @param generation - the generation after the stop
   */
  flush(generation: number): void {
    if (this.audio && this.gens.flush(generation)) this.audio.flush()
  }

  /**
   * Listen to the microphone's level (0…1, smoothed, ~30 Hz) while carrying; 0 once it stops.
   * @param listener - told each level
   * @returns the unsubscribe function
   */
  onLevel(listener: (value: number) => void): () => void {
    this.levelListeners.add(listener)
    return () => {
      this.levelListeners.delete(listener)
    }
  }

  /** The player's numbers (the demo page). */
  stats() {
    return this.audio?.stats() ?? Promise.resolve(null)
  }

  /** The audio context while the audio is open (the demo page). */
  context(): AudioContext | null {
    return this.audio?.context ?? null
  }

  private events(session: number): VoiceAudioEvents {
    const mine = () => session === this.session
    return {
      block: (samples) => {
        if (mine()) this.block(samples)
      },
      level: (rms) => {
        if (mine()) this.heard(rms)
      },
      ended: (why) => {
        if (!mine()) return
        console.warn("ORION:", why)
        this.release()
      },
    }
  }

  /** Send `voice_take`, then wait for the server's word. */
  private ask(): boolean {
    if (!this.send.json({ type: "voice_take", on: true })) return false
    this.gens = new Generations() // the server opens a fresh output for this take
    this.current = "waiting"
    this.early = []
    this.earlySamples = 0
    this.arm(this.timing.takeWaitMs, () => {
      if (this.current !== "waiting") return
      console.warn("ORION: the server didn't give this tab the voice; letting go")
      this.release()
    })
    return true
  }

  private agreed(): void {
    this.current = "carrying"
    this.disarm()
    if (this.pendingTalk) {
      this.pendingTalk = false
      this.send.json({ type: "talk" })
    }
    const early = this.early
    this.early = []
    this.earlySamples = 0
    for (const block of early) if (!this.sendBlock(block)) return
  }

  private block(samples: Float32Array): void {
    if (this.current === "carrying") this.sendBlock(samples)
    else if (this.current === "waiting") {
      this.early.push(samples)
      this.earlySamples += samples.length
      const keep = (this.timing.earlyKeepMs * MIC_RATE) / 1000
      while (this.earlySamples - this.early[0].length >= keep) {
        this.earlySamples -= this.early[0].length
        this.early.shift()
      }
    }
  }

  private sendBlock(samples: Float32Array): boolean {
    if (this.send.binary(encodeMicFrame(samples))) return true
    this.lose()
    return false
  }

  /** The connection dropped while carrying: hold on for it to come back (the server let go of us meanwhile). */
  private lose(): void {
    this.current = "lost"
    this.pendingTalk = false
    this.quiet()
    this.arm(this.timing.lostWaitMs, () => {
      if (this.current === "lost") this.shutdown()
    })
  }

  private heard(rms: number): void {
    if (this.current !== "carrying") return
    const value = Math.min(1, rms * MIC_LEVEL_GAIN)
    this.level += (value - this.level) * (value > this.level ? 0.6 : 0.25) // quick to rise, slower to fall
    for (const listener of this.levelListeners) listener(this.level)
  }

  private quiet(): void {
    if (this.level === 0) return
    this.level = 0
    for (const listener of this.levelListeners) listener(0)
  }

  private shutdown(): void {
    this.session++
    this.current = "off"
    this.disarm()
    this.pendingTalk = false
    this.early = []
    this.earlySamples = 0
    this.opening = null
    this.audio?.close()
    this.audio = null
    this.quiet()
  }

  private arm(ms: number, fn: () => void): void {
    this.disarm()
    this.timer = setTimeout(fn, ms)
  }

  private disarm(): void {
    clearTimeout(this.timer)
    this.timer = undefined
  }
}

/** The voice was let go before the microphone opened (the caller may ignore it: its name is "AbortError"). */
function letGo(): Error {
  const error = new Error("The voice was let go before the microphone opened.")
  error.name = "AbortError"
  return error
}
