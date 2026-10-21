import { describe, expect, it } from "vitest"
import { asksTheTime, cardFor } from "./cards"

describe("cardFor", () => {
  it("knows his own tools by name", () => {
    expect(cardFor("get_weather", "web")).toBe("weather")
    expect(cardFor("set_timer", "organizer")).toBe("timers")
    expect(cardFor("todo", "organizer")).toBe("tasks")
    expect(cardFor("system_status", "system")).toBe("vitals")
  })
  it("knows whole servers, and another server's tools by their words", () => {
    expect(cardFor("schedule_job", "automation")).toBe("jobs")
    expect(cardFor("gcal_list_events", "google")).toBe("calendar")
    expect(cardFor("look_at_screen", "eyes")).toBe("eyes")
  })
  it("leaves the rest on the conversation", () => {
    expect(cardFor("web_search", "web")).toBeNull()
    expect(cardFor("run_command", "system")).toBeNull()
  })
})

describe("asksTheTime", () => {
  it("hears a question about the time, and not the rest", () => {
    expect(asksTheTime("What time is it?")).toBe(true)
    expect(asksTheTime("hey orion what's the time")).toBe(true)
    expect(asksTheTime("Set a timer for the time it takes")).toBe(false)
  })
})
