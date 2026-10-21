/**
 * Horizon's scene: everything that is state and time, none of it WebGL (the renderer reads these fields each frame;
 * engine.ts keeps the clock). His state becomes the airglow's colour, which spreads from the talk button along the limb
 * like a wave; the levels drive the string (string.ts), his thinking raises the aurora, a tool's result launches a
 * meteor and flares the glow where it leaves, a recall lights stars in turn, a short answer is written by the pen
 * (pen.ts), rest dims the glow and draws the Orion figure. The sky drifts very slowly and leans a whisper with the
 * pointer. Every time here is in seconds; every length in CSS pixels.
 *
 * Reduced motion has no frames to run: `settle` puts the scene where it would come to rest (the colour spread, the
 * curtains standing, a writing complete, no meteors), and the engine draws that once.
 */
import type { Lin, Palette } from "./colors"
import type { CoreHue, CoreState } from "./index"
import * as pen from "./pen"
import type { Shape } from "./protocol"
import { mulberry32 } from "./random"
import {
  type Catalogue,
  CENTRE,
  direction,
  type Frame,
  figureSpan,
  M42,
  project,
  frame as skyFrame,
  view as skyView,
  starColor,
  type View,
} from "./sky"
import { NODES, Strand } from "./string"
import { arcUnder, type Limb, limb as makeLimb, onLimb, onString } from "./world"

const DEG = Math.PI / 180

/** The airglow's new colour spreads from the talk button this fast (CSS px a second), over this softness (CSS px). */
export const WIPE_SPEED = 2600
export const WIPE_SOFT = 160
/** The aurora rises over this many seconds (the time constant) and falls over the second; below AURORA_SEEN it isn't drawn. */
const AURORA_RISE_S = 0.9
const AURORA_FALL_S = 1.4
export const AURORA_SEEN = 0.004
/** The curtains' drift, CSS px a second, and their tallest ray's height as a share of the sky, within these bounds. */
const AURORA_DRIFT = 25
const AURORA_TOP = { share: 0.6, min: 140, max: 520 }
/** At rest the glow keeps this share of its light; it dims over REST_IN_S and wakes over REST_OUT_S (time constants). */
export const REST_DIM = 0.42
const REST_IN_S = 2.5
const REST_OUT_S = 0.35
/** The figure's lines start this far apart (s), each taking its length over FIGURE_SPEED (px a second), within these. */
const FIGURE_FIRST_S = 0.8
const FIGURE_GAP_S = 0.42
const FIGURE_SPEED = 110
const FIGURE_TIME = { min: 0.7, max: 2.6 }
/** The figure fades away over this many seconds (time constant) when he wakes. */
const FIGURE_OUT_S = 0.25
/** An error's shudder of light lasts this long (s). */
const SHUDDER_S = 3
/** A level that stops coming is silence after this long (s); levels follow what arrives with these time constants. */
const STALE_S = 0.3
const LEVEL_ATTACK_S = 0.04
const LEVEL_RELEASE_S = 0.15
/** A meteor's flight takes this long plus this much per CSS px; its tail is TRAIL_S long and it fades FADE_S after arriving. */
export const METEOR_MAX = 8
const METEOR_BASE_S = 0.5
const METEOR_PER_PX = 0.00035
export const METEOR_TRAIL_S = 0.26
export const METEOR_FADE_S = 0.5
/** A flare on the limb: where a meteor leaves, where a state begins. At most BUMPS at once. */
export const BUMPS = 4
/** Recalled stars brighten in turn, each starting this long after the one before; up to RECALL_MAX of them. */
export const RECALL_MAX = 8
const RECALL_STAGGER_S = 0.2
const RECALL_RISE_S = 0.12
const RECALL_FALL_S = 1
const RECALL_BOOST = 20
/** The stars a recall may choose: not fainter than this, clear of the top bar and the limb, this far apart (CSS px). */
const RECALL_FAINTEST = 4.6
const RECALL_TOP = 72
const RECALL_APART = 90
/** Light-writing: the writing's middle, as a share of the limb's height from the top; the digits' height as a share of it. */
const WRITE_Y = 0.42
const WRITE_EM = { share: 0.16, min: 56, max: 150 }
const WRITE_SIDE = 16
const WRITE_FADE_S = 0.9
/** A lone mark (a question, a check) is drawn at this share of the pen's speed. */
const WRITE_MARK_PACE = 0.45
/** The sky drifts in a slow, wide Lissajous (CSS px, periods in s) and leans away from the pointer (CSS px, seconds). */
const DRIFT = { x: 18, y: 7, px: 540, py: 810 }
const PARALLAX = { x: 5, y: 3, follow: 0.5 }

