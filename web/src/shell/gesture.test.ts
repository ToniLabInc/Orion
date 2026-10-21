import { describe, expect, it } from "vitest"
import { chipAfter, intentWords } from "./gesture"

describe("intentWords", () => {
  it("prefers the server's own words, without the gesture they start with", () => {
    expect(intentWords({ intent: "media", args: { action: "next" }, text: "Swipe right → next track" })).toBe(
      "Next track",
    )
    expect(intentWords({ intent: "new_chat", text: "new conversation" })).toBe("New conversation")
  })

  it("says what each intent did", () => {
    expect(intentWords({ intent: "media", args: { action: "next" } })).toBe("Next track")
    expect(intentWords({ intent: "volume", args: { action: "down" } })).toBe("Volume down")
    expect(intentWords({ intent: "show", args: { panels: ["comms", "vitals"] } })).toBe("Show comms, vitals")
    expect(intentWords({ intent: "hide", args: { panels: [] } })).toBe("Clear the stage")
    expect(intentWords({ intent: "mic", args: { action: "mute" } })).toBe("Microphone off")
    expect(intentWords({ intent: "say", args: { text: "hello" } })).toBe("“hello”")
  })

  it("falls back to the intent's name for one it doesn't know", () => {
    expect(intentWords({ intent: "dim_lights" })).toBe("dim lights")
  })
})

describe("chipAfter", () => {
  const up = { type: "intent" as const, intent: "volume", gesture: "thumb_up", showing: 7, args: { action: "up" } }
  it("counts the repeats of one held shape in place, and replaces it for anything else", () => {
    const first = chipAfter(null, up, 100)
    expect([first.seen, first.count]).toEqual([100, 1])
    const again = chipAfter(first, { ...up, again: true, result: "Volume is 55%." }, 600)
    expect([again.seen, again.count, again.result]).toEqual([100, 2, "Volume is 55%."]) // same chip, no new entrance
    expect(chipAfter(again, { ...up, again: true }, 1100).count).toBe(3)
    expect(chipAfter(again, { ...up, showing: 9 }, 1200)).toMatchObject({ seen: 1200, count: 1 }) // a new showing
    expect(chipAfter(again, { ...up, again: true, gesture: "thumb_down" }, 1300).count).toBe(1)
  })
})
