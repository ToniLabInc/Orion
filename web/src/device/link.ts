// The device link (the contract is index.ts): this browser's voice (voice.ts and src/audio), eyes (share.ts) and
// clipboard (clipboard.ts), and the server's requests to them.
import { messageOf } from "@/audio/errors"
import { type BrowserGlobals, cameraSeen, capsOf, deviceName, platformOf, whyNoVoice } from "./caps"
import { ClipboardAccess } from "./clipboard"
import type { Cap, DeviceLink, DeviceRequest, DeviceSend, Shared } from "./index"
import { type ShareEnv, Shares } from "./share"
import { type OpenVoiceAudio, VoiceCarrier } from "./voice"

/** What the link takes from the browser (the real one by default; a test hands in its own). */
export interface DeviceEnv {
  globals: BrowserGlobals
  openAudio: OpenVoiceAudio
  share?: ShareEnv
  clipboard?: () => Clipboard | undefined
}

interface AskedShare {
  what: Shared
  waiters: { resolve(): void; reject(error: Error): void }[]
  view: { what: Shared; accept(): Promise<void>; decline(): void }
}

/** The audio, opened inside the user's click: the context first (before any await), then the rest. */
const openAudio: OpenVoiceAudio = (events) => {
  let context: AudioContext
  try {
    context = new AudioContext({ latencyHint: "interactive" })
  } catch (error) {
    return Promise.reject(new Error(`This browser couldn't start its audio: ${messageOf(error)}`))
  }
  void context.resume().catch(() => {})
  return import("@/audio/engine").then(
    ({ startVoiceAudio }) => startVoiceAudio(context, events),
    (error: unknown) => {
      void context.close().catch(() => {})
      console.error("ORION: the voice's audio code didn't load", error)
      throw new Error("The audio code couldn't load: reload the page.")
    },
  )
}

const browser = (): DeviceEnv => ({ globals: globalThis as BrowserGlobals, openAudio })

export class BrowserDevice implements DeviceLink {
  private readonly send: DeviceSend
  private readonly env: DeviceEnv
  private readonly voice: VoiceCarrier
  private readonly shares: Shares
  private readonly clipboard: ClipboardAccess
  private readonly label: string
  private camera: boolean | undefined
  private asked: AskedShare | null = null
  private readonly sharingListeners = new Set<(shared: Shared[]) => void>()
  private readonly shareAskListeners = new Set<() => void>()
  private readonly copyListeners = new Set<() => void>()

  /**
   * @param send - the connection to the server
   * @param env - the browser
   */
  constructor(send: DeviceSend, env: DeviceEnv = browser()) {
    this.send = send
    this.env = env
    const nav = env.globals.navigator
    this.label = deviceName(nav?.userAgent ?? "", nav?.userAgentData, nav?.maxTouchPoints ?? 0)
    this.voice = new VoiceCarrier(send, env.openAudio, () => whyNoVoice(this.platform()))
    this.shares = new Shares((shared) => this.sharingChanged(shared), env.share)
    this.clipboard = new ClipboardAccess(() => tell(this.copyListeners), env.clipboard)
    void cameraSeen(nav?.mediaDevices as MediaDevices | undefined).then((seen) => {
      this.camera = seen
    })
  }

  // ---------------------------------------------------------------------------------------------- what it is
  caps(): Cap[] {
    return capsOf(this.platform())
  }

  name(): string {
    return this.label
  }

  // ---------------------------------------------------------------------------------------------- the voice
  takeVoice(): Promise<void> {
    return this.voice.take()
  }

  releaseVoice(): void {
    this.voice.release()
  }

  get carrying(): boolean {
    return this.voice.carrying
  }

  holder(isThisTab: boolean): void {
    this.voice.holder(isThisTab)
  }

  talk(): void {
    this.voice.talk()
  }

  onMicLevel(listener: (value: number) => void): () => void {
    return this.voice.onLevel(listener)
  }

  playFrame(frame: ArrayBuffer): void {
    this.voice.play(frame)
  }

  flushSpeaker(generation: number): void {
    this.voice.flush(generation)
  }

  /** The voice's state, the player's numbers and the audio context (the demo page; not part of the contract). */
  voiceDebug() {
    return { state: this.voice.state, stats: this.voice.stats(), context: this.voice.context() }
  }

