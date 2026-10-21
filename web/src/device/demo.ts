// A dev-only page (web/device-demo.html; with `npm run dev`: http://127.0.0.1:5173/device-demo.html): this browser as
// a device, against a stand-in server that plays the microphone back as his voice, sent the way orion/voice/tts.py's
// RemoteOutput sends it (a sound's first four 40 ms blocks at once, then one every 40 ms). Buttons for a person;
// `?auto` runs a scripted check and logs `DEMO-RESULT {…}` to the console (a headless browser's smoke test).
import { MIC_RATE, VOICE_RATE } from "@/audio/constants"
import { encodeVoiceFrame } from "@/audio/frames"
import type { PlayerStats } from "@/audio/player"
import { StreamResampler } from "@/audio/resampler"
import type { DeviceSend, Shared } from "./index"
import { BrowserDevice } from "./link"

const BLOCK = 960 // 40 ms at 24 kHz
const LEAD = 4 // RemoteOutput's REMOTE_LEAD_BLOCKS
const PERIOD_MS = 40

/** The server's side of the voice, in the page: takes, and the microphone looped back as his voice. */
class StandIn implements DeviceSend {
  link: BrowserDevice | null = null
  holder = false
  gen = 0
  micFrames = 0
  /** Microphone frames that came while this tab didn't carry the voice (the link must never send them). */
  strayFrames = 0
  readonly messages: Record<string, unknown>[] = []
  /** The microphone's last second, for its strongest frequency. */
  readonly recent = new Float32Array(MIC_RATE)
  private recentAt = 0
  private readonly up = new StreamResampler(MIC_RATE, VOICE_RATE)
  private pieces: Int16Array[] = []
  private queued = 0
  private playing = false
  private timer = 0
  private due = 0

  json(message: Record<string, unknown>): boolean {
    this.messages.push(message)
    if (message.type === "voice_take") setTimeout(() => this.take(message.on === true), 60)
    return true
  }

  binary(data: ArrayBuffer): boolean {
    if (new DataView(data).getUint8(0) !== 1) return true
    const samples = new Float32Array(data.slice(1))
    if (!this.holder) {
      this.strayFrames++
      return true
    }
    this.micFrames++
    for (const s of samples) {
      this.recent[this.recentAt] = s
      this.recentAt = (this.recentAt + 1) % this.recent.length
    }
    const out = new Float32Array(this.up.maxOutput(samples.length))
    const n = this.up.process(samples, out)
    this.pieces.push(
      Int16Array.from(out.subarray(0, n), (v) => Math.max(-32768, Math.min(32767, Math.round(v * 32767)))),
    )
    this.queued += n
    if (!this.timer) this.tick()
    return true
  }

  /** An instant stop (RemoteOutput.abort): a new generation, said to the browser; the next sound leads again. */
  stop(): void {
    clearTimeout(this.timer)
    this.timer = 0
    this.playing = false
    this.gen++
    this.pieces = []
    this.queued = 0
    this.link?.flushSpeaker(this.gen)
  }

  /** Another device takes the voice. */
  moveAway(): void {
    this.holder = false
    this.link?.holder(false)
  }

  private take(on: boolean): void {
    this.holder = on
    this.link?.holder(on)
  }

  private tick = (): void => {
    this.timer = 0
    if (!this.playing) {
      if (this.queued < LEAD * BLOCK) return // a sound starts with its first 160 ms at once
      this.playing = true
      for (let i = 0; i < LEAD; i++) this.send()
      this.due = performance.now() + PERIOD_MS
    } else {
      while (performance.now() >= this.due) {
        if (!this.queued) {
          this.playing = false // the sound ended: silence isn't sent
          return
        }
        this.send()
        this.due += PERIOD_MS
      }
    }
    this.timer = window.setTimeout(this.tick, Math.max(0, this.due - performance.now()))
  }

  private send(): void {
    const block = new Int16Array(BLOCK)
    let filled = 0
    while (filled < BLOCK && this.pieces.length) {
      const piece = this.pieces[0]
      const take = Math.min(BLOCK - filled, piece.length)
      block.set(piece.subarray(0, take), filled)
      filled += take
      if (take === piece.length) this.pieces.shift()
      else this.pieces[0] = piece.subarray(take)
    }
    this.queued -= filled
    this.link?.playFrame(encodeVoiceFrame(this.gen, block))
  }
}

