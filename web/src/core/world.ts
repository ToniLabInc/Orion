/**
 * The world's limb on the screen: a circle whose top is at (the screen's middle, `horizon`), its radius RADIUS_WIDTHS
 * times the screen's width, so the arc is the same gentle curve on every screen. Everything that lives on the limb
 * (the airglow's string, the aurora's curtains, a meteor's start, the stars it hides) measures from it here. CSS px,
 * y down. Plain numbers: tested in Node.
 *
 * The centre lies far below the screen (a few thousand pixels), so an altitude is never taken as a difference of two
 * large numbers: `altitude` divides the exact difference of squares instead (the shaders do the same).
 */

/** The limb's radius, in screen widths. */
export const RADIUS_WIDTHS = 3.2
/** How far past each side of the screen the string runs before its fixed ends, CSS px. */
export const STRING_MARGIN = 24

export interface Limb {
  /** The circle's centre and radius, CSS px. */
  cx: number
  cy: number
  r: number
  /** The limb's top, CSS px from the screen's top. */
  horizon: number
  /** The string's arc length and where it starts, from the top along the limb (negative: to the left). */
  length: number
  start: number
}

/**
 * The limb for a screen.
 * @param width the screen's width, CSS px
 * @param horizon the limb's top, CSS px from the screen's top
 * @returns the limb
 */
export function limb(width: number, horizon: number): Limb {
  const r = RADIUS_WIDTHS * Math.max(1, width)
  const half = r * Math.asin(Math.min(1, (width / 2 + STRING_MARGIN) / r))
  return { cx: width / 2, cy: horizon + r, r, horizon, length: 2 * half, start: -half }
}

/**
 * How far the limb falls below its top at a distance across (the sagitta): the curve's drop from the middle of the
 * screen to its edge.
 * @param l the limb
 * @param dx the distance from the screen's middle, CSS px
 * @returns CSS px, ≥ 0
 */
export function sag(l: Limb, dx: number): number {
  return l.r - Math.sqrt(Math.max(0, l.r * l.r - dx * dx))
}

/**
 * A point's height above the limb (negative inside the world), CSS px: (d² − r²) / (d + r), with d² − r² taken
 * exactly as dx² + e(e − 2r), e the point's depth below the limb's top.
 * @param l the limb
 * @param x the point's x
 * @param y its y
 * @returns the altitude
 */
export function altitude(l: Limb, x: number, y: number): number {
  const dx = x - l.cx
  const e = y - l.horizon
  const d = Math.hypot(dx, l.r - e)
  return (dx * dx + e * (e - 2 * l.r)) / (d + l.r)
}

/**
 * The arc length from the limb's top to the point of the limb under a point (along the radius through it).
 * @param l the limb
 * @param x the point's x
 * @param y its y
 * @returns CSS px along the limb, negative to the left
 */
export function arcUnder(l: Limb, x: number, y: number): number {
  return l.r * Math.atan2(x - l.cx, l.cy - y)
}

/**
 * The limb's point at an arc length from its top, and its outward normal.
 * @param l the limb
 * @param s CSS px along the limb from its top
 * @returns x, y, and the normal's nx, ny
 */
export function onLimb(l: Limb, s: number): { x: number; y: number; nx: number; ny: number } {
  const a = s / l.r
  const nx = Math.sin(a)
  const ny = -Math.cos(a)
  return { x: l.cx + l.r * nx, y: l.cy + l.r * ny, nx, ny }
}

/**
 * Where a point along the limb falls on the string, 0 at its left end and 1 at its right.
 * @param l the limb
 * @param s CSS px along the limb from its top
 * @returns the string's coordinate, clamped to 0..1
 */
export function onString(l: Limb, s: number): number {
  return Math.min(1, Math.max(0, (s - l.start) / l.length))
}
