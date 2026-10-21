/**
 * The palette for code that can't read CSS: the canvases (the neural map's three, the hands' live view, a picture
 * flattened to JPEG) and the colours code picks or mixes for an inline style.
 *
 * It parses `palette.css`, the one place the HUD defines a colour (Tailwind reads the same file), so a canvas and the
 * page can never drift apart. OKLCH tokens become sRGB through OKLab with Björn Ottosson's matrices, each channel
 * clamped the way a browser clips an out-of-gamut colour. Nothing is imported at run time besides the raw stylesheet,
 * so a worker can bundle this module too; the stylesheet is parsed on first use, not at import.
 */
import source from "../palette.css?raw"
import type { AssistantState } from "./types"

/** A colour in gamma-encoded sRGB, each channel from 0 to 1. */
export type Rgb = readonly [r: number, g: number, b: number]

/** A colour in OKLab: lightness, then the green–red and the blue–yellow axes. */
export type Lab = readonly [l: number, a: number, b: number]

/** His state as the HUD shows it: the assistant's own state, or offline while the link is down. */
type Shown = AssistantState | "offline"

/** The tokens and state tints read from a palette stylesheet. */
export interface Palette {
  /** Every `--color-*` token in OKLab, keyed by its name without the prefix (`rigel-500`, `warn`). */
  readonly labs: ReadonlyMap<string, Lab>
  /** The colour token each state tints the page with, keyed by state; `idle` is the `:root` default. */
  readonly tints: ReadonlyMap<string, string>
}

const COMMENT = /\/\*[\s\S]*?\*\//g
const COLOR = /--color-([a-z]+(?:-\d+)?):\s*oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)/g
const ALIAS = /--color-([a-z]+(?:-\d+)?):\s*var\(--color-([a-z]+(?:-\d+)?)\)/g
const TINT = /\[data-state="(\w+)"\]\s*\{\s*--state:\s*var\(--color-([a-z]+(?:-\d+)?)\)/g
const IDLE = /:root\s*\{\s*--state:\s*var\(--color-([a-z]+(?:-\d+)?)\)/

/**
 * Convert OKLCH to OKLab (a = C·cos h, b = C·sin h).
 * @param l lightness, 0 to 1
 * @param c chroma
 * @param h hue in degrees
 * @returns the same colour in OKLab
 */
function oklab(l: number, c: number, h: number): Lab {
  const radians = (h * Math.PI) / 180
  return [l, c * Math.cos(radians), c * Math.sin(radians)]
}

/**
 * Gamma-encode one linear-light sRGB channel and clamp it.
 * @param linear the channel in linear light (out-of-gamut values may fall outside 0..1)
 * @returns the encoded channel, 0 to 1
 */
function encode(linear: number): number {
  const value = linear <= 0.0031308 ? 12.92 * linear : 1.055 * linear ** (1 / 2.4) - 0.055
  return Math.min(1, Math.max(0, value))
}

/**
 * Convert OKLab to gamma-encoded sRGB with Björn Ottosson's matrices, clamping each channel.
 * @param lab the colour in OKLab
 * @returns the colour in sRGB
 */
function srgb([L, a, b]: Lab): Rgb {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  return [
    encode(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    encode(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    encode(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ]
}

/**
 * Read the colour tokens and the state tints of a palette stylesheet. Comments are ignored; a `var()` alias resolves to
 * the token it names when that token appears earlier.
 * @param css the stylesheet's text (palette.css)
 * @returns the tokens in OKLab and the tint token of each state
 */
export function parsePalette(css: string): Palette {
  const text = css.replace(COMMENT, "")
  const labs = new Map<string, Lab>()
  for (const [, name, l, c, h] of text.matchAll(COLOR)) labs.set(name, oklab(Number(l), Number(c), Number(h)))
  for (const [, name, target] of text.matchAll(ALIAS)) {
    const lab = labs.get(target)
    if (lab) labs.set(name, lab)
  }
  const tints = new Map<string, string>()
  const idle = IDLE.exec(text)
  if (idle) tints.set("idle", idle[1])
  for (const [, state, token] of text.matchAll(TINT)) tints.set(state, token)
  return { labs, tints }
}

let parsed: Palette | undefined
const converted = new Map<string, Rgb>()

/**
 * The HUD's palette, parsed from palette.css the first time it is needed.
 * @returns the parsed palette
 */
function palette(): Palette {
  parsed ??= parsePalette(source)
  return parsed
}

/**
 * Look a token up in OKLab.
 * @param token a palette token without the `--color-` prefix
 * @returns the token in OKLab
 * @throws Error when palette.css has no such token
 */
function labOf(token: string): Lab {
  const lab = palette().labs.get(token)
  if (!lab) throw new Error(`palette.css has no --color-${token}`)
  return lab
}

/**
 * Format a byte as two lowercase hex digits.
 * @param channel an sRGB channel, 0 to 1
 * @returns the channel as two hex digits
 */
function byte(channel: number): string {
  return Math.round(channel * 255)
    .toString(16)
    .padStart(2, "0")
}

/**
 * Format an sRGB colour as a CSS hex string.
 * @param color the colour in sRGB
 * @returns `#rrggbb`
 */
function toHex([r, g, b]: Rgb): string {
  return `#${byte(r)}${byte(g)}${byte(b)}`
}

/**
 * A palette token in sRGB, converted once and cached.
 * @param token a palette token without the `--color-` prefix, for example `rigel-500`
 * @returns the token in gamma-encoded sRGB
 * @throws Error when palette.css has no such token
 */
export function rgb(token: string): Rgb {
  let color = converted.get(token)
  if (!color) {
    color = srgb(labOf(token))
    converted.set(token, color)
  }
  return color
}

/**
 * A palette token as a CSS hex string, for libraries that don't take OKLCH (the force graph).
 * @param token a palette token without the `--color-` prefix
 * @returns `#rrggbb`
 * @throws Error when palette.css has no such token
 */
export function hex(token: string): string {
  return toHex(rgb(token))
}

/**
 * A palette token as a 24-bit number, the form three's colours take.
 * @param token a palette token without the `--color-` prefix
 * @returns the colour as `0xrrggbb`
 * @throws Error when palette.css has no such token
 */
export function num(token: string): number {
  const [r, g, b] = rgb(token)
  return (Math.round(r * 255) << 16) | (Math.round(g * 255) << 8) | Math.round(b * 255)
}

/**
 * Mix two tokens in OKLab, which keeps the blend perceptually even. Not cached: for a colour needed every frame,
 * compute it once outside the frame loop.
 * @param a the token at t = 0
 * @param b the token at t = 1
 * @param t how far towards `b`, 0 to 1
 * @returns the mix as `#rrggbb`
 * @throws Error when palette.css lacks either token
 */
export function mix(a: string, b: string, t: number): string {
  const [l1, a1, b1] = labOf(a)
  const [l2, a2, b2] = labOf(b)
  return toHex(srgb([l1 + (l2 - l1) * t, a1 + (a2 - a1) * t, b1 + (b2 - b1) * t]))
}

/**
 * The token a state tints the page with (the `--state` rules of palette.css).
 * @param state his state as shown
 * @returns a palette token, the idle tint for a state without its own rule
 */
export function stateTint(state: Shown): string {
  const { tints } = palette()
  return tints.get(state) ?? tints.get("idle") ?? "rigel-600"
}
