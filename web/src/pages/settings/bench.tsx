/**
 * The benchmark: brains scored on the same requests in a dry run (his real tools are offered to them, none really
 * runs): did each pick the right tool, with the right arguments, did it call one it never should, how soon his first
 * word came and how long the whole took, and what it cost. On Orion's own suite of requests, or on your own past ones.
 * Run asks the server for the plan first (nothing starts) and, for a run that costs (your Claude plan's allowance,
 * OpenRouter's credit), shows it and waits for your yes. A run shows as it goes (`bench` events) and can be stopped
 * (what it scored so far is kept); the latest is open, the ones before it fold.
 */
import { Collapsible } from "@base-ui/react/collapsible"
import { Progress } from "@base-ui/react/progress"
import { ChevronDown, FlaskConical, LoaderCircle, Play, Square, TriangleAlert } from "lucide-react"
import { type CSSProperties, type ReactNode, useState } from "react"
import { missing, useBenchRuns, usePlanBench, useRunBench, useStopBench } from "@/lib/api"
import {
  brainNote,
  canRun,
  costText,
  endNote,
  headline,
  leftOut,
  needsYes,
  type PlanTone,
  paidTotal,
  planLines,
  runState,
  shareText,
  timeText,
} from "@/lib/bench"
import { agoInWords } from "@/lib/format"
import { msText } from "@/lib/stages"
import type { BenchPlan, BenchResult, BenchRun, SettingsResponse } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Badge, type BadgeTone } from "@/ui/badge"
import { Button } from "@/ui/button"
import { Failed, Pending } from "@/ui/states"
import { Segmented } from "@/ui/tabs"
import { toast } from "@/ui/toast"
import { Note, Panel, Row } from "./fields"

/** The brains a run can score, by the name his settings give them (a provider: its model is the one he uses). */
const BRAINS: { id: string; label: string }[] = [
  { id: "claude", label: "Your Claude plan" },
  { id: "ollama", label: "This machine" },
  { id: "openrouter", label: "OpenRouter" },
]

const SUITES = [
  { value: "builtin" as const, label: "His own suite" },
  { value: "history" as const, label: "Your requests" },
]

/** How many of your past requests a run takes. */
const HISTORY_LIMITS = [
  { value: 10, label: "10" },
  { value: 25, label: "25" },
  { value: 50, label: "50" },
]

/** What each way of paying is tinted: free is fine, your plan is yours, money warms, a brain that can't run alerts. */
const PLAN_TONES: Record<PlanTone, BadgeTone> = { free: "ok", plan: "betel", money: "dusk", stopped: "alert" }

/**
 * The panel.
 * @param props the `settings` (the brain he thinks with, and his stand-in, chosen to start with)
 * @returns the panel (nothing on an Orion that can't run one)
 */
export function Bench({ settings }: { settings: SettingsResponse }) {
  const runs = useBenchRuns()
  const [opened, setOpened] = useState<Record<number, boolean>>({}) // what you opened or folded; else only the latest is open
  if (missing(runs.error)) return null
  return (
    <Panel
      title="Benchmark"
      icon={FlaskConical}
      description="Brains on the same requests, in a dry run: nothing really runs"
      id="bench"
      data-testid="brain-bench"
    >
      <Start settings={settings} running={runs.data?.some((r) => r.status === "running") ?? false} />
      {runs.isError ? (
        <div className="border-t px-5 py-4">
          <Failed title="The runs can't be read" error={runs.error} onRetry={() => void runs.refetch()} />
        </div>
      ) : runs.isPending ? (
        <Pending label="Reading the runs" delay={150} className="border-t px-5 py-4" />
      ) : runs.data.length ? (
        <div className="grid grid-cols-1 border-t">
          {runs.data.map((run, i) => (
            <Run
              key={run.id}
              run={run}
              open={opened[run.id] ?? i === 0}
              onOpenChange={(open) => setOpened((before) => ({ ...before, [run.id]: open }))}
            />
          ))}
        </div>
      ) : (
        <Note>No run yet.</Note>
      )}
    </Panel>
  )
}

/**
 * Starting a run: which brains, which suite (and how many of your requests), and Run, which plans first and asks
 * before a run that costs.
 * @param props the `settings`, and whether a run is `running` already
 * @returns the rows
 */