const server = new StandIn()
const link = new BrowserDevice(server)
server.link = link
const levels: number[] = []
link.onMicLevel((v) => levels.push(v))

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const stats = async () => (await link.voiceDebug().stats) as PlayerStats | null
const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e))

async function until(test: () => boolean, ms: number): Promise<boolean> {
  const end = performance.now() + ms
  while (!test()) {
    if (performance.now() > end) return false
    await sleep(10)
  }
  return true
}

/** The strongest frequency in the microphone's last second (50 Hz steps). */
function strongest(x: Float32Array, rate: number): number {
  let best = 0
  let bestPower = 0
  for (let f = 100; f < rate / 2; f += 50) {
    let re = 0
    let im = 0
    for (let n = 0; n < x.length; n++) {
      const a = (2 * Math.PI * f * n) / rate
      re += x[n] * Math.cos(a)
      im += x[n] * Math.sin(a)
    }
    const power = re * re + im * im
    if (power > bestPower) {
      bestPower = power
      best = f
    }
  }
  return best
}

async function pictureSize(url: string): Promise<string> {
  const img = new Image()
  img.src = url
  await img.decode()
  return `${img.naturalWidth}×${img.naturalHeight}`
}

/** Watch the player for `ms`: the jitter buffer's depth and what it did. */
async function watch(ms: number) {
  const held: number[] = []
  let last: PlayerStats | null = null
  for (let t = 0; t < ms; t += 100) {
    await sleep(100)
    last = await stats()
    if (last?.playing) held.push(last.heldMs)
  }
  const sorted = [...held].sort((a, b) => a - b)
  return {
    heldMs: { min: sorted[0], median: sorted[Math.floor(sorted.length / 2)], max: sorted.at(-1) },
    last,
  }
}

async function auto(): Promise<void> {
  const result: Record<string, unknown> = { name: link.name(), caps: link.caps() }
  const start = performance.now()
  await link.takeVoice()
  result.openMs = Math.round(performance.now() - start)
  result.carried = await until(() => link.carrying, 3000)
  result.agreedMs = Math.round(performance.now() - start)
  result.strayBeforeAgreement = server.strayFrames
  const context = link.voiceDebug().context
  result.context = context && {
    sampleRate: context.sampleRate,
    baseLatencyMs: Math.round(context.baseLatency * 1000),
    outputLatencyMs: Math.round((context.outputLatency || 0) * 1000),
  }
  const first = await watch(4000)
  result.loopback = first
  result.micFrames = server.micFrames
  result.micFramesPerSecond = Math.round((server.micFrames / ((performance.now() - start) / 1000)) * 10) / 10
  result.micStrongestHz = strongest(server.recent, MIC_RATE)
  result.levels = { count: levels.length, max: Math.max(0, ...levels).toFixed(3) }
  const before = await stats()
  server.stop() // the server cuts his voice: a new generation
  await sleep(30)
  const after = await stats()
  result.stop = { flushed: (after?.flushes ?? 0) - (before?.flushes ?? 0), heldMsAfter: after?.heldMs }
  result.afterStop = await watch(2000)
  const sent = server.micFrames
  server.moveAway() // another device takes the voice
  await sleep(300)
  result.moveAway = { state: link.voiceDebug().state, framesAfter: server.micFrames - sent, stray: server.strayFrames }
  await link.takeVoice()
  result.retaken = await until(() => link.carrying, 3000)
  link.releaseVoice()
  result.released = { state: link.voiceDebug().state, lastTake: server.messages.at(-1) }
  try {
    await link.handle({ id: "f1", kind: "frame", what: "screen" })
  } catch (e) {
    result.frameUnshared = messageOf(e)
  }
  for (const what of ["camera", "screen"] as Shared[]) {
    try {
      await link.share(what)
      const answer = await link.handle({ id: `f-${what}`, kind: "frame", what })
      result[what] = { frame: await pictureSize(String(answer.image)), bytes: String(answer.image).length }
      link.stopShare(what)
    } catch (e) {
      result[what] = messageOf(e)
    }
  }
  result.sharingSent = server.messages.filter((m) => m.type === "sharing").map((m) => m.what)
  const copy = link.handle({ id: "c1", kind: "clipboard_write", text: "Orion" })
  result.clipboardWrite = await Promise.race([
    copy.then(
      () => "copied",
      (e) => messageOf(e),
    ),
    sleep(1000).then(() => (link.pendingCopy ? "waits for a click" : "no answer")),
  ])
  link.pendingCopy?.dismiss()
  result.clipboardRead = await Promise.race([
    link.handle({ id: "c2", kind: "clipboard_read" }).then(
      (a) => `read: ${String(a.text)}`,
      (e) => messageOf(e),
    ),
    sleep(1000).then(() => "no answer"),
  ])
  console.log(`DEMO-RESULT ${JSON.stringify(result)}`)
}

