/**
 * General and Memory. General: who you are to him, the HUD's own settings, this screen's preferences (how hard Horizon
 * may work here, his captions; kept in this browser), and the service (its port, who can open the HUD, where his
 * settings and data live). Memory: how he learns from your conversations, how much he keeps in mind, privacy
 * (conversations in the logs, forgetting everything), his backups (./backups), and where the memory lives (the memory
 * itself, to read, forget or confirm, is the Memory page).
 */
import { BookOpen, Folder, MonitorCog, RefreshCw, Server, ShieldCheck, UserRound, Wand2 } from "lucide-react"
import type { CoreQuality } from "@/core"
import { useForgetEverything, useReviewMemory } from "@/lib/api"
import { setPref, usePrefs } from "@/lib/prefs"
import type { SettingsResponse } from "@/lib/types"
import { ask } from "@/ui/ask"
import { Button } from "@/ui/button"
import { Switch } from "@/ui/switch"
import { Segmented } from "@/ui/tabs"
import { toast } from "@/ui/toast"
import { Backups } from "./backups"
import { Field, Fields, Panel, PathValue, Row } from "./fields"

/** Who can open the HUD: server.host's two usual values, in words. */
const HOSTS = [
  { value: "127.0.0.1", label: "This machine alone" },
  { value: "0.0.0.0", label: "Your network too (each device paired)" },
]

/**
 * The General tab.
 * @param props the `settings`
 * @returns the tab
 */
export function General({ settings }: { settings: SettingsResponse }) {
  return (
    <>
      <Panel title="You" icon={UserRound} description="What he calls you, and where you are">
        <Fields paths={["user.name", "user.honorific", "user.city"]} />
      </Panel>
      <ThisScreen />
      <Panel title="The HUD" icon={Wand2}>
        <Fields paths={["hud.sounds", "hud.quick_timers", "hud.max_pictures"]} />
      </Panel>
      <Panel title="Service" icon={Server}>
        <Field path="server.port" />
        <Field path="server.host" choices={HOSTS} />
        <Row label="Settings file" hint="Every option on these pages, with a note on each: edit it in any text editor.">
          <PathValue path={settings.paths.config} data-testid="path-config" />
        </Row>
        <Row label="His data" hint="Everything he keeps: settings, memory, skills, models, logs.">
          <PathValue path={settings.paths.data} data-testid="path-data" />
        </Row>
      </Panel>
    </>
  )
}

const QUALITY: { value: CoreQuality; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "high", label: "High" },
  { value: "low", label: "Low" },
  { value: "off", label: "Off" },
]

/**
 * This screen's own preferences, kept in this browser (not in his settings file): how hard Horizon may work, his
 * captions.
 * @returns the panel
 */
function ThisScreen() {
  const prefs = usePrefs()
  return (
    <Panel title="This screen" icon={MonitorCog} description="Kept in this browser" data-testid="settings-screen">
      <Row
        label="The sky"
        hint="How hard Horizon may work on this screen. Auto draws less when frames come late (a busy GPU, a game), and more again when there is room. Off keeps a still sky."
      >
        <Segmented value={prefs.quality} onChange={(q) => setPref("quality", q)} choices={QUALITY} id="pref-quality" />
      </Row>
      <Row
        label="Captions"
        hint="His words on the sky while the conversation is folded, and yours when you speak."
        inline
      >
        <Switch
          checked={prefs.captions}
          onCheckedChange={(on) => setPref("captions", on)}
          data-testid="pref-captions"
        />
      </Row>
    </Panel>
  )
}

/**
 * A wait in words.
 * @param seconds how long
 * @returns for example `1 min 30 s`
 */
const duration = (seconds: string | number) => {
  const s = Number(seconds)
  const [minutes, rest] = [Math.floor(s / 60), s % 60]
  if (!minutes) return `${rest} s`
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`
}

/**
 * The Memory tab.
 * @param props the `settings`
 * @returns the tab
 */
export function Memory({ settings }: { settings: SettingsResponse }) {
  const review = useReviewMemory()
  const forgetAll = useForgetEverything()
  const askForgetAll = async () => {
    const text =
      "Every conversation, what he knows about you, his own notes, the journal, your notes and the logs are deleted for " +
      "good. Settings, timers, tasks and skills stay. He restarts to do it."
    if (await ask({ title: "Forget everything?", text, ok: "Forget everything", danger: true, kicker: "Memory" }))
      forgetAll.mutate()
  }
  const reviewNow = () =>
    review.mutate(undefined, {
      onSuccess: () => toast.add({ title: "Memory", description: "Reviewing the latest conversation…", type: "info" }),
    })
  return (
    <>
      <Panel
        title="Learning"
        icon={RefreshCw}
        action={
          <Button size="sm" disabled={review.isPending} onClick={reviewNow} data-testid="memory-review-now">
            Review now
          </Button>
        }
      >
        <Field path="memory.review" />
        {settings.config.memory.review && <Field path="memory.review_after_idle_s" format={duration} />}
        <Field path="memory.recall" />
      </Panel>
      <Panel title="In mind" icon={BookOpen}>
        <Fields paths={["brain.history_chars", "memory.user_chars", "memory.self_chars"]} />
      </Panel>
      <Panel title="Privacy" icon={ShieldCheck}>
        <Field path="memory.log_conversations" />
        <Row
          label="Forget everything"
          hint="Every conversation and everything he has learned, deleted for good. Settings, timers, tasks and skills stay."
        >
          <Button tone="danger" disabled={forgetAll.isPending} onClick={askForgetAll} data-testid="memory-forget-all">
            Forget everything
          </Button>
        </Row>
      </Panel>
      <Backups />
      <Panel title="Files" icon={Folder}>
        <Row label="Memory folder" hint="Plain Markdown: edit it in any text editor, and he picks up your changes.">
          <PathValue path={settings.paths.memory} data-testid="path-memory" />
        </Row>
      </Panel>
    </>
  )
}
