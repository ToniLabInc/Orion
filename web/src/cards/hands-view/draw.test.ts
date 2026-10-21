import { describe, expect, it } from "vitest"
import { BONES, ease, extend, fieldBox, holdNeeded, palmCentre, place, progress, TIPS, TRAIL_MS, tether } from "./draw"

describe("the live view's geometry", () => {
  it("fits the camera's field in the canvas, keeping its proportions", () => {
    expect(fieldBox(800, 300, 4 / 3)).toEqual({ x: 200, y: 0, w: 400, h: 300 }) // a wide canvas: bars left and right
    expect(fieldBox(400, 600, 4 / 3)).toEqual({ x: 0, y: 150, w: 400, h: 300 }) // a tall one: above and below
    const padded = fieldBox(420, 320, 4 / 3, 10)
    expect(padded.w).toBe(400)
    expect(padded.h).toBe(300)
  })

  it("places a landmark in the field", () => {
    const box = { x: 10, y: 20, w: 400, h: 300 }
    expect(place([0, 0], box)).toEqual([10, 20])
    expect(place([0.5, 1], box)).toEqual([210, 320])
  })

  it("joins the 21 points as a hand", () => {
    const used = new Set(BONES.flat())
    expect(used.size).toBe(21)
    expect([...TIPS]).toEqual([4, 8, 12, 16, 20])
  })

  it("finds the palm's centre", () => {
    const pts = Array.from({ length: 21 }, (_, i) => [i, 2 * i] as [number, number])
    expect(palmCentre(pts)).toEqual([8.8, 17.6]) // the mean of points 0, 5, 9, 13 and 17
  })

  it("knows how far a held shape is from firing", () => {
    expect(holdNeeded(undefined, 0.3)).toBeNull()
    expect(holdNeeded({ do: "media", action: "next" }, 0.3)).toBe(0.3) // the tracker's static_hold
    expect(holdNeeded({ do: "volume", action: "mute", hold: 1 }, 0.3)).toBe(1)
    expect(progress(0.5, 1)).toBe(0.5)
    expect(progress(3, 1)).toBe(1)
    expect(progress(1, null)).toBe(0)
  })

  it("eases a hand towards where it is now, and jumps the first time", () => {
    expect(ease(null, [[1, 1]], 0.5)).toEqual([[1, 1]])
    expect(ease([[0, 0]], [[1, 2]], 0.5)).toEqual([[0.5, 1]])
    expect(
      ease(
        [[0, 0]],
        [
          [1, 1],
          [2, 2],
        ],
        0.5,
      ),
    ).toEqual([
      [1, 1],
      [2, 2],
    ]) // another hand: no easing from it
  })
})

describe("the trails and the tether", () => {
  it("keeps a fingertip's trail short", () => {
    let trail = extend([], [0.5, 0.5], 0)
    trail = extend(trail, [0.5, 0.5], 10) // it didn't move: nothing new
    trail = extend(trail, [0.6, 0.5], 100)
    expect(trail.map((p) => p.x)).toEqual([0.5, 0.6])
    expect(extend(trail, [0.7, 0.5], 100 + TRAIL_MS).map((p) => p.x)).toEqual([0.7]) // the old ones went
  })

  it("marks where two hands count as together and as apart", () => {
    const t = tether([0, 0], [300, 0], 3, { together: 1.8, apart: 3 }) // 100 px a palm length
    expect(t?.state).toBe("apart")
    expect(t?.together.map(([x]) => Math.round(x))).toEqual([60, 240]) // 0.9 palm lengths either side of the middle
    expect(t?.apart.map(([x]) => Math.round(x))).toEqual([0, 300])
    expect(tether([0, 0], [100, 0], 1, { together: 1.8, apart: 3 })?.state).toBe("together")
    expect(tether([0, 0], [250, 0], 2.5, { together: 1.8, apart: 3 })?.state).toBe("between")
    expect(tether([0, 0], [0, 0], 2, { together: 1.8, apart: 3 })).toBeNull()
  })
})
