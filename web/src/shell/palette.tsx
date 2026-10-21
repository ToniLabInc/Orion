/**
 * The command palette (⌘K or Ctrl+K anywhere, a "/" on an empty composer): one navigator for everything. The actions
 * (a new conversation, stop him, mute, his voice, share your screen, back up now, run a benchmark, add a connector),
 * the cards, the pages and every tab of Customize, Automations and Settings, and, once you type, his connectors,
 * skills, plugins and jobs by name and his memory. Base UI's autocomplete does the keys: ↑ ↓ to move, Enter to run,
 * Esc to close.
 */
import { Autocomplete } from "@base-ui/react/autocomplete"
import { Dialog } from "@base-ui/react/dialog"
import {
  Archive,
  ArrowRight,
  Brain,
  Camera,
  Captions,
  CornerDownLeft,
  FlaskConical,
  type LucideIcon,
  MessageSquarePlus,
  MessagesSquare,
  Mic,
  MicOff,
  MonitorUp,
  PanelRightClose,
  Plug,
  Plus,
  Power,
  Puzzle,
  RotateCw,
  Search,
  Sparkles,
  Square,
  Volume2,
  VolumeOff,
  Workflow,
} from "lucide-react"
import { useDeferredValue, useEffect, useMemo, useState } from "react"
import { useLocation } from "wouter"
import { useShallow } from "zustand/react/shallow"
import { useBackupNow, useConnectors, useJobs, useNeural, usePlugins, usePower, useRunJob, useSkills } from "@/lib/api"
import { releaseVoice, share, stopShare, takeVoice, talk, useCarrying, useDevice } from "@/lib/device"
import { connectorIcon } from "@/lib/icons"
import { AUTOMATIONS_TABS, CARD_INFO, CUSTOMIZE_TABS, PAGES, preload, SETTINGS_TABS } from "@/lib/nav"
import { setPref, usePrefs } from "@/lib/prefs"
import { play } from "@/lib/sound"
import { CARDS, dismiss, present, setOpen, setPalette, useStage } from "@/lib/stage"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { newChat, setMuted, setVoice, stop } from "@/lib/ws"
import { KINDS, search } from "@/pages/memory/graph"
import { BACKDROP } from "@/ui/dialog"
import { Kbd } from "@/ui/kbd"
import { MENU_ITEM } from "@/ui/menu"
import { toast } from "@/ui/toast"
import { rankGroups } from "./rank"

/** One command. */
interface Command {
  id: string
  label: string
  hint?: string
  keywords?: string
  icon: LucideIcon
  run: () => void
}

/** A group of commands (a type, not an interface: Base UI's groups take any keys). */
type Group = {
  value: string
  items: Command[]
}

/** How many memories a search shows. */
const MEMORIES = 6

/** The groups that only show once you type: they hold a command for everything he has. */
const BY_NAME = new Set(["Connectors", "Skills", "Plugins", "Jobs"])

/**
 * The palette, while open.
 * @returns the dialog
 */
