/**
 * Gestures: what each hand gesture does, on the settings page of the gesture remote (a connector: a server that sends
 * intents, the ORION hands camera), not in Orion's own settings: they are the connector's. The camera first: its pause,
 * and a way to the Hands card (your hands as the Pi sees them, to find where it sees you and how it reads each shape);
 * then every gesture the remote recognises, by kind (hand shapes, motions, both hands), each with how to make it and
 * what it does: one of his features (the screen, the music and the volume, the microphone, a new conversation,
 * something to ask him, a skill, a timer) or any tool of any connector, so a gesture can open an app or switch on a
 * light. A hand shape can wait for a longer hold, and repeat while it is held. Every change saves at once (to
 * mcp_servers.<remote>.gestures in config.yaml); Back to the defaults forgets them. Only you choose these: he can
 * read them, never change them. A gesture's row lights up as he acts on it (red when he couldn't).
 */
import { Camera, Hand, type LucideIcon, Pause, Play, RotateCcw, Route, TriangleAlert, Waypoints } from "lucide-react"
import { motion } from "motion/react"
import { type ComponentProps, type ReactNode, useEffect, useRef } from "react"
import {
  byKind,
  freshBinding,
  holds,
  KINDS,
  type SimpleArg,
  simpleArgs,
  toolKey,
  withValue,
} from "@/cards/hands-view/bindings"
import { useGestures, useResetGestures, useSaveGestures, useTool } from "@/lib/api"
import { GESTURE_ICONS } from "@/lib/hands"
import { reducedMotion } from "@/lib/motion"
import { hex } from "@/lib/palette"
import { present } from "@/lib/stage"
import type { ActionParam, Connector, GestureBinding, GestureInfo, GestureRemote, GesturesPage } from "@/lib/types"
import { cn } from "@/lib/utils"
import { onEvent } from "@/lib/ws"
import { ask } from "@/ui/ask"
import { Button } from "@/ui/button"
import { Empty, Failed, Pending } from "@/ui/states"
import { Switch } from "@/ui/switch"
import { ARRIVE } from "../frame"
import { type Choice, Code, DraftInput, Note, Row, SelectBox } from "../settings/fields"

const KIND_ICONS: Record<string, LucideIcon> = { shape: Hand, motion: Route, two_hands: Hand }

/**
 * A gesture remote's gestures.
 * @param props the remote's `connector`
 * @returns its camera and its gestures
 */
export function GestureEditor({ connector }: { connector: Connector }) {
  const gestures = useGestures()
  if (gestures.isError)
    return <Failed title="The gestures can't be read" error={gestures.error} onRetry={() => void gestures.refetch()} />
  if (!gestures.data) return <Pending label="Asking the gesture camera" delay={150} page />
  const page = gestures.data
  const remote = page.remotes.find((r) => r.name === connector.id)
  if (!remote)
    return (
      <div className="glass">
        <Empty icon={<Hand />}>
          This connector hasn't said which gestures it recognises yet. Once it is connected, they are listed here.
        </Empty>
      </div>
    )
  return (
    <motion.div variants={ARRIVE}>
      <div className="glass @container overflow-hidden" data-testid={`gestures-${remote.name}`}>
        <CameraPart connector={connector} remote={remote} page={page} />
        {byKind(remote.gestures, remote.bindings).map(({ kind, gestures: listed }) => (
          <KindPart key={kind || "other"} kind={kind} gestures={listed} remote={remote} page={page} />
        ))}
        <Defaults remote={remote} />
      </div>
    </motion.div>
  )
}

/**
 * One part of a remote's block: a line with its icon and title (and an action at its end), then its rows.
 * @param props the `title`, `icon`, an `action` and the rows (`children`)
 * @returns the part
 */
function Part({
  title,
  icon: Icon,
  action,
  children,
  className,
  ...rest
}: { title: string; icon: LucideIcon; action?: ReactNode } & ComponentProps<"section">) {
  return (
    <section className={cn("border-t first:border-t-0", className)} {...rest}>
      <header className="flex min-h-14 items-center gap-3 py-1.5 pr-4 pl-5">
        <span className="grid size-8 shrink-0 place-items-center rounded-control bg-steel-50/6 text-rigel-300 shadow-[inset_0_0_0_1px_var(--hairline)]">
          <Icon aria-hidden="true" className="size-4" />
        </span>
        <h3 className="min-w-0 flex-1 font-medium text-md text-steel-50">{title}</h3>
        {action}
      </header>
      {children}
    </section>
  )
}

