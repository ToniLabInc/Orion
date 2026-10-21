/**
 * The frame round every page: Horizon behind everything, the top bar, and what floats over the pages (his questions
 * before a risky action, the gesture chip, the command palette, the link's banner, the boot, the HUD's own questions).
 * It keeps <html data-state> (CSS tints by it) and the tab's title with his state, <html data-hidden> while the tab is
 * hidden (CSS pauses), passes his "summon" events to the stage, sounds the cards' comings and goings, and owns ⌘K.
 */
import { lazy, type ReactNode, Suspense, useEffect, useState } from "react"
import { hookDevice } from "@/lib/device"
import { play } from "@/lib/sound"
import { setPalette, summoned, useStage } from "@/lib/stage"
import { useLive } from "@/lib/store"
import { onEvent } from "@/lib/ws"
import { Confirmation } from "@/stage/confirmation"
import { Requests } from "@/stage/sharing"
import { AskHost } from "@/ui/ask"
import { Boot } from "./boot"
import { CoreHost } from "./core-host"
import { LinkStatus } from "./link"
import { stateWords } from "./state-label"
import { TopBar } from "./top-bar"

/** The gesture chip: its own chunk (its icons stay out of the first load). */
const GestureChip = lazy(() => import("./gesture").then((m) => ({ default: m.GestureChip })))

/**
 * The command palette: its own chunk (its commands and its connector icons stay out of the first load). The chunk is
 * fetched once the HUD has settled, so the first ⌘K still opens it at once.
 */
const Palette = lazy(() => import("./palette").then((m) => ({ default: m.Palette })))

/** How long after the HUD starts the palette's chunk is fetched, in ms. */
const PALETTE_AFTER_MS = 3000

/**
 * Mounts the palette the first time it is asked for.
 * @returns the palette once wanted, else nothing
 */
function LatePalette() {
  const wanted = useStage((s) => s.palette)
  const [asked, setAsked] = useState(false)
  useEffect(() => {
    if (wanted) setAsked(true)
  }, [wanted])
  useEffect(() => {
    const id = setTimeout(() => void import("./palette"), PALETTE_AFTER_MS)
    return () => clearTimeout(id)
  }, [])
  return asked ? (
    <Suspense fallback={null}>
      <Palette />
    </Suspense>
  ) : null
}

/** His state on <html data-state> and in the tab's title; whether the tab is hidden on <html data-hidden>. */
function useDocumentState() {
  const state = useLive((s) => (s.link === "online" ? s.state : "offline"))
  const words = useLive((s) => stateWords(s))
  useEffect(() => {
    document.documentElement.dataset.state = state
  }, [state])
  useEffect(() => {
    document.title = state === "idle" ? "Orion" : `Orion · ${words}`
  }, [state, words])
  useEffect(() => {
    const root = document.documentElement
    const hidden = () => root.toggleAttribute("data-hidden", document.hidden)
    hidden()
    document.addEventListener("visibilitychange", hidden)
    return () => document.removeEventListener("visibilitychange", hidden)
  }, [])
}

/** The cards' sounds: one arriving, one leaving. */
function useSounds() {
  useEffect(
    () =>
      useStage.subscribe((now, before) => {
        if (now.cards === before.cards) return
        const ids = (cards: typeof now.cards) => new Set(cards.map((c) => c.id))
        const was = ids(before.cards)
        const is = ids(now.cards)
        if ([...is].some((id) => !was.has(id))) play("summon")
        else if ([...was].some((id) => !is.has(id))) play("dismiss")
      }),
    [],
  )
}

/** ⌘K (Ctrl+K) opens and closes the palette anywhere, a field included. */
function usePaletteKey() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== "k" || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return
      e.preventDefault()
      setPalette(!useStage.getState().palette)
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  }, [])
}

/**
 * The frame.
 * @param props the pages (`children`)
 * @returns the frame round them
 */
export function Shell({ children }: { children: ReactNode }) {
  useDocumentState()
  useSounds()
  usePaletteKey()
  useEffect(() => hookDevice(), [])
  useEffect(() => onEvent("summon", (ev) => summoned(ev.action, ev.panels)), [])
  return (
    <>
      <CoreHost />
      <main className="fixed inset-0 z-10" data-testid="page">
        {children}
      </main>
      <TopBar />
      <Confirmation />
      <Requests />
      <Suspense fallback={null}>
        <GestureChip />
      </Suspense>
      <LinkStatus />
      <LatePalette />
      <Boot />
      <AskHost />
    </>
  )
}
