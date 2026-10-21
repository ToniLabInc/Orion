/**
 * A turn's stages, under his answer: one thin bar of its parts in their colours (hearing you, thinking, his tools,
 * speaking), how long until his first word, and each part in figures. The live turn's arrive as a `timing` event; the
 * ones before the page opened come from GET /api/timings, read once while the conversation shows.
 */
import { useQuery } from "@tanstack/react-query"
import { timingsQuery } from "@/lib/api"
import { msText, type StageKey, stageParts } from "@/lib/stages"
import { useLive } from "@/lib/store"
import type { Timing } from "@/lib/types"

/** Each stage's colour (see lib/stages.ts). */
export const STAGE_COLORS: Record<StageKey, string> = {
  heard_ms: "bg-betel-500",
  think_ms: "bg-nebula-500",
  tools_ms: "bg-dusk-600",
  speak_ms: "bg-aurora-600",
}

/**
 * A turn's stages: live from the store, else from the latest turns the server measured.
 * @param turn the turn's id
 * @returns its stages, or undefined while none are known
 */
function useTiming(turn: string): Timing | undefined {
  const live = useLive((s) => s.timings[turn])
  const past = useQuery({
    ...timingsQuery(),
    select: (data) => data.items.find((t) => t.turn === turn),
    enabled: !live,
  })
  return live ?? past.data
}

/**
 * The strip.
 * @param props the `turn`'s id
 * @returns the strip, or nothing while the turn's stages aren't known
 */
export function StageStrip({ turn }: { turn: string }) {
  const timing = useTiming(turn)
  if (!timing || timing.total_ms <= 0) return null
  const parts = stageParts(timing)
  const words = parts.map((p) => `${p.label} ${msText(p.ms)}`).join(" · ")
  return (
    <p
      className="mt-3 flex items-center gap-2.5 border-t pt-2.5 font-mono text-micro text-steel-500 tabular-nums"
      title={parts.map((p) => `${p.name}: ${Math.round(p.ms)} ms`).join("\n")}
      data-testid="stage-strip"
    >
      <span aria-hidden="true" className="flex h-1 w-20 shrink-0 gap-px overflow-hidden rounded-full bg-steel-50/6">
        {parts.map((p) => (
          <span key={p.key} className={STAGE_COLORS[p.key]} style={{ width: `${p.share * 100}%` }} />
        ))}
      </span>
      <span className="shrink-0 text-steel-300">{msText(timing.total_ms)}</span>
      <span className="sr-only">to his first word:</span>
      <span className="min-w-0 truncate">{words}</span>
    </p>
  )
}
