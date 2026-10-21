/**
 * The boot: once a session, for a moment (1.6 s at most, a click or a key skips it), his name lights letter by letter
 * while the sky comes up behind it (Horizon's engine does that as it starts), and what he has loaded ticks underneath (his tools, brain, memory, voice, ears, from the `boot` events). Then the stage. Nothing
 * under reduced motion.
 */
import { animate, splitText, stagger } from "animejs"
import { Check, LoaderCircle, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useRef, useState } from "react"
import { reducedMotion } from "@/lib/motion"
import { useLive } from "@/lib/store"
import type { BootName } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Mark } from "@/ui/brand"

const KEY = "orion-booted" // in sessionStorage: the boot played in this tab
const BOOT_MS = 1600

const NAMES: Record<BootName, string> = {
  tools: "Tools",
  brain: "Brain",
  memory: "Memory",
  voice: "Voice",
  ears: "Ears",
}

/** Whether the boot should play now: once per tab, never under reduced motion. */
function first(): boolean {
  if (reducedMotion()) return false
  try {
    return !sessionStorage.getItem(KEY)
  } catch {
    return true // (no storage: it plays, and next time again)
  }
}

/**
 * The boot, while it plays.
 * @returns the overlay
 */
export function Boot() {
  const [on, setOn] = useState(first)
  const word = useRef<HTMLSpanElement>(null)
  const steps = useLive((s) => s.boot?.steps)

  useEffect(() => {
    if (!on) return
    try {
      sessionStorage.setItem(KEY, "1")
    } catch {}
    const timer = setTimeout(() => setOn(false), BOOT_MS)
    const skip = () => setOn(false)
    addEventListener("pointerdown", skip, { once: true })
    addEventListener("keydown", skip, { once: true })
    let split: ReturnType<typeof splitText> | undefined
    if (word.current) {
      split = splitText(word.current, { chars: true })
      animate(split.chars, {
        opacity: [0, 1],
        y: ["0.6em", "0em"],
        duration: 700,
        delay: stagger(70, { start: 150 }),
        ease: "out(4)",
      })
    }
    return () => {
      clearTimeout(timer)
      removeEventListener("pointerdown", skip)
      removeEventListener("keydown", skip)
      split?.revert()
    }
  }, [on])

  return (
    <AnimatePresence>
      {on && (
        <motion.div
          aria-hidden="true"
          className="fixed inset-0 z-[85] bg-[radial-gradient(circle_at_50%_38%,transparent,color-mix(in_oklch,var(--color-void-950)_80%,transparent)_55%)]"
          initial={{ opacity: 1 }}
          exit={{ opacity: 0, transition: { duration: 0.6 } }}
          data-testid="boot"
        >
          {/* in the sky as it comes up */}
          <div className="absolute inset-x-0 top-[30dvh] grid justify-items-center gap-5 px-4">
            <motion.span initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.5 }}>
              <Mark className="size-12 text-rigel-300" />
            </motion.span>
            <span ref={word} className="font-semibold text-xl text-steel-50 tracking-[0.5em]">
              ORION
            </span>
            <ul className="flex flex-wrap justify-center gap-1.5">
              {(steps ?? []).map((st, i) => (
                <motion.li
                  key={st.name}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.5 + i * 0.08 }}
                  className={cn(
                    "flex h-6 items-center gap-1.5 rounded-full bg-steel-50/6 px-2.5 text-micro shadow-[inset_0_0_0_1px_var(--hairline)]",
                    st.status === "error" ? "text-alert-300" : st.status === "ok" ? "text-steel-200" : "text-steel-500",
                  )}
                >
                  {st.status === "ok" ? (
                    <Check className="size-3 text-ok-400" />
                  ) : st.status === "error" ? (
                    <X className="size-3" />
                  ) : st.status === "pending" ? (
                    <LoaderCircle className="size-3 animate-spin" />
                  ) : null}
                  {NAMES[st.name] ?? st.name}
                </motion.li>
              ))}
            </ul>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
