/**
 * The composer: the talk button, then a glass field that grows with what you write (Enter sends, Shift+Enter breaks the
 * line), with the pictures you paste, drop or attach as chips (each sized down in the browser before it is sent), the
 * buttons to share your screen or camera and to attach a picture, and Send, or Stop while he thinks or speaks. Under
 * it, his brain (with a "stand-in" badge and its reason while another brain stands in) and where his voice is.
 *
 * Typing anywhere on the stage lands here (`typeInto`); sending opens the conversation.
 */
import { ArrowUp, Camera, ImagePlus, MonitorUp, Paperclip, Square, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { type ClipboardEvent, type DragEvent, type FormEvent, useEffect, useRef, useState } from "react"
import { useLocation } from "wouter"
import { useShallow } from "zustand/react/shallow"
import type { Shared } from "@/device"
import { share, stopShare, useDevice } from "@/lib/device"
import { POP, SPRING } from "@/lib/motion"
import { setOpen, setPalette } from "@/lib/stage"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { deviceLink, say, stop } from "@/lib/ws"
import { Badge } from "@/ui/badge"
import { IconButton } from "@/ui/button"
import { toast } from "@/ui/toast"
import { Tooltip } from "@/ui/tooltip"
import { compress, isPicture } from "./attachments"
import { TalkButton, VoiceChip } from "./talk"

// ---------- what you type anywhere on the stage lands here ----------
let typed: string | null = null // waiting for the field (null: nothing asked)
const takers = new Set<() => void>()

/**
 * Type into the composer and put the focus there (until its field is there, the text waits for it).
 * @param text what was typed ("" to put the focus there only)
 */
export function typeInto(text: string): void {
  typed = (typed ?? "") + text
  for (const take of takers) take()
}

interface Attachment {
  id: number
  url: string // a JPEG data URL, sized down
}

let attachmentIds = 0

/**
 * The composer.
 * @param props whether it is the phone's (a big talk button), and classes
 * @returns the composer
 */
export function Composer({ phone = false, className }: { phone?: boolean; className?: string }) {
  const { state, online, honorific, limit, maxPictures } = useLive(
    useShallow((s) => ({
      state: s.state,
      online: s.link === "online",
      honorific: s.honorific,
      limit: s.hud?.max_message_chars,
      maxPictures: s.hud?.max_pictures,
    })),
  )
  const [text, setText] = useState("")
  const [pictures, setPictures] = useState<Attachment[]>([])
  const [dragging, setDragging] = useState(false)
  const field = useRef<HTMLTextAreaElement>(null)
  const file = useRef<HTMLInputElement>(null)
  const loading = state === "loading"
  const working = state === "thinking" || state === "speaking"
  const room = maxPictures === undefined ? Number.POSITIVE_INFINITY : maxPictures - pictures.length

  useEffect(() => {
    const take = () => {
      if (typed === null || !field.current) return
      const more = typed
      typed = null
      if (more) setText((before) => before + more)
      field.current.focus()
    }
    takers.add(take)
    take()
    return () => {
      takers.delete(take)
    }
  }, [])

  const addPictures = async (files: Iterable<File>) => {
    const chosen = [...files].filter(isPicture)
    if (!chosen.length) return
    const kept = chosen.slice(0, Math.max(0, room))
    if (kept.length < chosen.length)
      toast.add({
        title: "Too many pictures",
        description: `A message carries ${maxPictures} at most.`,
        type: "warning",
      })
    for (const picture of kept) {
      try {
        const url = await compress(picture)
        setPictures((before) => [...before, { id: ++attachmentIds, url }])
      } catch (error) {
        toast.add({
          title: "That picture can't be read",
          description: error instanceof Error ? error.message : String(error),
          type: "warning",
        })
      }
    }
  }

  const send = (e?: FormEvent) => {
    e?.preventDefault()
    const words = text.trim()
    if ((!words && !pictures.length) || !online || loading) return
    if (
      say(
        words,
        pictures.map((p) => p.url),
      )
    ) {
      setText("")
      setPictures([])
      setOpen(true)
    }
  }

  const onPaste = (e: ClipboardEvent) => {
    const files = [...e.clipboardData.files].filter(isPicture)
    if (!files.length) return
    e.preventDefault()
    void addPictures(files)
  }
  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    void addPictures(e.dataTransfer.files)
  }

  const placeholder = !online
    ? "Offline, reconnecting…"
    : loading
      ? "Waking his brain, one moment…"
      : `Talk to Orion${honorific ? `, ${honorific}` : ""}…`
  const canSend = (text.trim().length > 0 || pictures.length > 0) && online && !loading

  return (
    <div className={cn("grid grid-cols-1 gap-1.5", className)} data-composer="">
      <div className={cn("flex items-end gap-2.5", phone && "gap-2")}>
        <TalkButton big={phone} className="mb-0.5" />
        <form
          onSubmit={send}
          onDragOver={(e) => {
            if ([...e.dataTransfer.items].some((item) => item.type.startsWith("image/"))) {
              e.preventDefault()
              setDragging(true)
            }
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            "glass relative flex min-w-0 flex-1 flex-col rounded-card transition-shadow duration-(--dur-quick) focus-within:shadow-[var(--shadow-elevated),inset_0_1px_0_0_var(--highlight),inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_40%,transparent)]",
            dragging && "shadow-[var(--shadow-elevated),inset_0_0_0_1.5px_var(--color-rigel-400)]",
          )}
          data-testid="composer"
        >
          <AnimatePresence initial={false}>
            {pictures.length > 0 && (
              <motion.ul
                className="flex flex-wrap gap-2 px-3 pt-3"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                aria-label="Pictures to send"
              >
                <AnimatePresence initial={false}>
                  {pictures.map((p, i) => (
                    <motion.li key={p.id} {...POP} layout className="relative">
                      <img
                        src={p.url}
                        alt={`To send, ${i + 1} of ${pictures.length}`}
                        className="size-14 rounded-control object-cover shadow-[inset_0_0_0_1px_var(--hairline-strong)]"
                      />
                      <button
                        type="button"
                        aria-label={`Leave picture ${i + 1} out`}
                        onClick={() => setPictures((before) => before.filter((x) => x.id !== p.id))}
                        className="glass-solid absolute -top-1.5 -right-1.5 grid size-5 place-items-center rounded-full text-steel-200 outline-none hover:text-steel-50 focus-visible:outline-2 focus-visible:outline-rigel-400"
                      >
                        <X aria-hidden="true" className="size-3" />
                      </button>
                    </motion.li>
                  ))}
                </AnimatePresence>
              </motion.ul>
            )}
          </AnimatePresence>
          <textarea
            ref={field}
            value={text}
            rows={1}
            onChange={(e) => setText(e.target.value)}
            onPaste={onPaste}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                send()
              } else if (e.key === "/" && !text) {
                e.preventDefault()
                setPalette(true) // a slash on an empty line: the palette
              } else if (e.key === "Escape" && !text) e.currentTarget.blur()
            }}
            maxLength={limit}
            placeholder={placeholder}
            disabled={!online || loading}
            aria-label="Message"
            className="field-sizing-content max-h-48 min-h-11 w-full resize-none bg-transparent px-4 pt-3 pb-1 text-base text-steel-50 outline-none placeholder:text-steel-500 disabled:cursor-not-allowed"
            data-testid="composer-field"
          />
          <div className="flex items-center gap-0.5 px-2 pb-2">
            <input
              ref={file}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => {
                if (e.target.files) void addPictures(e.target.files)
                e.target.value = ""
              }}
            />
            <IconButton
              label="Attach a picture"
              size="sm"
              disabled={!online || room <= 0}
              onClick={() => file.current?.click()}
            >
              {pictures.length ? <ImagePlus /> : <Paperclip />}
            </IconButton>
            <ShareButton what="screen" />
            <ShareButton what="camera" />
            <span className="ml-auto" />
            {working && (
              <IconButton
                label="Stop him"
                shortcut="Esc"
                size="sm"
                tone="secondary"
                onClick={() => stop()}
                data-testid="stop"
              >
                <Square className="fill-current" />
              </IconButton>
            )}
            <motion.span
              animate={{ scale: canSend ? 1 : 0.9, opacity: canSend ? 1 : 0.55 }}
              transition={SPRING.snappy}
              className="ml-1 inline-flex"
            >
              <IconButton
                label="Send"
                shortcut="↵"
                size="sm"
                tone={canSend ? "primary" : "secondary"}
                type="submit"
                disabled={!canSend}
                className="rounded-full"
                data-testid="send"
              >
                <ArrowUp strokeWidth={2.2} />
              </IconButton>
            </motion.span>
          </div>
        </form>
      </div>
      <Meta className={phone ? "px-1" : "pl-14"} />
    </div>
  )
}

