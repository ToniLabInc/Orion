/**
 * Eyes: what you share with him. While this browser shares its screen or camera, its live picture, large, with a way to
 * stop; otherwise the two ways to start (your browser asks what to share). Other devices sharing now are listed: he can
 * look through them too.
 */
import { Camera, MonitorUp, ScanEye, Square } from "lucide-react"
import { useEffect, useRef } from "react"
import type { Shared } from "@/device"
import { share, sharedStream, stopShare, useDevice } from "@/lib/device"
import { useLive } from "@/lib/store"
import { deviceLink } from "@/lib/ws"
import { Button } from "@/ui/button"
import type { CardProps } from "./card"

/**
 * The eyes card.
 * @param props the card's
 * @returns the body
 */
export default function Eyes({ setNote }: CardProps) {
  const sharing = useDevice((s) => s.sharing)
  const surfaces = useLive((s) => s.surfaces)
  const surface = useLive((s) => s.surface)
  const others = surfaces.filter((x) => x.id !== surface && x.sharing.length > 0)
  const caps = deviceLink().caps()
  useEffect(() => setNote(sharing.length ? "Sharing" : null), [sharing.length, setNote])
  return (
    <div className="grid gap-3 px-3 pb-3" data-testid="widget-eyes">
      {sharing.length ? (
        sharing.map((what) => <Preview key={what} what={what} />)
      ) : (
        <div className="grid justify-items-center gap-3 rounded-control bg-void-950/40 px-4 py-6 text-center shadow-[inset_0_0_0_1px_var(--hairline)]">
          <ScanEye aria-hidden="true" className="size-6 text-steel-500" />
          <p className="max-w-64 text-sm text-steel-300">
            Share your screen or camera and he can look at it when you ask him to.
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            <Button size="sm" disabled={!caps.includes("screen")} onClick={() => void share("screen")}>
              <MonitorUp aria-hidden="true" />
              Share screen
            </Button>
            <Button size="sm" disabled={!caps.includes("camera")} onClick={() => void share("camera")}>
              <Camera aria-hidden="true" />
              Share camera
            </Button>
          </div>
          {!caps.includes("screen") && !caps.includes("camera") && (
            <p className="text-xs text-steel-500">This browser can't share a screen or a camera.</p>
          )}
        </div>
      )}
      {others.length > 0 && (
        <ul className="grid gap-1">
          {others.map((s) => (
            <li key={s.id} className="flex items-center gap-2 rounded-control px-2 py-1.5 text-xs text-steel-300">
              <span aria-hidden="true" className="size-1.5 animate-breathe rounded-full bg-alert-400" />
              {s.device} shares its {s.sharing.join(" and ")}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * The live picture of what this browser shares.
 * @param props `what` it shares
 * @returns the preview
 */
function Preview({ what }: { what: Shared }) {
  const video = useRef<HTMLVideoElement>(null)
  useEffect(() => {
    const el = video.current
    const stream = sharedStream(what)
    if (!el || !stream) return
    el.srcObject = stream
    void el.play().catch(() => {})
    return () => {
      el.srcObject = null
    }
  }, [what])
  return (
    <figure className="relative overflow-hidden rounded-control bg-void-950 shadow-[inset_0_0_0_1px_var(--hairline)]">
      <video
        ref={video}
        muted
        playsInline
        className="aspect-video w-full object-contain"
        aria-label={`Your ${what}, as he sees it`}
      />
      <figcaption className="absolute inset-x-2 bottom-2 flex items-center gap-2">
        <span className="glass-solid flex h-7 items-center gap-1.5 rounded-full px-2.5 text-micro text-steel-100">
          <span aria-hidden="true" className="size-1.5 animate-breathe rounded-full bg-alert-400" />
          {what === "screen" ? "Your screen" : "Your camera"}
        </span>
        <Button size="xs" tone="danger" className="ml-auto rounded-full" onClick={() => stopShare(what)}>
          <Square aria-hidden="true" className="fill-current" />
          Stop
        </Button>
      </figcaption>
    </figure>
  )
}
