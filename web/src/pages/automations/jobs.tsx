/**
 * Jobs: the list of his scheduled jobs and the editor of one. Each in the list says when it runs (in English), when
 * next, and what it answered last; a switch turns it off. The editor: a name, a schedule (a cron line, explained in
 * English as you type it by Orion himself, with when it would run next, or what is wrong with it), what to ask him,
 * where the answer goes (spoken, shown here, sent to Discord), and Run now, Delete, Save. The list follows the `jobs`
 * events: a change made by voice ("every weekday at 7:30, brief me") shows here at once.
 */
import { CalendarClock, LoaderCircle, Play, Plus, Save, Trash2 } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { type FormEvent, type ReactNode, useEffect, useId, useState } from "react"
import { useSearch } from "wouter"
import {
  ApiError,
  explainSchedule,
  missing,
  useDeleteJob,
  useJobs,
  useRunJob,
  useSaveJob,
  useSettings,
} from "@/lib/api"
import { clockTime, shortDate, untilInWords } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { RISE, SPRING } from "@/lib/motion"
import { useLive } from "@/lib/store"
import type { Delivery, Job, JobInput } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { Input, Textarea } from "@/ui/input"
import { Empty, Failed, Pending } from "@/ui/states"
import { Switch } from "@/ui/switch"
import { ARRIVE } from "../frame"
import { Fields, FoldPanel, useShows } from "../settings/fields"

/** A few schedules to start from (a cron line each). */
const PRESETS: { label: string; schedule: string }[] = [
  { label: "Every morning at 7:30", schedule: "30 7 * * *" },
  { label: "Weekdays at 8:00", schedule: "0 8 * * 1-5" },
  { label: "Every hour", schedule: "0 * * * *" },
  { label: "Sundays at 18:00", schedule: "0 18 * * 0" },
]

/** How jobs run (what thinks for them, where they deliver): what of it the schema shows. */
const JOB_SETTINGS = ["jobs.provider", "jobs.deliver", "jobs.max_tool_rounds", "jobs.run_timeout_s", "jobs.catch_up_s"]

const DELIVERIES: { value: Delivery; label: string }[] = [
  { value: "speak", label: "Spoken" },
  { value: "hud", label: "Shown here" },
  { value: "discord", label: "On Discord" },
]

/**
 * The jobs tab.
 * @returns the tab
 */
