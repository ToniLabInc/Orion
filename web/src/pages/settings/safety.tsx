/**
 * Safety: what keeps the access he has from doing harm, and none of which he can change himself (only you, here or
 * in config.yaml). Asking first (once a request has read outside content, he asks before anything that changes or
 * sends something; the tools he always or never asks about; how long he waits for your answer); what each channel may
 * do (./channels); the sandbox his commands run in; your secrets (./vault); and how long the activity log keeps what
 * he did. What he sees (screens, cameras, pictures) is the Eyes connector's own, in Customize.
 */
import { Box, ScrollText, ShieldCheck, SlidersHorizontal, TriangleAlert } from "lucide-react"
import { Link } from "wouter"
import { missing, useSafety } from "@/lib/api"
import { type SandboxState, sandboxState } from "@/lib/rules"
import type { SafetyState, SettingsResponse } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { buttonClass } from "@/ui/button"
import { Failed, Pending } from "@/ui/states"
import { Channels } from "./channels"
import { Code, Fields, FoldPanel, Note, Panel, Row, useSection, useShows } from "./fields"
import { Vault } from "./vault"

const ASKING = ["safety.confirm_after_content", "safety.confirm_timeout_s"]
const WHICH = ["safety.always_confirm", "safety.never_confirm"]
const SWITCHES = ["safety.sandbox_commands", "safety.sandbox_network"]

/**
 * The Safety tab.
 * @param _props the settings (unused: its parts read their own)
 * @returns the tab
 */
export function Safety(_props: { settings: SettingsResponse }) {
  const safety = useSafety()
  const which = useShows(WHICH)
  return (
    <>
      <Panel title="Asking first" icon={ShieldCheck} description="Before risky actions">
        <Fields paths={ASKING} />
        <Note>
          He asks on every HUD at once (and aloud, if you spoke to him): the first answer counts, and no answer is a no.
        </Note>
      </Panel>
      {which && (
        <FoldPanel title="Which tools" icon={SlidersHorizontal}>
          <Fields paths={WHICH} />
        </FoldPanel>
      )}
      {safety.data ? (
        <>
          <Channels safety={safety.data} />
          <Sandbox sandbox={safety.data.sandbox} />
        </>
      ) : missing(safety.error) ? null : safety.isError ? (
        <Failed title="The safety rules can't be read" error={safety.error} onRetry={() => void safety.refetch()} />
      ) : (
        <Pending label="Reading the safety rules" delay={150} />
      )}
      <Vault />
      <ActivityLog />
    </>
  )
}

/**
 * The sandbox his shell commands run in: how it stands, the two switches (where the schema offers them), and what it
 * hides and lets him write.
 * @param props the `sandbox`
 * @returns the panel
 */
function Sandbox({ sandbox }: { sandbox: SafetyState["sandbox"] }) {
  const state = sandboxState(sandbox)
  const switches = useShows(SWITCHES)
  const words: Record<SandboxState, string> = {
    off: "The sandbox is switched off: his commands run with his full access, and he still asks you before each one.",
    missing: `His commands can't run: ${sandbox.why || "this machine can't make a sandbox"}. Mend that, or switch the sandbox off.`,
    "no-network":
      "His commands can't have the network on this machine: its kernel is too old to keep them away from his own services (Linux 6.12 or newer does). Cut the network off for commands, or update the kernel.",
    works:
      sandbox.available === "container"
        ? "He runs in a container: it keeps his commands away from the rest of this machine."
        : "His commands run in bubblewrap: the system read-only, your keys and his own data hidden, nothing of his environment.",
  }
  const bad = state !== "works"
  return (
    <Panel
      title="The sandbox"
      icon={Box}
      description="Where his shell commands run"
      action={
        bad ? (
          <Badge tone="alert" dot>
            {state === "off" ? "Full access" : "Commands blocked"}
          </Badge>
        ) : (
          <Badge tone="ok" dot>
            {sandbox.available === "container" ? "Container" : "bubblewrap"}
          </Badge>
        )
      }
      data-testid="safety-sandbox"
    >
      <p className={cn("flex items-start gap-2.5 px-5 pb-4 text-sm", bad ? "text-alert-100" : "text-steel-300")}>
        {bad && <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-alert-400" />}
        {words[state]}
      </p>
      {switches ? (
        <Fields paths={SWITCHES} />
      ) : (
        sandbox.available !== "" && (
          <Row label="The network" hint="Whether a command may reach the network." inline>
            <Badge tone={sandbox.network ? "neutral" : "rigel"}>{sandbox.network ? "Reachable" : "Cut off"}</Badge>
          </Row>
        )
      )}
      {sandbox.available !== "" && sandbox.commands && (
        <>
          <Paths label="Hidden" hint="Out of a command's sight, whatever it runs." paths={sandbox.hidden} />
          <Paths label="It may write" hint="Everything else is read-only to a command." paths={sandbox.writable} />
        </>
      )}
      <Note>
        The folders it hides and lets a command write are set in config.yaml, under <Code>safety.sandbox_hidden</Code>{" "}
        and <Code>safety.sandbox_writable</Code>. A command outside the sandbox always asks you first, and never runs
        from Discord or a job.
      </Note>
    </Panel>
  )
}

/**
 * Some paths, as a list of chips.
 * @param props the row's `label` and `hint`, and the `paths`
 * @returns the row
 */
function Paths({ label, hint, paths }: { label: string; hint: string; paths: string[] }) {
  return (
    <Row label={label} hint={hint}>
      {paths.length ? (
        <ul className="flex w-full flex-wrap gap-1.5 @lg:w-80 @lg:justify-end">
          {paths.map((path) => (
            <li
              key={path}
              className="rounded-chip bg-steel-50/6 px-2 py-0.5 font-mono text-xs text-steel-200 shadow-[inset_0_0_0_1px_var(--hairline)]"
            >
              {path}
            </li>
          ))}
        </ul>
      ) : (
        <span className="text-sm text-steel-400">None</span>
      )}
    </Row>
  )
}

/**
 * How long the activity log keeps what he did (his schema's activity options), and the way to it.
 * @returns the panel, or nothing on an Orion that lets the HUD set none of it
 */
function ActivityLog() {
  const keys = useSection("activity")
  if (!keys.length) return null
  return (
    <Panel
      title="The activity log"
      icon={ScrollText}
      description="What he did and who allowed it, secrets left out"
      action={
        <Link href="/activity" className={buttonClass({ size: "sm" })}>
          Open it
        </Link>
      }
    >
      <Fields paths={keys} />
    </Panel>
  )
}
