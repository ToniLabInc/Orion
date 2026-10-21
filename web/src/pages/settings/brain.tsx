/**
 * Brain: where he thinks. Three brains, each saying whether it is ready (your Claude plan, a model on this machine, or
 * one on OpenRouter), then the chosen one's panel:
 * - Claude: the models Claude Code offers on your plan (or how to sign in);
 * - this machine: the Ollama models here (pick one, delete one, download another with its progress live); Ollama
 *   itself is an opt-in install;
 * - OpenRouter: the API key (write-only) and its models that can use tools, free ones first.
 * And, only where the settings schema offers it, the scripted test brain (replies read from a file) as one quiet line.
 * Then the stand-in brain (the one he switches to while the main one can't answer, and whether it stands in now), how
 * fast each brain answers (./speed), what OpenRouter costs (./spend), the benchmark (./bench), how he thinks (folded
 * away), and what runs on his machine whichever brain thinks: the GPU's memory and his memory search.
 */
import { Meter } from "@base-ui/react/meter"
import { Progress } from "@base-ui/react/progress"
import { Radio } from "@base-ui/react/radio"
import { RadioGroup } from "@base-ui/react/radio-group"
import {
  Asterisk,
  CircleCheck,
  CircleDashed,
  Cpu,
  Download,
  Gauge,
  LifeBuoy,
  LoaderCircle,
  type LucideIcon,
  Route,
  ScrollText,
  Search,
  SlidersHorizontal,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react"
import { motion } from "motion/react"
import { type FormEvent, useState } from "react"
import { useShallow } from "zustand/react/shallow"
import { queryClient, useClaude, useDeleteModel, useModels, useOpenRouterModels, usePullModel } from "@/lib/api"
import { useLive } from "@/lib/store"
import type {
  ClaudeStatus,
  GpuStats,
  InstalledModel,
  ModelPull,
  ModelsResponse,
  OpenRouterModel,
  Provider,
  SettingsResponse,
} from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Badge } from "@/ui/badge"
import { Button, IconButton } from "@/ui/button"
import { Input } from "@/ui/input"
import { Failed, Pending } from "@/ui/states"
import { ARRIVE } from "../frame"
import { Bench } from "./bench"
import { Code, Diamond, Field, Fields, FoldPanel, Note, Panel, Row, useSetting, useShows, WithCode } from "./fields"
import { Speed } from "./speed"
import { Spend } from "./spend"

type Llm = ModelsResponse["llm"]

/** The brains he thinks with for real. */
type Real = Exclude<Provider, "script">

const BRAINS: { id: Real; icon: LucideIcon; name: string; line: string; tag?: string }[] = [
  {
    id: "claude",
    icon: Asterisk,
    name: "Your Claude plan",
    tag: "Default",
    line: "Claude, on the Pro or Max plan you already have. Nothing to load: he answers at once.",
  },
  {
    id: "ollama",
    icon: Cpu,
    name: "This machine",
    line: "A model on your own GPU: private, free, offline. It takes a few seconds to load.",
  },
  {
    id: "openrouter",
    icon: Route,
    name: "OpenRouter",
    line: "Bigger models in the cloud, with an API key. The free ones allow a few requests a minute.",
  },
]

/** The test brain: replies and tool calls read from a file instead of a model. */
const SCRIPT = {
  icon: ScrollText,
  name: "Scripted test brain",
  line: "Replies and tool calls read from a file, for tests.",
}

/**
 * A stand-in brain's choice in words: the brains' own names, and "Never" for none.
 * @param value the schema's value
 * @returns its words
 */
const standInName = (value: string | number) =>
  value === "none"
    ? "Never"
    : value === "script"
      ? SCRIPT.name
      : (BRAINS.find((b) => b.id === value)?.name ?? String(value))

/** Whether a brain can think right now: true, false (with why), or null while it is being checked. */
interface Readiness {
  ok: boolean | null
  text: string
}

const plan = (name: string) => (name ? `${name[0].toUpperCase()}${name.slice(1)} plan` : "")

/**
 * Whether Claude can think: Claude Code installed and signed in.
 * @param c Claude Code's status
 * @returns its readiness
 */
