import { expect, test } from "vitest"
import icon from "../assets/icon.svg?raw"
import source from "../palette.css?raw"
import { hex, parsePalette, stateTint } from "./palette"
import { cn } from "./utils"

test("palette.ts reads palette.css exactly, and the icon and cn() keep to its tokens", () => {
  const vectors = { "rigel-500": "#55b0dd", "rigel-700": "#357a9b", "nebula-400": "#cc70f2", "void-950": "#010206" }
  for (const [token, value] of Object.entries(vectors)) expect(hex(token)).toBe(value)
  const { labs } = parsePalette(source)
  for (const family of ["steel", "rigel", "nebula", "aurora", "betel", "dusk", "alert", "ok"])
    for (const step of [50, 100, 200, 300, 400, 500, 600, 700, 800, 900])
      expect(labs.has(`${family}-${step}`)).toBe(true)
  expect(hex("ok")).toBe(hex("ok-400"))
  expect(stateTint("thinking")).toBe("nebula-400")
  expect(stateTint("idle")).toBe("rigel-400")
  expect([hex("void-900"), hex("rigel-500"), hex("betel-400")].filter((color) => !icon.includes(color))).toEqual([])
  expect(cn("text-micro text-steel-400", "text-sm")).toBe("text-steel-400 text-sm")
  expect(cn("rounded-control", "rounded-card")).toBe("rounded-card")
})
