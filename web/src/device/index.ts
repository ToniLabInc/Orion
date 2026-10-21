// This browser as one of Orion's devices (FRONTEND spec §6): its microphone and speakers carry his voice, its
// screen or camera are his eyes, its clipboard is the user's. This file is the contract between the device code
// (src/device/*, src/audio/*, their owner) and the rest of the HUD, which wires it to the live connection.
import { BrowserDevice } from "./link"

export type Cap = "mic" | "speaker" | "screen" | "camera" | "clipboard"
export type Shared = "screen" | "camera"

/** A request the server sends to this tab alone (the `request` event). */
export interface DeviceRequest {
  id: string
  kind: "frame" | "clipboard_read" | "clipboard_write" | "share"
  what?: Shared
  text?: string
}

/** How the link talks to the server: JSON messages and binary frames over /ws. */
export interface DeviceSend {
  json(message: Record<string, unknown>): boolean
  binary(data: ArrayBuffer): boolean
}

export interface DeviceLink {
  /** What this browser offers (for the `hello` message): only what works here (secure context, APIs present). */
  caps(): Cap[]
  /** This device's name for people ("Firefox on Linux", "Safari on iPhone"). */
  name(): string
  /** Carry the voice: open the microphone (asking permission) and the player; mic frames start flowing. */
  takeVoice(): Promise<void>
  /** Stop carrying the voice: close the microphone and the player. */
  releaseVoice(): void
  /** Whether this tab carries the voice now (it took it, and the server agreed). */
  readonly carrying: boolean
  /** The server said who carries the voice (`voice_holder`): this tab or not. */
  holder(isThisTab: boolean): void
  /** Ask to talk now without his name (the talk button). */
  talk(): void
  /** The microphone's level (0..1), ~30 Hz, while carrying the voice. Returns an unsubscribe function. */
  onMicLevel(listener: (value: number) => void): () => void
  /** A binary frame of his voice from the server. */
  playFrame(frame: ArrayBuffer): void
  /** The server stopped his voice: drop what the player holds (`speaker` event). */
  flushSpeaker(generation: number): void
  /** Start sharing the screen or the camera (must follow a user gesture). */
  share(what: Shared): Promise<void>
  /** Stop sharing (one kind, or everything). */
  stopShare(what?: Shared): void
  /** What is shared now. */
  sharing(): Shared[]
  /** Told whenever what is shared changes. Returns an unsubscribe function. */
  onSharing(listener: (shared: Shared[]) => void): () => void
  /** The live stream of what is shared, for a preview. */
  stream(what: Shared): MediaStream | null
  /**
   * Answer a server request. Resolves with the answer's fields ({image}, {text}, {}), or rejects with an Error whose
   * message is said to the user. A `share` request must wait for the user's click: `pendingShare` tells the UI.
   */
  handle(request: DeviceRequest): Promise<Record<string, unknown>>
  /** A share the server asked for, waiting for the user's click (the UI shows Share / Not now), or null. */
  readonly pendingShare: { what: Shared; accept(): Promise<void>; decline(): void } | null
  /** Told whenever `pendingShare` changes. Returns an unsubscribe function. */
  onPendingShare(listener: () => void): () => void
  /**
   * Text the server asked to copy that the browser wouldn't copy without a click (`clipboard_write`), or null: the UI
   * shows it with a "Copy" button whose click calls `copy()` (the request is answered then), or `dismiss()`.
   */
  readonly pendingCopy: { text: string; copy(): Promise<void>; dismiss(): void } | null
  /** Told whenever `pendingCopy` changes. Returns an unsubscribe function. */
  onPendingCopy(listener: () => void): () => void
  dispose(): void
}

/** The link for this browser. */
export function createDeviceLink(send: DeviceSend): DeviceLink {
  return new BrowserDevice(send)
}