const EMPTY: readonly number[] = []

/** What a meteor needs the renderer to know. */
interface Meteor {
  t0: number
  ox: number
  oy: number
  tx: number
  ty: number
  dur: number
  colour: Lin
}

/** A flare on the limb. */
interface Bump {
  s: number
  sigma: number
  amp: number
  t0: number
  tau: number
}

/** A star being recalled. */
interface Recall {
  star: number
  t0: number
}

/** The writing on show. */
export interface Writing {
  key: string
  segments: pen.Segment[]
  duration: number
  t0: number
  /** When it starts to fade (s): after its hold, or a release. Infinity: not yet decided. */
  fadeAt: number
  origin: [number, number]
  colour: Lin
  pulse: boolean
  rev: number
}

export class Scene {
  readonly palette: Palette
  reduced: boolean
  private readonly rng = mulberry32(2026)

  // ---- the screen
  width = 1
  height = 1
  horizon = 1
  talkX = 0
  talkY = 0
  /** The first layout has come: before it nothing is drawn. */
  laid = false
  limb: Limb = makeLimb(1, 1)
  talkArc = 0

  // ---- the sky
  sky: Catalogue | null = null
  starColors = new Float32Array(0)
  /** The stars fading in once the catalogue has loaded, 0..1. */
  skyOpen = 0
  readonly view: View = skyView(CENTRE.ra, CENTRE.dec)
  frame: Frame = { scale: 1, x: 0, y: 0 }
  private span: { top: number; bottom: number } | null = null
  /** M42 in the sky's plane: x, y and the deviations across and up, radians. */
  m42: [number, number, number, number] = [0, 0, 1, 1]
  /** The figure's lines' start and length in time, 2 per line, and a counter that changes when they do. */
  figureTimes = new Float32Array(0)
  figureRev = 0
  /** The sky's shift from drift and parallax, CSS px. */
  shift: [number, number] = [0, 0]

  // ---- time
  time = 0

  // ---- his state and the airglow's colours
  state: CoreState = "idle"
  private from: CoreState = "idle"
  private to: CoreState = "idle"
  /** How far the new colour has spread from the talk button (CSS px along the limb), and where that is done. */
  wipeFront = Number.POSITIVE_INFINITY
  private wipeEnd = 0
  /** The glow's light: 1, less at rest, a shudder on an error. */
  glow = 1
  private errorAt = Number.NEGATIVE_INFINITY

  // ---- the string and the levels
  readonly strand = new Strand()
  readonly stringData = new Float32Array(NODES * 4)
  /** Changes whenever `stringData` does, so the renderer uploads it only then. */
  stringRev = 0
  private stringMoved = true
  voice = 0
  mic = 0
  private voiceTarget = 0
  private micTarget = 0
  private voiceAt = Number.NEGATIVE_INFINITY
  private micAt = Number.NEGATIVE_INFINITY

  // ---- the aurora
  aurora = 0
  auroraDrift = 0
  auroraTop = 0

  // ---- the loading sweep's place along the limb (CSS px), or far off
  sweep = -1e4

  // ---- rest
  resting = false
  /** 0 awake .. 1 at rest (the glow's dimming), and the figure's clock and light. */
  restMix = 0
  figureClock = 0
  figureAlpha = 0

  // ---- events
  private bumps: Bump[] = []
  readonly bumpData = new Float32Array(BUMPS * 4)
  meteors: Meteor[] = []
  private recalls: Recall[] = []
  writing: Writing | null = null
  private writingRev = 0
  private pointerTarget: [number, number] = [0, 0]
  private parallax: [number, number] = [0, 0]

