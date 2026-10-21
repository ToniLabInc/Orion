// What the stage shows besides Horizon: the cards summoned (by voice, a tool's answer, the palette, a chip in the top bar)
// and whether each is pinned or transient (a transient card leaves after TRANSIENT_MS unless the pointer rests on it);
// whether the conversation is open or folded to his captions (ambient); and which sheet is up on a small screen.
// Pinned cards are this browser's preference (lib/prefs.ts) and come back after a reload; the rest starts clean.
import { navigate } from "wouter/use-browser-location"
import { create } from "zustand"
import { pageOf } from "./nav"
import { setPref, usePrefs } from "./prefs"
import type { SummonAction } from "./types"

export const CARDS = [
  "today",
  "weather",
  "vitals",
  "calendar",
  "timers",
  "tasks",
  "news",
  "jobs",
  "hands",
  "eyes",
  "devices",
] as const
export type CardId = (typeof CARDS)[number]
export const isCard = (part: string): part is CardId => (CARDS as readonly string[]).includes(part)

/** How long a transient card stays (the pointer or the focus on it holds it). */
export const TRANSIENT_MS = 90_000

/** A card on the stage. */
export interface Card {
  id: CardId
  pinned: boolean
  at: number // when it was summoned (ms): the newest is dismissed first
  comet: boolean // it arrives with a meteor from the limb (not a pinned card coming back with the page)
}

export type Sheet = "cards" | null

interface Stage {
  cards: Card[] // newest first
  open: boolean // the conversation is open (else: his captions on the sky)
  sheet: Sheet // the phone's and the tablet's sheets
  palette: boolean // the command palette
}

const pinnedAtStart = (): Card[] =>
  usePrefs
    .getState()
    .pinned.filter(isCard)
    .map((id, i) => ({ id, pinned: true, at: i, comet: false }))

export const useStage = create<Stage>(() => ({ cards: pinnedAtStart(), open: false, sheet: null, palette: false }))

/** Keep the pinned cards in this browser's preferences. */
function savePins(cards: Card[]): void {
  setPref(
    "pinned",
    cards.filter((c) => c.pinned).map((c) => c.id),
  )
}

/** Back to the stage from another page. */
const toStage = () => {
  if (pageOf(location.pathname) !== "stage") navigate("/")
}

/**
 * Summon a card (to the top if it is there already).
 * @param id the card
 * @param options `pin` it, `go` to the stage if another page shows
 */
export function present(id: CardId, options: { pin?: boolean; go?: boolean } = {}): void {
  useStage.setState((s) => {
    const before = s.cards.find((c) => c.id === id)
    const card: Card = { id, pinned: options.pin ?? before?.pinned ?? false, at: Date.now(), comet: true }
    const cards = [card, ...s.cards.filter((c) => c.id !== id)]
    if (options.pin !== undefined) savePins(cards)
    return { cards }
  })
  if (options.go) toStage()
}

/** Send cards away (all of them when none is named). */
export function dismiss(...ids: CardId[]): void {
  useStage.setState((s) => {
    const cards = ids.length ? s.cards.filter((c) => !ids.includes(c.id)) : []
    if (s.cards.some((c) => c.pinned && !cards.includes(c))) savePins(cards)
    return { cards }
  })
}

/** Esc: the newest card goes. @returns whether one did */
export function dismissNewest(): boolean {
  const newest = useStage.getState().cards.toSorted((a, b) => b.at - a.at)[0]
  if (!newest) return false
  dismiss(newest.id)
  return true
}

/** Pin a card (it stays, also after a reload) or let it go transient. */
export function pin(id: CardId, on: boolean): void {
  useStage.setState((s) => {
    const cards = s.cards.map((c) => (c.id === id ? { ...c, pinned: on, at: on ? c.at : Date.now() } : c))
    savePins(cards)
    return { cards }
  })
}

/** Open the conversation (you typed, scrolled up, or asked), or fold it to his captions. */
export const setOpen = (open: boolean) => useStage.setState({ open })
export const setSheet = (sheet: Sheet) => useStage.setState({ sheet })
export const setPalette = (palette: boolean) => useStage.setState({ palette })

/** The old names of the conversation, as his summon events and gestures say them. */
const CONVERSATION = new Set(["comms", "conversation", "chat"])
/** The pages a summon can name. */
const PAGES: Record<string, string> = {
  neural: "/memory",
  memory: "/memory",
  automations: "/automations",
  jobs_page: "/automations",
  activity: "/activity",
  customize: "/customize/connectors",
  settings: "/settings",
}

/**
 * Whether a part of the HUD is on the screen now: a card on the stage, the conversation, or a page (or a settings tab).
 * @param part a card, "comms", "neural", "settings" or "settings/<tab>"
 * @returns whether it shows
 */
export function isShown(part: string): boolean {
  const here = pageOf(location.pathname)
  if (isCard(part)) return here === "stage" && useStage.getState().cards.some((c) => c.id === part)
  if (CONVERSATION.has(part)) return here === "stage" && useStage.getState().open
  if (part.startsWith("settings/")) return location.pathname === `/${part}`
  const page = PAGES[part]
  return page !== undefined && location.pathname.startsWith(page)
}

/**
 * A "summon" event: what he was asked to show or hide (by voice, or a gesture). Parts are cards, the conversation
 * ("comms"), a page ("neural", "automations", "settings") or "settings/<tab>". A toggle shows its part if it isn't on
 * the screen and hides it if it is; dismiss sends the newest card away (from a page, it goes back to the stage).
 * @param how "show", "hide", "toggle" or "dismiss"
 * @param asked what, in order (to hide, none means every card)
 */
export function summoned(how: SummonAction, asked: readonly string[]): void {
  const here = pageOf(location.pathname)
  if (how === "dismiss") {
    if (here !== "stage") navigate("/")
    else dismissNewest()
    return
  }
  const action = how === "toggle" ? (asked[0] && isShown(asked[0]) ? "hide" : "show") : how
  const cards = asked.filter(isCard)
  const talk = asked.some((part) => CONVERSATION.has(part))
  const page = asked.find((part) => part.startsWith("settings/") || part in PAGES)
  if (action === "show") {
    for (const id of cards.toReversed()) present(id)
    if (talk) setOpen(true)
    if (page) navigate(page.startsWith("settings/") ? `/${page}` : (PAGES[page] ?? "/"))
    else if (cards.length || talk) toStage()
    return
  }
  if (!asked.length) dismiss()
  if (cards.length) dismiss(...cards)
  if (talk) setOpen(false)
  if (page && here !== "stage") navigate("/")
}
