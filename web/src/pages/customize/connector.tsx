/**
 * A connector's page (/customize/connectors/<id>/<section>): its name, what it is for, its status and a switch to turn
 * it on and off; then its sections. Overview: how it is doing (and why not, with a way to try again), the sign-in for
 * one that has it (a browser on this PC does that), where it comes from, and removing it. Tools: each with what it
 * can do (reads, changes things, deletes; whether it reaches the internet) and its own switch. Settings: its own,
 * whatever they are (fields, your documents' folders, a gesture remote's gestures). Security: whose code it is, the
 * sandbox, its secrets, which channels may use it. Activity: what it did lately, live.
 */
import { Activity, Globe, Info, ScrollText, Trash2, TriangleAlert } from "lucide-react"
import { type ReactNode, useState } from "react"
import { Link, useLocation } from "wouter"
import {
  missing,
  openSignInTab,
  useConnect,
  useConnector,
  useDisconnect,
  useRemoveConnector,
  useSetConnector,
  useSwitchConnectorTool,
} from "@/lib/api"
import { STATUS, TRUST_WORDS } from "@/lib/connectors"
import { connectorIcon } from "@/lib/icons"
import type { Connector, ConnectorDetail, Risk } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { SearchField } from "@/ui/search"
import { Empty, Failed, Pending } from "@/ui/states"
import { Switch } from "@/ui/switch"
import { Tabs } from "@/ui/tabs"
import { Tooltip } from "@/ui/tooltip"
import { Entry } from "../activity/entry"
import { PageHeader } from "../frame"
import { Note, Panel, Row } from "../settings/fields"
import { BackLink, IconTile } from "./parts"
import { SecurityTab } from "./security"
import { SettingsTab } from "./settings"

const SECTIONS = ["overview", "tools", "settings", "security", "activity"] as const
type Section = (typeof SECTIONS)[number]

const path = (id: string, section: Section) =>
  `/customize/connectors/${encodeURIComponent(id)}${section === "overview" ? "" : `/${section}`}`

/**
 * A connector's page.
 * @param props the connector's `id` and the `section` open (from the address)
 * @returns the page
 */
export function ConnectorPage({ id, section }: { id: string; section?: string }) {
  const connector = useConnector(id)
  const [, navigate] = useLocation()
  const open: Section = SECTIONS.find((s) => s === section) ?? "overview"
  const back = <BackLink href="/customize/connectors" label="Connectors" />
  if (connector.isError)
    return (
      <>
        {back}
        {missing(connector.error) ? (
          <div className="glass">
            <Empty icon={<TriangleAlert />}>There is no connector called {id} (any more).</Empty>
          </div>
        ) : (
          <Failed
            title="The connector can't be read"
            error={connector.error}
            onRetry={() => void connector.refetch()}
          />
        )}
      </>
    )
  const c = connector.data
  if (!c)
    return (
      <>
        {back}
        <Pending label="Asking the connector" delay={150} page />
      </>
    )
  const items = [
    { value: "overview" as const, label: "Overview" },
    { value: "tools" as const, label: "Tools", count: c.tools.length },
    ...(c.settings.kind === "none" ? [] : [{ value: "settings" as const, label: "Settings" }]),
    { value: "security" as const, label: "Security" },
    { value: "activity" as const, label: "Activity", count: c.activity.length },
  ]
  return (
    <>
      <Header c={c} back={back} />
      <Tabs
        value={items.some((i) => i.value === open) ? open : "overview"}
        onChange={(next) => navigate(path(c.id, next))}
        items={items}
        label={`${c.title}: sections`}
        listClassName="glass rounded-card p-1"
        className="grid gap-4"
      >
        <div className="mt-4 grid gap-4" data-testid={`connector-${open}`}>
          {open === "overview" && <Overview c={c} />}
          {open === "tools" && <ToolsTab c={c} />}
          {open === "settings" && <SettingsTab c={c} />}
          {open === "security" && <SecurityTab c={c} />}
          {open === "activity" && <ActivityTab c={c} />}
        </div>
      </Tabs>
    </>
  )
}

/**
 * The page's header: icon and name, what it is for, its status and its switch.
 * @param props the connector (`c`) and the link `back`
 * @returns the header
 */
