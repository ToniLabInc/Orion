/**
 * The conversation column: open, a bottom-anchored list of glass messages whose older lines fade upward into the sky;
 * folded (ambient), his captions alone, held well above the limb (the band just over it is Horizon's, for the words
 * it writes in light), and a pill to open it again. It opens when you type, scroll up over it or ask for it, and folds
 * again after a while of nobody touching it. The list stays at its newest line unless you scroll up (a "Latest" button
 * brings it back), and it never smooth-scrolls on its own: an answer streaming in sticks to the bottom crisply.
 */
import { ArrowDown, ChevronsDown, MessagesSquare } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { type RefObject, useCallback, useEffect, useRef, useState } from "react"
import { DUR, EXPO, SPRING } from "@/lib/motion"
import { setOpen, useStage } from "@/lib/stage"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { say } from "@/lib/ws"
import { Button, IconButton } from "@/ui/button"
import { Captions } from "./captions"
import { Message } from "./message"

/** A conversation nobody has touched for this long folds back to his captions. */
const FOLD_AFTER_MS = 150_000

const TRY = ["What's the weather like?", "Set a timer for 10 minutes", "How is the PC doing?", "What's on my calendar?"]

/**
 * The column.
 * @param props classes
 * @returns the conversation, open or folded
 */
export function Conversation({ className }: { className?: string }) {
  const open = useStage((s) => s.open)
  const count = useLive((s) => s.messages.filter((m) => m.role === "user" || m.role === "assistant").length)
  const column = useRef<HTMLElement>(null)
  useFoldWhenIdle(column, open)

  // scrolling up over the folded column opens it
  useEffect(() => {
    const el = column.current
    if (!el || open) return
    const wheel = (e: WheelEvent) => {
      if (e.deltaY < -4) setOpen(true)
    }
    el.addEventListener("wheel", wheel, { passive: true })
    return () => el.removeEventListener("wheel", wheel)
  }, [open])

  return (
    <section
      ref={column}
      aria-label="Conversation"
      className={cn("relative flex min-h-0 flex-col justify-end", className)}
      data-testid="conversation"
      data-open={open || undefined}
    >
      <AnimatePresence initial={false} mode="popLayout">
        {open ? (
          <motion.div
            key="log"
            className="flex min-h-0 flex-1 flex-col"
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 16, transition: { duration: DUR.base, ease: [...EXPO] } }}
            transition={SPRING.float}
          >
            <Log />
          </motion.div>
        ) : (
          <motion.div
            key="folded"
            className="flex flex-col items-center"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, transition: { duration: DUR.quick } }}
            transition={{ delay: 0.15, ...SPRING.gentle }}
          >
            <Captions className="w-full" />
            <div aria-hidden="true" className="h-[clamp(2.5rem,13dvh,8rem)]" />
            <div className="flex h-8 justify-center">
              {count > 0 && (
                <button
                  type="button"
                  onClick={() => setOpen(true)}
                  className="glass flex h-8 items-center gap-2 rounded-full pr-3.5 pl-3 text-xs text-steel-300 outline-none transition-colors hover:text-steel-50 focus-visible:outline-2 focus-visible:outline-rigel-400"
                  data-testid="open-conversation"
                >
                  <MessagesSquare aria-hidden="true" className="size-3.5 text-rigel-300" />
                  Conversation
                  <span className="font-mono text-micro text-steel-400 tabular-nums">{count}</span>
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}

/**
 * Fold the open conversation after a while of nobody touching it (no pointer over it, no focus in it, nothing
 * streaming, nothing typed).
 * @param column the column's element
 * @param open whether it is open
 */
function useFoldWhenIdle(column: RefObject<HTMLElement | null>, open: boolean): void {
  useEffect(() => {
    const el = column.current
    if (!open || !el) return
    let timer = 0
    const arm = () => {
      clearTimeout(timer)
      timer = window.setTimeout(() => {
        const busy =
          el.matches(":hover") ||
          el.contains(document.activeElement) ||
          document.activeElement?.closest("[data-composer]") ||
          useLive.getState().messages.some((m) => m.streaming)
        if (busy) arm()
        else setOpen(false)
      }, FOLD_AFTER_MS)
    }
    arm()
    const kinds = ["pointermove", "wheel", "keydown", "focusin"] as const
    for (const kind of kinds) el.addEventListener(kind, arm, { passive: true })
    const offStore = useLive.subscribe((s, before) => {
      if (s.messages !== before.messages) arm()
    })
    return () => {
      clearTimeout(timer)
      for (const kind of kinds) el.removeEventListener(kind, arm)
      offStore()
    }
  }, [column, open])
}

/**
 * Keep a list at its newest line while you are at its bottom, and leave it where it is once you scroll up. Only
 * scrolling up leaves the bottom: a burst of growth below (a reply streaming, a question appearing) never does.
 * @param scroller the scrolling element
 * @param content its content (watched for growth)
 * @returns `away` (you scrolled up) and `toBottom()`
 */
function useStickToBottom(scroller: RefObject<HTMLElement | null>, content: RefObject<HTMLElement | null>) {
  const stuck = useRef(true)
  const [away, setAway] = useState(false)
  const toBottom = useCallback(() => {
    const el = scroller.current
    if (el) el.scrollTop = el.scrollHeight
    stuck.current = true
    setAway(false)
  }, [scroller])
  useEffect(() => {
    const el = scroller.current
    const inner = content.current
    if (!el || !inner) return
    el.scrollTop = el.scrollHeight
    let last = el.scrollTop
    const onScroll = () => {
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 40) stuck.current = true
      else if (el.scrollTop < last - 1) stuck.current = false
      last = el.scrollTop
      setAway(!stuck.current)
    }
    el.addEventListener("scroll", onScroll, { passive: true })
    const follow = new ResizeObserver(() => {
      if (!stuck.current) return
      el.scrollTop = el.scrollHeight
      last = el.scrollTop
    })
    follow.observe(inner)
    follow.observe(el)
    return () => {
      el.removeEventListener("scroll", onScroll)
      follow.disconnect()
    }
  }, [scroller, content])
  return { away, toBottom }
}

