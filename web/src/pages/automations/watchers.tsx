/**
 * Watchers: what he keeps an eye on in his machine, and when he speaks up. First the readings now (battery, the
 * processor's and the graphics card's heat, the disk, the memory) as rings; then each rule, with its switch and its
 * limit (the watchers.* settings, drawn from his schema: a limit of 0 is off), greyed when this machine has no such
 * reading; then how he warns (aloud, on Discord) and, folded, how long a reading must stay past its limit.
 */
import { BatteryCharging, Cpu, HardDrive, MemoryStick, Radar, Thermometer, Volume2 } from "lucide-react"
import { motion } from "motion/react"
import type { ReactNode } from "react"
import { useWatchers } from "@/lib/api"
import type { WatchersResponse } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { ProgressRing, type RingTone } from "@/ui/progress-ring"
import { Failed, Pending } from "@/ui/states"
import { Switch } from "@/ui/switch"
import { ARRIVE } from "../frame"
import { Field, Fields, FoldPanel, Panel, useSetting, useShows } from "../settings/fields"

/** How long a reading must stay past its limit, and how soon a warning may come again (only config.yaml may set them). */
const PATIENCE = ["watchers.hold_s", "watchers.repeat_min"]

/**
 * The watchers tab.
 * @returns the tab
 */
export function Watchers() {
  const watchers = useWatchers()
  const patience = useShows(PATIENCE)
  if (watchers.isError)
    return (
      <motion.div variants={ARRIVE}>
        <Failed title="His watchers can't be read" error={watchers.error} onRetry={() => void watchers.refetch()} />
      </motion.div>
    )
  if (!watchers.data) return <Pending label="Reading his watchers" delay={150} page />
  const { sensors, rules } = watchers.data
  return (
    <>
      <Readings sensors={sensors} rules={rules} />
      <Panel title="When he speaks up" icon={Radar} description="A limit of 0 switches a rule off">
        {rules.map((rule) => (
          <Rule key={rule.id} rule={rule} />
        ))}
      </Panel>
      <Panel title="How he warns" icon={Volume2}>
        <Fields paths={["watchers.speak", "watchers.discord"]} />
      </Panel>
      {patience && (
        <FoldPanel title="Patience" icon={Radar}>
          <Fields paths={PATIENCE} />
        </FoldPanel>
      )}
    </>
  )
}

/**
 * The readings now, as rings: red past the limit of the rule that watches them (none past an off rule).
 * @param props the `sensors` and the `rules`
 * @returns the tiles
 */
function Readings({ sensors, rules }: { sensors: WatchersResponse["sensors"]; rules: WatchersResponse["rules"] }) {
  const past = (id: string, reading: number) => {
    const rule = rules.find((r) => r.id === id)
    if (!rule?.enabled || !rule.limit) return false
    return rule.above === false ? reading <= rule.limit : reading >= rule.limit
  }
  const tiles: {
    key: string
    label: string
    value: number
    shown: string
    note?: string
    tone: RingTone
    icon: ReactNode
  }[] = []
  if (sensors.battery)
    tiles.push({
      key: "battery",
      label: "Battery",
      value: sensors.battery.percent / 100,
      shown: `${Math.round(sensors.battery.percent)}%`,
      note: sensors.battery.plugged ? "charging" : "on battery",
      tone: past("battery_low", sensors.battery.percent) ? "alert" : "ok",
      icon: <BatteryCharging />,
    })
  if (sensors.temperatures?.cpu)
    tiles.push({
      key: "cpu",
      label: "Processor",
      value: Math.min(1, sensors.temperatures.cpu / 100),
      shown: `${Math.round(sensors.temperatures.cpu)}°`,
      tone: past("cpu_hot", sensors.temperatures.cpu) ? "alert" : "rigel",
      icon: <Cpu />,
    })
  if (sensors.temperatures?.gpu)
    tiles.push({
      key: "gpu",
      label: "Graphics",
      value: Math.min(1, sensors.temperatures.gpu / 100),
      shown: `${Math.round(sensors.temperatures.gpu)}°`,
      tone: past("gpu_hot", sensors.temperatures.gpu) ? "alert" : "nebula",
      icon: <Thermometer />,
    })
  tiles.push(
    {
      key: "disk",
      label: "Disk",
      value: sensors.disk.percent / 100,
      shown: `${Math.round(sensors.disk.percent)}%`,
      note: `${Math.round(sensors.disk.free_gb)} GB free`,
      tone: past("disk_full", sensors.disk.percent) ? "alert" : "aurora",
      icon: <HardDrive />,
    },
    {
      key: "memory",
      label: "Memory",
      value: sensors.memory.percent / 100,
      shown: `${Math.round(sensors.memory.percent)}%`,
      tone: past("memory_full", sensors.memory.percent) ? "alert" : "aurora",
      icon: <MemoryStick />,
    },
  )
  return (
    <motion.div variants={ARRIVE} className="grid grid-cols-2 gap-3 tab:grid-cols-3 desk:grid-cols-5">
      {tiles.map((t) => (
        <div key={t.key} className="glass flex items-center gap-3 p-3.5" data-testid={`reading-${t.key}`}>
          <ProgressRing value={t.value} size={52} stroke={3.5} tone={t.tone} label={`${t.label} ${t.shown}`}>
            <span
              className={cn(
                "grid place-items-center [&_svg]:size-4",
                t.tone === "alert" ? "text-alert-300" : "text-steel-300",
              )}
            >
              {t.icon}
            </span>
          </ProgressRing>
          <span className="min-w-0">
            <span className="block text-xs text-steel-400">{t.label}</span>
            <span className="block font-serif text-2xl text-steel-50 tabular-nums leading-tight">{t.shown}</span>
            {t.note && <span className="block truncate text-micro text-steel-500">{t.note}</span>}
          </span>
        </div>
      ))}
    </motion.div>
  )
}

/**
 * One rule: its switch (a limit of 0 is off; on again, it takes his schema's default), its limit, what it does now.
 * @param props the `rule`
 * @returns the row
 */
function Rule({ rule }: { rule: WatchersResponse["rules"][number] }) {
  const path = `watchers.${rule.id}`
  const { value, spec, save } = useSetting<number>(path)
  if (!spec) return null
  const on = typeof value === "number" && value > 0
  return (
    <div
      className={cn("border-t first-of-type:border-t-0", !rule.available && "opacity-55")}
      data-testid={`rule-${rule.id}`}
    >
      <div className="flex items-center gap-3 px-5 pt-3.5">
        <Switch
          checked={on}
          disabled={!rule.available}
          onCheckedChange={(next) => save(next ? Number(spec.default) || 0 : 0)}
          aria-label={`${rule.title}: on or off`}
        />
        <span className="min-w-0 flex-1 font-medium text-steel-50">{rule.title}</span>
        {rule.available ? (
          <Badge tone={on ? "rigel" : "neutral"}>{rule.detail}</Badge>
        ) : (
          <Badge>Not on this machine</Badge>
        )}
      </div>
      {on && rule.available && <Field path={path} label={rule.above === false ? "Warns below" : "Warns above"} />}
      {!on && <div className="h-3.5" />}
    </div>
  )
}
