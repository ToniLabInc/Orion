import { describe, expect, it } from "vitest"
import { defaultLayout } from "./horizon"

// where the stage really puts its talk button, measured in a browser at these sizes (the centre, rounded)
const MEASURED: [width: number, height: number, x: number, y: number][] = [
  [1440, 900, 294, 783],
  [1920, 1080, 602, 950],
  [1280, 720, 134, 616],
  [1024, 768, 154, 660],
  [820, 1180, 110, 1043],
  [700, 900, 110, 783],
  [390, 844, 44, 721],
  [360, 640, 44, 531],
  [430, 932, 44, 803],
]

describe("where the limb is on a page without the stage", () => {
  it.each(MEASURED)("is where the stage puts it at %i x %i", (width, height, x, y) => {
    const layout = defaultLayout(width, height)
    expect(Math.abs(layout.talk.x - x)).toBeLessThanOrEqual(1)
    expect(Math.abs(layout.talk.y - y)).toBeLessThanOrEqual(1)
    expect(layout.horizon).toBe(layout.talk.y)
  })
  it("lifts a phone's by the safe area under its screen, and nothing else's", () => {
    expect(defaultLayout(390, 844, 34).talk.y).toBeCloseTo(defaultLayout(390, 844).talk.y - 34)
    expect(defaultLayout(1440, 900, 34).talk.y).toBe(defaultLayout(1440, 900).talk.y)
  })
  it("keeps the space under the stage between 1 and 5.5 rem, whatever the height", () => {
    expect(100 - defaultLayout(1440, 100).talk.y).toBeCloseTo(16 + 32 + 22) // tiny: 1 rem
    expect(4000 - defaultLayout(1440, 4000).talk.y).toBeCloseTo(88 + 32 + 22) // huge: 5.5 rem
  })
})
