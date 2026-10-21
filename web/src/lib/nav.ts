// The pages, their tabs and the cards: the palette, the top bar and the pages list them from here. The stage is
// Horizon, the conversation and the cards; the other pages take the screen under the top bar. A page's code is
// preloaded when the pointer or the focus reaches its link, so going there is instant. An address from an older HUD
// (a settings tab that became a page of its own) leads to where it lives now.
import {
  Activity,
  AudioLines,
  Blocks,
  BrainCircuit,
  CalendarClock,
  CalendarDays,
  CloudSun,
  Cpu,
  Hand,
  HardDrive,
  Hourglass,
  ListChecks,
  type LucideIcon,
  MonitorSmartphone,
  Newspaper,
  Orbit,
  Plug,
  Puzzle,
  Radar,
  ScanEye,
  ScrollText,
  Settings2,
  ShieldCheck,
  Sparkles,
  Sunrise,
  UserRound,
  Waypoints,
  Workflow,
} from "lucide-react"
import type { CardId } from "./stage"

export const PAGES = [
  { id: "stage", href: "/", label: "Stage", title: "The sky, the conversation and the cards", icon: Orbit },
  { id: "memory", href: "/memory", label: "Memory", title: "His memory, as a map", icon: Waypoints },
  { id: "automations", href: "/automations", label: "Automations", title: "Jobs and watchers", icon: Workflow },
  { id: "activity", href: "/activity", label: "Activity", title: "What he did, and who allowed it", icon: ScrollText },
  { id: "customize", href: "/customize", label: "Customize", title: "Connectors, skills and plugins", icon: Blocks },
  { id: "settings", href: "/settings", label: "Settings", title: "Settings", icon: Settings2 },
] as const

export type PageId = (typeof PAGES)[number]["id"]

/** The cards, in the palette's order: their title, what they show, and their icon. */
export const CARD_INFO: Readonly<Record<CardId, { title: string; about: string; icon: LucideIcon }>> = {
  today: { title: "Today", about: "Your day: events, reminders, timers and jobs", icon: Sunrise },
  weather: { title: "Weather", about: "Now, the next hours and the week", icon: CloudSun },
  vitals: { title: "Vitals", about: "How hard the PC is working", icon: Activity },
  calendar: { title: "Calendar", about: "Your agenda, day by day", icon: CalendarDays },
  timers: { title: "Timers & reminders", about: "What is counting down", icon: Hourglass },
  tasks: { title: "Tasks", about: "Your to-do list", icon: ListChecks },
  news: { title: "News", about: "The top headlines", icon: Newspaper },
  jobs: { title: "Jobs", about: "What he runs on a schedule", icon: CalendarClock },
  hands: { title: "Hands", about: "Your hands, as the gesture camera sees them", icon: Hand },
  eyes: { title: "Eyes", about: "What you share with him: your screen or camera", icon: ScanEye },
  devices: { title: "Devices", about: "Where he hears and speaks", icon: MonitorSmartphone },
}

export const SETTINGS_TABS = [
  { id: "general", label: "General", title: "You, this screen and the service", icon: UserRound },
  {
    id: "brain",
    label: "Brain",
    title: "Where he thinks, what it costs, and how fast he answers",
    icon: BrainCircuit,
  },
  { id: "voice", label: "Voice", title: "Listening, speech, his voice and instant commands", icon: AudioLines },
  { id: "memory", label: "Memory", title: "What he keeps in mind, how he learns, and his backups", icon: HardDrive },
  {
    id: "safety",
    label: "Safety",
    title: "Asking first, what each channel may do, the sandbox and your secrets",
    icon: ShieldCheck,
  },
  { id: "devices", label: "Devices", title: "Paired devices, the ones connected now, this machine", icon: Cpu },
] as const

export type SettingsTab = (typeof SETTINGS_TABS)[number]["id"]

const TAB_IDS: readonly string[] = SETTINGS_TABS.map((t) => t.id)
export const isSettingsTab = (tab: string | undefined): tab is SettingsTab => TAB_IDS.includes(tab ?? "")

