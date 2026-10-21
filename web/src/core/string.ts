/**
 * The airglow's string: his voice. A damped one-dimensional wave equation over NODES nodes along the limb, its ends
 * fixed a little past the screen's sides:
 *
 *   u_tt = c² u_ξξ − 2σ₀ u_t + 2σ₁ ∂t u_ξξ + f        ξ: 0..1 along the string, u: CSS px outward
 *
 * stepped explicitly (Bilbao, Numerical Sound Synthesis, ch. 7: the centred scheme, the frequency-dependent loss taken
 * backwards in time) at a fixed STEP, so the same levels give the same string at any frame rate. σ₁ lets short waves
 * die sooner than long ones, as on a real string: a ripple softens as it runs. Two things drive it:
 *
 * - his voice: each of its 16 bands drives one standing wave, mode MODES[k] (k + 2 half-waves along the string), at
 *   that mode's own frequency c·n/2, so it builds up and rings down as a real string's mode does; low bands are the
 *   long waves. A band at full level holds its mode at VOICE_PX·(2/n)^VOICE_FALL;
 * - your voice: a small source at the talk button, oscillating at SOURCE_HZ with your level, kicked harder at each
 *   onset (the level rising faster than its own average): bursts of ripples running outward both ways. `pluck` is
 *   one such burst anywhere (an error's shudder).
 *
 * `write` fills what the shader reads per node: the displacement (softly limited to SOFT_LIMIT_PX), its slope along the
 * limb (the line keeps its width where it tilts) and how far it moves in an exposure of EXPOSURE_S (the line smeared along its own motion, its light
 * spread: a fast vibration shows its envelope, as a real string does to the eye). Plain numbers: tested in Node.
 */

export const NODES = 512
/** Seconds a wave takes from one end of the string to the other: c = 1 / CROSS_S strings a second. */
export const CROSS_S = 1.1
/** σ₀, per second: how fast the long waves die. */
export const LOSS = 1.6
/** σ₁, (string lengths)² per second: the extra loss of short waves, σ₀ + σ₁β² for a wavenumber β. */
export const LOSS_HIGH = 3e-4
/** The force on the string is worked out once in this many steps (200 Hz: its signals are under 10 Hz). */
const DRIVE_EVERY = 4
/** The fixed time step, seconds (stable: λ² + 4σ₁k/h² ≈ 0.73 ≤ 1). */
export const STEP = 1 / 800
/** The modes his voice's bands drive, one per band, low to high. */
export const MODES: readonly number[] = Array.from({ length: 16 }, (_, k) => k + 2)
/** A band at full level: its mode's amplitude for the lowest, CSS px; higher modes fall as n^−VOICE_FALL. */
export const VOICE_PX = 9
export const VOICE_FALL = 0.25
/** Your voice's source: its frequency, its ripples' amplitude at full level and at an onset, its width. */
export const SOURCE_HZ = 6.5
export const SOURCE_PX = 1.1
export const ONSET_PX = 4.2
const SOURCE_WIDTH = 0.006
/** An onset: the level this far above its own average (which follows it over AVERAGE_S), at most every GAP_S. */
const ONSET = 0.07
const AVERAGE_S = 0.25
const GAP_S = 0.12
/** An onset's kick dies over this, seconds. */
const KICK_S = 0.14
/** Band levels follow what arrives: rising over ATTACK_S, falling over RELEASE_S. */
const ATTACK_S = 0.05
const RELEASE_S = 0.16
/** The line never strays further than about this from the limb: a soft limit (tanh), so loud speech can't throw it about. */
export const SOFT_LIMIT_PX = 26
/** The motion a frame's exposure smears, seconds. */
export const EXPOSURE_S = 1 / 60
/** Below these the string is still: zeroed, and nothing more to draw. */
const STILL_PX = 0.02
const STILL_SPEED = 0.5 // CSS px a second
/** A level below this is a room's hush, not a voice. */
const QUIET = 0.02

const c = 1 / CROSS_S
const h = 1 / (NODES - 1)
const k = STEP
const LAMBDA2 = ((c * k) / h) ** 2
const ZETA = (2 * LOSS_HIGH * k) / (h * h)
const DAMP = LOSS * k

/** A burst of ripples from one place: where, how strong (CSS px), when it started. */
interface Burst {
  at: number
  px: number
  start: number
}

