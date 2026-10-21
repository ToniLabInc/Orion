/**
 * Dialogs (Base UI): dense glass over a dimmed page (no blur behind: it would re-blur the living scene every frame).
 * The popup rises 8 px while it fades in and sinks back as it goes; focus is trapped inside and Esc closes it. Never
 * scaled.
 */
import { Dialog as Base } from "@base-ui/react/dialog"
import { X } from "lucide-react"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { IconButton } from "./button"

/** The dimmed page behind a dialog or a sheet. */
export const BACKDROP =
  "fixed inset-0 z-50 bg-void-950/62 transition-opacity duration-(--dur-base) ease-out data-ending-style:opacity-0 data-starting-style:opacity-0"

/**
 * A dialog, opened and closed by its owner.
 * @param props `open`, `onOpenChange`, its `title` (and an optional `description`), a `kicker` above the title,
 *   `actions` along its bottom, the body (`children`), and its width and classes
 * @returns the dialog
 */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  kicker,
  actions,
  children,
  wide = false,
  className,
  testId,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  description?: ReactNode
  kicker?: ReactNode
  actions?: ReactNode
  children?: ReactNode
  wide?: boolean
  className?: string
  testId?: string
}) {
  return (
    <Base.Root open={open} onOpenChange={onOpenChange}>
      <Base.Portal>
        <Base.Backdrop className={BACKDROP} />
        <Base.Viewport className="fixed inset-0 z-50 grid grid-cols-[minmax(0,1fr)] place-items-center overflow-y-auto p-4">
          <Base.Popup
            data-testid={testId}
            className={cn(
              "glass-solid relative grid w-full max-w-md grid-cols-1 gap-5 rounded-card p-6 text-steel-100 outline-none",
              "transition-[opacity,translate] duration-(--dur-base) ease-(--ease-expo) data-ending-style:translate-y-2 data-ending-style:opacity-0 data-starting-style:translate-y-2 data-starting-style:opacity-0",
              wide && "max-w-xl",
              className,
            )}
          >
            <div className="grid gap-1.5 pr-8">
              {kicker && <p className="caps text-rigel-300">{kicker}</p>}
              <Base.Title className="font-semibold text-lg text-steel-50">{title}</Base.Title>
              {description && <Base.Description className="text-sm text-steel-400">{description}</Base.Description>}
            </div>
            {children}
            {actions && <div className="flex flex-wrap items-center justify-end gap-2">{actions}</div>}
            <Base.Close render={<IconButton label="Close" tip={false} size="sm" className="absolute top-4 right-4" />}>
              <X />
            </Base.Close>
          </Base.Popup>
        </Base.Viewport>
      </Base.Portal>
    </Base.Root>
  )
}

export { Base as DialogParts }
