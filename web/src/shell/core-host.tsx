/**
 * Horizon's canvas, mounted once behind everything (the full viewport: the sky, the world's limb where the stage puts
 * its composer, his light), and its wiring, all outside React's renders:
 *
 * - his state (and offline: the canvas dims and loses its colour, CSS), a short red shudder when something fails;
 * - the levels: his voice and the microphone, straight from the live connection, or from this tab's own microphone
 *   while it carries the voice;
 * - the pointer (a whisper of parallax), −1..1 with y down, once a frame at most while it moves;
 * - frames on the stage only: another page keeps the sky still, dimmed behind it (a hidden tab draws nothing: the
 *   engine sees to that);
 * - the moments: a meteor to where a tool's answer lands, memories recalled, a few words written in light (the time
 *   when you ask it, the temperature, a new timer's length, "?" while a question waits, ✓ when you say yes), and rest
 *   after a minute of nobody talking to him.
 */
import { useEffect, useRef } from "react"
import { useLocation } from "wouter"
import type { CoreState } from "@/core"
import { ANSWER_CARDS, asksTheTime, cardFor } from "@/lib/cards"
import { cardTarget, coreActions, coreState, getCore, messageTarget, mountCore, setCoreQuality } from "@/lib/core"
import { onMicLevel, useCarrying } from "@/lib/device"
import { lightTemp, lightTime, lightTimer } from "@/lib/light"
import { reducedMotion } from "@/lib/motion"
import { pageOf } from "@/lib/nav"
import { usePrefs } from "@/lib/prefs"
import { present, useStage } from "@/lib/stage"
import { type Live, useLive } from "@/lib/store"
import { onEvent, onLevel } from "@/lib/ws"

/** A minute of nobody talking to him: he rests. */
const REST_AFTER_MS = 60_000
/** How long a red shudder lasts. */
const SHUDDER_MS = 900
/** How long a word written in light holds, and a ✓. */
const WRITE_MS = 2600
const CHECK_MS = 1600
/** This tab's microphone gives a level without bands. */
const NO_BANDS: readonly number[] = []

/** His state as the Core shows it. */
const shownState = (s: Pick<Live, "state" | "link">): CoreState =>
  s.link !== "online" || s.state === "offline" ? "idle" : s.state

/**
 * The canvas and its wiring.
 * @returns the canvas
 */
export function CoreHost() {
  const canvas = useRef<HTMLCanvasElement>(null)
  const quality = usePrefs((s) => s.quality)
  const carrying = useCarrying()
  const carryingRef = useRef(carrying)
  carryingRef.current = carrying
  const [location] = useLocation()
  const stage = pageOf(location) === "stage"

  // the Core itself, once
  useEffect(() => {
    const el = canvas.current
    if (!el) return
    const dispose = mountCore(el, usePrefs.getState().quality, reducedMotion())
    const offs = wire(() => carryingRef.current)
    return () => {
      for (const off of offs) off()
      dispose()
    }
  }, [])

  useEffect(() => {
    setCoreQuality(quality)
  }, [quality])

  // frames only behind the stage: another page keeps the sky as it was, dimmed
  useEffect(() => {
    getCore()?.visibility(stage)
  }, [stage])

  // The canvas belongs to the engine (a worker draws on it; it may put a fresh one in its place, with the same class):
  // stepping back behind a page, or while the link is down, is its frame's, in CSS.
  return (
    <div
      aria-hidden="true"
      data-dim={!stage || undefined}
      className="pointer-events-none fixed inset-0 z-0 transition-[opacity,filter] duration-(--dur-ambient) data-dim:opacity-30 tab:data-dim:opacity-50 [:root[data-state=offline]_&]:opacity-55 [:root[data-state=offline]_&]:saturate-[0.15]"
    >
      <canvas ref={canvas} className="fixed inset-0 size-full" />
    </div>
  )
}

/**
 * Wire the Core to the live connection and the page (see the module docstring).
 * @param carrying whether this tab carries his voice now
 * @returns the functions that undo it
 */
