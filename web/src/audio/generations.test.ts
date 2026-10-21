import { describe, expect, it } from "vitest"
import { Generations } from "./generations"

describe("generations of his voice", () => {
  it("play a stream of one generation", () => {
    const g = new Generations()
    expect(g.frame(0)).toBe("play") // the first: nothing is held yet
    expect(g.frame(0)).toBe("play")
    expect(g.frame(0)).toBe("play")
  })

  it("start with whatever generation comes first", () => {
    const g = new Generations()
    expect(g.frame(3)).toBe("play")
    expect(g.frame(2)).toBe("drop")
  })

  it("drop what is held when a newer generation's frame comes first", () => {
    const g = new Generations()
    g.frame(0)
    expect(g.frame(1)).toBe("flush-play")
    expect(g.frame(1)).toBe("play")
    expect(g.frame(3)).toBe("flush-play")
  })

  it("drop a late frame of an older generation", () => {
    const g = new Generations()
    g.frame(3)
    expect(g.frame(2)).toBe("drop")
    expect(g.frame(1)).toBe("drop")
    expect(g.frame(3)).toBe("play")
  })

  it("flush on the server's `speaker {gen}`, and void the generations before it", () => {
    const g = new Generations()
    g.frame(1)
    expect(g.flush(2)).toBe(true)
    expect(g.frame(1)).toBe("drop")
    expect(g.frame(2)).toBe("play") // its frames came after the event
  })

  it("don't flush again when the new generation's frames came before its `speaker` event", () => {
    const g = new Generations()
    g.frame(0)
    expect(g.frame(1)).toBe("flush-play") // frames go out ahead of events
    expect(g.flush(1)).toBe(false) // …so the event that announced them must not drop them
    expect(g.frame(1)).toBe("play")
  })

  it("ignore an old `speaker` event", () => {
    const g = new Generations()
    g.frame(4)
    expect(g.flush(2)).toBe(false)
    expect(g.flush(4)).toBe(false)
  })

  it("ignore a stop heard before any frame (the old output's, still in flight; or a stop of nothing)", () => {
    const g = new Generations()
    expect(g.flush(7)).toBe(false)
    expect(g.frame(0)).toBe("play") // the fresh output plays
    expect(g.frame(0)).toBe("play")
  })

  it("take the fresh output's generation 0 after the old output's tail (the voice taken again at once)", () => {
    const g = new Generations()
    expect(g.frame(5)).toBe("play") // the old output's last frames, still in flight
    expect(g.flush(6)).toBe(true) // …and its stop
    expect(g.frame(0)).toBe("flush-play") // the fresh output, numbered from 0
    expect(g.frame(0)).toBe("play")
    expect(g.flush(1)).toBe(true)
    expect(g.frame(0)).toBe("drop") // once: back to the usual rules
    expect(g.frame(1)).toBe("play")
  })
})
