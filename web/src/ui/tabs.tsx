/**
 * Tabs (Base UI) and the segmented control. Tabs: a row of words with a pill of light that glides to the active one
 * (Base UI's indicator, sized and placed by CSS variables: no layout measuring of ours). The segmented control is the
 * same pill inside a well, for a few choices of one setting (a radio group underneath).
 */
import { Radio } from "@base-ui/react/radio"
import { RadioGroup } from "@base-ui/react/radio-group"
import { Tabs as Base } from "@base-ui/react/tabs"
import { motion } from "motion/react"
import { type ReactNode, useId } from "react"
import { SPRING } from "@/lib/motion"
import { cn } from "@/lib/utils"

/** One tab: its value, its words and an optional icon or count. */
export interface TabItem<V extends string> {
  value: V
  label: string
  icon?: ReactNode
  count?: number
}

const PILL =
  "absolute top-1/2 left-0 -z-1 h-[calc(100%-6px)] w-(--active-tab-width) translate-x-(--active-tab-left) -translate-y-1/2 rounded-control bg-steel-50/9 shadow-[inset_0_0_0_1px_var(--hairline),inset_0_1px_0_0_var(--highlight)] transition-[translate,width] duration-(--dur-base) ease-(--ease-expo)"

const TAB =
  "relative flex h-9 shrink-0 select-none items-center gap-2 whitespace-nowrap rounded-control px-3.5 text-sm text-steel-400 outline-none transition-colors duration-(--dur-quick) hover:text-steel-100 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-rigel-400 data-active:text-steel-50 [&_svg]:size-4 [&_svg]:text-steel-500 data-active:[&_svg]:text-rigel-300"

/**
 * Tabs: the row, and the panels under it (each a child keyed by its value, rendered by `panel`).
 * @param props the `value`, `onChange`, the `items`, the open tab's content (`children`), and classes
 * @returns the tabs
 */
export function Tabs<V extends string>({
  value,
  onChange,
  items,
  children,
  label,
  className,
  listClassName,
}: {
  value: V
  onChange: (value: V) => void
  items: TabItem<V>[]
  children: ReactNode
  label: string
  className?: string
  listClassName?: string
}) {
  return (
    <Base.Root value={value} onValueChange={(next) => onChange(next as V)} className={cn("min-w-0", className)}>
      <Base.List
        aria-label={label}
        className={cn("no-scrollbar relative isolate flex w-fit max-w-full gap-0.5 overflow-x-auto", listClassName)}
      >
        {items.map((item) => (
          <Base.Tab key={item.value} value={item.value} className={TAB} data-testid={`tab-${item.value}`}>
            {item.icon}
            {item.label}
            {item.count !== undefined && item.count > 0 && (
              <span className="rounded-full bg-steel-50/8 px-1.5 font-mono text-micro text-steel-300 tabular-nums">
                {item.count}
              </span>
            )}
          </Base.Tab>
        ))}
        <Base.Indicator className={PILL} />
      </Base.List>
      <Base.Panel value={value} className="outline-none">
        {children}
      </Base.Panel>
    </Base.Root>
  )
}

/**
 * A few choices of one value, side by side, the chosen one in a pill.
 * @param props the `value`, `onChange`, the `choices`, an aria `label`, a test `id` and classes
 * @returns the control
 */
export function Segmented<V extends string | number>({
  value,
  onChange,
  choices,
  label,
  id,
  className,
}: {
  value: V | undefined
  onChange: (value: V) => void
  choices: { value: V; label: string }[]
  label?: string
  id?: string
  className?: string
}) {
  const pill = useId()
  return (
    <RadioGroup
      value={value}
      onValueChange={(next) => onChange(next as V)}
      aria-label={label}
      data-testid={id}
      className={cn(
        "relative isolate flex w-fit max-w-full rounded-control bg-void-950/55 p-[3px] shadow-[inset_0_0_0_1px_var(--hairline)]",
        className,
      )}
    >
      {choices.map((choice) => (
        <Radio.Root
          key={choice.value}
          value={choice.value}
          nativeButton
          render={<button type="button" />}
          className="relative h-7 min-w-14 flex-auto select-none whitespace-nowrap rounded-[7px] px-3 font-medium text-xs text-steel-400 outline-none transition-colors duration-(--dur-quick) hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400 data-checked:text-steel-50"
        >
          {choice.value === value && (
            <motion.span
              layoutId={pill}
              aria-hidden="true"
              transition={SPRING.snappy}
              className="absolute inset-0 -z-1 rounded-[7px] bg-steel-50/10 shadow-[inset_0_0_0_1px_var(--hairline-strong),inset_0_1px_0_0_var(--highlight)]"
            />
          )}
          {choice.label}
        </Radio.Root>
      ))}
    </RadioGroup>
  )
}