function Start({ settings, running }: { settings: SettingsResponse; running: boolean }) {
  const brain = settings.config.brain as { provider: string; fallback?: string }
  const [brains, setBrains] = useState<string[]>(() =>
    BRAINS.map((b) => b.id).filter((id) => id === brain.provider || id === brain.fallback),
  )
  const [suite, setSuite] = useState<"builtin" | "history">("builtin")
  const [limit, setLimit] = useState(25)
  const plan = usePlanBench()
  const run = useRunBench()
  const toggle = (id: string) =>
    setBrains((before) => (before.includes(id) ? before.filter((b) => b !== id) : [...before, id]))
  const start = async () => {
    const request = { brains, suite, ...(suite === "history" && { limit }) }
    const planned = await plan.mutateAsync(request).catch(() => null) // (a toast says why not)
    if (!planned) return
    if (!canRun(planned)) {
      const why = planned.brains.map((b) => `${b.label}: ${b.why}`).join(" ")
      toast.add({ title: "No brain can run", description: why, type: "warning", timeout: 12000 })
      return
    }
    const costs = needsYes(planned)
    if (
      costs &&
      !(await ask({
        title: "Run the benchmark?",
        kicker: "Benchmark",
        text: headline(planned),
        body: <PlanSummary plan={planned} />,
        ok: "Run it",
      }))
    )
      return
    run.mutate({ ...request, confirm: costs })
  }
  return (
    <>
      <Row
        label="Brains"
        hint="Each answers every request of the suite. Claude's use your plan's allowance, OpenRouter's your credit."
      >
        <fieldset className="m-0 flex flex-wrap gap-1.5 border-0 p-0" aria-label="Brains to score">
          {BRAINS.map((b) => {
            const on = brains.includes(b.id)
            return (
              <button
                key={b.id}
                type="button"
                aria-pressed={on}
                onClick={() => toggle(b.id)}
                className={cn(
                  "h-8 rounded-full px-3 text-sm outline-none transition-colors focus-visible:outline-2 focus-visible:outline-rigel-400",
                  on
                    ? "bg-rigel-400/16 text-rigel-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_45%,transparent)]"
                    : "bg-steel-50/5 text-steel-300 shadow-[inset_0_0_0_1px_var(--hairline)] hover:text-steel-100",
                )}
                data-testid={`bench-brain-${b.id}`}
              >
                {b.label}
              </button>
            )
          })}
        </fieldset>
      </Row>
      <Row
        label="Requests"
        hint={
          suite === "builtin"
            ? "About forty requests over his real tools: timers, the weather, the web, your calendar…"
            : "Your own latest requests, and the tools he used for them then."
        }
      >
        <div className="flex flex-wrap items-center gap-2">
          <Segmented value={suite} onChange={setSuite} choices={SUITES} label="Which requests" id="bench-suite" />
          {suite === "history" && (
            <Segmented value={limit} onChange={setLimit} choices={HISTORY_LIMITS} label="How many" id="bench-limit" />
          )}
        </div>
      </Row>
      <div className="flex flex-wrap items-center justify-end gap-3 border-t px-5 py-3.5">
        {running && <span className="mr-auto text-sm text-steel-400">A run is under way.</span>}
        <Button
          tone="primary"
          disabled={!brains.length || running || plan.isPending || run.isPending}
          onClick={() => void start()}
          data-testid="bench-run"
        >
          {plan.isPending || run.isPending ? (
            <LoaderCircle aria-hidden="true" className="animate-spin" />
          ) : (
            <Play aria-hidden="true" />
          )}
          Run the benchmark
        </Button>
      </div>
    </>
  )
}

/**
 * What a run that costs would do, for the question before it: each brain, where it runs, what it costs and how many
 * requests it makes, and what else you should know.
 * @param props the `plan`
 * @returns the list
 */
