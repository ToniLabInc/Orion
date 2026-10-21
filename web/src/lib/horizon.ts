// Where the world's limb is on a page that has no talk button to measure (the settings, Customize, the activity log):
// the place the stage would put it, worked out from the screen alone, so the sky is already there on a direct visit and
// doesn't jump when you go to the stage. It repeats the stage's geometry (stage/index.tsx: `--world`, the dock under
// the conversation, the talk button 44 px, or 64 px on a phone, above its 24 px line), as measured at nine screen
// sizes (horizon.test.ts); the stage itself, once mounted, reports the real place.
import type { CoreLayout } from "@/core"

const REM = 16
const TABLET = 43.75 * REM // the stage's breakpoints
const DESKTOP = 68.75 * REM
const WORLD = { min: 1 * REM, share: 0.07, max: 5.5 * REM } // --world: clamp(1rem, 7dvh, 5.5rem), the space under the stage
const UNDER_BUTTON = 32 // the composer's status line, its gap, and the button's margin below it
const BUTTON = { phone: 64, other: 44 }

/**
 * Where the stage's talk button is on a screen of this size, and with it the horizon.
 * @param width the viewport's width in px
 * @param height the viewport's height in px
 * @param safeBottom the safe area under a phone's screen (a notch's bar), in px
 * @returns the layout the Core is told
 */
export function defaultLayout(width: number, height: number, safeBottom = 0): CoreLayout {
  const phone = width < TABLET
  const world = Math.min(WORLD.max, Math.max(WORLD.min, height * WORLD.share))
  const size = phone ? BUTTON.phone : BUTTON.other
  const y = height - world - (phone ? safeBottom : 0) - UNDER_BUTTON - size / 2
  return { horizon: y, talk: { x: talkLeft(width, phone) + size / 2, y } }
}

/**
 * Where the talk button's left edge is: at the left of the composer, which sits in the middle column of the desktop's
 * grid (its dock reaches 20 px outside it), in the middle one of the tablet's, or at the phone's edge.
 * @param width the viewport's width in px
 * @param phone whether it is a phone
 * @returns the x in px
 */
function talkLeft(width: number, phone: boolean): number {
  if (phone) return 12 // px-3
  if (width < DESKTOP) {
    // [3.5rem | the conversation | 3.5rem] between 1rem margins and 1rem gaps; the dock is at most 760 wide, centred
    const middle = width - 2 * 16 - 2 * 56 - 2 * 16
    return 16 + 56 + 16 + (middle - Math.min(760, middle)) / 2
  }
  // [1fr | 720 | at least 380] between 1.5rem margins and 1.5rem gaps, the grid at most 1880 wide, centred
  const grid = Math.min(width, 1880)
  const free = grid - 2 * 24 - 2 * 24 - 720
  const left = Math.max(0, free - Math.max(380, free / 2)) // the first column: what the third one doesn't take
  return (width - grid) / 2 + 24 + left + 24 - 20
}
