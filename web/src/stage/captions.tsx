/**
 * His captions, the ambient view of the conversation (the folded column, above the limb): the sentence he is
 * speaking, word by word, in Instrument Serif italic; your words for a moment when you speak to him, in Betelgeuse's
 * orange; otherwise his last reply for a while after he gave it, then nothing but the sky. The words fade and rise
 * into place (a still fade with reduced motion); a screen reader hears nothing here (the conversation says it all).
 */
import { AnimatePresence, motion } from "motion/react"
import { Fragment, useEffect, useMemo, useState } from "react"
import { useShallow } from "zustand/react/shallow"
import { DUR, EXPO } from "@/lib/motion"
import { usePrefs } from "@/lib/prefs"
import { type Live, useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { plainText } from "./markdown"

/** How long a spoken sentence stays after it starts, and your words after you say them. */
const SPOKEN_MS = 14_000
const YOURS_MS = 6_000
/** How long his last reply stays on the sky once he gave it. */
const REPLY_MS = 120_000
/** The longest reply shown: the rest is in the conversation. */
const REPLY_CHARS = 240
/** A spoken word's pace (a voice says about three a second), and a written one's. */
const SPOKEN_WORD_S = 0.28
const WRITTEN_WORD_S = 0.022

interface Shown {
  key: string
  text: string
  who: "orion" | "you"
  pace: number // seconds between two words
}

/** His last reply (not an alert), if any. */
const lastReply = (s: Live) => s.messages.findLast((m) => m.role === "assistant" && m.source !== "announcement")

/**
 * What the captions show now.
 * @param s the live state's slice
 * @param now the time (ms)
 * @returns the words, or null
 */
function shownNow(
  s: Pick<Live, "caption" | "state" | "transcript"> & {
    reply?: { id: string; text: string; at: number; streaming?: boolean }
  },
  now: number,
): Shown | null {
  const { caption, transcript, reply } = s
  if (caption?.who === "orion" && (s.state === "speaking" || now - caption.at < SPOKEN_MS))
    return { key: `c${caption.at}`, text: caption.text, who: "orion", pace: SPOKEN_WORD_S }
  if (caption?.who === "you" && now - caption.at < YOURS_MS)
    return { key: `y${caption.at}`, text: caption.text, who: "you", pace: WRITTEN_WORD_S }
  if (transcript && transcript.purpose !== "answer" && now - transcript.at < YOURS_MS / 2)
    return { key: `t${transcript.at}`, text: transcript.text, who: "you", pace: WRITTEN_WORD_S }
  if (reply && !reply.streaming && reply.text && now - reply.at < REPLY_MS) {
    const plain = plainText(reply.text)
    const text = plain.length > REPLY_CHARS ? `${plain.slice(0, REPLY_CHARS - 1).replace(/\s+\S*$/, "")}…` : plain
    return { key: `r${reply.id}`, text, who: "orion", pace: WRITTEN_WORD_S }
  }
  return null
}

/**
 * The captions.
 * @param props classes (their place comes from the stage)
 * @returns the captions
 */
export function Captions({ className }: { className?: string }) {
  const on = usePrefs((p) => p.captions)
  // the reply is the store's own message object: it changes only when that message does
  const s = useLive(
    useShallow((l) => ({ caption: l.caption, state: l.state, transcript: l.transcript, reply: lastReply(l) })),
  )
  const [now, setNow] = useState(() => Date.now())
  const shown = on ? shownNow(s, now) : null
  // look again when what shows runs out (no ticking in between)
  useEffect(() => {
    const t = Date.now()
    const ends = [
      s.caption && s.caption.at + (s.caption.who === "orion" ? SPOKEN_MS : YOURS_MS),
      s.transcript && s.transcript.at + YOURS_MS / 2,
      s.reply && s.reply.at + REPLY_MS,
    ].filter((at): at is number => typeof at === "number" && at > t)
    setNow(t)
    if (!ends.length) return
    const timer = setTimeout(() => setNow(Date.now()), Math.min(...ends) - t + 20)
    return () => clearTimeout(timer)
  }, [s.caption, s.transcript, s.reply])

  return (
    <div
      aria-hidden="true"
      className={cn("pointer-events-none flex justify-center px-6", className)}
      data-testid="captions"
    >
      <AnimatePresence mode="wait">{shown && <Words key={shown.key} shown={shown} />}</AnimatePresence>
    </div>
  )
}

/**
 * One caption, its words arriving one by one.
 * @param props what is `shown`
 * @returns the line
 */
function Words({ shown }: { shown: Shown }) {
  const words = useMemo(() => shown.text.split(/\s+/).filter(Boolean), [shown.text])
  const yours = shown.who === "you"
  return (
    <motion.p
      initial="hidden"
      animate="shown"
      exit={{ opacity: 0, y: -6, transition: { duration: DUR.base } }}
      variants={{ shown: { transition: { staggerChildren: shown.pace } } }}
      className={cn(
        "max-w-[min(46rem,100%)] text-balance text-center [text-shadow:0_2px_18px_var(--color-void-950),0_0_2px_var(--color-void-950)]",
        yours
          ? "font-sans text-betel-200 text-lg"
          : "font-serif text-steel-50 text-xl italic leading-snug tab:text-2xl desk:text-[1.75rem] desk:leading-9",
      )}
    >
      {yours && <span className="caps mr-2.5 align-[3px] text-betel-400/80 not-italic">You</span>}
      {words.map((word, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: a word's place in its sentence is its identity
        <Fragment key={i}>
          <motion.span
            className="inline-block"
            variants={{
              hidden: { opacity: 0, y: 7 },
              shown: { opacity: 1, y: 0, transition: { duration: DUR.slow, ease: [...EXPO] } },
            }}
          >
            {word}
          </motion.span>{" "}
        </Fragment>
      ))}
    </motion.p>
  )
}
