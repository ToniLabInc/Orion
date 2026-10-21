/**
 * Copy some text, the icon turning into a tick for a second.
 */
import { Check, Copy } from "lucide-react"
import { useEffect, useState } from "react"
import { cn } from "@/lib/utils"
import { toast } from "./toast"

/**
 * The button.
 * @param props the `text`, the button's `label` for screen readers, and classes
 * @returns the button
 */
export function CopyButton({ text, label = "Copy", className }: { text: string; label?: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1200)
    return () => clearTimeout(timer)
  }, [copied])
  return (
    <button
      type="button"
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      className={cn(
        "grid size-7 shrink-0 place-items-center rounded-chip text-steel-500 outline-none transition-colors hover:bg-steel-50/8 hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400",
        copied && "text-ok-400 hover:text-ok-400",
        className,
      )}
      onClick={(e) => {
        e.stopPropagation()
        navigator.clipboard
          ?.writeText(text)
          .then(() => setCopied(true))
          .catch(() => toast.add({ title: "Not copied", description: "The browser didn't allow it.", type: "warning" }))
      }}
    >
      {copied ? <Check aria-hidden="true" className="size-3.5" /> : <Copy aria-hidden="true" className="size-3.5" />}
    </button>
  )
}
