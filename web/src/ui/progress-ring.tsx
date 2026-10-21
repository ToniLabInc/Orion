/**
 * A progress ring: a faint track and an arc with round ends, its colour a gradient of one tone, drawn clockwise from
 * twelve o'clock. Countdowns (timers, a confirmation's time to answer) re-render once a second; a big ring lets the arc
 * glide between ticks with a linear transition as long as the tick (`glide`), so it moves continuously at one render a
 * second, and a small one (an arc of a fraction of a pixel a second, in the top bar) steps with the tick instead: a
 * transition on an SVG attribute is the main thread's work every frame, for as long as it runs. Something inside (a
 * figure, an icon) goes in `children`.
 */
import { type ReactNode, useId } from "react"
import { cn } from "@/lib/utils"

export type RingTone = "rigel" | "betel" | "nebula" | "aurora" | "dusk" | "alert" | "ok"

/**
 * The ring.
 * @param props `value` (0 to 1), its `size` and `stroke` in px, the `tone`, how long the arc takes to reach a new value
 *   (`glide`, ms: 0 for a jump, the tick's length for a countdown), an aria `label`, and what goes inside
 * @returns the ring
 */
export function ProgressRing({
  value,
  size = 40,
  stroke = 3,
  tone = "rigel",
  glide = 240,
  linear = false,
  label,
  children,
  className,
}: {
  value: number
  size?: number
  stroke?: number
  tone?: RingTone
  glide?: number
  linear?: boolean
  label?: string
  children?: ReactNode
  className?: string
}) {
  const id = useId()
  const r = (size - stroke) / 2
  const shown = Math.min(1, Math.max(0, value))
  const named = label ? { role: "img", "aria-label": label } : { "aria-hidden": true }
  return (
    <span
      {...named}
      className={cn("relative inline-grid shrink-0 place-items-center", className)}
      style={{ width: size, height: size }}
    >
      <svg viewBox={`0 0 ${size} ${size}`} className="absolute inset-0 -rotate-90" aria-hidden="true">
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor={`var(--color-${tone}-300)`} />
            <stop offset="1" stopColor={`var(--color-${tone}-500)`} />
          </linearGradient>
        </defs>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={stroke} className="stroke-steel-50/9" />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={`url(#${id})`}
          strokeWidth={stroke}
          strokeLinecap="round"
          pathLength={1}
          strokeDasharray="1 1"
          strokeDashoffset={1 - shown}
          opacity={shown > 0 ? 1 : 0}
          style={{
            transition: glide ? `stroke-dashoffset ${glide}ms ${linear ? "linear" : "var(--ease-expo)"}` : undefined,
          }}
        />
      </svg>
      {children !== undefined && <span className="relative">{children}</span>}
    </span>
  )
}
