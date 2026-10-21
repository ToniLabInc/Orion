/**
 * Small hooks shared across the HUD: one shared clock per period, an element's width, a value that settles before it
 * is used, whether a key is being typed into a field, and the address's #anchor.
 */
import { useEffect, useRef, useState, useSyncExternalStore } from "react"

/** One timer for every reader of the time at a given period. */
interface Ticker {
  now: Date
  timer: ReturnType<typeof setTimeout> | undefined
  readonly listeners: Set<() => void>
  readonly subscribe: (listener: () => void) => () => void
  readonly read: () => Date
}

const tickers = new Map<number, Ticker>()

/**
 * Wake on the next whole multiple of `every` ms on the wall clock, so every reader ticks in the same frame and the
 * ticks never drift.
 * @param ticker the ticker to advance
 * @param every its period in milliseconds
 */
function schedule(ticker: Ticker, every: number): void {
  ticker.timer = setTimeout(
    () => {
      ticker.now = new Date()
      for (const listener of ticker.listeners) listener()
      if (ticker.listeners.size > 0) schedule(ticker, every)
      else ticker.timer = undefined // the last reader left during this tick
    },
    every - (Date.now() % every),
  )
}

/**
 * The shared ticker for a period, created on first use; it runs only while someone listens.
 * @param period the period in milliseconds (at least one frame, 16 ms)
 * @returns the ticker
 */
function tickerFor(period: number): Ticker {
  const every = Math.max(16, Math.round(period))
  const existing = tickers.get(every)
  if (existing) return existing
  const ticker: Ticker = {
    now: new Date(),
    timer: undefined,
    listeners: new Set(),
    read: () => ticker.now,
    subscribe: (listener) => {
      ticker.listeners.add(listener)
      if (ticker.listeners.size === 1) {
        ticker.now = new Date()
        schedule(ticker, every)
      }
      return () => {
        ticker.listeners.delete(listener)
        if (ticker.listeners.size > 0) return
        clearTimeout(ticker.timer)
        ticker.timer = undefined
      }
    },
  }
  tickers.set(every, ticker)
  return ticker
}

/**
 * Subscribe to nothing (the clock while a reader is inactive).
 * @returns a no-op unsubscribe
 */
const still = (): (() => void) => () => {}

/**
 * The time, re-read every `every` ms while `active`: a clock, a countdown, a live trace's elapsed time. Readers of the
 * same period share one timer and change in the same frame.
 * @param every the period in milliseconds (1000 by default)
 * @param active whether to keep ticking; while false the value stays at the last tick and nothing runs
 * @returns the time of the latest tick
 */
export function useNow(every = 1000, active = true): Date {
  const ticker = tickerFor(every)
  return useSyncExternalStore(active ? ticker.subscribe : still, ticker.read)
}

/**
 * Whether a key press is being typed into a field (an input, a text area, a select or an editable element), so the
 * page's own keys leave it alone.
 * @param target the key event's target
 * @returns the field, or null
 */
export const fieldOf = (target: EventTarget | null) =>
  (target as HTMLElement | null)?.closest?.<HTMLElement>("input, textarea, select, [contenteditable]") ?? null

/**
 * An element's width, followed as it changes.
 * @returns a ref for the element, and its width (0 before it is measured)
 */
export function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const observer = new ResizeObserver(() => setWidth(Math.round(el.clientWidth)))
    observer.observe(el)
    setWidth(Math.round(el.clientWidth))
    return () => observer.disconnect()
  }, [])
  return { ref, width }
}

/**
 * A value once it has stopped changing for `ms` (what you type in a search, before it is asked for).
 * @param value the value
 * @param ms how long it must hold
 * @returns the settled value
 */
export function useSettled<T>(value: T, ms = 250): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return settled
}

const NAVIGATIONS = ["hashchange", "popstate", "pushState", "replaceState"] as const

/**
 * Follow the address' #anchor, however the page was reached (a link, the back button, the palette: the router tells
 * with its own pushState and replaceState events).
 * @param listener what to call when the address changes
 * @returns the function that stops following
 */
function followAddress(listener: () => void): () => void {
  for (const type of NAVIGATIONS) addEventListener(type, listener)
  return () => {
    for (const type of NAVIGATIONS) removeEventListener(type, listener)
  }
}

/**
 * The address' #anchor without its #.
 * @returns the anchor ("" for none)
 */
export function useHash(): string {
  return useSyncExternalStore(
    followAddress,
    () => location.hash.slice(1),
    () => "",
  )
}
