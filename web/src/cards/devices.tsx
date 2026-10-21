/**
 * Devices: where he hears and speaks. Every HUD connected now (this one marked), what each offers, which carries his
 * voice (in your orange) and what each shares; his own machine first, as what carries the voice when no browser does.
 * This device can take the voice or give it back from here.
 */
import { Camera, Clipboard, Laptop, Mic, Monitor, MonitorUp, Server, Smartphone, Tablet, Volume2 } from "lucide-react"
import { motion } from "motion/react"
import { useEffect } from "react"
import { useShallow } from "zustand/react/shallow"
import { releaseVoice, takeVoice, useCarrying, useDevice, voiceSupport } from "@/lib/device"
import { RISE } from "@/lib/motion"
import { useLive } from "@/lib/store"
import type { DeviceCap, Surface } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import type { CardProps } from "./card"

const CAP_ICONS: Record<DeviceCap, typeof Mic> = {
  mic: Mic,
  speaker: Volume2,
  screen: MonitorUp,
  camera: Camera,
  clipboard: Clipboard,
}

/**
 * A device's icon, from its name.
 * @param name "Safari on iPhone", "Firefox on Linux"
 * @returns the icon
 */
export function deviceIcon(name: string): typeof Mic {
  if (/iphone|android.*phone|phone/i.test(name)) return Smartphone
  if (/ipad|tablet/i.test(name)) return Tablet
  if (/mac|windows|chromebook/i.test(name)) return Laptop
  return Monitor
}

/**
 * The devices card.
 * @param props the card's
 * @returns the body
 */
export default function Devices({ setNote }: CardProps) {
  const { surfaces, surface, holder, host } = useLive(
    useShallow((s) => ({ surfaces: s.surfaces, surface: s.surface, holder: s.voiceHolder, host: s.host })),
  )
  const carrying = useCarrying()
  const taking = useDevice((s) => s.taking)
  const support = voiceSupport()
  useEffect(() => setNote(`${surfaces.length || 1} connected`), [surfaces.length, setNote])
  const mine = surfaces.find((s) => s.id === surface)
  const others = surfaces.filter((s) => s.id !== surface)
  return (
    <div className="grid gap-1.5 px-3 pb-3" data-testid="widget-devices">
      <Row
        icon={Server}
        name="His own machine"
        detail={
          host ? [host.platform, host.session, host.container ? "in a container" : ""].filter(Boolean).join(" · ") : ""
        }
        voice={!holder.device}
      />
      {mine ? <Row icon={deviceIcon(mine.device)} name={mine.device} device={mine} voice={carrying} here /> : null}
      {others.map((s) => (
        <Row key={s.id} icon={deviceIcon(s.device)} name={s.device} device={s} voice={holder.device === s.id} />
      ))}
      <div className="mt-1.5 flex items-center gap-2 border-t pt-3">
        {carrying ? (
          <Button size="sm" onClick={releaseVoice}>
            Give his voice back
          </Button>
        ) : (
          <Button size="sm" tone="you" disabled={!support.ok || taking} onClick={() => void takeVoice()}>
            <Mic aria-hidden="true" />
            Take his voice here
          </Button>
        )}
        {!support.ok && <span className="text-xs text-steel-500">{support.why}</span>}
      </div>
    </div>
  )
}

/**
 * One device.
 * @param props its `icon` and `name`, a `detail`, the HUD (`device`), whether it carries the `voice`, and whether it
 *   is `here`
 * @returns the row
 */
function Row({
  icon: Icon,
  name,
  detail,
  device,
  voice,
  here,
}: {
  icon: typeof Mic
  name: string
  detail?: string
  device?: Surface
  voice: boolean
  here?: boolean
}) {
  return (
    <motion.div
      {...RISE}
      className={cn(
        "flex items-center gap-3 rounded-control px-2.5 py-2",
        voice && "bg-betel-400/8 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-betel-400)_30%,transparent)]",
      )}
    >
      <span
        className={cn(
          "grid size-8 shrink-0 place-items-center rounded-full bg-steel-50/6",
          voice ? "text-betel-300" : "text-steel-300",
        )}
      >
        <Icon aria-hidden="true" className="size-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-sm text-steel-100">{name}</span>
          {here && <Badge tone="rigel">This one</Badge>}
        </span>
        <span className="flex items-center gap-1.5 text-micro text-steel-500">
          {device ? (
            device.caps.map((cap) => {
              const CapIcon = CAP_ICONS[cap]
              return <CapIcon key={cap} aria-label={cap} className="size-3" />
            })
          ) : (
            <span className="truncate">{detail}</span>
          )}
          {device?.sharing.length ? (
            <span className="text-alert-300">· sharing {device.sharing.join(", ")}</span>
          ) : null}
        </span>
      </span>
      {voice && (
        <Badge tone="betel" dot>
          Voice
        </Badge>
      )}
    </motion.div>
  )
}
