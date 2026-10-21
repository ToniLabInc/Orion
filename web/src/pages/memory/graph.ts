/**
 * His memory as the neural map draws it: the kinds of node and how each looks, the graph shaped from GET /api/neural,
 * a node's neighbourhood, the way from a hub out to a node, whose names show, and search.
 *
 * The server's ids are stable (an entry's id comes from its text, a day's from its date), so a node is known by its id
 * alone, and a node or a link keeps its object, and with it its place and its look, from one load to the next. Nodes
 * carry no text: the panel fetches a node's text when it opens.
 */
import type { NeuralGraph, NeuralKind } from "@/lib/types"

export type Kind = NeuralKind

/** A node's solid: each kind has its own, so the kinds differ without colour too. */
export type Shape = "octahedron" | "core" | "sphere" | "diamond" | "dashed" | "hollow" | "cube" | "hexagon" | "triangle"

/** How a kind looks: its name in the legend, its shade (a palette token), its glyph and its size. */
export interface KindLook {
  name: string
  token: string
  shape: Shape
  size: number
}

/** Each kind's look. Yours are Betelgeuse's orange (you, what he knows about you, your notes), his Rigel's teal (him,
 *  his notes) with his skills aqua, the concepts that join them the nebula's magenta, the journal steel, and an
 *  unverified memory deep blue. An unverified memory is a fact's diamond drawn only in
 *  dashes: kept, but not yet trusted. */
export const KINDS: Readonly<Record<Kind, KindLook>> = {
  you: { name: "You", token: "betel-200", shape: "octahedron", size: 6 },
  orion: { name: "Orion", token: "rigel-500", shape: "core", size: 7 },
  concept: { name: "Concepts", token: "nebula-400", shape: "sphere", size: 2.4 },
  fact: { name: "About you", token: "betel-400", shape: "diamond", size: 2.4 },
  unverified: { name: "Unverified", token: "dusk-300", shape: "dashed", size: 2.4 },
  insight: { name: "His notes", token: "rigel-300", shape: "hollow", size: 2.4 },
  day: { name: "Journal", token: "steel-400", shape: "cube", size: 2.1 },
  note: { name: "Notes", token: "betel-300", shape: "hexagon", size: 2.6 },
  skill: { name: "Skills", token: "aurora-400", shape: "triangle", size: 2.8 },
}

/** The kinds the legend can show or hide (the server counts these); the two hubs always stay. */
export const LEGEND = [
  "concept",
  "fact",
  "unverified",
  "insight",
  "day",
  "note",
  "skill",
] as const satisfies readonly Kind[]

/**
 * Whether a kind is one of the two hubs that nearly everything joins.
 * @param kind the node's kind
 * @returns true for You and Orion
 */
export const isHub = (kind: Kind) => kind === "you" || kind === "orion"

/** A node as the force layout moves it (it adds x, y, z and their speeds; f* pin it). */
export interface MapNode {
  id: string
  kind: Kind
  label: string
  degree: number
  x?: number
  y?: number
  z?: number
  fx?: number
  fy?: number
  fz?: number
}

/** A link; the layout swaps the ids for the nodes themselves. */
export interface MapLink {
  source: string | MapNode
  target: string | MapNode
}

/** The graph as the map holds it. */
export interface MapGraph {
  nodes: MapNode[]
  links: MapLink[]
  /** Each node by its id. */
  byId: Map<string, MapNode>
  /** Each node's neighbours. */
  near: Map<string, MapNode[]>
  /** Each link by its two ends (see `pairOf`). */
  pairs: Map<string, MapLink>
  /** What wasn't there on the last load (nothing on the first). */
  fresh: string[]
  /** Every node already has its place: nothing to lay out. */
  settled: boolean
}

/**
 * The key of the link between two nodes, the same whichever end comes first.
 * @param a one end's id
 * @param b the other end's id
 * @returns the pair's key
 */
const pairOf = (a: string, b: string) => (a < b ? `${a}\n${b}` : `${b}\n${a}`)

/**
 * A small fixed offset per id: a new node starts beside a neighbour, the same way every time.
 * @param id the node's id
 * @param axis 1, 2 or 3
 * @returns the offset, -8 to 8
 */