export class Strand {
  /** The displacement, CSS px outward, per node. */
  u = new Float32Array(NODES)
  private prev = new Float32Array(NODES)
  private next = new Float32Array(NODES)
  private lap = new Float32Array(NODES)
  private lapPrev = new Float32Array(NODES)
  private force = new Float32Array(NODES)
  /** What `write` shows: the displacement after the soft limit, now and one step ago. */
  private shown = new Float32Array(NODES)
  private shownPrev = new Float32Array(NODES)
  /** sin(nπξ) for each mode, NODES apiece. */
  private shapes: Float32Array
  /** Each mode's drive per unit band level (2σω·A), its angular frequency and its phase. */
  private gains: Float32Array
  private omegas: Float32Array
  private phases: Float32Array
  private bands = new Float32Array(MODES.length)
  private targets = new Float32Array(MODES.length)
  private time = 0
  private owed = 0
  private steps = 0
  private mic = 0
  private average = 0
  private kick = 0
  /** When the last onset was (the source starts each burst of ripples at its own phase 0). */
  private onsetAt = Number.NEGATIVE_INFINITY
  private phaseAt = 0
  private bursts: Burst[] = []
  /** Whether the string has come to rest (zeroed): nothing moves until something drives it. */
  still = true
  /** The talk button's place on the string, 0..1. */
  talk = 0.5

  constructor() {
    const n = MODES.length
    this.shapes = new Float32Array(n * NODES)
    this.gains = new Float32Array(n)
    this.omegas = new Float32Array(n)
    this.phases = new Float32Array(n)
    for (let m = 0; m < n; m++) {
      const beta = MODES[m] * Math.PI
      const omega = c * beta
      const loss = LOSS + LOSS_HIGH * beta * beta
      const amplitude = VOICE_PX * (MODES[0] / MODES[m]) ** VOICE_FALL
      this.omegas[m] = omega
      this.gains[m] = 2 * loss * omega * amplitude
      this.phases[m] = (m * 2.399963) % (2 * Math.PI) // the golden angle: no two modes start in step
      for (let j = 0; j < NODES; j++) this.shapes[m * NODES + j] = Math.sin(beta * j * h)
    }
  }

  /**
   * His voice: the bands' levels the modes follow (missing bands are silent).
   * @param bands up to 16 levels, 0..1, low to high
   */
  voice(bands: ArrayLike<number>): void {
    for (let m = 0; m < MODES.length; m++) this.targets[m] = m < bands.length ? Math.min(1, Math.max(0, bands[m])) : 0
    if (this.targets.some((b) => b > QUIET)) this.still = false
  }

  /**
   * Your voice's level at the talk button.
   * @param level 0..1
   */
  listen(level: number): void {
    this.mic = Math.min(1, Math.max(0, level))
    if (this.mic > QUIET) this.still = false
  }

  /**
   * A burst of ripples from a place on the string.
   * @param at where, 0..1
   * @param px how strong, CSS px
   */
  pluck(at: number, px: number): void {
    this.bursts.push({ at: Math.min(1, Math.max(0, at)), px, start: this.time })
    this.still = false
  }

  /** Let go of everything: no drive, the string at rest. */
  silence(): void {
    this.targets.fill(0)
    this.bands.fill(0)
    this.mic = this.average = this.kick = 0
    this.bursts = []
    this.u.fill(0)
    this.prev.fill(0)
    this.lap.fill(0)
    this.lapPrev.fill(0)
    this.still = true
  }

  /**
   * Run the string on by `dt` seconds, in whole STEPs (the remainder carries over).
   * @param dt seconds
   */
  advance(dt: number): void {
    if (this.still) {
      this.owed = 0
      return
    }
    this.owed += Math.min(dt, 0.1)
    this.follow(dt)
    while (this.owed >= STEP) {
      this.owed -= STEP
      // the drive's force changes slowly (voices and ripples under 10 Hz): work it out every DRIVE_EVERY steps
      if (this.steps++ % DRIVE_EVERY === 0) this.drive()
      this.step()
      this.time += STEP
    }
    this.settle()
  }

  /** The bands and your level follow what arrived; an onset kicks the source. */
  private follow(dt: number): void {
    for (let m = 0; m < MODES.length; m++) {
      const target = this.targets[m]
      const tau = target > this.bands[m] ? ATTACK_S : RELEASE_S
      this.bands[m] += (target - this.bands[m]) * (1 - Math.exp(-dt / tau))
    }
    this.average += (this.mic - this.average) * (1 - Math.exp(-dt / AVERAGE_S))
    this.kick *= Math.exp(-dt / KICK_S)
    const rise = this.mic - this.average
    if (rise > ONSET && this.time - this.onsetAt > GAP_S) {
      this.kick = Math.min(1, this.kick + rise * 4)
      this.onsetAt = this.phaseAt = this.time
    }
  }

