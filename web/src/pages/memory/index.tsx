/**
 * Memory: his memory in 3D. You and Orion are the hubs; round them the facts he knows about you (and the unverified
 * ones he keeps apart), his own notes, the journal of your conversations, your notes and his skills, joined through
 * the concepts they share, in the open network the forces settle into. The legend shows or hides a kind, a selected
 * node dims everything beyond its neighbourhood and opens its inspector, and the command palette searches it (a node
 * it finds comes as ?node=…). When he learns something a pulse runs out from its hub, the new node flashes, and a
 * short "Learned" toast says what. The map draws only while something happens.
 */
import { Focus, Search, Waypoints } from "lucide-react"
import { AnimatePresence } from "motion/react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useLocation, useSearch } from "wouter"
import { queryClient, useNeural } from "@/lib/api"
import { fieldOf } from "@/lib/hooks"
import { OVERLAYS } from "@/lib/nav"
import { setPalette } from "@/lib/stage"
import { useLive } from "@/lib/store"
import type { NeuralGraph } from "@/lib/types"
import { cn } from "@/lib/utils"
import { onEvent } from "@/lib/ws"
import { Button, IconButton } from "@/ui/button"
import { Kbd, modKey } from "@/ui/kbd"
import { Empty, Failed, Pending } from "@/ui/states"
import { toast } from "@/ui/toast"
import type { Cover } from "./controller"
import { isEmpty, isHub, type Kind, type MapGraph, shapeGraph } from "./graph"
import { type MapHandle, NeuralMap } from "./map"
import { Legend, NodePanel } from "./overlays"

/** Kept between visits, like the camera (controller.ts): the graph, so every node keeps its place, and the view. */
const kept: { graph: MapGraph | null; hops: number; hidden: ReadonlySet<Kind> } = {
  graph: null,
  hops: 2,
  hidden: new Set(),
}

const TOP = 112 // px: the top bar and the controls under it
const LEARNED_MS = 10_000 // how long after a memory event a new node still counts as just learned

/**
 * The server's graph shaped for the map, reusing the last one's nodes and links.
 * @param data what GET /api/neural sent
 * @returns the graph, or null before the first load
 */
function useGraph(data: NeuralGraph | undefined) {
  const graph = useMemo(() => (data ? shapeGraph(data, kept.graph, innerHeight > innerWidth) : null), [data])
  useEffect(() => {
    if (graph) kept.graph = graph
  }, [graph])
  return graph
}

/**
 * He learned something (a memory event): once the refreshed graph brings it in, a short toast says what.
 * @param graph the graph
 */
function useLearned(graph: MapGraph | null) {
  const heard = useRef(0)
  useEffect(() => {
    const mark = () => {
      heard.current = Date.now()
    }
    const offs = [onEvent("memory_update", mark), onEvent("memory_review", mark)]
    return () => {
      for (const off of offs) off()
    }
  }, [])
  useEffect(() => {
    if (!graph?.fresh.length || Date.now() - heard.current > LEARNED_MS) return
    heard.current = 0
    const fresh = new Set(graph.fresh)
    const learned = graph.nodes.filter((n) => fresh.has(n.id) && !isHub(n.kind))
    const memories = learned.filter((n) => n.kind !== "concept")
    const text = (memories.length ? memories : learned).map((n) => n.label).join(" · ")
    if (text)
      toast.add({
        title: "Learned",
        description: text.length > 160 ? `${text.slice(0, 157)}…` : text,
        type: "success",
        timeout: 4000,
      })
  }, [graph])
}

/**
 * The memory page.
 * @returns the page
 */
