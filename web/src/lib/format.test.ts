import { describe, expect, it } from "vitest"
import {
  agoInWords,
  bytes,
  clockTime,
  countdown,
  fineMoney,
  imageLink,
  money,
  shortDate,
  stepDuration,
  stepTarget,
  traceSummary,
  untilInWords,
  utcMidnight,
} from "./format"

const now = new Date(2026, 9, 4, 12, 0, 0)
const t = now.getTime() / 1000

describe("words and numbers", () => {
  it("counts down timers and says how long until a reminder", () => {
    expect(countdown(t + 0.3, now)).toBe("0:01")
    expect(countdown(t - 5, now)).toBe("now")
    expect(countdown(t + 642, now)).toBe("10:42")
    expect(countdown(t + 3599, now)).toBe("59:59")
    expect(untilInWords(t + 40 * 60, now)).toBe("in 40 min")
    expect(untilInWords(t + 179 * 60, now)).toBe("in 2 h 59 min")
    expect(untilInWords(t + 3 * 3600, now)).toBe("in 3 h")
    expect(untilInWords(t + 4 * 86400, now)).toBe("in 4 days")
    expect(untilInWords(t + 20, now)).toBe("any moment")
  })

  it("says how long ago", () => {
    expect(agoInWords(t - 20, now)).toBe("just now")
    expect(agoInWords(t - 12 * 60, now)).toBe("12 min ago")
    expect(agoInWords(t - 3 * 3600, now)).toBe(`at ${clockTime(new Date((t - 3 * 3600) * 1000), false)}`)
    expect(agoInWords(t - 3 * 86400, now)).toBe(shortDate(new Date((t - 3 * 86400) * 1000)))
  })

  it("writes the trace's durations, targets and summary", () => {
    expect(stepDuration(812)).toBe("0.8 s")
    expect(stepDuration(12_400)).toBe("12 s")
    expect(stepDuration(64_000)).toBe("1 m 04 s")
    expect(stepTarget({ minutes: 10 })).toBe("10 min")
    expect(stepTarget({ query: "weather Lisbon", count: 3 })).toBe("weather Lisbon")
    const step = (at: number, ms: number, kind = "tool", status = "ok") => ({ at, ms, kind, status })
    const steps = [step(0, 800), step(500, 0, "memory"), step(1000, 2100, "tool", "failed"), step(1200, 400)]
    expect(traceSummary(steps)).toBe("4 operations · 3.1 s · 1 memory saved · 1 failed")
  })

  it("writes money: nothing, a fraction of a cent, cents, whole dollars", () => {
    expect(money(0)).toBe("$0")
    expect(money(-1)).toBe("$0")
    expect(money(0.004)).toBe("<$0.01")
    expect(money(0.42)).toBe("$0.42")
    expect(money(14.375)).toBe("$14.38")
    expect(money(99.999)).toBe("$100.00")
    expect(money(1234.5)).toBe("$1,235")
  })
  it("writes what a benchmark cost to a hundredth of a cent", () => {
    expect(fineMoney(0)).toBe("$0")
    expect(fineMoney(0.00004)).toBe("<$0.0001")
    expect(fineMoney(0.0034)).toBe("$0.0034")
    expect(fineMoney(0.123456)).toBe("$0.1235")
    expect(fineMoney(1.5)).toBe("$1.50")
  })
  it("says when OpenRouter's UTC day starts on this clock, never as 24:00", () => {
    const winter = new Date(Date.UTC(2026, 0, 5, 12))
    const offset = new Date(Date.UTC(2026, 0, 5)).getTimezoneOffset() // minutes the clock is behind UTC
    const minutes = (1440 - offset) % 1440
    const hh = String(Math.floor(minutes / 60)).padStart(2, "0")
    const mm = String(minutes % 60).padStart(2, "0")
    expect(utcMidnight(winter)).toBe(`${hh}:${mm}`)
    expect(utcMidnight(winter)).not.toMatch(/^24/)
  })
  it("writes a size in powers of a thousand, a decimal below ten", () => {
    expect(bytes(0)).toBe("0 B")
    expect(bytes(820)).toBe("820 B")
    expect(bytes(1000)).toBe("1.0 KB")
    expect(bytes(34_500)).toBe("35 KB")
    expect(bytes(48_300_000)).toBe("48 MB")
    expect(bytes(4_700_000_000)).toBe("4.7 GB")
    expect(bytes(-5)).toBe("0 B")
  })
  it("never shows an image, only a link to it", () => {
    const base = "http://127.0.0.1:8765/"
    expect(imageLink("https://evil.example/p?d=x", "facts", base)).toMatchObject({ label: "facts evil.example" })
    expect(imageLink("javascript:alert(1)", "facts", base)).toEqual({ text: "facts" })
    expect(imageLink("", "facts", base)).toEqual({ text: "facts" })
    expect(imageLink("https://evil.example/p.png", "logo", base, true)).toEqual({ text: "logo" })
  })
})
