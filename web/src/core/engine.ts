/**
 * Horizon's engine: it runs in the worker (worker.ts) on the canvas the page hands over, or on the page itself where
 * a worker can't draw. It keeps the clock (60 frames a second while something moves, 24 while only the stars twinkle,
 * none while hidden; one still frame at a time for reduced motion), steps the quality with the frame times, loads the
 * star catalogue, and turns the page's messages into what the scene does (scene.ts) and the renderer draws
 * (render.ts).
 */
import { makePalette } from "./colors"
import { BANDS, type BenchTier, type EngineInit, type FromEngine, type ToEngine } from "./protocol"
import { ACTIVE_FPS, effectiveMs, Governor, IDLE_FPS, Pacer, TIERS } from "./quality"
import { createRenderer, type Renderer } from "./render"
import { Scene } from "./scene"
import { decode } from "./sky"
import { synthLevel } from "./synth"

export interface Engine {
  handle(message: ToEngine): void
}

/** The longest step the scene takes at once (a longer frame takes two). */
const MAX_STEP = 0.05
/** The demo's synthetic levels come this often (s), as the HUD's do. */
const SYNTH_EVERY = 1 / 30

/**
 * Start the engine on a canvas.
 * @param canvas the canvas (an OffscreenCanvas in the worker)
 * @param init how it starts
 * @param emit what it tells the page
 * @returns the engine, which takes the page's messages
 * @throws Error when WebGL 2 is not available, or palette.css lacks a token Horizon reads
 */
