// The voice's AudioWorklet module: the capture processor (the microphone, resampled to 16 kHz blocks for the server)
// and the player (his voice, through a jitter buffer, resampled to the context's rate). Loaded from our own origin by
// `audioWorklet.addModule` (Vite bundles it as its own file, see engine.ts): a worklet runs under `script-src 'self'`.
// The work itself is plain logic in capture.ts and player.ts, tested in Node.
import { MicCapture } from "./capture"
import { JitterPlayer } from "./player"
import {
  CAPTURE_PROCESSOR,
  type CaptureReply,
  PLAYER_PROCESSOR,
  type PlayerMessage,
  type PlayerReply,
  type StopMessage,
} from "./protocol"

// AudioWorkletGlobalScope, which TypeScript's DOM library doesn't describe
declare const sampleRate: number
declare class AudioWorkletProcessor {
  readonly port: MessagePort
}
declare function registerProcessor(name: string, processor: new () => AudioWorkletProcessor): void

class Capture extends AudioWorkletProcessor {
  private readonly capture = new MicCapture(sampleRate)
  private live = true
  private readonly sink = {
    block: (samples: Float32Array) => this.post({ type: "block", samples }, [samples.buffer]),
    level: (rms: number) => this.post({ type: "level", rms }),
  }

  constructor() {
    super()
    this.port.onmessage = (e: MessageEvent<StopMessage>) => {
      if (e.data.type === "stop") this.live = false
    }
  }

  process(inputs: Float32Array[][]): boolean {
    const channels = inputs[0]
    if (this.live && channels?.length) this.capture.push(channels, this.sink)
    return this.live
  }

  private post(message: CaptureReply, transfer: Transferable[] = []) {
    this.port.postMessage(message, transfer)
  }
}

class Player extends AudioWorkletProcessor {
  private readonly player = new JitterPlayer(sampleRate)
  private live = true

  constructor() {
    super()
    this.port.onmessage = (e: MessageEvent<PlayerMessage>) => {
      const message = e.data
      switch (message.type) {
        case "pcm":
          this.player.push(message.pcm)
          break
        case "flush":
          this.player.flush()
          break
        case "stats":
          this.port.postMessage({ type: "stats", stats: this.player.stats() } satisfies PlayerReply)
          break
        case "stop":
          this.live = false
          break
      }
    }
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const channels = outputs[0]
    if (this.live && channels?.length) {
      this.player.render(channels[0])
      for (let c = 1; c < channels.length; c++) channels[c].set(channels[0])
    }
    return this.live
  }
}

registerProcessor(CAPTURE_PROCESSOR, Capture)
registerProcessor(PLAYER_PROCESSOR, Player)
