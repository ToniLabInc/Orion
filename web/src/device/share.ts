// His eyes on this device: the screen (getDisplayMedia) or the camera (getUserMedia), each kept playing in a hidden
// <video>; a frame is the video's current picture drawn to a canvas, its longest side at most 1600 px, as a JPEG
// data URL. The browser's own "Stop sharing" ends the stream, and whoever listens is told.
import { messageOf, nameOf } from "@/audio/errors"
import type { Shared } from "./index"

/** The longest side of a frame sent (the server shrinks it further to its own `eyes.image_max_side`). */
export const FRAME_MAX_SIDE = 1600
export const FRAME_QUALITY = 0.85
/** How long a frame waits for a share that just started to show its first picture. */
export const FRAME_WAIT_MS = 3000

const KINDS: readonly Shared[] = ["screen", "camera"]

/** What sharing needs from the browser (the real one by default; a test hands in its own). */
export interface ShareEnv {
  mediaDevices(): MediaDevices | undefined
  video(): HTMLVideoElement
  canvas(): HTMLCanvasElement
}

export const browserShareEnv: ShareEnv = {
  mediaDevices: () => (typeof navigator === "undefined" ? undefined : navigator.mediaDevices),
  video: () => document.createElement("video"),
  canvas: () => document.createElement("canvas"),
}

interface Live {
  stream: MediaStream
  video: HTMLVideoElement
}

export class Shares {
  private readonly env: ShareEnv
  private readonly changed: (shared: Shared[]) => void
  private readonly live = new Map<Shared, Live>()
  private readonly starting = new Map<Shared, Promise<void>>()
  private canvas: HTMLCanvasElement | null = null

  /**
   * @param changed - told what is shared after every change
   * @param env - the browser
   */
  constructor(changed: (shared: Shared[]) => void, env: ShareEnv = browserShareEnv) {
    this.changed = changed
    this.env = env
  }

  /** What is shared now. */
  list(): Shared[] {
    return KINDS.filter((what) => this.live.has(what))
  }

  has(what: Shared): boolean {
    return this.live.has(what)
  }

  stream(what: Shared): MediaStream | null {
    return this.live.get(what)?.stream ?? null
  }

  /**
   * Start sharing (call it inside the user's click: the browser asks for the screen synchronously).
   * @param what - the screen or the camera
   * @returns resolves once it is shared; rejects with words for the user
   */
  start(what: Shared): Promise<void> {
    if (this.live.has(what)) return Promise.resolve()
    const already = this.starting.get(what)
    if (already) return already
    const media = this.env.mediaDevices()
    let asked: Promise<MediaStream>
    try {
      if (what === "screen") {
        if (typeof media?.getDisplayMedia !== "function") throw new ShareError("This browser can't share its screen.")
        asked = media.getDisplayMedia({ video: true, audio: false })
      } else {
        if (typeof media?.getUserMedia !== "function") throw new ShareError("This browser can't open a camera.")
        asked = media.getUserMedia({ video: { facingMode: "user" }, audio: false })
      }
    } catch (error) {
      return Promise.reject(shareError(what, error))
    }
    const started = asked
      .then(
        (stream) => this.keep(what, stream),
        (error: unknown) => {
          throw shareError(what, error)
        },
      )
      .finally(() => this.starting.delete(what))
    this.starting.set(what, started)
    return started
  }

  /**
   * Stop sharing one thing, or everything.
   * @param what - the screen or the camera (both when left out)
   */
  stop(what?: Shared): void {
    let any = false
    for (const kind of what ? [what] : KINDS) {
      const live = this.live.get(kind)
      if (!live) continue
      this.live.delete(kind)
      for (const track of live.stream.getTracks()) track.stop()
      live.video.pause()
      live.video.srcObject = null
      any = true
    }
    if (any) this.changed(this.list())
  }

  /**
   * The current picture of what is shared.
   * @param what - the screen or the camera
   * @returns a JPEG data URL, its longest side at most FRAME_MAX_SIDE
   */
  async frame(what: Shared): Promise<string> {
    const live = this.live.get(what)
    if (!live) throw new Error(`the ${what} isn't shared`)
    const { video } = live
    if (video.paused) void video.play().catch(() => {})
    await firstPicture(video, what)
    const width = video.videoWidth
    const height = video.videoHeight
    const scale = Math.min(1, FRAME_MAX_SIDE / Math.max(width, height))
    this.canvas ??= this.env.canvas()
    const canvas = this.canvas
    canvas.width = Math.max(1, Math.round(width * scale))
    canvas.height = Math.max(1, Math.round(height * scale))
    const g = canvas.getContext("2d")
    if (!g) throw new Error(`the ${what} couldn't be drawn here`)
    g.imageSmoothingEnabled = true
    g.imageSmoothingQuality = "high"
    g.drawImage(video, 0, 0, canvas.width, canvas.height)
    const url = canvas.toDataURL("image/jpeg", FRAME_QUALITY)
    canvas.width = canvas.height = 1 // let the picture's memory go
    return url
  }

  private keep(what: Shared, stream: MediaStream): void {
    const video = this.env.video()
    video.muted = true
    video.playsInline = true
    video.autoplay = true
    video.srcObject = stream
    const [track] = stream.getVideoTracks()
    track?.addEventListener("ended", () => {
      if (this.live.get(what)?.stream === stream) this.stop(what) // the browser's own "Stop sharing"
    })
    this.live.set(what, { stream, video })
    this.changed(this.list())
    void video.play().catch(() => {}) // a frame waits for its first picture (and starts it again if held back)
  }
}

class ShareError extends Error {}

/** Starting to share failed: why, for the user (and for Orion, when he asked). */
export function shareError(what: Shared, error: unknown): Error {
  if (error instanceof ShareError) return new Error(error.message)
  const message = messageOf(error)
  switch (nameOf(error)) {
    case "NotAllowedError":
    case "PermissionDeniedError":
    case "SecurityError":
      if (/system/i.test(message))
        return new Error(
          `The system doesn't let this browser record the ${what}: allow it in the system's privacy settings.`,
        )
      return new Error(
        what === "screen"
          ? "The screen wasn't shared (the browser's picker was closed, or sharing is blocked for this site)."
          : "The camera is blocked for this site: allow it in the browser's address bar.",
      )
    case "InvalidStateError":
      return new Error(`Sharing the ${what} has to start from a click.`)
    case "NotFoundError":
    case "DevicesNotFoundError":
    case "OverconstrainedError":
      return new Error(what === "screen" ? "There is no screen to share." : "No camera was found on this device.")
    case "NotReadableError":
    case "TrackStartError":
    case "AbortError":
      return new Error(`The ${what} couldn't start: another app may be using it.`)
    case "NotSupportedError":
    case "TypeError":
      return new Error(`This browser can't share its ${what}.`)
    default:
      return new Error(`The ${what} couldn't be shared: ${message}`)
  }
}

/** Wait (a little) for a video that just started to have a picture. */
function firstPicture(video: HTMLVideoElement, what: Shared): Promise<void> {
  const ready = () => video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0
  if (ready()) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const check = () => {
      if (!ready()) return
      done()
      resolve()
    }
    const timer = setTimeout(() => {
      done()
      reject(new Error(`the ${what} shows nothing yet: try again in a moment`))
    }, FRAME_WAIT_MS)
    const done = () => {
      clearTimeout(timer)
      for (const type of ["loadeddata", "resize", "playing"]) video.removeEventListener(type, check)
    }
    for (const type of ["loadeddata", "resize", "playing"]) video.addEventListener(type, check)
  })
}
