/**
 * His state in words beside the wordmark: "At rest", "Listening", "Thinking · Search the web", "Speaking", "Waking
 * his brain", "Offline". When the words change the old letters lift away and the new ones rise into place one after
 * another (Anime.js splits the text into letters for the move, then puts plain text back, so it stays crisp and
 * selectable). Under reduced motion the words simply change. A screen reader hears the new state.
 */
import { animate, splitText, stagger } from "animejs"
import { useEffect, useLayoutEffect, useRef } from "react"
import { reducedMotion } from "@/lib/motion"
import { type Live, useLive } from "@/lib/store"
import { cn } from "@/lib/utils"

/**
 * His state in words.
 * @param s the live state's slice
 * @returns the words
 */
export function stateWords(s: Pick<Live, "state" | "link" | "messages">): string {
  if (s.link !== "online") return s.link === "connecting" ? "Connecting" : "Offline"
  switch (s.state) {
    case "idle":
      return "At rest"
    case "listening":
      return "Listening"
    case "thinking": {
      const turn = s.messages.findLast((m) => m.role === "assistant" && m.streaming)
      const op = turn?.steps?.findLast((st) => st.status === "running")
      return op ? `Thinking · ${op.title}` : "Thinking"
    }
    case "speaking":
      return "Speaking"
    case "loading":
      return "Waking his brain"
    default:
      return "Offline"
  }
}

/**
 * The label.
 * @param props classes
 * @returns the label
 */
export function StateLabel({ className }: { className?: string }) {
  const words = useLive((s) => stateWords(s))
  const el = useRef<HTMLSpanElement>(null)
  const shown = useRef("")

  // the first words, at once
  useLayoutEffect(() => {
    if (el.current && !shown.current) {
      el.current.textContent = words
      shown.current = words
    }
  }, [words])

  useEffect(() => {
    const node = el.current
    if (!node || words === shown.current) return
    shown.current = words
    if (reducedMotion()) {
      node.textContent = words
      return
    }
    let cancelled = false
    const out = splitText(node, { chars: true })
    const leave = animate(out.chars, {
      opacity: [1, 0],
      y: ["0em", "-0.45em"],
      duration: 160,
      delay: stagger(8),
      ease: "in(2)",
    })
    let enter: ReturnType<typeof animate> | undefined
    leave.then(() => {
      if (cancelled) return
      out.revert()
      node.textContent = words
      const split = splitText(node, { chars: true })
      enter = animate(split.chars, {
        opacity: [0, 1],
        y: ["0.5em", "0em"],
        duration: 520,
        delay: stagger(16),
        ease: "out(4)",
      })
      enter.then(() => {
        if (cancelled) return
        split.revert()
        node.textContent = words
      })
    })
    return () => {
      cancelled = true
      leave.cancel()
      enter?.cancel()
      node.textContent = shown.current
    }
  }, [words])

  return (
    <span className={cn("relative inline-flex min-w-0 items-center", className)}>
      <span ref={el} aria-hidden="true" className="inline-block truncate whitespace-pre text-(--state)" />
      <span role="status" className="sr-only">
        {words}
      </span>
    </span>
  )
}