export function startEngine(
  canvas: HTMLCanvasElement | OffscreenCanvas,
  init: EngineInit,
  emit: (message: FromEngine) => void,
): Engine {
  const context = canvas.getContext("webgl2", {
    alpha: false,
    antialias: false,
    depth: false,
    stencil: false,
    premultipliedAlpha: true,
    preserveDrawingBuffer: false,
    powerPreference: "low-power", // the integrated GPU where there are two: the other is busy with his voice and models
  }) as WebGL2RenderingContext | null
  if (!context) throw new Error("WebGL 2 is not available")
  const gl: WebGL2RenderingContext = context

  const palette = makePalette()
  const governor = new Governor(init.quality, init.debug.tier)
  const scene = new Scene(palette, init.reduced)
  let quality = init.quality
  let reduced = init.reduced
  let visible = init.visible
  let disposed = false
  let lost = false
  let frozen = false // the demo's `advance` took the clock
  const synth = init.debug.synth === true

  let renderer: Renderer
  const build = () => {
    renderer = createRenderer(gl, palette)
    renderer.tier(TIERS[governor.tier])
  }
  build()

  // ---------------------------------------------------------------- size
  let cssW = Math.max(1, init.width)
  let cssH = Math.max(1, init.height)
  let dpr = init.dpr > 0 ? init.dpr : 1
  function resize() {
    const ratio = Math.min(dpr, TIERS[governor.tier].dpr)
    const w = Math.max(1, Math.round(cssW * ratio))
    const h = Math.max(1, Math.round(cssH * ratio))
    if (canvas.width !== w) canvas.width = w
    if (canvas.height !== h) canvas.height = h
    renderer.size(w, h, cssW, cssH)
    scene.size(cssW, cssH)
  }

  // ---------------------------------------------------------------- the sky
  // a debug `advance` or `bench` waits for the catalogue (a screenshot before the sky has come would show none)
  let loading = true
  let waiting: ToEngine | null = null
  fetch(init.stars)
    .then((r) => {
      if (!r.ok) throw new Error(`${init.stars}: ${r.status}`)
      return r.arrayBuffer()
    })
    .then((buffer) => {
      if (disposed) return
      scene.setSky(decode(buffer))
    })
    .catch((error) =>
      emit({ type: "problem", message: `the star catalogue didn't load: ${String(error?.message ?? error)}` }),
    )
    .finally(() => {
      loading = false
      poke()
      const asked = waiting
      waiting = null
      if (asked) handle(asked)
    })

  // ---------------------------------------------------------------- synthetic levels (the demo's)
  const bands = new Float32Array(BANDS)
  let synthAt = 0
  function feed() {
    if (!synth || scene.time - synthAt < SYNTH_EVERY) return
    synthAt = scene.time
    if (scene.state === "listening") scene.level(0, synthLevel(scene.time + 3.1, bands), bands)
    else if (scene.state === "speaking") scene.level(1, synthLevel(scene.time, bands), bands)
  }

  // ---------------------------------------------------------------- the GPU's own time per frame
  const timer = gl.getExtension("EXT_disjoint_timer_query_webgl2")
  const queries: WebGLQuery[] = []
  let timing: WebGLQuery | null = null
  /** The GPU's time per frame, smoothed (ms), from queries read a frame or two late so that nothing waits; null until one comes. */
  let gpuMs: number | null = null

  const gpuBegin = () => {
    if (!timer || queries.length >= 4) return
    timing = gl.createQuery()
    if (timing) gl.beginQuery(timer.TIME_ELAPSED_EXT, timing)
  }
  const gpuEnd = () => {
    if (!timer || !timing) return
    gl.endQuery(timer.TIME_ELAPSED_EXT)
    queries.push(timing)
    timing = null
  }
  const gpuPoll = () => {
    const oldest = queries[0]
    if (!timer || !oldest || !gl.getQueryParameter(oldest, gl.QUERY_RESULT_AVAILABLE)) return
    queries.shift()
    if (!gl.getParameter(timer.GPU_DISJOINT_EXT)) {
      const ms = (gl.getQueryParameter(oldest, gl.QUERY_RESULT) as number) / 1e6
      gpuMs = gpuMs === null ? ms : gpuMs + (ms - gpuMs) * 0.1
    }
    gl.deleteQuery(oldest)
  }
  /** A new tier is a new cost: forget the old one's. */
  const gpuForget = () => {
    gpuMs = null
  }

  // ---------------------------------------------------------------- the clock
  const raf: (fn: (ms: number) => void) => number =
    typeof requestAnimationFrame === "function"
      ? (fn) => requestAnimationFrame(fn)
      : (fn) => setTimeout(() => fn(performance.now()), 16) as unknown as number
  const caf: (id: number) => void = typeof cancelAnimationFrame === "function" ? cancelAnimationFrame : clearTimeout
  let loop = 0
  let last = 0
  let drawnAt = 0
  const pacer = new Pacer()
  let wasActive = false
  let statsAt = 0
  let frames = 0
  let spent = 0 // ms between frames drawn
  let busy = 0 // ms of this thread's time the frames took

  const live = () => visible && !disposed && !lost && !reduced && !frozen && quality !== "off" && scene.laid

  function step(dt: number) {
    const steps = dt > MAX_STEP ? 2 : 1
    for (let i = 0; i < steps; i++) {
      scene.update(dt / steps)
      feed()
    }
  }

  function frame(ms: number) {
    loop = 0
    if (!live()) return
    loop = raf(frame)
    if (!last) {
      last = drawnAt = ms
      return
    }
    const delta = ms - last
    last = ms
    const active = scene.active()
    if (!pacer.due(delta, active ? ACTIVE_FPS : IDLE_FPS)) return
    const gap = ms - drawnAt
    drawnAt = ms
    const dt = Math.min(0.1, gap / 1000)
    gpuPoll()
    if (active && wasActive && governor.frame(effectiveMs(gap, gpuMs))) {
      tier()
      gpuForget()
    }
    wasActive = active
    const t0 = performance.now()
    step(dt)
    gpuBegin()
    renderer.draw(scene, true)
    gpuEnd()
    busy += performance.now() - t0
    frames++
    spent += gap
    if (ms - statsAt > 2000) {
      emit({
        type: "stats",
        fps: Math.round((frames * 1000) / (ms - statsAt)),
        ms: frames ? Math.round((spent / frames) * 10) / 10 : 0,
        cpu: frames ? Math.round((busy / frames) * 100) / 100 : 0,
        gpu: gpuMs === null ? null : Math.round(gpuMs * 100) / 100,
        tier: governor.tier,
      })
      statsAt = ms
      frames = 0
      spent = 0
      busy = 0
    }
  }

  /** Start the loop if it should run; a still frame where it shouldn't but something must show. */
  function go() {
    if (live()) {
      if (!loop) {
        last = 0
        pacer.reset()
        wasActive = false
        governor.reset()
        loop = raf(frame)
      }
      return
    }
    if (loop) caf(loop)
    loop = 0
    if (reduced && visible && !disposed && !lost && !frozen && quality !== "off") still()
  }

  /** Something changed: the clock starts if it should now run and doesn't; a still one needs a frame. */
  const poke = go

  // a still frame (reduced motion): the scene where it comes to rest, drawn once (coalesced to one per animation frame)
  let stillFrame = 0
  function still() {
    if (stillFrame || disposed) return
    stillFrame = raf(() => {
      stillFrame = 0
      if (!visible || disposed || lost || frozen || quality === "off") return
      scene.settle()
      renderer.draw(scene, false)
    })
  }

  function tier() {
    renderer.tier(TIERS[governor.tier])
    resize()
  }

  // ---------------------------------------------------------------- a lost context comes back
  canvas.addEventListener("webglcontextlost", (event) => {
    event.preventDefault()
    lost = true
    if (loop) caf(loop)
    loop = 0
  })
  canvas.addEventListener("webglcontextrestored", () => {
    lost = false
    build()
    resize()
    go()
  })

  // ---------------------------------------------------------------- the bench's GPU timer
  /**
   * The GPU's own time per frame, by pass: `frames` frames drawn back to back, each pass inside a TIME_ELAPSED query
   * (the renderer's profile hook ends one and begins the next), the results read once the browser has them.
   * @param frames how many frames to draw
   * @returns ms per frame in total and by pass, or null without EXT_disjoint_timer_query_webgl2 (or if the GPU's
   *   clock was disturbed meanwhile)
   */
  async function gpuTime(frames: number): Promise<BenchTier["gpu"]> {
    const ext = gl.getExtension("EXT_disjoint_timer_query_webgl2")
    if (!ext) return null
    const timed: { pass: string; query: WebGLQuery }[] = []
    let open = false
    gl.getParameter(ext.GPU_DISJOINT_EXT) // forget an earlier disturbance
    renderer.profile = (pass) => {
      if (open) gl.endQuery(ext.TIME_ELAPSED_EXT)
      open = false
      const query = pass ? gl.createQuery() : null
      if (!query) return
      gl.beginQuery(ext.TIME_ELAPSED_EXT, query)
      open = true
      timed.push({ pass, query })
    }
    for (let i = 0; i < frames; i++) {
      step(1 / 60)
      renderer.draw(scene, true)
    }
    renderer.profile = null
    const passes: Record<string, number> = {}
    const last = timed[timed.length - 1]?.query
    const until = performance.now() + 5000
    // the last query finishing means the rest have
    while (last && !gl.getQueryParameter(last, gl.QUERY_RESULT_AVAILABLE) && performance.now() < until)
      await new Promise((resolve) => setTimeout(resolve, 2))
    let good = !gl.getParameter(ext.GPU_DISJOINT_EXT)
    for (const { pass, query } of timed) {
      if (!gl.getQueryParameter(query, gl.QUERY_RESULT_AVAILABLE)) good = false
      else passes[pass] = (passes[pass] ?? 0) + (gl.getQueryParameter(query, gl.QUERY_RESULT) as number) / 1e6
      gl.deleteQuery(query)
    }
    if (!good) return null
    let total = 0
    for (const pass of Object.keys(passes)) {
      passes[pass] /= Math.max(1, frames)
      total += passes[pass]
    }
    return { total, passes }
  }

  /**
   * Time whole frames at every tier at this size: a step and a draw at a time with the GPU waited for after each (a
   * pixel read back), then the GPU's own time by pass (timer queries, read after the task ends: WebGL makes their
   * results available only then).
   * @param frames how many frames per tier
   */
  async function bench(frames: number) {
    frozen = true
    if (loop) caf(loop)
    loop = 0
    const was = governor.tier
    const tiers: BenchTier[] = []
    const pixel = new Uint8Array(4)
    const fence = () => gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel)
    for (let t = 0; t < TIERS.length && !disposed; t++) {
      governor.tier = t
      tier()
      for (let i = 0; i < 8; i++) {
        step(1 / 60)
        renderer.draw(scene, true)
      }
      fence()
      const start = performance.now()
      for (let i = 0; i < frames; i++) {
        step(1 / 60)
        renderer.draw(scene, true)
        fence()
      }
      const ms = (performance.now() - start) / Math.max(1, frames)
      tiers.push({ tier: t, ms, gpu: await gpuTime(frames) })
    }
    if (disposed) return
    governor.tier = was
    tier()
    renderer.draw(scene, true)
    emit({ type: "bench", size: [canvas.width, canvas.height], tiers })
  }

  // ---------------------------------------------------------------- the page's messages
  let writingTimer: ReturnType<typeof setTimeout> | undefined
  const levels = new Float32Array(BANDS)
  function handle(m: ToEngine) {
    if (disposed) return
    switch (m.type) {
      case "size":
        cssW = Math.max(1, m.width)
        cssH = Math.max(1, m.height)
        dpr = m.dpr > 0 ? m.dpr : 1
        resize()
        poke()
        break
      case "layout":
        scene.layout(m.horizon, m.talkX, m.talkY)
        poke()
        break
      case "state":
        scene.setState(m.state)
        poke()
        break
      case "level": {
        if (reduced) break
        for (let i = 0; i < BANDS; i++) levels[i] = m.data[2 + i] ?? 0
        scene.level(m.data[0] === 0 ? 0 : 1, m.data[1], levels)
        break
      }
      case "pointer":
        if (!reduced) scene.pointer(m.x, m.y)
        break
      case "meteor":
        if (!reduced && quality !== "off") scene.meteor(m.hue, m.x, m.y)
        break
      case "recall":
        if (!reduced && quality !== "off" && m.count > 0) scene.recall(m.count)
        break
      case "write":
        if (quality === "off") break
        scene.write(m.text, m.shape, m.ms)
        if (reduced) {
          // no pen: the writing stands, complete, for as long as it is held
          clearTimeout(writingTimer)
          if (m.ms === 0) {
            if (scene.writing && scene.writing.fadeAt <= scene.time) scene.endWriting()
          } else
            writingTimer = setTimeout(() => {
              scene.endWriting()
              still()
            }, m.ms)
        }
        poke()
        break
      case "rest":
        scene.rest(m.on)
        poke()
        break
      case "quality":
        // the same again (a page that sends it on every start) keeps the tier it has adapted to
        if (m.quality !== quality) {
          quality = m.quality
          governor.prefer(m.quality, init.debug.tier)
          tier()
          if (loop) caf(loop)
          loop = 0
        }
        go()
        break
      case "visible":
        visible = m.on
        go()
        break
      case "motion":
        reduced = m.reduced
        scene.reduced = reduced
        go()
        break
      case "advance": {
        if (loading) {
          waiting = m
          break
        }
        frozen = true
        if (loop) caf(loop)
        loop = 0
        const n = Math.round(Math.max(0, m.seconds) * 60)
        for (let i = 0; i < n; i++) {
          scene.update(1 / 60)
          feed()
        }
        renderer.draw(scene, true)
        emit({ type: "advanced" })
        break
      }
      case "bench":
        if (loading) waiting = m
        else void bench(m.frames)
        break
      case "dispose":
        disposed = true
        if (loop) caf(loop)
        if (stillFrame) caf(stillFrame)
        clearTimeout(writingTimer)
        renderer.dispose()
        gl.getExtension("WEBGL_lose_context")?.loseContext()
        break
    }
  }

  resize()
  emit({ type: "ready", tier: governor.tier })
  go()
  return { handle }
}
