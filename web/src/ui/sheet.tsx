/**
 * Sheets (Base UI's Drawer): glass panels that slide in from an edge and follow your finger; swipe them back to close.
 * A bottom sheet (the phone's Now and cards) has a grab handle and 28 px top corners; a side sheet (the tablet's
 * Now on the left, its cards on the right) has them on its inner side. The backdrop fades with the swipe; a close
 * button sits by the title for those who don't swipe.
 */
import { Drawer as Base } from "@base-ui/react/drawer"
import { X } from "lucide-react"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { IconButton } from "./button"

/**
 * A sheet, opened and closed by its owner.
 * @param props `open`, `onOpenChange`, the `side` it comes from, its `title` (shown, or only for screen readers with
 *   `hideTitle`), the body (`children`) and classes
 * @returns the sheet
 */
export function Sheet({
  open,
  onOpenChange,
  side = "bottom",
  title,
  hideTitle = false,
  children,
  className,
  testId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  side?: "bottom" | "right" | "left"
  title: ReactNode
  hideTitle?: boolean
  children: ReactNode
  className?: string
  testId?: string
}) {
  const bottom = side === "bottom"
  return (
    <Base.Root open={open} onOpenChange={onOpenChange} swipeDirection={bottom ? "down" : side}>
      <Base.Portal>
        <Base.Backdrop className="fixed inset-0 z-50 bg-void-950 opacity-[calc(0.62*(1-var(--drawer-swipe-progress)))] transition-opacity duration-(--dur-slow) ease-(--ease-expo) data-ending-style:opacity-0 data-starting-style:opacity-0 data-swiping:duration-0" />
        <Base.Viewport
          className={cn(
            "fixed inset-0 z-50 flex",
            bottom ? "items-end justify-center" : side === "right" ? "justify-end" : "justify-start",
          )}
        >
          <Base.Popup
            data-testid={testId}
            className={cn(
              "glass-solid flex flex-col overflow-hidden text-steel-100 outline-none transition-transform duration-(--dur-slow) ease-(--ease-expo) data-swiping:select-none data-swiping:duration-0 data-ending-style:duration-(--dur-base)",
              bottom
                ? "max-h-[88dvh] w-full rounded-t-sheet rounded-b-none pb-(--safe-b) [transform:translateY(var(--drawer-swipe-movement-y))] data-ending-style:[transform:translateY(100%)] data-starting-style:[transform:translateY(100%)]"
                : side === "right"
                  ? "h-full w-[min(26rem,calc(100vw-3rem))] rounded-l-sheet rounded-r-none [transform:translateX(var(--drawer-swipe-movement-x))] data-ending-style:[transform:translateX(100%)] data-starting-style:[transform:translateX(100%)]"
                  : "h-full w-[min(22rem,calc(100vw-3rem))] rounded-r-sheet rounded-l-none [transform:translateX(var(--drawer-swipe-movement-x))] data-ending-style:[transform:translateX(-100%)] data-starting-style:[transform:translateX(-100%)]",
              className,
            )}
          >
            {bottom && (
              <div aria-hidden="true" className="flex shrink-0 justify-center pt-2.5 pb-1">
                <span className="h-1 w-10 rounded-full bg-steel-50/20" />
              </div>
            )}
            <div className={cn("flex shrink-0 items-center gap-2 pr-3 pl-5", bottom ? "pt-1 pb-2" : "pt-3 pb-2")}>
              <Base.Title className={cn("min-w-0 flex-1 font-semibold text-md text-steel-50", hideTitle && "sr-only")}>
                {title}
              </Base.Title>
              <Base.Close render={<IconButton label="Close" tip={false} size="sm" className="ml-auto" />}>
                <X />
              </Base.Close>
            </div>
            <Base.Content className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pb-4">
              {children}
            </Base.Content>
          </Base.Popup>
        </Base.Viewport>
      </Base.Portal>
    </Base.Root>
  )
}