  constructor(palette: Palette, reduced: boolean) {
    this.palette = palette
    this.reduced = reduced
  }

  // ------------------------------------------------------------------------------------------------ the screen
  /**
   * The canvas's size.
   * @param width CSS px
   * @param height CSS px
   */
  size(width: number, height: number): void {
    this.width = Math.max(1, width)
    this.height = Math.max(1, height)
    this.rebuild()
  }

  /**
   * Where the limb's top is and where the talk button is.
   * @param horizon CSS px from the top
   * @param talkX CSS px from the left
   * @param talkY CSS px from the top
   */
  layout(horizon: number, talkX: number, talkY: number): void {
    this.horizon = horizon
    this.talkX = talkX
    this.talkY = talkY
    this.laid = true
    this.rebuild()
  }

  /** The star catalogue has loaded: the sky can open. */
  setSky(sky: Catalogue): void {
    this.sky = sky
    this.starColors = new Float32Array(sky.count * 3)
    for (let i = 0; i < sky.count; i++) this.starColors.set(starColor(sky.bv[i]), i * 3)
    this.span = figureSpan(sky)
    this.rebuild()
  }

  private rebuild(): void {
    if (!this.laid) return
    const horizon = Math.min(Math.max(this.horizon, 1), this.height)
    this.limb = makeLimb(this.width, horizon)
    this.talkArc = arcUnder(this.limb, this.talkX, this.talkY)
    this.strand.talk = onString(this.limb, this.talkArc)
    this.auroraTop = Math.min(AURORA_TOP.max, Math.max(AURORA_TOP.min, horizon * AURORA_TOP.share))
    if (!this.sky || !this.span) return
    this.frame = skyFrame(this.width, horizon, this.span)
    const p = project(this.view, ...direction(M42.ra, M42.dec))
    if (p) this.m42 = [p[0], p[1], M42.sx * DEG, M42.sy * DEG]
    const lines = this.sky.lines
    const times = new Float32Array(lines.length)
    for (let k = 0; k < lines.length / 2; k++) {
      const a = this.plane(lines[k * 2])
      const b = this.plane(lines[k * 2 + 1])
      const length = a && b ? Math.hypot(a[0] - b[0], a[1] - b[1]) * this.frame.scale : 0
      times[k * 2] = FIGURE_FIRST_S + k * FIGURE_GAP_S
      times[k * 2 + 1] = Math.min(FIGURE_TIME.max, Math.max(FIGURE_TIME.min, length / FIGURE_SPEED))
    }
    this.figureTimes = times
    this.figureRev++
  }

  /** A catalogue star in the sky's plane (radians at the centre), or null behind the viewer. */
  private plane(i: number): [number, number] | null {
    const d = this.sky?.dirs
    return d ? project(this.view, d[i * 3], d[i * 3 + 1], d[i * 3 + 2]) : null
  }

  /** A catalogue star's place on the screen now, CSS px (null when it isn't on the sky's plane). */
  starAt(i: number): [number, number] | null {
    const p = this.plane(i)
    return p
      ? [this.frame.x + p[0] * this.frame.scale + this.shift[0], this.frame.y - p[1] * this.frame.scale + this.shift[1]]
      : null
  }

  // ------------------------------------------------------------------------------------------------ what the page says
  /** His state: the colour spreads from the talk button, and what moves follows. */
  setState(next: CoreState): void {
    if (next === this.state) return
    this.state = next
    this.from = this.to
    this.to = next
    const reach = Math.max(
      Math.abs(this.talkArc - this.limb.start),
      Math.abs(this.limb.start + this.limb.length - this.talkArc),
    )
    this.wipeFront = 0
    this.wipeEnd = reach + WIPE_SOFT
    if (next === "listening") {
      this.strand.pluck(this.strand.talk, 2.4)
      this.bump(this.talkArc, 90, 0.5, 0.5)
    } else if (next === "error") {
      this.errorAt = this.time
      this.strand.pluck(this.strand.talk, 6)
      this.bump(this.talkArc, 140, 1.2, 0.6)
    } else if (next === "thinking" || next === "speaking") {
      this.bump(this.talkArc, 120, 0.6, 0.6)
    }
  }

