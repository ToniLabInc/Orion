// A low-pass interpolation kernel for changing sample rates: a Kaiser-windowed sinc, tabulated at fractional positions
// (a polyphase table) and interpolated linearly between them, so any ratio (48 → 16 kHz, 44.1 → 16 kHz, 24 → 48 kHz,
// and a ratio that drifts while the player catches up) is filtered properly. Plain arithmetic: it runs in the
// AudioWorklets and in Node's tests alike.

export interface KernelOptions {
  /** The pass band's edge (its −6 dB point) as a fraction of the lower rate's Nyquist frequency. */
  cutoff?: number
  /** The sinc's zero crossings on each side: the filter's length, so how steep its transition is. */
  zeros?: number
  /** The Kaiser window's β: its stop band (7 ≈ 70 dB). */
  beta?: number
  /** Fractional positions tabulated per input sample. */
  phases?: number
}

export interface SincKernel {
  /** Taps on each side: an output at input position i + f (0 ≤ f < 1) reads samples i − half + 1 … i + half. */
  readonly half: number
  /** Taps per output sample (2 × half). */
  readonly taps: number
  readonly phases: number
  /** `phases` rows of `taps` weights: row p is for f = p / phases. */
  readonly table: Float32Array
  /** Each row's step to the next one (row p + 1 − row p; the last steps to f = 1), for the interpolation. */
  readonly slope: Float32Array
}

const DEFAULTS: Required<KernelOptions> = { cutoff: 0.92, zeros: 24, beta: 7, phases: 64 }

/**
 * The kernel for resampling `inRate` to `outRate`: its band ends below the lower of the two Nyquist frequencies, so
 * downsampling doesn't alias and upsampling leaves no images. Every row sums to 1 (DC passes unchanged).
 * @param inRate - the input's sample rate (Hz)
 * @param outRate - the output's sample rate (Hz)
 * @param options - its shape (defaults: cutoff 0.92, 24 zero crossings a side, β 7, 64 phases)
 * @returns the kernel
 */
export function sincKernel(inRate: number, outRate: number, options: KernelOptions = {}): SincKernel {
  const { cutoff, zeros, beta, phases } = { ...DEFAULTS, ...options }
  const fc = 0.5 * cutoff * Math.min(1, outRate / inRate) // cycles per input sample
  const width = zeros / (2 * fc) // the window's half width, in input samples
  const half = Math.ceil(width)
  const taps = 2 * half
  const rows = phases + 1 // the last row (f = 1) only gives the last slope
  const all = new Float64Array(rows * taps)
  const norm = bessel0(beta)
  for (let p = 0; p < rows; p++) {
    let sum = 0
    for (let j = 0; j < taps; j++) {
      const t = j - half + 1 - p / phases // the tap's distance from the output position
      const x = t / width
      const w = Math.abs(x) < 1 ? (2 * fc * sinc(2 * fc * t) * bessel0(beta * Math.sqrt(1 - x * x))) / norm : 0
      all[p * taps + j] = w
      sum += w
    }
    for (let j = 0; j < taps; j++) all[p * taps + j] /= sum
  }
  const table = new Float32Array(phases * taps)
  const slope = new Float32Array(phases * taps)
  for (let i = 0; i < phases * taps; i++) {
    table[i] = all[i]
    slope[i] = all[i + taps] - all[i]
  }
  return { half, taps, phases, table, slope }
}

/**
 * One output sample: the kernel at fraction `frac` over `taps` samples of `buf` from `base`.
 * @param k - the kernel
 * @param buf - the input samples
 * @param base - index in `buf` of the first tap (the position's integer part − half + 1)
 * @param frac - the position's fractional part, 0 ≤ frac < 1
 * @returns the sample
 */
export function interpolate(k: SincKernel, buf: Float32Array, base: number, frac: number): number {
  const at = frac * k.phases
  const p = Math.floor(at)
  const f = at - p
  const row = p * k.taps
  const { table, slope, taps } = k
  let acc = 0
  for (let j = 0; j < taps; j++) acc += buf[base + j] * (table[row + j] + f * slope[row + j])
  return acc
}

function sinc(x: number): number {
  if (x === 0) return 1
  const px = Math.PI * x
  return Math.sin(px) / px
}

/** The modified Bessel function of the first kind, order 0 (the Kaiser window), by its series. */
function bessel0(x: number): number {
  const q = (x * x) / 4
  let term = 1
  let sum = 1
  for (let k = 1; k < 100 && term > sum * 1e-15; k++) {
    term *= q / (k * k)
    sum += term
  }
  return sum
}
