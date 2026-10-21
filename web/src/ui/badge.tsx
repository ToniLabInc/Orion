/**
 * Badges: a word or a count in a small pill, tinted by what it means (rigel his, betel yours, nebula thinking, aurora
 * speaking, dusk loading, alert a problem, ok done, neutral steel). An optional dot in front; `pulse` makes the dot
 * breathe while something runs.
 */
import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"

export type BadgeTone = "neutral" | "rigel" | "betel" | "nebula" | "aurora" | "dusk" | "alert" | "ok"

const TONES: Record<BadgeTone, string> = {
  neutral: "bg-steel-50/7 text-steel-300 shadow-[inset_0_0_0_1px_var(--hairline)]",
  rigel:
    "bg-rigel-400/12 text-rigel-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_24%,transparent)]",
  betel:
    "bg-betel-400/12 text-betel-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-betel-400)_26%,transparent)]",
  nebula:
    "bg-nebula-400/14 text-nebula-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-nebula-400)_28%,transparent)]",
  aurora:
    "bg-aurora-400/12 text-aurora-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-aurora-400)_24%,transparent)]",
  dusk: "bg-dusk-400/12 text-dusk-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-dusk-400)_24%,transparent)]",
  alert:
    "bg-alert-400/14 text-alert-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_30%,transparent)]",
  ok: "bg-ok-400/12 text-ok-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-ok-400)_26%,transparent)]",
}

const DOTS: Record<BadgeTone, string> = {
  neutral: "bg-steel-400",
  rigel: "bg-rigel-400",
  betel: "bg-betel-400",
  nebula: "bg-nebula-400",
  aurora: "bg-aurora-400",
  dusk: "bg-dusk-400",
  alert: "bg-alert-400",
  ok: "bg-ok-400",
}

/**
 * A badge.
 * @param props the `tone`, whether to show a `dot` and whether it `pulse`s, and a span's props
 * @returns the badge
 */
export function Badge({
  tone = "neutral",
  dot = false,
  pulse = false,
  className,
  children,
  ...props
}: ComponentProps<"span"> & { tone?: BadgeTone; dot?: boolean; pulse?: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-2 font-medium text-micro [&_svg]:size-3",
        TONES[tone],
        className,
      )}
      {...props}
    >
      {dot && (
        <span aria-hidden="true" className={cn("size-1.5 rounded-full", DOTS[tone], pulse && "animate-breathe")} />
      )}
      {children}
    </span>
  )
}
