/**
 * The stage: Horizon behind everything (the shell draws it: the sky, and the night side of the world along the
 * bottom), and over it the conversation (folded to his captions when nobody types), the cards on the right, and the
 * composer sitting on the world's limb, its talk button where your voice plucks the airglow. A tablet opens the cards
 * as a sheet from the right; a phone has the conversation over the whole sky and the cards as a bottom sheet.
 *
 * Its keys: typing anywhere lands in the composer ("/" only puts the focus there); Esc stops him while he works, else
 * sends the newest card away, else folds the conversation.
 */
import { Layers } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { useEffect, useState } from "react"
import { useShallow } from "zustand/react/shallow"
import { fieldOf } from "@/lib/hooks"
import { POP } from "@/lib/motion"
import { OVERLAYS } from "@/lib/nav"
import { dismissNewest, setOpen, setSheet, useStage } from "@/lib/stage"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { onEvent, stop } from "@/lib/ws"
import { Sheet } from "@/ui/sheet"
import { toast } from "@/ui/toast"
import { CardStack } from "./cards"
import { Composer, typeInto } from "./composer"
import { Conversation } from "./conversation"
import { SharingChips } from "./sharing"

export type Layout = "phone" | "tablet" | "desktop"

const TABLET = "(width >= 43.75rem)"
const DESKTOP = "(width >= 68.75rem)"

/**
 * The layout for this screen's width.
 * @returns phone, tablet or desktop
 */
const readLayout = (): Layout =>
  matchMedia(DESKTOP).matches ? "desktop" : matchMedia(TABLET).matches ? "tablet" : "phone"

/**
 * The layout for this screen's width, followed as it changes.
 * @returns phone, tablet or desktop
 */
export function useLayout(): Layout {
  const [layout, setLayout] = useState<Layout>(readLayout)
  useEffect(() => {
    const queries = [matchMedia(DESKTOP), matchMedia(TABLET)]
    const change = () => setLayout(readLayout())
    for (const q of queries) q.addEventListener("change", change)
    return () => {
      for (const q of queries) q.removeEventListener("change", change)
    }
  }, [])
  return layout
}

/**
 * The stage page.
 * @returns the stage
 */
export default function Stage() {
  const layout = useLayout()
  useStageKeys()
  useAnnouncements()
  return (
    <div className="absolute inset-0" data-testid="stage" data-layout={layout}>
      {layout === "desktop" ? <Desktop /> : layout === "tablet" ? <Tablet /> : <Phone />}
    </div>
  )
}

/** The composer, and what you share just above it. */
function Dock({ phone = false, className }: { phone?: boolean; className?: string }) {
  return (
    <div className={cn("relative grid gap-2.5", className)}>
      <SharingChips />
      <Composer phone={phone} />
    </div>
  )
}

/**
 * The desktop: the conversation and the composer in the middle, the cards on the right (their column keeps its room;
 * the conversation's is centred on the screen whenever there is room for both).
 * @returns the layout
 */
function Desktop() {
  return (
    <div className="relative z-20 mx-auto grid h-full max-w-[1880px] grid-cols-[minmax(0,1fr)_minmax(0,720px)_minmax(380px,1fr)] grid-rows-[minmax(0,1fr)_auto] gap-x-6 px-6 pt-[calc(var(--bar-h)+0.75rem)] pb-(--world)">
      <Conversation className="col-start-2 row-start-1 min-h-0" />
      <CardStack className="col-start-3 row-span-2 row-start-1 w-[380px] justify-self-end pb-1" />
      <Dock className="col-start-2 row-start-2 -mx-5 pt-3" />
    </div>
  )
}

/**
 * A tablet: the conversation and the composer; the cards in a sheet from the right.
 * @returns the layout
 */