export default function Memory() {
  const neural = useNeural()
  const graph = useGraph(neural.data)
  const [selected, setSelected] = useState<string | null>(null)
  const [hops, setHops] = useState(kept.hops)
  const [hidden, setHidden] = useState(kept.hidden)
  const map = useRef<MapHandle>(null)
  const legend = useRef<HTMLFieldSetElement>(null)
  const query = useSearch()
  const [, navigate] = useLocation()
  useEffect(() => {
    Object.assign(kept, { hops, hidden })
  }, [hops, hidden])
  useLearned(graph)

  /** What lies over the map: the top bar and the controls, the legend along the bottom, and the inspector (a column
   *  on the right from 1024 px, else up to about half the height along the bottom, where it hides the legend). */
  const cover = useCallback<Cover>((_width, height, panel) => {
    const wide = matchMedia("(min-width: 64rem)").matches
    const shown = legend.current?.offsetHeight ? legend.current : null
    const under = shown ? height - shown.offsetTop : 0
    return {
      top: TOP,
      right: panel && wide ? 400 : 0,
      bottom: Math.max(under, panel && !wide ? Math.round(height * 0.52) + 12 : 0),
    }
  }, [])

  // a node that was forgotten, or whose kind is hidden, isn't selected
  const node = selected ? graph?.byId.get(selected) : undefined
  const chosen = node && !hidden.has(node.kind) ? node : undefined
  const go = useCallback((id: string) => {
    setSelected(id)
    map.current?.flyTo(id)
  }, [])
  const toggle = (kind: Kind) =>
    setHidden((before) => {
      const next = new Set(before)
      if (!next.delete(kind)) next.add(kind)
      return next
    })

  // a node the palette found (?node=…): select it and fly there, once the graph has it
  useEffect(() => {
    const id = new URLSearchParams(query).get("node")
    if (!id || !graph?.byId.has(id)) return
    setHidden((before) => {
      const kind = graph.byId.get(id)?.kind
      if (!kind || !before.has(kind)) return before
      const next = new Set(before)
      next.delete(kind)
      return next
    })
    // wait a frame: the map takes the graph first; then the address forgets the node (not before: the new address
    // runs this again, and its cleanup would cancel the frame)
    const frame = requestAnimationFrame(() => {
      go(id)
      navigate("/memory", { replace: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [query, graph, go, navigate])

  // the link back after a failed read: read again
  useEffect(
    () =>
      useLive.subscribe((now, before) => {
        if (
          now.link === "online" &&
          before.link !== "online" &&
          queryClient.getQueryState(["neural"])?.status === "error"
        )
          void queryClient.refetchQueries({ queryKey: ["neural"], exact: true })
      }),
    [],
  )

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = fieldOf(e.target)
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || typing || document.querySelector(OVERLAYS)) return
      if (e.key === "/") {
        e.preventDefault()
        setPalette(true)
      } else if (e.key === "f" || e.key === "F") map.current?.fit()
      else if (e.key === "Escape")
        setSelected(null) // only that: Esc never leaves the page
      else if (e.key === "1" || e.key === "2" || e.key === "3") setHops(Number(e.key))
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  }, [])

  const counts = neural.data?.counts
  return (
    <div data-testid="neural" className="relative h-full overflow-hidden bg-void-950">
      {graph && (
        <NeuralMap
          ref={map}
          graph={graph}
          selected={chosen?.id ?? null}
          hops={hops}
          hidden={hidden}
          cover={cover}
          onSelect={setSelected}
        />
      )}
      <div className="pointer-events-none absolute inset-x-0 top-(--bar-h) flex items-center gap-2 px-3 pt-3 tab:px-5">
        <div className="pointer-events-auto flex items-center gap-2.5 pr-2">
          <Waypoints aria-hidden="true" className="size-4 text-rigel-300" />
          <h1 className="font-serif text-2xl text-steel-50">His memory</h1>
        </div>
        <Button
          className="glass pointer-events-auto rounded-full text-steel-400"
          onClick={() => setPalette(true)}
          disabled={!graph}
          data-testid="neural-search"
        >
          <Search aria-hidden="true" />
          Search
          <Kbd className="ml-1">{modKey()}</Kbd>
          <Kbd>K</Kbd>
        </Button>
        <IconButton
          label="Fit everything in view"
          shortcut="F"
          tip="bottom"
          tone="secondary"
          className="glass pointer-events-auto rounded-full"
          onClick={() => map.current?.fit()}
          disabled={!graph}
          data-testid="neural-fit"
        >
          <Focus />
        </IconButton>
      </div>
      {graph && counts && (
        <Legend
          ref={legend}
          counts={counts}
          hidden={hidden}
          onToggle={toggle}
          className={cn(
            "no-scrollbar absolute inset-x-3 bottom-3 overflow-x-auto tab:inset-x-5 tab:bottom-5 tab:flex-wrap tab:overflow-visible",
            chosen && "max-desk:hidden desk:right-[25rem]", // beside the inspector, or under it on a narrower screen
          )}
        />
      )}
      <AnimatePresence>
        {graph && chosen && (
          <NodePanel
            key="panel"
            node={chosen}
            graph={graph}
            hidden={hidden}
            hops={hops}
            onHops={setHops}
            onChoose={go}
            onClose={() => setSelected(null)}
            className="absolute max-desk:inset-x-3 max-desk:bottom-3 max-desk:mx-auto max-desk:max-h-[52%] max-desk:max-w-xl desk:top-[calc(var(--bar-h)+0.75rem)] desk:right-5 desk:bottom-5 desk:w-[23rem]"
          />
        )}
      </AnimatePresence>
      {!graph ? (
        <div className="absolute inset-0 grid place-items-center p-6" data-testid="neural-status">
          {neural.isError ? (
            <Failed
              title="His memory can't be read right now"
              error={neural.error}
              onRetry={() => void neural.refetch()}
            />
          ) : (
            <Pending label="Reading his memory" delay={150} page />
          )}
        </div>
      ) : (
        counts &&
        isEmpty(counts) && (
          <div className="pointer-events-none absolute inset-x-0 bottom-24 grid justify-items-center px-3">
            <div data-testid="neural-empty" className="glass max-w-sm rounded-card px-5">
              <Empty icon={<Waypoints />}>
                Nothing learned yet. What he learns about you, his notes and your conversations appear here, joined by
                what they share.
              </Empty>
            </div>
          </div>
        )
      )}
    </div>
  )
}
