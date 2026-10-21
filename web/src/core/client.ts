/**
 * The page's side of Horizon: `createCore` (index.ts) is this. It asks once what the browser can do (WebGL 2, WebGL 2
 * on an OffscreenCanvas), hands the canvas to a module worker that draws there (worker.ts, from our own origin), or
 * runs the same engine on the page where a worker can't draw, and turns the HUD's calls into messages (protocol.ts).
 * With quality "off", or where WebGL 2 is missing, it puts the SVG still (still.ts) beside the canvas instead and the
 * engine never starts. It tells the engine what it needs to know about the page by itself: the canvas's size and pixel
 * density, the tab hidden or shown, the reduced-motion wish. Nothing here loops: a pointer move is sent at the next
 * animation frame at most, and nothing else is sent unless the HUD calls.
 *
 * Units: layout, meteors in CSS pixels from the viewport's top left (getBoundingClientRect); the pointer −1..1, x to
 * the right and y down (clientX / innerWidth * 2 − 1, the same for y).
 */
import type { Core, CoreLayout, CoreOptions } from "./index"
import {
  type EngineDebug,
  type EngineInit,
  encodeLayout,
  encodeLevel,
  encodeMeteor,
  encodeWrite,
  type FromEngine,
  RECALL_MAX,
  type ToEngine,
} from "./protocol"
import { decode } from "./sky"
import { createStill, type Still } from "./still"

/** The demo's knobs, besides the engine's own: `worker: false` draws on the page. */
export interface DebugOptions extends EngineDebug {
  worker?: boolean
}

/** Horizon with the demo's extra: run its clock on to a moment and draw that one frame. */
export interface DebugCore extends Core {
  advance(seconds: number): void
  /** Time whole frames at each tier (the canvas gets a `corebench` event with the results). */
  bench(frames: number): void
}

/** Where the catalogue is (Vite gives it a hashed name of its own in a build). */
const STARS = new URL("./stars.bin", import.meta.url).href

/** What this browser can do, asked once with a throwaway context. */
function probe(): { webgl2: boolean; offscreen: boolean } {
  const ask = (c: HTMLCanvasElement | OffscreenCanvas): boolean => {
    const gl = c.getContext("webgl2") as WebGL2RenderingContext | null
    gl?.getExtension("WEBGL_lose_context")?.loseContext()
    return gl !== null
  }
  let offscreen = false
  let webgl2 = false
  try {
    if (typeof OffscreenCanvas !== "undefined") {
      offscreen = ask(new OffscreenCanvas(1, 1))
      webgl2 = offscreen
    }
  } catch {} // no WebGL on an OffscreenCanvas here
  if (!webgl2)
    try {
      webgl2 = ask(document.createElement("canvas"))
    } catch {}
  return { webgl2, offscreen }
}

/** Canvases handed to a worker already: a canvas can be handed over once, so a second Core on one (React's strict
 *  mode mounts twice in development) draws on a fresh canvas in its place. */
const handedOver = new WeakSet<HTMLCanvasElement>()

/** Messages that are state: an engine that takes over (the page after a worker that failed) starts from them. */
const KEEP: ReadonlySet<ToEngine["type"]> = new Set(["size", "layout", "state", "rest", "quality", "visible", "motion"])

/**
 * Start Horizon on a canvas.
 * @param canvas the full-viewport canvas behind the DOM (handed to a worker: draw nothing else on it)
 * @param options the quality and the reduced-motion wish
 * @param debug the demo's knobs (the HUD passes none)
 * @returns the Core
 */