function Header({ c, back }: { c: ConnectorDetail; back: ReactNode }) {
  const set = useSetConnector()
  const status = STATUS[c.status]
  const on = c.status !== "off" && c.status !== "unavailable"
  return (
    <PageHeader
      back={back}
      title={
        <span className="flex items-center gap-3.5">
          <IconTile icon={connectorIcon(c.icon)} className="size-12 [&_svg]:size-6" />
          <span className="truncate">{c.title}</span>
        </span>
      }
      lead={c.description}
      actions={
        <>
          <Badge tone={status.tone} dot={c.status === "connected"} pulse={c.status === "connecting"}>
            {status.label}
          </Badge>
          {c.trust === "unreviewed" && <Badge tone="dusk">Not reviewed</Badge>}
          {/* biome-ignore lint/a11y/noLabelWithoutControl: the switch inside is the control */}
          <label className="flex items-center gap-2 text-sm text-steel-300">
            On
            <Switch
              checked={on}
              disabled={set.isPending || c.status === "unavailable"}
              onCheckedChange={(enabled) => set.mutate({ id: c.id, change: { enabled } })}
              aria-label={`${c.title}: on or off`}
              data-testid="connector-switch"
            />
          </label>
        </>
      }
    />
  )
}

// ---------- overview ----------
/** What a status means for you, in a sentence. */
function statusWords(c: Connector): string {
  switch (c.status) {
    case "connected":
      return "Connected: its tools are his to use."
    case "needs_sign_in":
      return "Not connected: sign in to let him use it."
    case "needs_setup":
      return "It needs a little setup before it can connect."
    case "connecting":
      return "Connecting…"
    case "error":
      return "It isn't answering."
    case "off":
      return "Switched off: he doesn't use it."
    case "unavailable":
      return "This machine can't run it."
  }
}

/**
 * The overview.
 * @param props the connector (`c`)
 * @returns the panels
 */
function Overview({ c }: { c: ConnectorDetail }) {
  const [, navigate] = useLocation()
  const connect = useConnect()
  const disconnect = useDisconnect()
  const remove = useRemoveConnector()
  const local = c.local
  const oauth = c.auth === "oauth"
  const askRemove = async () => {
    const text = "Its tools disappear from Orion, and its secrets leave the vault. You can add it again any time."
    if (await ask({ title: `Remove ${c.title}?`, text, ok: "Remove", danger: true, kicker: "Connectors" }))
      remove.mutate(c.id, { onSuccess: () => navigate("/customize/connectors") })
  }
  return (
    <>
      <Panel title="How it is doing" icon={Activity} data-testid="connector-status">
        <p className="px-5 pb-4 text-sm text-steel-200">{statusWords(c)}</p>
        {c.status === "error" && (
          <div className="flex flex-wrap items-center gap-3 border-t px-5 py-3.5">
            <TriangleAlert aria-hidden="true" className="size-4 shrink-0 text-alert-400" />
            <span className="min-w-0 flex-1 break-words font-mono text-xs text-alert-100">
              {c.error || "It stopped answering."}
            </span>
            <Button
              size="xs"
              disabled={connect.isPending}
              onClick={() => connect.mutate({ id: c.id, tab: null })}
              data-testid="connector-reconnect"
            >
              Reconnect
            </Button>
          </div>
        )}
        {c.needs.length > 0 && <Note>This machine lacks {c.needs.join(", ")}.</Note>}
        {c.status === "needs_setup" && c.settings.kind !== "none" && (
          <Note>
            <Link href={path(c.id, "settings")} className="text-rigel-300 underline-offset-2 hover:underline">
              Open its settings
            </Link>{" "}
            to finish.
          </Note>
        )}
        {oauth && (
          <Row
            label={c.signed_in ? "Signed in" : "Not signed in"}
            hint={
              local
                ? c.signed_in
                  ? "The sign-in is kept in his vault. Disconnecting removes it."
                  : "A page of the service opens in a new tab on this PC. The sign-in goes to his vault."
                : "Connect from the PC: signing in happens in a browser on Orion's own machine."
            }
          >
            {c.signed_in ? (
              <Button
                disabled={disconnect.isPending}
                onClick={() => disconnect.mutate(c.id)}
                data-testid="connector-disconnect"
              >
                Disconnect
              </Button>
            ) : (
              <Button
                tone="primary"
                disabled={!local || connect.isPending}
                onClick={() => connect.mutate({ id: c.id, tab: openSignInTab() })}
                data-testid="connector-connect"
              >
                Connect
              </Button>
            )}
          </Row>
        )}
        <Row label="Used" hint="How many times he has called one of its tools." inline>
          <span className="font-mono text-sm text-steel-100 tabular-nums">{c.calls.toLocaleString("en")}</span>
        </Row>
      </Panel>

      <Panel title="About it" icon={Info}>
        <Row label={WHERE[c.kind]} hint={TRUST_WORDS[c.trust]} inline>
          {c.plugin ? (
            <Link
              href={`/customize/plugins/${encodeURIComponent(c.plugin)}`}
              className="text-sm text-rigel-300 underline-offset-2 hover:underline"
            >
              {c.plugin}
            </Link>
          ) : (
            <span className="font-mono text-xs text-steel-400">{c.id}</span>
          )}
        </Row>
        {c.removable && (
          <Row label="Remove it" hint="Its tools leave Orion, and its secrets leave the vault.">
            <Button
              tone="danger"
              disabled={remove.isPending}
              onClick={() => void askRemove()}
              data-testid="connector-remove"
            >
              <Trash2 aria-hidden="true" />
              Remove
            </Button>
          </Row>
        )}
      </Panel>
    </>
  )
}

