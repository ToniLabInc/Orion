/**
 * Horizon's demo page (dev only: Vite serves web/core-demo.html; the build leaves it out). Every call of the Core's
 * API by hand, a mock composer and conversation card to judge contrast on the world, sine speech for the microphone
 * and his voice, and knobs in the query string for screenshots:
 *
 *   state=thinking   rest=1   write=12:41   shape=question|check   hold=4000 (a writing's hold, ms)
 *   meteor=1 (to the card)   recall=3   quality=auto|high|low|off   worker=0 (draw on the page)   tier=0..3
 *   synth=1 (the engine feeds itself speech-like levels while listening and speaking)   twice=1 (a Core started and
 *   disposed on the canvas first, as React's strict mode does)   reduced=1   ui=0 (no controls)   card=0 (no mock
 *   conversation)   horizon=0.66 (the limb's top, a share of the height)
 *   advance=4 (run the clock this far, then hold that frame: the page's body gets data-advanced when it is drawn)
 *   bench=40 (time that many whole frames at each tier: wall time, and the GPU's own by pass where the browser has timer
 *   queries, in the page's title and a line of text)
 *
 * `window.demo.run({ ... })` does the same from a script (the screenshot driver's): it sets what it is given, advances
 * the clock, and resolves once the frame is drawn.
 */
import { hex } from "@/lib/palette"
import { connect } from "./client"
import type { CoreHue, CoreQuality, CoreState } from "./index"
import type { BenchTier } from "./protocol"
import { synthLevel } from "./synth"

const query = new URLSearchParams(location.search)
const num = (key: string, fallback: number) => {
  const v = query.get(key)
  return v === null || v === "" || Number.isNaN(Number(v)) ? fallback : Number(v)
}
const STATES: CoreState[] = ["idle", "listening", "thinking", "speaking", "loading", "error"]
const HUES: CoreHue[] = ["rigel", "nebula", "aurora", "betel", "alert", "ok"]
const QUALITIES: CoreQuality[] = ["auto", "high", "low", "off"]

const style = document.createElement("style")
style.textContent = `
html, body { margin: 0; height: 100%; overflow: hidden; background: ${hex("void-950")}; color: ${hex("steel-200")};
  font: 13px/1.45 system-ui, sans-serif; }
canvas.core { position: fixed; inset: 0; width: 100vw; height: 100vh; display: block; }
.card { position: fixed; box-sizing: border-box; border-radius: 20px; padding: 14px 18px;
  background: color-mix(in oklch, ${hex("void-850")} 58%, transparent);
  border: 1px solid color-mix(in oklch, ${hex("steel-300")} 12%, transparent); backdrop-filter: blur(18px);
  touch-action: none; }
.card h3 { margin: 0 0 8px; font-size: 11px; font-weight: 600; letter-spacing: 0.16em; text-transform: uppercase;
  color: ${hex("steel-400")}; cursor: grab; user-select: none; }
.card p { margin: 0 0 6px; color: ${hex("steel-200")}; font-size: 15px; }
.composer { position: fixed; display: flex; align-items: center; gap: 12px; box-sizing: border-box; padding: 0 22px;
  height: 52px; border-radius: 26px; background: color-mix(in oklch, ${hex("void-850")} 70%, transparent);
  border: 1px solid color-mix(in oklch, ${hex("steel-300")} 14%, transparent); color: ${hex("steel-300")}; }
.talk { position: fixed; width: 56px; height: 56px; margin: -28px 0 0 -28px; border-radius: 50%;
  border: 1px solid color-mix(in oklch, ${hex("steel-300")} 24%, transparent);
  background: color-mix(in oklch, ${hex("void-850")} 80%, transparent); }
.controls { right: 16px; top: 16px; width: 300px; max-height: calc(100vh - 32px); overflow: auto; }
.controls .row { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; align-items: center; }
.controls button, .controls select, .controls input { font: inherit; color: ${hex("steel-100")};
  background: color-mix(in oklch, ${hex("steel-300")} 10%, transparent);
  border: 1px solid color-mix(in oklch, ${hex("steel-300")} 18%, transparent); border-radius: 8px; padding: 4px 8px; }
.controls input[type=text], .controls input[type=number] { width: 70px; }
.controls input[type=range] { width: 120px; padding: 0; }
.controls button[aria-pressed=true] { border-color: ${hex("rigel-400")}; color: ${hex("rigel-400")}; }
.stats { font: 12px ui-monospace, monospace; color: ${hex("steel-400")}; }
`
document.head.append(style)

// ---------------------------------------------------------------- Horizon
const canvas = document.getElementById("core") as HTMLCanvasElement
const quality = (QUALITIES as string[]).includes(query.get("quality") ?? "")
  ? (query.get("quality") as CoreQuality)
  : "auto"
if (query.get("twice") === "1") connect(canvas, { quality, reducedMotion: false }).dispose()
const core = connect(
  canvas,
  { quality, reducedMotion: query.get("reduced") === "1" },
  {
    worker: query.get("worker") !== "0",
    tier: query.has("tier") ? num("tier", 2) : undefined,
    synth: query.get("synth") === "1",
  },
)