function nudge(id: string, axis: number) {
  let h = 2166136261 + axis
  for (let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 16777619)
  return ((h >>> 0) / 2 ** 32 - 0.5) * 16
}

/**
 * Pin a node where it is, or at the given place.
 * @param n the node
 * @param x where to pin it (its place by default)
 * @param y where to pin it
 * @param z where to pin it
 */
function pin(n: MapNode, x = n.x ?? 0, y = n.y ?? 0, z = n.z ?? 0) {
  Object.assign(n, { x, y, z, fx: x, fy: y, fz: z })
}

/**
 * Shape the server's graph, reusing the nodes and links of `before`: a node that found its place (pinned when the
 * layout settled) keeps it, a new one starts beside a pinned neighbour, and the two hubs sit apart either side of the
 * middle (one above the other on a tall screen). Links to nowhere, to themselves and twice over are dropped.
 * @param data what GET /api/neural sent
 * @param before the graph of the last load, if any
 * @param tall whether the screen is taller than it is wide
 * @returns the graph for the map
 */
export function shapeGraph(data: NeuralGraph, before?: MapGraph | null, tall = false): MapGraph {
  const byId = new Map<string, MapNode>()
  for (const n of data.nodes) {
    if (byId.has(n.id)) continue
    const node = before?.byId.get(n.id) ?? { id: n.id, kind: n.kind, label: n.label, degree: n.degree }
    Object.assign(node, { kind: n.kind, label: n.label, degree: n.degree })
    byId.set(n.id, node)
  }

  const links: MapLink[] = []
  const pairs = new Map<string, MapLink>()
  const near = new Map<string, MapNode[]>([...byId.keys()].map((id) => [id, []]))
  for (const { source, target } of data.links) {
    const [a, b] = [byId.get(source), byId.get(target)]
    const pair = pairOf(source, target)
    if (!a || !b || a === b || pairs.has(pair)) continue
    const link = before?.pairs.get(pair) ?? { source, target }
    pairs.set(pair, link)
    links.push(link)
    near.get(source)?.push(b)
    near.get(target)?.push(a)
  }

  const nodes = [...byId.values()]
  const spread = 36 + 5 * Math.sqrt(nodes.length)
  for (const n of nodes) {
    if (n.fx !== undefined || n.x !== undefined) continue // placed, or still finding its place
    if (isHub(n.kind)) {
      const side = n.kind === "you" ? -spread : spread
      pin(n, tall ? 0 : side, tall ? -side : 0, 0)
      continue
    }
    const anchor = before && near.get(n.id)?.find((m) => m.fx !== undefined)
    if (anchor)
      Object.assign(n, {
        x: (anchor.fx ?? 0) + nudge(n.id, 1),
        y: (anchor.fy ?? 0) + nudge(n.id, 2),
        z: (anchor.fz ?? 0) + nudge(n.id, 3),
      })
  }
  return {
    nodes,
    links,
    byId,
    near,
    pairs,
    fresh: before ? nodes.filter((n) => !before.byId.has(n.id)).map((n) => n.id) : [],
    settled: nodes.every((n) => n.fx !== undefined),
  }
}

/**
 * The layout settled: every node keeps the place it found from now on (new ones settle around them).
 * @param graph the graph
 */
export const pinAll = (graph: MapGraph) => {
  for (const n of graph.nodes) if (n.x !== undefined) pin(n)
}

/**
 * The link between two nodes, if there is one.
 * @param graph the graph
 * @param a one end's id
 * @param b the other end's id
 * @returns the link
 */
export const linkBetween = (graph: MapGraph, a: string, b: string) => graph.pairs.get(pairOf(a, b))

/**
 * The nodes up to `hops` links from `id`, by hop (0 is the node itself), leaving out hidden kinds. The hubs link to
 * nearly everything, so a path that reaches one ends there instead of spreading through it.
 * @param graph the graph
 * @param id where to start
 * @param hops how far to go
 * @param hidden the kinds the legend hides
 * @returns each node reached and its hop; empty for an unknown id
 */