  /**
   * A level arrived (at most 30 a second).
   * @param source 0 your microphone, 1 his voice
   * @param value 0..1
   * @param bands 16 values 0..1
   */
  level(source: 0 | 1, value: number, bands: Float32Array): void {
    if (source === 1) {
      this.voiceTarget = value
      this.voiceAt = this.time
      this.strand.voice(bands)
    } else {
      this.micTarget = value
      this.micAt = this.time
      this.strand.listen(value)
    }
  }

  /** The pointer, −1..1 (y down): the sky leans away from it. */
  pointer(x: number, y: number): void {
    this.pointerTarget = [x, y]
  }

  /**
   * A meteor from the limb, near the talk button, to a point.
   * @param hue its colour
   * @param x CSS px
   * @param y CSS px
   */
  meteor(hue: CoreHue, x: number, y: number): void {
    if (!this.laid) return
    const s = this.talkArc + (this.rng() - 0.5) * 0.08 * this.width
    const o = onLimb(this.limb, s)
    const distance = Math.hypot(x - o.x, y - o.y)
    if (this.meteors.length >= METEOR_MAX) this.meteors.shift()
    this.meteors.push({
      t0: this.time,
      ox: o.x,
      oy: o.y - 1,
      tx: x,
      ty: y,
      dur: METEOR_BASE_S + distance * METEOR_PER_PX,
      colour: this.palette.hues[hue],
    })
    this.bump(s, 70, 1, 0.45)
  }

  /** A flare on the limb: where it is (arc length), how wide, how strong, how long it takes to die away. */
  private bump(s: number, sigma: number, amp: number, tau: number): void {
    const now = (b: Bump) => b.amp * Math.exp(-(this.time - b.t0) / b.tau)
    if (this.bumps.length >= BUMPS) {
      let weakest = 0
      for (let i = 1; i < this.bumps.length; i++) if (now(this.bumps[i]) < now(this.bumps[weakest])) weakest = i
      this.bumps.splice(weakest, 1)
    }
    this.bumps.push({ s, sigma, amp, t0: this.time, tau })
  }

  /** Memories recalled: stars brighten in turn, picked from the sky's middle, clear of each other. */
  recall(count: number): void {
    const sky = this.sky
    if (!this.laid || !sky) return
    const n = Math.min(RECALL_MAX, Math.max(0, Math.floor(count)))
    const pool: { i: number; x: number; y: number }[] = []
    for (let i = 0; i < sky.count && sky.vmag[i] <= RECALL_FAINTEST; i++) {
      const at = this.starAt(i)
      if (at && at[1] > RECALL_TOP && at[1] < this.horizon * 0.8 && at[0] > 24 && at[0] < this.width - 24)
        pool.push({ i, x: at[0], y: at[1] })
    }
    const chosen: { i: number; x: number; y: number }[] = []
    while (chosen.length < n && pool.length) {
      const pick = pool.splice(Math.floor(this.rng() * pool.length), 1)[0]
      if (chosen.every((c) => Math.hypot(c.x - pick.x, c.y - pick.y) >= RECALL_APART)) chosen.push(pick)
    }
    chosen.forEach((c, k) => {
      this.recalls.push({ star: c.i, t0: this.time + k * RECALL_STAGGER_S })
    })
  }

