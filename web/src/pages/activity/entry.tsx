/**
 * One entry of the activity log: its time and kind on the log's line, what happened and who asked, how it ended; it
 * opens to everything recorded about it (when, who, which connector and tool, how long, and the detail, with secrets
 * already left out by the server). Shared by the Activity page and a connector's own Activity tab. A row is skipped by
 * the browser's layout and paint while it is off screen (`content-visibility`), so a long log stays light.
 */
import {
  Archive,
  Ban,
  BellRing,
  CalendarClock,
  ChevronDown,
  Cog,
  FlaskConical,
  KeyRound,
  LogIn,
  type LucideIcon,
  Plug,
  Puzzle,
  ShieldQuestion,
  ShieldX,
  Sparkles,
  Wrench,
  Zap,
} from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { memo, useState } from "react"
import { KINDS, outcomeTone, SOURCES } from "@/lib/activity"
import { clockTime } from "@/lib/format"
import { FOLD } from "@/lib/motion"
import { msText } from "@/lib/stages"
import type { Activity, ActivityKind } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { CopyButton } from "@/ui/copy-button"

/** Each kind's icon and the colour of its mark on the line. */
const LOOK: Record<ActivityKind, { icon: LucideIcon; tone: string }> = {
  tool: { icon: Wrench, tone: "text-steel-300" },
  ask: { icon: ShieldQuestion, tone: "text-dusk-300" },
  deny: { icon: Ban, tone: "text-alert-300" },
  block: { icon: ShieldX, tone: "text-alert-300" },
  alert: { icon: BellRing, tone: "text-alert-300" },
  job: { icon: CalendarClock, tone: "text-nebula-300" },
  instant: { icon: Zap, tone: "text-dusk-300" },
  backup: { icon: Archive, tone: "text-aurora-300" },
  connector: { icon: Plug, tone: "text-rigel-300" },
  plugin: { icon: Puzzle, tone: "text-rigel-300" },
  skill: { icon: Sparkles, tone: "text-nebula-300" },
  secret: { icon: KeyRound, tone: "text-betel-300" },
  login: { icon: LogIn, tone: "text-rigel-300" },
  bench: { icon: FlaskConical, tone: "text-aurora-300" },
  system: { icon: Cog, tone: "text-steel-300" },
}

/**
 * An entry's outcome as the badge says it: "ok" is "Done", the rest capitalised.
 * @param outcome its word
 * @returns the word for the badge
 */
const outcomeWord = (outcome: string) =>
  outcome === "ok" ? "Done" : outcome.charAt(0).toUpperCase() + outcome.slice(1)

/**
 * One entry; only one whose entry changed renders again.
 * @param props the `item`
 * @returns the row
 */
export const Entry = memo(function Entry({ item }: { item: Activity }) {
  const [open, setOpen] = useState(false)
  const look = LOOK[item.kind] ?? LOOK.system
  const when = new Date(item.at * 1000)
  const who = [item.actor, SOURCES[item.source] ?? item.source].filter(Boolean).join(" · ")
  const where = [item.connector, item.tool].filter(Boolean).join(" · ")
  const tone = outcomeTone(item.outcome)
  const facts: [string, string][] = [
    ["When", when.toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "medium" })],
    ["Kind", KINDS[item.kind] ?? item.kind],
    ["From", SOURCES[item.source] ?? item.source],
    ["By", item.actor],
    ["Connector", item.connector],
    ["Tool", item.tool],
    ["Took", item.ms > 0 ? msText(item.ms) : ""],
    ["Turn", item.turn],
  ]
  return (
    <li
      className="relative [contain-intrinsic-size:auto_3.5rem] [content-visibility:auto]"
      data-testid={`activity-${item.id}`}
      data-kind={item.kind}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="group/entry grid w-full grid-cols-[2.75rem_2rem_minmax(0,1fr)_auto] items-start gap-x-2 rounded-control px-1.5 py-2 text-left outline-none transition-colors duration-(--dur-instant) hover:bg-steel-50/4 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-rigel-400 aria-expanded:bg-steel-50/4 tab:gap-x-3"
      >
        <time dateTime={when.toISOString()} className="pt-1.5 text-right font-mono text-xs text-steel-400 tabular-nums">
          {clockTime(when, false)}
        </time>
        <span className="relative z-1 grid place-items-center">
          <span
            className={cn(
              "grid size-7 place-items-center rounded-full bg-void-900 shadow-[inset_0_0_0_1px_var(--hairline-strong)]",
              look.tone,
            )}
          >
            <look.icon aria-label={KINDS[item.kind] ?? item.kind} className="size-3.5" />
          </span>
        </span>
        <span className="min-w-0 pt-0.5">
          <span className="block truncate text-sm text-steel-50">{item.title || item.tool || item.kind}</span>
          <span className="block truncate text-xs text-steel-400">{[who, where].filter(Boolean).join(" · ")}</span>
        </span>
        <span className="flex items-center gap-2 pt-1">
          {item.outcome && <Badge tone={tone}>{outcomeWord(item.outcome)}</Badge>}
          <ChevronDown
            aria-hidden="true"
            className="size-3.5 shrink-0 text-steel-500 transition-transform duration-(--dur-base) group-aria-expanded/entry:rotate-180"
          />
        </span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div {...FOLD} className="overflow-hidden">
            <div className="mr-1.5 mb-2 ml-1.5 grid gap-3 rounded-control bg-void-950/45 p-3 shadow-[inset_0_0_0_1px_var(--hairline)] tab:ml-[calc(0.375rem+2.75rem+2rem+1.5rem)]">
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs">
                {facts
                  .filter(([, value]) => value)
                  .map(([name, value]) => (
                    <div key={name} className="contents">
                      <dt className="text-steel-500">{name}</dt>
                      <dd className="break-words font-mono text-steel-200">{value}</dd>
                    </div>
                  ))}
              </dl>
              {item.detail && (
                <div className="relative">
                  <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words pr-8 font-mono text-xs text-steel-300 leading-relaxed">
                    {item.detail}
                  </pre>
                  <CopyButton text={item.detail} label="Copy the detail" className="absolute -top-1 -right-1" />
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </li>
  )
})
