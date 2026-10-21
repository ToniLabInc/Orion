import { expect, test } from "vitest"
import { rank, rankGroups, score } from "./rank"

const items = [
  { label: "New conversation", keywords: "reset chat" },
  { label: "Weather", keywords: "forecast rain" },
  { label: "Show the weather card", keywords: "" },
  { label: "Settings: Voice", keywords: "microphone speech" },
]

test("the palette ranks a label's start first, then a word's, then anywhere, then keywords", () => {
  expect(rank(items, "wea").map((i) => i.label)).toEqual(["Weather", "Show the weather card"])
  expect(rank(items, "rain").map((i) => i.label)).toEqual(["Weather"])
  expect(rank(items, "voice set").map((i) => i.label)).toEqual(["Settings: Voice"])
  expect(rank(items, "")).toHaveLength(4)
  expect(score(items[0], "weather")).toBe(0)
  expect(score({ label: "(odd) [label]" }, "(odd")).toBeGreaterThan(0) // what you type is never a pattern
})

test("the group with the best match comes first, the palette's order kept among equals", () => {
  const groups = [
    { value: "Actions", items: [{ label: "Shut Orion down", keywords: "power off" }, { label: "Restart Orion" }] },
    { value: "Cards", items: [{ label: "Weather" }, { label: "Vitals" }] },
    { value: "Pages", items: [{ label: "Settings" }] },
  ]
  const ranked = rankGroups(groups, "we")
  expect(ranked.map((g) => g.value)).toEqual(["Cards", "Actions"])
  expect(ranked[0].items.map((i) => i.label)).toEqual(["Weather"])
  expect(rankGroups(groups, "orion").map((g) => g.value)).toEqual(["Actions"])
  expect(rankGroups(groups, "zzz")).toEqual([])
})
