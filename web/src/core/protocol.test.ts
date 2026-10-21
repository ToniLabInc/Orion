import { expect, test } from "vitest"
import {
  BANDS,
  decodeLevel,
  encodeLayout,
  encodeLevel,
  encodeMeteor,
  encodeWrite,
  LEVEL_FLOATS,
  WRITABLE,
  WRITE_MAX_CHARS,
  WRITE_MAX_MS,
} from "./protocol"

test("levels: one small array, clamped, 16 bands (missing ones silent), read back into the engine's own", () => {
  const data = encodeLevel("voice", 1.7, [0.5, -1, Number.NaN, 2])
  expect(data).toHaveLength(LEVEL_FLOATS)
  expect([...data.slice(0, 6)]).toEqual([1, 1, 0.5, 0, 0, 1])
  const bands = new Float32Array(BANDS).fill(9)
  expect(decodeLevel(data, bands)).toEqual({ source: 1, value: 1 })
  expect([...bands]).toEqual([0.5, 0, 0, 1, ...new Array(12).fill(0)])
  expect(encodeLevel("mic", 0.25, new Array(40).fill(0.5))[0]).toBe(0)
  expect(decodeLevel(encodeLevel("mic", 0.25, []), bands)).toEqual({ source: 0, value: 0.25 })
})

test("layout: moved to the canvas's own corner; a coordinate that isn't a number is refused", () => {
  expect(encodeLayout({ horizon: 620, talk: { x: 800, y: 640 } }, 10, 20)).toEqual({
    type: "layout",
    horizon: 600,
    talkX: 790,
    talkY: 620,
  })
  expect(encodeLayout({ horizon: Number.NaN, talk: { x: 1, y: 1 } })).toBeNull()
  expect(encodeLayout({ horizon: 1, talk: { x: Number.POSITIVE_INFINITY, y: 1 } })).toBeNull()
  // biome-ignore lint/suspicious/noExplicitAny: a layout from outside the type, as a stale caller might send
  expect(encodeLayout({ horizon: 1 } as any)).toBeNull()
})

test("meteors: to a point (moved to the canvas's corner); an unknown hue is his own; no point, no meteor", () => {
  expect(encodeMeteor({ to: { x: 300, y: 200 }, hue: "aurora" }, 0, 100)).toEqual({
    type: "meteor",
    hue: "aurora",
    x: 300,
    y: 100,
  })
  // biome-ignore lint/suspicious/noExplicitAny: a hue from outside the type
  expect(encodeMeteor({ to: { x: 1, y: 1 }, hue: "plasma" as any })?.hue).toBe("rigel")
  expect(encodeMeteor({ to: { x: Number.NaN, y: 1 }, hue: "ok" })).toBeNull()
  // biome-ignore lint/suspicious/noExplicitAny: a meteor without a target
  expect(encodeMeteor({ hue: "ok" } as any)).toBeNull()
})

test("writing: text the pen can spell, trimmed, or a mark; the hold clamped; a release by ms 0", () => {
  expect(encodeWrite({ text: "  12:41 ", ms: 3000 })).toEqual({ type: "write", text: "12:41", shape: null, ms: 3000 })
  expect(encodeWrite({ text: "−3°C", ms: 1 })).toMatchObject({ text: "−3°C" })
  expect(encodeWrite({ shape: "question", ms: 5000 })).toEqual({
    type: "write",
    text: null,
    shape: "question",
    ms: 5000,
  })
  expect(encodeWrite({ shape: "check", ms: 1e12 })).toMatchObject({ ms: WRITE_MAX_MS })
  // text wins over a shape
  expect(encodeWrite({ text: "21°", shape: "check", ms: 1000 })).toMatchObject({ text: "21°", shape: null })
  // releases: of a text, of a shape, of whatever is written
  expect(encodeWrite({ text: "12:41", ms: 0 })).toEqual({ type: "write", text: "12:41", shape: null, ms: 0 })
  expect(encodeWrite({ shape: "question", ms: 0 })).toEqual({ type: "write", text: null, shape: "question", ms: 0 })
  expect(encodeWrite({ ms: 0 })).toEqual({ type: "write", text: null, shape: null, ms: 0 })
})

test("writing refuses what the pen can't spell, with a plain reason, and never a silent substitute", () => {
  expect(encodeWrite({ text: "a".repeat(WRITE_MAX_CHARS + 1), ms: 100 })).toEqual({
    error: `"${"a".repeat(WRITE_MAX_CHARS + 1)}" is too long to write: ${WRITE_MAX_CHARS + 1} characters, ${WRITE_MAX_CHARS} at most`,
  })
  expect(encodeWrite({ text: "9 pm!", ms: 100 })).toEqual({ error: `the pen can't write "!"` })
  expect(encodeWrite({ text: "Zq", ms: 100 })).toEqual({ error: `the pen can't write "Z", "q"` })
  // biome-ignore lint/suspicious/noExplicitAny: a shape from outside the type
  expect(encodeWrite({ shape: "cube" as any, ms: 100 })).toEqual({ error: `the pen knows no shape "cube"` })
  expect(encodeWrite({ text: "1", ms: -1 })).toHaveProperty("error")
  expect(encodeWrite({ text: "1", ms: Number.NaN })).toHaveProperty("error")
  expect(encodeWrite({ ms: 100 })).toEqual({ error: "nothing to write: give a text or a shape" })
  expect(encodeWrite({ text: "   ", ms: 100 })).toHaveProperty("error")
  // every character of WRITABLE passes
  expect(encodeWrite({ text: WRITABLE.slice(0, WRITE_MAX_CHARS).trim(), ms: 1 })).toHaveProperty("type", "write")
})
