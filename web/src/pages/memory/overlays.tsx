/**
 * What lies over the memory map: the legend (a chip per kind shows or hides it), each kind's icon in its shape and
 * shade, and the selected node's inspector, a glass sheet (what it is, what it says, when and how he learned it, how
 * far round it the map stays lit, and what it links to; a memory can be forgotten, an unverified one confirmed). The
 * graph carries no text: the inspector reads a node's when it opens. Searching is the command palette's.
 */
import { Check, Trash2, X } from "lucide-react"
import { motion } from "motion/react"
import type { Ref } from "react"
import { useConfirmNode, useForgetNode, useNeuralNode } from "@/lib/api"
import { shortDate } from "@/lib/format"
import { DUR, SPRING } from "@/lib/motion"
import type { NeuralGraph, NeuralNode } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Button, IconButton } from "@/ui/button"
import { Failed, Pending } from "@/ui/states"
import { Segmented } from "@/ui/tabs"
import { toast } from "@/ui/toast"
import { isHub, KINDS, type Kind, LEGEND, type MapGraph, type MapNode, type Shape } from "./graph"

const PATHS: Record<Shape, string> = {
  octahedron: "M6 .8 11.2 6 6 11.2.8 6ZM6 3.4 8.6 6 6 8.6 3.4 6Z",
  core: "M6 3.4a2.6 2.6 0 1 1 0 5.2 2.6 2.6 0 0 1 0-5.2ZM1.2 8.2a5.3 1.9-25 1 0 9.6-4.4 5.3 1.9-25 1 0-9.6 4.4Z",
  sphere: "M6 1.4a4.6 4.6 0 1 1 0 9.2 4.6 4.6 0 0 1 0-9.2Z",
  diamond: "M6 .8 11.2 6 6 11.2.8 6Z",
  dashed: "M6 .8 11.2 6 6 11.2.8 6Z",
  hollow: "M6 .8 11.2 6 6 11.2.8 6Z",
  cube: "M1.8 1.8h8.4v8.4H1.8Z",
  hexagon: "M6 .9 10.6 3.5v5L6 11.1 1.4 8.5v-5Z",
  triangle: "M6 1.2 11 10.4H1Z",
}

/**
 * A kind's shape in its shade: filled lightly with a bright edge, like the solids on the map (an unverified memory's
 * diamond only in dashes).
 * @param props the `kind`, and whether it is switched `off` in the legend
 * @returns the icon
 */
export function KindIcon({ kind, off, className }: { kind: Kind; off?: boolean; className?: string }) {
  const { shape, token } = KINDS[kind]
  return (
    <svg
      viewBox="0 0 12 12"
      aria-hidden="true"
      className={cn("size-3 shrink-0", className)}
      style={{ color: `var(--color-${off ? "steel-600" : token})` }}
      fill="currentColor"
      fillOpacity={shape === "hollow" || shape === "dashed" || off ? 0 : 0.35}
      stroke="currentColor"
      strokeWidth={1.2}
      strokeLinejoin="round"
      strokeDasharray={shape === "dashed" ? "1.9 1.3" : undefined}
    >
      <path d={PATHS[shape]} />
    </svg>
  )
}

/**
 * One chip per kind he has, with how many: pressing it hides that kind (and again shows it).
 * @param props the server's `counts`, the `hidden` kinds, `onToggle`, and a `ref` the page measures
 * @returns the legend
 */
