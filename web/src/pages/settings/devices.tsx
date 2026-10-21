/**
 * Devices: where he meets you. The devices connected now (every HUD open in a browser, what each offers, which carries
 * his voice), pairing (on his own machine: a code to pair another device, each device with when it was last used and
 * a way to unpair it; on a paired device, only that it is one), and what his own machine has (from init.host: a
 * desktop, sound, a battery…, which decides what his tools can do there).
 */
import { Check, Cpu, Link2, MonitorSmartphone, X } from "lucide-react"
import { useState } from "react"
import { useShallow } from "zustand/react/shallow"
import { deviceIcon } from "@/cards/devices"
import { usePairing, useStartPairing, useUnpair } from "@/lib/api"
import { agoInWords, shortDate } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { useLive } from "@/lib/store"
import type { HostProfile, PairingState, SettingsResponse } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { Code, Note, Panel, Row } from "./fields"

/**
 * The Devices tab.
 * @param _props the settings (unused: everything here is live or its own query)
 * @returns the tab
 */
export function Devices(_props: { settings: SettingsResponse }) {
  return (
    <>
      <Connected />
      <Pairing />
      <ThisMachine />
    </>
  )
}

/**
 * The HUDs connected now.
 * @returns the panel
 */
function Connected() {
  const { surfaces, surface, holder } = useLive(
    useShallow((s) => ({ surfaces: s.surfaces, surface: s.surface, holder: s.voiceHolder })),
  )
  const now = useNow(60_000)
  return (
    <Panel
      title="Connected now"
      icon={MonitorSmartphone}
      description="Every HUD open in a browser"
      data-testid="settings-connected"
    >
      {surfaces.length ? (
        surfaces.map((s) => {
          const Icon = deviceIcon(s.device)
          return (
            <Row
              key={s.id}
              label={
                <span className="flex items-center gap-2.5">
                  <Icon aria-hidden="true" className="size-4 text-steel-400" />
                  {s.device}
                  {s.id === surface && <Badge tone="rigel">This one</Badge>}
                  {holder.device === s.id && (
                    <Badge tone="betel" dot>
                      Carries his voice
                    </Badge>
                  )}
                </span>
              }
              hint={`${s.local ? "On his machine" : "On your network"} · offers ${s.caps.join(", ") || "nothing yet"}${s.sharing.length ? ` · sharing its ${s.sharing.join(" and ")}` : ""}`}
            >
              {s.id !== surface && <span className="text-xs text-steel-500">seen {agoInWords(s.seen, now)}</span>}
            </Row>
          )
        })
      ) : (
        <Note className="border-t-0">Only this page, as far as he says (an older Orion doesn't list the others).</Note>
      )}
    </Panel>
  )
}

/**
 * The devices paired with him.
 * @returns the panel, once the server has said
 */
function Pairing() {
  const state = usePairing()
  if (!state.data) return null
  return (
    <Panel
      title="Pairing"
      icon={Link2}
      description="The devices allowed to open the HUD"
      data-testid="settings-devices"
    >
      {state.data.local ? (
        <OnHisMachine state={state.data} />
      ) : (
        <Note className="border-t-0">
          This device is paired with him. Devices are paired and unpaired on his machine.
        </Note>
      )}
    </Panel>
  )
}

/**
 * The devices, as his own machine sees them.
 * @param props the pairing `state`
 * @returns the rows
 */
function OnHisMachine({ state }: { state: PairingState }) {
  const start = useStartPairing()
  const unpair = useUnpair()
  const [code, setCode] = useState<{ code: string; ends: number } | null>(null)
  const now = useNow(1000, code !== null).getTime() / 1000
  const left = code ? Math.max(0, Math.ceil(code.ends - now)) : 0
  const askUnpairAll = async () => {
    const text = "Every device paired with him stops working with him at once; each pairs again with a new code."
    if (await ask({ title: "Unpair every device?", text, ok: "Unpair all", danger: true, kicker: "Devices" }))
      unpair.mutate(null)
  }
  return (
    <>
      <Row
        label="Pair a device"
        hint={
          state.open ? (
            <>
              On the device, open <Code>{state.url}</Code> and type the code shown here.
            </>
          ) : (
            "Let your network in first (Settings, General, Service), then restart him."
          )
        }
      >
        <Button
          tone="primary"
          disabled={!state.open || start.isPending}
          onClick={() => start.mutate(undefined, { onSuccess: setCode })}
          data-testid="pair-start"
        >
          {code && left ? "New code" : "Pair a device"}
        </Button>
      </Row>
      {code && left > 0 && (
        <Row label="The code" hint={`It works once, for ${left} s more.`}>
          <span className="font-mono text-3xl text-steel-50 tracking-[0.3em]" data-testid="pair-shown">
            {code.code}
          </span>
        </Row>
      )}
      {state.devices.map((device) => (
        <Row
          key={device.id}
          label={device.name}
          hint={`Paired ${shortDate(new Date(device.paired * 1000))}, last used ${agoInWords(device.seen, new Date())}.`}
        >
          <Button size="sm" disabled={unpair.isPending} onClick={() => unpair.mutate(device.id)}>
            Unpair
          </Button>
        </Row>
      ))}
      {state.devices.length > 0 && (
        <Row label="Unpair every device" hint="Each stops working with him at once.">
          <Button size="sm" tone="danger" disabled={unpair.isPending} onClick={() => void askUnpairAll()}>
            Unpair all
          </Button>
        </Row>
      )}
      {state.open && !state.tls && (
        <Note>
          Over plain HTTP, what a device sends crosses your network as it is, its key too, and a browser won't lend its
          microphone: give him a certificate (<Code>server.tls_cert</Code> and <Code>server.tls_key</Code>) to serve the
          HUD over HTTPS.
        </Note>
      )}
    </>
  )
}

/** What each of the machine's abilities means, and what needs it. */
const ABILITIES: { key: keyof HostProfile; label: string; needs: string }[] = [
  { key: "session", label: "A desktop", needs: "opening apps and files" },
  { key: "sound", label: "Sound", needs: "the volume" },
  { key: "dbus", label: "The desktop's bus", needs: "media players, the screen lock" },
  { key: "power", label: "Power control", needs: "suspend, restart, shut down" },
  { key: "clipboard", label: "A clipboard", needs: "copying and pasting on it" },
  { key: "screen", label: "Screen capture", needs: "looking at its screen" },
  { key: "battery", label: "A battery", needs: "the battery watcher" },
  { key: "temperatures", label: "Temperature sensors", needs: "the heat watchers" },
  { key: "nvidia", label: "An NVIDIA GPU", needs: "the GPU's vitals" },
]

/**
 * His own machine: what it is and what it has.
 * @returns the panel
 */
function ThisMachine() {
  const host = useLive((s) => s.host)
  if (!host) return null
  return (
    <Panel
      title="His machine"
      icon={Cpu}
      description={[host.platform, host.desktop || host.session, host.container ? "in a container" : ""]
        .filter(Boolean)
        .join(" · ")}
      data-testid="settings-host"
    >
      <ul className="grid grid-cols-1 gap-x-6 border-t px-5 py-3 @lg:grid-cols-2">
        {ABILITIES.map((a) => {
          const value = host[a.key]
          const has = Boolean(value)
          return (
            <li key={a.key} className="flex items-start gap-2.5 py-1.5">
              {has ? (
                <Check aria-label="Has it" className="mt-0.5 size-4 shrink-0 text-ok-400" />
              ) : (
                <X aria-label="Hasn't it" className="mt-0.5 size-4 shrink-0 text-steel-600" />
              )}
              <span className="min-w-0">
                <span className={cn("block text-sm", has ? "text-steel-100" : "text-steel-500")}>
                  {a.label}
                  {typeof value === "string" && value && (
                    <span className="ml-1.5 font-mono text-xs text-steel-500">{value}</span>
                  )}
                </span>
                <span className="block text-xs text-steel-500">{a.needs}</span>
              </span>
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}