  /** The force on each node now: the voice's modes, your source, the bursts. */
  private drive(): void {
    const f = this.force
    f.fill(0)
    const t = this.time
    for (let m = 0; m < MODES.length; m++) {
      const level = this.bands[m]
      if (level < 1e-4) continue
      const a = this.gains[m] * level * Math.sin(this.omegas[m] * t + this.phases[m])
      const shape = this.shapes.subarray(m * NODES, (m + 1) * NODES)
      for (let j = 1; j < NODES - 1; j++) f[j] += a * shape[j]
    }
    const omega = 2 * Math.PI * SOURCE_HZ
    // a point source radiates waves of amplitude F / (2cω) each way: F for the amplitude wanted
    const px = SOURCE_PX * this.average + ONSET_PX * this.kick
    if (px > 1e-3) this.source(this.talk, 2 * c * omega * px * Math.sin(omega * (t - this.phaseAt)))
    this.bursts = this.bursts.filter((b) => {
      const age = t - b.start
      const cycles = 1.5
      if (age > cycles / SOURCE_HZ) return false
      const envelope = Math.sin((Math.PI * age * SOURCE_HZ) / cycles) ** 2
      this.source(b.at, 2 * c * omega * b.px * envelope * Math.sin(omega * age))
      return true
    })
  }

  /** A force F spread over SOURCE_WIDTH round `at` (a Gaussian of unit integral). */
  private source(at: number, force: number): void {
    const centre = at * (NODES - 1)
    const width = SOURCE_WIDTH * (NODES - 1)
    const reach = Math.ceil(width * 4)
    const norm = force / (SOURCE_WIDTH * Math.sqrt(2 * Math.PI))
    const lo = Math.max(1, Math.floor(centre - reach))
    const hi = Math.min(NODES - 2, Math.ceil(centre + reach))
    for (let j = lo; j <= hi; j++) {
      const d = (j - centre) / width
      this.force[j] += norm * Math.exp(-0.5 * d * d)
    }
  }

  /** One step of the scheme (the ends stay at 0). */
  private step(): void {
    const { u, prev, next, lap, lapPrev, force } = this
    for (let j = 1; j < NODES - 1; j++) lap[j] = u[j + 1] - 2 * u[j] + u[j - 1]
    const kk = k * k
    for (let j = 1; j < NODES - 1; j++) {
      next[j] =
        (2 * u[j] - (1 - DAMP) * prev[j] + LAMBDA2 * lap[j] + ZETA * (lap[j] - lapPrev[j]) + kk * force[j]) / (1 + DAMP)
    }
    this.prev = u
    this.u = next
    this.next = prev
    this.lapPrev = lap
    this.lap = lapPrev
  }

  /** Nothing driving it and too little moving to see: zero it and call it still. */
  private settle(): void {
    if (this.mic > QUIET || this.kick > 1e-3 || this.bursts.length || this.bands.some((b) => b > QUIET)) return
    let most = 0
    let fastest = 0
    for (let j = 0; j < NODES; j++) {
      most = Math.max(most, Math.abs(this.u[j]))
      fastest = Math.max(fastest, Math.abs(this.u[j] - this.prev[j]))
    }
    if (most < STILL_PX && fastest / STEP < STILL_SPEED) this.silence()
  }

  /** The largest displacement now, CSS px. */
  peak(): number {
    let most = 0
    for (let j = 0; j < NODES; j++) most = Math.max(most, Math.abs(this.u[j]))
    return most
  }

  /**
   * What the shader reads, 4 floats per node: the displacement (CSS px), its slope along the limb, half the distance
   * it moves in an exposure (CSS px), and 0.
   * @param out NODES × 4 floats
   * @param length the string's length on screen, CSS px
   */
  write(out: Float32Array, length: number): void {
    const { u, prev, shown, shownPrev } = this
    const ds = length / (NODES - 1)
    for (let j = 0; j < NODES; j++) {
      shown[j] = SOFT_LIMIT_PX * Math.tanh(u[j] / SOFT_LIMIT_PX)
      shownPrev[j] = SOFT_LIMIT_PX * Math.tanh(prev[j] / SOFT_LIMIT_PX)
    }
    for (let j = 0; j < NODES; j++) {
      const left = j > 0 ? shown[j - 1] : -shown[1] // the fixed end continues the slope through it
      const right = j < NODES - 1 ? shown[j + 1] : -shown[NODES - 2]
      out[j * 4] = shown[j]
      out[j * 4 + 1] = (right - left) / (2 * ds)
      out[j * 4 + 2] = (Math.abs(shown[j] - shownPrev[j]) / STEP) * EXPOSURE_S * 0.5
      out[j * 4 + 3] = 0
    }
  }
}