function claudeReadiness(c: ClaudeStatus | undefined): Readiness {
  if (!c) return { ok: null, text: "Checking…" }
  if (!c.installed) return { ok: false, text: "Not installed" }
  if (!c.logged_in) return { ok: false, text: "Not signed in" }
  if (c.method && c.method !== "claude.ai") return { ok: true, text: "Signed in with an API key" }
  return { ok: true, text: ["Signed in", plan(c.plan)].filter(Boolean).join(" · ") }
}

/**
 * Whether this machine can think: Ollama installed, and a model to think with.
 * @param llm the brain as GET /api/models describes it
 * @returns its readiness
 */
function localReadiness(llm: Llm | undefined): Readiness {
  if (!llm) return { ok: null, text: "Checking…" }
  if (llm.ollama && !llm.ollama.installed) return { ok: false, text: "Ollama not installed" }
  const models = llm.installed.filter((m) => !m.capabilities?.includes("embedding")).length
  if (models) return { ok: true, text: `${models} model${models === 1 ? "" : "s"} here` }
  if (llm.ollama && !llm.ollama.running) return { ok: true, text: "Ollama starts when chosen" }
  return { ok: false, text: llm.error ? "Ollama isn't running" : "No model downloaded yet" }
}

const KEY_WORDS = {
  vault: "API key in the vault",
  config: "API key saved",
  env: "API key from the environment",
  "": "Needs an API key",
}

/**
 * The Brain tab.
 * @param props the `settings`
 * @returns the tab
 */
export function Brain({ settings }: { settings: SettingsResponse }) {
  const models = useModels()
  const llm = models.data?.llm
  const claude = useClaude()
  const { value: provider = "claude", save: choose, spec } = useSetting<Provider>("brain.provider")
  const scripted = spec?.choices?.some((c) => c.value === "script") === true
  const { state, loading, liveError } = useLive(
    useShallow((s) => ({ state: s.state, loading: s.loading, liveError: s.brainError })),
  )
  const key = settings.secrets["openrouter.api_key"] ?? ""
  const error = llm ? llm.brain_error : liveError
  const readiness: Record<Real, Readiness> = {
    claude: claudeReadiness(claude.data),
    ollama: localReadiness(llm),
    openrouter: { ok: Boolean(key), text: KEY_WORDS[key] },
  }
  if (state === "loading" && provider !== "script")
    readiness[provider] = { ok: null, text: `Loading ${loading || "the model"}…` }

  return (
    <>
      <motion.div variants={ARRIVE}>
        <RadioGroup
          value={provider}
          onValueChange={(next) => choose(next as Provider)}
          aria-label="Where he thinks"
          className="@container grid gap-3"
          data-testid="field-brain-provider"
        >
          <div className="grid gap-3 @2xl:grid-cols-3">
            {BRAINS.map((brain) => (
              <BrainCard key={brain.id} brain={brain} readiness={readiness[brain.id]} loading={state === "loading"} />
            ))}
          </div>
          {scripted && <ScriptChoice />}
        </RadioGroup>
      </motion.div>

      {error && (
        <motion.div
          variants={ARRIVE}
          role="alert"
          className="flex items-start gap-3 rounded-card bg-alert-400/10 px-4 py-3 text-sm text-alert-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_35%,transparent)]"
          data-testid="brain-error"
        >
          <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-alert-400" />
          <span>
            <WithCode text={error} />
          </span>
        </motion.div>
      )}

      {provider === "claude" && <ClaudeBrain claude={claude.data} settings={settings} />}
      {provider === "ollama" && <LocalBrain llm={llm} failed={models.error} onRetry={() => void models.refetch()} />}
      {provider === "openrouter" && <OpenRouterBrain />}
      {provider === "script" && <ScriptBrain />}

      <StandIn />
      <Speed />
      <Spend settings={settings} />
      <Bench settings={settings} />

      <FoldPanel title="How he thinks" icon={SlidersHorizontal} data-testid="brain-thinking">
        {provider !== "claude" && <Field path="brain.temperature" />}
        {provider === "ollama" && <Field path="ollama.num_ctx" format={(n) => `${Number(n) / 1024}K`} />}
        <Field path="brain.think" />
        <Field path="brain.max_tool_rounds" />
        {provider !== "claude" && <Field path="tools.max_per_turn" />}
      </FoldPanel>

      <Panel title="GPU and memory search" icon={Gauge}>
        {models.data?.gpu && (
          <Row label="GPU memory" hint={models.data.gpu.name}>
            <GpuMemory gpu={models.data.gpu} />
          </Row>
        )}
        <Row
          label="Memory search"
          hint={
            llm?.embed ? (
              "Finds what he remembers by meaning, here on his machine, whichever brain thinks."
            ) : (
              <>
                Keyword search only: run <Code>orion setup</Code> to search by meaning.
              </>
            )
          }
        >
          <span className="font-mono text-sm text-steel-100" data-testid="memory-search">
            {llm ? llm.embed || "keywords" : "…"}
          </span>
        </Row>
      </Panel>
    </>
  )
}

