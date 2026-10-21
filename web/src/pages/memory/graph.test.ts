import { describe, expect, it } from "vitest"
import type { NeuralGraph, NeuralKind } from "@/lib/types"
import { pathFromHub, shapeGraph, within } from "./graph"

/** What GET /api/neural sends, from "id id …" and "from>to …": a node's kind is its id's prefix. */
const api = (ids: string, links: string): NeuralGraph => ({
  nodes: ids.split(" ").map((id) => ({ id, label: id, kind: id.split(":")[0] as NeuralKind, degree: 1 })),
  links: links.split(" ").map((pair) => ({ source: pair.split(">")[0], target: pair.split(">")[1] })),
  counts: {},
})

describe("the neural graph", () => {
  it("keeps a node's object when an earlier fact is forgotten, and marks only the new one fresh", () => {
    const first = shapeGraph(api("you fact:a fact:b", "fact:a>you fact:b>you"))
    const next = shapeGraph(api("you fact:b fact:c", "fact:b>you fact:c>you"), first)
    expect(next.byId.get("fact:b")).toBe(first.byId.get("fact:b"))
    expect(next.fresh).toEqual(["fact:c"])
  })

  it("drops links to nowhere, to themselves and twice over", () => {
    expect(shapeGraph(api("you fact:a", "fact:a>you you>fact:a fact:a>fact:a fact:a>gone")).links).toHaveLength(1)
  })

  it("walks a neighbourhood up to the hubs, and finds the way out from a hub", () => {
    const ids = "you orion fact:a concept:pi day:1 skill:s"
    const g = shapeGraph(api(ids, "fact:a>you you>orion fact:a>concept:pi concept:pi>day:1 skill:s>orion"))
    expect([...within(g, "fact:a", 3).keys()]).toEqual(["fact:a", "you", "concept:pi", "day:1"]) // not past you
    expect(within(g, "you", 1).has("orion")).toBe(true) // unless it starts there
    expect(within(g, "fact:a", 3, new Set(["concept"])).has("day:1")).toBe(false)
    expect(within(g, "nobody", 2).size).toBe(0)
    expect(pathFromHub(g, "day:1")).toEqual(["you", "fact:a", "concept:pi", "day:1"])
  })
})
