import { describe, expect, it } from "vitest"
import {
  brainNote,
  canRun,
  costText,
  endNote,
  headline,
  leftOut,
  needsYes,
  paidTotal,
  planLines,
  requestsWords,
  runState,
  shareText,
  timeText,
} from "./bench"
import type { BenchBrainPlan, BenchPlanView, BenchResult } from "./types"

const brain = (over: Partial<BenchBrainPlan> = {}): BenchBrainPlan => ({
  brain: "ollama:qwen3.5:9b",
  label: "qwen3.5:9b",
  paid: "",
  where: "this PC",
  cloud: false,
  requests: { typical: 0, most: 0 },
  ready: true,
  why: "",
  ...over,
})
const claude = brain({
  brain: "claude",
  label: "Claude Sonnet 4.5",
  paid: "plan",
  where: "your Claude plan",
  cloud: true,
  requests: { typical: 90, most: 220 },
})
const router = brain({
  brain: "openrouter",
  label: "Qwen3 235B",
  paid: "money",
  where: "OpenRouter",
  cloud: true,
  requests: { typical: 60, most: 180 },
})
const plan = (...brains: BenchBrainPlan[]): BenchPlanView => ({
  suite: "builtin",
  cases: 40,
  dry_run: true,
  skipped: [],
  brains,
  paid_requests: { typical: 0, most: 0 },
})
const result = (over: Partial<BenchResult> = {}): BenchResult => ({
  brain: "claude",
  label: "Claude Sonnet 4.5",
  paid: "plan",
  cases: 40,
  answered: 38,
  passed: 34,
  tool_accuracy: 0.95,
  args_accuracy: 0.9,
  forbidden: 0,
  rounds: 1.43,
  p50_first_token_ms: 640,
  p95_first_token_ms: 1200,
  p50_total_ms: 2300,
  p95_total_ms: 4100,
  cost: 0,
  errors: 2,
  skipped: 0,
  error: "",
  failures: [],
  ...over,
})

describe("whether a benchmark can run and needs your yes", () => {
  it("runs when one brain is ready, and not when none is", () => {
    expect(canRun(plan(brain()))).toBe(true)
    expect(canRun(plan(brain({ ready: false, why: "no model" }), router))).toBe(true)
    expect(canRun(plan(brain({ ready: false, why: "no model" })))).toBe(false)
  })
  it("asks for a yes only where a brain that can run is paid, as the server does", () => {
    expect(needsYes(plan(brain()))).toBe(false)
    expect(needsYes(plan(brain(), claude))).toBe(true)
    expect(needsYes(plan(router))).toBe(true)
    // a paid brain that can't run costs nothing, so it asks for nothing
    expect(needsYes(plan(brain(), { ...router, ready: false, why: "spent" }))).toBe(false)
  })
})

