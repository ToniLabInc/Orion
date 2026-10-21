/**
 * The switch (Base UI): a pill whose thumb glides over on a snappy curve; on, the track fills with Rigel and the thumb
 * glows a little. A larger hit area than it shows.
 */
import { Switch as Base } from "@base-ui/react/switch"
import { cn } from "@/lib/utils"

/**
 * A switch.
 * @param props Base UI's switch props, and a `size`
 * @returns the switch
 */
export function Switch({ size = "md", className, ...props }: Base.Root.Props & { size?: "sm" | "md" }) {
  return (
    <Base.Root
      data-size={size}
      className={cn(
        "group/switch relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full bg-steel-50/10 p-0.5 shadow-[inset_0_0_0_1px_var(--hairline-strong)] outline-none transition-[background-color,box-shadow] duration-(--dur-quick) ease-out",
        "after:absolute after:-inset-2 hover:bg-steel-50/14 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rigel-400",
        "data-checked:bg-rigel-400 data-checked:shadow-[inset_0_0_0_1px_var(--color-rigel-300),0_0_16px_-4px_var(--color-rigel-400)] data-disabled:cursor-not-allowed data-disabled:opacity-40",
        "data-[size=sm]:h-4 data-[size=sm]:w-7",
        className,
      )}
      {...props}
    >
      <Base.Thumb className="block size-4 rounded-full bg-steel-200 shadow-[0_1px_3px_oklch(0_0_0/0.4)] transition-[translate,background-color] duration-(--dur-base) ease-(--ease-expo) group-data-[size=sm]/switch:size-3 data-checked:translate-x-4 data-checked:bg-white group-data-[size=sm]/switch:data-checked:translate-x-3" />
    </Base.Root>
  )
}