// ---------------------------------------------------------------- the mock HUD: talk button, composer, a conversation card
const talk = Object.assign(document.createElement("div"), { className: "talk" })
const composer = Object.assign(document.createElement("div"), {
  className: "composer",
  textContent: "Ask him anything",
})
const conversation = document.createElement("section")
conversation.className = "card"
conversation.dataset.id = "conversation"
conversation.innerHTML = `<h3>Orion</h3><p>The build is green. I pushed nothing: the command is on your clipboard.</p><p style="color:${hex("steel-400")}">Read 4 files, ran 2 commands.</p>`
document.body.append(talk, composer)
if (query.get("card") !== "0") document.body.append(conversation)

let horizonShare = num("horizon", 0.66)
function layout() {
  const horizon = Math.round(innerHeight * horizonShare)
  const talkAt = { x: innerWidth / 2, y: horizon + 6 }
  core.layout({ horizon, talk: talkAt })
  Object.assign(talk.style, { left: `${talkAt.x}px`, top: `${talkAt.y}px` })
  const width = Math.min(560, innerWidth - 32)
  Object.assign(composer.style, {
    left: `${(innerWidth - width) / 2}px`,
    top: `${horizon + 44}px`,
    width: `${width}px`,
  })
  const cw = Math.min(640, innerWidth - 32)
  Object.assign(conversation.style, { left: `${(innerWidth - cw) / 2}px`, top: `${horizon - 168}px`, width: `${cw}px` })
}
layout()
addEventListener("resize", layout)
addEventListener("pointermove", (e) =>
  core.pointer((e.clientX / innerWidth) * 2 - 1, (e.clientY / innerHeight) * 2 - 1),
)

/** Where a meteor flies: the middle of the conversation card, or somewhere in the sky. */
function target(): { x: number; y: number } {
  const r = conversation.getBoundingClientRect()
  if (r.width > 0) return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
  return { x: innerWidth * (0.2 + Math.random() * 0.6), y: innerHeight * 0.3 }
}

// ---------------------------------------------------------------- levels: a slider or sine speech, 30 a second
const levels = { mic: { on: false, value: 0 }, voice: { on: false, value: 0 } }
const bands = new Float32Array(16)
let clock = 0
setInterval(() => {
  clock += 1 / 30
  for (const source of ["mic", "voice"] as const) {
    const l = levels[source]
    if (l.on) core.level(source, synthLevel(clock + (source === "mic" ? 3.1 : 0), bands), [...bands])
    else if (l.value > 0) core.level(source, l.value, new Array(16).fill(l.value * 0.7))
  }
}, 1000 / 30)

// ---------------------------------------------------------------- the controls
function controls() {
  const panel = document.createElement("section")
  panel.className = "card controls"
  const row = (label: string) => {
    const r = document.createElement("div")
    r.className = "row"
    if (label) r.append(Object.assign(document.createElement("span"), { textContent: label }))
    panel.append(r)
    return r
  }
  const button = (parent: HTMLElement, text: string, fn: () => void) => {
    const b = Object.assign(document.createElement("button"), { textContent: text, type: "button" })
    b.addEventListener("click", fn)
    parent.append(b)
    return b
  }
  panel.append(Object.assign(document.createElement("h3"), { textContent: "Horizon" }))

  const states = row("")
  const stateButtons = STATES.map((s) =>
    button(states, s, () => {
      core.state(s)
      for (const b of stateButtons) b.setAttribute("aria-pressed", String(b.textContent === s))
    }),
  )

  for (const source of ["mic", "voice"] as const) {
    const r = row(source)
    const slider = Object.assign(document.createElement("input"), { type: "range", min: "0", max: "1", step: "0.01" })
    slider.value = "0"
    slider.addEventListener("input", () => {
      levels[source].value = Number(slider.value)
    })
    r.append(slider)
    const sine = button(r, "speech", () => {
      levels[source].on = !levels[source].on
      sine.setAttribute("aria-pressed", String(levels[source].on))
    })
  }

  const meteors = row("meteor")
  for (const hue of HUES) button(meteors, hue, () => core.meteor({ to: target(), hue }))
  button(meteors, "→ sky", () =>
    core.meteor({ to: { x: innerWidth * (0.15 + Math.random() * 0.7), y: innerHeight * 0.25 }, hue: "aurora" }),
  )

  const recall = row("recall")
  const count = Object.assign(document.createElement("input"), { type: "number", value: "3", min: "1", max: "8" })
  recall.append(count)
  button(recall, "recall", () => core.recall(Number(count.value)))

  const write = row("write")
  const text = Object.assign(document.createElement("input"), { type: "text", value: "12:41" })
  const hold = Object.assign(document.createElement("input"), { type: "number", value: "3000", step: "500" })
  write.append(text, hold)
  button(write, "text", () => core.write({ text: text.value, ms: Number(hold.value) }))
  const shapes = row("")
  button(shapes, "?", () => core.write({ shape: "question", ms: Number(hold.value) }))
  button(shapes, "✓", () => core.write({ shape: "check", ms: Number(hold.value) }))
  button(shapes, "release", () => core.write({ ms: 0 }))

  const rest = row("")
  let resting = false
  const restButton = button(rest, "rest", () => {
    resting = !resting
    core.rest(resting)
    restButton.setAttribute("aria-pressed", String(resting))
  })
  let shown = true
  const shownButton = button(rest, "visible", () => {
    shown = !shown
    core.visibility(shown)
    shownButton.setAttribute("aria-pressed", String(shown))
  })
  shownButton.setAttribute("aria-pressed", "true")

  const q = row("quality")
  const select = document.createElement("select")
  for (const v of QUALITIES) select.append(new Option(v, v, v === quality, v === quality))
  select.addEventListener("change", () => core.quality(select.value as CoreQuality))
  q.append(select)

  const h = row("limb")
  const slider = Object.assign(document.createElement("input"), {
    type: "range",
    min: "0.35",
    max: "0.9",
    step: "0.01",
  })
  slider.value = String(horizonShare)
  slider.addEventListener("input", () => {
    horizonShare = Number(slider.value)
    layout()
  })
  h.append(slider)

  const stats = Object.assign(document.createElement("div"), { className: "stats", textContent: "…" })
  panel.append(stats)
  canvas.addEventListener("corestats", (e) => {
    const s = (e as CustomEvent<{ fps: number; ms: number; cpu: number; gpu: number | null; tier: number }>).detail
    const gpu = s.gpu === null ? "" : ` · ${s.gpu} ms gpu`
    stats.textContent = `${s.fps} fps · ${s.ms} ms apart · ${s.cpu} ms cpu${gpu} · tier ${s.tier}`
    document.title = `${s.fps} fps · ${s.ms} ms · cpu ${s.cpu} ms${gpu} · tier ${s.tier}`
  })
  document.body.append(panel)
  panel.querySelector("h3")?.addEventListener("click", () => panel.classList.toggle("collapsed"))
}
if (query.get("ui") !== "0") controls()