export const AUTOMATIONS_TABS = [
  {
    id: "jobs",
    label: "Jobs",
    title: "Requests he runs on his own at the times you set, and where their answers go",
    icon: CalendarClock,
  },
  { id: "watchers", label: "Watchers", title: "What he keeps an eye on, and when he speaks up", icon: Radar },
] as const

export type AutomationsTab = (typeof AUTOMATIONS_TABS)[number]["id"]

export const CUSTOMIZE_TABS = [
  {
    id: "connectors",
    label: "Connectors",
    title: "Everything that gives him tools: his own, the ones you add, and the ones plugins bring",
    icon: Plug,
  },
  {
    id: "skills",
    label: "Skills",
    title: "Routines he follows step by step: his own, the ones he learned, yours and plugins'",
    icon: Sparkles,
  },
  {
    id: "plugins",
    label: "Plugins",
    title: "Claude plugins: their skills and commands become his skills, their servers his connectors",
    icon: Puzzle,
  },
] as const

type CustomizeTab = (typeof CUSTOMIZE_TABS)[number]["id"]

const CUSTOMIZE_IDS: readonly string[] = CUSTOMIZE_TABS.map((t) => t.id)
export const isCustomizeTab = (tab: string | undefined): tab is CustomizeTab => CUSTOMIZE_IDS.includes(tab ?? "")

/** Which page a path shows; null for a path that isn't one of them. */
export function pageOf(path: string): PageId | null {
  if (path === "/") return "stage"
  if (path === "/memory" || path === "/neural") return "memory"
  if (path === "/automations" || path.startsWith("/automations/")) return "automations"
  if (path === "/activity") return "activity"
  const customize = /^\/customize(?:\/([a-z]+)(?:\/.*)?)?\/?$/.exec(path)
  if (customize && (customize[1] === undefined || isCustomizeTab(customize[1]))) return "customize"
  const settings = /^\/settings(?:\/([a-z]+))?\/?$/.exec(path)
  if (settings && (settings[1] === undefined || isSettingsTab(settings[1]))) return "settings"
  return null
}

/** The address the gestures had: it leads to the first gesture remote's own page, which only the server knows. */
export const GESTURES = "gestures"

/**
 * Where an address that moved lives now.
 * @param path the address asked for
 * @returns the new address, GESTURES for the gesture remote's page, or null for an address that hasn't moved
 */
export function movedTo(path: string): string | null {
  switch (path.replace(/\/$/, "")) {
    case "/neural":
      return "/memory"
    case "/customize":
      return "/customize/connectors"
    case "/settings/abilities":
      return "/customize/connectors"
    case "/settings/eyes":
      return "/settings/safety"
    case "/settings/gestures":
      return GESTURES
    case "/automations/skills":
      return "/customize/skills"
    default:
      return null
  }
}

/** Open dialogs and menus: while one is, Esc and a page's own keys belong to it (a toast is a "dialog" too, but not one
 *  that takes the keys). */
export const OVERLAYS =
  '[role="dialog"]:not([data-slot="toast"]), [role="alertdialog"], [role="menu"], [role="listbox"]'

/** Each page's code, loaded once (app.tsx makes its lazy pages from these; links preload them). */
export const CHUNKS = {
  stage: () => import("@/stage"),
  memory: () => import("@/pages/memory"),
  automations: () => import("@/pages/automations"),
  activity: () => import("@/pages/activity"),
  customize: () => import("@/pages/customize"),
  settings: () => import("@/pages/settings"),
} as const satisfies Record<PageId, () => Promise<unknown>>

const preloaded = new Set<PageId>()

/** Start loading a page's code (the pointer or the focus reached its link, or the palette opened). */
export function preload(page: PageId) {
  if (preloaded.has(page)) return
  preloaded.add(page)
  void CHUNKS[page]().catch(() => preloaded.delete(page)) // a failed fetch is tried again next time
}
