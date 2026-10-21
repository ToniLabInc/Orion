/**
 * The link to him, when it isn't simply up. A link that drops and stays down a moment shows a banner under the top bar
 * (it reconnects by itself, and says for how long it has been trying) while the sky dims and loses its colour; a
 * restart says he is back in a moment; when he comes back, "Back" for a moment. When he shuts down on purpose, or a
 * page is opened from a machine he doesn't answer, the screen says so whole: how to start him, or why not here.
 */
import { CircleCheck, LoaderCircle, Power, ShieldOff, WifiOff } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useRef, useState } from "react"
import { useNow } from "@/lib/hooks"
import { SPRING } from "@/lib/motion"
import { type Link, useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { Mark } from "@/ui/brand"

const LOST_AFTER_MS = 1500 // a link down this long is lost (a blip is back sooner)
const BACK_MS = 1800 // how long "Back" shows

/** What the screen says about the link. */
export type Signal = "lost" | "restart" | "shutdown" | "refused"

/**
 * What the screen should say about the link, if anything.
 * @param link the link
 * @param shutdown the shutdown he announced, if any (until his next "init")
 * @param down whether the link has been down long enough to count as lost
 * @returns the signal, or null while all is well or a blip has yet to count
 */
export function signalOf(link: Link, shutdown: { restart: boolean } | null, down: boolean): Signal | null {
  if (link === "refused") return "refused"
  if (link === "unpaired") return null // the pairing screen says it (shell/pair.tsx)
  if (shutdown) return shutdown.restart ? "restart" : "shutdown"
  return link !== "online" && down ? "lost" : null
}

/**
 * How long since `since`, as m:ss, ticking each second.
 * @param props `since` (ms since the epoch)
 * @returns the time
 */
function Elapsed({ since }: { since: number }) {
  const now = useNow().getTime()
  const seconds = Math.max(0, Math.floor((now - since) / 1000))
  return <span className="tabular-nums">{`${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`}</span>
}

/**
 * The link's banner and screens.
 * @returns what the screen says, while there is something to say
 */
export function LinkStatus() {
  const link = useLive((s) => s.link)
  const shutdown = useLive((s) => s.shutdown)
  const online = link === "online"
  const [down, setDown] = useState(false)
  const droppedAt = useRef(Date.now())
  const [back, setBack] = useState(false)
  const was = useRef<Signal | null>(null)

  useEffect(() => {
    if (online) {
      setDown(false)
      return
    }
    droppedAt.current = Date.now()
    const timer = setTimeout(() => setDown(true), LOST_AFTER_MS)
    return () => clearTimeout(timer)
  }, [online])

  const signal = signalOf(link, shutdown, down)
  useEffect(() => {
    if (!signal && was.current && online) {
      setBack(true)
      const timer = setTimeout(() => setBack(false), BACK_MS)
      was.current = null
      return () => clearTimeout(timer)
    }
    if (signal) was.current = signal
  }, [signal, online])

  const banner = signal === "lost" || signal === "restart" ? signal : back ? "back" : null
  return (
    <>
      <div className="pointer-events-none fixed inset-x-0 top-[calc(var(--bar-h)+0.5rem)] z-[70] flex justify-center px-3">
        <AnimatePresence>
          {banner && (
            <motion.div
              key={banner}
              role="status"
              aria-live="polite"
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={SPRING.gentle}
              className={cn(
                "glass-solid flex h-10 items-center gap-2.5 rounded-full pr-4 pl-3 text-sm",
                banner === "back"
                  ? "text-ok-200 shadow-[var(--shadow-float),inset_0_0_0_1px_color-mix(in_oklch,var(--color-ok-400)_35%,transparent)]"
                  : "text-steel-100 shadow-[var(--shadow-float),inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_35%,transparent)]",
              )}
              data-testid="link-banner"
              data-signal={banner}
            >
              {banner === "back" ? (
                <CircleCheck aria-hidden="true" className="size-4 text-ok-400" />
              ) : banner === "lost" ? (
                <WifiOff aria-hidden="true" className="size-4 text-alert-400" />
              ) : (
                <LoaderCircle aria-hidden="true" className="size-4 animate-spin text-rigel-300" />
              )}
              {banner === "back" ? (
                "Back with him"
              ) : banner === "lost" ? (
                <>
                  Link lost: reconnecting
                  <span className="font-mono text-xs text-steel-500">
                    <Elapsed since={droppedAt.current} />
                  </span>
                </>
              ) : (
                "Restarting: back in a moment"
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
      <AnimatePresence>
        {(signal === "shutdown" || signal === "refused") && (
          <motion.div
            key={signal}
            role="alert"
            className="fixed inset-0 z-[90] grid place-items-center bg-void-950/88 p-6"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.4 }}
            data-testid="signal"
            data-signal={signal}
          >
            <motion.div
              className="grid max-w-sm justify-items-center gap-5 text-center"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ ...SPRING.float, delay: 0.15 }}
            >
              <span className="relative grid size-28 place-items-center">
                <span className="absolute inset-0 rounded-full shadow-[inset_0_0_0_1px_var(--hairline-strong)]" />
                <Mark className="size-14 text-steel-500" mono />
              </span>
              <p className="caps flex items-center gap-2 text-steel-500">
                {signal === "shutdown" ? (
                  <Power aria-hidden="true" className="size-3.5" />
                ) : (
                  <ShieldOff aria-hidden="true" className="size-3.5" />
                )}
                {signal === "shutdown" ? "Powered down" : "Not this machine"}
              </p>
              <h2 className="font-serif text-4xl text-steel-50">
                {signal === "shutdown" ? "Orion is off" : "Not here"}
              </h2>
              <p className="text-sm text-steel-400">
                {signal === "shutdown"
                  ? "Start him again from the app menu or with “orion start”; this page comes back by itself."
                  : "He answers pages opened on his own machine, or on a device paired with it."}
              </p>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}