/** The page for a person: buttons and what happens. */
function page(): void {
  const root = document.getElementById("demo")
  if (!root) return
  root.style.cssText = "font: 14px/1.5 system-ui, sans-serif; padding: 16px; max-width: 760px; margin: auto"
  const status = document.createElement("pre")
  const log = document.createElement("pre")
  const preview = document.createElement("img")
  preview.alt = ""
  preview.style.cssText = "max-width: 100%; display: block"
  const say = (line: string) => {
    log.textContent = `${new Date().toLocaleTimeString()}  ${line}\n${log.textContent ?? ""}`
  }
  const act = (label: string, fn: () => unknown) => {
    const button = document.createElement("button")
    button.type = "button"
    button.textContent = label
    button.style.margin = "0 6px 6px 0"
    button.onclick = async () => {
      try {
        const answer = await fn()
        say(`${label}: ${answer === undefined ? "done" : JSON.stringify(answer).slice(0, 200)}`)
      } catch (e) {
        say(`${label}: ${messageOf(e)}`)
      }
    }
    root.append(button)
  }
  const title = document.createElement("h1")
  title.textContent = `${link.name()} · ${link.caps().join(", ") || "nothing to offer here"}`
  title.style.fontSize = "18px"
  root.append(title)
  act("Take the voice", () => link.takeVoice())
  act("Talk", () => link.talk())
  act("Stop his voice", () => server.stop())
  act("Another device takes it", () => server.moveAway())
  act("Let go", () => link.releaseVoice())
  act("Share the screen", () => link.share("screen"))
  act("Share the camera", () => link.share("camera"))
  act("Stop sharing", () => link.stopShare())
  for (const what of ["screen", "camera"] as Shared[])
    act(`Frame of the ${what}`, async () => {
      const { image } = await link.handle({ id: "f", kind: "frame", what })
      preview.src = String(image)
      return pictureSize(String(image))
    })
  act("Orion asks to see the screen", () => link.handle({ id: "s", kind: "share", what: "screen" }))
  act("Share (the prompt's button)", () => link.pendingShare?.accept())
  act("Not now", () => link.pendingShare?.decline())
  act("Read the clipboard", () => link.handle({ id: "r", kind: "clipboard_read" }))
  act("Copy “Orion”", () => link.handle({ id: "w", kind: "clipboard_write", text: "Orion" }))
  act("Copy (the toast's button)", () => link.pendingCopy?.copy())
  root.append(status, preview, log)
  link.onPendingShare(() => say(`pending share: ${link.pendingShare?.what ?? "none"}`))
  link.onPendingCopy(() => say(`pending copy: ${link.pendingCopy?.text ?? "none"}`))
  link.onSharing((shared) => say(`sharing: ${shared.join(", ") || "nothing"}`))
  setInterval(async () => {
    const s = await stats()
    status.textContent = [
      `voice: ${link.voiceDebug().state}   mic level: ${(levels.at(-1) ?? 0).toFixed(2)}   frames sent: ${server.micFrames}`,
      s
        ? `player: holds ${s.heldMs} ms, speed ${s.speed}, drains ${s.drains}, skips ${s.skips}, flushes ${s.flushes}`
        : "",
    ].join("\n")
  }, 250)
}

page()
if (new URLSearchParams(location.search).has("auto"))
  auto().catch((e) => console.log(`DEMO-RESULT ${JSON.stringify({ failed: messageOf(e) })}`))