export function Legend({
  counts,
  hidden,
  onToggle,
  className,
  ref,
}: {
  counts: NeuralGraph["counts"]
  hidden: ReadonlySet<Kind>
  onToggle: (kind: Kind) => void
  className?: string
  ref?: Ref<HTMLFieldSetElement>
}) {
  return (
    <fieldset ref={ref} data-testid="neural-legend" className={cn("m-0 flex min-w-0 gap-1.5 border-0 p-0", className)}>
      <legend className="sr-only">What the map shows</legend>
      {LEGEND.filter((kind) => (counts[kind] ?? 0) > 0).map((kind) => {
        const shown = !hidden.has(kind)
        return (
          <button
            key={kind}
            type="button"
            aria-pressed={shown}
            onClick={() => onToggle(kind)}
            data-testid={`neural-legend-${kind}`}
            className="glass flex h-8 shrink-0 items-center gap-2 rounded-full px-3 text-xs text-steel-200 outline-none transition-[color,opacity] duration-(--dur-quick) hover:text-steel-50 focus-visible:outline-2 focus-visible:outline-rigel-400 aria-[pressed=false]:text-steel-500 aria-[pressed=false]:opacity-70"
          >
            <KindIcon kind={kind} off={!shown} />
            {KINDS[kind].name}
            <span className="font-mono text-micro text-steel-500 tabular-nums">{counts[kind]}</span>
          </button>
        )
      })}
    </fieldset>
  )
}

/** The kinds of node that can be forgotten one by one (or, unverified, confirmed): memories themselves. */
const FORGETTABLE: ReadonlySet<Kind> = new Set(["fact", "insight", "unverified", "day", "note"])

/** The order the inspector lists a node's links in: the hubs, then the legend's order. */
const ORDER: Kind[] = ["you", "orion", ...LEGEND]

/** Where a memory came from, as the inspector says it (a memory from before this was kept says nothing). */
const SOURCES: Partial<Record<string, string>> = {
  brain: "in conversation",
  tainted: "from web or app content",
  hud: "in the HUD",
  app: "through an app",
  review: "while reviewing his memory",
  hand: "written by hand",
}

/**
 * What the inspector shows of a node: its title, and what it says beyond it (a long memory's name is its first words,
 * so its whole text becomes the title; a note opens with its title, which isn't said twice; Markdown's heading marks
 * go).
 * @param node the node on the map
 * @param graph the graph, to count a concept's memories
 * @param detail the node as GET /api/neural/node sent it, once it has come
 * @returns the title and the body ("" for none)
 */
