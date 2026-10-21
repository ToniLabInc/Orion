/**
 * Motion tokens: the HUD's durations, curves and springs in one place, for Motion (JS), Anime.js and CSS alike.
 *
 * TypeScript is the source. `applyMotionTokens` writes the durations on `<html>` as `--dur-*` (milliseconds) and the
 * curve as `--ease-expo`, so CSS reads them as `duration-(--dur-quick) ease-(--ease-expo)`. Under reduced motion every
 * duration drops to 0 except `quick`, which stays a 120 ms fade, and the springs become a plain fade. Motion only ever
 * answers a change: at rest, nothing moves but the sky.
 *
 * - instant (90 ms): a press, a hover.
 * - quick (160 ms): a popup, a chip, a fade.
 * - base (240 ms): a card arriving, a list settling.
 * - slow (420 ms): a page, a sheet, the conversation folding.
 * - ambient (1200 ms): his captions, the state label's letters, light changing.
 */
import type { MotionProps, Transition } from "motion/react"

/** Durations in seconds (Motion's unit). */
export const DUR = {
  instant: 0.09,
  quick: 0.16,
  base: 0.24,
  slow: 0.42,
  ambient: 1.2,
} as const

/** The name of a duration token. */
export type DurationName = keyof typeof DUR

/** The reduced-motion set: everything instant except a short fade. */
const REDUCED_DUR: Readonly<Record<DurationName, number>> = {
  instant: 0,
  quick: 0.12,
  base: 0,
  slow: 0,
  ambient: 0,
}

/** out-expo: fast out of the gate, a long soft landing (entrances, moves). */
export const EXPO = [0.16, 1, 0.3, 1] as const

/** The springs: snappy (controls, chips), gentle (cards, layout), float (big, slow things: sheets, the column). */
export const SPRING = {
  snappy: { type: "spring", stiffness: 420, damping: 32 },
  gentle: { type: "spring", stiffness: 180, damping: 24 },
  float: { type: "spring", stiffness: 90, damping: 18 },
} as const satisfies Record<string, Transition>

/** RISE: fade in while rising 8 px into place on a gentle spring; leave with a quick fade. */
export const RISE = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, transition: { duration: DUR.quick } },
  transition: SPRING.gentle,
} as const satisfies MotionProps

/** FOLD: open to the content's height while fading in; close the same way. */
export const FOLD = {
  initial: { opacity: 0, height: 0 },
  animate: { opacity: 1, height: "auto" },
  exit: { opacity: 0, height: 0 },
  transition: { duration: DUR.base, ease: [...EXPO] },
} as const satisfies MotionProps

/** POP: a small thing appearing (a chip, a badge): scale from 0.92 and fade, on the snappy spring. Never on text that
 *  must stay crisp while it reads: the scale ends at 1 in a few frames. */
export const POP = {
  initial: { opacity: 0, scale: 0.92 },
  animate: { opacity: 1, scale: 1 },
  exit: { opacity: 0, scale: 0.96, transition: { duration: DUR.quick } },
  transition: SPRING.snappy,
} as const satisfies MotionProps

/**
 * A stagger for lists that arrive together: each child `step` seconds after the one before.
 * @param step seconds between two children
 * @param delay before the first
 * @returns Motion's transition for the parent
 */
export const stagger = (step = 0.035, delay = 0) => ({ staggerChildren: step, delayChildren: delay })

/** The media query for the user's reduced-motion preference. */
export const REDUCED_MOTION = "(prefers-reduced-motion: reduce)"

let reducedQuery: MediaQueryList | undefined

/**
 * Whether the user asks for reduced motion right now (for code outside React, such as Anime.js timelines and Web
 * Animations; components get the same answer from Motion's `MotionConfig reducedMotion="user"`).
 * @returns true when `prefers-reduced-motion: reduce` matches
 */
export function reducedMotion(): boolean {
  if (typeof matchMedia === "undefined") return false
  reducedQuery ??= matchMedia(REDUCED_MOTION)
  return reducedQuery.matches
}

/**
 * Write the motion tokens as CSS variables: `--dur-<name>` in milliseconds and `--ease-expo`. Call it before the first
 * render and again when the preference changes.
 * @param reduced whether to write the reduced-motion durations
 * @param root the element that receives the variables, `<html>` by default
 */
export function applyMotionTokens(reduced: boolean, root: HTMLElement = document.documentElement): void {
  const durations = reduced ? REDUCED_DUR : DUR
  for (const name of Object.keys(DUR) as DurationName[])
    root.style.setProperty(`--dur-${name}`, `${Math.round(durations[name] * 1000)}ms`)
  root.style.setProperty("--ease-expo", `cubic-bezier(${EXPO.join(", ")})`)
}

/**
 * A duration as it should run now: the token, or its reduced-motion value.
 * @param name the token
 * @returns seconds
 */
export function dur(name: DurationName): number {
  return reducedMotion() ? REDUCED_DUR[name] : DUR[name]
}
