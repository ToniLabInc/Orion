/**
 * The settings pages' building blocks.
 *
 * `<Field path="voice.chime" />` draws an option of config.yaml from the server's schema (GET /api/settings, `schema`):
 * the control its kind calls for (a switch; a slider, or a number box for a wide range; a segmented control for a few
 * choices, a select for more; a text box; a write-only secret; chips for a list; an audio device), its label and help,
 * a Restart tag when it only takes effect after one, and Saved ✓ for a moment once the server says it changed. Every
 * change saves the moment it is made, through useSaveSettings: the page shows it at once and puts the old value back,
 * with a toast, if Orion refuses it. Nothing here knows a default or a range of its own: they all come from the schema.
 *
 * The rest are the pieces the pages share: glass panels, rows, and the small controls.
 */
import { Field as BaseField } from "@base-ui/react/field"
import { Check, ChevronDown, Copy, type LucideIcon, X } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { type ComponentProps, type FormEvent, type ReactNode, type Ref, useEffect, useState } from "react"
import { create } from "zustand"
import { useSaveSettings, useSettings } from "@/lib/api"
import { FOLD, POP } from "@/lib/motion"
import type { FieldSpec } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Button, IconButton } from "@/ui/button"
import { Input } from "@/ui/input"
import { type Choice, SelectBox } from "@/ui/select"
import { Slider } from "@/ui/slider"
import { Switch } from "@/ui/switch"
import { Segmented } from "@/ui/tabs"
import { toast } from "@/ui/toast"
import { ARRIVE } from "../frame"

export type { Choice }
export { SelectBox }

const SAVED_MS = 1400 // how long Saved ✓ shows after a change
const MAX_STEPS = 100 // a wider range gets a number box instead of a slider

/** A value as a field shows it (a slider's figure, a choice's label), instead of the schema's way. */
export type Format = (value: string | number) => string

/**
 * The test id of an option's control.
 * @param path its dotted key
 * @returns for example `field-voice-chime`
 */
const testId = (path: string) => `field-${path.replace(".", "-")}`

/**
 * The schema's help (written for config.yaml) as a sentence for the page: a capital, a full stop, and without the
 * "[restart]" mark (the Restart tag says it).
 * @param help the schema's help
 * @returns the sentence, or "" for none
 */
export function sentence(help: string): string {
  const text = help.replace(/\s*\[restart\]\s*$/i, "").trim()
  if (!text) return ""
  const capital = text[0].toUpperCase() + text.slice(1)
  return /[.!?]$/.test(capital) ? capital : `${capital}.`
}

/** When each key last saved with a change (Date.now()), for its Saved tick. */
const useSaved = create<Record<string, number>>(() => ({}))

/**
 * An option of config.yaml: its value, its schema, and a function that saves a new value (Saved ✓ follows once the
 * server says it changed).
 * @param path its dotted key, for example `voice.chime`
 * @returns `value` and `spec` (both undefined until the settings come) and `save`
 */
export function useSetting<T = unknown>(path: string) {
  const settings = useSettings().data
  const { mutate } = useSaveSettings()
  const dot = path.indexOf(".")
  const section = settings?.config[path.slice(0, dot)] as Record<string, unknown> | undefined
  const save = (value: T) =>
    mutate(
      { [path]: value },
      { onSuccess: ({ changed }) => useSaved.setState(Object.fromEntries(changed.map((key) => [key, Date.now()]))) },
    )
  return { value: section?.[path.slice(dot + 1)] as T | undefined, spec: settings?.schema[path], save }
}

// ---------- fields ----------
interface FieldProps {
  /** The option's dotted key. */
  path: string
  /** A label instead of the schema's (where a heading above already says what it is). */
  label?: ReactNode
  /** Words under the label instead of the schema's help. */
  hint?: ReactNode
  /** Choices instead of the schema's (to give them lines under them). */
  choices?: Choice[]
  /** How to show a value instead of the schema's way. */
  format?: Format
}

/**
 * One option, drawn from the schema; nothing for an option this Orion doesn't have (or before the settings come).
 * @param props the option's `path`, and optionally a `label`, a `hint`, `choices` or a `format` of its own
 * @returns the option's row
 */