  // ---------------------------------------------------------------------------------------------- the eyes
  share(what: Shared): Promise<void> {
    return this.shares.start(what)
  }

  stopShare(what?: Shared): void {
    this.shares.stop(what)
  }

  sharing(): Shared[] {
    return this.shares.list()
  }

  onSharing(listener: (shared: Shared[]) => void): () => void {
    return subscribe(this.sharingListeners, listener)
  }

  stream(what: Shared): MediaStream | null {
    return this.shares.stream(what)
  }

  get pendingShare(): AskedShare["view"] | null {
    return this.asked?.view ?? null
  }

  onPendingShare(listener: () => void): () => void {
    return subscribe(this.shareAskListeners, listener)
  }

  // ---------------------------------------------------------------------------------------------- the clipboard
  get pendingCopy() {
    return this.clipboard.pending
  }

  onPendingCopy(listener: () => void): () => void {
    return subscribe(this.copyListeners, listener)
  }

  // ---------------------------------------------------------------------------------------------- requests
  async handle(request: DeviceRequest): Promise<Record<string, unknown>> {
    switch (request.kind) {
      case "frame": {
        const what = request.what ?? "screen"
        if (!this.shares.has(what)) throw new Error(`the ${what} isn't shared`)
        return { image: await this.shares.frame(what) }
      }
      case "share":
        return this.askToShare(request.what ?? "screen")
      case "clipboard_read":
        return { text: await this.clipboard.read() }
      case "clipboard_write":
        await this.clipboard.write(request.text ?? "")
        return {}
      default:
        throw new Error(`this device doesn't know the request "${(request as { kind: string }).kind}"`)
    }
  }

  dispose(): void {
    this.voice.release()
    this.closeAsk(new Error("the HUD closed"))
    this.clipboard.drop("the HUD closed")
    this.shares.stop()
    this.sharingListeners.clear()
    this.shareAskListeners.clear()
    this.copyListeners.clear()
  }

  // ---------------------------------------------------------------------------------------------- inside
  private platform() {
    return platformOf(this.env.globals, this.camera)
  }

  private sharingChanged(shared: Shared[]): void {
    this.send.json({ type: "sharing", what: shared })
    for (const listener of this.sharingListeners) listener(shared)
    const asked = this.asked
    if (asked && shared.includes(asked.what)) {
      // shared another way meanwhile (the composer's button): what Orion asked for is there
      this.asked = null
      tell(this.shareAskListeners)
      for (const waiter of asked.waiters) waiter.resolve()
    }
  }

  /** The server wants to see the screen or camera: once the user clicks Share (`pendingShare.accept`). */
  private askToShare(what: Shared): Promise<Record<string, unknown>> {
    if (this.shares.has(what)) return Promise.resolve({})
    if (!this.caps().includes(what))
      return Promise.reject(
        new Error(what === "screen" ? "this browser can't share its screen" : "this device has no camera to share"),
      )
    return new Promise((resolve, reject) => {
      if (this.asked && this.asked.what !== what) this.closeAsk(new Error(`Orion asked to see the ${what} instead`))
      const asked = this.asked ?? this.newAsk(what)
      asked.waiters.push({ resolve: () => resolve({}), reject })
    })
  }

  private newAsk(what: Shared): AskedShare {
    const asked: AskedShare = {
      what,
      waiters: [],
      view: {
        what,
        accept: () => {
          if (this.asked !== asked) return Promise.resolve()
          const started = this.shares.start(what) // first, while the click still counts
          this.asked = null
          tell(this.shareAskListeners)
          started.then(
            () => {
              for (const waiter of asked.waiters) waiter.resolve()
            },
            (error: Error) => {
              for (const waiter of asked.waiters) waiter.reject(error)
            },
          )
          return started
        },
        decline: () => {
          if (this.asked !== asked) return
          this.closeAsk(new Error("the user said no"))
        },
      },
    }
    this.asked = asked
    tell(this.shareAskListeners)
    return asked
  }

  private closeAsk(why: Error): void {
    const asked = this.asked
    if (!asked) return
    this.asked = null
    tell(this.shareAskListeners)
    for (const waiter of asked.waiters) waiter.reject(why)
  }
}

function subscribe<T>(set: Set<T>, listener: T): () => void {
  set.add(listener)
  return () => {
    set.delete(listener)
  }
}

function tell(listeners: Set<() => void>): void {
  for (const listener of listeners) listener()
}