/**
 * The stand-in brain: which one takes over while the main one can't answer (a usage limit, signed out, not running),
 * whether it stands in now and why, and when the main one is tried again.
 * @returns the panel
 */
function StandIn() {
  const models = useLive((s) => s.capabilities?.models)
  const standing = models?.standing_in === true
  return (
    <Panel
      title="Stand-in brain"
      icon={LifeBuoy}
      description="When the main brain can't answer"
      action={
        standing ? (
          <Badge tone="dusk" dot pulse>
            Standing in now
          </Badge>
        ) : models?.fallback ? (
          <Badge tone="ok" dot>
            Ready: {models.fallback}
          </Badge>
        ) : null
      }
      data-testid="brain-fallback"
    >
      {standing && (
        <Note className="border-t-0 pt-0 text-dusk-100">
          {models?.llm} thinks for him now{models?.reason ? `: ${models.reason}` : ""}. The main brain is tried again by
          itself.
        </Note>
      )}
      <Field path="brain.fallback" format={standInName} />
      <Fields paths={["brain.fallback_on", "brain.fallback_retry_min"]} />
    </Panel>
  )
}

/**
 * One brain to choose: its name, a line on what it is, and whether it is ready.
 * @param props the `brain`, its `readiness`, and whether a model is `loading`
 * @returns the card
 */
function BrainCard({
  brain,
  readiness,
  loading,
}: {
  brain: (typeof BRAINS)[number]
  readiness: Readiness
  loading: boolean
}) {
  const { ok, text } = readiness
  return (
    <Radio.Root
      value={brain.id}
      render={<div />}
      className={cn(
        "glass group/brain flex cursor-pointer flex-col gap-2.5 rounded-card p-4 outline-none transition-shadow duration-(--dur-quick)",
        "hover:shadow-[var(--shadow-elevated),inset_0_0_0_1px_var(--hairline-strong)] focus-visible:outline-2 focus-visible:outline-rigel-400",
        "data-checked:shadow-[var(--shadow-elevated),inset_0_0_0_1.5px_var(--color-rigel-400),0_0_40px_-16px_var(--color-rigel-400)]",
      )}
      data-testid={`brain-${brain.id}`}
    >
      <span className="flex items-center gap-2.5">
        <brain.icon
          aria-hidden="true"
          className="size-5 shrink-0 text-steel-400 group-data-checked/brain:text-rigel-300"
        />
        <span className="truncate font-medium text-steel-100 group-data-checked/brain:text-steel-50">{brain.name}</span>
        <Diamond className="ml-auto" />
      </span>
      <span className="text-sm text-steel-400">{brain.line}</span>
      <span className="mt-auto flex items-center gap-1.5 pt-1 text-xs [&_svg]:size-3.5 [&_svg]:shrink-0">
        {ok === null ? (
          loading ? (
            <LoaderCircle aria-hidden="true" className="animate-spin text-rigel-300" />
          ) : (
            <CircleDashed aria-hidden="true" className="text-steel-500" />
          )
        ) : ok ? (
          <CircleCheck aria-hidden="true" className="text-ok-400" />
        ) : (
          <TriangleAlert aria-hidden="true" className="text-dusk-400" />
        )}
        <span className={cn("min-w-0 flex-1", ok === false ? "text-steel-300" : "text-steel-200")}>{text}</span>
        {brain.tag && <span className="shrink-0 pl-2 text-micro text-steel-500">{brain.tag}</span>}
      </span>
    </Radio.Root>
  )
}