/**
 * The camera: a way to its live view (the Hands card) and its pause.
 * @param props the remote's `connector`, its gesture `remote` and the `page`
 * @returns the part
 */
function CameraPart({ connector, remote, page }: { connector: Connector; remote: GestureRemote; page: GesturesPage }) {
  const { run, pending } = useTool()
  const own = connector.tools.map((t) => t.name)
  const pausable = own.includes("hands_pause") && own.includes("hands_resume")
  const enabled = connector.status !== "off"
  const connected = connector.status === "connected"
  const control = (tool: string, args: Record<string, unknown>) =>
    run(connector.id, tool, args, { title: "Gestures", success: true })
  const arm = Object.entries(remote.bindings).find(([, b]) => b.do === "arm")
  return (
    <Part
      title={connector.title}
      icon={Camera}
      data-testid="gestures-camera"
      action={
        enabled && (
          <Button size="sm" onClick={() => present("hands", { go: true })} data-testid="gestures-show-hands">
            <Hand aria-hidden="true" />
            Show my hands
          </Button>
        )
      }
    >
      <p className="px-5 pb-3 text-sm text-steel-400">
        Show the camera a gesture and he does what you chose for it: any of his features, or any tool of any server.
        Listening stays with his name.
      </p>
      {!enabled && <Note>Switched off: the camera on the Pi stays off, and gestures do nothing.</Note>}
      {pausable && enabled && (
        <Row
          label="Pause"
          hint={`Gestures off (and the camera) for ${page.pause_minutes} minutes, or until you resume.`}
        >
          <div className="flex gap-2">
            <Button
              disabled={pending || !connected}
              onClick={() => void control("hands_pause", { minutes: page.pause_minutes })}
            >
              <Pause aria-hidden="true" />
              Pause
            </Button>
            <Button disabled={pending || !connected} onClick={() => void control("hands_resume", {})}>
              <Play aria-hidden="true" />
              Resume
            </Button>
          </div>
        </Row>
      )}
      {arm && (
        <Note>
          Gestures sleep until you make the {arm[0].replaceAll("_", " ")}; each gesture then keeps them awake.
        </Note>
      )}
    </Part>
  )
}

/**
 * One kind of gesture: its gestures, each with what it does.
 * @param props the `kind`, its `gestures`, the `remote` and the `page`
 * @returns the part
 */
function KindPart({
  kind,
  gestures,
  remote,
  page,
}: {
  kind: string
  gestures: GestureInfo[]
  remote: GestureRemote
  page: GesturesPage
}) {
  const info = KINDS.find((k) => k.kind === kind)
  const save = useSaveGestures()
  // the whole set, with null for a gesture that now does nothing (so a wrong entry in config.yaml that couldn't be
  // shown stays there until its own gesture is changed here)
  const set = (gesture: string, binding: GestureBinding | null) =>
    save.mutate({ server: remote.name, bindings: { ...remote.bindings, [gesture]: binding } })
  return (
    <Part
      title={info?.title ?? "Other gestures"}
      icon={KIND_ICONS[kind] ?? Waypoints}
      data-testid={`gestures-${kind || "other"}`}
    >
      {info && <p className="px-5 pb-3 text-sm text-steel-400">{info.hint}</p>}
      {gestures.map((gesture) => (
        <GestureRow
          key={gesture.name}
          gesture={gesture}
          binding={remote.bindings[gesture.name]}
          problem={remote.problems[gesture.name]}
          remote={remote}
          page={page}
          onChange={(binding) => set(gesture.name, binding)}
        />
      ))}
    </Part>
  )
}

/**
 * One gesture: its name and how to make it, and what it does.
 * @param props the `gesture`, its `binding`, a `problem` with the binding in config.yaml, the `remote`, the `page`,
 *   `onChange`
 * @returns the row
 */
