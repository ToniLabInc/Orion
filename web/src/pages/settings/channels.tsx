/**
 * What each channel may do: for each way a request reaches him (your voice, the HUD, Discord, his jobs, other apps),
 * the tools he may never use from there, not even after a yes, and the ones he always asks about first when he decides
 * to use them himself. Each list is a field of chips: type to find a kind of tool, a whole connector or one tool; ×
 * takes one out. A change saves at once (and comes back as it was if Orion refuses it).
 */
import { Combobox } from "@base-ui/react/combobox"
import { AppWindow, CalendarClock, Check, type LucideIcon, MessagesSquare, Mic, Monitor, Route, X } from "lucide-react"
import { useMemo } from "react"
import { useConnectors, useSaveChannel } from "@/lib/api"
import { asOptions, CHANNELS, offered, type RuleGroup, type RuleOption, ruleGroups } from "@/lib/rules"
import type { Channel, ChannelRules, SafetyState } from "@/lib/types"
import { cn } from "@/lib/utils"
import { FIELD } from "@/ui/input"
import { MENU, MENU_ITEM } from "@/ui/menu"
import { Note, Panel } from "./fields"

const ICONS: Record<Channel, LucideIcon> = {
  voice: Mic,
  hud: Monitor,
  discord: MessagesSquare,
  job: CalendarClock,
  app: AppWindow,
}

/**
 * The panel.
 * @param props the `safety` state (each channel's lists, and what a list can name)
 * @returns the panel
 */
export function Channels({ safety }: { safety: SafetyState }) {
  const connectors = useConnectors()
  const titles = useMemo(
    () => Object.fromEntries((connectors.data ?? []).map((c) => [c.id, c.title])),
    [connectors.data],
  )
  const groups = useMemo(() => ruleGroups(safety.choices, titles), [safety.choices, titles])
  const save = useSaveChannel()
  return (
    <Panel
      title="What each channel may do"
      icon={Route}
      description="By where a request comes from"
      data-testid="safety-channels"
    >
      <Note className="border-t-0 pt-0">
        Never holds even after a yes, whoever asks from there. Asks first is for what he decides to do on his own: he
        checks with you, where the request came from, before he does it.
      </Note>
      {CHANNELS.map((channel) => {
        const rules = safety.channels[channel.id] ?? { never: [], confirm: [] }
        const set = (change: Partial<ChannelRules>) =>
          save.mutate({ channel: channel.id, rules: { ...rules, ...change } })
        const Icon = ICONS[channel.id]
        return (
          <section
            key={channel.id}
            aria-label={channel.label}
            className="grid gap-3 border-t px-5 py-4"
            data-testid={`channel-${channel.id}`}
          >
            <p className="flex items-center gap-2.5">
              <Icon aria-hidden="true" className="size-4 shrink-0 text-steel-400" />
              <span className="font-medium text-base text-steel-50">{channel.label}</span>
              <span className="truncate text-sm text-steel-400">{channel.about}</span>
            </p>
            <div className="grid gap-3 @2xl:grid-cols-2">
              <Rules
                scope={channel.label}
                label="Never"
                tone="alert"
                entries={rules.never}
                groups={offered(groups, rules.confirm)}
                all={groups}
                onChange={(never) => set({ never })}
              />
              <Rules
                scope={channel.label}
                label="Asks first"
                tone="dusk"
                entries={rules.confirm}
                groups={offered(groups, rules.never)}
                all={groups}
                onChange={(confirm) => set({ confirm })}
              />
            </div>
          </section>
        )
      })}
    </Panel>
  )
}

const CHIPS = {
  alert:
    "bg-alert-400/12 text-alert-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_30%,transparent)]",
  dusk: "bg-dusk-400/12 text-dusk-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-dusk-400)_28%,transparent)]",
} as const

/**
 * One list of a channel: its entries as chips, a field to find another, and the options in groups.
 * @param props the channel it belongs to (`scope`), its `label`, its chips' `tone`, its `entries`, the `groups` it may
 *   take, `all` the options (to name an entry), and `onChange`
 * @returns the list
 */
