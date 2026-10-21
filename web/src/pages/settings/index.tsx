/**
 * Settings: one tab per part of him, the tab in the address (/settings/voice): General, Brain, Voice, Memory, Safety
 * and Devices. A connector's own settings (the gestures of a gesture remote, the folders of your documents, a server's
 * fields) are on its page in Customize, not here. Each tab opens with its name in Instrument Serif, what it holds, and
 * a live word on that part of him. Every change saves the moment it is made (see ./fields). While config.yaml has a
 * problem he can't read past, a line at the top says so: nothing saves until it is fixed.
 */
import { FileCog, TriangleAlert } from "lucide-react"
import { AnimatePresence, motion } from "motion/react"
import { type ReactNode, Suspense } from "react"
import { useRoute } from "wouter"
import { useShallow } from "zustand/react/shallow"
import { useSettings } from "@/lib/api"
import { DUR, EXPO, stagger } from "@/lib/motion"
import { isSettingsTab, SETTINGS_TABS, type SettingsTab } from "@/lib/nav"
import { useLive } from "@/lib/store"
import type { SettingsProblem, SettingsResponse } from "@/lib/types"
import { Failed, Pending } from "@/ui/states"
import { ARRIVE, Frame, PageHeader } from "../frame"
import { Brain } from "./brain"
import { Devices } from "./devices"
import { General, Memory } from "./general"
import { Safety } from "./safety"
import { VoiceTab } from "./voice"

const TABS: Record<SettingsTab, (props: { settings: SettingsResponse }) => ReactNode> = {
  general: General,
  brain: Brain,
  voice: VoiceTab,
  memory: Memory,
  safety: Safety,
  devices: Devices,
}

const ITEMS = SETTINGS_TABS.map((t) => ({ href: `/settings/${t.id}`, label: t.label, icon: t.icon }))

/**
 * The settings page.
 * @returns the page
 */
export default function Settings() {
  const [, params] = useRoute("/settings/:tab?")
  const tab: SettingsTab = isSettingsTab(params?.tab) ? params.tab : "general"
  const settings = useSettings()
  const footer = settings.data && (
    <p
      className="mt-auto hidden items-center gap-2 border-t px-5 py-3.5 font-mono text-micro text-steel-500 desk:flex"
      title={settings.data.paths.config}
    >
      <FileCog aria-hidden="true" className="size-3.5 shrink-0 text-steel-500" />
      <span className="truncate">{settings.data.paths.config}</span>
    </p>
  )
  return (
    <Frame
      title="Settings"
      note="Every change saves the moment you make it."
      items={ITEMS}
      active={`/settings/${tab}`}
      footer={footer}
      testId="settings"
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={tab}
          className="grid grid-cols-1 gap-4"
          initial="hidden"
          animate="shown"
          exit={{ opacity: 0, y: -6, transition: { duration: DUR.quick, ease: [...EXPO] } }}
          variants={{ shown: { transition: stagger(0.05) } }}
          data-testid={`settings-${tab}`}
        >
          <Header tab={tab} />
          <Content tab={tab} />
        </motion.div>
      </AnimatePresence>
    </Frame>
  )
}

/**
 * A tab's header: its name, what it holds, and a live word on that part of him.
 * @param props the open `tab`
 * @returns the header
 */
function Header({ tab }: { tab: SettingsTab }) {
  const info = SETTINGS_TABS.find((t) => t.id === tab)
  const status = useStatus(tab)
  if (!info) return null
  return <PageHeader title={info.label} lead={`${info.title}.`} status={status || undefined} />
}

/**
 * The live word in a tab's header: what that part of him is doing right now, from what he reports.
 * @param tab the open tab
 * @returns a few words, or "" for none
 */
function useStatus(tab: SettingsTab): string {
  const { caps, name, honorific, voiceAvailable, surfaces } = useLive(
    useShallow((s) => ({
      caps: s.capabilities,
      name: s.userName,
      honorific: s.honorific,
      voiceAvailable: s.voiceAvailable,
      surfaces: s.surfaces.length,
    })),
  )
  const m = caps?.models
  switch (tab) {
    case "general":
      return [name, honorific].filter(Boolean).join(" · ")
    case "voice":
      return m ? `${voiceAvailable ? "Ears on" : "Ears off"} · ${m.stt || "—"} · ${m.voice || "—"}` : ""
    case "brain":
      return m ? `${m.standing_in ? "Stand-in: " : ""}${m.llm || m.provider}` : ""
    case "devices":
      return surfaces ? `${surfaces} connected` : ""
    default:
      return ""
  }
}

/**
 * The open tab, once the settings have come.
 * @param props the `tab`
 * @returns the tab, a wait, or why the settings couldn't be read
 */
function Content({ tab }: { tab: SettingsTab }) {
  const settings = useSettings()
  if (settings.data) {
    const Tab = TABS[tab]
    return (
      <>
        {settings.data.broken && <Broken problem={settings.data.broken} file={settings.data.paths.config} />}
        <Suspense fallback={<Pending delay={150} page />}>
          <Tab settings={settings.data} />
        </Suspense>
      </>
    )
  }
  if (settings.isError)
    return <Failed title="His settings can't be read" error={settings.error} onRetry={() => void settings.refetch()} />
  return <Pending label="Reading his settings" delay={150} page />
}

/**
 * config.yaml has a problem he can't read past: nothing saves until it is fixed.
 * @param props the `problem` and the settings `file`
 * @returns the warning
 */
function Broken({ problem, file }: { problem: SettingsProblem; file: string }) {
  return (
    <motion.div
      variants={ARRIVE}
      role="alert"
      className="flex items-start gap-3 rounded-card bg-alert-400/10 px-4 py-3 text-sm text-alert-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_35%,transparent)]"
      data-testid="settings-broken"
    >
      <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-alert-400" />
      <span className="min-w-0">
        Nothing saves until {file.split("/").pop()} is fixed: {problem.message}
      </span>
    </motion.div>
  )
}