/**
 * Share the screen or the camera (or stop): browsers ask you which one, and only after a click.
 * @param props `what` it shares
 * @returns the button
 */
function ShareButton({ what }: { what: Shared }) {
  const sharing = useDevice((s) => s.sharing.includes(what))
  const online = useLive((s) => s.link === "online")
  const can = deviceLink().caps().includes(what)
  const Icon = what === "screen" ? MonitorUp : Camera
  const label = sharing
    ? `Stop sharing your ${what}`
    : can
      ? `Share your ${what} with him`
      : what === "screen"
        ? "This browser can't share its screen"
        : "This browser can't share a camera"
  return (
    <IconButton
      label={label}
      size="sm"
      aria-pressed={sharing}
      disabled={(!can || !online) && !sharing}
      onClick={() => (sharing ? stopShare(what) : void share(what))}
      className={cn(sharing && "text-betel-300 aria-pressed:bg-betel-400/14 aria-pressed:text-betel-200")}
      data-testid={`share-${what}`}
    >
      <Icon />
    </IconButton>
  )
}

/**
 * Under the composer: his brain (and a stand-in's badge), where his voice is.
 * @param props classes
 * @returns the line
 */
function Meta({ className }: { className?: string }) {
  const models = useLive((s) => s.capabilities?.models)
  const [, navigate] = useLocation()
  return (
    <div className={cn("flex min-h-6 items-center gap-2 text-micro text-steel-500", className)}>
      {models && (
        <button
          type="button"
          onClick={() => navigate("/settings/brain")}
          className="flex min-w-0 items-center gap-1.5 rounded-full px-1.5 py-0.5 outline-none transition-colors hover:text-steel-200 focus-visible:outline-2 focus-visible:outline-rigel-400"
          title="His brain (Settings, Brain)"
        >
          <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-(--state)" />
          <span className="truncate">Thinking with {models.llm || models.provider}</span>
        </button>
      )}
      {models?.standing_in && (
        <Tooltip
          content={models.reason ? `The main brain can't answer: ${models.reason}` : "The main brain can't answer now"}
          render={<span className="shrink-0" />}
        >
          <Badge tone="dusk" dot>
            Stand-in
          </Badge>
        </Tooltip>
      )}
      <span className="ml-auto" />
      <VoiceChip />
    </div>
  )
}