describe("the plan in words", () => {
  it("says how many requests, of which suite, and that no tool really runs", () => {
    expect(headline({ suite: "builtin", cases: 40 })).toBe(
      "40 requests of his own suite, asked of each brain in a dry run: no tool really runs.",
    )
    expect(headline({ suite: "history", cases: 1 })).toBe(
      "1 request of your own, asked of each brain in a dry run: no tool really runs.",
    )
    expect(requestsWords({ typical: 90, most: 220 })).toBe("About 90 requests, at most 220.")
    expect(requestsWords({ typical: 1, most: 3 })).toBe("About 1 request, at most 3.")
  })
  it("tags each brain: free, your plan, paid, or won't run, with what to know", () => {
    const lines = planLines({
      suite: "builtin",
      brains: [
        brain(),
        claude,
        router,
        brain({
          brain: "openrouter:x",
          label: "X",
          paid: "money",
          ready: false,
          why: "today's OpenRouter budget is spent ($1.02 of $1.00)",
        }),
      ],
    })
    expect(lines.map((l) => [l.label, l.tag, l.tone])).toEqual([
      ["qwen3.5:9b", "Free", "free"],
      ["Claude Sonnet 4.5", "Your Claude plan", "plan"],
      ["Qwen3 235B", "Paid", "money"],
      ["X", "Won't run", "stopped"],
    ])
    expect(lines[0].notes).toEqual([])
    expect(lines[1].notes).toEqual(["About 90 requests, at most 220."])
    expect(lines[3].notes).toEqual(["Today's OpenRouter budget is spent ($1.02 of $1.00)."])
  })
  it("warns that your own requests leave the PC only for a brain in the cloud", () => {
    const lines = planLines({ suite: "history", brains: [brain(), claude] })
    expect(lines[0].notes).toEqual([])
    expect(lines[1].notes).toEqual([
      "About 90 requests, at most 220.",
      "Your own requests are sent to your Claude plan.",
    ])
  })
  it("adds up the paid requests only when more than one brain makes them", () => {
    const view = { ...plan(claude, router), paid_requests: { typical: 150, most: 400 } }
    expect(paidTotal(view)).toBe("In all: about 150 requests, at most 400.")
    expect(paidTotal({ ...plan(claude, brain()), paid_requests: { typical: 90, most: 220 } })).toBe("")
  })
  it("says how many requests were left out", () => {
    expect(leftOut({ skipped: [] })).toBe("")
    expect(leftOut({ skipped: [{ say: "a", why: "needs x" }] })).toBe("1 request left out: their tools aren't here.")
    expect(
      leftOut({
        skipped: [
          { say: "a", why: "" },
          { say: "b", why: "" },
        ],
      }),
    ).toBe("2 requests left out: their tools aren't here.")
  })
})

describe("how a run ended", () => {
  it("tags a run by how far it is, done, stopped by you, or failed", () => {
    expect(runState({ status: "running", progress: 0.426, error: "" })).toEqual({ label: "43%", tone: "nebula" })
    expect(runState({ status: "done", progress: 1, error: "" })).toEqual({ label: "Done", tone: "ok" })
    expect(runState({ status: "stopped", progress: 0.3, error: "" })).toEqual({ label: "Stopped", tone: "dusk" })
    expect(runState({ status: "failed", progress: 0.3, error: "RuntimeError: boom" })).toEqual({
      label: "Failed",
      tone: "alert",
    })
  })
  it("says what went wrong, and that a stopped run keeps what it scored", () => {
    expect(endNote({ status: "running", error: "" })).toBe("")
    expect(endNote({ status: "done", error: "" })).toBe("")
    expect(endNote({ status: "stopped", error: "" })).toBe("Stopped before the end. The scores are what it got so far.")
    expect(endNote({ status: "failed", error: "Orion stopped during the run." })).toBe("Orion stopped during the run.")
  })
})

describe("a brain's scores", () => {
  it("shows a dash where nothing was measured: the server sends 0", () => {
    expect(shareText(result(), 0.95)).toBe("95%")
    expect(shareText(result({ cases: 0 }), 0)).toBe("—")
    expect(timeText(result(), 640)).toBe("640 ms")
    expect(timeText(result(), 2300)).toBe("2.3 s")
    expect(timeText(result({ answered: 0 }), 0)).toBe("—")
  })
  it("says what a brain cost: dollars for a paid model, your plan, or nothing", () => {
    expect(costText(result({ paid: "money", cost: 0.0034 }))).toBe("$0.0034")
    expect(costText(result({ paid: "money", cost: 0 }))).toBe("$0")
    expect(costText(result({ paid: "plan" }))).toBe("Your plan")
    expect(costText(result({ paid: "" }))).toBe("Free")
  })
  it("says why a brain stopped, how many requests it never got to, or how many got no answer", () => {
    expect(brainNote(result({ errors: 0 }))).toBe("")
    expect(brainNote(result({ errors: 2 }))).toBe("2 requests got no answer.")
    expect(brainNote(result({ errors: 1 }))).toBe("1 request got no answer.")
    expect(brainNote(result({ error: "usage limit reached", skipped: 28 }))).toBe(
      "usage limit reached. 28 requests not asked.",
    )
    expect(brainNote(result({ error: "Not ready.", skipped: 0 }))).toBe("Not ready.")
  })
})
