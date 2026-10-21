/**
 * Horizon's renderer: the GL resources and the draws of one frame, from what the scene says (scene.ts). The order:
 *
 *   1. the aurora, while it shows, into a texture of its own at its tier's density, only over the strip above the limb
 *      (a scissor), freed after twenty seconds of not being needed;
 *   2. the background: the deep world is a clear, the far sky one strip of quads and the band round the limb another
 *      (a strip of radial quads is a region with its altitude and arc length in its vertices: no pixel works either
 *      out), the aurora laid on. While nothing in it moves it is drawn once into a texture of its own and an idle
 *      frame is a blit of that and the stars (what changes the background changes the signature it was drawn from,
 *      and it is drawn again);
 *   3. over it, blended: the stars (and the few being recalled), the Orion figure while resting, the pen's writing,
 *      the meteors.
 *
 * That is at most nine draw calls and no other pass: no bloom, no blur, no post-processing. The string reaches the
 * shaders as a 512 × 1 float texture; the catalogue as one static buffer of instances (brightest first: the tier's
 * faintest magnitude is a count); the sky's slow noise as one 256² texture made on the CPU once.
 */
import type { Palette } from "./colors"
import { framebuffer, type Program, program, texture } from "./gl"
import { makeNoise, makeRays, NOISE_SIZE, RAY_SIZE } from "./noise"
import type { Tier } from "./quality"
import { AURORA_SEEN, METEOR_FADE_S, METEOR_MAX, METEOR_TRAIL_S, RECALL_MAX, type Scene, WIPE_SOFT } from "./scene"
import * as shaders from "./shaders"
import {
  type Baked,
  MW_MARGIN,
  MW_PEAK,
  milkyWay,
  STAR_CUTOFF,
  STAR_EXPOSURE,
  STAR_SIGMA,
  WING_SHARE,
  WING_WIDTH,
} from "./sky"
import { NODES, SOFT_LIMIT_PX } from "./string"

/** Floats per star in the static buffer: direction (3), magnitude, colour (3). */
const STAR_FLOATS = 7
/** Floats per recalled star: the same and the share of light added. */
const RECALL_FLOATS = 8
/** Floats per figure line: its two stars' directions (6) and its start and length in time. */
const FIGURE_FLOATS = 8
/** Floats per writing segment: its ends (4), the pen's times (2) and the ink (2). */
const SEGMENT_FLOATS = 8
/** Floats per meteor: from (2), to (2), age, duration, colour (3). */
const METEOR_FLOATS = 9
/** The vertices of a meteor's ribbon: 2 for each of 37 samples along it. */
const METEOR_VERTICES = 74
/** The colour's spread once it is done: far past any screen, but a number a shader can do arithmetic with. */
const WIPE_DONE = 1e6
/** The cached background is freed when it hasn't been needed for this long (s). */
const CACHE_KEEP_S = 60
/** The glow's slow patches are drawn at this many steps of their phase, and the sky's shift at this many parts of a CSS px. */
const PATCH_STEPS = 256
const SHIFT_STEPS = 4
/** The aurora's texture is freed when it hasn't been needed for this long (s). */
const AURORA_KEEP_S = 20

export interface Renderer {
  /** The bench's hook: called with each pass's name just before it, and with "" after the last (GPU timer queries). */
  profile: ((pass: string) => void) | null
  /** The canvas's size in device pixels, and the viewport's in CSS pixels. */
  size(w: number, h: number, cssW: number, cssH: number): void
  tier(tier: Tier): void
  /** Draw a frame; `twinkle: false` freezes what flickers (a still frame). */
  draw(scene: Scene, twinkle: boolean): void
  dispose(): void
}

interface Target {
  fbo: WebGLFramebuffer
  tex: WebGLTexture
  w: number
  h: number
}

/**
 * @param gl the context
 * @param palette the colours
 * @returns the renderer
 * @throws Error when a shader fails to compile or link, or a texture or buffer can't be made
 */
