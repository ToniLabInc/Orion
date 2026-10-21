/**
 * A key, as the HUD names its shortcuts: a small cap with a hairline and a darker bottom edge. `modKey()` says ⌘ on a
 * Mac and Ctrl elsewhere.
 */
import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"

/**
 * A key cap.
 * @param props a kbd's props
 * @returns the key
 */
export function Kbd({ className, ...props }: ComponentProps<"kbd">) {
  return (
    <kbd
      className={cn(
        "pointer-events-none inline-flex h-5 min-w-5 select-none items-center justify-center gap-0.5 rounded-chip bg-steel-50/7 px-1.5 font-mono text-micro text-steel-300 shadow-[inset_0_0_0_1px_var(--hairline),inset_0_-1px_0_0_var(--hairline-strong)] [&_svg]:size-3",
        className,
      )}
      {...props}
    />
  )
}

/**
 * The key that goes with K for the command palette: ⌘ on Apple devices, Ctrl elsewhere.
 * @returns the key's name
 */
export function modKey(): string {
  if (typeof navigator === "undefined") return "Ctrl"
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? "⌘" : "Ctrl"
}
