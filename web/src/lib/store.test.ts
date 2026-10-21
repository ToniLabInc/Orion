import { describe, expect, it } from "vitest"
import { initial, type Live, reduce, TIMINGS_KEPT } from "./store"
import type { ServerEvent } from "./types"

const TOOL = { type: "tool", name: "web_search", title: "Search the web", server: "web", source: "brain", client: "" }
const ev = (event: object) => event as ServerEvent
const start = (id: string, turn = "a1", more = {}) => ev({ ...TOOL, phase: "start", id, turn, ...more })
const end = (id: string) => ev({ ...TOOL, phase: "end", id, turn: "a1", ok: true, ms: 812, result: "12°C" })
const memory = (more: object) => ev({ type: "memory_update", id: "m1", ok: true, profile: true, ...more })
const say = (type: string, id: string, text = "", error = false) => ev({ type: `assistant_${type}`, id, text, error })
const CC = "Claude Code"
const app = (id: string, ts: number) => start(id, "", { source: "app", client: CC, ts })

/** Run events through the reducer, all at one moment (a running step counts as busy until then). */
function run(events: ServerEvent[], from: Live = initial): Live {
  let s = from
  for (const event of events) s = { ...s, ...reduce(s, event, 1_000_000) }
  return s
}

describe("the trace", () => {
  it("puts each operation on its answer and ends it", () => {
    const s = run([say("start", "a1"), start("t1"), end("t1"), say("delta", "a1", "12"), say("done", "a1", "12 °C.")])
    expect(s.messages).toMatchObject([{ text: "12 °C.", streaming: false }])
    expect(s.messages[0].steps?.[0]).toMatchObject({ status: "ok", ms: 812, title: "Search the web" })
  })
  it("makes the answer of an early step once, and ends it on an apology", () => {
    const s = run([start("t1"), say("start", "a1"), say("done", "e1", "My apologies.", true)])
    expect(s.messages).toMatchObject([{ id: "a1", error: true, streaming: false }])
  })
  it("turns a memory call into its step, notes a later memory, and keeps background ones apart", () => {
    let s = run([say("start", "a1"), start("t1"), memory({ call: "t1" })])
    expect(s.messages[0].steps).toMatchObject([{ id: "t1", kind: "memory", title: "Memory saved" }])
    s = run([say("done", "a1", "Done."), memory({ id: "m2", turn: "a1", profile: false }), memory({ id: "m3" })], s)
    expect(s.messages[0].steps?.map((st) => st.title)).toEqual(["Memory saved", "Noted"])
    expect(s.messages[1]).toMatchObject({ role: "ops", source: "background", client: "Memory" })
  })
  it("groups another app's calls 10 s apart, starts again after you speak, and ignores the HUD's own", () => {
    let s = run([app("x1", 1000), app("x2", 1010)])
    expect(s.messages.map((m) => m.steps?.length)).toEqual([2])
    s = run([ev({ type: "user_message", text: "Hi", source: "hud" }), app("x3", 1012)], s)
    expect(s.messages.map((m) => m.role)).toEqual(["ops", "user", "ops"])
    expect(reduce(s, start("h1", "", { source: "hud" }), 1_000_000)).toBeUndefined()
  })
  it("restores a failed step, and one app's rows within 30 s as one group", () => {
    const step = (id: string, at: number, ok = true) => ({ id, kind: "tool", title: "Timer", at, ok })
    const ops = (id: string, ts: number) => ({ id: `ops-${id}`, role: "ops", ts, client: CC, steps: [step(id, ts)] })
    const asked = { id: "m1", role: "user", content: "Weather?", source: "hud", ts: 1000 }
    const answer = { id: "a1", role: "assistant", content: "Sorry.", ts: 1001, steps: [step("t1", 1000, false)] }
    const s = run([ev({ type: "init", history: [asked, answer, ops("x1", 1002), ops("x2", 1020)] })])
    expect(s.messages[1].steps?.[0]).toMatchObject({ status: "failed" })
    expect(s.messages.map((m) => m.steps?.map((st) => st.id))).toEqual([undefined, ["t1"], ["x1", "x2"]])
    expect(s.stepHome.x2).toBe("ops-x1")
  })
})