export function connect(canvas: HTMLCanvasElement, options: CoreOptions, debug: DebugOptions = {}): DebugCore {
  const can = probe()
  const motion = matchMedia("(prefers-reduced-motion: reduce)")
  let quality = options.quality
  let shown = true
  let failed = !can.webgl2
  let disposed = false
  let started = false
  let surface = canvas // where the engine draws: a fresh canvas in its place when that one is a worker's
  let fresh: HTMLCanvasElement | null = null
  const canvasVisibility = canvas.style.visibility
  let worker: Worker | null = null
  let left = 0
  let top = 0
  let deliver: (message: ToEngine) => void = () => {}
  const latest = new Map<ToEngine["type"], ToEngine>()
  const { worker: useWorker, ...engineDebug } = debug
  let still: Still | null = null
  let stillLayout: Extract<ToEngine, { type: "layout" }> | null = null
  let stillState: Extract<ToEngine, { type: "state" }>["state"] = "idle"
  let stillRest = false
  let size = { width: 1, height: 1, dpr: 1 }

  const reduced = () => options.reducedMotion || motion.matches
  const visibleNow = () => shown && !document.hidden
  const measure = () => {
    const r = surface.getBoundingClientRect()
    left = r.left
    top = r.top
    size = {
      width: Math.max(1, Math.round(r.width)),
      height: Math.max(1, Math.round(r.height)),
      dpr: devicePixelRatio || 1,
    }
    return size
  }
  const start = (): EngineInit => ({
    ...measure(),
    quality,
    reduced: reduced(),
    visible: visibleNow(),
    stars: STARS,
    debug: engineDebug,
  })

  function post(message: ToEngine) {
    if (disposed) return
    if (KEEP.has(message.type)) latest.set(message.type, message)
    if (!failed) deliver(message)
  }

  function receive(message: FromEngine) {
    if (message.type === "failed") fail(message.error)
    else if (message.type === "problem") console.warn(`ORION: Horizon: ${message.message}`)
    else if (message.type === "advanced") canvas.dispatchEvent(new CustomEvent("coreadvanced"))
    else if (message.type !== "ready")
      canvas.dispatchEvent(new CustomEvent(message.type === "bench" ? "corebench" : "corestats", { detail: message }))
  }

  function fail(error: unknown) {
    failed = true
    deliver = () => {}
    console.warn("ORION: Horizon can't be drawn here, showing its still:", error)
    syncStill()
  }

  // ---------------------------------------------------------------- the still
  /** The still stands where the engine doesn't draw: switched off here, or no WebGL 2. */
  function syncStill() {
    if (disposed) return
    const wanted = failed || quality === "off"
    if (!wanted) {
      if (still) {
        still.dispose()
        still = null
        for (const c of [canvas, fresh]) if (c) c.style.visibility = canvasVisibility
      }
      return
    }
    if (still) return
    still = createStill()
    for (const c of [canvas, fresh]) if (c) c.style.visibility = "hidden"
    ;(fresh ?? canvas).after(still.element)
    const s = still
    fetch(STARS)
      .then((r) => {
        if (!r.ok) throw new Error(`${STARS}: ${r.status}`)
        return r.arrayBuffer()
      })
      .then((buffer) => {
        if (still === s) s.sky(decode(buffer))
      })
      .catch((e) => console.warn("ORION: Horizon's stars didn't load:", e))
    s.state(stillState)
    s.rest(stillRest)
    if (stillLayout) s.layout(size.width, size.height, stillLayout.horizon, stillLayout.talkX, stillLayout.talkY)
  }

  // ---------------------------------------------------------------- the engine
  /** A fresh canvas in the given one's place (that one belongs to a worker, now or from an earlier Core). */
  function replace() {
    const c = document.createElement("canvas")
    c.className = canvas.className
    c.style.cssText = canvas.style.cssText
    c.setAttribute("aria-hidden", "true")
    ;(fresh ?? canvas).after(c)
    fresh?.remove()
    canvas.style.display = "none"
    fresh = c
    surface = c
    sizes.observe(c)
  }

  /** The engine on this thread (on a fresh canvas when the one it has belongs to a worker by now). */
  function onPage() {
    if (handedOver.has(surface)) replace()
    worker = null
    // while the engine loads, what is said waits for it in order; levels and the pointer would be stale by then. It
    // starts from the latest state (some may have gone to a worker that failed).
    const early: ToEngine[] = []
    deliver = (m) => {
      if (m.type !== "level" && m.type !== "pointer" && early.length < 64) early.push(m)
    }
    import("./engine")
      .then(({ startEngine }) => {
        if (disposed || failed) return
        const engine = startEngine(surface, start(), receive)
        deliver = (m) => engine.handle(m)
        for (const m of latest.values()) engine.handle(m)
        for (const m of early) engine.handle(m)
      })
      .catch(fail)
  }

  function inWorker(): boolean {
    if (
      useWorker === false ||
      !can.offscreen ||
      !("transferControlToOffscreen" in surface) ||
      typeof Worker === "undefined"
    )
      return false
    let w: Worker
    let offscreen: OffscreenCanvas
    try {
      w = new Worker(new URL("./worker.ts", import.meta.url), { type: "module", name: "orion-core" })
    } catch {
      return false
    }
    try {
      offscreen = surface.transferControlToOffscreen()
    } catch {
      w.terminate() // the page's own code has a context on it: draw there instead
      return false
    }
    handedOver.add(surface)
    worker = w
    let settled = false
    const giveUp = (why: string) => {
      if (settled) return
      settled = true
      w.terminate()
      console.info("ORION: Horizon draws on the page:", why)
      onPage()
    }
    w.addEventListener("error", (event) => {
      event.preventDefault()
      giveUp(event.message || "the worker didn't start")
    })
    w.addEventListener("message", ({ data }: MessageEvent<FromEngine>) => {
      if (data.type === "failed" && !settled) return giveUp(data.error)
      if (data.type === "ready") settled = true
      receive(data)
    })
    w.postMessage({ type: "init", canvas: offscreen, init: start() }, [offscreen])
    for (const m of latest.values()) w.postMessage(m) // what the page was told before the engine was wanted
    deliver = (m) => w.postMessage(m)
    return true
  }

  /** Start the engine once, the first time it is wanted (never, for a Core that is only ever off). */
  function ensureEngine() {
    if (started || failed || disposed) return
    started = true
    if (handedOver.has(canvas) && !fresh) replace()
    if (!inWorker()) onPage()
  }

  // what the engine needs to know about the page, without being asked
  const sizes = new ResizeObserver(() => {
    const m = measure()
    post({ type: "size", ...m })
    if (stillLayout) still?.layout(m.width, m.height, stillLayout.horizon, stillLayout.talkX, stillLayout.talkY)
  })
  const onVisibility = () => post({ type: "visible", on: visibleNow() })
  const onMotion = () => post({ type: "motion", reduced: reduced() })
  let density: MediaQueryList | null = null
  const onDensity = () => {
    post({ type: "size", ...measure() })
    watchDensity()
  }
  function watchDensity() {
    density?.removeEventListener("change", onDensity)
    density = matchMedia(`(resolution: ${devicePixelRatio}dppx)`)
    density.addEventListener("change", onDensity, { once: true })
  }

  if (failed) console.warn("ORION: Horizon can't be drawn here: no WebGL 2")
  measure()
  if (quality !== "off") ensureEngine()
  syncStill()
  sizes.observe(surface)
  document.addEventListener("visibilitychange", onVisibility)
  motion.addEventListener("change", onMotion)
  watchDensity()

  let pointerX = 0
  let pointerY = 0
  let pointerFrame = 0

  return {
    state(state) {
      stillState = state
      still?.state(state)
      post({ type: "state", state })
    },
    level(source, value, bands) {
      post({ type: "level", data: encodeLevel(source, value, bands) })
    },
    pointer(x, y) {
      pointerX = x
      pointerY = y
      pointerFrame ||= requestAnimationFrame(() => {
        pointerFrame = 0
        post({ type: "pointer", x: pointerX, y: pointerY })
      })
    },
    layout(layout: CoreLayout) {
      const message = encodeLayout(layout, left, top)
      if (!message) return console.warn("ORION: Horizon's layout needs numbers:", layout)
      stillLayout = message
      still?.layout(size.width, size.height, message.horizon, message.talkX, message.talkY)
      post(message)
    },
    meteor(meteor) {
      const message = encodeMeteor(meteor, left, top)
      if (message) post(message)
      else console.warn("ORION: a meteor needs a point to fly to:", meteor)
    },
    recall(count) {
      if (Number.isFinite(count)) post({ type: "recall", count: Math.min(RECALL_MAX, Math.max(0, Math.floor(count))) })
      else console.warn("ORION: a recall needs a number of memories:", count)
    },
    write(write) {
      const message = encodeWrite(write)
      if ("error" in message) console.warn(`ORION: Horizon can't write that: ${message.error}`)
      else post(message)
    },
    rest(on) {
      stillRest = on
      still?.rest(on)
      post({ type: "rest", on })
    },
    quality(value) {
      quality = value
      syncStill()
      if (value !== "off") ensureEngine()
      post({ type: "quality", quality: value })
    },
    visibility(visible) {
      shown = visible
      post({ type: "visible", on: visibleNow() })
    },
    advance(seconds) {
      post({ type: "advance", seconds })
    },
    bench(frames) {
      post({ type: "bench", frames })
    },
    dispose() {
      if (disposed) return
      post({ type: "dispose" })
      disposed = true
      const w = worker
      if (w) setTimeout(() => w.terminate(), 250) // it closes itself; this is the safety net
      sizes.disconnect()
      document.removeEventListener("visibilitychange", onVisibility)
      motion.removeEventListener("change", onMotion)
      density?.removeEventListener("change", onDensity)
      if (pointerFrame) cancelAnimationFrame(pointerFrame)
      still?.dispose()
      if (fresh) {
        fresh.remove()
        canvas.style.display = ""
      }
      canvas.style.visibility = canvasVisibility
    },
  }
}
