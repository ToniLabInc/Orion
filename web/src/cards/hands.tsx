/**
 * Hands: your hands as the gesture camera sees them (./hands-view), to find where it sees you and how it reads each
 * shape. The header says how the camera is doing; the camera button shows its picture behind the hands (it comes from
 * the Pi only while this asks for it), and the cog opens the remote's own page (Customize), where each gesture is given
 * something to do. Open, it is the only time the Pi sends anything but gestures.
 */
import { Camera, CameraOff, Hand, Settings2 } from "lucide-react"
import { useEffect } from "react"
import { useLocation } from "wouter"
import { useGestures } from "@/lib/api"
import { type HandsStatus, useHandsStatus } from "@/lib/hands"
import { setPref, usePrefs } from "@/lib/prefs"
import { IconButton } from "@/ui/button"
import { Empty } from "@/ui/states"
import type { CardProps } from "./card"
import { HandsView } from "./hands-view/view"

/**
 * The header's few words on the camera.
 * @param status the view's state
 * @returns the words
 */
export function handsMeta(status: HandsStatus): string {
  switch (status.state) {
    case "live":
      return `${status.hands} in view · ${status.fps} fps`
    case "down":
      return "No link"
    case "paused":
      return "Paused"
    case "stale":
      return "No signal"
    default:
      return "Waking"
  }
}

/**
 * The hands card.
 * @param props the card's
 * @returns the body
 */
export default function Hands({ setNote }: CardProps) {
  const picture = usePrefs((s) => s.handsPicture)
  const [, navigate] = useLocation()
  const gestures = useGestures()
  const remote = gestures.data?.remotes.find((r) => r.enabled) ?? gestures.data?.remotes[0]
  const status = useHandsStatus(remote?.name)
  const none = gestures.data && !gestures.data.remotes.length
  const meta = none ? null : handsMeta(status)
  useEffect(() => setNote(meta), [meta, setNote])
  if (none)
    return (
      <Empty icon={<Hand />}>
        No gesture camera is set up. Add the ORION hands tracker as an MCP server with intents: true (see the
        HandTracking README).
      </Empty>
    )
  return (
    <div className="grid gap-2 px-3 pb-3" data-testid="widget-hands">
      <HandsView
        server={remote?.name}
        picture={picture}
        className="aspect-[4/3] w-full overflow-hidden rounded-control bg-void-950/50"
      />
      <div className="flex items-center gap-1">
        <IconButton
          label={picture ? "Hide the camera's picture" : "Show the camera's picture behind your hands"}
          size="xs"
          aria-pressed={picture}
          onClick={() => setPref("handsPicture", !picture)}
          data-testid="hands-picture"
        >
          {picture ? <Camera /> : <CameraOff />}
        </IconButton>
        {picture && (
          <span className="text-micro text-steel-500">The picture comes from the Pi only while it shows here.</span>
        )}
        <IconButton
          label="What each gesture does"
          size="xs"
          className="ml-auto"
          onClick={() =>
            navigate(
              remote ? `/customize/connectors/${encodeURIComponent(remote.name)}/settings` : "/customize/connectors",
            )
          }
        >
          <Settings2 />
        </IconButton>
      </div>
    </div>
  )
}
