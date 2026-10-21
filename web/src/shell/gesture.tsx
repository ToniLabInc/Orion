/**
 * Hand gestures, as he acts on them: when the camera on the Pi sees one (ORION hands, an MCP server; he gets the
 * gesture, never the video) and he does what it is bound to, a chip at the top of the screen says so for a moment: the
 * hand, the gesture's name, an arrow, and what it did ("Swipe right → Next track"), in red when he couldn't. A
 * gesture made while gestures sleep (one is bound to waking them) says so, quietly. A soft tick confirms it, since your
 * hands are away from the keyboard. A newer gesture replaces it; a repeat of the same one (a thumb held up, the volume
 * going on up) updates it in place, with how many times and, for the volume, a dial of its level.
 */
import { ArrowRight, Hand as HandIcon, Moon } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useState } from "react"
import { GESTURE_ICONS } from "@/lib/hands"
import { SPRING } from "@/lib/motion"
import { play } from "@/lib/sound"
import type { EventOf } from "@/lib/types"
import { cn } from "@/lib/utils"
import { onEvent } from "@/lib/ws"

const SHOW_MS = 2400

type Intent = EventOf<"intent">
type Chip = Intent & { seen: number; count: number }

const MEDIA: Record<string, string> = { play_pause: "Play / pause", next: "Next track", previous: "Previous track" }
const VOLUME: Record<string, string> = { up: "Volume up", down: "Volume down", mute: "Sound off" }

/**
 * What an intent did, in a few words: the server's own when it sent them (without the gesture it starts with, "Swipe
 * right → next track", which the chip shows on its own).
 * @param ev the intent event
 * @returns the words
 */
export function intentWords(ev: Pick<Intent, "intent" | "args" | "text">): string {
  const said = ev.text?.split("→").pop()?.trim()
  if (said) return said.charAt(0).toUpperCase() + said.slice(1)
  const args = ev.args ?? {}
  const panels = Array.isArray(args.panels) ? (args.panels as string[]) : []
  switch (ev.intent) {
    case "mic":
      return args.action === "mute" ? "Microphone off" : args.action === "unmute" ? "Microphone on" : "Microphone"
    case "spoken":
      return args.action === "off"
        ? "Spoken replies off"
        : args.action === "on"
          ? "Spoken replies on"
          : "Spoken replies"
    case "show":
      return panels.length ? `Show ${panels.join(", ")}` : "Show"
    case "hide":
      return panels.length ? `Hide ${panels.join(", ")}` : "Clear the stage"
    case "media":
      return MEDIA[String(args.action)] ?? "Media"
    case "volume":
      return VOLUME[String(args.action)] ?? "Volume"
    case "new_chat":
      return "New conversation"
    case "say":
      return typeof args.text === "string" ? `“${args.text}”` : "Say"
    case "toggle":
      return typeof args.panel === "string" ? `${args.panel} on / off` : "On / off"
    case "dismiss":
      return "Dismiss the newest panel"
    case "asleep":
      return "Gestures are asleep"
    default:
      return ev.intent.replaceAll("_", " ")
  }
}

/**
 * The chip once an intent came: a repeat of the showing it shows updates it in place and counts, anything else
 * replaces it.
 * @param before the chip shown until now
 * @param ev the intent
 * @param now performance.now()
 * @returns the chip to show
 */
export function chipAfter(before: Chip | null, ev: Intent, now: number): Chip {
  const repeat = before && ev.again && ev.showing === before.showing && ev.gesture === before.gesture
  return repeat ? { ...ev, seen: before.seen, count: before.count + 1 } : { ...ev, seen: now, count: 1 }
}

/**
 * The gesture chip, while there is a gesture to show.
 * @returns the chip
 */
export function GestureChip() {
  const [shown, setShown] = useState<Chip | null>(null)
  useEffect(
    () =>
      onEvent("intent", (ev) => {
        setShown((before) => chipAfter(before, ev, performance.now()))
        if (!ev.again && ev.intent !== "asleep") play("tick")
      }),
    [],
  )
  useEffect(() => {
    if (!shown) return
    const timer = setTimeout(() => setShown(null), SHOW_MS)
    return () => clearTimeout(timer)
  }, [shown])

  const asleep = shown?.intent === "asleep"
  const Hand = asleep ? Moon : (shown?.gesture && GESTURE_ICONS[shown.gesture]) || HandIcon
  const failed = shown?.ok === false && !asleep
  const level = shown?.intent === "volume" && !failed ? shown.result?.match(/(\d+)%/)?.[1] : undefined
  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed inset-x-0 top-[calc(var(--bar-h)+0.5rem)] z-[60] flex justify-center px-3"
    >
      <AnimatePresence>
        {shown && (
          <motion.div
            key={shown.seen}
            data-testid="gesture"
            data-intent={shown.intent}
            title={failed ? shown.result : undefined}
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, transition: { duration: 0.3 } }}
            transition={SPRING.snappy}
            className={cn(
              "glass-solid flex h-10 max-w-full items-center gap-2.5 rounded-full pr-4 pl-3 text-sm",
              failed
                ? "shadow-[var(--shadow-float),inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_45%,transparent)]"
                : "shadow-[var(--shadow-float),inset_0_0_0_1px_color-mix(in_oklch,var(--color-betel-400)_30%,transparent)]",
            )}
          >
            <Hand aria-hidden="true" className={cn("size-4 shrink-0", failed ? "text-alert-400" : "text-betel-300")} />
            {shown.gesture && (
              <span className="shrink-0 font-medium text-steel-100 capitalize">
                {shown.gesture.replaceAll("_", " ")}
              </span>
            )}
            <ArrowRight aria-hidden="true" className="size-3.5 shrink-0 text-steel-500" />
            <span className={cn("truncate", failed ? "text-alert-300" : asleep ? "text-steel-400" : "text-steel-100")}>
              {failed ? `Couldn't: ${intentWords(shown)}` : intentWords(shown)}
            </span>
            {level && <Dial value={Number(level)} />}
            {level && <span className="shrink-0 text-steel-50 tabular-nums">{level}%</span>}
            {shown.count > 1 && <span className="shrink-0 font-mono text-micro text-steel-500">×{shown.count}</span>}
            {Boolean(shown.awake) && !asleep && (
              <span className="shrink-0 text-micro text-steel-500">awake {Math.round(shown.awake ?? 0)} s</span>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/**
 * The volume's level as a dial, turning as a held thumb takes it up or down.
 * @param props the `value`, 0 to 100
 * @returns the dial
 */
function Dial({ value }: { value: number }) {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" className="size-4 shrink-0 -rotate-90" data-testid="gesture-dial">
      <circle cx="10" cy="10" r="7.5" strokeWidth="2.2" className="fill-none stroke-steel-50/12" />
      <circle
        cx="10"
        cy="10"
        r="7.5"
        strokeWidth="2.2"
        pathLength={1}
        strokeDasharray={`${Math.min(1, Math.max(0, value / 100))} 1`}
        className="fill-none stroke-betel-300 transition-[stroke-dasharray] duration-(--dur-base) ease-(--ease-expo)"
      />
    </svg>
  )
}