export function Field({ path, label: named, hint, choices, format }: FieldProps) {
  const { value, spec, save } = useSetting<unknown>(path)
  const where = useSettings().data?.secrets[path] ?? ""
  if (!spec) return null
  const id = testId(path)
  let control: ReactNode
  if (spec.kind === "bool")
    control = <Switch checked={value === true} onCheckedChange={(on) => save(on)} data-testid={id} />
  else if (spec.kind === "int" || spec.kind === "float")
    control = <NumberControl spec={spec} value={value} format={format} onSave={save} id={id} />
  else if (spec.kind === "choice")
    control = <ChoiceControl spec={spec} value={value} choices={choices} format={format} onSave={save} id={id} />
  else if (spec.kind === "device")
    control = (
      <SelectBox
        value={(value ?? null) as Choice["value"]}
        choices={deviceChoices(spec)}
        onChange={save}
        id={id}
        label={spec.label}
        className="@lg:w-72"
      />
    )
  else if (spec.kind === "secret") control = <SecretInput label={spec.label} where={where} onSave={save} id={id} />
  else if (spec.kind === "list")
    control = (
      <ListInput
        spec={spec}
        value={value}
        onSave={save}
        id={id}
        wide={Array.isArray(value) && value.length > LONG_LIST}
      />
    )
  else
    control = (
      <DraftInput
        value={String(value ?? "")}
        placeholder={typeof spec.default === "string" ? spec.default : undefined}
        onCommit={(text) => {
          save(text.trim())
          return true
        }}
        aria-label={spec.label}
        data-testid={id}
      />
    )
  const label =
    named ??
    (spec.kind === "secret" ? (
      <>
        {spec.label}
        <SecretTag where={where} />
      </>
    ) : (
      spec.label
    ))
  return (
    <Row
      label={label}
      hint={hint ?? sentence(spec.help)}
      restart={spec.restart}
      saved={path}
      inline={spec.kind === "bool"}
      stacked={spec.kind === "list" && Array.isArray(value) && value.length > LONG_LIST}
    >
      {control}
    </Row>
  )
}

/**
 * Several options in a row, each from the schema.
 * @param props the options' dotted keys (`paths`)
 * @returns their rows
 */
export function Fields({ paths }: { paths: readonly string[] }) {
  return paths.map((path) => <Field key={path} path={path} />)
}

/**
 * The options of a section his schema offers here, in config.yaml's order (all of them but those named), each from
 * the schema: a part of Orion shows every option it lets the HUD set, and no more.
 * @param section the section ("backup", "instant")
 * @param except dotted keys shown elsewhere
 * @returns the dotted keys
 */
export function useSection(section: string, except: readonly string[] = []): string[] {
  const schema = useSettings().data?.schema
  return Object.keys(schema ?? {}).filter((key) => key.startsWith(`${section}.`) && !except.includes(key))
}

/**
 * A section's options, each from the schema (see `useSection`).
 * @param props the `section` and the keys shown elsewhere (`except`)
 * @returns their rows
 */
export function SectionFields({ section, except }: { section: string; except?: readonly string[] }) {
  return <Fields paths={useSection(section, except)} />
}

/**
 * Whether this Orion shows any of these options (his schema leaves out the ones only config.yaml sets): a panel of
 * nothing but those isn't drawn.
 * @param paths the options' dotted keys
 * @returns true when at least one is in the schema
 */
export function useShows(paths: readonly string[]): boolean {
  const schema = useSettings().data?.schema
  return paths.some((path) => schema?.[path] !== undefined)
}

/**
 * A number as its field shows it: with the decimals its step needs, and its unit.
 * @param spec the option's schema
 * @param value the number
 * @returns for example `1.05×`, `700 ms` or `12,000 chars`
 */
function numberText(spec: FieldSpec, value: number): string {
  const digits = spec.kind === "float" ? (String(spec.step ?? 0.01).split(".")[1] ?? "").length : 0
  const text = value.toLocaleString("en", { minimumFractionDigits: digits, maximumFractionDigits: digits })
  if (!spec.unit) return text
  return spec.unit === "×" ? `${text}×` : spec.unit === "$" ? `$${text}` : `${text} ${spec.unit}`
}

/** A list longer than this goes under its label with the room of the whole row, not beside it. */
const LONG_LIST = 4

const first = (value: number | readonly number[]) => (typeof value === "number" ? value : value[0])

/**
 * A number: a slider showing its value while you drag (saved when you let go, or with each arrow key), or a number
 * box when the range is too wide to slide through.
 * @param props the option's `spec` and `value`, a `format` of its own, `onSave` and the test `id`
 * @returns the control
 */
