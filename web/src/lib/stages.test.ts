import { describe, expect, it } from "vitest"
import { brainLabel, INSTANT, msText, STAGES, stageParts } from "./stages"

describe("a turn's stages", () => {
  it("lists the stages it went through in order, each with its share", () => {
    const parts = stageParts({ heard_ms: 500, think_ms: 1500, tools_ms: 1000, speak_ms: 1000, total_ms: 4000 })
    expect(parts.map((p) => p.key)).toEqual(["heard_ms", "think_ms", "tools_ms", "speak_ms"])
    expect(parts.map((p) => p.share)).toEqual([0.125, 0.375, 0.25, 0.25])
    expect(parts.reduce((sum, p) => sum + p.share, 0)).toBeCloseTo(1)
  })
  it("leaves out a stage that took no time: a typed request isn't heard, a written answer isn't spoken", () => {
    const parts = stageParts({ heard_ms: 0, think_ms: 900, tools_ms: 0, speak_ms: 0, total_ms: 900 })
    expect(parts).toMatchObject([{ key: "think_ms", share: 1, name: "Thinking" }])
    expect(stageParts({ heard_ms: 0, think_ms: 0, tools_ms: 0, speak_ms: 0, total_ms: 0 })).toEqual([])
  })
  it("names four stages, each once", () => {
    expect(STAGES.map((s) => s.label)).toEqual(["heard", "think", "tools", "speak"])
  })
})

describe("a duration as the stages say it", () => {
  it("is milliseconds below a second, a decimal below ten seconds, then whole seconds", () => {
    expect(msText(0)).toBe("0 ms")
    expect(msText(-5)).toBe("0 ms")
    expect(msText(840.4)).toBe("840 ms")
    expect(msText(1000)).toBe("1.0 s")
    expect(msText(2449)).toBe("2.4 s")
    expect(msText(9999)).toBe("10.0 s")
    expect(msText(10_400)).toBe("10 s")
    expect(msText(61_000)).toBe("61 s")
  })
})

describe("a brain's name in the speeds", () => {
  it("is the model's own, and says Instant commands for the turns no model answered", () => {
    expect(INSTANT).toBe("instant")
    expect(brainLabel("instant")).toBe("Instant commands")
    expect(brainLabel("Claude Sonnet 4.6")).toBe("Claude Sonnet 4.6")
    expect(brainLabel("qwen3.5:9b")).toBe("qwen3.5:9b")
  })
})
