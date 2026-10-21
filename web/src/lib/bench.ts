// What a benchmark needs said around it, kept pure for the tests: whether a plan can run and whether it costs (and so
// needs your yes), how its brains are described before it starts, how a run ended, and how a score is shown (a
// dash where nothing was measured: the server sends 0 for it).
import { fineMoney } from "./format"
import { msText } from "./stages"
import type { BenchBrainPlan, BenchPlanView, BenchResult, BenchRun, Paid } from "./types"

/** The words a run you stopped is recorded with (orion/bench.py). */

/**
 * Whether at least one brain of the plan can run.
 * @param plan the plan
 * @returns true when one can
 */
export const canRun = (plan: Pick<BenchPlanView, "brains">) => plan.brains.some((b) => b.ready)

/**
 * Whether the run costs something and so needs your yes (the server asks for the same: a brain that is paid and
 * ready, your Claude plan's allowance or OpenRouter's credit).
 * @param plan the plan
 * @returns true when it does
 */
export const needsYes = (plan: Pick<BenchPlanView, "brains">) => plan.brains.some((b) => b.paid !== "" && b.ready)

const plural = (count: number, one: string) => `${count} ${one}${count === 1 ? "" : "s"}`

/**
 * How many requests a brain makes over the whole suite.
 * @param requests the typical count (a good brain) and the most
 * @returns its words
 */
export const requestsWords = (requests: { typical: number; most: number }) =>
  `About ${plural(requests.typical, "request")}, at most ${requests.most}.`

/**
 * What the run does, in a sentence.
 * @param plan its suite and how many requests each brain is asked
 * @returns the sentence
 */
export const headline = (plan: Pick<BenchPlanView, "suite" | "cases">) =>
  `${plural(plan.cases, "request")} ${plan.suite === "history" ? "of your own" : "of his own suite"}, asked of each brain in a dry run: no tool really runs.`

/** How a brain's plan is tinted: it costs nothing, your plan, money, or it won't run. */
export type PlanTone = "free" | "plan" | "money" | "stopped"

/** One brain of a plan, for the question before a run. */
interface PlanLine {
  brain: string
  label: string
  where: string
  tag: string
  tone: PlanTone
  notes: string[]
}

const TAGS: Record<Paid, string> = { "": "Free", plan: "Your Claude plan", money: "Paid" }

/**
 * Each brain of the plan: its name and where it runs, what it costs, and what you should know before saying yes.
 * @param plan the plan
 * @returns the lines, in the order asked
 */
export function planLines(plan: Pick<BenchPlanView, "brains" | "suite">): PlanLine[] {
  return plan.brains.map((b: BenchBrainPlan) => {
    const notes: string[] = []
    if (!b.ready) notes.push(b.why ? `${b.why[0].toUpperCase()}${b.why.slice(1)}.` : "It can't run.")
    else {
      if (b.paid !== "") notes.push(requestsWords(b.requests))
      if (plan.suite === "history" && b.cloud) notes.push(`Your own requests are sent to ${b.where}.`)
    }
    return {
      brain: b.brain,
      label: b.label,
      where: b.where,
      tag: b.ready ? TAGS[b.paid] : "Won't run",
      tone: b.ready ? (b.paid === "" ? "free" : b.paid) : "stopped",
      notes,
    }
  })
}

/**
 * The paid requests of the whole run, when more than one brain makes them (one brain's own line says it already).
 * @param plan the plan
 * @returns the sentence, or "" when there is nothing to add
 */
export function paidTotal(plan: Pick<BenchPlanView, "brains" | "paid_requests">): string {
  const paid = plan.brains.filter((b) => b.paid !== "" && b.ready).length
  return paid > 1 ? `In all: ${requestsWords(plan.paid_requests).toLowerCase()}` : ""
}

/**
 * The requests left out of the suite, in a sentence.
 * @param plan the plan
 * @returns the sentence, or "" when none was
 */
export const leftOut = (plan: Pick<BenchPlanView, "skipped">) =>
  plan.skipped.length ? `${plural(plan.skipped.length, "request")} left out: their tools aren't here.` : ""

/** How a run is tagged. */
interface RunState {
  label: string
  tone: "nebula" | "ok" | "dusk" | "alert"
}

/**
 * A run's state: how far it is, done, stopped by you, or failed.
 * @param run the run
 * @returns the tag
 */
export function runState(run: Pick<BenchRun, "status" | "progress" | "error">): RunState {
  if (run.status === "running") return { label: `${Math.round(run.progress * 100)}%`, tone: "nebula" }
  if (run.status === "done") return { label: "Done", tone: "ok" }
  return run.status === "stopped" ? { label: "Stopped", tone: "dusk" } : { label: "Failed", tone: "alert" }
}

/**
 * What a run that didn't finish says: that you stopped it (and the scores are what it got), or what went wrong.
 * @param run the run
 * @returns the sentence, or "" for a run that is going or done
 */
export function endNote(run: Pick<BenchRun, "status" | "error">): string {
  if (run.status === "stopped") return "Stopped before the end. The scores are what it got so far."
  return run.status === "failed" ? run.error : ""
}

/**
 * A share (0 to 1) of the requests asked, as a percentage; a dash while none was asked.
 * @param result the brain's result
 * @param share the share
 * @returns its words
 */
export const shareText = (result: Pick<BenchResult, "cases">, share: number) =>
  result.cases ? `${Math.round(share * 100)}%` : "—"

/**
 * A time, or a dash when nothing came back to time.
 * @param result the brain's result
 * @param ms the time
 * @returns its words
 */
export const timeText = (result: Pick<BenchResult, "answered">, ms: number) => (result.answered ? msText(ms) : "—")

/**
 * What a brain cost: dollars for a paid model (to a hundredth of a cent), the allowance of your plan, or nothing.
 * @param result the brain's result
 * @returns its words
 */
export function costText(result: Pick<BenchResult, "paid" | "cost">): string {
  if (result.paid === "money") return fineMoney(result.cost)
  return result.paid === "plan" ? "Your plan" : "Free"
}

/**
 * What went wrong with a brain: why it stopped (and how many requests it never got), or how many failed to answer.
 * @param result the brain's result
 * @returns the sentence, or "" when all went well
 */
export function brainNote(result: Pick<BenchResult, "error" | "skipped" | "errors">): string {
  if (result.error) {
    const why = /[.!?]$/.test(result.error) ? result.error : `${result.error}.`
    return result.skipped ? `${why} ${plural(result.skipped, "request")} not asked.` : why
  }
  return result.errors ? `${plural(result.errors, "request")} got no answer.` : ""
}