// ---------------------------------------------------------------- what a script (or the query string) asks for
interface Run {
  state?: CoreState
  rest?: boolean
  write?: { text?: string; shape?: "question" | "check"; ms: number }
  meteor?: CoreHue | { to: { x: number; y: number }; hue: CoreHue }
  recall?: number
  mic?: boolean
  voice?: boolean
  seconds?: number
}

/** Set what is given, advance the clock, and say when the frame is drawn. */
function run(r: Run): Promise<void> {
  if (r.state) core.state(r.state)
  if (r.rest !== undefined) core.rest(r.rest)
  if (r.write) core.write(r.write)
  if (r.meteor) core.meteor(typeof r.meteor === "string" ? { to: target(), hue: r.meteor } : r.meteor)
  if (r.recall) core.recall(r.recall)
  if (r.mic !== undefined) levels.mic.on = r.mic
  if (r.voice !== undefined) levels.voice.on = r.voice
  if (r.seconds === undefined) return Promise.resolve()
  document.body.removeAttribute("data-advanced")
  return new Promise((resolve) => {
    canvas.addEventListener(
      "coreadvanced",
      () => {
        document.body.dataset.advanced = "true"
        resolve()
      },
      { once: true },
    )
    core.advance(r.seconds ?? 0)
  })
}
Object.assign(window, { core, demo: { run } })

const state = query.get("state") as CoreState | null
const shape = query.get("shape")
void run({
  state: state && STATES.includes(state) ? state : undefined,
  rest: query.get("rest") === "1" ? true : undefined,
  write: query.get("write")
    ? { text: query.get("write") ?? "", ms: num("hold", 4000) }
    : shape === "question" || shape === "check"
      ? { shape, ms: num("hold", 4000) }
      : undefined,
  meteor: query.get("meteor") === "1" ? "rigel" : undefined,
  recall: query.has("recall") ? num("recall", 3) : undefined,
  seconds: query.has("advance") ? num("advance", 0) : undefined,
})
if (query.has("bench")) {
  // the frame time at each tier, shown in the page's title and a line of text (read by --dump-dom)
  const out = Object.assign(document.createElement("pre"), { id: "bench", className: "stats" })
  Object.assign(out.style, { position: "fixed", left: "16px", bottom: "8px", margin: "0" })
  document.body.append(out)
  canvas.addEventListener("corebench", (e) => {
    const b = (e as CustomEvent<{ size: number[]; tiers: BenchTier[] }>).detail
    const gpu = (t: BenchTier) => (t.gpu ? ` (gpu ${t.gpu.total.toFixed(2)})` : "")
    out.textContent = `bench ${b.size.join("x")}: ${b.tiers.map((t) => `tier ${t.tier} ${t.ms.toFixed(1)} ms${gpu(t)}`).join(" | ")}`
    document.title = out.textContent
  })
  core.bench(num("bench", 30))
}