export function within(graph: MapGraph, id: string, hops: number, hidden: ReadonlySet<Kind> = new Set()) {
  const start = graph.byId.get(id)
  const found = new Map<string, number>()
  if (!start) return found
  found.set(id, 0)
  let edge = [start]
  for (let hop = 1; hop <= hops && edge.length; hop++) {
    const next: MapNode[] = []
    for (const from of edge) {
      if (from !== start && isHub(from.kind)) continue
      for (const n of graph.near.get(from.id) ?? []) {
        if (found.has(n.id) || hidden.has(n.kind)) continue
        found.set(n.id, hop)
        next.push(n)
      }
    }
    edge = next
  }
  return found
}

/**
 * The shortest way from a hub out to a node, through shown kinds and never through the other hub: the way the pulse
 * of a new memory travels before it lights up.
 * @param graph the graph
 * @param id the node
 * @param hidden the kinds the legend hides
 * @returns the ids from the hub to the node; just the node for a hub, empty when no hub can be reached
 */
export function pathFromHub(graph: MapGraph, id: string, hidden: ReadonlySet<Kind> = new Set()): string[] {
  const start = graph.byId.get(id)
  if (!start) return []
  const from = new Map<string, string>([[id, ""]]) // each node reached, and the node it was reached from
  let edge = [start]
  while (edge.length) {
    const next: MapNode[] = []
    for (const n of edge) {
      if (isHub(n.kind)) {
        const path = [n.id]
        for (let back = from.get(n.id); back; back = from.get(back)) path.push(back)
        return path
      }
      for (const m of graph.near.get(n.id) ?? []) {
        if (from.has(m.id) || hidden.has(m.kind)) continue
        from.set(m.id, n.id)
        next.push(m)
      }
    }
    edge = next
  }
  return []
}

/**
 * Whose names show: the hubs, the best-connected concepts, and the selected node with its best-connected neighbours
 * (a few dozen names at most, so they stay readable).
 * @param graph the graph
 * @param selected the selected node's id
 * @param hidden the kinds the legend hides
 * @returns the ids whose names show
 */
export function labelled(graph: MapGraph, selected: string | null, hidden: ReadonlySet<Kind> = new Set()) {
  const byDegree = (a: MapNode, b: MapNode) => b.degree - a.degree
  const concepts = graph.nodes.filter((n) => n.kind === "concept").sort(byDegree)
  const near = (selected && graph.near.get(selected)?.filter((n) => !hidden.has(n.kind))) || []
  return new Set([
    ...graph.nodes.filter((n) => isHub(n.kind)).map((n) => n.id),
    ...concepts.slice(0, 16).map((n) => n.id),
    ...(selected && graph.byId.has(selected) ? [selected] : []),
    ...near
      .sort(byDegree)
      .slice(0, 24)
      .map((n) => n.id),
  ])
}

/**
 * The nodes whose names hold the query: names that start with it (or have a word that does) first, then the rest; the
 * better connected first within each (the command palette searches his memory with it).
 * @param nodes the nodes (the map's, or GET /api/neural's)
 * @param query what was typed
 * @param hidden the kinds to leave out
 * @param limit how many at most
 * @returns the matches
 */
export function search<N extends { label: string; kind: Kind; degree: number }>(
  nodes: readonly N[],
  query: string,
  hidden: ReadonlySet<Kind> = new Set(),
  limit = 8,
): N[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const rank = (n: N) => {
    const label = n.label.toLowerCase()
    if (label.startsWith(q) || label.includes(` ${q}`)) return 0
    return label.includes(q) ? 1 : 2
  }
  return nodes
    .filter((n) => !hidden.has(n.kind))
    .map((n) => [n, rank(n)] as const)
    .filter(([, r]) => r < 2)
    .sort((a, b) => a[1] - b[1] || b[0].degree - a[0].degree)
    .slice(0, limit)
    .map(([n]) => n)
}

/**
 * Whether he has learned nothing yet: no facts, unverified memories, notes of his, journal days or notes (the hubs,
 * the skills and the concepts don't count).
 * @param counts the server's counts by kind
 * @returns true while there is nothing learned to show
 */
export const isEmpty = (counts: NeuralGraph["counts"]) =>
  (["fact", "unverified", "insight", "day", "note"] as const).every((kind) => !counts[kind])