function PlanSummary({ plan }: { plan: BenchPlan }) {
  const rest = [paidTotal(plan), leftOut(plan)].filter(Boolean).join(" ")
  return (
    <div className="grid grid-cols-1 gap-2.5" data-testid="bench-plan">
      <ul className="grid grid-cols-1 gap-2">
        {planLines(plan).map((line) => (
          <li
            key={line.brain}
            className="grid grid-cols-1 gap-1 rounded-control bg-steel-50/4 px-3 py-2.5 shadow-[inset_0_0_0_1px_var(--hairline)]"
          >
            <span className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm text-steel-50">
                {line.label}
                <span className="text-steel-400"> · {line.where}</span>
              </span>
              <Badge tone={PLAN_TONES[line.tone]}>{line.tag}</Badge>
            </span>
            {line.notes.map((note) => (
              <span key={note} className="text-xs text-steel-300">
                {note}
              </span>
            ))}
          </li>
        ))}
      </ul>
      {rest && <p className="text-xs text-steel-400">{rest}</p>}
    </div>
  )
}

/**
 * One run: when, on what, its state; open, how far it is (and Stop), what happened to it, and each brain's scores.
 * @param props the `run`, whether it is `open`, and what to do when you open or fold it
 * @returns the run
 */
function Run({ run, open, onOpenChange }: { run: BenchRun; open: boolean; onOpenChange: (open: boolean) => void }) {
  const now = new Date()
  const stop = useStopBench()
  const state = runState(run)
  const notes = [endNote(run), leftOut(run.plan)].filter(Boolean)
  const names = (run.results.length ? run.results.map((r) => r.label) : run.brains).join(", ")
  return (
    <Collapsible.Root
      open={open}
      onOpenChange={onOpenChange}
      className="border-b last:border-b-0"
      data-testid={`bench-run-${run.id}`}
    >
      <Collapsible.Trigger className="group/run flex w-full items-center gap-3 px-5 py-3 text-left outline-none hover:bg-steel-50/3 focus-visible:bg-steel-50/5">
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-steel-100">
            {run.suite === "builtin" ? "His own suite" : "Your requests"} · {names}
          </span>
          <span className="block text-xs text-steel-400">{agoInWords(run.at, now)}</span>
        </span>
        <Badge tone={state.tone} dot={run.status === "running"} pulse={run.status === "running"}>
          {state.label}
        </Badge>
        <ChevronDown
          aria-hidden="true"
          className="size-4 shrink-0 text-steel-500 transition-transform duration-(--dur-base) group-data-panel-open/run:rotate-180"
        />
      </Collapsible.Trigger>
      <Collapsible.Panel className="h-(--collapsible-panel-height) overflow-hidden transition-[height] duration-(--dur-base) ease-(--ease-expo) data-ending-style:h-0 data-starting-style:h-0">
        <div className="grid grid-cols-1 gap-3 px-5 pb-4">
          {run.status === "running" && (
            <div className="flex items-center gap-3">
              <Progress.Root
                value={Math.round(run.progress * 100)}
                aria-label="How far the run is"
                className="min-w-0 flex-1"
              >
                <Progress.Track className="h-1.5 overflow-hidden rounded-full bg-nebula-500/18">
                  <Progress.Indicator className="h-full rounded-full bg-nebula-500 transition-[width] duration-(--dur-slow)" />
                </Progress.Track>
              </Progress.Root>
              <Button size="xs" disabled={stop.isPending} onClick={() => stop.mutate(run.id)} data-testid="bench-stop">
                <Square aria-hidden="true" className="fill-current" />
                Stop
              </Button>
            </div>
          )}
          {notes.map((note) => (
            <p key={note} className="text-sm text-steel-300">
              {note}
            </p>
          ))}
          {run.results.length > 0 && <Scores results={run.results} />}
        </div>
      </Collapsible.Panel>
    </Collapsible.Root>
  )
}

/** A time with the one 95% of the answers came within under it. */
function Timed({ result, p50, p95 }: { result: BenchResult; p50: number; p95: number }) {
  return (
    <>
      {timeText(result, p50)}
      {result.answered > 0 && <span className="block text-micro text-steel-400">95%: {msText(p95)}</span>}
    </>
  )
}

