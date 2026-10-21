/**
 * This browser as a device, for the UI: what it shares, what Orion asked that waits for your click (a share; text to
 * copy that the browser wouldn't copy on its own), and the actions behind the talk button and the share buttons. The
 * link itself lives in lib/ws.ts (`deviceLink`), which speaks to the server for it; who carries his voice is the
 * server's word (`voiceHolder` in the store, compared with this tab's `surface`), so the UI never guesses. The
 * microphone's level never enters React: `onMicLevel` hands it on.
 */
import { create } from "zustand"
import type { Shared } from "@/device"
import { toast } from "@/ui/toast"
import { useLive } from "./store"
import { deviceLink } from "./ws"

interface DeviceState {
  sharing: Shared[] // what this tab shares now
  pendingShare: { what: Shared; accept(): Promise<void>; decline(): void } | null // asked by Orion, waiting
  pendingCopy: { text: string; copy(): Promise<void>; dismiss(): void } | null // text to copy, waiting for a click
  taking: boolean // the voice is being taken (the microphone's permission, the player)
}

export const useDevice = create<DeviceState>(() => ({
  sharing: [],
  pendingShare: null,
  pendingCopy: null,
  taking: false,
}))

let hooked = false

/** Follow the link's sharing, its pending share and its pending copy (once; the app does it as it starts); let it go
 *  as the page goes away. */
export function hookDevice(): void {
  if (hooked) return
  hooked = true
  const link = deviceLink()
  addEventListener("pagehide", (e) => {
    if (!e.persisted) link.dispose() // (a page kept in the back-forward cache keeps its link)
  })
  useDevice.setState({ sharing: link.sharing(), pendingShare: link.pendingShare, pendingCopy: link.pendingCopy })
  link.onSharing((sharing) => useDevice.setState({ sharing }))
  link.onPendingShare(() => useDevice.setState({ pendingShare: deviceLink().pendingShare }))
  link.onPendingCopy(() => useDevice.setState({ pendingCopy: deviceLink().pendingCopy }))
}

/** Whether this tab carries his voice (the server says so). */
export const useCarrying = () => useLive((s) => Boolean(s.surface) && s.voiceHolder.device === s.surface)

/** What this browser offers, and why it can't talk when it can't. */
export function voiceSupport(): { ok: boolean; why: string } {
  if (typeof window !== "undefined" && !window.isSecureContext)
    return { ok: false, why: "Open the HUD over HTTPS (or localhost) to talk from this device" }
  if (!deviceLink().caps().includes("mic")) return { ok: false, why: "This browser can't carry his voice" }
  return { ok: true, why: "" }
}

/**
 * Say why something the device was asked failed, in a toast (not when you called it off yourself: an AbortError).
 * @param title the toast's title
 * @param error what failed
 */
function failed(title: string, error: unknown): void {
  if (error instanceof Error && error.name === "AbortError") return
  toast.add({ title, description: error instanceof Error ? error.message : String(error), type: "error" })
}

/**
 * Say why this device can't talk (a press on the talk button where it can't).
 * @param why the reason
 */
export function explainVoice(why: string): void {
  toast.add({ title: "Not from this device", description: `${why}.`, type: "info" })
}

/**
 * Carry his voice on this device: the microphone's permission is asked now. Call it inside the click (nothing awaited
 * before it): a phone opens its audio only in the gesture.
 */
export function takeVoice(): Promise<void> {
  useDevice.setState({ taking: true })
  return deviceLink()
    .takeVoice()
    .catch((error: unknown) => failed("The voice stays where it is", error))
    .finally(() => useDevice.setState({ taking: false }))
}

/** The talk button's press: take the voice if this tab doesn't carry it yet, and ask him to listen now. */
export function pressTalk(): void {
  const link = deviceLink()
  if (!link.carrying) void takeVoice()
  link.talk()
}

/** Give his voice back (to Orion's own machine). */
export const releaseVoice = () => deviceLink().releaseVoice()

/** Talk now, without his name. */
export const talk = () => deviceLink().talk()

/**
 * Share the screen or the camera (after a click: browsers ask the user).
 * @param what the screen or the camera
 */
export async function share(what: Shared): Promise<void> {
  try {
    await deviceLink().share(what)
  } catch (error) {
    failed(what === "screen" ? "Screen not shared" : "Camera not shared", error)
  }
}

/** Stop sharing one thing, or everything. */
export const stopShare = (what?: Shared) => deviceLink().stopShare(what)

/** Listen to the microphone's level (0..1, ~30 a second) while this tab carries the voice; returns the unsubscribe. */
export const onMicLevel = (listener: (value: number) => void) => deviceLink().onMicLevel(listener)

/** The live stream of what is shared, for a preview. */
export const sharedStream = (what: Shared) => deviceLink().stream(what)
