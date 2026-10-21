import { expect, test } from "vitest"
import { fitSize, isPicture, MAX_SIDE } from "./attachments"

test("a picture is sized down to 1600 px on its longest side, never up", () => {
  expect(fitSize(4000, 3000)).toEqual({ width: MAX_SIDE, height: 1200 })
  expect(fitSize(1080, 2400)).toEqual({ width: 720, height: MAX_SIDE })
  expect(fitSize(800, 600)).toEqual({ width: 800, height: 600 })
  expect(fitSize(0, 0)).toEqual({ width: 1, height: 1 })
})

test("only pictures are taken", () => {
  expect(isPicture({ type: "image/png" })).toBe(true)
  expect(isPicture({ type: "application/pdf" })).toBe(false)
})