function wire(carrying: () => boolean): (() => void)[] {
  const offs: (() => void)[] = []
  let shudder = 0

  // his state
  coreState(shownState(useLive.getState()))
  offs.push(
    useLive.subscribe((s, before) => {
      if (s.state === before.state && s.link === before.link) return
      if (!shudder) coreState(shownState(s))
      if (s.state !== "idle" || s.link !== "online") wake()
    }),
  )
  const fail = () => {
    clearTimeout(shudder)
    coreState("error")
    shudder = window.setTimeout(() => {
      shudder = 0
      coreState(shownState(useLive.getState()))
    }, SHUDDER_MS)
  }
  offs.push(() => clearTimeout(shudder))

  // the levels: the microphone from this tab while it carries the voice (its level alone), else from Orion's machine
  offs.push(
    onLevel((ev) => {
      if (ev.source === "mic" && carrying()) return
      getCore()?.level(ev.source, ev.value, ev.bands)
    }),
    onMicLevel((value) => getCore()?.level("mic", value, NO_BANDS)),
  )

  // the pointer, once a frame at most
  let frame = 0
  let px = 0
  let py = 0
  const point = () => {
    frame = 0
    getCore()?.pointer((px / innerWidth) * 2 - 1, (py / innerHeight) * 2 - 1)
  }
  const move = (e: PointerEvent) => {
    px = e.clientX
    py = e.clientY
    if (!frame) frame = requestAnimationFrame(point)
  }
  addEventListener("pointermove", move, { passive: true })
  offs.push(() => {
    removeEventListener("pointermove", move)
    cancelAnimationFrame(frame)
  })

  // rest after a minute of nobody talking to him; anything he or you do wakes him
  let resting = false
  let restTimer = 0
  const arm = () => {
    clearTimeout(restTimer)
    restTimer = window.setTimeout(() => {
      const s = useLive.getState()
      const talking = s.state !== "idle" || s.hearing || s.listenUntil > Date.now() || s.confirms.length > 0
      if (talking || s.messages.some((m) => m.streaming)) return arm()
      resting = true
      coreActions.rest(true)
    }, REST_AFTER_MS)
  }
  function wake() {
    if (resting) {
      resting = false
      coreActions.rest(false)
    }
    arm()
  }
  arm()
  offs.push(() => clearTimeout(restTimer))
  for (const type of ["keydown", "pointerdown"] as const) {
    addEventListener(type, wake, { passive: true, capture: true })
    offs.push(() => removeEventListener(type, wake, { capture: true }))
  }

  // the moments
  let weatherAsked = 0
  let timers = new Set(useLive.getState().reminders.map((r) => r.id))
  offs.push(
    onEvent("wake", wake),
    onEvent("hearing", wake),
    onEvent("user_message", (ev) => {
      wake()
      if (asksTheTime(ev.text)) coreActions.write({ text: lightTime(new Date()), ms: WRITE_MS })
    }),
    onEvent("tool", (ev) => {
      if (ev.phase !== "end" || ev.source === "hud") return
      const card = cardFor(ev.name, ev.server)
      const turn = ev.turn ? messageTarget(ev.turn) : undefined
      if (!ev.ok) {
        fail()
        if (turn) coreActions.meteor(turn, "alert")
        return
      }
      if (ev.name === "get_weather") weatherAsked = Date.now()
      // the answer lands on its card: summoned (it arrives on a meteor of its own), or reached by one
      if (card && ANSWER_CARDS.has(card) && ev.source === "brain") {
        if (useStage.getState().cards.some((c) => c.id === card)) coreActions.meteor(cardTarget(card), "rigel", turn)
        else present(card)
      } else if (turn) coreActions.meteor(turn, "rigel")
    }),
    onEvent("assistant_done", (ev) => {
      if (ev.error) fail()
    }),
    onEvent("recall", (ev) => {
      if (ev.items.length) coreActions.recall(ev.items.length)
    }),
    onEvent("weather", (ev) => {
      if (Date.now() - weatherAsked < 15_000) coreActions.write({ text: lightTemp(ev.weather.temp), ms: WRITE_MS })
      weatherAsked = 0
    }),
    onEvent("reminders", (ev) => {
      const fresh = ev.items.find((r) => r.kind === "timer" && !timers.has(r.id))
      timers = new Set(ev.items.map((r) => r.id))
      if (fresh) coreActions.write({ text: lightTimer(fresh), ms: WRITE_MS })
    }),
    onEvent("confirm", (ev) => {
      wake()
      coreActions.write({ shape: "question", ms: Math.max(1000, ev.expires * 1000 - Date.now()) })
    }),
    onEvent("confirm_done", (ev) => {
      // the "?" goes as the last question is settled: a ✓ for a yes; let go for a no (a release of the "?" leaves
      // whatever is written since alone)
      if (useLive.getState().confirms.length) return
      if (ev.answer === "yes") coreActions.write({ shape: "check", ms: CHECK_MS })
      else coreActions.release("question")
    }),
  )
  return offs
}
