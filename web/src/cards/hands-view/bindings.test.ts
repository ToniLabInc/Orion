import { describe, expect, it } from "vitest"
import type { GesturesPage, GestureTool } from "@/lib/types"
import { byKind, freshBinding, holds, simpleArgs, withValue } from "./bindings"

// what the server says of a few actions: their options with the choices in words, and what each starts with
const ACTIONS: GesturesPage["actions"] = {
  media: {
    title: "Media",
    group: "Media",
    repeats: false,
    params: [
      {
        name: "action",
        kind: "choice",
        choices: [
          { value: "play_pause", label: "Play / pause" },
          { value: "next", label: "Next track" },
        ],
        required: true,
        default: null,
      },
    ],
    start: { do: "media", action: "play_pause" },
  },
  volume: {
    title: "Volume",
    group: "Media",
    repeats: true,
    params: [
      {
        name: "action",
        kind: "choice",
        choices: [{ value: "up", label: "Volume up" }],
        required: true,
        default: null,
      },
    ],
    start: { do: "volume", action: "up" },
  },
  show: {
    title: "Show on the screen",
    group: "Screen",
    repeats: false,
    params: [{ name: "panels", kind: "panels", choices: [], required: true, default: null }],
    start: { do: "show", panels: ["comms"] },
  },
  skill: {
    title: "Run a skill",
    group: "Orion",
    repeats: false,
    params: [{ name: "name", kind: "skill", choices: [], required: true, default: null }],
    start: null, // no skill to run yet
  },
}

const TOOL: GestureTool = {
  server: "system",
  name: "open",
  title: "Open",
  description: "Open an app.",
  destructive: false,
  schema: { type: "object", properties: { target: { type: "string", description: "What" } }, required: ["target"] },
}

describe("editing a binding", () => {
  it("starts a newly chosen action from what the server says", () => {
    expect(freshBinding("media", ACTIONS.media, undefined, false)).toEqual({ do: "media", action: "play_pause" })
    expect(freshBinding("show", ACTIONS.show, undefined, false)).toEqual({ do: "show", panels: ["comms"] })
    expect(freshBinding("skill", ACTIONS.skill, undefined, false)).toEqual({ do: "skill" })
    const fresh = freshBinding("show", ACTIONS.show, undefined, false)
    ;(fresh.panels as string[]).push("news")
    expect(ACTIONS.show.start).toEqual({ do: "show", panels: ["comms"] }) // a copy: editing it changes nothing else
  })

  it("keeps a shape's hold, and repeats an action that repeats", () => {
    expect(freshBinding("volume", ACTIONS.volume, { do: "media", action: "next", hold: 1 }, true)).toEqual({
      do: "volume",
      action: "up",
      hold: 1,
      repeat: true,
    })
    expect(freshBinding("volume", ACTIONS.volume, { do: "media", action: "next", hold: 1 }, false)).toEqual({
      do: "volume",
      action: "up",
    })
    expect(freshBinding("media", ACTIONS.media, { do: "media", action: "next" }, false)).toEqual({
      do: "media",
      action: "next",
    })
  })

  it("offers the remote's hold steps, and the binding's own", () => {
    expect(holds({ hold_steps: [0.6, 1, 1.5, 2] }, undefined)).toEqual([0, 0.6, 1, 1.5, 2])
    expect(holds({ hold_steps: [0.6, 1] }, 3)).toEqual([0, 0.6, 1, 3]) // written in config.yaml
    expect(holds({ hold_steps: [] }, 1)).toEqual([0, 1]) // a remote that has never said
  })

  it("keeps an option's value among its choices", () => {
    const minutes = [
      { value: 5, label: "5 min" },
      { value: 10, label: "10 min" },
    ]
    expect(withValue(minutes, 10)).toBe(minutes)
    expect(withValue(minutes, 7)).toEqual([...minutes, { value: 7, label: "7" }])
    expect(withValue(minutes, undefined)).toBe(minutes)
  })

  it("lists a remote's gestures by kind, and any its bindings alone name", () => {
    const groups = byKind(
      [
        { name: "victory", title: "Victory", kind: "shape", how: "" },
        { name: "wave", title: "Wave", kind: "motion", how: "" },
      ],
      { victory: { do: "new_chat" }, flick: { do: "dismiss" } },
    )
    expect(groups.map((g) => [g.kind, g.gestures.map((x) => x.name)])).toEqual([
      ["shape", ["victory"]],
      ["motion", ["wave"]],
      ["", ["flick"]],
    ])
  })

  it("edits a tool's plain arguments one by one, and the rest as JSON", () => {
    expect(simpleArgs(TOOL)).toEqual([
      { name: "target", type: "string", choices: undefined, required: true, description: "What" },
    ])
    const nested = { ...TOOL, schema: { type: "object", properties: { opts: { type: "object" } } } }
    expect(simpleArgs(nested)).toBeNull()
    expect(simpleArgs(undefined)).toBeNull()
  })
})
