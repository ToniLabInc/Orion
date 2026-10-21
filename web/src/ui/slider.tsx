/**
 * The slider (Base UI): a 4 px track with a Rigel run and a round thumb that grows a halo while held.
 */
import { Slider as Base } from "@base-ui/react/slider"
import { cn } from "@/lib/utils"

/**
 * A slider (one thumb per value).
 * @param props Base UI's slider props
 * @returns the slider
 */
export function Slider({ className, defaultValue, value, min = 0, max = 100, ...props }: Base.Root.Props) {
  const values = Array.isArray(value) ? value : Array.isArray(defaultValue) ? defaultValue : [min]
  return (
    <Base.Root
      className={cn("w-full", className)}
      defaultValue={defaultValue}
      value={value}
      min={min}
      max={max}
      thumbAlignment="edge"
      {...props}
    >
      <Base.Control className="relative flex h-6 w-full touch-none select-none items-center data-disabled:opacity-50">
        <Base.Track className="relative h-1 w-full grow rounded-full bg-steel-50/10 shadow-[inset_0_0_0_1px_var(--hairline)]">
          <Base.Indicator className="h-full rounded-full bg-linear-to-r from-rigel-600 to-rigel-400" />
        </Base.Track>
        {values.map((_, index) => (
          <Base.Thumb
            // biome-ignore lint/suspicious/noArrayIndexKey: a thumb's only identity is its position (first value, first thumb)
            key={index}
            className="relative block size-4 shrink-0 rounded-full bg-white shadow-[0_1px_4px_oklch(0_0_0/0.5),0_0_0_4px_color-mix(in_oklch,var(--color-rigel-400)_0%,transparent)] outline-none transition-[box-shadow] duration-(--dur-quick) after:absolute after:-inset-2 hover:shadow-[0_1px_4px_oklch(0_0_0/0.5),0_0_0_5px_color-mix(in_oklch,var(--color-rigel-400)_22%,transparent)] focus-visible:shadow-[0_1px_4px_oklch(0_0_0/0.5),0_0_0_5px_color-mix(in_oklch,var(--color-rigel-400)_40%,transparent)] data-dragging:shadow-[0_1px_4px_oklch(0_0_0/0.5),0_0_0_7px_color-mix(in_oklch,var(--color-rigel-400)_30%,transparent)]"
          />
        ))}
      </Base.Control>
    </Base.Root>
  )
}