/**
 * The test brain as one quiet line under the three brains (a test tool: it is here only because the schema offers it).
 * @returns the choice
 */
function ScriptChoice() {
  return (
    <Radio.Root
      value="script"
      render={<div />}
      className="group/script flex cursor-pointer items-center gap-3 rounded-control px-4 py-2.5 text-sm shadow-[inset_0_0_0_1px_var(--hairline)] outline-none transition-shadow duration-(--dur-quick) hover:shadow-[inset_0_0_0_1px_var(--hairline-strong)] focus-visible:outline-2 focus-visible:outline-rigel-400 data-checked:shadow-[inset_0_0_0_1.5px_var(--color-rigel-400)]"
      data-testid="brain-script"
    >
      <SCRIPT.icon
        aria-hidden="true"
        className="size-4 shrink-0 text-steel-400 group-data-checked/script:text-rigel-300"
      />
      <span className="shrink-0 text-steel-100 group-data-checked/script:text-steel-50">{SCRIPT.name}</span>
      <span className="min-w-0 flex-1 truncate text-steel-400 max-tab:hidden">{SCRIPT.line}</span>
      <Diamond className="ml-auto" />
    </Radio.Root>
  )
}

/**
 * The test brain's panel: the file it reads (when the schema lets the HUD set it).
 * @returns the panel
 */
function ScriptBrain() {
  return (
    <Panel title={SCRIPT.name} icon={SCRIPT.icon} description={SCRIPT.line} data-testid="brain-script-panel">
      <Field path="script.path" />
      {!useShows(["script.path"]) && (
        <Note className="border-t-0">
          The file is named by <Code>script.path</Code> in config.yaml.
        </Note>
      )}
    </Panel>
  )
}

// ---------- your Claude plan ----------
/**
 * Claude: how to sign in if needed, and the models of your plan.
 * @param props Claude Code's status (`claude`) and the `settings` (their model list)
 * @returns the panel
 */
function ClaudeBrain({ claude, settings }: { claude: ClaudeStatus | undefined; settings: SettingsResponse }) {
  const { value: model, save } = useSetting<string>("claude.model")
  const lines = Object.fromEntries(settings.claude_models.map((m) => [m.value, m.detail]))
  const ready = claude?.installed && claude.logged_in
  const check = () => {
    void queryClient.invalidateQueries({ queryKey: ["claude"] })
    void queryClient.invalidateQueries({ queryKey: ["models"] }) // the server clears his brain error once signed in
  }
  return (
    <Panel title="Claude model" icon={Asterisk}>
      {claude && !ready && (
        <div className="px-5 pb-4">
          <ol className="grid list-decimal gap-2 pl-5 text-base text-steel-200 marker:font-mono marker:text-rigel-300">
            {!claude.installed && (
              <li>
                Install Claude Code: <Code>curl -fsSL https://claude.ai/install.sh | bash</Code>
              </li>
            )}
            <li>
              Run <Code>claude</Code> in a terminal and sign in with the account that has your Pro or Max plan.
            </li>
            <li>Come back here: no restart needed.</li>
          </ol>
          <Button size="sm" className="mt-4" onClick={check}>
            Check again
          </Button>
        </div>
      )}
      <RadioGroup
        value={model}
        onValueChange={(next) => save(next as string)}
        aria-label="Claude model"
        className="grid gap-1.5 border-t px-5 pt-4 pb-4"
        data-testid="field-claude-model"
      >
        {Object.entries(lines).map(([id, line]) => (
          <Radio.Root
            key={id}
            value={id}
            render={<div />}
            className="flex cursor-pointer items-center gap-3 rounded-control bg-void-950/40 px-3.5 py-2.5 shadow-[inset_0_0_0_1px_var(--hairline)] outline-none transition-[background-color,box-shadow] hover:shadow-[inset_0_0_0_1px_var(--hairline-strong)] focus-visible:outline-2 focus-visible:outline-rigel-400 data-checked:bg-rigel-400/10 data-checked:shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_55%,transparent)]"
          >
            <Diamond />
            <span className="w-16 shrink-0 font-medium text-steel-100">{id}</span>
            <span className="text-sm text-steel-400">{line}</span>
          </Radio.Root>
        ))}
      </RadioGroup>
      <Note>
        Through Claude Code, which you sign in to yourself: Orion never sees your login. Your messages, what he
        remembers about you and his tools' results go to Anthropic; his ears, voice and memory search stay here.
      </Note>
    </Panel>
  )
}

