// What this browser prefers, kept in localStorage (the server's settings are his; these are the screen's): how hard
// Horizon may work here, whether his words show as captions, which cards stay pinned on the stage, and
// whether the hands' live view shows the camera's picture. A private window or blocked storage simply starts from the
// defaults every time.
import { create } from "zustand"
import type { CoreQuality } from "@/core"

export interface Prefs {
  quality: CoreQuality
  captions: boolean
  pinned: string[] // the cards pinned on the stage, in order
  handsPicture: boolean // the live view of the hands shows the camera's picture behind them
}

const KEY = "orion-prefs"
const QUALITIES: readonly CoreQuality[] = ["auto", "high", "low", "off"]
const DEFAULTS: Prefs = { quality: "auto", captions: true, pinned: [], handsPicture: false }

function load(): Prefs {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<Prefs>
    return {
      quality: QUALITIES.includes(saved.quality as CoreQuality) ? (saved.quality as CoreQuality) : "auto",
      captions: saved.captions !== false,
      pinned: Array.isArray(saved.pinned) ? saved.pinned.filter((id) => typeof id === "string") : [],
      handsPicture: saved.handsPicture === true,
    }
  } catch {
    return DEFAULTS
  }
}

export const usePrefs = create<Prefs>(() => (typeof window === "undefined" ? DEFAULTS : load()))

/** Change a preference, now and for next time. */
export function setPref<K extends keyof Prefs>(key: K, value: Prefs[K]): void {
  usePrefs.setState({ [key]: value } as Pick<Prefs, K>)
  try {
    localStorage.setItem(KEY, JSON.stringify(usePrefs.getState()))
  } catch {} // (no storage: it lasts until the page reloads)
}
