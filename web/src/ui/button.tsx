/**
 * The HUD's buttons (Base UI's Button). Five tones: `primary` (Rigel, the one main action of a view), `secondary`
 * (glass with a hairline, the default), `ghost` (quiet: toolbars, icon actions), `danger` (something that can't be
 * undone) and `you` (Betelgeuse: your own voice, the talk button's family). A press steps the light and the inner
 * shadow; a button with words never scales, so its label stays crisp. `IconButton` is the square form, with the
 * aria-label it must have and an optional tooltip saying the same.
 */
import { Button as BaseButton } from "@base-ui/react/button"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { Tooltip } from "./tooltip"

const BASE =
  "group/button relative inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap rounded-control font-medium outline-none transition-[color,background-color,box-shadow,opacity,translate] duration-(--dur-instant) ease-out focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rigel-400 disabled:pointer-events-none disabled:opacity-40 data-disabled:pointer-events-none data-disabled:opacity-40 [&_svg]:pointer-events-none [&_svg]:shrink-0"

const TONES = {
  primary:
    "bg-rigel-400 text-void-950 shadow-[inset_0_1px_0_0_oklch(1_0_0/0.35),0_8px_24px_-12px_var(--color-rigel-400)] hover:bg-rigel-300 hover:shadow-[inset_0_1px_0_0_oklch(1_0_0/0.4),0_10px_30px_-10px_var(--color-rigel-400)] active:bg-rigel-500 active:shadow-[inset_0_2px_4px_0_oklch(0_0_0/0.25)]",
  secondary:
    "bg-steel-50/6 text-steel-100 shadow-[inset_0_0_0_1px_var(--hairline),inset_0_1px_0_0_var(--highlight)] hover:bg-steel-50/10 hover:text-steel-50 hover:shadow-[inset_0_0_0_1px_var(--hairline-strong),inset_0_1px_0_0_var(--highlight)] active:bg-steel-50/4 aria-pressed:bg-rigel-400/16 aria-pressed:text-rigel-100 aria-pressed:shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_40%,transparent)] aria-expanded:bg-steel-50/10",
  ghost:
    "text-steel-300 hover:bg-steel-50/7 hover:text-steel-50 active:bg-steel-50/4 aria-pressed:bg-rigel-400/14 aria-pressed:text-rigel-200 aria-expanded:bg-steel-50/8 aria-expanded:text-steel-50",
  danger:
    "bg-alert-400/14 text-alert-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_32%,transparent)] hover:bg-alert-400/24 hover:text-alert-100 active:bg-alert-400/10",
  you: "bg-betel-400 text-void-950 shadow-[inset_0_1px_0_0_oklch(1_0_0/0.3),0_8px_24px_-12px_var(--color-betel-400)] hover:bg-betel-300 active:bg-betel-500",
} as const

const SIZES = {
  xs: "h-7 gap-1.5 px-2.5 text-xs [&_svg:not([class*='size-'])]:size-3.5",
  sm: "h-8 gap-1.5 px-3 text-sm [&_svg:not([class*='size-'])]:size-4",
  md: "h-9 gap-2 px-3.5 text-sm [&_svg:not([class*='size-'])]:size-4",
  lg: "h-11 gap-2 px-5 text-base [&_svg:not([class*='size-'])]:size-[18px]",
} as const

const ICON_SIZES = {
  xs: "size-7 [&_svg:not([class*='size-'])]:size-3.5",
  sm: "size-8 [&_svg:not([class*='size-'])]:size-4",
  md: "size-9 [&_svg:not([class*='size-'])]:size-[18px]",
  lg: "size-11 [&_svg:not([class*='size-'])]:size-5",
} as const

export type Tone = keyof typeof TONES
export type Size = keyof typeof SIZES

/**
 * A button's classes, also for other elements that should look like one (a link).
 * @param options the `tone` (secondary by default), the `size` (md) and extra classes
 * @returns the classes
 */
export function buttonClass({
  tone = "secondary",
  size = "md",
  icon = false,
  className,
}: {
  tone?: Tone
  size?: Size
  icon?: boolean
  className?: string
} = {}) {
  return cn(BASE, TONES[tone], icon ? ICON_SIZES[size] : SIZES[size], className)
}

type BaseProps = Omit<BaseButton.Props, "className"> & { className?: string; tone?: Tone; size?: Size }

/**
 * A button.
 * @param props Base UI's button props, a `tone`, a `size` and classes merged over the defaults
 * @returns the button
 */
export function Button({ className, tone = "secondary", size = "md", ...props }: BaseProps) {
  return <BaseButton data-slot="button" className={buttonClass({ tone, size, className })} {...props} />
}

/**
 * A square button with only an icon: its `label` is said to screen readers and, unless `tip` is false, shown in a
 * tooltip (with an optional `shortcut`). A press nudges it 1 px down (no scale).
 * @param props the button's props, its `label`, the `tip` side (or false), a `shortcut` and the icon (`children`)
 * @returns the button
 */
export function IconButton({
  label,
  tip = "top",
  shortcut,
  tone = "ghost",
  size = "md",
  className,
  children,
  ...props
}: BaseProps & {
  label: string
  tip?: "top" | "bottom" | "left" | "right" | false
  shortcut?: ReactNode
  children?: ReactNode
}) {
  const common = {
    "data-slot": "icon-button",
    "aria-label": label,
    className: buttonClass({ tone, size, icon: true, className: cn("active:translate-y-px", className) }),
    ...props,
  }
  if (tip === false) return <BaseButton {...common}>{children}</BaseButton>
  return (
    <Tooltip content={label} shortcut={shortcut} side={tip} render={<BaseButton {...common} />}>
      {children}
    </Tooltip>
  )
}