export function Jobs() {
  const { jobs, error, pending, retry } = useJobs()
  const settingsShown = useShows(JOB_SETTINGS)
  const query = useSearch()
  const [chosen, setChosen] = useState<number | "new" | null>(null)
  useEffect(() => {
    const id = Number(new URLSearchParams(query).get("job"))
    if (id) setChosen(id)
  }, [query])
  if (error)
    return (
      <motion.div variants={ARRIVE}>
        <Failed title="His jobs can't be read" error={error} onRetry={retry} />
      </motion.div>
    )
  if (pending || !jobs) return <Pending label="Reading his jobs" delay={150} page />
  const job = typeof chosen === "number" ? jobs.find((j) => j.id === chosen) : undefined
  const editing = chosen === "new" || job !== undefined
  return (
    <>
      <motion.div variants={ARRIVE} className="grid items-start gap-4 desk:grid-cols-[22rem_minmax(0,1fr)]">
        <div className="grid gap-2">
          <Button tone="primary" className="justify-self-start" onClick={() => setChosen("new")} data-testid="job-new">
            <Plus aria-hidden="true" />
            New job
          </Button>
          {jobs.length ? (
            <ul className="grid gap-2">
              <AnimatePresence initial={false}>
                {jobs.map((j) => (
                  <motion.li key={j.id} {...RISE} layout="position">
                    <JobItem job={j} chosen={chosen === j.id} onChoose={() => setChosen(j.id)} />
                  </motion.li>
                ))}
              </AnimatePresence>
            </ul>
          ) : (
            <div className="glass">
              <Empty icon={<CalendarClock />}>
                No jobs yet. Make one here, or ask him: “every weekday at 7:30, brief me on the news and the weather”.
              </Empty>
            </div>
          )}
        </div>
        <AnimatePresence mode="wait" initial={false}>
          {editing ? (
            <Editor key={job?.id ?? "new"} job={job} onDone={(id) => setChosen(id ?? null)} />
          ) : (
            <motion.div key="none" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="max-desk:hidden">
              <div className="glass grid min-h-64 place-items-center p-6 text-center">
                <p className="max-w-xs text-sm text-steel-400">Choose a job to change it, or make a new one.</p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
      {settingsShown && (
        <FoldPanel title="How jobs run" icon={CalendarClock}>
          <Fields paths={JOB_SETTINGS} />
        </FoldPanel>
      )}
    </>
  )
}

/**
 * One job in the list.
 * @param props the `job`, whether it is `chosen`, and `onChoose`
 * @returns the item
 */
function JobItem({ job, chosen, onChoose }: { job: Job; chosen: boolean; onChoose: () => void }) {
  const save = useSaveJob()
  const running = useLive((s) => s.running.includes(job.id))
  const now = useNow(30_000)
  return (
    <div
      className={cn(
        "glass grid gap-1 p-3.5 transition-shadow duration-(--dur-quick)",
        chosen && "shadow-[var(--shadow-elevated),inset_0_0_0_1.5px_var(--color-rigel-400)]",
      )}
      data-testid={`job-${job.id}`}
    >
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onChoose}
          aria-current={chosen || undefined}
          className="min-w-0 flex-1 truncate rounded-chip text-left font-medium text-steel-50 outline-none after:absolute after:inset-0 focus-visible:outline-2 focus-visible:outline-rigel-400"
        >
          {job.name}
        </button>
        {running && (
          <Badge tone="nebula" dot pulse>
            Running
          </Badge>
        )}
        <Switch
          size="sm"
          checked={job.enabled}
          aria-label={`${job.name}: on or off`}
          onCheckedChange={(enabled) => save.mutate({ id: job.id, job: { enabled } })}
          className="relative z-1"
        />
      </div>
      <p className="truncate text-xs text-steel-400">
        {job.explain}
        {job.enabled && job.next_run ? (
          <span className="text-steel-500"> · next {untilInWords(job.next_run, now)}</span>
        ) : null}
      </p>
      {job.last_status && (
        <p className={cn("line-clamp-1 text-xs", job.last_status === "error" ? "text-alert-300" : "text-steel-500")}>
          {job.last_status === "error" ? "Failed: " : "Last: "}
          {job.last_text || "(nothing said)"}
        </p>
      )}
    </div>
  )
}

/**
 * The editor of one job (or a new one).
 * @param props the `job` (none: a new one) and `onDone` (with the job's id once saved, or nothing)
 * @returns the editor
 */
function Editor({ job, onDone }: { job?: Job; onDone: (id?: number) => void }) {
  const defaults = useSettings().data?.config.jobs as { deliver?: Delivery[] } | undefined
  const [draft, setDraft] = useState<JobInput>(() => ({
    name: job?.name ?? "",
    schedule: job?.schedule ?? "",
    prompt: job?.prompt ?? "",
    deliver: job?.deliver ?? defaults?.deliver ?? [],
    enabled: job?.enabled ?? true,
  }))
  const save = useSaveJob()
  const remove = useDeleteJob()
  const run = useRunJob()
  const running = useLive((s) => (job ? s.running.includes(job.id) : false))
  const explained = useExplain(draft.schedule)
  const scheduleId = useId()
  const set = <K extends keyof JobInput>(key: K, value: JobInput[K]) => setDraft((d) => ({ ...d, [key]: value }))
  const valid = draft.name.trim() && draft.prompt.trim() && explained.ok !== false
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!valid) return
    save.mutate(
      {
        id: job?.id,
        job: { ...draft, name: draft.name.trim(), schedule: draft.schedule.trim(), prompt: draft.prompt.trim() },
      },
      { onSuccess: (saved) => onDone(saved?.id ?? job?.id) },
    )
  }
  const askDelete = async () => {
    if (!job) return
    if (
      await ask({
        title: `Delete ${job.name}?`,
        text: "It won't run again.",
        ok: "Delete",
        danger: true,
        kicker: "Jobs",
      })
    )
      remove.mutate(job.id, { onSuccess: () => onDone() })
  }
  return (
    <motion.form
      onSubmit={submit}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6, transition: { duration: 0.14 } }}
      transition={SPRING.gentle}
      data-testid="job-editor"
    >
      <div className="glass grid gap-5 p-5">
        <div className="flex items-center gap-3">
          <h2 className="min-w-0 flex-1 truncate font-serif text-2xl text-steel-50">{job ? job.name : "A new job"}</h2>
          {/* biome-ignore lint/a11y/noLabelWithoutControl: the switch inside is the control */}
          <label className="flex items-center gap-2 text-sm text-steel-300">
            On
            <Switch checked={draft.enabled} onCheckedChange={(on) => set("enabled", on)} />
          </label>
        </div>
        <Labelled label="Name">
          <Input
            value={draft.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder="Morning briefing"
            maxLength={80}
          />
        </Labelled>
        <div className="grid gap-2 text-sm text-steel-200">
          <label htmlFor={scheduleId}>When</label>
          <Input
            id={scheduleId}
            value={draft.schedule}
            onChange={(e) => set("schedule", e.target.value)}
            placeholder="minute hour day month weekday, e.g. 30 7 * * 1-5"
            className="font-mono"
            aria-invalid={explained.ok === false || undefined}
            spellCheck={false}
            data-testid="job-schedule"
          />
          <div className="flex flex-wrap gap-1.5">
            {PRESETS.map((p) => (
              <Button
                key={p.schedule}
                type="button"
                size="xs"
                className="rounded-full"
                onClick={() => set("schedule", p.schedule)}
              >
                {p.label}
              </Button>
            ))}
          </div>
          <Explanation state={explained} />
        </div>
        <Labelled label="What to ask him">
          <Textarea
            value={draft.prompt}
            onChange={(e) => set("prompt", e.target.value)}
            placeholder="Brief me: the weather, my calendar today and the top news, in a few sentences."
            className="min-h-28"
            maxLength={4000}
          />
        </Labelled>
        <fieldset className="grid gap-2">
          <legend className="mb-2 text-sm text-steel-200">Where the answer goes</legend>
          <div className="flex flex-wrap gap-2">
            {DELIVERIES.map((d) => {
              const on = draft.deliver.includes(d.value)
              return (
                <button
                  key={d.value}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    set("deliver", on ? draft.deliver.filter((x) => x !== d.value) : [...draft.deliver, d.value])
                  }
                  className={cn(
                    "h-8 rounded-full px-3.5 text-sm outline-none transition-[background-color,box-shadow,color] duration-(--dur-quick) focus-visible:outline-2 focus-visible:outline-rigel-400",
                    on
                      ? "bg-rigel-400/16 text-rigel-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_45%,transparent)]"
                      : "bg-steel-50/5 text-steel-400 shadow-[inset_0_0_0_1px_var(--hairline)] hover:text-steel-100",
                  )}
                >
                  {d.label}
                </button>
              )
            })}
          </div>
        </fieldset>
        {job?.last_status && (
          <div className="grid gap-1 rounded-control bg-void-950/50 p-3 text-sm shadow-[inset_0_0_0_1px_var(--hairline)]">
            <p className="flex items-center gap-2 text-xs text-steel-500">
              <Badge tone={job.last_status === "error" ? "alert" : "ok"} dot>
                {job.last_status === "error" ? "Failed" : "Done"}
              </Badge>
              {job.last_run &&
                `last ran ${shortDate(new Date(job.last_run * 1000))}, ${clockTime(new Date(job.last_run * 1000), false)}`}
            </p>
            <p className={cn("whitespace-pre-wrap", job.last_status === "error" ? "text-alert-200" : "text-steel-200")}>
              {job.last_text || "(nothing said)"}
            </p>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2 border-t pt-4">
          {job && (
            <>
              <Button type="button" tone="danger" onClick={() => void askDelete()} disabled={remove.isPending}>
                <Trash2 aria-hidden="true" />
                Delete
              </Button>
              <Button type="button" onClick={() => run.mutate(job.id)} disabled={run.isPending || running}>
                {running ? <LoaderCircle aria-hidden="true" className="animate-spin" /> : <Play aria-hidden="true" />}
                {running ? "Running" : "Run now"}
              </Button>
            </>
          )}
          <span className="ml-auto" />
          <Button type="button" tone="ghost" onClick={() => onDone(job?.id)}>
            {job ? "Close" : "Cancel"}
          </Button>
          <Button type="submit" tone="primary" disabled={!valid || save.isPending} data-testid="job-save">
            {save.isPending ? (
              <LoaderCircle aria-hidden="true" className="animate-spin" />
            ) : (
              <Save aria-hidden="true" />
            )}
            {job ? "Save" : "Make it"}
          </Button>
        </div>
      </div>
    </motion.form>
  )
}