/**
 * The open conversation.
 * @returns the list
 */
function Log() {
  const messages = useLive((s) => s.messages)
  const scroller = useRef<HTMLDivElement>(null)
  const inner = useRef<HTMLDivElement>(null)
  const { away, toBottom } = useStickToBottom(scroller, inner)
  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={scroller}
        role="log"
        aria-label="Messages"
        // biome-ignore lint/a11y/noNoninteractiveTabindex: only a log that takes the focus scrolls with the keys
        tabIndex={0}
        className="fade-top no-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain rounded-card outline-none [--fade:5rem] [overflow-anchor:none] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-rigel-400"
        data-testid="messages"
      >
        <div ref={inner} className="flex min-h-full flex-col justify-end px-1 pt-20 pb-3">
          {messages.length ? (
            <ol className="grid grid-cols-1 gap-4">
              <AnimatePresence initial={false}>
                {messages.map((m) => (
                  <Message key={m.id} message={m} />
                ))}
              </AnimatePresence>
            </ol>
          ) : (
            <Suggestions />
          )}
        </div>
      </div>
      <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
        <AnimatePresence>
          {away && (
            <motion.div
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 6 }}
              transition={SPRING.snappy}
            >
              <Button size="xs" className="glass-solid pointer-events-auto rounded-full" onClick={toBottom}>
                <ArrowDown aria-hidden="true" />
                Latest
              </Button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
      <IconButton
        label="Fold to his captions"
        tip="left"
        size="sm"
        className="absolute top-2 right-0"
        onClick={() => setOpen(false)}
        data-testid="fold-conversation"
      >
        <ChevronsDown />
      </IconButton>
    </div>
  )
}

/**
 * A new conversation: a few things to ask.
 * @returns the suggestions
 */
function Suggestions() {
  const online = useLive((s) => s.link === "online")
  const honorific = useLive((s) => s.honorific)
  return (
    <div className="grid justify-items-center gap-4 pb-6 text-center">
      <p className="font-serif text-2xl text-steel-100 italic">
        {honorific ? `At your service, ${honorific}.` : "At your service."}
      </p>
      <p className="text-sm text-steel-400">Ask anything, or try one of these:</p>
      <div className="flex max-w-lg flex-wrap justify-center gap-2">
        {TRY.map((text) => (
          <Button key={text} size="sm" className="rounded-full" disabled={!online} onClick={() => say(text)}>
            {text}
          </Button>
        ))}
      </div>
    </div>
  )
}
