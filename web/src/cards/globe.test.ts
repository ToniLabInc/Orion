import { describe, expect, it } from "vitest"
import { subsolar } from "./globe"

describe("subsolar", () => {
  it("puts the sun over the Tropic of Cancer at the June solstice, over Greenwich at noon UTC", () => {
    const { lat, lon } = subsolar(new Date(Date.UTC(2026, 5, 21, 12)))
    expect(lat).toBeCloseTo(23.4, 0)
    expect(Math.abs(lon)).toBeLessThan(0.5)
  })

  it("puts it over the Tropic of Capricorn in December, and a quarter turn west six hours later", () => {
    const { lat, lon } = subsolar(new Date(Date.UTC(2026, 11, 21, 18)))
    expect(lat).toBeCloseTo(-23.4, 0)
    expect(lon).toBeCloseTo(-90, 5)
  })

  it("keeps the longitude within ±180", () => {
    for (let hour = 0; hour < 24; hour++) {
      const { lon } = subsolar(new Date(Date.UTC(2026, 2, 20, hour)))
      expect(lon).toBeGreaterThanOrEqual(-180)
      expect(lon).toBeLessThan(180)
    }
  })
})