function describe(node: MapNode, graph: MapGraph, detail?: NeuralNode) {
  if (node.kind === "concept") {
    const memories = (graph.near.get(node.id) ?? []).filter((m) => !isHub(m.kind)).length
    return { title: node.label, body: `Mentioned in ${memories} ${memories === 1 ? "memory" : "memories"}.` }
  }
  const label = detail?.label ?? node.label
  const text = (detail?.text ?? "").replace(/^#+\s*/gm, "").trim()
  if (label.endsWith("…") && text.startsWith(label.slice(0, -1))) return { title: text, body: "" }
  return { title: label, body: text === label ? "" : (text.startsWith(label) ? text.slice(label.length) : text).trim() }
}

/**
 * The selected node's inspector: a glass sheet on the right of the map, along the bottom on a narrow screen.
 * @param props the `node`, the `graph`, the `hidden` kinds, the focus `hops` with `onHops`, `onChoose` (a linked node)
 *   and `onClose`
 * @returns the inspector
 */
export function NodePanel({
  node,
  graph,
  hidden,
  hops,
  onHops,
  onChoose,
  onClose,
  className,
}: {
  node: MapNode
  graph: MapGraph
  hidden: ReadonlySet<Kind>
  hops: number
  onHops: (hops: number) => void
  onChoose: (id: string) => void
  onClose: () => void
  className?: string
}) {
  const detail = useNeuralNode(node.kind === "concept" ? null : node.id) // a concept has nothing more to say
  const forget = useForgetNode()
  const trust = useConfirmNode()
  const forgetIt = async () => {
    const text = "It's deleted from his memory for good."
    if (await ask({ title: "Forget this memory?", text, ok: "Forget", danger: true, kicker: "Memory" }))
      forget.mutate(node.id, { onSuccess: onClose })
  }
  const trustIt = () =>
    trust.mutate(node.id, {
      onSuccess: ({ message }) => {
        toast.add({ title: "Memory", description: message, type: "success" })
        onClose()
      },
    })
  const { title, body } = describe(node, graph, detail.data)
  const learned = detail.data?.learned_at
  const source = SOURCES[detail.data?.source ?? ""]
  const near = (graph.near.get(node.id) ?? [])
    .filter((m) => !hidden.has(m.kind))
    .sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind) || b.degree - a.degree)
  return (
    <motion.aside
      data-testid="neural-panel"
      aria-label={`${KINDS[node.kind].name}: ${title}`}
      initial={{ opacity: 0, x: 16 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 12, transition: { duration: DUR.quick } }}
      transition={SPRING.gentle}
      className={cn("glass-solid flex flex-col overflow-hidden rounded-card", className)}
    >
      <header className="flex h-12 shrink-0 items-center gap-2.5 border-b pr-2 pl-4">
        <KindIcon kind={node.kind} className="size-3.5" />
        <span className="font-medium text-xs text-steel-200">{KINDS[node.kind].name}</span>
        <span className="truncate font-mono text-micro text-steel-500">
          {near.length} {near.length === 1 ? "link" : "links"}
        </span>
        <IconButton label="Close" shortcut="Esc" size="sm" className="ml-auto" onClick={onClose}>
          <X />
        </IconButton>
      </header>
      <div className="min-h-0 shrink overflow-y-auto px-4 pt-4">
        <h2 className="text-balance font-serif text-2xl text-steel-50 leading-snug">{title}</h2>
        {learned !== undefined && (
          <p className="mt-2 text-xs text-steel-500">
            Learned {shortDate(new Date(learned * 1000))}
            {source && ` · ${source}`}
          </p>
        )}
        {node.kind === "unverified" && (
          <p className="mt-2 text-sm text-dusk-200">Kept out of his core memory until you confirm it.</p>
        )}
        {detail.isLoading ? (
          <Pending label="Reading" delay={150} className="mt-3" />
        ) : detail.isError ? (
          <Failed
            title="Can't read this memory"
            error={detail.error}
            onRetry={() => void detail.refetch()}
            className="mt-3"
          />
        ) : (
          body && <p className="mt-3 whitespace-pre-line text-sm text-steel-300 leading-relaxed">{body}</p>
        )}
      </div>
      {FORGETTABLE.has(node.kind) && (
        <div className="flex shrink-0 flex-wrap gap-2 px-4 pt-4">
          {node.kind === "unverified" && (
            <Button tone="primary" size="sm" disabled={trust.isPending} onClick={trustIt} data-testid="neural-confirm">
              <Check aria-hidden="true" />
              Confirm
            </Button>
          )}
          <Button tone="danger" size="sm" disabled={forget.isPending} onClick={forgetIt} data-testid="neural-forget">
            <Trash2 aria-hidden="true" />
            Forget
          </Button>
        </div>
      )}
      <div className="flex shrink-0 items-center gap-3 px-4 pt-4">
        <span className="text-xs text-steel-400">Light up</span>
        <Segmented
          value={hops}
          onChange={onHops}
          choices={[1, 2, 3].map((hop) => ({ value: hop, label: String(hop) }))}
          label="How many links away stay lit"
          id="neural-hops"
        />
        <span className="text-xs text-steel-500">{hops === 1 ? "link" : "links"} round it</span>
      </div>
      {near.length > 0 && (
        <>
          <p className="caps shrink-0 px-4 pt-4 pb-1.5 text-steel-500">Linked · {near.length}</p>
          <ul className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
            {near.map((m) => (
              <li key={m.id}>
                <button
                  type="button"
                  onClick={() => onChoose(m.id)}
                  data-testid="neural-link"
                  className="flex w-full items-center gap-2.5 rounded-control px-2 py-1.5 text-left text-sm text-steel-200 outline-none transition-colors hover:bg-steel-50/6 hover:text-steel-50 focus-visible:bg-steel-50/8"
                >
                  <KindIcon kind={m.kind} />
                  <span className="truncate">{m.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </motion.aside>
  )
}
