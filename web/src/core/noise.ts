/**
 * The one texture the shaders draw their slow, large-scale structure from: 256 × 256 RGBA8 of smooth, tileable value
 * noise, a different field in each channel (sampled with REPEAT and LINEAR, so a lookup is one fetch and the pattern
 * never shows a seam):
 *
 * - R: four lattice cells across the tile: broad folds (an aurora curtain's lean, the glow's drifting patches);
 * - G: sixteen cells: where curtains stand and where there are none;
 * - B: sixty-four cells: fine structure;
 * - A: two octaves, thirty-two and sixty-four cells: the cloud decks the world's night side is made of.
 *
 * Each channel is stretched to its full range, so a threshold means the same wherever it is used. Value noise on a
 * lattice, quintic-smoothed, evaluated at every texel here on the CPU once (about 5 ms): the shaders then need no
 * noise functions of their own, and the fine aurora rays, which must not repeat, use a hash instead (shaders.ts).
 * Plain numbers: tested in Node.
 */
import { mulberry32 } from "./random"

export const NOISE_SIZE = 256

/** Each channel's octaves: lattice cells across the tile, and weight. */
const CHANNELS: readonly (readonly (readonly [number, number])[])[] = [
  [[4, 1]],
  [[16, 1]],
  [[64, 1]],
  [
    [32, 0.65],
    [64, 0.35],
  ],
]

/** The quintic fade 6t⁵ − 15t⁴ + 10t³ (zero slope and curvature at the lattice points: no grid shows). */
const fade = (t: number) => t * t * t * (t * (t * 6 - 15) + 10)

/**
 * One octave of tileable value noise over the whole tile.
 * @param cells lattice cells across the tile
 * @param rng the random sequence
 * @returns NOISE_SIZE² values, 0..1, row by row
 */
function octave(cells: number, rng: () => number): Float32Array {
  const lattice = new Float32Array(cells * cells)
  for (let i = 0; i < lattice.length; i++) lattice[i] = rng()
  const out = new Float32Array(NOISE_SIZE * NOISE_SIZE)
  const step = cells / NOISE_SIZE
  for (let y = 0; y < NOISE_SIZE; y++) {
    const fy = y * step
    const y0 = Math.floor(fy)
    const ty = fade(fy - y0)
    const row0 = (y0 % cells) * cells
    const row1 = ((y0 + 1) % cells) * cells
    for (let x = 0; x < NOISE_SIZE; x++) {
      const fx = x * step
      const x0 = Math.floor(fx)
      const tx = fade(fx - x0)
      const x1 = (x0 + 1) % cells
      const a = lattice[row0 + (x0 % cells)] + (lattice[row0 + x1] - lattice[row0 + (x0 % cells)]) * tx
      const b = lattice[row1 + (x0 % cells)] + (lattice[row1 + x1] - lattice[row1 + (x0 % cells)]) * tx
      out[y * NOISE_SIZE + x] = a + (b - a) * ty
    }
  }
  return out
}

/**
 * The noise texture's pixels.
 * @param seed the random seed (the same seed, the same texture)
 * @returns NOISE_SIZE² RGBA8 pixels
 */
export function makeNoise(seed = 20): Uint8Array {
  const rng = mulberry32(seed)
  const pixels = new Uint8Array(NOISE_SIZE * NOISE_SIZE * 4)
  CHANNELS.forEach((octaves, channel) => {
    const field = new Float32Array(NOISE_SIZE * NOISE_SIZE)
    for (const [cells, weight] of octaves) {
      const o = octave(cells, rng)
      for (let i = 0; i < field.length; i++) field[i] += o[i] * weight
    }
    let lo = Number.POSITIVE_INFINITY
    let hi = Number.NEGATIVE_INFINITY
    for (const v of field) {
      lo = Math.min(lo, v)
      hi = Math.max(hi, v)
    }
    for (let i = 0; i < field.length; i++) pixels[i * 4 + channel] = Math.round(((field[i] - lo) / (hi - lo)) * 255)
  })
  return pixels
}

// ---------------------------------------------------------------------------------------------------- the aurora's rays
/** The rays' texture is this many texels long and one tall, and a texel is RAY_TEXEL_PX CSS px wide: it tiles every 3 277 px. */
export const RAY_SIZE = 4096
export const RAY_TEXEL_PX = 0.8
/** Each channel's lattice cells round the tile: rays 3.2 px wide, finer rays 8.8 px, the height each stands to (46 px), its lower border (61 px). */
const RAY_CELLS = [1024, 372, 71, 54] as const

/**
 * One line of tileable value noise (quintic-smoothed lattice values).
 * @param cells lattice cells round the line
 * @param rng the random sequence
 * @returns RAY_SIZE values, 0..1
 */
function line(cells: number, rng: () => number): Float32Array {
  const lattice = new Float32Array(cells)
  for (let i = 0; i < cells; i++) lattice[i] = rng()
  const out = new Float32Array(RAY_SIZE)
  const step = cells / RAY_SIZE
  for (let x = 0; x < RAY_SIZE; x++) {
    const f = x * step
    const i = Math.floor(f)
    const t = fade(f - i)
    out[x] = lattice[i % cells] + (lattice[(i + 1) % cells] - lattice[i % cells]) * t
  }
  return out
}

/**
 * The aurora's rays, baked: four value noises of different scales along one line, so that a single fetch gives a
 * point's ray, its finer ray, the height it stands to and its lower border (sampled with REPEAT and LINEAR). A hash
 * in the shader would do the same, at ten times the cost per pixel.
 * @param seed the random seed
 * @returns RAY_SIZE RGBA8 texels, each channel stretched to its full range
 */
export function makeRays(seed = 41): Uint8Array {
  const rng = mulberry32(seed)
  const pixels = new Uint8Array(RAY_SIZE * 4)
  RAY_CELLS.forEach((cells, channel) => {
    const values = line(cells, rng)
    let lo = Number.POSITIVE_INFINITY
    let hi = Number.NEGATIVE_INFINITY
    for (const v of values) {
      lo = Math.min(lo, v)
      hi = Math.max(hi, v)
    }
    for (let x = 0; x < RAY_SIZE; x++) pixels[x * 4 + channel] = Math.round(((values[x] - lo) / (hi - lo)) * 255)
  })
  return pixels
}