/** The scores, one row each, in the order they are read. */
const ROWS: { label: string; cell: (r: BenchResult) => ReactNode }[] = [
  { label: "Passed", cell: (r) => (r.cases ? `${r.passed} of ${r.cases}` : "—") },
  { label: "Right tool", cell: (r) => shareText(r, r.tool_accuracy) },
  { label: "Right arguments", cell: (r) => shareText(r, r.args_accuracy) },
  {
    label: "Forbidden calls",
    cell: (r) => (r.cases ? <span className={cn(r.forbidden > 0 && "text-alert-200")}>{r.forbidden}</span> : "—"),
  },
  { label: "Tool rounds", cell: (r) => (r.answered ? String(Math.round(r.rounds * 10) / 10) : "—") },
  { label: "First word", cell: (r) => <Timed result={r} p50={r.p50_first_token_ms} p95={r.p95_first_token_ms} /> },
  { label: "Whole answer", cell: (r) => <Timed result={r} p50={r.p50_total_ms} p95={r.p95_total_ms} /> },
  { label: "Cost", cell: (r) => costText(r) },
]

/**
 * The brains side by side, a column each (a few columns read on a phone), one score a row, and what each got wrong.
 * @param props the `results`
 * @returns the table, and under it the notes and the misses
 */
function Scores({ results }: { results: BenchResult[] }) {
  const notes = results.filter((r) => brainNote(r))
  return (
    <div className="grid grid-cols-1 gap-3">
      <div className="max-w-full overflow-x-auto rounded-control bg-void-950/45 shadow-[inset_0_0_0_1px_var(--hairline)] @2xl:w-fit">
        <table
          className="w-full min-w-[19rem] table-fixed text-left text-sm @2xl:w-(--width)"
          style={{ "--width": `calc(11rem + ${results.length} * 10rem)` } as CSSProperties}
          data-testid="bench-scores"
        >
          <colgroup>
            <col className="w-32 @2xl:w-44" />
          </colgroup>
          <thead>
            <tr className="border-b">
              <td className="px-3 py-2" />
              {results.map((r) => (
                <th key={r.brain} scope="col" className="px-3 py-2 text-right font-medium text-steel-50 leading-snug">
                  {r.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="font-mono text-steel-100 tabular-nums">
            {ROWS.map((row) => (
              <tr key={row.label} className="border-b last:border-b-0">
                <th scope="row" className="px-3 py-2 font-normal font-sans text-steel-300">
                  {row.label}
                </th>
                {results.map((r) => (
                  <td key={r.brain} className="px-3 py-2 text-right">
                    {row.cell(r)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {notes.map((r) => (
        <p key={r.brain} className="text-sm text-steel-300">
          <span className="text-steel-100">{r.label}:</span> {brainNote(r)}
        </p>
      ))}
      {results.map((r) => r.failures.length > 0 && <Misses key={r.brain} result={r} />)}
    </div>
  )
}

/**
 * What a brain got wrong: each request, what was expected and what it did.
 * @param props the brain's `result`
 * @returns the list, folded
 */
function Misses({ result }: { result: BenchResult }) {
  const words = (value: unknown) => (typeof value === "string" ? value : JSON.stringify(value))
  return (
    <Collapsible.Root className="rounded-control shadow-[inset_0_0_0_1px_var(--hairline)]">
      <Collapsible.Trigger className="group/miss flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-steel-200 outline-none hover:text-steel-50 focus-visible:outline-2 focus-visible:outline-rigel-400">
        <TriangleAlert aria-hidden="true" className="size-4 shrink-0 text-dusk-400" />
        {result.label}: {result.failures.length} missed
        <ChevronDown
          aria-hidden="true"
          className="ml-auto size-4 text-steel-500 transition-transform group-data-panel-open/miss:rotate-180"
        />
      </Collapsible.Trigger>
      <Collapsible.Panel>
        <ul className="grid gap-2 border-t px-3 py-2.5">
          {result.failures.map((f, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: a request may be asked twice; its place is its identity
            <li key={i} className="grid gap-0.5 text-xs">
              <span className="text-steel-100">“{f.say}”</span>
              <span className="font-mono text-steel-400">expected {words(f.expected)}</span>
              <span className="font-mono text-alert-200">got {words(f.got)}</span>
            </li>
          ))}
        </ul>
      </Collapsible.Panel>
    </Collapsible.Root>
  )
}