function GestureRow({
  gesture,
  binding,
  problem,
  remote,
  page,
  onChange,
}: {
  gesture: GestureInfo
  binding: GestureBinding | undefined
  problem?: string
  remote: GestureRemote
  page: GesturesPage
  onChange: (binding: GestureBinding | null) => void
}) {
  const Icon = GESTURE_ICONS[gesture.name] ?? Hand
  const row = useRef<HTMLDivElement>(null)
  useEffect(
    () =>
      onEvent("intent", (ev) => {
        if (ev.source !== remote.name || ev.gesture !== gesture.name || ev.again || reducedMotion()) return
        const lit = hex(ev.ok === false ? "alert-400" : "betel-400")
        row.current?.animate(
          [
            { boxShadow: `inset 3px 0 0 ${lit}, inset 0 0 28px color-mix(in oklch, ${lit} 22%, transparent)` },
            { boxShadow: "inset 0 0 0 transparent, inset 0 0 0 transparent" },
          ],
          { duration: 1100, easing: "ease-out" },
        )
      }),
    [remote.name, gesture.name],
  )
  return (
    <Row
      ref={row}
      label={
        <span className="flex items-center gap-2.5">
          <Icon
            aria-hidden="true"
            className={cn("size-[18px] shrink-0", binding ? "text-betel-300" : "text-steel-500")}
          />
          {gesture.title}
        </span>
      }
      hint={
        <>
          {gesture.how}
          {problem && (
            <span className="mt-1 flex items-start gap-1.5 text-alert-300">
              <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
              In config.yaml, not used: {problem}
            </span>
          )}
        </>
      }
    >
      <Binder gesture={gesture} binding={binding} remote={remote} page={page} onChange={onChange} />
    </Row>
  )
}

/**
 * What a gesture does: the action, its options, and for a hand shape its hold and whether it repeats.
 * @param props the `gesture`, its `binding`, the `remote`, the `page` and `onChange`
 * @returns the controls
 */
