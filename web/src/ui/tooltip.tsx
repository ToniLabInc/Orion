/**
 * Tooltips (Base UI): a small dense-glass label beside its trigger, with an optional key. It fades in while sliding
 * 4 px away from the trigger; moving between triggers of one group shows the next at once (the provider in app.tsx
 * shares the delay). Never scaled.
 */
import { Tooltip as Base } from "@base-ui/react/tooltip"
import type { ReactElement, ReactNode } from "react"
import { cn } from "@/lib/utils"
import { Kbd } from "./kbd"

/** Shares the opening delay among the tooltips inside it, so a second one opens at once. */
export function TooltipProvider({ delay = 320, ...props }: Base.Provider.Props) {
  return <Base.Provider delay={delay} {...props} />
}

/**
 * A tooltip round a trigger.
 * @param props what it says (`content`), an optional `shortcut`, its `side`, the trigger element (`render`, without
 *   children) and the trigger's content (`children`)
 * @returns the trigger with its tooltip
 */
export function Tooltip({
  content,
  shortcut,
  side = "top",
  render,
  children,
  className,
}: {
  content: ReactNode
  shortcut?: ReactNode
  side?: "top" | "bottom" | "left" | "right"
  render: ReactElement<Record<string, unknown>>
  children?: ReactNode
  className?: string
}) {
  return (
    <Base.Root>
      <Base.Trigger render={render}>{children}</Base.Trigger>
      <Base.Portal>
        <Base.Positioner side={side} sideOffset={8} className="isolate z-[70]">
          <Base.Popup
            className={cn(
              "flex max-w-xs items-center gap-2 rounded-control bg-void-700/95 px-2.5 py-1.5 text-xs text-steel-100 shadow-[0_10px_30px_-12px_oklch(0_0_0/0.8),inset_0_0_0_1px_var(--hairline-strong)]",
              "origin-(--transform-origin) transition-[opacity,translate] duration-(--dur-quick) ease-(--ease-expo) data-instant:transition-none data-ending-style:opacity-0 data-starting-style:opacity-0",
              "data-[side=bottom]:data-starting-style:-translate-y-1 data-[side=top]:data-starting-style:translate-y-1 data-[side=left]:data-starting-style:translate-x-1 data-[side=right]:data-starting-style:-translate-x-1",
              className,
            )}
          >
            {content}
            {shortcut && <Kbd className="-mr-1">{shortcut}</Kbd>}
          </Base.Popup>
        </Base.Positioner>
      </Base.Portal>
    </Base.Root>
  )
}
