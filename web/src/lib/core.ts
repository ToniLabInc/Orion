/**
 * Horizon on the page: the one Core (src/core, its engine's contract) the HUD mounts behind everything, and how the
 * rest of the HUD reaches it. React never talks to the engine directly: components call `coreActions` (a meteor,
 * memories recalled, a word written in light) and the stage tells it where the world's limb is (`useHorizon`); levels,
 * his state and the pointer go to it from the shell (shell/core-host.tsx), outside React, on events only.
 *
 * Where a meteor lands: a point, or an element that registered as a target (`useMeteorTarget`: a card, a message),
 * measured the moment it flies. Nothing here measures anything at rest.
 *
 * Where the world is: the stage reports its talk button (`useHorizon`); on any other page the place the stage would
 * put it (lib/horizon.ts), followed as the window changes size. The Core draws nothing until it has one.
 */
import { type RefObject, useLayoutEffect } from "react"
import { type Core, type CoreHue, type CoreLayout, type CoreQuality, type CoreState, createCore } from "@/core"
import { defaultLayout } from "./horizon"

let core: Core | null = null

/**
 * Create the Core on its canvas (once: CoreHost does it).
 * @param canvas the full-viewport canvas behind the DOM
 * @param quality the quality tier this screen asks for
 * @param reducedMotion whether the user asks for reduced motion (the engine then draws still frames)
 * @returns the function that disposes of it
 */
export function mountCore(canvas: HTMLCanvasElement, quality: CoreQuality, reducedMotion: boolean): () => void {
  core = createCore(canvas, { quality, reducedMotion })
  core.layout(lastLayout ?? screenLayout())
  let frame = 0
  const resized = () => {
    if (!frame)
      frame = requestAnimationFrame(() => {
        frame = 0
        if (!measured) send(screenLayout()) // (the stage follows its own button)
      })
  }
  addEventListener("resize", resized, { passive: true })
  return () => {
    removeEventListener("resize", resized)
    cancelAnimationFrame(frame)
    core?.dispose()
    core = null
  }
}

/** The quality tier changed (Settings, This screen). */
export const setCoreQuality = (quality: CoreQuality) => core?.quality(quality)

/** Raw access for the shell's wiring (state, levels, pointer, visibility): null before the canvas mounts. */
export const getCore = (): Core | null => core

/** His state for the Core. */
export const coreState = (state: CoreState) => core?.state(state)

// ---------- where things are ----------
/** A point on the page, in CSS px from the viewport's top left. */
export interface Point {
  x: number
  y: number
}

/** The elements a meteor can land on, by id ("card-weather", "msg-<turn>"). */
const targets = new Map<string, HTMLElement>()

/** The target a card registers as. */
export const cardTarget = (id: string) => `card-${id}`
/** The target a turn's message registers as. */
export const messageTarget = (id: string) => `msg-${id}`

/**
 * The middle of a target while it is on screen (its middle kept within the viewport), else null.
 * @param id the target
 * @returns the point
 */
function middleOf(id: string): Point | null {
  const el = targets.get(id)
  if (!el) return null
  const box = el.getBoundingClientRect()
  if (!box.width || !box.height || box.bottom < 0 || box.top > innerHeight) return null
  return { x: box.left + box.width / 2, y: Math.min(Math.max(box.top + box.height / 2, 0), innerHeight) }
}

/**
 * Let meteors land on an element by `id` while it is mounted.
 * @param ref the element
 * @param id its id, unique on the page ("card-weather", "msg-<turn>"), or null for one the sky isn't behind (a card
 *   in a sheet)
 */
export function useMeteorTarget(ref: RefObject<HTMLElement | null>, id: string | null): void {
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || id === null) return
    targets.set(id, el)
    return () => {
      if (targets.get(id) === el) targets.delete(id)
    }
  }, [ref, id])
}

let lastLayout: CoreLayout | null = null
let measured = false // the stage's talk button is mounted, and says where the world is

/** The place the stage would put the world on this screen (a page without a talk button). */
const screenLayout = () =>
  defaultLayout(
    innerWidth,
    innerHeight,
    Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--safe-b")) || 0,
  )

/**
 * Tell the Core a layout, unless it already has it.
 * @param layout where the limb and the talk button are
 */
function send(layout: CoreLayout): void {
  if (
    lastLayout &&
    Math.abs(lastLayout.talk.x - layout.talk.x) < 0.5 &&
    Math.abs(lastLayout.talk.y - layout.talk.y) < 0.5
  )
    return
  lastLayout = layout
  core?.layout(layout)
}

/**
 * Tell the Core where the world's limb is: through the middle of the talk button, which sits on it (your voice plucks
 * the airglow there). Measured as the button mounts, and again when the window or the dock changes size; a button
 * that isn't laid out (the stage hidden behind another page) says nothing, and the last layout stays. When the stage
 * goes, the world is put where the stage would have it on this screen.
 * @param ref the talk button
 */
export function useHorizon(ref: RefObject<HTMLElement | null>): void {
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    let frame = 0
    measured = true
    const report = () => {
      frame = 0
      const box = el.getBoundingClientRect()
      if (!box.width) return
      const talk = { x: box.left + box.width / 2, y: box.top + box.height / 2 }
      send({ horizon: talk.y, talk })
    }
    const later = () => {
      if (!frame) frame = requestAnimationFrame(report)
    }
    report()
    // the composer round it (its line underneath can change height); the window for the rest
    const sizes = new ResizeObserver(later)
    sizes.observe(el.closest("[data-composer]") ?? el)
    addEventListener("resize", later, { passive: true })
    return () => {
      sizes.disconnect()
      removeEventListener("resize", later)
      cancelAnimationFrame(frame)
      measured = false
      send(screenLayout())
    }
  }, [ref])
}

// ---------- the actions components call ----------
export const coreActions = {
  /**
   * A meteor from the limb to a target (by id) or a point: a tool's answer arriving where it shows. A target that
   * isn't on screen sends it to `fallback` if given, else nowhere.
   */
  meteor(to: string | Point, hue: CoreHue, fallback?: string): void {
    if (!core) return
    const point = typeof to === "string" ? (middleOf(to) ?? (fallback ? middleOf(fallback) : null)) : to
    if (point) core.meteor({ to: point, hue })
  },
  /** Memories arriving: a few stars brighten in turn. */
  recall(count: number): void {
    core?.recall(count)
  },
  /** A few words, or a sign, written in light above the limb for `ms` (held until released). */
  write(write: { text?: string; shape?: "question" | "check"; ms: number }): void {
    core?.write(write)
  },
  /** Let a held writing go now: the one with this `shape` (a stale release leaves a newer writing alone). */
  release(shape: "question" | "check"): void {
    core?.write({ shape, ms: 0 })
  },
  /** Rest (nobody has talked to him for a while), or wake. */
  rest(on: boolean): void {
    core?.rest(on)
  },
}