  /**
   * Light written by the pen.
   * @param text the text, already checked (protocol.ts): null for a shape or a release
   * @param shape a mark: null for a text or a release
   * @param ms how long it is held once written; 0 releases the writing of that text or shape (neither: any)
   */
  write(text: string | null, shape: Shape | null, ms: number): void {
    const key = text ?? shape ?? ""
    if (ms === 0) {
      if (this.writing && (key === "" || this.writing.key === key))
        this.writing.fadeAt = Math.min(this.writing.fadeAt, this.time)
      return
    }
    if (!this.laid) return
    const glyphs = text !== null ? [...text] : [shape as string]
    // as tall as is pleasing, and no wider than the screen
    const width100 = pen.write(glyphs, 100, 1).width
    const em = Math.min(
      WRITE_EM.max,
      Math.max(WRITE_EM.min, this.horizon * WRITE_EM.share),
      width100 > 0 ? ((this.width - 2 * WRITE_SIDE) / width100) * 100 : WRITE_EM.max,
    )
    const writing = pen.write(glyphs, em, text === null ? WRITE_MARK_PACE : 1)
    const half = writing.width / 2 + WRITE_SIDE
    const colour =
      shape === "question"
        ? this.palette.hues.betel
        : shape === "check"
          ? this.palette.hues.ok
          : mixWhite(this.palette.glow[this.state], 0.55)
    this.writing = {
      key,
      segments: writing.segments,
      duration: writing.duration,
      t0: this.time,
      fadeAt: this.time + writing.duration + ms / 1000,
      origin: [Math.min(Math.max(this.talkX, half), Math.max(half, this.width - half)), this.horizon * WRITE_Y],
      colour,
      pulse: shape === "question",
      rev: ++this.writingRev,
    }
  }

  /** Rest after a long idle (the glow dims, the figure draws itself), or wake. */
  rest(on: boolean): void {
    if (on === this.resting) return
    this.resting = on
    if (on) this.figureClock = 0
  }

  // ------------------------------------------------------------------------------------------------ time
  /**
   * Run the scene on.
   * @param dt seconds since the last frame
   */
  update(dt: number): void {
    this.time += dt
    const t = this.time
    const ease = (tau: number) => 1 - Math.exp(-dt / tau)

    // the levels follow what arrives, and silence when it stops
    if (t - this.voiceAt > STALE_S && this.voiceTarget > 0) {
      this.voiceTarget = 0
      this.strand.voice(EMPTY)
    }
    if (t - this.micAt > STALE_S && this.micTarget > 0) {
      this.micTarget = 0
      this.strand.listen(0)
    }
    this.voice +=
      (this.voiceTarget - this.voice) * ease(this.voiceTarget > this.voice ? LEVEL_ATTACK_S : LEVEL_RELEASE_S)
    this.mic += (this.micTarget - this.mic) * ease(this.micTarget > this.mic ? LEVEL_ATTACK_S : LEVEL_RELEASE_S)

    // the string
    const moving = !this.strand.still
    if (moving || this.stringMoved) {
      this.strand.advance(dt)
      this.strand.write(this.stringData, this.limb.length)
      this.stringRev++
      this.stringMoved = moving
    }

    // the colour's spread, the glow's light, the sweep
    if (this.wipeFront < this.wipeEnd) {
      this.wipeFront += WIPE_SPEED * dt
      if (this.wipeFront >= this.wipeEnd) {
        this.wipeFront = Number.POSITIVE_INFINITY
        this.from = this.to
      }
    }
    this.restMix += ((this.resting ? 1 : 0) - this.restMix) * ease(this.resting ? REST_IN_S : REST_OUT_S)
    const since = t - this.errorAt
    const shudder = since < SHUDDER_S ? Math.exp(-since / 0.5) * (0.5 + 0.5 * Math.sin(Math.PI * 18 * since)) : 0
    this.glow = (1 - (1 - REST_DIM) * this.restMix) * (1 + 0.6 * shudder)
    this.sweep = this.state === "loading" ? this.limb.start + (((t / 2.2) % 1) * 1.2 - 0.1) * this.limb.length : -1e4

    // the aurora
    const target = this.state === "thinking" ? 1 : 0
    this.aurora += (target - this.aurora) * ease(target > this.aurora ? AURORA_RISE_S : AURORA_FALL_S)
    if (this.aurora > AURORA_SEEN) this.auroraDrift += dt * AURORA_DRIFT
    else {
      this.aurora = target === 0 ? 0 : this.aurora
      this.auroraDrift = 0
    }

    // the figure
    if (this.resting) {
      this.figureClock += dt
      this.figureAlpha += (1 - this.figureAlpha) * ease(1.2)
    } else {
      this.figureAlpha += (0 - this.figureAlpha) * ease(FIGURE_OUT_S)
      if (this.figureAlpha < 0.003) this.figureAlpha = 0
    }
    this.skyOpen = this.sky ? Math.min(1, this.skyOpen + dt / 1.6) : 0

    // flares, meteors, recalls, the writing
    this.bumps = this.bumps.filter((b) => b.amp * Math.exp(-(t - b.t0) / b.tau) > 0.01)
    this.meteors = this.meteors.filter((m) => t - m.t0 < m.dur + METEOR_FADE_S + 0.05)
    this.recalls = this.recalls.filter((r) => t - r.t0 < RECALL_RISE_S + RECALL_FALL_S * 9)
    if (this.writing && t > this.writing.fadeAt + WRITE_FADE_S) this.writing = null

    // the sky's drift and lean
    this.lean(dt)
    this.placeSky()
  }

