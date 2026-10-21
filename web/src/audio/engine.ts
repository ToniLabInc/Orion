// The voice's audio on the page: one AudioContext for the microphone and his voice (so the browser's echo cancellation
// hears what the speakers play), the microphone opened with the browser's own clean-up, and the two AudioWorklet
// processors (voice.worklet.ts). This is the only code here that needs a real browser; it loads when this tab first
// takes the voice (the device link imports it then).

import { messageOf, micError } from "./errors"
import type { PlayerStats } from "./player"
import {
  CAPTURE_PROCESSOR,
  type CaptureReply,
  PLAYER_PROCESSOR,
  type PlayerMessage,
  type PlayerReply,
} from "./protocol"
import workletUrl from "./voice.worklet.ts?worker&url"

export interface VoiceAudioEvents {
  /** A 40 ms block of the microphone, 16 kHz mono. */
  block(samples: Float32Array): void
  /** The microphone's RMS over the last ~33 ms. */
  level(rms: number): void
  /** The microphone stopped by itself (unplugged, its permission taken back): the message says so. */
  ended(why: string): void
}

export interface VoiceAudio {
  /** Queue his voice (24 kHz samples; the buffer is transferred). */
  play(pcm: Int16Array): void
  /** Drop what the player holds. */
  flush(): void
  /** Start the context again if the browser suspended it (call it from a click when you can). */
  resume(): void
  /** The player's numbers (for the demo page and debugging). */
  stats(): Promise<PlayerStats>
  /** Close the microphone and the context. */
  close(): void
  readonly context: AudioContext
}

/**
 * Open the microphone and the player on `context` (created and resumed by the caller inside the user's click).
 * @param context - the AudioContext, at the device's own rate
 * @param events - where blocks, levels and a microphone that stopped go
 * @returns the running audio
 * @throws Error with a message for the user (blocked, missing, busy, can't load); the context is closed then
 */
export async function startVoiceAudio(context: AudioContext, events: VoiceAudioEvents): Promise<VoiceAudio> {
  const [mic, worklet] = await Promise.allSettled([
    navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: { ideal: 1 } },
      video: false,
    }),
    context.audioWorklet.addModule(workletUrl),
  ])
  if (mic.status === "rejected" || worklet.status === "rejected") {
    if (mic.status === "fulfilled") for (const track of mic.value.getTracks()) track.stop()
    void context.close().catch(() => {})
    if (mic.status === "rejected") throw micError(mic.reason)
    console.error("ORION: the voice's audio worklet didn't load", worklet.status === "rejected" && worklet.reason)
    throw new Error("The audio processor couldn't load: reload the page.")
  }
  const stream = mic.value
  let source: MediaStreamAudioSourceNode
  let capture: AudioWorkletNode
  let player: AudioWorkletNode
  try {
    source = context.createMediaStreamSource(stream)
    capture = new AudioWorkletNode(context, CAPTURE_PROCESSOR, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    })
    player = new AudioWorkletNode(context, PLAYER_PROCESSOR, {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    })
  } catch (error) {
    for (const track of stream.getTracks()) track.stop()
    void context.close().catch(() => {})
    throw new Error(`This browser couldn't wire its audio: ${messageOf(error)}`)
  }
  source.connect(capture)
  capture.connect(context.destination) // silent: connected only so the graph keeps pulling it
  player.connect(context.destination)
  capture.port.onmessage = (e: MessageEvent<CaptureReply>) => {
    if (e.data.type === "block") events.block(e.data.samples)
    else events.level(e.data.rms)
  }
  const asked: ((stats: PlayerStats) => void)[] = []
  player.port.onmessage = (e: MessageEvent<PlayerReply>) => asked.shift()?.(e.data.stats)
  const tell = (message: PlayerMessage, transfer: Transferable[] = []) => player.port.postMessage(message, transfer)

  let closed = false
  const [track] = stream.getAudioTracks()
  track?.addEventListener("ended", () => {
    if (!closed) events.ended("The microphone stopped: it was unplugged, or its permission was taken back.")
  })
  const resume = () => {
    if (!closed && context.state !== "running" && context.state !== "closed") void context.resume().catch(() => {})
  }
  // a phone call or another app took the audio ("suspended", Safari's "interrupted"): back as soon as the browser
  // lets it (or at the next click: `talk` resumes too)
  context.addEventListener("statechange", resume)
  resume()

  return {
    context,
    play: (pcm) => tell({ type: "pcm", pcm }, [pcm.buffer]),
    flush: () => tell({ type: "flush" }),
    resume,
    stats: () =>
      new Promise((resolve) => {
        asked.push(resolve)
        tell({ type: "stats" })
      }),
    close() {
      if (closed) return
      closed = true
      capture.port.postMessage({ type: "stop" })
      tell({ type: "stop" })
      source.disconnect()
      capture.disconnect()
      player.disconnect()
      for (const t of stream.getTracks()) t.stop()
      void context.close().catch(() => {})
    },
  }
}