export function Palette() {
  const open = useStage((s) => s.palette)
  useEffect(() => {
    if (open) {
      play("open")
      for (const page of ["memory", "automations", "activity", "customize", "settings"] as const) preload(page)
    }
  }, [open])
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        setPalette(next)
        if (!next) play("close")
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className={BACKDROP} />
        <Dialog.Viewport className="fixed inset-0 z-50 flex items-start justify-center px-3 pt-[min(14vh,7rem)] pb-3">
          <Dialog.Popup
            aria-label="Command palette"
            className="glass-solid flex max-h-[min(36rem,calc(100dvh-6rem))] w-full max-w-xl flex-col overflow-hidden rounded-card outline-none transition-[opacity,translate] duration-(--dur-base) ease-(--ease-expo) data-ending-style:-translate-y-2 data-ending-style:opacity-0 data-starting-style:-translate-y-2 data-starting-style:opacity-0"
            data-testid="palette"
          >
            {open && <Commands />}
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

/**
 * Every command, as the HUD stands now.
 * @returns the groups
 */
function useCommands(query: string): Group[] {
  const [location, navigate] = useLocation()
  const s = useLive(
    useShallow((l) => ({
      online: l.link === "online",
      state: l.state,
      muted: l.muted,
      voice: l.voice,
      voiceAvailable: l.voiceAvailable,
    })),
  )
  const cards = useStage((st) => st.cards)
  const open = useStage((st) => st.open)
  const sharing = useDevice((d) => d.sharing)
  const carrying = useCarrying()
  const captions = usePrefs((p) => p.captions)
  const power = usePower()
  const skills = useSkills()
  const connectors = useConnectors()
  const plugins = usePlugins()
  const backup = useBackupNow()
  const { jobs } = useJobs()
  const runJob = useRunJob()
  const memory = useNeural(query.trim().length >= 2)
  const go = (href: string) => () => navigate(href)

  {
    const working = s.state === "thinking" || s.state === "speaking"
    const actions: Command[] = [
      ...(working ? [{ id: "stop", label: "Stop him", hint: "Esc", icon: Square, run: () => stop() }] : []),
      {
        id: "new-chat",
        label: "New conversation",
        keywords: "reset clear chat forget context",
        icon: MessageSquarePlus,
        run: () => newChat(),
      },
      open
        ? {
            id: "fold",
            label: "Fold the conversation to captions",
            keywords: "ambient hide",
            icon: Captions,
            run: () => setOpen(false),
          }
        : {
            id: "unfold",
            label: "Open the conversation",
            keywords: "messages chat log",
            icon: MessagesSquare,
            run: () => setOpen(true),
          },
      carrying
        ? { id: "talk", label: "Talk now", hint: "listens without his name", icon: Mic, run: () => talk() }
        : {
            id: "take-voice",
            label: "Take his voice to this device",
            keywords: "microphone speakers",
            icon: Mic,
            run: () => void takeVoice(),
          },
      ...(carrying
        ? [{ id: "give-voice", label: "Give his voice back", icon: MicOff, run: () => releaseVoice() }]
        : []),
      sharing.includes("screen")
        ? { id: "unshare-screen", label: "Stop sharing your screen", icon: MonitorUp, run: () => stopShare("screen") }
        : {
            id: "share-screen",
            label: "Share your screen with him",
            keywords: "eyes look see",
            icon: MonitorUp,
            run: () => void share("screen"),
          },
      sharing.includes("camera")
        ? { id: "unshare-camera", label: "Stop sharing your camera", icon: Camera, run: () => stopShare("camera") }
        : {
            id: "share-camera",
            label: "Share your camera with him",
            keywords: "eyes look see webcam",
            icon: Camera,
            run: () => void share("camera"),
          },
      s.voice
        ? {
            id: "voice-off",
            label: "Silence his spoken replies",
            keywords: "voice mute speech",
            icon: VolumeOff,
            run: () => setVoice(false),
          }
        : {
            id: "voice-on",
            label: "Let him speak his replies",
            keywords: "voice speech",
            icon: Volume2,
            run: () => setVoice(true),
          },
      ...(s.voiceAvailable
        ? [
            s.muted
              ? {
                  id: "unmute",
                  label: "Unmute his machine's microphone",
                  keywords: "mic listen",
                  icon: Mic,
                  run: () => setMuted(false),
                }
              : {
                  id: "mute",
                  label: "Mute his machine's microphone",
                  keywords: "mic listen",
                  icon: MicOff,
                  run: () => setMuted(true),
                },
          ]
        : []),
      {
        id: "captions",
        label: captions ? "Hide his captions" : "Show his captions",
        keywords: "subtitles words",
        icon: Captions,
        run: () => setPref("captions", !captions),
      },
      ...(cards.length
        ? [
            {
              id: "clear",
              label: "Send every card away",
              keywords: "dismiss clear cards",
              icon: PanelRightClose,
              run: () => dismiss(),
            },
          ]
        : []),
      {
        id: "backup",
        label: "Back up now",
        keywords: "backup save copy archive restic",
        icon: Archive,
        run: () =>
          backup.mutate(undefined, {
            onSuccess: () =>
              toast.add({ title: "Backing up", description: "He tells you when it is done.", type: "info" }),
          }),
      },
      {
        id: "bench",
        label: "Run a benchmark",
        hint: "Settings, Brain",
        keywords: "benchmark score brains speed accuracy test",
        icon: FlaskConical,
        run: go("/settings/brain#bench"),
      },
      {
        id: "add-connector",
        label: "Add a connector",
        hint: "Discover",
        keywords: "connector connect service integration mcp server directory",
        icon: Plug,
        run: go("/customize/connectors?view=discover"),
      },
      {
        id: "add-custom-connector",
        label: "Add your own connector",
        hint: "A command or a URL",
        keywords: "connector custom mcp server command url",
        icon: Plus,
        run: go("/customize/connectors?add=custom"),
      },
      {
        id: "find-plugin",
        label: "Find a plugin",
        hint: "Discover",
        keywords: "plugin marketplace claude install",
        icon: Puzzle,
        run: go("/customize/plugins?view=discover"),
      },
      {
        id: "restart",
        label: "Restart Orion",
        keywords: "power reboot",
        icon: RotateCw,
        run: () => void power("restart"),
      },
      {
        id: "shutdown",
        label: "Shut Orion down",
        keywords: "power off quit stop",
        icon: Power,
        run: () => void power("shutdown"),
      },
    ]
    const cardCommands: Command[] = CARDS.map((id) => {
      const info = CARD_INFO[id]
      const shown = cards.some((c) => c.id === id)
      return {
        id: `card-${id}`,
        label: info.title,
        hint: shown ? "Shown · hide" : "Show",
        keywords: `card ${info.about}`,
        icon: info.icon,
        run: () => (shown && location === "/" ? dismiss(id) : present(id, { go: true })),
      }
    })
    const pages: Command[] = [
      ...PAGES.map((p) => ({
        id: `page-${p.id}`,
        label: p.label,
        hint: p.title,
        keywords: "page go",
        icon: p.icon,
        run: go(p.href),
      })),
      ...AUTOMATIONS_TABS.map((t) => ({
        id: `automations-${t.id}`,
        label: `Automations: ${t.label}`,
        hint: t.title,
        keywords: "automation schedule",
        icon: t.icon,
        run: go(`/automations/${t.id}`),
      })),
      ...CUSTOMIZE_TABS.map((t) => ({
        id: `customize-${t.id}`,
        label: `Customize: ${t.label}`,
        hint: t.title,
        keywords: "customize connectors skills plugins",
        icon: t.icon,
        run: go(`/customize/${t.id}`),
      })),
      ...SETTINGS_TABS.map((t) => ({
        id: `settings-${t.id}`,
        label: `Settings: ${t.label}`,
        hint: t.title,
        keywords: "settings preferences options",
        icon: t.icon,
        run: go(`/settings/${t.id}`),
      })),
    ]
    const skillCommands: Command[] = (skills.data ?? []).map((sk) => ({
      id: `skill-${sk.name}`,
      label: sk.name,
      hint: sk.description,
      keywords: `skill routine ${sk.triggers.join(" ")}`,
      icon: Sparkles,
      run: go(`/customize/skills/${encodeURIComponent(sk.name)}`),
    }))
    const connectorCommands: Command[] = (connectors.data ?? []).map((c) => ({
      id: `connector-${c.id}`,
      label: c.title,
      hint: c.description,
      keywords: `connector ${c.id} ${c.tools.map((t) => t.title || t.name).join(" ")}`,
      icon: connectorIcon(c.icon),
      run: go(`/customize/connectors/${encodeURIComponent(c.id)}`),
    }))
    const pluginCommands: Command[] = (plugins.data ?? []).map((p) => ({
      id: `plugin-${p.name}`,
      label: p.name,
      hint: p.description,
      keywords: "plugin",
      icon: Puzzle,
      run: go(`/customize/plugins/${encodeURIComponent(p.name)}`),
    }))
    const jobCommands: Command[] = (jobs ?? []).flatMap((job) => [
      {
        id: `job-${job.id}`,
        label: job.name,
        hint: job.explain,
        keywords: "job schedule",
        icon: Workflow,
        run: go(`/automations/jobs?job=${job.id}`),
      },
      {
        id: `job-run-${job.id}`,
        label: `Run ${job.name} now`,
        keywords: "job run",
        icon: ArrowRight,
        run: () => runJob.mutate(job.id),
      },
    ])
    const memories: Command[] =
      query.trim().length >= 2
        ? search(memory.data?.nodes ?? [], query, new Set(), MEMORIES).map((n) => ({
            id: `memory-${n.id}`,
            label: n.label,
            hint: KINDS[n.kind].name,
            keywords: `memory ${query}`, // found by the map's own search: always shown
            icon: Brain,
            run: go(`/memory?node=${encodeURIComponent(n.id)}`),
          }))
        : []
    return [
      { value: "Actions", items: actions },
      { value: "Cards", items: cardCommands },
      { value: "Pages", items: pages },
      { value: "Connectors", items: connectorCommands },
      { value: "Skills", items: skillCommands },
      { value: "Plugins", items: pluginCommands },
      { value: "Jobs", items: jobCommands },
      { value: "His memory", items: memories },
    ]
  }
}

/**
 * The field and the list.
 * @returns the commands
 */
function Commands() {
  const [query, setQuery] = useState("")
  const deferred = useDeferredValue(query)
  const groups = useCommands(deferred)
  const filtered = useMemo(
    () =>
      deferred.trim() ? rankGroups(groups, deferred) : groups.filter((g) => g.items.length && !BY_NAME.has(g.value)),
    [groups, deferred],
  )
  const run = (command: Command) => {
    setPalette(false)
    play("tick")
    command.run()
  }
  return (
    <Autocomplete.Root
      open
      inline
      items={groups}
      filteredItems={filtered}
      itemToStringValue={(c: Command) => c.label}
      value={query}
      onValueChange={setQuery}
      autoHighlight="always"
      keepHighlight
    >
      <div className="flex items-center gap-3 border-b px-4">
        <Search aria-hidden="true" className="size-4 shrink-0 text-steel-500" />
        <Autocomplete.Input
          placeholder="Search cards, pages, settings, his memory…"
          aria-label="Search commands"
          className="h-14 w-full bg-transparent text-md text-steel-50 outline-none placeholder:text-steel-500"
          data-testid="palette-input"
        />
        <Kbd>Esc</Kbd>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-2 [scroll-padding-block:0.5rem]">
        <Autocomplete.Empty>
          <p className="px-3 py-8 text-center text-sm text-steel-500">Nothing matches.</p>
        </Autocomplete.Empty>
        <Autocomplete.List className="grid gap-2">
          {(group: Group) => (
            <Autocomplete.Group key={group.value} items={group.items} className="grid">
              <Autocomplete.GroupLabel className="caps px-3 pt-1.5 pb-1 text-steel-500">
                {group.value}
              </Autocomplete.GroupLabel>
              <Autocomplete.Collection>
                {(c: Command) => (
                  <Autocomplete.Item
                    key={c.id}
                    value={c}
                    onClick={() => run(c)}
                    className={cn(MENU_ITEM, "group/item min-h-10 gap-3 data-highlighted:bg-steel-50/9")}
                    data-testid={`command-${c.id}`}
                  >
                    <span className="grid size-7 shrink-0 place-items-center rounded-control bg-steel-50/6 text-steel-300 shadow-[inset_0_0_0_1px_var(--hairline)] group-data-highlighted/item:text-rigel-200">
                      <c.icon aria-hidden="true" className="size-4" />
                    </span>
                    <span className="min-w-0 flex-1 truncate text-steel-100">{c.label}</span>
                    {c.hint && <span className="max-w-[45%] truncate text-xs text-steel-500">{c.hint}</span>}
                    <CornerDownLeft
                      aria-hidden="true"
                      className="size-3.5 shrink-0 text-steel-600 opacity-0 group-data-highlighted/item:opacity-100"
                    />
                  </Autocomplete.Item>
                )}
              </Autocomplete.Collection>
            </Autocomplete.Group>
          )}
        </Autocomplete.List>
      </div>
      <div className="flex items-center gap-4 border-t px-4 py-2.5 text-micro text-steel-500">
        <span className="flex items-center gap-1.5">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd> move
        </span>
        <span className="flex items-center gap-1.5">
          <Kbd>↵</Kbd> run
        </span>
        <span className="ml-auto">Type two letters to search his memory too</span>
      </div>
    </Autocomplete.Root>
  )
}
