// What the HUD writes in light above the world's limb: a time, a temperature, a timer's length. The Core writes only a
// short text of its own alphabet (core/protocol.ts: WRITABLE, WRITE_MAX_CHARS) and ignores any other, so these build
// theirs from ASCII digits whatever the browser's language is.
import type { Reminder } from "./types"

const two = (n: number) => String(n).padStart(2, "0")

/**
 * A time of day: 14:05.
 * @param date the time
 * @returns its text (always 24-hour, always ASCII digits)
 */
export const lightTime = (date: Date) => `${two(date.getHours())}:${two(date.getMinutes())}`

/**
 * A temperature: 14°.
 * @param degrees the temperature
 * @returns its text, rounded to a whole degree
 */
export const lightTemp = (degrees: number) => `${Math.round(degrees) || 0}°`

/**
 * A timer's length: 10:00, 1:30, 2 h, 1.5 h.
 * @param timer the timer (when it was set and when it is due, in epoch seconds)
 * @returns its text
 */
export function lightTimer(timer: Pick<Reminder, "due" | "ts">): string {
  const s = Math.max(0, Math.round(timer.due - timer.ts))
  if (s >= 3600) return `${Math.round(s / 360) / 10} h`
  return `${Math.floor(s / 60)}:${two(s % 60)}`
}
