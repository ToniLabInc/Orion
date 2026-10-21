/**
 * What keeps a connector in its place: whether its code is ours or someone else's and what that means, whether it runs
 * in the sandbox (a connector that isn't reviewed does, and gets only its own folder, its own secrets and the network),
 * the secrets it holds (their names and whether they are set, never their values), and which channels may use it: by
 * voice, on the HUD, from Discord, in his jobs, from other apps. A channel can allow it, make him ask first (when he
 * decides to use it himself), or never allow it, not even after a yes. These are Safety's limits (Settings), set here
 * for this connector as a whole.
 */
import { KeyRound, Route, ShieldCheck } from "lucide-react"
import { Link } from "wouter"
import { missing, useSafety, useSaveChannel, useSetConnector } from "@/lib/api"
import { type Stance, stanceOf, TRUST_WORDS, withStance } from "@/lib/connectors"
import { CHANNELS } from "@/lib/rules"
import type { Connector } from "@/lib/types"
import { Badge } from "@/ui/badge"
import { Failed, Pending } from "@/ui/states"
import { Switch } from "@/ui/switch"
import { Segmented } from "@/ui/tabs"
import { Note, Panel, Row } from "../settings/fields"

const STANCES: { value: Stance; label: string }[] = [
  { value: "allowed", label: "Allowed" },
  { value: "asks", label: "Asks first" },
  { value: "never", label: "Never" },
]

/**
 * The tab.
 * @param props the connector (`c`)
 * @returns the panels
 */
export function SecurityTab({ c }: { c: Connector }) {
  return (
    <>
      <Trust c={c} />
      <Secrets c={c} />
      <Channels c={c} />
    </>
  )
}

/**
 * Whose code it is, and the sandbox.
 * @param props the connector (`c`)
 * @returns the panel
 */
function Trust({ c }: { c: Connector }) {
  const set = useSetConnector()
  const unreviewed = c.trust === "unreviewed"
  return (
    <Panel title="Whose code it is" icon={ShieldCheck} data-testid="security-trust">
      <p className="px-5 pb-4 text-sm text-steel-300">{TRUST_WORDS[c.trust]}</p>
      <Row
        label="The sandbox"
        hint={
          c.sandbox === "bwrap"
            ? "It runs in bubblewrap: its own folder as home, no sight of your keys, your files or his environment."
            : unreviewed
              ? "Off: it runs with your account's access. Switch it on unless you trust this code."
              : "Not needed: its code is part of Orion, or a version we reviewed."
        }
        inline
      >
        {unreviewed ? (
          <Switch
            checked={c.sandbox === "bwrap"}
            disabled={set.isPending}
            onCheckedChange={(on) => set.mutate({ id: c.id, change: { sandbox: on } })}
            aria-label="Run it in the sandbox"
            data-testid="sandbox-switch"
          />
        ) : (
          <Badge>{c.sandbox === "bwrap" ? "bubblewrap" : "None"}</Badge>
        )}
      </Row>
    </Panel>
  )
}

/**
 * The secrets it holds.
 * @param props the connector (`c`)
 * @returns the panel
 */
function Secrets({ c }: { c: Connector }) {
  return (
    <Panel
      title="Its secrets"
      icon={KeyRound}
      description="Kept in the vault: only this connector gets them, and no model sees them"
      data-testid="security-secrets"
    >
      {c.secrets.length ? (
        <ul className="border-t">
          {c.secrets.map((s) => (
            <li key={s.name} className="flex items-center gap-3 border-b px-5 py-3 last:border-b-0">
              <span className="min-w-0 flex-1 truncate font-mono text-sm text-steel-50">{s.name}</span>
              {s.set ? <Badge tone="ok">Set</Badge> : <Badge>Not set</Badge>}
            </li>
          ))}
        </ul>
      ) : (
        <Note className="border-t-0 pt-0">It holds no secret.</Note>
      )}
      <Note>
        Set or remove them in{" "}
        <Link href="/settings/safety" className="text-rigel-300 underline-offset-2 hover:underline">
          Settings, Safety
        </Link>
        , or among its settings.
      </Note>
    </Panel>
  )
}

/**
 * Which channels may use it.
 * @param props the connector (`c`)
 * @returns the panel
 */
function Channels({ c }: { c: Connector }) {
  const safety = useSafety()
  const save = useSaveChannel()
  if (missing(safety.error)) return null
  return (
    <Panel
      title="Which channels may use it"
      icon={Route}
      description="By where a request comes from"
      data-testid="security-channels"
    >
      {safety.isError ? (
        <div className="px-5 pb-4">
          <Failed title="The limits can't be read" error={safety.error} onRetry={() => void safety.refetch()} />
        </div>
      ) : !safety.data ? (
        <Pending label="Reading the limits" delay={150} className="px-5 pb-4" />
      ) : (
        CHANNELS.map((channel) => {
          const rules = safety.data.channels[channel.id] ?? { never: [], confirm: [] }
          const { stance, named } = stanceOf(c, rules)
          return (
            <Row
              key={channel.id}
              label={channel.label}
              hint={`${channel.about[0].toUpperCase()}${channel.about.slice(1)}.${
                named ? ` ${named} of its tools ${named === 1 ? "is" : "are"} named one by one in Safety.` : ""
              }`}
            >
              <Segmented
                value={stance}
                onChange={(next) => save.mutate({ channel: channel.id, rules: withStance(c.id, rules, next) })}
                choices={STANCES}
                label={`${channel.label}: may it use ${c.title}`}
                id={`stance-${channel.id}`}
              />
            </Row>
          )
        })
      )}
    </Panel>
  )
}