  /** The sky leans away from the pointer, easing after it. */
  private lean(dt: number): void {
    const k = 1 - Math.exp(-dt / PARALLAX.follow)
    this.parallax[0] += (this.pointerTarget[0] - this.parallax[0]) * k
    this.parallax[1] += (this.pointerTarget[1] - this.parallax[1]) * k
  }

  /** The sky's shift: its slow drift and its lean. */
  private placeSky(): void {
    const t = this.time
    this.shift[0] = DRIFT.x * Math.sin((2 * Math.PI * t) / DRIFT.px) - this.parallax[0] * PARALLAX.x
    this.shift[1] = DRIFT.y * Math.sin((2 * Math.PI * t) / DRIFT.py + 1) - this.parallax[1] * PARALLAX.y
  }

  /** Put the scene where it comes to rest, for a still frame (reduced motion). */
  settle(): void {
    this.from = this.to = this.state
    this.wipeFront = Number.POSITIVE_INFINITY
    this.voice = this.mic = this.voiceTarget = this.micTarget = 0
    this.strand.silence()
    this.strand.write(this.stringData, this.limb.length)
    this.stringRev++
    this.stringMoved = false
    this.restMix = this.resting ? 1 : 0
    this.glow = 1 - (1 - REST_DIM) * this.restMix
    this.sweep = -1e4
    this.aurora = this.state === "thinking" ? 1 : 0
    this.auroraDrift = 0
    this.figureClock = this.resting ? 1e3 : 0
    this.figureAlpha = this.resting ? 1 : 0
    this.skyOpen = this.sky ? 1 : 0
    this.bumps = []
    this.meteors = []
    this.recalls = []
    this.parallax = [0, 0]
    this.shift = [0, 0]
    if (this.writing) {
      this.writing.fadeAt = Number.POSITIVE_INFINITY
      this.writing.t0 = this.time - this.writing.duration - 5
    }
  }

  /** Drop the writing (reduced motion: its hold is over). */
  endWriting(): void {
    this.writing = null
  }

  /** Whether anything that needs sixty frames a second is moving (the stars' twinkle and the drift alone do not). */
  active(): boolean {
    const t = this.time
    const writing = this.writing
    return (
      !this.strand.still ||
      this.aurora > AURORA_SEEN ||
      this.state === "thinking" ||
      this.state === "loading" ||
      this.meteors.length > 0 ||
      this.recalls.length > 0 ||
      this.bumps.length > 0 ||
      this.wipeFront < this.wipeEnd ||
      t - this.errorAt < SHUDDER_S ||
      (writing !== null && (t - writing.t0 < writing.duration + 1 || t > writing.fadeAt))
    )
  }

  // ------------------------------------------------------------------------------------------------ what the renderer reads
  /** The airglow's colours: from, to, and the colours it turns to higher up. */
  get colours(): { a: Lin; b: Lin; upA: Lin; upB: Lin } {
    const { glow, upper } = this.palette
    return { a: glow[this.from], b: glow[this.to], upA: upper[this.from], upB: upper[this.to] }
  }

  /** Whether the string may be off the limb (it moves, or the last frame it did is yet to be drawn): the shader reads it only then. */
  get stringLive(): boolean {
    return !this.strand.still || this.stringMoved
  }

