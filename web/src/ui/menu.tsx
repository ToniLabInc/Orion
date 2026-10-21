/**
 * Floating lists and popovers (Base UI): dense glass with a 14 px radius that fades and drops 4 px from its anchor.
 * `MENU` and `MENU_ITEM` are the classes the select and the palette share; `Popover` holds free content.
 */
import { Popover as BasePopover } from "@base-ui/react/popover"
import type { ReactElement, ReactNode } from "react"
import { cn } from "@/lib/utils"

/** A floating list's surface. */
export const MENU =
  "glass-solid overflow-y-auto overscroll-contain rounded-menu p-1.5 text-sm text-steel-100 outline-none origin-(--transform-origin) transition-[opacity,translate] duration-(--dur-quick) ease-(--ease-expo) data-ending-style:opacity-0 data-starting-style:opacity-0 data-[side=bottom]:data-starting-style:-translate-y-1 data-[side=top]:data-starting-style:translate-y-1"

/** One row of a floating list. */
export const MENU_ITEM =
  "relative flex min-h-9 w-full cursor-default select-none items-center gap-2.5 rounded-control px-2.5 py-1.5 text-sm text-steel-200 outline-none transition-colors duration-(--dur-instant) data-disabled:pointer-events-none data-highlighted:bg-steel-50/8 data-highlighted:text-steel-50 data-disabled:opacity-45 [&_svg:not([class*='size-'])]:size-4 [&_svg]:shrink-0"

/**
 * A popover: free content in a glass bubble beside its trigger.
 * @param props the trigger element (`trigger`), its content (`label`), the popover's content (`children`), and its
 *   placement and classes
 * @returns the popover
 */
export function Popover({
  trigger,
  label,
  children,
  side = "bottom",
  align = "center",
  open,
  onOpenChange,
  className,
}: {
  trigger: ReactElement<Record<string, unknown>>
  label?: ReactNode
  children: ReactNode
  side?: "top" | "bottom" | "left" | "right"
  align?: "start" | "center" | "end"
  open?: boolean
  onOpenChange?: (open: boolean) => void
  className?: string
}) {
  return (
    <BasePopover.Root open={open} onOpenChange={onOpenChange}>
      <BasePopover.Trigger render={trigger}>{label}</BasePopover.Trigger>
      <BasePopover.Portal>
        <BasePopover.Positioner side={side} align={align} sideOffset={10} className="isolate z-[60]">
          <BasePopover.Popup className={cn(MENU, "p-3", className)}>{children}</BasePopover.Popup>
        </BasePopover.Positioner>
      </BasePopover.Portal>
    </BasePopover.Root>
  )
}
