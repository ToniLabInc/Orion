/**
 * Jobs: what he runs on a schedule. Each with when it runs (in English), when next, a switch, Run now (a spinner while
 * it runs, from the job_run events), and what it answered last time (or why it failed). Editing a job is on the
 * Automations page.
 */
import { CalendarClock, Play } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect } from "react"
import { Link } from "wouter"
import { useJobs, useRunJob, useSaveJob } from "@/lib/api"
import { untilInWords } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { RISE } from "@/lib/motion"
import { useLive } from "@/lib/store"
import type { Job } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { IconButton } from "@/ui/button"
import { Empty, Failed, Skeleton } from "@/ui/states"
import { Switch } from "@/ui/switch"
import type { CardProps } from "./card"

/**
 * The jobs card.
 * @param props the card's
 * @returns the body
 */
export default function Jobs({ setNote }: CardProps) {
  const { jobs, error, pending, retry } = useJobs()
  const running = useLive((s) => s.running)
  const now = useNow(30_000)
  const on = jobs?.filter((j) => j.enabled).length ?? 0
  useEffect(() => setNote(jobs?.length ? `${on} of ${jobs.length} on` : null), [jobs?.length, on, setNote])
  if (error) return <Failed title="His jobs can't be read" error={error} onRetry={retry} className="px-4 pb-4" />
  if (pending || !jobs)
    return (
      <div className="px-4 pb-4">
        <Skeleton lines={3} />
      </div>
    )
  return (
    <div className="grid gap-2 px-3 pt-1 pb-3" data-testid="widget-jobs">
      {jobs.length ? (
        <ul className="grid gap-1">
          <AnimatePresence initial={false}>
            {jobs.map((job) => (
              <motion.li key={job.id} {...RISE}>
                <JobRow job={job} running={running.includes(job.id)} now={now} />
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
      ) : (
        <Empty icon={<CalendarClock />}>
          No jobs yet. Ask him: “every weekday at 7:30, brief me on the news and the weather”.
        </Empty>
      )}
      <Link
        href="/automations"
        className="justify-self-end rounded-chip px-2 py-1 text-xs text-steel-400 outline-none transition-colors hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400"
      >
        Edit in Automations →
      </Link>
    </div>
  )
}

/**
 * One job.
 * @param props the `job`, whether it is `running`, and the time `now`
 * @returns the row
 */
function JobRow({ job, running, now }: { job: Job; running: boolean; now: Date }) {
  const save = useSaveJob()
  const run = useRunJob()
  return (
    <div className="group/job grid gap-1 rounded-control px-2 py-2 transition-colors hover:bg-steel-50/4">
      <div className="flex items-center gap-2">
        <span className={cn("min-w-0 flex-1 truncate text-sm", job.enabled ? "text-steel-100" : "text-steel-500")}>
          {job.name}
        </span>
        {running ? (
          <Badge tone="nebula" dot pulse>
            Running
          </Badge>
        ) : (
          <IconButton
            label={`Run ${job.name} now`}
            size="xs"
            disabled={run.isPending}
            onClick={() => run.mutate(job.id)}
            className="opacity-0 transition-opacity group-hover/job:opacity-100 focus-visible:opacity-100 max-desk:opacity-100"
          >
            <Play />
          </IconButton>
        )}
        <Switch
          size="sm"
          checked={job.enabled}
          aria-label={`${job.name}: on or off`}
          onCheckedChange={(enabled) => save.mutate({ id: job.id, job: { enabled } })}
        />
      </div>
      <p className="truncate text-xs text-steel-400">
        {job.explain}
        {job.enabled && job.next_run && (
          <span className="text-steel-500"> · next {untilInWords(job.next_run, now)}</span>
        )}
      </p>
      {job.last_status && (
        <p className={cn("line-clamp-2 text-xs", job.last_status === "error" ? "text-alert-300" : "text-steel-500")}>
          <span className={job.last_status === "error" ? "text-alert-400" : "text-ok-400"}>
            {job.last_status === "error" ? "Failed" : "Last"}:
          </span>{" "}
          {job.last_text || "(nothing said)"}
        </p>
      )}
    </div>
  )
}
