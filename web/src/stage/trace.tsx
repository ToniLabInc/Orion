/**
 * The trace: what he did for an answer, as it happens, as a row of compact chips. Each chip is one operation (a tool
 * he ran, a memory he saved, the skill he followed, what he recalled): its server's icon, its title, how long it took
 * and how it went (a spinner while it runs, a tick, a cross). A chip opens to its details: the arguments, what it
 * answered and the pictures it returned. A step that waits for your yes glows alert-red and asks inline, with Yes and
 * No. A long trace shows its first chips and how many more.
 */
import {
  BookmarkPlus,
  Brain,
  CalendarClock,
  Check,
  Globe,
  Hand,
  History,
  ListChecks,
  LoaderCircle,
  type LucideIcon,
  Monitor,
  Orbit,
  Plug,
  ScanEye,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { memo, useState } from "react"
import { stepDuration, stepTarget, traceSummary } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { FOLD, POP } from "@/lib/motion"
import type { Step } from "@/lib/store"
import { cn } from "@/lib/utils"
import { answerConfirm } from "@/lib/ws"
import { Button } from "@/ui/button"
import { CopyButton } from "@/ui/copy-button"
import { Pictures } from "@/ui/pictures"

/** How many chips a finished trace shows before "+n". */
const SHOWN = 6

const SERVER_ICONS: Record<string, LucideIcon> = {
  web: Globe,
  system: Monitor,
  organizer: ListChecks,
  memory: Brain,
  skills: Sparkles,
  orion: Orbit,
  core: Orbit,
  eyes: ScanEye,
  automation: CalendarClock,
  hands: Hand,
}

/**
 * A step's icon: by its kind (a memory saved, a recall, a skill), else by its server.
 * @param step the step
 * @returns the icon
 */
export function stepIcon(step: Pick<Step, "kind" | "server">): LucideIcon {
  if (step.kind === "recall") return History
  if (step.kind === "memory") return BookmarkPlus
  if (step.kind === "skill") return Sparkles
  return SERVER_ICONS[step.server] ?? Plug
}

/**
 * An answer's operations.
 * @param props the `steps`, and whether the answer is still `streaming` (a step still marked running in an answer that
 *   ended is shown stopped)
 * @returns the trace
 */
export const Trace = memo(function Trace({ steps, streaming = false }: { steps: Step[]; streaming?: boolean }) {
  const [open, setOpen] = useState<string | null>(null)
  const [all, setAll] = useState(false)
  const running = streaming && steps.some((st) => st.status === "running")
  const now = useNow(1000, running).getTime()
  const asking = steps.find((st) => st.ask)
  const shown = all || streaming ? steps : steps.slice(0, SHOWN)
  const hidden = steps.length - shown.length
  const chosen = steps.find((st) => st.id === open)
  return (
    <div className="grid gap-2" data-testid="trace">
      <ul aria-label={`Operations: ${traceSummary(steps, now)}`} className="flex flex-wrap gap-1.5">
        <AnimatePresence initial={false}>
          {shown.map((st) => (
            <motion.li key={st.id} {...POP} layout="position">
              <Chip
                step={st}
                now={now}
                stale={!streaming}
                open={open === st.id}
                onToggle={() => setOpen((id) => (id === st.id ? null : st.id))}
              />
            </motion.li>
          ))}
        </AnimatePresence>
        {hidden > 0 && (
          <li>
            <button
              type="button"
              onClick={() => setAll(true)}
              className="h-7 rounded-chip px-2 font-mono text-micro text-steel-400 outline-none hover:bg-steel-50/6 hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400"
            >
              +{hidden}
            </button>
          </li>
        )}
      </ul>
      <AnimatePresence initial={false}>
        {chosen && (
          <motion.div key={chosen.id} {...FOLD} className="overflow-hidden">
            <Details step={chosen} running={chosen.status === "running" && streaming} />
          </motion.div>
        )}
      </AnimatePresence>
      <AnimatePresence initial={false}>
        {asking?.ask && (
          <motion.div key={asking.ask.question} {...FOLD} className="overflow-hidden">
            <Ask question={asking.ask.question} text={asking.ask.text} title={asking.title} />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
})

/**
 * One operation as a chip.
 * @param props the `step`, the time `now`, whether its answer ended (`stale`), whether it is `open`, and `onToggle`
 * @returns the chip
 */
function Chip({
  step,
  now,
  stale,
  open,
  onToggle,
}: {
  step: Step
  now: number
  stale: boolean
  open: boolean
  onToggle: () => void
}) {
  const running = step.status === "running" && !stale
  const asking = Boolean(step.ask)
  const failed = step.status === "failed"
  const Icon = stepIcon(step)
  const duration = running
    ? `${Math.max(0, Math.floor((now - step.at) / 1000))}s`
    : step.ms !== undefined && step.kind === "tool"
      ? stepDuration(step.ms).replace(" ", "")
      : ""
  const target = stepTarget(step.args)
  return (
    <button
      type="button"
      aria-expanded={open}
      onClick={onToggle}
      title={target ? `${step.title}: ${target}` : step.title}
      data-status={asking ? "ask" : running ? "running" : step.status}
      className={cn(
        "group/chip flex h-7 max-w-64 items-center gap-1.5 rounded-chip pr-2 pl-1.5 text-xs outline-none transition-[background-color,box-shadow,color] duration-(--dur-quick) focus-visible:outline-2 focus-visible:outline-rigel-400",
        "bg-steel-50/5 text-steel-200 shadow-[inset_0_0_0_1px_var(--hairline)] hover:bg-steel-50/9 hover:text-steel-50 aria-expanded:bg-steel-50/10 aria-expanded:text-steel-50",
        running &&
          "bg-nebula-400/8 text-steel-50 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-nebula-400)_35%,transparent),0_0_18px_-6px_var(--color-nebula-400)]",
        failed && "shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_40%,transparent)]",
        asking &&
          "animate-breathe bg-alert-400/12 text-alert-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_60%,transparent),0_0_22px_-4px_var(--color-alert-400)]",
      )}
    >
      <span className="grid size-4 shrink-0 place-items-center">
        {running && !asking ? (
          <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin text-nebula-300" />
        ) : (
          <Icon aria-hidden="true" className={cn("size-3.5", asking ? "text-alert-300" : "text-steel-400")} />
        )}
      </span>
      <span className="truncate">{step.title}</span>
      {duration && <span className="shrink-0 font-mono text-micro text-steel-500 tabular-nums">{duration}</span>}
      {!running &&
        !asking &&
        (failed ? (
          <X aria-label="failed" className="size-3 shrink-0 text-alert-400" />
        ) : step.status === "ok" ? (
          <Check aria-label="done" className="size-3 shrink-0 text-ok-400" />
        ) : null)}
    </button>
  )
}

/**
 * What an operation was given and what it answered.
 * @param props the `step`, and whether it is still `running`
 * @returns the details
 */
function Details({ step, running }: { step: Step; running: boolean }) {
  const args = Object.entries(step.args).filter(([, v]) => v !== "" && v !== null && v !== undefined)
  const result = step.result ?? ""
  return (
    <div className="grid gap-2.5 rounded-control bg-void-950/45 p-3 text-xs shadow-[inset_0_0_0_1px_var(--hairline)]">
      {args.length > 0 && (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
          {args.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="font-mono text-steel-500">{key}</dt>
              <dd className="break-words font-mono text-steel-200">
                {typeof value === "object" ? JSON.stringify(value) : String(value)}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {running ? (
        <p className="flex items-center gap-2 text-steel-400">
          <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin text-nebula-300" />
          Working on it…
        </p>
      ) : (
        result && (
          <div className="relative">
            <pre
              className={cn(
                "line-clamp-[10] whitespace-pre-wrap break-words pr-8 font-mono leading-relaxed",
                step.status === "failed" ? "text-alert-200" : "text-steel-300",
              )}
            >
              {result}
            </pre>
            <CopyButton text={result} label="Copy what it answered" className="absolute -top-1 -right-1" />
          </div>
        )
      )}
      {step.images && step.images.length > 0 && <Pictures images={step.images} label={`${step.title} returned`} />}
    </div>
  )
}

/**
 * A step that waits for your yes: his question, inline, with Yes and No (the confirmation over the stage asks the same
 * question; whichever answers first settles it everywhere).
 * @param props the `question`'s id, its `text` and the operation's `title`
 * @returns the question
 */
function Ask({ question, text, title }: { question: string; text: string; title: string }) {
  const [sent, setSent] = useState<boolean | null>(null)
  const answer = (yes: boolean) => {
    if (answerConfirm(question, yes)) setSent(yes)
  }
  return (
    <fieldset
      aria-label={`${title} waits for your answer`}
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-control bg-alert-400/8 p-3 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_42%,transparent),0_0_32px_-12px_var(--color-alert-400)]"
    >
      <ShieldAlert aria-hidden="true" className="size-4 shrink-0 text-alert-300" />
      <p className="min-w-0 flex-1 text-sm text-alert-50">{text}</p>
      <div className="flex shrink-0 gap-1.5">
        <Button size="xs" tone="danger" disabled={sent !== null} onClick={() => answer(true)}>
          Yes
        </Button>
        <Button size="xs" disabled={sent !== null} onClick={() => answer(false)}>
          No
        </Button>
      </div>
    </fieldset>
  )
}
