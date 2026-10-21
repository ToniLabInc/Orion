// The hand-gesture camera's live view, and how each gesture looks. The view (a remote's orion://hands) is followed
// only while something on the page shows it: useHandsView() tells Orion so while it is mounted (hands_view), again
// after every reconnect, and asks for the camera's picture while any viewer wants it. Frames come about 15 a second:
// they never pass through React. Whoever draws them listens (onHands in lib/ws.ts), and the latest one of each remote
// is kept here (latestHands) for a view that opens between two frames.
import {
  CircleCheck,
  FoldHorizontal,
  Hand,
  HandFist,
  HandGrab,
  HandMetal,
  type LucideIcon,
  MoveDown,
  MoveLeft,
  MoveRight,
  MoveUp,
  Phone,
  Pointer,
  RotateCcw,
  RotateCw,
  Scissors,
  ThumbsDown,
  ThumbsUp,
  UnfoldHorizontal,
  WavesHorizontal,
  ZoomIn,
  ZoomOut,
} from "lucide-react"
import { useEffect, useState } from "react"
import type { HandsFrame } from "./types"
import { onEvent, onHands, send } from "./ws"

/** Each gesture's icon. */
export const GESTURE_ICONS: Record<string, LucideIcon> = {
  open_palm: Hand,
  closed_fist: HandFist,
  pointing_up: Pointer,
  victory: Scissors,
  three: Hand,
  thumb_up: ThumbsUp,
  thumb_down: ThumbsDown,
  i_love_you: HandMetal,
  call_me: Phone,
  ok_sign: CircleCheck,
  pinch: HandGrab,
  swipe_left: MoveLeft,
  swipe_right: MoveRight,
  swipe_up: MoveUp,
  swipe_down: MoveDown,
  wave: WavesHorizontal,
  circle_cw: RotateCw,
  circle_ccw: RotateCcw,
  push: ZoomIn,
  pull: ZoomOut,
  hands_apart: UnfoldHorizontal,
  hands_together: FoldHorizontal,
}

/**
 * A gesture's name for people when its remote didn't say ("swipe_right" → "Swipe right").
 * @param name the gesture
 * @returns its name in words
 */
export function gestureWords(name: string): string {
  const words = name.replaceAll("_", " ")
  return words.charAt(0).toUpperCase() + words.slice(1)
}

// ---------- who shows the view ----------
const viewers = new Map<symbol, boolean>() // each one showing it: with the picture?
let told: "off" | "on" | "picture" | null = "off" // what Orion was last told (null: tell him again)
let hooked = false

/** Tell Orion what the page shows of the view now, if that changed (and again once he can hear it). */
function tell() {
  const picture = [...viewers.values()].some(Boolean)
  const want = viewers.size === 0 ? "off" : picture ? "picture" : "on"
  if (want === told) return
  if (send({ type: "hands_view", on: want !== "off", picture })) told = want
  else told = null
}

/** The first viewer: from now on, a reconnect tells Orion again, and the latest frames are kept. */
function hook() {
  if (hooked) return
  hooked = true
  onEvent("init", () => {
    told = null // a new connection knows nothing of what this page shows
    tell()
  })
  onHands((ev) => latest.set(ev.server, received(latest.get(ev.server), ev.frame, performance.now())))
}

/**
 * Show the live view while this component is mounted (and the camera's picture too while `picture` is true).
 * @param picture whether to show the camera's picture
 */
export function useHandsView(picture = false): void {
  useEffect(() => {
    hook()
    const key = Symbol("viewer")
    viewers.set(key, picture)
    tell()
    return () => {
      viewers.delete(key)
      tell()
    }
  }, [picture])
}

// ---------- the latest frames ----------
export interface Received {
  frame: HandsFrame
  at: number // performance.now() when it came
}

const latest = new Map<string, Received>()

/**
 * A remote's latest frame once another came: it keeps the picture it had when the new frame has none of its own
 * (Orion sends each picture once), and the frame counts as new only when its number is (the same frame again, its
 * link's state changed, is no news of the camera: the view still goes stale when the camera stops).
 * @param before the frame kept until now
 * @param frame the one that came
 * @param now performance.now()
 * @returns what to keep
 */
export function received(before: Received | undefined, frame: HandsFrame, now: number): Received {
  const keepPicture = !frame.jpg && before?.frame.jpg && frame.jpg_seq === before.frame.jpg_seq
  const kept = keepPicture ? { ...frame, jpg: before?.frame.jpg } : frame
  return { frame: kept, at: before && frame.seq && before.frame.seq === frame.seq ? before.at : now }
}

/**
 * The latest frame of a remote (or of any, when none is named).
 * @param server the remote
 * @returns the frame and when it came, or undefined
 */
export function latestHands(server?: string): (Received & { server: string }) | undefined {
  if (server) {
    const got = latest.get(server)
    return got && { ...got, server }
  }
  let best: (Received & { server: string }) | undefined
  for (const [name, got] of latest) if (!best || got.at > best.at) best = { ...got, server: name }
  return best
}

/** How long a frame stays current: past this the view says it has no signal. */
export const STALE_MS = 2000

/** What the view's header says: the link, the frame rate and the hands, a few times a second. */
export interface HandsStatus {
  state: "waiting" | "live" | "stale" | "down" | "paused" | "off"
  fps: number
  hands: number
  mode: string
  error: string
}

/**
 * The view's state, sampled every `everyMs` (not on every frame).
 * @param server the remote (any, when not named)
 * @param everyMs how often to look
 * @returns the state
 */
export function useHandsStatus(server?: string, everyMs = 500): HandsStatus {
  const [status, setStatus] = useState<HandsStatus>(() => statusOf(server))
  useEffect(() => {
    const look = () =>
      setStatus((before) => {
        const now = statusOf(server)
        return now.state === before.state &&
          now.fps === before.fps &&
          now.hands === before.hands &&
          now.mode === before.mode &&
          now.error === before.error
          ? before
          : now
      })
    look()
    const timer = setInterval(look, everyMs)
    return () => clearInterval(timer)
  }, [server, everyMs])
  return status
}

/**
 * The view's state from its latest frame.
 * @param server the remote (any, when not named)
 * @param now performance.now()
 * @returns the state
 */
export function statusOf(server?: string, now = performance.now()): HandsStatus {
  const got = latestHands(server)
  if (!got) return { state: "waiting", fps: 0, hands: 0, mode: "", error: "" }
  const { frame } = got
  const link = frame.link
  const base = { fps: Math.round(frame.fps ?? 0), hands: frame.hands.length, mode: frame.mode ?? "", error: "" }
  if (link && !link.connected) return { ...base, state: "down", hands: 0, error: link.error }
  if (link?.paused) return { ...base, state: "paused", hands: 0 }
  if (now - got.at > STALE_MS) return { ...base, state: frame.seq ? "stale" : "waiting", hands: 0 }
  return { ...base, state: frame.seq ? "live" : "waiting" }
}