function Tablet() {
  const { sheet, count } = useStage(useShallow((s) => ({ sheet: s.sheet, count: s.cards.length })))
  return (
    <>
      <div className="relative z-20 grid h-full grid-cols-[3.5rem_minmax(0,1fr)_3.5rem] grid-rows-[minmax(0,1fr)_auto] gap-x-4 px-4 pt-[calc(var(--bar-h)+0.75rem)] pb-(--world)">
        <Conversation className="col-start-2 row-start-1 mx-auto min-h-0 w-full max-w-[720px]" />
        <Dock className="col-start-2 row-start-2 mx-auto w-full max-w-[760px] pt-3" />
        <div className="col-start-3 row-span-2 row-start-1 self-start">
          <CardsButton count={count} />
        </div>
      </div>
      <Sheet open={sheet === "cards"} onOpenChange={(o) => setSheet(o ? "cards" : null)} side="right" title="Cards">
        <CardStack inSheet className="pb-2" />
      </Sheet>
    </>
  )
}

/**
 * A phone: the conversation over the sky, the cards' chip when there are any, and the composer with its big talk
 * button; the cards open as a bottom sheet.
 * @returns the layout
 */
function Phone() {
  const { sheet, count } = useStage(useShallow((s) => ({ sheet: s.sheet, count: s.cards.length })))
  return (
    <>
      <div className="relative z-20 flex h-full flex-col px-3 pt-[calc(var(--bar-h)+0.25rem)] pb-[calc(var(--world)+var(--safe-b))]">
        <Conversation className="min-h-0 flex-1" />
        <AnimatePresence initial={false}>
          {count > 0 && (
            <motion.div key="cards" {...POP} className="flex justify-center py-2">
              <CardsButton count={count} wide />
            </motion.div>
          )}
        </AnimatePresence>
        <Dock phone />
      </div>
      <Sheet open={sheet === "cards"} onOpenChange={(o) => setSheet(o ? "cards" : null)} title="Cards">
        <CardStack inSheet />
      </Sheet>
    </>
  )
}

/**
 * The button that opens the cards' sheet, with how many there are.
 * @param props the `count`, and whether it is `wide` (a chip with its word)
 * @returns the button
 */
function CardsButton({ count, wide = false }: { count: number; wide?: boolean }) {
  return (
    <button
      type="button"
      onClick={() => setSheet("cards")}
      aria-label={`Cards${count ? ` (${count})` : ""}`}
      className={cn(
        "glass relative flex items-center gap-2 text-steel-200 outline-none transition-colors hover:text-steel-50 focus-visible:outline-2 focus-visible:outline-rigel-400 [&_svg]:size-4",
        wide ? "h-9 rounded-full px-4 text-sm" : "size-12 justify-center rounded-card",
      )}
      data-testid="sheet-cards"
    >
      <Layers aria-hidden="true" />
      {wide && "Cards"}
      {count > 0 && (
        <span
          className={cn(
            "grid h-4 min-w-4 place-items-center rounded-full bg-rigel-400 px-1 font-mono text-micro text-void-950 leading-none tabular-nums",
            !wide && "absolute -top-1 -right-1",
          )}
        >
          {count}
        </span>
      )}
    </button>
  )
}

/** The stage's keys (see the module docstring). */
function useStageKeys() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return
      if (document.querySelector(OVERLAYS)) return
      const target = e.target as HTMLElement | null
      const field = fieldOf(target)
      if (e.key === "Escape") {
        const { state } = useLive.getState()
        if (state === "thinking" || state === "speaking") stop()
        else if (field) field.blur()
        else if (!dismissNewest() && useStage.getState().open) setOpen(false)
        return
      }
      // a character typed on a button or a link goes to the composer too; lists and menus keep theirs (type-ahead)
      if (field || target?.closest?.("[role=listbox], [role=combobox], [role=menu], [role=tree], [role=grid]")) return
      if (e.key.length !== 1 || e.key === " ") return
      e.preventDefault()
      typeInto(e.key === "/" ? "" : e.key)
      setOpen(true)
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  }, [])
}

/** An alert he gives (a timer due, a reminder) shows in the conversation; while it is folded, a toast says it too. */
function useAnnouncements() {
  useEffect(
    () =>
      onEvent("announcement", (ev) => {
        if (!useStage.getState().open) toast.add({ title: ev.title, description: ev.text, type: "info", timeout: 8000 })
      }),
    [],
  )
}
