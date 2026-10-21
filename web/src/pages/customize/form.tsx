/**
 * A connector's own settings as a form: each field of its schema with the control its kind calls for (text, a secret
 * that is never shown, a choice, a switch, a number, a list of chips), its help under it and, when the form is saved
 * with something wrong, what is wrong. The form is a controlled draft (lib/connectors.ts); saving it is the owner's.
 */
import { X } from "lucide-react"
import { type ReactNode, useId, useState } from "react"
import type { Draft } from "@/lib/connectors"
import type { ConnectorField } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { IconButton } from "@/ui/button"
import { Input } from "@/ui/input"
import { SelectBox } from "@/ui/select"
import { Switch } from "@/ui/switch"
import { Prose } from "./parts"

/**
 * The fields.
 * @param props the `fields`, the `draft` and `onChange` (with the new draft), the `problems` to show by field, and
 *   whether the form is `disabled`
 * @returns the fields
 */
export function ConnectorForm({
  fields,
  draft,
  onChange,
  problems = {},
  disabled = false,
}: {
  fields: readonly ConnectorField[]
  draft: Draft
  onChange: (draft: Draft) => void
  problems?: Record<string, string>
  disabled?: boolean
}) {
  return (
    <div className="grid gap-4">
      {fields.map((f) => (
        <Labelled key={f.key} field={f} problem={problems[f.key]}>
          {(id) => (
            <Control
              id={id}
              field={f}
              value={draft[f.key]}
              invalid={Boolean(problems[f.key])}
              disabled={disabled}
              onChange={(value) => onChange({ ...draft, [f.key]: value })}
            />
          )}
        </Labelled>
      ))}
    </div>
  )
}

/**
 * A field's label, its control, its help and its problem.
 * @param props the `field`, its `problem`, and the control (given the id the label points at)
 * @returns the field
 */
function Labelled({
  field,
  problem,
  children,
}: {
  field: ConnectorField
  problem?: string
  children: (id: string) => ReactNode
}) {
  const id = useId()
  const secret = field.secret || field.kind === "secret"
  return (
    <div className="grid gap-1.5">
      <label htmlFor={id} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-steel-100">
        {field.label}
        {field.required && <span className="text-xs text-steel-400">required</span>}
        {secret && (field.set ? <Badge tone="ok">Set</Badge> : <Badge>Not set</Badge>)}
      </label>
      {children(id)}
      {problem ? (
        <p role="alert" className="text-xs text-alert-300">
          {field.label}: {problem}.
        </p>
      ) : (
        field.help && (
          <p className="text-xs text-steel-400">
            <Prose text={field.help} />
          </p>
        )
      )}
    </div>
  )
}

/**
 * The control of one field.
 * @param props the `field`, its `id`, its `value`, whether it is `invalid` or `disabled`, and `onChange`
 * @returns the control
 */
function Control({
  field,
  id,
  value,
  invalid,
  disabled,
  onChange,
}: {
  field: ConnectorField
  id: string
  value: string | boolean | string[] | undefined
  invalid: boolean
  disabled: boolean
  onChange: (value: string | boolean | string[]) => void
}) {
  const text = typeof value === "string" ? value : ""
  const bad = invalid || undefined
  if (field.secret || field.kind === "secret")
    return (
      <Input
        id={id}
        type="password"
        autoComplete="off"
        value={text}
        disabled={disabled}
        aria-invalid={bad}
        placeholder={field.set ? "Type a new one to replace it" : field.placeholder || "Paste it here"}
        onChange={(e) => onChange(e.target.value)}
        className="font-mono"
      />
    )
  switch (field.kind) {
    case "switch":
      return (
        <Switch
          id={id}
          checked={value === true}
          disabled={disabled}
          onCheckedChange={onChange}
          aria-label={field.label}
        />
      )
    case "select":
      return (
        <SelectBox
          id={id}
          label={field.label}
          value={text || null}
          disabled={disabled}
          placeholder="Choose…"
          choices={[
            ...(field.required ? [] : [{ value: null, label: "Not chosen" }]),
            ...field.options.map((o) => ({ value: o, label: o })),
          ]}
          onChange={(next) => onChange(next ?? "")}
        />
      )
    case "list":
      return (
        <ChipList
          id={id}
          label={field.label}
          items={Array.isArray(value) ? value : []}
          disabled={disabled}
          placeholder={field.placeholder}
          onChange={onChange}
        />
      )
    case "number":
      return (
        <Input
          id={id}
          inputMode="decimal"
          value={text}
          disabled={disabled}
          aria-invalid={bad}
          placeholder={field.placeholder}
          onChange={(e) => onChange(e.target.value)}
          className="font-mono"
        />
      )
    default:
      return (
        <Input
          id={id}
          value={text}
          disabled={disabled}
          aria-invalid={bad}
          placeholder={field.placeholder}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => onChange(e.target.value)}
        />
      )
  }
}

/**
 * A list as chips: × takes one out; type one and press Enter (or leave the box) to add it.
 * @param props an `id` for the box, the list's `label`, its `items`, a `placeholder`, whether it is `disabled`, and
 *   `onChange`
 * @returns the chips and the box
 */
function ChipList({
  id,
  label,
  items,
  placeholder,
  disabled = false,
  onChange,
  className,
}: {
  id?: string
  label: string
  items: string[]
  placeholder?: string
  disabled?: boolean
  onChange: (items: string[]) => void
  className?: string
}) {
  const [draft, setDraft] = useState("")
  const add = () => {
    const item = draft.trim()
    setDraft("")
    if (item && !items.includes(item)) onChange([...items, item])
  }
  return (
    <div className={cn("flex flex-wrap items-center gap-1.5", className)}>
      {items.map((item) => (
        <span
          key={item}
          className="flex h-8 max-w-full items-center rounded-full bg-steel-50/6 pl-3 font-mono text-sm text-steel-100 shadow-[inset_0_0_0_1px_var(--hairline)]"
        >
          <span className="truncate">{item}</span>
          <IconButton
            label={`Take ${item} out`}
            tip={false}
            size="xs"
            className="ml-0.5 rounded-full"
            disabled={disabled}
            onClick={() => onChange(items.filter((i) => i !== item))}
          >
            <X />
          </IconButton>
        </span>
      ))}
      <Input
        id={id}
        value={draft}
        disabled={disabled}
        placeholder={placeholder || "Add…"}
        aria-label={`Add to ${label.toLowerCase()}`}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key !== "Enter") return
          e.preventDefault()
          add()
        }}
        onBlur={add}
        className="h-8 min-w-32 flex-1 font-mono"
      />
    </div>
  )
}