const WHERE: Record<Connector["kind"], string> = {
  builtin: "Part of Orion",
  catalog: "From the directory",
  custom: "Added by you",
  plugin: "Brought by a plugin",
}

// ---------- tools ----------
const RISK: Record<Risk, { label: string; tone: "neutral" | "dusk" | "alert" }> = {
  read: { label: "Reads", tone: "neutral" },
  change: { label: "Changes things", tone: "dusk" },
  destroy: { label: "Deletes", tone: "alert" },
}

/**
 * The tools, each with its switch.
 * @param props the connector (`c`)
 * @returns the list
 */
function ToolsTab({ c }: { c: Connector }) {
  const [typed, setTyped] = useState("")
  const switchTool = useSwitchConnectorTool()
  const words = typed.toLowerCase().split(/\s+/).filter(Boolean)
  const shown = c.tools.filter((t) => {
    const text = `${t.title} ${t.name} ${t.description}`.toLowerCase()
    return words.every((w) => text.includes(w))
  })
  const off = c.status === "off" || c.status === "unavailable"
  const on = c.tools.filter((t) => t.enabled).length
  return (
    <div className="grid gap-3" data-testid="connector-tools">
      <div className="flex flex-wrap items-center gap-3">
        <p className="text-sm text-steel-300">
          {c.tools.length ? `${on} of ${c.tools.length} on.` : "No tools yet."}
          {off && " The connector is off: none of them is his to use."}
        </p>
        {c.tools.length > 8 && (
          <SearchField
            value={typed}
            onChange={setTyped}
            label="Search its tools"
            placeholder="Search its tools…"
            className="ml-auto w-full max-w-xs"
          />
        )}
      </div>
      {c.tools.length === 0 ? (
        <div className="glass">
          <Empty icon={<Globe />}>Its tools show up once it's connected.</Empty>
        </div>
      ) : shown.length === 0 ? (
        <div className="glass">
          <Empty icon={<Globe />}>No tool matches.</Empty>
        </div>
      ) : (
        <ul className="glass overflow-hidden">
          {shown.map((t) => (
            <li
              key={t.name}
              className={cn("flex items-center gap-4 border-b px-5 py-3 last:border-b-0", off && "opacity-60")}
              data-testid={`tool-${t.name}`}
            >
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
                  <span className={cn("truncate text-base", t.enabled && !off ? "text-steel-50" : "text-steel-400")}>
                    {t.title || t.name}
                  </span>
                  <Badge tone={RISK[t.risk].tone}>{RISK[t.risk].label}</Badge>
                  {t.open_world && (
                    <Tooltip
                      content="It reaches the internet"
                      render={<span className="grid size-5 place-items-center text-steel-400" />}
                    >
                      <Globe aria-label="Reaches the internet" className="size-3.5" />
                    </Tooltip>
                  )}
                </span>
                <span className="mt-0.5 line-clamp-2 block text-sm text-steel-400">{t.description}</span>
              </span>
              <Switch
                size="sm"
                checked={t.enabled}
                disabled={off}
                onCheckedChange={(enabled) => switchTool.mutate({ id: c.id, tool: t.name, enabled })}
                aria-label={`Let him use ${t.title || t.name}`}
                data-testid={`tool-switch-${t.name}`}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------- activity ----------
/**
 * What it did lately.
 * @param props the connector (`c`)
 * @returns the list
 */
function ActivityTab({ c }: { c: ConnectorDetail }) {
  return (
    <div className="grid gap-3" data-testid="connector-activity">
      {c.activity.length ? (
        <ol className="glass overflow-hidden p-1.5">
          {c.activity.map((item) => (
            <Entry key={item.id} item={item} />
          ))}
        </ol>
      ) : (
        <div className="glass">
          <Empty icon={<ScrollText />}>Nothing yet: its calls show here as he makes them.</Empty>
        </div>
      )}
      <Link
        href={`/activity?q=${encodeURIComponent(c.id)}`}
        className="w-fit text-sm text-rigel-300 underline-offset-2 hover:underline"
      >
        All of his activity →
      </Link>
    </div>
  )
}
