/**
 * The select (Base UI): a field-styled trigger and a dense-glass menu of choices, the chosen one ticked. `SelectBox`
 * is the whole thing from a list of choices, each with an optional line under it; a value that isn't among them (a
 * model since removed) stays shown, marked as not available.
 */
import { Select as Base } from "@base-ui/react/select"
import { Check, ChevronsUpDown } from "lucide-react"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { FIELD } from "./input"
import { MENU, MENU_ITEM } from "./menu"

/** One choice: what it stores, what it says, and a line under it in the menu. */
export interface Choice<V = string | number | null> {
  value: V
  label: string
  detail?: ReactNode
}

/**
 * A select from a list of choices.
 * @param props the `value`, the `choices`, `onChange`, an aria `label`, a test `id`, extra classes, and a `placeholder`
 *   for no value (null)
 * @returns the select
 */
export function SelectBox<V extends string | number | null>({
  value,
  choices,
  onChange,
  label,
  id,
  disabled,
  className,
  placeholder,
}: {
  value: V
  choices: Choice<V>[]
  onChange: (value: V) => void
  label?: string
  id?: string
  disabled?: boolean
  className?: string
  placeholder?: string
}) {
  const items: Choice<V>[] =
    choices.some((c) => c.value === value) || (value === null && placeholder)
      ? choices
      : [...choices, { value, label: String(value ?? "—"), detail: "Not available now" }]
  return (
    <Base.Root<V> items={items} value={value} onValueChange={(next) => onChange(next as V)} disabled={disabled}>
      <Base.Trigger
        aria-label={label}
        data-testid={id}
        className={cn(
          FIELD,
          "flex h-9 w-full min-w-0 select-none items-center justify-between gap-2 pr-2.5 pl-3 text-left data-popup-open:shadow-[inset_0_0_0_1px_var(--color-rigel-400)]",
          className,
        )}
      >
        <Base.Value placeholder={placeholder} className="min-w-0 flex-1 truncate" />
        <Base.Icon render={<ChevronsUpDown aria-hidden="true" className="size-4 shrink-0 text-steel-500" />} />
      </Base.Trigger>
      <Base.Portal>
        <Base.Positioner sideOffset={6} alignItemWithTrigger={false} className="isolate z-[60]">
          <Base.Popup className={cn(MENU, "max-h-[min(var(--available-height),22rem)] w-(--anchor-width) min-w-44")}>
            <Base.List>
              {items.map((choice) => (
                <Base.Item key={String(choice.value)} value={choice.value} className={cn(MENU_ITEM, "pr-8")}>
                  <span className="grid min-w-0 gap-0.5">
                    <Base.ItemText className="truncate text-steel-100">{choice.label}</Base.ItemText>
                    {choice.detail && <span className="text-xs text-steel-500">{choice.detail}</span>}
                  </span>
                  <Base.ItemIndicator className="absolute right-2.5">
                    <Check aria-hidden="true" className="size-4 text-rigel-300" />
                  </Base.ItemIndicator>
                </Base.Item>
              ))}
            </Base.List>
          </Base.Popup>
        </Base.Positioner>
      </Base.Portal>
    </Base.Root>
  )
}