interface Explained {
  ok: boolean | null // null: nothing to say yet (empty, or asking)
  text: string
  next: number | null
  asking: boolean
}

/**
 * A schedule in English as you type it: Orion explains it (with when it would run next), or says what is wrong.
 * @param schedule the cron line
 * @returns what he said
 */
function useExplain(schedule: string): Explained {
  const [state, setState] = useState<Explained>({ ok: null, text: "", next: null, asking: false })
  useEffect(() => {
    const text = schedule.trim()
    if (!text) {
      setState({ ok: null, text: "", next: null, asking: false })
      return
    }
    setState((s) => ({ ...s, asking: true }))
    let live = true
    const timer = setTimeout(() => {
      explainSchedule(text)
        .then((r) => live && setState({ ok: true, text: r.explain, next: r.next_run, asking: false }))
        .catch((error: unknown) => {
          if (!live) return
          if (missing(error)) setState({ ok: null, text: "", next: null, asking: false })
          else
            setState({
              ok: false,
              text: error instanceof ApiError ? error.message : "That isn't a schedule.",
              next: null,
              asking: false,
            })
        })
    }, 350)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [schedule])
  return state
}

/**
 * What Orion says of the schedule being typed.
 * @param props the explanation's `state`
 * @returns the line
 */
function Explanation({ state }: { state: Explained }) {
  const now = useNow(60_000)
  return (
    <p
      aria-live="polite"
      className={cn(
        "flex min-h-5 items-center gap-2 text-sm",
        state.ok === false ? "text-alert-300" : "text-aurora-200",
        state.asking && "opacity-60",
      )}
      data-testid="job-explain"
    >
      {state.asking && <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin text-steel-500" />}
      {state.text}
      {state.ok && state.next ? <span className="text-steel-500">· next {untilInWords(state.next, now)}</span> : null}
    </p>
  )
}

/**
 * A form field with its label above it.
 * @param props the `label` and the control(s) (`children`)
 * @returns the field
 */
function Labelled({ label, children }: { label: string; children: ReactNode }) {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: the control is inside
    <label className="grid gap-2 text-sm text-steel-200">
      {label}
      {children}
    </label>
  )
}