// ---------- this machine ----------
const gb = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`

/**
 * This machine as his brain: the models here and downloading another, or how to install Ollama.
 * @param props the brain as GET /api/models describes it (`llm`), why that couldn't be read (`failed`), and `onRetry`
 * @returns the panel
 */
function LocalBrain({ llm, failed, onRetry }: { llm: Llm | undefined; failed: Error | null; onRetry: () => void }) {
  if (!llm)
    return (
      <Panel title="Models on this machine" icon={Cpu}>
        <div className="px-5 pb-4">
          {failed ? (
            <Failed title="His models can't be listed" error={failed} onRetry={onRetry} />
          ) : (
            <Pending label="Looking for models" delay={150} />
          )}
        </div>
      </Panel>
    )
  if (llm.ollama && !llm.ollama.installed)
    return (
      <Panel title="Models on this machine" icon={Cpu} data-testid="ollama-missing">
        <Note className="border-t-0 pt-0 text-base text-steel-200">
          Ollama runs the models here, and it isn't installed. In Orion's folder run{" "}
          <Code>./install.sh --local-brain</Code>, then come back here.
        </Note>
      </Panel>
    )
  const chat = llm.installed.filter((m) => !m.capabilities?.includes("embedding"))
  return (
    <Panel title="Models on this machine" icon={Cpu}>
      {chat.length > 0 ? (
        <LocalModels models={chat} />
      ) : (
        <Note className="border-t-0 pt-0">{llm.error || "No model here yet: download one below."}</Note>
      )}
      <PullModel />
    </Panel>
  )
}

/**
 * A model's size, quantisation and state in a line.
 * @param m the model
 * @returns for example `4B · Q4_K_M · 3.4 GB · loaded`
 */
function describe(m: InstalledModel) {
  const parts = [m.params, m.quant, m.size ? gb(m.size) : "", m.loaded ? "loaded" : ""]
  if (m.capabilities?.length && !m.capabilities.includes("tools")) parts.push("can't use his tools")
  return parts.filter(Boolean).join(" · ")
}

/**
 * The models on this machine: pick the one he thinks with, delete another.
 * @param props the `models`
 * @returns the list
 */
function LocalModels({ models }: { models: InstalledModel[] }) {
  const { value: current, save } = useSetting<string>("ollama.model")
  const remove = useDeleteModel()
  const askDelete = async (name: string) => {
    const text = "It's deleted from this machine; you can download it again later."
    if (await ask({ title: `Delete ${name}?`, text, ok: "Delete", danger: true, kicker: "Models" })) remove.mutate(name)
  }
  return (
    <RadioGroup
      value={current}
      onValueChange={(name) => save(name as string)}
      aria-label="His model"
      className="grid gap-1.5 px-5 pb-4"
      data-testid="field-ollama-model"
    >
      {models.map((m) => (
        <div
          key={m.name}
          className="flex items-center rounded-control bg-void-950/40 shadow-[inset_0_0_0_1px_var(--hairline)] has-data-checked:bg-rigel-400/10 has-data-checked:shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_55%,transparent)]"
        >
          <Radio.Root
            value={m.name}
            render={<div />}
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-3 rounded-control px-3.5 py-2.5 outline-none focus-visible:outline-2 focus-visible:outline-rigel-400"
            data-testid={`model-${m.name}`}
          >
            <Diamond />
            <span className="min-w-0">
              <span className="block truncate font-mono text-base text-steel-50">{m.name}</span>
              <span className="block text-xs text-steel-400">{describe(m)}</span>
            </span>
          </Radio.Root>
          {m.name !== current && (
            <IconButton label={`Delete ${m.name}`} className="mr-1" onClick={() => askDelete(m.name)}>
              <Trash2 />
            </IconButton>
          )}
        </div>
      ))}
    </RadioGroup>
  )
}

/**
 * Download a model by name, with each download's progress (from the model_pull events).
 * @returns the row and the progress bars
 */
function PullModel() {
  const [name, setName] = useState("")
  const pull = usePullModel()
  const pulls = useLive((s) => s.pulls)
  const shown = Object.values(pulls).filter((p) => p.status !== "done")
  const start = (e: FormEvent) => {
    e.preventDefault()
    if (name.trim()) pull.mutate(name.trim(), { onSuccess: () => setName("") })
  }
  return (
    <>
      <Row label="Download a model" hint="Any name from ollama.com/library. He needs one that can use tools.">
        <form onSubmit={start} className="flex w-full gap-2 @lg:w-80">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="qwen3.5:9b"
            aria-label="Model to download"
            className="min-w-0 flex-1 font-mono"
            data-testid="pull-name"
          />
          <Button type="submit" disabled={!name.trim() || pull.isPending}>
            <Download aria-hidden="true" />
            Download
          </Button>
        </form>
      </Row>
      {shown.length > 0 && (
        <div className="grid gap-3 border-t px-5 py-4">
          {shown.map((p) => (
            <PullProgress key={p.name} pull={p} />
          ))}
        </div>
      )}
    </>
  )
}

/**
 * One download: its name, Ollama's words for where it is, and a bar; a failed one says why until dismissed.
 * @param props the `pull`
 * @returns the progress
 */
function PullProgress({ pull }: { pull: ModelPull }) {
  const failed = pull.status === "error"
  const percent = pull.total ? Math.round(((pull.completed ?? 0) / pull.total) * 100) : null
  const dismiss = () =>
    useLive.setState((s) => {
      const { [pull.name]: _gone, ...rest } = s.pulls
      return { pulls: rest }
    })
  return (
    <Progress.Root value={failed ? null : percent} className="grid gap-1.5" data-testid={`pull-${pull.name}`}>
      <div className="flex items-baseline gap-3 text-sm">
        <Progress.Label className="font-mono text-steel-100">{pull.name}</Progress.Label>
        <span className={cn("min-w-0 flex-1 truncate", failed ? "text-alert-300" : "text-steel-400")}>
          {failed ? pull.error || "failed" : pull.status}
        </span>
        {percent !== null && !failed && <span className="font-mono text-steel-200 tabular-nums">{percent}%</span>}
        {failed && (
          <IconButton label="Dismiss" size="xs" onClick={dismiss}>
            <X />
          </IconButton>
        )}
      </div>
      {!failed && (
        <Progress.Track className="h-1.5 overflow-hidden rounded-full bg-steel-50/8">
          <Progress.Indicator className="h-full rounded-full bg-linear-to-r from-rigel-600 to-rigel-400 transition-[width] duration-(--dur-slow)" />
        </Progress.Track>
      )}
    </Progress.Root>
  )
}

// ---------- OpenRouter ----------
const SHOWN = 50 // models in the list at once: the search finds the rest

/**
 * OpenRouter as his brain: the key and the model.
 * @returns the panel
 */
function OpenRouterBrain() {
  return (
    <Panel title="OpenRouter key and model" icon={Route}>
      <Field path="openrouter.api_key" hint="Make one at openrouter.ai/keys. Free models need no credit." />
      <OpenRouterModels />
      <Note>
        Your messages, what he remembers about you and his tools' results go to OpenRouter and the model's maker; his
        ears, voice and memory search stay on his machine.
      </Note>
    </Panel>
  )
}

/**
 * How much a model can read at once.
 * @param tokens its context in tokens
 * @returns for example `262K` or `1M`
 */
const context = (tokens: number) =>
  tokens >= 1e6 ? `${(tokens / 1e6).toFixed(1).replace(/\.0$/, "")}M` : `${Math.round(tokens / 1000)}K`

/**
 * OpenRouter's models that can use tools, searchable; an id typed in full can be used even if it isn't listed.
 * @returns the model picker
 */
function OpenRouterModels() {
  const models = useOpenRouterModels()
  const { value: current = "", save } = useSetting<string>("openrouter.model")
  const [query, setQuery] = useState("")
  const q = query.trim().toLowerCase()
  const all = models.data?.models ?? []
  const found = all.filter((m) => !q || `${m.id} ${m.name}`.toLowerCase().includes(q))
  const typed = q.includes("/") && !all.some((m) => m.id.toLowerCase() === q) ? query.trim() : ""
  return (
    <div className="border-t px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="font-medium text-base text-steel-50">Model</p>
        <p className="min-w-0 truncate text-sm text-steel-400">
          Now <span className="font-mono text-steel-200">{current}</span>
        </p>
      </div>
      <div className="relative mt-3">
        <Search aria-hidden="true" className="pointer-events-none absolute top-2.5 left-3 size-4 text-steel-500" />
        <Input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search, e.g. gemma, qwen, free…"
          aria-label="Search OpenRouter's models"
          className="pl-9 [&::-webkit-search-cancel-button]:appearance-none"
          data-testid="openrouter-search"
        />
      </div>
      {models.isError ? (
        <Failed
          title="OpenRouter's models can't be listed"
          error={models.error}
          onRetry={() => void models.refetch()}
          className="mt-3"
        />
      ) : models.isPending ? (
        <Pending label="Asking OpenRouter" delay={150} className="mt-3" />
      ) : (
        <RadioGroup
          value={current}
          onValueChange={(id) => save(id as string)}
          aria-label="OpenRouter model"
          className="mt-2 grid max-h-80 overflow-y-auto rounded-control bg-void-950/40 shadow-[inset_0_0_0_1px_var(--hairline)]"
          data-testid="field-openrouter-model"
        >
          {typed && <ModelRow model={{ id: typed, name: `Use “${typed}”`, context: 0, free: false, price: "" }} />}
          {found.slice(0, SHOWN).map((m) => (
            <ModelRow key={m.id} model={m} />
          ))}
          {!typed && !found.length && <p className="px-3.5 py-3 text-sm text-steel-400">No model matches.</p>}
          {found.length > SHOWN && (
            <p className="px-3.5 py-2.5 text-sm text-steel-400">
              {found.length - SHOWN} more: search to narrow the list.
            </p>
          )}
        </RadioGroup>
      )}
    </div>
  )
}

/**
 * One OpenRouter model: its name and id, how much it reads at once, and its price (or Free).
 * @param props the `model`
 * @returns the row
 */
function ModelRow({ model }: { model: OpenRouterModel }) {
  return (
    <Radio.Root
      value={model.id}
      render={<div />}
      className="flex cursor-pointer items-center gap-3 border-b px-3.5 py-2 outline-none last:border-b-0 hover:bg-steel-50/4 focus-visible:bg-steel-50/8 data-checked:bg-rigel-400/10"
    >
      <Diamond className="size-3.5" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-base text-steel-100 in-data-checked:text-steel-50">{model.name}</span>
        <span className="block truncate font-mono text-xs text-steel-500">{model.id}</span>
      </span>
      {model.context > 0 && (
        <span className="hidden shrink-0 font-mono text-xs text-steel-400 @md:inline">{context(model.context)}</span>
      )}
      {(model.free || model.price) && (
        <span
          className="w-24 shrink-0 text-right font-mono text-xs text-steel-300"
          title={model.free ? undefined : `${model.price}: what you send / what he writes`}
        >
          {model.free ? "Free" : model.price.replace(/ per million tokens$/, "")}
        </span>
      )}
    </Radio.Root>
  )
}

// ---------- the GPU ----------
/**
 * How much of the GPU's memory is in use.
 * @param props the `gpu`
 * @returns the meter
 */
function GpuMemory({ gpu }: { gpu: GpuStats }) {
  const gib = (mb: number) => (mb / 1024).toFixed(1)
  return (
    <Meter.Root
      value={gpu.mem_used}
      max={gpu.mem_total}
      aria-label="GPU memory in use"
      className="flex w-full items-center gap-4 @lg:w-80"
    >
      <Meter.Track className="h-1.5 flex-1 overflow-hidden rounded-full bg-steel-50/8">
        <Meter.Indicator className="h-full rounded-full bg-linear-to-r from-nebula-600 to-nebula-400" />
      </Meter.Track>
      <span className="w-24 shrink-0 text-right font-mono text-sm text-steel-100 tabular-nums">
        {gib(gpu.mem_used)} / {gib(gpu.mem_total)} GB
      </span>
    </Meter.Root>
  )
}