function NumberControl({
  spec,
  value,
  format,
  onSave,
  id,
}: {
  spec: FieldSpec
  value: unknown
  format?: Format
  onSave: (value: number) => void
  id: string
}) {
  const [moving, setMoving] = useState<number>()
  const { min, max, step = 1 } = spec
  if (min === undefined || max === undefined || (max - min) / step > MAX_STEPS)
    return (
      <span className="flex items-center gap-2">
        {spec.unit === "$" && <span className="text-sm text-steel-400">$</span>}
        <DraftInput
          value={value === undefined || value === null ? "" : String(value)}
          inputMode={spec.kind === "int" ? "numeric" : "decimal"}
          onCommit={(text) => {
            const number = Number(text)
            const fits = Number.isFinite(number) && number >= (min ?? -Infinity) && number <= (max ?? Infinity)
            if (!text.trim() || !fits || (spec.kind === "int" && !Number.isInteger(number))) return false
            onSave(number)
            return true
          }}
          aria-label={spec.label}
          className="font-mono @lg:w-32"
          data-testid={id}
        />
        {spec.unit && spec.unit !== "$" && <span className="text-sm text-steel-400">{spec.unit}</span>}
      </span>
    )
  const saved = typeof value === "number" ? value : min
  const shown = moving ?? saved
  return (
    <div className="flex w-full items-center gap-4 @lg:w-72">
      <Slider
        value={[shown]}
        min={min}
        max={max}
        step={step}
        aria-label={spec.label}
        onValueChange={(next) => setMoving(first(next))}
        onValueCommitted={(next) => {
          setMoving(undefined)
          if (first(next) !== saved) onSave(first(next))
        }}
        className="flex-1"
        data-testid={id}
      />
      <output className="w-20 shrink-0 text-right font-mono text-sm text-steel-100">
        {format ? format(shown) : numberText(spec, shown)}
      </output>
    </div>
  )
}

/**
 * One of a few choices as a segmented control, or of more as a select (which also keeps a value no longer offered).
 * @param props the option's `spec` and `value`, `choices` and a `format` of its own, `onSave` and the test `id`
 * @returns the control
 */
function ChoiceControl({
  spec,
  value,
  choices,
  format,
  onSave,
  id,
}: {
  spec: FieldSpec
  value: unknown
  choices?: Choice[]
  format?: Format
  onSave: (value: Choice["value"]) => void
  id: string
}) {
  const list = (choices ?? spec.choices ?? []).map((c) => ({
    ...c,
    label: format && c.value !== null ? format(c.value) : c.label,
  }))
  if (list.length <= 4 && !list.some((c) => "detail" in c && c.detail))
    return (
      <Segmented
        value={value as string | number | undefined}
        onChange={onSave}
        choices={list as { value: string | number; label: string }[]}
        label={spec.label}
        id={id}
      />
    )
  return (
    <SelectBox
      value={(value ?? null) as Choice["value"]}
      choices={list}
      onChange={onSave}
      id={id}
      label={spec.label}
      className="@lg:w-72"
    />
  )
}

/**
 * An audio device's choices: the system's default, the devices connected now, and the chosen one if it isn't.
 * @param spec the option's schema (its choices are the devices connected now)
 * @returns the choices
 */
function deviceChoices(spec: FieldSpec): Choice[] {
  return [{ value: null, label: "System default" }, ...(spec.choices ?? [])]
}

// ---------- layout ----------
/**
 * One topic of a page: a glass panel with an icon and a title (and actions on the right), then its rows.
 * @param props the `title`, the `icon`, an `action` for the header, a `description` under the title, and the
 *   section's own props
 * @returns the panel
 */