export function createRenderer(gl: WebGL2RenderingContext, palette: Palette): Renderer {
  const skyPass = program(gl, shaders.BAND_VERTEX, shaders.SKY_FRAGMENT)
  const nearPass = program(gl, shaders.BAND_VERTEX, shaders.NEAR_FRAGMENT)
  const auroraPass = program(gl, shaders.BAND_VERTEX, shaders.AURORA)
  const starPass = program(gl, shaders.STAR_VERTEX, shaders.STAR_FRAGMENT)
  const figurePass = program(gl, shaders.FIGURE_VERTEX, shaders.STROKE_FRAGMENT)
  const writePass = program(gl, shaders.WRITE_VERTEX, shaders.STROKE_FRAGMENT)
  const meteorPass = program(gl, shaders.METEOR_VERTEX, shaders.METEOR_FRAGMENT)

  // the clear colour is the deep world's: display values, the sRGB encoding of its linear light
  const display = (c: number) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055)
  const ground = palette.ground.map(display)

  const buffer = (): WebGLBuffer => {
    const b = gl.createBuffer()
    if (!b) throw new Error("no buffer")
    return b
  }
  const vao = (): WebGLVertexArrayObject => {
    const v = gl.createVertexArray()
    if (!v) throw new Error("no vertex array")
    return v
  }
  /** Instanced float attributes, each `{ location, size, offset }` in floats, from one buffer. */
  const attributes = (buf: WebGLBuffer, stride: number, list: readonly [number, number, number][]) => {
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    for (const [location, size, offset] of list) {
      gl.enableVertexAttribArray(location)
      gl.vertexAttribPointer(location, size, gl.FLOAT, false, stride * 4, offset * 4)
      gl.vertexAttribDivisor(location, 1)
    }
  }

  // ---- the geometry's buffers
  const starBuffer = buffer()
  const starVao = vao()
  gl.bindVertexArray(starVao)
  attributes(starBuffer, STAR_FLOATS, [
    [0, 3, 0],
    [1, 1, 3],
    [2, 3, 4],
  ])
  gl.bindVertexArray(null)
  const recallBuffer = buffer()
  const recallVao = vao()
  gl.bindVertexArray(recallVao)
  attributes(recallBuffer, RECALL_FLOATS, [
    [0, 3, 0],
    [1, 1, 3],
    [2, 3, 4],
    [3, 1, 7],
  ])
  gl.bindVertexArray(null)
  gl.bindBuffer(gl.ARRAY_BUFFER, recallBuffer)
  gl.bufferData(gl.ARRAY_BUFFER, RECALL_MAX * RECALL_FLOATS * 4, gl.DYNAMIC_DRAW)
  const figureBuffer = buffer()
  const figureVao = vao()
  gl.bindVertexArray(figureVao)
  attributes(figureBuffer, FIGURE_FLOATS, [
    [0, 3, 0],
    [1, 3, 3],
    [2, 2, 6],
  ])
  gl.bindVertexArray(null)
  const writeBuffer = buffer()
  const writeVao = vao()
  gl.bindVertexArray(writeVao)
  attributes(writeBuffer, SEGMENT_FLOATS, [
    [0, 4, 0],
    [1, 4, 4],
  ])
  gl.bindVertexArray(null)
  const meteorBuffer = buffer()
  const meteorVao = vao()
  gl.bindVertexArray(meteorVao)
  attributes(meteorBuffer, METEOR_FLOATS, [
    [0, 4, 0],
    [1, 2, 4],
    [2, 3, 6],
  ])
  gl.bindVertexArray(null)
  gl.bindBuffer(gl.ARRAY_BUFFER, meteorBuffer)
  gl.bufferData(gl.ARRAY_BUFFER, METEOR_MAX * METEOR_FLOATS * 4, gl.DYNAMIC_DRAW)
  // a star's boost is zero unless the recall's buffer says otherwise: attribute 3 is a constant for the main field
  gl.vertexAttrib1f(3, 0)

  // ---- the textures
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1) // the Milky Way's rows are single bytes
  const rgba8 = { internal: gl.RGBA8, format: gl.RGBA, type: gl.UNSIGNED_BYTE }
  const r8 = { internal: gl.R8, format: gl.RED, type: gl.UNSIGNED_BYTE }
  const noisePixels = makeNoise()
  const noiseTex = texture(gl, NOISE_SIZE, NOISE_SIZE, rgba8, gl.LINEAR, noisePixels, gl.REPEAT)
  const raysTex = texture(gl, RAY_SIZE, 1, rgba8, gl.LINEAR, makeRays(), gl.REPEAT)
  let milkyWayTex = texture(gl, 1, 1, r8, gl.LINEAR, new Uint8Array(1)) // until the band is baked
  let milkyWayBaked: Baked | null = null
  let milkyWayFor = ""
  const stringTex = texture(gl, NODES, 1, { internal: gl.RGBA32F, format: gl.RGBA, type: gl.FLOAT }, gl.NEAREST, null)
  let stringRev = -1

  // ---- what depends on the screen, the tier and the catalogue
  let width = 1
  let height = 1
  let cssW = 1
  let cssH = 1
  let current: Tier | null = null
  let starCount = 0
  let starsReady = 0 // the catalogue's count once it is uploaded
  let figureRev = -1
  let writeRev = -1
  let segmentCount = 0
  let aurora: Target | null = null
  let auroraHorizon = -1
  let auroraRadius = -1
  let auroraSeen = Number.NEGATIVE_INFINITY
  const recallData = new Float32Array(RECALL_MAX * RECALL_FLOATS)
  const meteorData = new Float32Array(METEOR_MAX * METEOR_FLOATS)

  /** The Milky Way's band, baked again when the screen or the sky's place on it changes. */
  const bakeMilkyWay = (scene: Scene) => {
    if (!scene.sky) return
    const key = `${scene.width}|${scene.height}|${scene.frame.scale}|${scene.frame.x}|${scene.frame.y}`
    if (key === milkyWayFor) return
    milkyWayFor = key
    milkyWayBaked = milkyWay(scene.width, scene.height, scene.frame, scene.view, noisePixels, NOISE_SIZE)
    milkyWayRev++
    gl.deleteTexture(milkyWayTex)
    milkyWayTex = texture(gl, milkyWayBaked.w, milkyWayBaked.h, r8, gl.LINEAR, milkyWayBaked.data)
  }

  // ---- the cached layers of the background: a target, and the numbers it was drawn from
  interface Layer {
    target: Target | null
    sig: Float32Array | null
    used: number
  }
  /** The deep world and the far sky: nearly always the same. */
  const far: Layer = { target: null, sig: null, used: Number.NEGATIVE_INFINITY }
  /** The whole background (far and near), while nothing in it moves. */
  const full: Layer = { target: null, sig: null, used: Number.NEGATIVE_INFINITY }
  let milkyWayRev = 0

  const dropLayer = (layer: Layer) => {
    if (layer.target) {
      gl.deleteFramebuffer(layer.target.fbo)
      gl.deleteTexture(layer.target.tex)
    }
    layer.target = null
    layer.sig = null
  }

  /** The numbers the far layer is drawn from, in a fixed order: it is good while they are all the same. */
  const signFar = (scene: Scene, reach: number): number[] => {
    const l = scene.limb
    const c = scene.colours
    return [
      width,
      height,
      cssW,
      cssH,
      l.cx,
      l.cy,
      l.r,
      l.horizon,
      scene.frame.scale,
      scene.frame.x,
      scene.frame.y,
      scene.skyOpen,
      scene.sky ? 1 : 0,
      milkyWayRev,
      ...scene.m42,
      scene.glow,
      ...shiftOf(scene),
      ...c.b,
      ...c.upB,
      reach,
    ]
  }

  /** And the full background's: the far layer's and what the near band adds. */
  const signFull = (scene: Scene, reach: number): number[] => {
    const c = scene.colours
    return [
      ...signFar(scene, reach),
      scene.limb.length,
      scene.talkArc,
      quantised(scene.patch, PATCH_STEPS),
      scene.auroraTop,
      ...c.a,
      ...c.upA,
    ]
  }

  /**
   * Make a layer good: draw it again into its target if the numbers it was drawn from have changed (or it has none yet).
   * @returns the layer's target
   */
  const bake = (layer: Layer, sig: number[], draw: () => void): Target => {
    if (!layer.target) {
      const made = framebuffer(gl, width, height, rgba8, gl.NEAREST)
      layer.target = { fbo: made.fbo, tex: made.tex[0], w: width, h: height }
    }
    const now = Float32Array.from(sig)
    const old = layer.sig
    if (!old || old.length !== now.length || now.some((v, i) => v !== old[i])) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, layer.target.fbo)
      gl.viewport(0, 0, width, height)
      draw()
      gl.bindFramebuffer(gl.FRAMEBUFFER, null)
      layer.sig = now
    }
    return layer.target
  }

  /** Copy a layer onto the framebuffer that is the draw target (null: the screen). */
  const blit = (from: Target, to: WebGLFramebuffer | null) => {
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, from.fbo)
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, to)
    gl.blitFramebuffer(0, 0, width, height, 0, 0, width, height, gl.COLOR_BUFFER_BIT, gl.NEAREST)
    gl.bindFramebuffer(gl.FRAMEBUFFER, null)
  }

  const dropAurora = () => {
    if (!aurora) return
    gl.deleteFramebuffer(aurora.fbo)
    gl.deleteTexture(aurora.tex)
    aurora = null
  }

  /** The aurora's target: this tier's density (never more than the canvas has), cleared once. */
  const auroraTarget = (scene: Scene): Target => {
    const t = current as Tier
    const ratio = Math.min(t.aurora, width / cssW, height / cssH)
    const w = Math.max(1, Math.round(cssW * ratio))
    const h = Math.max(1, Math.round(cssH * ratio))
    const { horizon, r } = scene.limb
    if (aurora && aurora.w === w && aurora.h === h && auroraHorizon === horizon && auroraRadius === r) return aurora
    dropAurora()
    const made = framebuffer(
      gl,
      w,
      h,
      { internal: gl.SRGB8_ALPHA8, format: gl.RGBA, type: gl.UNSIGNED_BYTE },
      gl.LINEAR,
    )
    aurora = { fbo: made.fbo, tex: made.tex[0], w, h }
    auroraHorizon = horizon
    auroraRadius = r
    gl.bindFramebuffer(gl.FRAMEBUFFER, made.fbo)
    gl.clearColor(0, 0, 0, 1)
    gl.clear(gl.COLOR_BUFFER_BIT)
    return aurora
  }

  /** Upload the catalogue once it has come. */
  const uploadSky = (scene: Scene) => {
    const sky = scene.sky
    if (!sky || starsReady === sky.count) return
    const data = new Float32Array(sky.count * STAR_FLOATS)
    for (let i = 0; i < sky.count; i++) {
      data.set(sky.dirs.subarray(i * 3, i * 3 + 3), i * STAR_FLOATS)
      data[i * STAR_FLOATS + 3] = sky.vmag[i]
      data.set(scene.starColors.subarray(i * 3, i * 3 + 3), i * STAR_FLOATS + 4)
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, starBuffer)
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW)
    starsReady = sky.count
    countStars(scene)
    figureRev = -1
  }

  /** How many of the (brightest first) stars this tier draws. */
  const countStars = (scene: Scene) => {
    const sky = scene.sky
    if (!sky || !current) return
    let lo = 0
    let hi = sky.count
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (sky.vmag[mid] <= current.faintest) lo = mid + 1
      else hi = mid
    }
    starCount = lo
  }

  const uploadFigure = (scene: Scene) => {
    const sky = scene.sky
    if (!sky || figureRev === scene.figureRev) return
    const n = sky.lines.length / 2
    const data = new Float32Array(n * FIGURE_FLOATS)
    for (let k = 0; k < n; k++) {
      const a = sky.lines[k * 2]
      const b = sky.lines[k * 2 + 1]
      data.set(sky.dirs.subarray(a * 3, a * 3 + 3), k * FIGURE_FLOATS)
      data.set(sky.dirs.subarray(b * 3, b * 3 + 3), k * FIGURE_FLOATS + 3)
      data[k * FIGURE_FLOATS + 6] = scene.figureTimes[k * 2]
      data[k * FIGURE_FLOATS + 7] = scene.figureTimes[k * 2 + 1]
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, figureBuffer)
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW)
    figureRev = scene.figureRev
  }

  const uploadWriting = (scene: Scene) => {
    const w = scene.writing
    if (!w || writeRev === w.rev) return
    const data = new Float32Array(w.segments.length * SEGMENT_FLOATS)
    w.segments.forEach((s, i) => {
      data.set([s.x0, s.y0, s.x1, s.y1, s.t0, s.t1, s.ink0, s.ink1], i * SEGMENT_FLOATS)
    })
    gl.bindBuffer(gl.ARRAY_BUFFER, writeBuffer)
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW)
    segmentCount = w.segments.length
    writeRev = w.rev
  }

  // ---- uniforms common to the programs
  const geometry = (p: Program, scene: Scene, pxX: number, pxY: number) => {
    const { u } = p
    const l = scene.limb
    gl.uniform2f(u.uCss, scene.width, scene.height)
    gl.uniform2f(u.uPx, pxX, pxY)
    gl.uniform4f(u.uLimb, l.cx, l.cy, l.r, l.horizon)
    gl.uniform2f(u.uSpan, l.start, 1 / l.length)
  }
  const sky = (p: Program, scene: Scene, open: number) => {
    const { u } = p
    gl.uniformMatrix3fv(u.uViewInv, false, scene.view)
    gl.uniform4f(u.uFrame, scene.frame.scale, scene.frame.x, scene.frame.y, open)
    gl.uniform2f(u.uShift, scene.shift[0], scene.shift[1])
  }
  const bind = (unit: number, tex: WebGLTexture) => {
    gl.activeTexture(gl.TEXTURE0 + unit)
    gl.bindTexture(gl.TEXTURE_2D, tex)
  }
  const rgb = (loc: WebGLUniformLocation | null, c: readonly number[]) => gl.uniform3f(loc, c[0], c[1], c[2])
  // biome-ignore lint/correctness/useHookAtTopLevel: useProgram is WebGL's call, not a React hook
  const bindProgram = (p: Program) => gl.useProgram(p.program)

  // ---- the passes
  /** The world's regions are strips of radial quads: this many cells across the limb (the vertex shader gets it in uBand.w). */
  const BAND_CELLS = 128
  /** Draw a strip over altitudes `from`..`to` (CSS px) across the whole screen's width. */
  const band = (p: Program, scene: Scene, from: number, to: number) => {
    const l = scene.limb
    // radial edges that reach past the screen's sides at the strip's lowest, where the circle is narrowest
    const theta = Math.asin(Math.min(1, (cssW / 2 + 40) / (l.r + Math.min(from, 0))))
    gl.uniform4f(p.u.uBand, from, to, theta, BAND_CELLS)
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 2 * (BAND_CELLS + 1))
  }

  const drawAurora = (scene: Scene) => {
    const t = current as Tier
    const target = auroraTarget(scene)
    gl.bindFramebuffer(gl.FRAMEBUFFER, target.fbo)
    gl.viewport(0, 0, target.w, target.h)
    bindProgram(auroraPass)
    const { u } = auroraPass
    geometry(auroraPass, scene, target.w / cssW, target.h / cssH)
    bind(0, stringTex)
    bind(1, noiseTex)
    bind(4, raysTex)
    gl.uniform1i(u.uString, 0)
    gl.uniform1i(u.uNoise, 1)
    gl.uniform1i(u.uRays, 4)
    gl.uniform4f(u.uLive, 0, scene.bumpCount, 0, 0)
    gl.uniform4f(u.uAur, scene.aurora, scene.auroraTop, t.sheets, scene.auroraDrift)
    gl.uniform4fv(u.uBump, scene.bumpsForShader())
    rgb(u.uAuLow, palette.auroraLow)
    rgb(u.uAuHigh, palette.auroraHigh)
    gl.uniform1f(u.uTime, scene.flicker)
    // from where the string can sink below the limb to the curtains' tallest top: only this strip is drawn
    band(auroraPass, scene, -(SOFT_LIMIT_PX + 4), scene.auroraTop)
    gl.bindFramebuffer(gl.FRAMEBUFFER, null)
  }

  /** What the sky's light needs, for the far sky and for the near band. */
  const skyLight = (p: Program, scene: Scene) => {
    const { u } = p
    geometry(p, scene, width / cssW, height / cssH)
    sky(p, scene, scene.sky ? smooth(scene.skyOpen) : 0)
    const [x, y] = shiftOf(scene) // the background's own shift: a quarter pixel at a time (the stars have the exact one)
    gl.uniform2f(u.uShift, x, y)
    bind(1, noiseTex)
    bind(3, milkyWayTex)
    gl.uniform1i(u.uNoise, 1)
    gl.uniform1i(u.uMilkyWay, 3)
    if (milkyWayBaked) gl.uniform4f(u.uMW, MW_MARGIN, 1 / (cssW + 2 * MW_MARGIN), 1 / (cssH + 2 * MW_MARGIN), MW_PEAK)
    gl.uniform4f(u.uGlow, scene.glow, scene.voice, scene.mic, scene.sweep)
    const c = scene.colours
    rgb(u.uColB, c.b)
    rgb(u.uUpB, c.upB)
    rgb(u.uSky, palette.sky)
    rgb(u.uNebula, palette.nebula)
    rgb(u.uDust, palette.dust)
    gl.uniform4f(u.uM42, scene.m42[0], scene.m42[1], scene.m42[2], scene.m42[3])
  }

  /** The deep world is a clear, and above the near band the far sky is one strip. */
  const drawFar = (scene: Scene, reach: number) => {
    gl.clearColor(ground[0], ground[1], ground[2], 1)
    gl.clear(gl.COLOR_BUFFER_BIT)
    const top = scene.limb.horizon + 8
    if (top > reach) {
      bindProgram(skyPass)
      skyLight(skyPass, scene)
      band(skyPass, scene, reach, top)
    }
  }

  /** The band round the limb, eighty px below it to `reach` above (220, taller while the aurora shows): the glow's work. */
  const drawNear = (scene: Scene, seen: boolean, reach: number) => {
    bindProgram(nearPass)
    const { u } = nearPass
    skyLight(nearPass, scene)
    bind(0, stringTex)
    gl.uniform1i(u.uString, 0)
    gl.uniform4f(
      u.uLive,
      scene.stringLive ? 1 : 0,
      scene.bumpCount,
      scene.mic > 0.002 ? 1 : 0,
      scene.sweep > -1000 ? 1 : 0,
    )
    if (seen && aurora) {
      bind(2, aurora.tex)
      gl.uniform1i(u.uAurora, 2)
    }
    gl.uniform2f(u.uAurSeen, seen ? 1 : 0, scene.auroraTop)
    gl.uniform1f(u.uPatch, quantised(scene.patch, PATCH_STEPS))
    const c = scene.colours
    rgb(u.uColA, c.a)
    rgb(u.uUpA, c.upA)
    gl.uniform3f(u.uWipe, scene.talkArc, Math.min(scene.wipeFront, WIPE_DONE), WIPE_SOFT)
    gl.uniform4fv(u.uBump, scene.bumpsForShader())
    rgb(u.uGround, palette.ground)
    band(nearPass, scene, -80, Math.min(reach, scene.limb.horizon + 8))
  }

  /**
   * The background. A still frame (reduced motion) is drawn once, straight to the screen. Otherwise the far layer is
   * drawn into a texture when what it is drawn from changes, and a frame is a copy of it with the near band drawn live
   * over it; while nothing in the background moves, even that is a texture of its own, and a frame is one copy.
   */
  const drawBackground = (scene: Scene, seen: boolean, twinkle: boolean) => {
    const reach = seen ? Math.max(scene.auroraTop + 8, 220) : 220
    if (!twinkle) {
      drawFar(scene, reach)
      drawNear(scene, seen, reach)
      return
    }
    far.used = scene.time
    const farTarget = bake(far, signFar(scene, reach), () => drawFar(scene, reach))
    if (seen || !scene.backgroundStill()) {
      blit(farTarget, null)
      drawNear(scene, seen, reach)
      return
    }
    full.used = scene.time
    const fullTarget = bake(full, signFull(scene, reach), () => {
      blit(farTarget, (full.target as Target).fbo)
      gl.bindFramebuffer(gl.FRAMEBUFFER, (full.target as Target).fbo)
      drawNear(scene, false, reach)
    })
    blit(fullTarget, null)
  }

  const drawStars = (scene: Scene, twinkle: boolean) => {
    const open = smooth(scene.skyOpen)
    if (!scene.sky || open <= 0) return
    gl.enable(gl.BLEND)
    gl.blendEquation(gl.FUNC_ADD)
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_COLOR)
    bindProgram(starPass)
    const { u } = starPass
    geometry(starPass, scene, width / cssW, height / cssH)
    sky(starPass, scene, open)
    gl.uniform4f(u.uStar, STAR_SIGMA, STAR_EXPOSURE * open, WING_SHARE, WING_WIDTH)
    gl.uniform1f(u.uCut, STAR_CUTOFF)
    gl.uniform1f(u.uTime, scene.flicker)
    gl.uniform1f(u.uTwinkle, twinkle ? 1 : 0)
    gl.bindVertexArray(starVao)
    gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, starCount)
    const lit = scene.recallsForShader(recallData)
    if (lit > 0) {
      gl.bindBuffer(gl.ARRAY_BUFFER, recallBuffer)
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, recallData, 0, lit * RECALL_FLOATS)
      gl.bindVertexArray(recallVao)
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, lit)
    }
    gl.bindVertexArray(null)
  }

  /** The strokes' common uniforms. */
  const strokes = (
    p: Program,
    t: number,
    colour: readonly number[],
    stroke: readonly number[],
    ink: readonly number[],
  ) => {
    const { u } = p
    gl.uniform1f(u.uT, t)
    rgb(u.uColour, colour)
    gl.uniform4f(u.uStroke, stroke[0], stroke[1], stroke[2], stroke[3])
    gl.uniform3f(u.uInk, ink[0], ink[1], ink[2])
  }

  const drawFigure = (scene: Scene) => {
    const sk = scene.sky
    if (!sk || scene.figureAlpha < 0.003 || scene.skyOpen <= 0) return
    uploadFigure(scene)
    bindProgram(figurePass)
    const { u } = figurePass
    geometry(figurePass, scene, width / cssW, height / cssH)
    sky(figurePass, scene, 1)
    gl.uniform1f(u.uMargin, 6)
    gl.uniform1f(u.uGap, 7)
    strokes(figurePass, scene.figureClock, palette.figure, [0.5, 0, 1, 0.38 * scene.figureAlpha], [0, 1, 0.9])
    gl.bindVertexArray(figureVao)
    gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, sk.lines.length / 2)
    gl.bindVertexArray(null)
  }

  const drawWriting = (scene: Scene) => {
    const w = scene.writing
    const gain = scene.writeGain()
    if (!w || gain <= 0) return
    uploadWriting(scene)
    bindProgram(writePass)
    const { u } = writePass
    geometry(writePass, scene, width / cssW, height / cssH)
    gl.uniform1f(u.uMargin, 16)
    gl.uniform2f(u.uOrigin, w.origin[0], w.origin[1])
    strokes(writePass, scene.time - w.t0, w.colour, [0.8, 0.08, 3.2, gain], [1.2, 0.35, 2.2])
    gl.bindVertexArray(writeVao)
    gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, 4, segmentCount)
    gl.bindVertexArray(null)
  }

  const drawMeteors = (scene: Scene) => {
    const n = scene.meteorsForShader(meteorData)
    if (n === 0) return
    gl.enable(gl.BLEND)
    gl.blendEquation(gl.FUNC_ADD)
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_COLOR)
    bindProgram(meteorPass)
    const { u } = meteorPass
    geometry(meteorPass, scene, width / cssW, height / cssH)
    gl.uniform2f(u.uTrail, METEOR_TRAIL_S, METEOR_FADE_S)
    gl.bindBuffer(gl.ARRAY_BUFFER, meteorBuffer)
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, meteorData, 0, n * METEOR_FLOATS)
    gl.bindVertexArray(meteorVao)
    gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, METEOR_VERTICES, n)
    gl.bindVertexArray(null)
  }

  let profile: Renderer["profile"] = null
  const mark = (pass: string) => profile?.(pass)

  return {
    get profile() {
      return profile
    },
    set profile(hook) {
      profile = hook
    },
    size(w, h, cw, ch) {
      width = w
      height = h
      cssW = cw
      cssH = ch
      dropAurora() // its target follows the canvas
      dropLayer(far)
      dropLayer(full)
    },
    tier(t) {
      const changed = current?.aurora !== t.aurora
      current = t
      if (changed) dropAurora()
    },
    draw(scene, twinkle) {
      gl.viewport(0, 0, width, height)
      gl.disable(gl.BLEND)
      if (!scene.laid || !current) {
        gl.clearColor(0, 0, 0, 1)
        gl.clear(gl.COLOR_BUFFER_BIT)
        return
      }
      uploadSky(scene)
      bakeMilkyWay(scene)
      countStars(scene)
      if (scene.stringRev !== stringRev) {
        bind(0, stringTex)
        gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, NODES, 1, gl.RGBA, gl.FLOAT, scene.stringData)
        stringRev = scene.stringRev
      }
      const seen = scene.aurora > AURORA_SEEN
      if (seen) {
        auroraSeen = scene.time
        mark("aurora")
        drawAurora(scene)
      } else if (aurora && scene.time - auroraSeen > AURORA_KEEP_S) dropAurora()
      for (const layer of [far, full]) if (layer.target && scene.time - layer.used > CACHE_KEEP_S) dropLayer(layer)
      gl.viewport(0, 0, width, height)
      mark("background")
      drawBackground(scene, seen, twinkle)
      mark("stars")
      drawStars(scene, twinkle)
      gl.disable(gl.BLEND)
      if (scene.figureAlpha >= 0.003 || scene.writing) {
        mark("strokes")
        gl.enable(gl.BLEND)
        gl.blendEquation(gl.MAX)
        gl.blendFunc(gl.ONE, gl.ONE)
        drawFigure(scene)
        drawWriting(scene)
        gl.blendEquation(gl.FUNC_ADD)
      }
      mark("meteors")
      drawMeteors(scene)
      gl.disable(gl.BLEND)
      mark("")
    },
    dispose() {
      dropAurora()
      dropLayer(far)
      dropLayer(full)
      for (const p of [skyPass, nearPass, auroraPass, starPass, figurePass, writePass, meteorPass])
        gl.deleteProgram(p.program)
      for (const b of [starBuffer, recallBuffer, figureBuffer, writeBuffer, meteorBuffer]) gl.deleteBuffer(b)
      for (const v of [starVao, recallVao, figureVao, writeVao, meteorVao]) gl.deleteVertexArray(v)
      gl.deleteTexture(noiseTex)
      gl.deleteTexture(raysTex)
      gl.deleteTexture(milkyWayTex)
      gl.deleteTexture(stringTex)
    },
  }
}

/** A phase held to a number of steps (so a background drawn at one phase and one drawn at the next differ by a step). */
const quantised = (v: number, steps: number) => Math.floor(v * steps) / steps

/** The sky's shift for the background, a quarter of a CSS px at a time. */
const shiftOf = (scene: Scene): [number, number] => [
  Math.round(scene.shift[0] * SHIFT_STEPS) / SHIFT_STEPS,
  Math.round(scene.shift[1] * SHIFT_STEPS) / SHIFT_STEPS,
]

const smooth = (t: number) => {
  const x = Math.min(1, Math.max(0, t))
  return x * x * (3 - 2 * x)
}