describe("the microphone", () => {
  it("shows up when his ears finish loading after the HUD connected, and goes if they fail", () => {
    const boot = (status: string) =>
      ev({ type: "boot", ready: status !== "pending", steps: [{ name: "ears", status }] })
    let s = run([ev({ type: "init", history: [], voice_available: false }), boot("pending")])
    expect(s.voiceAvailable).toBe(false)
    s = run([boot("ok")], s)
    expect(s.voiceAvailable).toBe(true)
    expect(run([boot("error")], s).voiceAvailable).toBe(false)
  })
})

describe("his questions before a risky action", () => {
  const question = { id: "q1", turn: "a1", tool: "run_command", title: "Run a shell command", expires: 2000 }
  it("waits in the list and on the step that asked, and leaves both when it is answered anywhere", () => {
    let s = run([say("start", "a1"), start("t1"), ev({ type: "confirm", ...question })])
    s = run(
      [ev({ type: "tool", phase: "ask", id: "t1", turn: "a1", name: "run_command", question: "q1", text: "Shall I?" })],
      s,
    )
    expect(s.confirms.map((c) => c.id)).toEqual(["q1"])
    expect(s.messages[0].steps?.[0].ask).toEqual({ question: "q1", text: "Shall I?" })
    s = run([ev({ type: "confirm", ...question })], s) // the same question again (a reconnect): once
    expect(s.confirms).toHaveLength(1)
    s = run([ev({ type: "confirm_done", id: "q1", answer: "no" })], s)
    expect(s.confirms).toEqual([])
    expect(s.messages[0].steps?.[0].ask).toBeUndefined()
  })
  it("comes with init after a reconnect, and an ended step asks no more", () => {
    let s = run([ev({ type: "init", history: [], confirms: [question] })])
    expect(s.confirms).toHaveLength(1)
    s = run([say("start", "a1"), start("t1")], s)
    s = run(
      [ev({ type: "tool", phase: "ask", id: "t1", turn: "a1", name: "x", question: "q1", text: "?" }), end("t1")],
      s,
    )
    expect(s.messages[0].steps?.[0]).toMatchObject({ status: "ok" })
    expect(s.messages[0].steps?.[0].ask).toBeUndefined()
  })
})

describe("the devices", () => {
  it("knows this tab, the HUDs connected and who carries the voice", () => {
    const phone = { id: "hud-2", device: "Safari on iPhone", local: false, caps: ["mic"], sharing: [], seen: 1 }
    let s = run([ev({ type: "init", history: [], surface: "hud-1", voice_holder: { device: "", name: "" } })])
    expect([s.surface, s.voiceHolder.device]).toEqual(["hud-1", ""])
    s = run(
      [ev({ type: "surfaces", items: [phone] }), ev({ type: "voice_holder", device: "hud-2", name: "Safari" })],
      s,
    )
    expect(s.surfaces).toEqual([phone])
    expect(s.voiceHolder).toEqual({ device: "hud-2", name: "Safari" })
  })
  it("shows the pictures you sent with a message", () => {
    const s = run([
      ev({ type: "user_message", text: "What's this?", source: "hud", images: ["data:image/jpeg;base64,AA"] }),
    ])
    expect(s.messages[0]).toMatchObject({ role: "user", images: ["data:image/jpeg;base64,AA"] })
  })
})