  /**
   * Whether nothing in the background (the sky, the world, the airglow) is moving right now: no string off the limb,
   * no flare, nobody's voice, no sweep, no colour still spreading, no aurora. The renderer then draws it once and
   * keeps it, as long as the values it is drawn from stay the same.
   */
  backgroundStill(): boolean {
    return (
      !this.stringLive &&
      this.bumps.length === 0 &&
      this.mic < 0.002 &&
      this.voice < 0.002 &&
      this.sweep < -1000 &&
      this.wipeFront === Number.POSITIVE_INFINITY &&
      this.aurora <= AURORA_SEEN
    )
  }

  /** How many flares are alive. */
  get bumpCount(): number {
    return this.bumps.length
  }

  /** The flares for the shader: place, width, strength, 0 for each of BUMPS. */
  bumpsForShader(): Float32Array {
    const out = this.bumpData
    out.fill(0)
    this.bumps.forEach((b, i) => {
      out[i * 4] = b.s
      out[i * 4 + 1] = b.sigma
      out[i * 4 + 2] = b.amp * Math.exp(-(this.time - b.t0) / b.tau)
    })
    // an unused slot has no width: keep it from dividing by zero
    for (let i = this.bumps.length; i < BUMPS; i++) out[i * 4 + 1] = 1
    return out
  }

  /**
   * The recalled stars for the renderer, 8 floats each: direction (3), magnitude, colour (3), the share of light added.
   * @param out room for RECALL_MAX × 8
   * @returns how many are lit
   */
  recallsForShader(out: Float32Array): number {
    const sky = this.sky
    if (!sky) return 0
    let n = 0
    for (const r of this.recalls) {
      const age = this.time - r.t0
      if (age < 0 || n >= RECALL_MAX) continue
      const boost =
        RECALL_BOOST * (age < RECALL_RISE_S ? age / RECALL_RISE_S : Math.exp(-(age - RECALL_RISE_S) / RECALL_FALL_S))
      if (boost < 0.02) continue
      out.set(sky.dirs.subarray(r.star * 3, r.star * 3 + 3), n * 8)
      out[n * 8 + 3] = sky.vmag[r.star]
      out.set(this.starColors.subarray(r.star * 3, r.star * 3 + 3), n * 8 + 4)
      out[n * 8 + 7] = boost
      n++
    }
    return n
  }

  /** A meteor's data for the renderer: from (2), to (2), age, duration, colour (3). */
  meteorsForShader(out: Float32Array): number {
    let n = 0
    for (const m of this.meteors) {
      if (n >= METEOR_MAX) break
      out.set([m.ox, m.oy, m.tx, m.ty, this.time - m.t0, m.dur, m.colour[0], m.colour[1], m.colour[2]], n * 9)
      n++
    }
    return n
  }

  /** The writing's light: its fade (and a held question's slow pulse). 0 when there is none. */
  writeGain(): number {
    const w = this.writing
    if (!w) return 0
    const fade = this.time > w.fadeAt ? 1 - smooth((this.time - w.fadeAt) / WRITE_FADE_S) : 1
    const pulse =
      w.pulse && this.time - w.t0 > w.duration ? 1 + 0.12 * Math.sin(2 * Math.PI * 0.8 * (this.time - w.t0)) : 1
    return fade * pulse
  }

  /** The time the shaders' flicker runs on: wrapped, since the twinkle is as good at any phase. */
  get flicker(): number {
    return this.time % 1024
  }

  /** The slow phase of the glow's drifting patches, 0..1 (the noise is one tile: a wrap is not seen). */
  get patch(): number {
    return (this.time / 80) % 1
  }
}

const smooth = (t: number) => {
  const x = Math.min(1, Math.max(0, t))
  return x * x * (3 - 2 * x)
}

/** A colour mixed towards white. */
const mixWhite = (c: Lin, t: number): Lin => [c[0] + (1 - c[0]) * t, c[1] + (1 - c[1]) * t, c[2] + (1 - c[2]) * t]
