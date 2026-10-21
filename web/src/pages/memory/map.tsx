/**
 * The neural map's 3D view: react-force-graph-3d (three.js with a d3 force layout) lays the graph out, draws it and
 * moves the camera. nodes.ts is how a node and a link look; controller.ts runs the rest: the drift, the stars' life,
 * the sparks that fire along the links as he listens, thinks and speaks (30 frames a second, 60 while you move the
 * camera; with reduced motion it draws only while something moves). The links take his state's tint.
 */
import { useReducedMotion } from "motion/react"
import { type Ref, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react"
import ForceGraph3D from "react-force-graph-3d"
import { CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js"
import { stateTint } from "@/lib/palette"
import { useLive } from "@/lib/store"
import { Controller, type Cover, endId, type Graph, lookOf, paintLink, type State } from "./controller"
import { type Kind, labelled, type MapGraph, type MapLink, type MapNode, within } from "./graph"
import { makeNode, paint, update } from "./nodes"

const RENDERER = { antialias: true, alpha: true, powerPreference: "low-power", premultipliedAlpha: true } as const
const noTooltip = () => ""

/** What the page can ask of the map. */
export interface MapHandle {
  fit(): void
  flyTo(id: string): void
}

interface Props {
  graph: MapGraph
  selected: string | null
  hops: number
  hidden: ReadonlySet<Kind>
  cover: Cover
  onSelect: (id: string | null) => void
  ref?: Ref<MapHandle>
}

/**
 * The map, while the page is shown.
 * @param props the `graph`, the `selected` node, how many `hops` round it stay lit, the `hidden` kinds, what `cover`s
 *   the map, `onSelect`, and a `ref` for fitting and flying
 * @returns the map's box, with the scene in it once it has a size
 */
export function NeuralMap(props: Props) {
  const [host, setHost] = useState<HTMLDivElement | null>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  // The scene exists while the page is shown and is torn down when it goes (the size drops to 0): under React's
  // <Activity> a hidden page loses its effects, and react-force-graph-3d can't come back from that. The nodes keep
  // their places and the camera is kept, so it comes back as it was.
  useEffect(() => {
    if (!host) return
    const watch = new ResizeObserver(() => setSize({ width: host.clientWidth, height: host.clientHeight }))
    watch.observe(host)
    return () => {
      watch.disconnect()
      setSize({ width: 0, height: 0 })
    }
  }, [host])
  return (
    <div ref={setHost} data-testid="neural-canvas" className="absolute inset-0 isolate overflow-hidden">
      {host && size.width > 0 && <Scene {...props} {...size} host={host} />}
    </div>
  )
}

/**
 * The scene: the library, and the controller that runs it.
 * @param props the map's props, its `width` and `height`, and the `host` element
 * @returns the force graph
 */
function Scene({
  graph,
  selected,
  hops,
  hidden,
  cover,
  onSelect,
  ref,
  width,
  height,
  host,
}: Props & { width: number; height: number; host: HTMLDivElement }) {
  const fg = useRef<Graph>(undefined)
  const ctl = useRef<Controller>(null)
  const still = useReducedMotion() ?? false
  const mood = useLive((live) => live.state)
  const tint = stateTint(mood)
  const [labels] = useState(() => [new CSS2DRenderer()])
  const data = useMemo(() => ({ nodes: graph.nodes, links: graph.links }), [graph])
  const focus = useMemo(
    () => (selected ? within(graph, selected, hops, hidden) : null),
    [graph, selected, hops, hidden],
  )
  const named = useMemo(() => labelled(graph, selected, hidden), [graph, selected, hidden])

  // The state the library's callbacks and the frame loop read, brought up to date before the library's next update
  // (a layout effect). When what is drawn changed (a selection, the hops, the legend, his state, new data), the nodes
  // and links are repainted and drawn: the loop may be resting.
  const [s] = useState<State>(() => ({
    graph,
    selected,
    focus,
    named,
    hidden,
    still,
    mood,
    tint,
    cover,
    onSelect,
    hovered: null,
    flashes: new Map(),
    pulses: new Map(),
  }))
  useLayoutEffect(() => {
    const nodes = s.focus !== focus || s.named !== named || s.hidden !== hidden
    const links = s.focus !== focus || s.tint !== tint || s.graph !== graph
    const moved = s.mood !== mood
    Object.assign(s, { graph, selected, focus, named, hidden, still, mood, tint, cover, onSelect })
    if (nodes) for (const n of graph.nodes) paint(n, lookOf(s, n))
    if (links) for (const l of graph.links) paintLink(s, l)
    if (nodes || moved) ctl.current?.wake()
    else if (links) ctl.current?.wake(600) // a new tint: enough to draw it
  })

  // callbacks that never change: a new nodeThreeObject would make the library rebuild every node
  const on = useMemo(
    () => ({
      node: (n: MapNode) => makeNode(n, lookOf(s, n)),
      material: (l: MapLink) => paintLink(s, l),
      click: (n: MapNode) => s.onSelect(n.id),
      background: () => s.onSelect(null),
      hover: (n: MapNode | null) => {
        const before = s.graph.byId.get(s.hovered ?? "")
        s.hovered = n?.id ?? null
        for (const m of [before, n]) if (m) paint(m, lookOf(s, m))
      },
      settled: () => ctl.current?.settled(),
    }),
    [s],
  )
  // these do change with the legend: the library then shows or hides the nodes and links
  const visible = useMemo(
    () => ({
      node: (n: MapNode) => !hidden.has(n.kind),
      link: (l: MapLink) =>
        [l.source, l.target].every((end) => {
          const n = graph.byId.get(endId(end))
          return n !== undefined && !hidden.has(n.kind)
        }),
    }),
    [graph, hidden],
  )

  useLayoutEffect(() => {
    if (!fg.current) return
    const c = new Controller(fg.current, host, s)
    ctl.current = c
    return () => {
      c.dispose()
      ctl.current = null
    }
  }, [host, s])

  // new data: refresh what changed on the nodes it kept, flash what's new, and let the layout run
  useLayoutEffect(() => {
    for (const n of graph.nodes) update(n)
    for (const id of graph.fresh.slice(0, 12)) s.flashes.set(id, 0)
    ctl.current?.loaded()
  }, [graph, s])

  useEffect(() => {
    if (!width || !height) return
    ctl.current?.wake(500) // a new size: draw it
  }, [width, height])

  // a new selection sends pulses out through its neighbourhood
  const before = useRef(selected)
  useEffect(() => {
    if (focus && selected !== before.current) ctl.current?.burst(focus)
    before.current = selected
  }, [focus, selected])

  useImperativeHandle(ref, () => ({ fit: () => ctl.current?.fit(), flyTo: (id) => ctl.current?.flyTo(id) }), [])

  return (
    <ForceGraph3D<MapNode, MapLink>
      ref={fg}
      width={width}
      height={height}
      controlType="orbit"
      rendererConfig={RENDERER}
      extraRenderers={labels}
      backgroundColor="rgba(0,0,0,0)"
      showNavInfo={false}
      graphData={data}
      nodeThreeObject={on.node}
      nodeVisibility={visible.node}
      nodeLabel={noTooltip}
      linkMaterial={on.material}
      linkVisibility={visible.link}
      enableNodeDrag={false}
      warmupTicks={graph.settled ? 0 : still ? 300 : 40}
      cooldownTicks={graph.settled || still ? 0 : Number.POSITIVE_INFINITY}
      cooldownTime={8000}
      d3AlphaMin={0.02}
      onEngineStop={on.settled}
      onNodeClick={on.click}
      onNodeHover={on.hover}
      onBackgroundClick={on.background}
    />
  )
}
