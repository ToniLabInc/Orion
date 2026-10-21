/**
 * The talk button and the voice's place. The talk button is this browser's microphone: the first press takes his
 * voice to this device (the microphone's permission is asked then), and from then on a tap, or a press held, asks him
 * to listen now, without his name. It sits on the world's limb: Horizon's airglow runs through its middle, and your
 * voice plucks it there (lib/core.ts, useHorizon). A ring round it follows your voice's level as you speak (written
 * straight from the microphone, never through React), and breathes while he listens. Where the HTTPS a microphone
 * needs is missing it says so instead.
 *
 * The voice chip says which device carries his voice (this one, another, or his own machine), and opens to take it,
 * give it back, mute his machine's microphone and switch his spoken replies.
 */
import { Mic, MicOff, Speaker, Volume2, VolumeOff } from "lucide-react"
import { useEffect, useRef } from "react"
import { useShallow } from "zustand/react/shallow"
import { useHorizon } from "@/lib/core"
import {
  explainVoice,
  onMicLevel,
  pressTalk,
  releaseVoice,
  takeVoice,
  useCarrying,
  useDevice,
  voiceSupport,
} from "@/lib/device"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { setMuted, setVoice } from "@/lib/ws"
import { Button } from "@/ui/button"
import { Popover } from "@/ui/menu"
import { Switch } from "@/ui/switch"
import { Tooltip } from "@/ui/tooltip"

/**
 * The talk button.
 * @param props whether it is the phone's `big` one, and classes
 * @returns the button
 */
export function TalkButton({ big = false, className }: { big?: boolean; className?: string }) {
  const carrying = useCarrying()
  const taking = useDevice((s) => s.taking)
  const { listening, online } = useLive(
    useShallow((s) => ({ listening: s.state === "listening", online: s.link === "online" })),
  )
  const ring = useRef<HTMLSpanElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const support = voiceSupport()
  useHorizon(button)

  // your voice's level, straight onto the ring (a CSS variable: no render)
  useEffect(() => {
    let quiet = 0
    return onMicLevel((value) => {
      const el = ring.current
      if (!el) return
      el.style.setProperty("--level", Math.min(1, value * 1.6).toFixed(3))
      clearTimeout(quiet)
      quiet = window.setTimeout(() => el.style.setProperty("--level", "0"), 300)
    })
  }, [])

  // one click: take the voice if this tab doesn't carry it yet (inside the click, nothing awaited before: a phone
  // opens its audio only in the gesture), and ask him to listen now
  const press = () => {
    if (!online) return
    if (!support.ok) explainVoice(support.why)
    else pressTalk()
  }
  const label = !support.ok
    ? support.why
    : !online
      ? "Offline: reconnecting"
      : carrying
        ? "Talk to him now (or say “Orion”)"
        : "Talk from this device (it takes his voice here)"
  const size = big ? "size-16" : "size-11"
  return (
    <Tooltip
      content={label}
      side="top"
      render={
        <button
          ref={button}
          type="button"
          aria-label={label}
          aria-pressed={carrying}
          aria-disabled={!support.ok || !online || undefined}
          onClick={press}
          data-testid="talk"
          data-carrying={carrying || undefined}
          data-listening={(carrying && listening) || undefined}
          className={cn(
            "group/talk relative grid shrink-0 place-items-center rounded-full outline-none transition-[background-color,box-shadow,color] duration-(--dur-quick) focus-visible:outline-2 focus-visible:outline-offset-3 focus-visible:outline-rigel-400",
            size,
            carrying
              ? "bg-betel-400 text-void-950 shadow-[inset_0_1px_0_0_oklch(1_0_0/0.35),0_10px_30px_-10px_var(--color-betel-400)] hover:bg-betel-300 active:bg-betel-500"
              : "glass text-steel-100 hover:text-steel-50",
            (!support.ok || !online) && "cursor-not-allowed opacity-45",
            className,
          )}
        />
      }
    >
      {/* the level ring: grows and brightens with your voice; breathes while he listens to you */}
      <span
        ref={ring}
        aria-hidden="true"
        className="pointer-events-none absolute -inset-1 rounded-full shadow-[0_0_0_2px_var(--color-betel-400)] opacity-[calc(var(--level,0)*1.4)] transition-[transform,opacity] duration-75 ease-out [transform:scale(calc(1+var(--level,0)*0.45))] group-data-listening/talk:animate-breathe group-data-listening/talk:opacity-70"
      />
      {taking ? (
        <span
          aria-hidden="true"
          className="size-5 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      ) : support.ok ? (
        <Mic aria-hidden="true" className={big ? "size-7" : "size-5"} strokeWidth={carrying ? 2 : 1.5} />
      ) : (
        <MicOff aria-hidden="true" className={big ? "size-7" : "size-5"} />
      )}
    </Tooltip>
  )
}