export function Panel({
  title,
  icon: Icon,
  action,
  description,
  children,
  className,
  ...rest
}: { title: string; icon: LucideIcon; action?: ReactNode; description?: ReactNode } & ComponentProps<"section">) {
  return (
    <motion.div variants={ARRIVE}>
      <div className={cn("glass @container overflow-hidden", className)}>
        <section {...rest}>
          <header className="flex min-h-14 items-center gap-3 px-5 pt-1">
            <span className="grid size-8 shrink-0 place-items-center rounded-control bg-steel-50/6 text-rigel-300 shadow-[inset_0_0_0_1px_var(--hairline)]">
              <Icon aria-hidden="true" className="size-4" />
            </span>
            <div className="min-w-0 flex-1">
              <h2 className="font-medium text-md text-steel-50">{title}</h2>
              {description && <p className="text-xs text-steel-500">{description}</p>}
            </div>
            {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
          </header>
          {children}
        </section>
      </div>
    </motion.div>
  )
}

/**
 * A panel that starts closed, for options most people never change.
 * @param props the `title`, the `icon` and its rows (`children`)
 * @returns the panel
 */
export function FoldPanel({
  title,
  icon: Icon,
  children,
  ...rest
}: { title: string; icon: LucideIcon; children: ReactNode } & ComponentProps<"section">) {
  const [open, setOpen] = useState(false)
  return (
    <motion.div variants={ARRIVE}>
      <div className="glass @container overflow-hidden">
        <section {...rest}>
          <h2>
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
              className="group/fold flex min-h-14 w-full items-center gap-3 px-5 text-left outline-none transition-colors hover:bg-steel-50/3 focus-visible:bg-steel-50/5"
            >
              <span className="grid size-8 shrink-0 place-items-center rounded-control bg-steel-50/6 text-steel-400 shadow-[inset_0_0_0_1px_var(--hairline)] group-aria-expanded/fold:text-rigel-300">
                <Icon aria-hidden="true" className="size-4" />
              </span>
              <span className="font-medium text-md text-steel-200 group-hover/fold:text-steel-50 group-aria-expanded/fold:text-steel-50">
                {title}
              </span>
              <ChevronDown
                aria-hidden="true"
                className="ml-auto size-4 text-steel-500 transition-transform duration-(--dur-base) ease-(--ease-expo) group-aria-expanded/fold:rotate-180"
              />
            </button>
          </h2>
          <AnimatePresence initial={false}>
            {open && (
              <motion.div {...FOLD} className="overflow-hidden">
                <div className="border-t">{children}</div>
              </motion.div>
            )}
          </AnimatePresence>
        </section>
      </div>
    </motion.div>
  )
}

/**
 * One option: its name (with a Restart tag if it waits for one, and Saved ✓ just after a change), a line of help, and
 * its control, beside them, or under them when the panel is narrow (a switch always stays beside).
 * @param props the `label`, the `hint`, whether it waits for a `restart`, the key whose Saved tick it shows (`saved`),
 *   whether the control stays `inline`, whether it goes `stacked` under them at every width (a long list), and the
 *   control (`children`)
 * @returns the row
 */
export function Row({
  label,
  hint,
  restart,
  saved,
  inline,
  stacked,
  children,
  ref,
}: {
  label: ReactNode
  hint?: ReactNode
  restart?: boolean
  saved?: string
  inline?: boolean
  stacked?: boolean
  children: ReactNode
  ref?: Ref<HTMLDivElement>
}) {
  return (
    <BaseField.Root
      ref={ref}
      className={cn(
        "relative flex gap-x-8 gap-y-3 border-t px-5 py-4 transition-colors duration-(--dur-instant) first-of-type:border-t-0 hover:bg-steel-50/[0.025] has-focus-visible:bg-steel-50/[0.04]",
        inline ? "items-center" : stacked ? "flex-col" : "flex-col @lg:flex-row @lg:items-center",
      )}
    >
      <div className="min-w-0 flex-1">
        <BaseField.Label className="flex flex-wrap items-center gap-x-2.5 gap-y-1 font-medium text-base text-steel-50">
          {label}
          {restart && <RestartTag />}
          {saved && <Saved path={saved} />}
        </BaseField.Label>
        {hint && <BaseField.Description className="mt-1 text-sm text-steel-400">{hint}</BaseField.Description>}
      </div>
      <div className={cn("flex min-w-0 shrink-0 items-center", !inline && (stacked ? "w-full" : "w-full @lg:w-auto"))}>
        {children}
      </div>
    </BaseField.Root>
  )
}

/** On an option that only takes effect after a restart (the top bar then offers one). */
export function RestartTag() {
  return (
    <span
      className="inline-flex h-5 shrink-0 items-center rounded-full bg-dusk-400/12 px-2 font-medium text-dusk-200 text-micro shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-dusk-400)_28%,transparent)]"
      title="Takes effect after a restart"
    >
      Restart
    </span>
  )
}

/**
 * Saved ✓ for a moment after the server says an option changed.
 * @param props the option's dotted key (`path`)
 * @returns the tick, or nothing
 */
export function Saved({ path }: { path: string }) {
  const at = useSaved((saved) => saved[path] ?? 0)
  const [shown, setShown] = useState(false)
  useEffect(() => {
    const left = at + SAVED_MS - Date.now()
    if (left <= 0) return
    setShown(true)
    const timer = setTimeout(() => setShown(false), left)
    return () => clearTimeout(timer)
  }, [at])
  return (
    <AnimatePresence>
      {shown && (
        <motion.span
          {...POP}
          role="status"
          className="flex shrink-0 items-center gap-1 font-medium text-micro text-ok-300"
        >
          <Check aria-hidden="true" className="size-3" />
          Saved
        </motion.span>
      )}
    </AnimatePresence>
  )
}

/**
 * A choice's mark: a small ring, filled inside the radio that is chosen.
 * @param props extra classes
 * @returns the mark
 */
export function Diamond({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "grid size-4 shrink-0 place-items-center rounded-full shadow-[inset_0_0_0_1.5px_var(--hairline-strong)] in-data-checked:shadow-[inset_0_0_0_1.5px_var(--color-rigel-400)] after:size-2 after:scale-0 after:rounded-full after:bg-rigel-400 after:transition-transform after:duration-(--dur-quick) in-data-checked:after:scale-100",
        className,
      )}
    />
  )
}