function Binder({
  gesture,
  binding,
  remote,
  page,
  onChange,
}: {
  gesture: GestureInfo
  binding: GestureBinding | undefined
  remote: GestureRemote
  page: GesturesPage
  onChange: (binding: GestureBinding | null) => void
}) {
  // a gesture the remote hasn't described (it never said yet) may be a shape: its hold shows when it has one
  const shape = gesture.kind === "shape" || (!gesture.kind && Boolean(binding?.hold || binding?.repeat))
  const spec = binding ? page.actions[binding.do] : undefined
  const actions: Choice[] = [
    { value: "", label: "Nothing" },
    ...Object.entries(page.actions)
      .filter(([name, action]) => action.start || name === binding?.do) // one with nothing to start from waits
      .map(([name, action]) => ({ value: name, label: action.title, detail: action.group })),
  ]
  const change = (patch: Partial<GestureBinding>) => binding && onChange({ ...binding, ...patch } as GestureBinding)
  const tool =
    binding?.do === "tool" ? page.tools.find((t) => t.server === binding.server && t.name === binding.tool) : undefined
  return (
    <div className="grid w-full gap-2 @lg:w-80" data-testid={`binding-${gesture.name}`}>
      <SelectBox
        value={binding?.do ?? ""}
        choices={actions}
        onChange={(value) =>
          onChange(value ? freshBinding(String(value), page.actions[String(value)], binding, shape) : null)
        }
        id={`binding-action-${gesture.name}`}
        label={`What ${gesture.title} does`}
      />
      {binding &&
        spec?.params.map((param) => (
          <Option key={param.name} param={param} binding={binding} page={page} onChange={change} />
        ))}
      {tool?.destructive && (
        <p className="flex items-start gap-1.5 text-xs text-alert-300">
          <TriangleAlert aria-hidden="true" className="mt-px size-3.5 shrink-0" />
          This tool changes things on his machine, and a gesture runs it without asking.
        </p>
      )}
      {binding && shape && (
        <div className="flex items-center gap-3">
          <SelectBox
            value={binding.hold ?? 0}
            choices={holds(remote, binding.hold).map((s) => ({
              value: s,
              label: s ? `Held ${s} s` : "As soon as it counts",
            }))}
            onChange={(value) => change({ hold: Number(value) || undefined })}
            id={`binding-hold-${gesture.name}`}
            label="How long to hold it"
            className="flex-1"
          />
          {(spec?.repeats || binding.do === "tool" || binding.repeat) && (
            // biome-ignore lint/a11y/noLabelWithoutControl: the switch inside is the control
            <label className="flex shrink-0 items-center gap-2 text-sm text-steel-300">
              <Switch
                size="sm"
                checked={binding.repeat === true}
                onCheckedChange={(on) => change({ repeat: on || undefined })}
                data-testid={`binding-repeat-${gesture.name}`}
              />
              Repeat
            </label>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * One option of a binding's action, with the control its kind calls for.
 * @param props the `param`, the `binding`, the `page` and `onChange` (a patch of the binding)
 * @returns the control
 */
function Option({
  param,
  binding,
  page,
  onChange,
}: {
  param: ActionParam
  binding: GestureBinding
  page: GesturesPage
  onChange: (patch: Partial<GestureBinding>) => void
}) {
  const value = binding[param.name]
  const pick = (choices: Choice[]) => (
    <SelectBox
      value={value as Choice["value"]}
      choices={choices}
      onChange={(v) => onChange({ [param.name]: v })}
      label={param.name}
    />
  )
  switch (param.kind) {
    case "panels":
      return (
        <Parts
          parts={page.parts}
          chosen={Array.isArray(value) ? (value as string[]) : []}
          empty={param.required ? undefined : "Everything"}
          onChange={(parts) => onChange({ [param.name]: parts })}
        />
      )
    case "panel":
      return pick(page.parts)
    case "choice":
    case "minutes":
    case "seconds":
      return pick(withValue(param.choices, value))
    case "skill":
      return pick(page.skills.map((s) => ({ value: s, label: s })))
    case "text":
      return (
        <DraftInput
          value={String(value ?? "")}
          onCommit={(text) => {
            if (!text.trim()) return false
            onChange({ [param.name]: text })
            return true
          }}
          placeholder="What to ask him"
          aria-label="What to ask him"
          className="@lg:w-80"
        />
      )
    case "tool":
      return (
        <SelectBox
          value={toolKey(String(binding.server ?? ""), String(value ?? ""))}
          choices={page.tools.map((t) => ({
            value: toolKey(t.server, t.name),
            label: t.title || t.name,
            detail: `${t.server} · ${t.description}`,
          }))}
          onChange={(v) => {
            const [server, ...rest] = String(v).split("/")
            onChange({ server, tool: rest.join("/"), args: {} })
          }}
          label="The tool"
        />
      )
    case "args":
      return <Args binding={binding} page={page} onChange={(args) => onChange({ args })} />
    default:
      return null // "server": chosen with the tool
  }
}

/**
 * A tool's arguments: each plain one with its own box, or the lot as JSON.
 * @param props the `binding`, the `page` and `onChange`
 * @returns the controls
 */
function Args({
  binding,
  page,
  onChange,
}: {
  binding: GestureBinding
  page: GesturesPage
  onChange: (args: Record<string, unknown>) => void
}) {
  const tool = page.tools.find((t) => t.server === binding.server && t.name === binding.tool)
  const args = (binding.args && typeof binding.args === "object" ? binding.args : {}) as Record<string, unknown>
  const plain = simpleArgs(tool)
  if (!plain)
    return (
      <DraftInput
        value={JSON.stringify(args)}
        onCommit={(text) => {
          try {
            const parsed = JSON.parse(text || "{}")
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return false
            onChange(parsed)
            return true
          } catch {
            return false
          }
        }}
        placeholder='{"name": "value"}'
        className="font-mono @lg:w-80"
        aria-label="The tool's arguments, as JSON"
      />
    )
  if (!plain.length) return null
  const set = (name: string, v: unknown) => {
    const next = { ...args }
    if (v === "" || v === undefined) delete next[name]
    else next[name] = v
    onChange(next)
  }
  return (
    <div className="grid gap-1.5">
      {plain.map((arg) => (
        <ArgControl key={arg.name} arg={arg} value={args[arg.name]} onChange={(v) => set(arg.name, v)} />
      ))}
    </div>
  )
}

/**
 * One plain argument of a tool.
 * @param props the `arg`, its `value` and `onChange`
 * @returns the control
 */
function ArgControl({ arg, value, onChange }: { arg: SimpleArg; value: unknown; onChange: (v: unknown) => void }) {
  const label = (control: ReactNode) => (
    // biome-ignore lint/a11y/noLabelWithoutControl: the control is inside
    <label className="grid gap-1 text-xs text-steel-400" title={arg.description}>
      <span className="font-mono">
        {arg.name}
        {arg.required && " *"}
      </span>
      {control}
    </label>
  )
  if (arg.type === "boolean")
    return label(<Switch size="sm" checked={value === true} onCheckedChange={(on) => onChange(on)} />)
  if (arg.choices)
    return label(
      <SelectBox
        value={(value as string) ?? ""}
        choices={[{ value: "", label: "—" }, ...arg.choices.map((c) => ({ value: c, label: c }))]}
        onChange={(v) => onChange(v || undefined)}
      />,
    )
  return label(
    <DraftInput
      value={value === undefined ? "" : String(value)}
      onCommit={(text) => {
        if (arg.type === "string") {
          onChange(text)
          return true
        }
        if (!text.trim()) {
          onChange(undefined)
          return true
        }
        const n = Number(text)
        if (!Number.isFinite(n) || (arg.type === "integer" && !Number.isInteger(n))) return false
        onChange(n)
        return true
      }}
      placeholder={arg.description.slice(0, 60)}
      className="@lg:w-80"
    />,
  )
}

/**
 * Which parts of the HUD: a toggle per part (none at all may mean everything).
 * @param props the `parts` to choose from, those `chosen`, what none means (`empty`), and `onChange`
 * @returns the toggles
 */
function Parts({
  parts,
  chosen,
  empty,
  onChange,
}: {
  parts: GesturesPage["parts"]
  chosen: string[]
  empty?: string
  onChange: (parts: string[]) => void
}) {
  const shown = parts.filter((p) => !p.value.startsWith("settings/"))
  return (
    <fieldset className="m-0 flex min-w-0 flex-wrap gap-1.5 border-0 p-0" aria-label="Parts of the HUD">
      {shown.map((p) => {
        const on = chosen.includes(p.value)
        return (
          <button
            key={p.value}
            type="button"
            aria-pressed={on}
            onClick={() => {
              const next = on ? chosen.filter((c) => c !== p.value) : [...chosen, p.value]
              if (next.length || empty) onChange(next)
            }}
            className={cn(
              "h-7 rounded-full px-2.5 text-xs outline-none transition-colors focus-visible:outline-2 focus-visible:outline-rigel-400",
              on
                ? "bg-rigel-400/16 text-rigel-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_45%,transparent)]"
                : "bg-steel-50/5 text-steel-400 shadow-[inset_0_0_0_1px_var(--hairline)] hover:text-steel-100",
            )}
          >
            {p.label}
          </button>
        )
      })}
      {empty && !chosen.length && <span className="self-center text-xs text-steel-500">{empty}</span>}
    </fieldset>
  )
}

/**
 * Back to the defaults, for a remote whose bindings you chose.
 * @param props the `remote`
 * @returns the button and where they are kept
 */
function Defaults({ remote }: { remote: GestureRemote }) {
  const reset = useResetGestures()
  const askReset = async () => {
    const text = "Every gesture does what it did at first again; what you chose here is forgotten."
    if (await ask({ title: "Back to the defaults?", text, ok: "Reset", danger: true, kicker: "Gestures" }))
      reset.mutate(remote.name)
  }
  return (
    <div className="flex flex-wrap items-center gap-3 border-t px-5 py-4">
      <Button disabled={!remote.custom || reset.isPending} onClick={() => void askReset()} data-testid="gestures-reset">
        <RotateCcw aria-hidden="true" />
        Back to the defaults
      </Button>
      <p className="text-sm text-steel-500">
        {remote.custom ? "Yours, kept" : "The defaults; a change is kept"} in config.yaml under{" "}
        <Code>{`mcp_servers.${remote.name}.gestures`}</Code>.
      </p>
    </div>
  )
}