/**
 * Where his voice is, and its controls.
 * @returns the chip
 */
export function VoiceChip() {
  const { holder, surface, muted, voice, voiceAvailable, online } = useLive(
    useShallow((s) => ({
      holder: s.voiceHolder,
      surface: s.surface,
      muted: s.muted,
      voice: s.voice,
      voiceAvailable: s.voiceAvailable,
      online: s.link === "online",
    })),
  )
  const carrying = useCarrying()
  const taking = useDevice((s) => s.taking)
  const support = voiceSupport()
  const where = carrying ? "this device" : holder.device ? holder.name || "another device" : "his machine"
  return (
    <Popover
      side="top"
      align="end"
      className="w-72"
      trigger={
        <button
          type="button"
          className="flex h-6 max-w-48 items-center gap-1.5 rounded-full px-2 text-micro text-steel-400 outline-none transition-colors hover:bg-steel-50/6 hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400"
          data-testid="voice-chip"
        />
      }
      label={
        <>
          <Speaker
            aria-hidden="true"
            className={cn("size-3 shrink-0", carrying ? "text-betel-400" : "text-steel-500")}
          />
          <span className="truncate">Voice on {where}</span>
          {!voice && <VolumeOff aria-label="spoken replies off" className="size-3 shrink-0 text-steel-500" />}
        </>
      }
    >
      <div className="grid gap-3">
        <div>
          <p className="caps text-steel-500">His voice</p>
          <p className="mt-1 text-sm text-steel-100">
            {carrying
              ? "This device carries it: its microphone hears you, its speakers play him."
              : holder.device
                ? `${holder.name || "Another device"} carries it.`
                : "His own machine's microphone and speakers carry it."}
          </p>
        </div>
        {carrying ? (
          <Button size="sm" onClick={releaseVoice}>
            Give it back
          </Button>
        ) : (
          <Button
            size="sm"
            tone="you"
            disabled={!support.ok || !online || taking}
            onClick={() => void takeVoice()}
            title={support.ok ? undefined : support.why}
          >
            <Mic aria-hidden="true" />
            Take it to this device
          </Button>
        )}
        {!support.ok && <p className="text-xs text-steel-500">{support.why}.</p>}
        <div className="grid gap-2.5 border-t pt-3">
          {/* biome-ignore lint/a11y/noLabelWithoutControl: the switch inside is the control */}
          <label className="flex items-center justify-between gap-3 text-sm text-steel-200">
            <span className="flex items-center gap-2">
              {voice ? (
                <Volume2 aria-hidden="true" className="size-4" />
              ) : (
                <VolumeOff aria-hidden="true" className="size-4" />
              )}
              Spoken replies
            </span>
            <Switch checked={voice} disabled={!online} onCheckedChange={(on) => setVoice(on)} />
          </label>
          {voiceAvailable && (
            // biome-ignore lint/a11y/noLabelWithoutControl: the switch inside is the control
            <label className="flex items-center justify-between gap-3 text-sm text-steel-200">
              <span className="flex items-center gap-2">
                {muted ? (
                  <MicOff aria-hidden="true" className="size-4" />
                ) : (
                  <Mic aria-hidden="true" className="size-4" />
                )}
                His machine's microphone
              </span>
              <Switch checked={!muted} disabled={!online} onCheckedChange={(on) => setMuted(!on)} />
            </label>
          )}
        </div>
        {surface === "" && online && (
          <p className="text-xs text-steel-500">This Orion doesn't let a browser carry his voice yet.</p>
        )}
      </div>
    </Popover>
  )
}
