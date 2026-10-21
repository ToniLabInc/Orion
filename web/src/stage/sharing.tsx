/**
 * What this browser shares with him, and what he asks of it that needs your click: while the screen or the camera is
 * shared, a chip with its live picture and a way to stop; when he asks to see your screen or camera (a `request` of
 * kind share), a prompt with Share and Not now, because a browser shares only after your own click; when he wants to
 * copy text and the browser won't without a click, the text with Copy. The prompts show over any page.
 */
import { Camera, ClipboardCopy, MonitorUp, ScanEye, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useRef } from "react"
import type { Shared } from "@/device"
import { sharedStream, stopShare, useDevice } from "@/lib/device"
import { SPRING } from "@/lib/motion"
import { cn } from "@/lib/utils"
import { Button } from "@/ui/button"

/**
 * The chips of what is shared now.
 * @param props classes
 * @returns the chips
 */
export function SharingChips({ className }: { className?: string }) {
  const sharing = useDevice((s) => s.sharing)
  return (
    <div className={cn("flex flex-wrap justify-center gap-2", className)}>
      <AnimatePresence initial={false}>
        {sharing.map((what) => (
          <motion.div
            key={what}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={SPRING.snappy}
          >
            <SharingChip what={what} />
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}

/**
 * One shared thing: its live picture, its name, and stop.
 * @param props `what` is shared
 * @returns the chip
 */
function SharingChip({ what }: { what: Shared }) {
  const video = useRef<HTMLVideoElement>(null)
  useEffect(() => {
    const el = video.current
    const stream = sharedStream(what)
    if (!el || !stream) return
    el.srcObject = stream
    void el.play().catch(() => {})
    return () => {
      el.srcObject = null
    }
  }, [what])
  const Icon = what === "screen" ? MonitorUp : Camera
  return (
    <div
      className="glass flex h-11 items-center gap-2.5 rounded-full py-1 pr-1.5 pl-1 shadow-[var(--shadow-float),inset_0_0_0_1px_color-mix(in_oklch,var(--color-betel-400)_40%,transparent)]"
      data-testid={`sharing-${what}`}
    >
      <span className="relative h-9 w-14 overflow-hidden rounded-full bg-void-950">
        <video ref={video} muted playsInline className="size-full object-cover" aria-label={`Your ${what}, live`} />
        <span
          aria-hidden="true"
          className="absolute top-1 right-1.5 size-1.5 animate-breathe rounded-full bg-alert-400"
        />
      </span>
      <span className="flex items-center gap-1.5 text-xs text-steel-100">
        <Icon aria-hidden="true" className="size-3.5 text-betel-300" />
        {what === "screen" ? "Sharing your screen" : "Sharing your camera"}
      </span>
      <button
        type="button"
        onClick={() => stopShare(what)}
        aria-label={`Stop sharing your ${what}`}
        className="grid size-8 place-items-center rounded-full text-steel-300 outline-none transition-colors hover:bg-alert-400/16 hover:text-alert-200 focus-visible:outline-2 focus-visible:outline-rigel-400"
      >
        <X aria-hidden="true" className="size-4" />
      </button>
    </div>
  )
}

/**
 * What he asks of this browser that waits for your click, over any page: to see your screen or camera, to copy text.
 * @returns the prompts
 */
export function Requests() {
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-[calc(1.25rem+var(--safe-b))] z-[62] flex flex-col items-center gap-2 px-3 tab:top-[calc(var(--bar-h)+0.75rem)] tab:bottom-auto">
      <SharePrompt className="pointer-events-auto w-full max-w-lg" />
      <CopyPrompt className="pointer-events-auto w-full max-w-lg" />
    </div>
  )
}

/**
 * Text he wants copied that the browser won't copy without your click.
 * @param props classes
 * @returns the prompt, while it waits
 */
function CopyPrompt({ className }: { className?: string }) {
  const pending = useDevice((s) => s.pendingCopy)
  return (
    <AnimatePresence>
      {pending && (
        <motion.div
          key={pending.text}
          role="alertdialog"
          aria-label="Orion wants to copy text"
          initial={{ opacity: 0, y: -12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={SPRING.gentle}
          className={cn("glass-solid grid gap-3 rounded-card p-3 pl-4", className)}
          data-testid="copy-prompt"
        >
          <div className="flex items-center gap-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-full bg-rigel-400/14 text-rigel-200">
              <ClipboardCopy aria-hidden="true" className="size-4" />
            </span>
            <p className="min-w-0 flex-1 text-sm text-steel-100">He wants to copy this for you.</p>
            <div className="flex gap-2">
              <Button size="sm" onClick={() => pending.dismiss()}>
                Not now
              </Button>
              <Button size="sm" tone="primary" onClick={() => void pending.copy()}>
                Copy
              </Button>
            </div>
          </div>
          <pre className="max-h-24 overflow-y-auto whitespace-pre-wrap break-words rounded-control bg-void-950/60 px-3 py-2 font-mono text-xs text-steel-300">
            {pending.text}
          </pre>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

/**
 * His asking to see your screen or camera, waiting for your click.
 * @param props classes
 * @returns the prompt, while he asks
 */
function SharePrompt({ className }: { className?: string }) {
  const pending = useDevice((s) => s.pendingShare)
  return (
    <AnimatePresence>
      {pending && (
        <motion.div
          key={pending.what}
          role="alertdialog"
          aria-label={`Orion wants to see your ${pending.what}`}
          initial={{ opacity: 0, y: -12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={SPRING.gentle}
          className={cn(
            "glass-solid flex flex-wrap items-center gap-3 rounded-card p-3 pl-4 shadow-[var(--shadow-elevated),inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_35%,transparent)]",
            className,
          )}
          data-testid="share-prompt"
        >
          <span className="grid size-9 shrink-0 place-items-center rounded-full bg-rigel-400/14 text-rigel-200">
            <ScanEye aria-hidden="true" className="size-4.5" />
          </span>
          <p className="min-w-0 flex-1 text-sm text-steel-100">
            Orion wants to see your {pending.what}.
            <span className="block text-xs text-steel-400">Your browser asks you what to share.</span>
          </p>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => pending.decline()}>
              Not now
            </Button>
            <Button size="sm" tone="primary" onClick={() => void pending.accept()}>
              Share
            </Button>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