function Rules({
  scope,
  label,
  tone,
  entries,
  groups,
  all,
  onChange,
}: {
  scope: string
  label: string
  tone: keyof typeof CHIPS
  entries: string[]
  groups: RuleGroup[]
  all: RuleGroup[]
  onChange: (entries: string[]) => void
}) {
  const value = asOptions(entries, all)
  return (
    <Combobox.Root<RuleOption, true>
      multiple
      items={groups}
      value={value}
      onValueChange={(next) => onChange(next.map((o) => o.value))}
      isItemEqualToValue={(a, b) => a.value === b.value}
      itemToStringLabel={(o) => o.label}
      autoHighlight
    >
      <div className="grid content-start gap-1.5">
        <Combobox.Label className="caps text-steel-400">{label}</Combobox.Label>
        <Combobox.Chips
          className={cn(
            FIELD,
            "flex min-h-9 flex-wrap content-start items-center gap-1 px-1.5 py-1 focus-within:bg-void-950/75",
          )}
        >
          <Combobox.Value>
            {(chosen: RuleOption[]) => (
              <>
                {chosen.map((o) => (
                  <Combobox.Chip
                    key={o.value}
                    aria-label={o.label}
                    title={o.detail}
                    className={cn(
                      "flex h-6 max-w-full items-center gap-1 rounded-chip pr-0.5 pl-2 text-xs",
                      CHIPS[tone],
                    )}
                  >
                    <span className="truncate">{o.label}</span>
                    <Combobox.ChipRemove
                      aria-label={`Take ${o.label} out`}
                      className="grid size-5 shrink-0 place-items-center rounded-[4px] opacity-70 outline-none hover:bg-steel-50/10 hover:opacity-100 focus-visible:outline-2 focus-visible:outline-rigel-400"
                    >
                      <X aria-hidden="true" className="size-3" />
                    </Combobox.ChipRemove>
                  </Combobox.Chip>
                ))}
                <Combobox.Input
                  aria-label={`${scope}, ${label.toLowerCase()}: add a tool, a connector or a kind`}
                  placeholder={chosen.length ? "Add…" : "Nothing: add a tool, a connector or a kind…"}
                  className="h-7 min-w-28 flex-1 bg-transparent px-1.5 text-sm text-steel-50 outline-none placeholder:text-steel-500"
                />
              </>
            )}
          </Combobox.Value>
        </Combobox.Chips>
      </div>
      <Combobox.Portal>
        <Combobox.Positioner sideOffset={6} className="isolate z-[60]">
          <Combobox.Popup
            className={cn(MENU, "max-h-[min(var(--available-height),22rem)] w-(--anchor-width) min-w-64")}
          >
            <Combobox.Empty>
              <p className="px-2.5 py-2 text-sm text-steel-400">Nothing matches.</p>
            </Combobox.Empty>
            <Combobox.List>
              {(group: RuleGroup) => (
                <Combobox.Group key={group.value} items={group.items} className="grid">
                  <Combobox.GroupLabel className="caps px-2.5 pt-2 pb-1 text-steel-500">
                    {group.value}
                  </Combobox.GroupLabel>
                  <Combobox.Collection>
                    {(o: RuleOption) => (
                      <Combobox.Item key={o.value} value={o} className={cn(MENU_ITEM, "pr-8")}>
                        <span className="grid min-w-0 gap-0.5">
                          <span className="truncate text-steel-100">{o.label}</span>
                          {o.detail && <span className="truncate text-xs text-steel-500">{o.detail}</span>}
                        </span>
                        <Combobox.ItemIndicator className="absolute right-2.5">
                          <Check aria-hidden="true" className="size-4 text-rigel-300" />
                        </Combobox.ItemIndicator>
                      </Combobox.Item>
                    )}
                  </Combobox.Collection>
                </Combobox.Group>
              )}
            </Combobox.List>
          </Combobox.Popup>
        </Combobox.Positioner>
      </Combobox.Portal>
    </Combobox.Root>
  )
}
