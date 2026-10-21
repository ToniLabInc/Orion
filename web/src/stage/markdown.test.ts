import { expect, test } from "vitest"
import { blocks, plainText } from "./markdown"

test("a streaming answer splits into blocks at blank lines, never inside a code fence", () => {
  expect(blocks("one\n\ntwo\nlines\n\n\n- a\n- b")).toEqual(["one", "two\nlines", "- a\n- b"])
  expect(blocks("```py\nx = 1\n\ny = 2\n```\nafter")).toEqual(["```py\nx = 1\n\ny = 2\n```", "after"])
  expect(blocks("text\n```\nstill open")).toEqual(["text", "```\nstill open"])
})

test("a reply reads as plain words for the captions", () => {
  expect(plainText("## Today\n- **12°** and *cloudy*, see [the forecast](https://e.x)")).toBe(
    "Today 12° and cloudy, see the forecast",
  )
  expect(plainText("Run `ls`:\n```sh\nls -la\n```\nDone.")).toBe("Run ls: Done.")
})
