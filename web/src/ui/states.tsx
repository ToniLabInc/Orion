/**
 * One language for waiting, nothing and failure across the HUD: a pending line that appears only if the wait lasts,
 * skeleton bars that shimmer while something loads, an empty note, a failure with its first error line and a retry,
 * a "not available yet" note for a part of Orion that isn't there (an endpoint answering 404), and the error boundary
 * that puts the failure in place of a part that broke.
 */
import { LoaderCircle, RotateCw, Sparkle, TriangleAlert } from "lucide-react"
import { Component, type ReactNode, useEffect, useState } from "react"
import { missing } from "@/lib/api"
import { cn } from "@/lib/utils"
import { Button } from "./button"

/**
 * A wait worth showing: nothing for `delay` ms (so a quick load never flashes), then a spinner and a label.
 * @param props the words (`label`), the `delay` and, for a page, `page` (centred in its box)
 * @returns the line, or nothing yet
 */
export function Pending({
  label = "Loading",
  delay = 0,
  page = false,
  className,
}: {
  label?: string
  delay?: number
  page?: boolean
  className?: string
}) {
  const [shown, setShown] = useState(delay <= 0)
  useEffect(() => {
    if (delay <= 0) return
    const timer = setTimeout(() => setShown(true), delay)
    return () => clearTimeout(timer)
  }, [delay])
  if (!shown) return null
  const line = (
    <span role="status" className={cn("inline-flex items-center gap-2 text-sm text-steel-400", className)}>
      <LoaderCircle aria-hidden="true" className="size-4 animate-spin text-rigel-300" />
      {label}
    </span>
  )
  return page ? <div className="grid size-full min-h-40 place-items-center p-6">{line}</div> : line
}

/**
 * Bars standing in for data that hasn't come yet, a soft light passing over them.
 * @param props how many `lines`, and classes
 * @returns the bars
 */
export function Skeleton({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div aria-hidden="true" className={cn("grid gap-2.5 py-1", className)}>
      {Array.from({ length: lines }, (_, i) => 92 - i * 18).map((width) => (
        <span
          key={width}
          className="relative h-3 overflow-hidden rounded-full bg-steel-50/6"
          style={{ width: `${width}%` }}
        >
          <span className="absolute inset-0 animate-shimmer bg-linear-to-r from-transparent via-steel-50/8 to-transparent" />
        </span>
      ))}
    </div>
  )
}

/**
 * Nothing to show, said calmly: an icon and a line (and an action).
 * @param props the `icon`, the words (`children`), an `action` and classes
 * @returns the note
 */
export function Empty({
  icon,
  children,
  action,
  className,
}: {
  icon?: ReactNode
  children: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn("grid justify-items-center gap-2.5 px-4 py-6 text-center", className)}>
      <span
        aria-hidden="true"
        className="grid size-9 place-items-center rounded-full bg-steel-50/5 text-steel-500 shadow-[inset_0_0_0_1px_var(--hairline)] [&_svg]:size-4"
      >
        {icon ?? <Sparkle />}
      </span>
      <p className="max-w-xs text-sm text-steel-400">{children}</p>
      {action}
    </div>
  )
}

/**
 * Something that failed, with its first error line and a way to try again; a part of Orion that isn't there yet says
 * so instead.
 * @param props `title` (what failed), `error` (why), `onRetry` and its words (`retry`)
 * @returns the failure
 */
export function Failed({
  title,
  error,
  onRetry,
  retry = "Try again",
  className,
}: {
  title: string
  error?: unknown
  onRetry?: () => void
  retry?: string
  className?: string
}) {
  if (missing(error))
    return (
      <p className={cn("flex items-start gap-2 text-sm text-steel-400", className)} data-testid="not-yet">
        <Sparkle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-steel-500" />
        Not available yet: this Orion doesn't offer it. It appears here once he does.
      </p>
    )
  const reason = (error instanceof Error ? error.message : error ? String(error) : "").split("\n")[0]
  return (
    <div role="alert" className={cn("grid max-w-md justify-items-start gap-2", className)}>
      <p className="flex items-center gap-2 font-medium text-sm text-steel-100">
        <TriangleAlert aria-hidden="true" className="size-4 shrink-0 text-alert-400" />
        {title}
      </p>
      {reason && <p className="line-clamp-2 break-words font-mono text-xs text-steel-400">{reason}</p>}
      {onRetry && (
        <Button size="xs" onClick={onRetry} className="mt-1">
          <RotateCw aria-hidden="true" />
          {retry}
        </Button>
      )}
    </div>
  )
}

type BoundaryState = { error: Error | null }

/**
 * Keeps a failure inside its part of the HUD (a page, a card): the rest goes on working. Without a `fallback` the
 * failure with a Reload button takes the part's place.
 */
export class ErrorBoundary extends Component<{ fallback?: ReactNode; children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null }

  /**
   * Turn whatever was thrown into an Error to show.
   * @param error what was thrown
   * @returns the boundary's new state
   */
  static getDerivedStateFromError(error: unknown): BoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  /**
   * The children, or the fallback once something under them failed.
   * @returns what to show
   */
  render() {
    const { error } = this.state
    if (!error) return this.props.children
    if (this.props.fallback !== undefined) return this.props.fallback
    return (
      <div className="grid size-full min-h-40 place-items-center p-6" data-testid="page-error">
        <Failed title="This part couldn't load" error={error} retry="Reload" onRetry={() => location.reload()} />
      </div>
    )
  }
}
