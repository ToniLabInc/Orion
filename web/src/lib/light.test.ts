import { describe, expect, it } from "vitest"
import { WRITABLE, WRITE_MAX_CHARS } from "@/core/protocol"
import { lightTemp, lightTime, lightTimer } from "./light"

const writable = (text: string) => text.length <= WRITE_MAX_CHARS && [...text].every((c) => WRITABLE.includes(c))

describe("what is written in light", () => {
  it("writes a time as two digits and two digits, whatever the hour", () => {
    expect(lightTime(new Date(2026, 9, 12, 14, 5))).toBe("14:05")
    expect(lightTime(new Date(2026, 9, 12, 0, 0))).toBe("00:00")
    expect(lightTime(new Date(2026, 9, 12, 9, 41))).toBe("09:41")
  })
  it("writes a temperature in whole degrees, never as minus zero", () => {
    expect(lightTemp(14.4)).toBe("14°")
    expect(lightTemp(14.5)).toBe("15°")
    expect(lightTemp(-3.2)).toBe("-3°")
    expect(lightTemp(-0.4)).toBe("0°")
  })
  it("writes a timer's length as minutes and seconds, then hours", () => {
    const timer = (seconds: number) => ({ ts: 1000, due: 1000 + seconds })
    expect(lightTimer(timer(600))).toBe("10:00")
    expect(lightTimer(timer(90))).toBe("1:30")
    expect(lightTimer(timer(7200))).toBe("2 h")
    expect(lightTimer(timer(5400))).toBe("1.5 h")
    expect(lightTimer(timer(0))).toBe("0:00")
    expect(lightTimer({ ts: 2000, due: 1000 })).toBe("0:00")
  })
  it("only ever writes what the Core can write", () => {
    const texts = [
      lightTime(new Date(2026, 0, 1, 23, 59)),
      lightTemp(-39.6),
      lightTemp(41.2),
      ...[1, 59, 61, 3599, 3600, 5400, 86_399, 360_000].map((s) => lightTimer({ ts: 0, due: s })),
    ]
    for (const text of texts) expect(writable(text), text).toBe(true)
  })
})
