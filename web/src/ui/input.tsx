/**
 * Text fields: a dark well under the glass with a hairline that lights Rigel while you type in it (inputs, text areas,
 * a select's trigger share `FIELD`). A text area grows with what is written (`field-sizing: content`), up to its
 * max-height.
 */
import { Input as BaseInput } from "@base-ui/react/input"
import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"

/** What every field shares. */
export const FIELD =
  "rounded-control bg-void-950/55 text-sm text-steel-50 shadow-[inset_0_0_0_1px_var(--hairline),inset_0_1px_2px_0_oklch(0_0_0/0.25)] outline-none transition-[box-shadow,background-color] duration-(--dur-quick) placeholder:text-steel-500 hover:shadow-[inset_0_0_0_1px_var(--hairline-strong),inset_0_1px_2px_0_oklch(0_0_0/0.25)] focus-visible:bg-void-950/75 focus-visible:shadow-[inset_0_0_0_1px_var(--color-rigel-400),0_0_0_3px_color-mix(in_oklch,var(--color-rigel-400)_18%,transparent)] focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:shadow-[inset_0_0_0_1px_var(--color-alert-400)]"

/**
 * A one-line text field.
 * @param props an input's props
 * @returns the field
 */
export function Input({ className, ...props }: ComponentProps<"input">) {
  return <BaseInput data-slot="input" className={cn(FIELD, "h-9 w-full min-w-0 px-3", className)} {...props} />
}

/**
 * A text area that grows with its text.
 * @param props a textarea's props
 * @returns the field
 */
export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(FIELD, "field-sizing-content min-h-20 w-full resize-none px-3 py-2.5 leading-relaxed", className)}
      {...props}
    />
  )
}