/**
 * A short note under a panel's rows.
 * @param props the words (`children`) and extra classes
 * @returns the note
 */
export function Note({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("border-t px-5 py-3.5 text-sm text-steel-400", className)}>{children}</p>
}

/**
 * A shell command or a file name inside help text.
 * @param props the code (`children`)
 * @returns the code
 */
export function Code({ children }: { children: ReactNode }) {
  return (
    <code className="whitespace-nowrap rounded-[5px] bg-steel-50/8 px-1.5 py-0.5 font-mono text-[0.92em] text-rigel-100">
      {children}
    </code>
  )
}

/**
 * Text with `backticks` shown as code (his own messages use them for commands).
 * @param props the `text`
 * @returns the text and its code
 */
export function WithCode({ text }: { text: string }) {
  const parts: ReactNode[] = []
  let at = 0
  for (const match of text.matchAll(/`([^`]+)`/g)) {
    parts.push(text.slice(at, match.index), <Code key={match.index}>{match[1]}</Code>)
    at = match.index + match[0].length
  }
  parts.push(text.slice(at))
  return parts
}

// ---------- controls ----------
/**
 * A text box that saves when you leave it or press Enter; Esc puts back what is saved.
 * @param props the saved `value`, `onCommit` (which says whether it took the text; if not, the saved value comes
 *   back), and an input's props
 * @returns the text box
 */
export function DraftInput({
  value,
  onCommit,
  className,
  ...props
}: { value: string; onCommit: (text: string) => boolean } & Omit<ComponentProps<"input">, "value" | "onChange">) {
  const [draft, setDraft] = useState(value)
  const [shown, setShown] = useState(value)
  if (shown !== value) {
    // saved elsewhere, or put back after a refusal
    setShown(value)
    setDraft(value)
  }
  return (
    <Input
      {...props}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        if (draft !== value && !onCommit(draft)) setDraft(value)
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur()
        else if (e.key === "Escape") setDraft(value)
      }}
      className={cn("w-full @lg:w-72", className)}
    />
  )
}

/** What a secret's tag says about where it comes from. */
const SECRET_WORDS = { vault: "In the vault", config: "Saved", env: "From his environment", "": "Not set" } as const

/**
 * A secret: never shown, only replaced (type a new one and save) or removed (SecretTag says whether one is set).
 * @param props the option's `label`, `where` the secret comes from now, `onSave` and the test `id`
 * @returns the control
 */
function SecretInput({
  label,
  where,
  onSave,
  id,
}: {
  label: string
  where: keyof typeof SECRET_WORDS
  onSave: (value: string) => void
  id: string
}) {
  const [text, setText] = useState("")
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!text.trim()) return
    onSave(text.trim())
    setText("")
  }
  const remove = async () => {
    const question = { title: `Remove the ${label}?`, text: "It is deleted for good.", ok: "Remove" }
    if (await ask({ ...question, danger: true, kicker: "Settings" })) onSave("")
  }
  return (
    <form onSubmit={submit} className="flex w-full flex-wrap items-center gap-2 @lg:w-80 @lg:flex-nowrap">
      <Input
        type="password"
        autoComplete="off"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={where ? "Type a new one to replace it" : "Paste it here"}
        aria-label={label}
        className="min-w-0 flex-1 font-mono"
        data-testid={id}
      />
      {text.trim() ? (
        <Button type="submit" tone="primary">
          Save
        </Button>
      ) : where === "config" || where === "vault" ? (
        <Button type="button" tone="ghost" onClick={remove}>
          Remove
        </Button>
      ) : null}
    </form>
  )
}

/**
 * Where a secret comes from now, beside its name.
 * @param props `where`: "vault", "config", "env" or "" (not set)
 * @returns the tag
 */
function SecretTag({ where }: { where: keyof typeof SECRET_WORDS }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 shrink-0 items-center rounded-full px-2 font-medium text-micro shadow-[inset_0_0_0_1px_var(--hairline)]",
        where ? "bg-ok-400/10 text-ok-200" : "bg-steel-50/6 text-steel-400",
      )}
    >
      {SECRET_WORDS[where]}
    </span>
  )
}

/**
 * A list as chips: × removes one; type one and press Enter (or leave the box) to add it. Numbers stay in order and
 * within the schema's range.
 * @param props the option's `spec` and `value`, `onSave` and the test `id`
 * @returns the control
 */
function ListInput({
  spec,
  value,
  onSave,
  id,
  wide,
}: {
  spec: FieldSpec
  value: unknown
  onSave: (value: (string | number)[]) => void
  id: string
  wide: boolean
}) {
  const items = Array.isArray(value) ? (value as (string | number)[]) : []
  const numbers = Array.isArray(spec.default) && typeof spec.default[0] === "number"
  const unit = spec.unit ? ` ${spec.unit}` : ""
  const [draft, setDraft] = useState("")
  const [wrong, setWrong] = useState(false)
  const add = () => {
    const text = draft.trim()
    if (!text) return
    const item = numbers ? Number(text) : text
    const fits =
      typeof item !== "number" ||
      (Number.isInteger(item) && item >= (spec.min ?? -Infinity) && item <= (spec.max ?? Infinity))
    if (!fits) return setWrong(true)
    setDraft("")
    if (items.includes(item)) return
    onSave(numbers ? [...items, item].sort((a, b) => Number(a) - Number(b)) : [...items, item])
  }
  return (
    <div
      className={cn("flex w-full flex-wrap items-center gap-1.5", !wide && "@lg:w-80 @lg:justify-end")}
      data-testid={id}
    >
      <AnimatePresence initial={false}>
        {items.map((item) => (
          <motion.span
            key={item}
            {...POP}
            layout
            className="flex h-8 items-center rounded-full bg-steel-50/6 pl-3 font-mono text-sm text-steel-100 shadow-[inset_0_0_0_1px_var(--hairline)]"
          >
            {item}
            {unit}
            <IconButton
              label={`Remove ${item}${unit}`}
              tip={false}
              size="xs"
              className="ml-0.5 rounded-full"
              onClick={() => onSave(items.filter((i) => i !== item))}
            >
              <X />
            </IconButton>
          </motion.span>
        ))}
      </AnimatePresence>
      <Input
        value={draft}
        inputMode={numbers ? "numeric" : "text"}
        placeholder="Add…"
        aria-label={`Add to ${spec.label.toLowerCase()}`}
        aria-invalid={wrong || undefined}
        onChange={(e) => {
          setDraft(e.target.value)
          setWrong(false)
        }}
        onKeyDown={(e) => {
          if (e.key !== "Enter") return
          e.preventDefault()
          add()
        }}
        onBlur={add}
        className="h-8 w-24 font-mono"
      />
    </div>
  )
}

/**
 * A folder or file on Orion's machine, to read or copy.
 * @param props the `path`, and a div's props
 * @returns the path with a copy button
 */
export function PathValue({ path, ...rest }: { path: string } & ComponentProps<"div">) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(path)
      setCopied(true)
      setTimeout(() => setCopied(false), SAVED_MS)
    } catch {
      toast.add({
        title: "Not copied",
        description: "The browser didn't allow it: select the path instead.",
        type: "warning",
      })
    }
  }
  return (
    <div
      className="flex w-full min-w-0 items-center rounded-control bg-void-950/55 shadow-[inset_0_0_0_1px_var(--hairline)] @lg:w-80"
      {...rest}
    >
      <code className="min-w-0 flex-1 select-all truncate px-3 py-2 font-mono text-sm text-steel-200" title={path}>
        {path}
      </code>
      <IconButton label={copied ? "Copied" : "Copy the path"} size="sm" tip={false} onClick={copy} className="mr-0.5">
        {copied ? <Check className="text-ok-400" /> : <Copy />}
      </IconButton>
    </div>
  )
}
