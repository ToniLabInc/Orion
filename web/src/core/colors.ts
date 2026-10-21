/**
 * Horizon's colours, all from palette.css through lib/palette.ts (the worker parses the same stylesheet), in linear
 * light: the airglow's colour in each state (its family's neon, the 400), a meteor's by hue, the sky and the night
 * side from the void tokens, M42's pink between nebula and betel, the aurora's lower border and its tops. A new
 * palette re-colours the scene with nothing to change here. The stars keep their own colours (sky.ts).
 */
import { rgb } from "@/lib/palette"
import type { CoreHue, CoreState } from "./index"

/** A colour in linear-light sRGB (channels may pass 1 before the exposure). */
export type Lin = [number, number, number]

/** The palette families Horizon draws with. */
export const FAMILIES = ["rigel", "nebula", "aurora", "betel", "dusk", "alert", "ok"] as const
export type Family = (typeof FAMILIES)[number]

const decode = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)

/**
 * A palette token in linear light.
 * @param token a token without the `--color-` prefix
 * @returns the colour, linear sRGB
 * @throws Error when palette.css has no such token
 */
export function linear(token: string): Lin {
  const [r, g, b] = rgb(token)
  return [decode(r), decode(g), decode(b)]
}

/**
 * Linear sRGB to OKLab (Björn Ottosson's matrices).
 * @param c the colour
 * @returns L, a, b
 */
export function toLab([r, g, b]: Lin): Lin {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ]
}

/**
 * OKLab to linear sRGB, negative channels clipped.
 * @param lab L, a, b
 * @returns the colour
 */
export function fromLab([L, a, b]: Lin): Lin {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  return [
    Math.max(0, 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    Math.max(0, -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    Math.max(0, -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ]
}

/**
 * A colour at another lightness, its chroma scaled: its hue kept.
 * @param c the colour
 * @param lightness OKLab L, 0..1
 * @param chroma how much of its chroma to keep
 * @returns the tone
 */
export function tone(c: Lin, lightness: number, chroma: number): Lin {
  const [, a, b] = toLab(c)
  return fromLab([lightness, a * chroma, b * chroma])
}

/**
 * Two colours mixed in OKLab (perceptually even).
 * @param x the colour at t = 0
 * @param y the colour at t = 1
 * @param t how far towards y
 * @returns the mix
 */
export function mixLab(x: Lin, y: Lin, t: number): Lin {
  const p = toLab(x)
  const q = toLab(y)
  return fromLab([p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t])
}

/** A family's neon, linear. */
export const neon = (family: Family): Lin => linear(`${family}-400`)

/** The family each state lights the airglow with: his own at rest, yours while he listens, and so on. */
export const STATE_FAMILY: Readonly<Record<CoreState, Family>> = {
  idle: "rigel",
  listening: "betel",
  thinking: "nebula",
  speaking: "aurora",
  loading: "dusk",
  error: "alert",
}

/** The family each state's glow turns to higher up: the way real airglow goes from green to red with height. */
export const UPPER_FAMILY: Readonly<Record<CoreState, Family>> = {
  idle: "nebula",
  listening: "alert",
  thinking: "rigel",
  speaking: "rigel",
  loading: "betel",
  error: "betel",
}

export interface Palette {
  /** The airglow's colour in each state, and the one it turns to higher up. */
  glow: Record<CoreState, Lin>
  upper: Record<CoreState, Lin>
  /** A meteor's colour by hue. */
  hues: Record<CoreHue, Lin>
  /** The sky's own colour (the page's void), and the night side's (darker: text on it has the most contrast). */
  sky: Lin
  ground: Lin
  /** The Milky Way's faint band. */
  dust: Lin
  /** M42's faint pink. */
  nebula: Lin
  /** The Orion figure's pale lines. */
  figure: Lin
  /** The aurora's lower border and its tops. */
  auroraLow: Lin
  auroraHigh: Lin
}

/**
 * Horizon's palette, from palette.css.
 * @returns every colour the engine draws with
 * @throws Error when palette.css lacks a token Horizon reads
 */
export function makePalette(): Palette {
  const glow = {} as Record<CoreState, Lin>
  const upper = {} as Record<CoreState, Lin>
  for (const [state, family] of Object.entries(STATE_FAMILY) as [CoreState, Family][]) {
    glow[state] = neon(family)
    upper[state] = tone(mixLab(neon(family), neon(UPPER_FAMILY[state]), 0.45), 0.7, 1.1)
  }
  const hues = {} as Record<CoreHue, Lin>
  for (const h of ["rigel", "nebula", "aurora", "betel", "alert", "ok"] as const) hues[h] = tone(neon(h), 0.86, 1.05)
  const nebula = neon("nebula")
  return {
    glow,
    upper,
    hues,
    sky: linear("void-950"),
    ground: linear("black"),
    dust: tone(neon("rigel"), 0.82, 0.35),
    nebula: tone(mixLab(nebula, neon("betel"), 0.35), 0.72, 0.9),
    figure: tone(neon("rigel"), 0.9, 0.45),
    auroraLow: tone(nebula, 0.8, 1.05),
    auroraHigh: tone(mixLab(neon("rigel"), neon("aurora"), 0.5), 0.62, 1.1),
  }
}
