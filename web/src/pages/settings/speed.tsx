/**
 * How fast he answers: for each brain, the median of its last 200 turns from the end of your words to his first,
 * as one bar on a scale the brains share, split into its stages (hearing you, thinking, his tools, speaking), with the
 * 95th percentile beside it (a slow turn now and then shows there, not in the median). The figures under each bar say
 * every stage, so nothing is told by colour alone.
 */
import { Gauge } from "lucide-react"
import { missing, useTimings } from "@/lib/api"
import { brainLabel, msText, STAGES, stageParts } from "@/lib/stages"
import type { TimingsResponse } from "@/lib/types"
import { cn } from "@/lib/utils"
import { STAGE_COLORS } from "@/stage/stages"
import { Failed, Pending } from "@/ui/states"
import { Note, Panel } from "./fields"

/**
 * The panel.
 * @returns the panel (nothing on an Orion that doesn't measure turns)
 */
export function Speed() {
  const timings = useTimings()
  if (missing(timings.error)) return null
  return (
    <Panel
      title="How fast he answers"
      icon={Gauge}
      description="Each brain's median over its last 200 turns, to his first word"
      data-testid="brain-speed"
    >
      {timings.isError ? (
        <div className="px-5 pb-4">
          <Failed title="The timings can't be read" error={timings.error} onRetry={() => void timings.refetch()} />
        </div>
      ) : timings.isPending ? (
        <Pending label="Reading the timings" delay={150} className="px-5 pb-4" />
      ) : timings.data.stats.length ? (
        <Brains stats={timings.data.stats} />
      ) : (
        <Note className="border-t-0 pt-0">
          No turn measured yet: each turn's stages show here once he has answered.
        </Note>
      )}
    </Panel>
  )
}

/**
 * Each brain's bar, on one scale, with the stages' key above them.
 * @param props the brains' `stats`
 * @returns the bars
 */
function Brains({ stats }: { stats: TimingsResponse["stats"] }) {
  const longest = Math.max(1, ...stats.map((s) => s.p50.total_ms))
  return (
    <div className="grid gap-4 border-t px-5 pt-3.5 pb-5">
      <ul aria-label="The stages" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-steel-300">
        {STAGES.map((stage) => (
          <li key={stage.key} className="flex items-center gap-1.5">
            <span aria-hidden="true" className={cn("size-2.5 rounded-[3px]", STAGE_COLORS[stage.key])} />
            {stage.name}
          </li>
        ))}
      </ul>
      <ul className="grid gap-4">
        {stats.map((s) => {
          const parts = stageParts(s.p50)
          return (
            <li key={s.brain} className="grid gap-1.5" data-testid={`speed-${s.brain}`}>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                <span className="min-w-0 flex-1 truncate text-sm text-steel-100">{brainLabel(s.brain)}</span>
                <span className="text-xs text-steel-400">
                  {s.turns} turn{s.turns === 1 ? "" : "s"}
                </span>
                <span className="font-mono text-sm text-steel-50 tabular-nums">{msText(s.p50.total_ms)}</span>
                <span className="font-mono text-xs text-steel-400 tabular-nums">95%: {msText(s.p95.total_ms)}</span>
              </div>
              <div
                aria-hidden="true"
                className="flex h-2.5 gap-0.5"
                style={{ width: `${Math.max(2, (s.p50.total_ms / longest) * 100)}%` }}
              >
                {parts.map((p) => (
                  <span
                    key={p.key}
                    title={`${p.name}: ${msText(p.ms)}`}
                    className={cn("h-full min-w-0.5 last:rounded-r-[4px]", STAGE_COLORS[p.key])}
                    style={{ flexGrow: p.ms, flexBasis: 0 }}
                  />
                ))}
              </div>
              <p className="font-mono text-micro text-steel-400 tabular-nums">
                {parts.map((p) => `${p.label} ${msText(p.ms)}`).join(" · ")}
              </p>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