describe("jobs and alerts", () => {
  it("follows the jobs that run, and keeps one alert per id", () => {
    let s = run([
      ev({ type: "init", history: [], jobs: [], alerts: [] }),
      ev({ type: "job_run", id: 3, phase: "start" }),
    ])
    expect(s.running).toEqual([3])
    s = run([ev({ type: "job_run", id: 3, phase: "end", ok: true })], s)
    expect(s.running).toEqual([])
    const alert = { id: "battery", rule: "battery", level: "warn", title: "Battery low", text: "12% left", at: 1 }
    s = run([ev({ type: "alert", item: alert }), ev({ type: "alert", item: { ...alert, text: "9% left" } })], s)
    expect(s.alerts.map((a) => a.text)).toEqual(["9% left"])
  })
  it("knows an Orion without jobs from one with none", () => {
    expect(run([ev({ type: "init", history: [] })]).jobs).toBeNull()
    expect(run([ev({ type: "init", history: [], jobs: [] })]).jobs).toEqual([])
  })
})

describe("the news", () => {
  it("arrives with the first connection and stays fresh with each news event", () => {
    const headline = { title: "A headline", source: "Reuters", date: "2026-10-05T12:00:00+00:00", url: "https://e.x/1" }
    let s = run([ev({ type: "init", history: [] })])
    expect(s.news).toBeNull() // a Orion that never fetched them, or an older one
    s = run([ev({ type: "init", history: [], news: [headline] })])
    expect(s.news).toEqual([headline])
    s = run([ev({ type: "news", items: [{ ...headline, title: "Newer" }] })], s)
    expect(s.news?.map((n) => n.title)).toEqual(["Newer"])
  })
})

describe("what this round's events change", () => {
  const timing = (turn: string, more = {}) =>
    ev({
      type: "timing",
      turn,
      at: 1,
      source: "voice",
      brain: "Claude",
      instant: false,
      heard_ms: 400,
      think_ms: 1200,
      tools_ms: 0,
      speak_ms: 300,
      total_ms: 1900,
      ...more,
    })
  it("keeps each turn's stages by turn, the newest of a turn winning, and no more than it keeps", () => {
    let s = run([timing("a1"), timing("a2")])
    expect(Object.keys(s.timings)).toEqual(["a1", "a2"])
    s = run([timing("a1", { total_ms: 2500 })], s)
    expect(Object.keys(s.timings)).toEqual(["a2", "a1"])
    expect(s.timings.a1).toMatchObject({ total_ms: 2500, brain: "Claude" })
    expect(s.timings.a1).not.toHaveProperty("type")
    s = run(
      Array.from({ length: TIMINGS_KEPT + 20 }, (_, i) => timing(`t${i}`)),
      s,
    )
    expect(Object.keys(s.timings)).toHaveLength(TIMINGS_KEPT)
    expect(s.timings.a2).toBeUndefined()
    expect(s.timings[`t${TIMINGS_KEPT + 19}`]).toBeDefined()
  })
  it("keeps the stages across a reconnect, and takes the spend the init brings", () => {
    const s = run([timing("a1"), ev({ type: "init", history: [], spend: { today: 0.42, budget: 1 } })])
    expect(s.timings.a1).toBeDefined()
    expect(s.spend).toEqual({ today: 0.42, budget: 1 })
    expect(run([ev({ type: "init", history: [] })]).spend).toBeNull()
  })
  it("follows the spend as it grows", () => {
    expect(run([ev({ type: "spend", today: 0.5, budget: 2 })]).spend).toEqual({ today: 0.5, budget: 2 })
  })
  it("marks an answer an instant command gave, whichever event brings the mark first", () => {
    let s = run([say("start", "a1")])
    expect(s.messages[0].instant).toBeUndefined()
    s = run(
      [
        ev({ type: "assistant_start", id: "a1", instant: true }),
        ev({ type: "assistant_done", id: "a1", text: "Done." }),
      ],
      s,
    )
    expect(s.messages).toMatchObject([{ id: "a1", instant: true, text: "Done.", streaming: false }])
    s = run([ev({ type: "assistant_done", id: "a2", text: "Five minutes.", instant: true })])
    expect(s.messages).toMatchObject([{ id: "a2", instant: true }])
    expect(run([say("start", "a3"), say("done", "a3", "Hello.")]).messages[0].instant).toBeUndefined()
  })
})
