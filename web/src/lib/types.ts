// What ORION sends and answers: the wire contract with orion/server.py, runtime.py, brain/__init__.py and mcp.py.
// Types only (palette.ts and the core worker import ShownState from here, never the store).

// ---------- his live state (WebSocket /ws) ----------
export type AssistantState = "idle" | "listening" | "thinking" | "speaking" | "loading"
/** What the HUD shows: his state, or "offline" while there is no connection. */
export type ShownState = AssistantState | "offline"
export type Provider = "claude" | "ollama" | "openrouter" | "script"
export type MessageSource = "hud" | "voice" | "announcement" | "app" | "background"
export type StepKind = "tool" | "memory" | "skill" | "recall"
/** Who asked for a tool call: he did (brain), you on the HUD, another AI app on this PC (app, through /mcp), or your own
 *  direct command without the model (user: an instant command, a gesture). */
export type ToolSource = "brain" | "hud" | "app" | "user"

/** One operation of a turn's trace, as the server stores it (history, live turn). */
export interface StepRecord {
  id: string
  turn: string
  kind: StepKind
  name: string // the tool's model-visible name (or the skill's)
  title: string
  server: string
  args: Record<string, unknown> // ≤ 8 keys, strings ≤ 160 chars
  source: ToolSource
  client: string // the other app's name (source "app")
  at: number // start, epoch seconds
  ms: number
  ok: boolean
  result: string // ≤ hud.tool_result_chars
  taints: boolean
  memory: { id: string; text: string; profile: boolean } | null
  images?: string[] // pictures the tool returned (data URLs), when Orion sends them
}

/** A message of the conversation as init sends it: user and assistant rows, and other apps' operations. */
export interface HistoryMessage {
  id: string // "m<row id>" for user messages and announcements; the turn id for answers; "x-<step id>" for ops
  role: "user" | "assistant" | "ops"
  content: string
  source: MessageSource
  ts: number
  steps?: StepRecord[]
  client?: string
}

/** The answer under way when the HUD connects. */
export interface LiveTurn {
  id: string
  text: string
  steps: StepRecord[]
}

/** The hud.* settings: the only HUD constants, from data/config.yaml. */
export interface HudConfig {
  quick_timers: number[]
  max_message_chars: number
  stats_interval_s: number
  tool_result_chars: number
  sounds: boolean
  max_pictures?: number // pictures one message may carry (the server cuts the rest; missing from an older Orion)
}

export type BootName = "tools" | "brain" | "memory" | "voice" | "ears"
export interface BootStep {
  name: BootName
  status: "pending" | "ok" | "error" | "skipped"
  detail: string
}
/** Startup progress: what has loaded, what failed and why. */
export interface Boot {
  ready: boolean
  steps: BootStep[]
}

export interface Reminder {
  id: number
  text: string
  due: number // epoch seconds
  kind: "timer" | "reminder"
  ts: number // when it was set
}

export interface Todo {
  id: number
  text: string
  ts: number
}

export interface WeatherDay {
  date: string
  condition: string
  code: number // the WMO weather code
  max: number
  min: number
  rain_chance: number
  sunrise: string
  sunset: string
}

/** One hour of the forecast. */
export interface WeatherHour {
  time: string // ISO, local
  temp: number
  code: number
  rain_chance: number
}

export interface Weather {
  place: string
  temp: number
  feels_like: number
  humidity: number
  wind: number
  condition: string
  code: number
  days: WeatherDay[]
  hours?: WeatherHour[] // the next hours, when Orion sends them (the card's curve; else the days' highs and lows)
  lat?: number // where it is (the widget's globe); missing from a forecast cached before it was sent
  lon?: number
}

export interface GpuStats {
  name: string
  util: number
  mem_used: number // MiB
  mem_total: number // MiB
  temp: number
}

export interface Stats {
  cpu: number
  cpu_temp: number | null
  ram_used: number // GB
  ram_total: number // GB
  ram: number
  disk: number
  disk_free: number // GB
  battery: { percent: number; plugged: boolean | null } | null
  gpu: GpuStats | null
  uptime: number // seconds since the PC booted
  top?: Program[] // the busiest programs, by CPU then memory (missing from an older Orion)
}

/** A program the PC runs, its processes added up. */
export interface Program {
  name: string
  cpu: number // % of the whole CPU
  mem: number // GB
}

/** A headline from the top news (his get_news tool, with no topic). */
export interface Headline {
  title: string
  source: string
  date: string // ISO
  url: string
}

export interface Capabilities {
  tools: number
  servers: number
  calls: number
  skills: number
  models: {
    llm: string
    provider: Provider
    stt: string
    stt_device: string
    voice: string
    fallback?: string // the stand-in brain's label ("" for none)
    standing_in?: boolean // the stand-in thinks now (the main brain can't answer)
    reason?: string // why
  }
}

// ---------- the devices, his questions, his jobs and his watchers ----------
/** What a browser offers Orion (lib/ws.ts says it in `hello`). */
export type DeviceCap = "mic" | "speaker" | "screen" | "camera" | "clipboard"

/** A HUD connected now: this tab, another browser on this PC, a phone. */
export interface Surface {
  id: string // its HUD's id ("hud-1a2b3c4d")
  device: string // "Firefox on Linux"
  local: boolean // its browser runs on Orion's machine
  caps: DeviceCap[]
  sharing: ("screen" | "camera")[]
  seen: number // when it was last used (epoch seconds)
}

/** Who carries his voice (its microphone and speakers): a HUD's id and name, or "" for Orion's own machine. */
export interface VoiceHolder {
  device: string
  name: string
}

/** What Orion's machine has (init.host): Settings, Devices, This machine. */
export interface HostProfile {
  platform: string
  container: boolean
  session: string // "wayland" | "x11" | ""
  desktop: string
  dbus: boolean
  opener: string
  sound: string
  power: boolean
  battery: boolean
  temperatures: boolean
  nvidia: boolean
  clipboard: string
  screen: string
}

/** A question before a risky action (`confirm`): answered yes or no from any HUD, his voice or Discord. */
export interface ConfirmRequest {
  id: string
  turn: string
  tool: string
  title: string // the tool's title ("Run a shell command")
  server: string
  action: string // what would be done, in words
  detail: string // the call's arguments, one "name: value" line each
  // why he asks: the request read outside content, you asked to be asked about this tool, the channel it came from
  // must always ask, or it would run outside the sandbox
  reason: "content" | "always" | "channel" | "outside_sandbox"
  source: string // what the request read ("Read a web page"), "" for "always"
  text: string // the question as it is said
  source_kind: string // where the request came from ("voice", "hud", "discord", "job")
  expires: number // when no answer becomes no (epoch seconds)
}

/** Where a job's result goes. */
export type Delivery = "speak" | "hud" | "discord"

/** A scheduled job (GET /api/jobs, the `jobs` event). */
export interface Job {
  id: number
  name: string
  schedule: string // cron: minute hour day month weekday
  explain: string // the schedule in English ("At 07:30 on Monday through Friday")
  prompt: string
  deliver: Delivery[]
  enabled: boolean
  next_run: number | null // epoch seconds
  last_run: number | null
  last_status: "ok" | "error" | ""
  last_text: string
  created: number
}

/** What a new job (or a change to one) says. */
export interface JobInput {
  name: string
  schedule: string
  prompt: string
  deliver: Delivery[]
  enabled: boolean
}

/** A watcher's alert (a battery running low, a hot CPU). */
export interface Alert {
  id: string
  rule: string
  level: "info" | "warn" | "critical"
  title: string
  text: string
  at: number // epoch seconds
}

/** GET /api/watchers: the sensors now and each rule. */
export interface WatchersResponse {
  sensors: {
    battery?: { percent: number; plugged: boolean | null; secs_left: number | null } | null
    temperatures?: { cpu?: number | null; gpu?: number | null } | null
    disk: { percent: number; free_gb: number }
    memory: { percent: number }
  }
  rules: {
    id: string
    title: string
    enabled: boolean
    available: boolean // this machine has the reading
    detail: string // what it does now, in words ("warns below 15%")
    limit?: number
    unit?: string
    above?: boolean
  }[]
  alerts?: Alert[]
}

/** An event of the calendars (GET /api/calendar). */
export interface CalendarEvent {
  id: string
  source: string
  calendar: string
  title: string
  start: string // ISO (a date alone for an all-day event)
  end: string
  all_day: boolean
  location: string
}

/** GET /api/calendar?days=N */
export interface CalendarResponse {
  sources: { name: string; ok: boolean; error: string }[]
  events: CalendarEvent[]
}

/** Something wrong with data/config.yaml, as the server words it. */
export interface SettingsProblem {
  kind: string
  key: string
  message: string
}

export interface InitEvent {
  type: "init"
  ts?: number
  state: AssistantState
  loading: string
  boot: Boot
  muted: boolean
  voice_available: boolean
  voice: boolean
  conv: number
  history: HistoryMessage[]
  live_turn: LiveTurn | null
  reminders: Reminder[]
  todos: Todo[]
  weather: Weather | null
  news?: Headline[] | null
  stats: Stats | null
  stats_history?: Stats[] // the last 80 s, oldest first, so the charts open full
  capabilities: Capabilities
  restart: string[]
  honorific: string
  user_name: string
  brain_error: string
  hud: HudConfig
  // what a newer Orion adds (each missing from an older one)
  surface?: string // this tab's HUD id
  confirms?: ConfirmRequest[] // the questions waiting
  host?: HostProfile // what Orion's machine has
  surfaces?: Surface[] // the HUDs connected now
  voice_holder?: VoiceHolder
  jobs?: Job[]
  alerts?: Alert[] // the active ones
  spend?: SpendToday // what OpenRouter cost today, and the daily budget
}

export type ToolStart = {
  phase: "start"
  id: string
  turn: string // the turn it belongs to ("" outside one)
  name: string
  title: string
  server: string
  args: Record<string, unknown>
  source: ToolSource
  client: string
  read_only: boolean | null
}

export type ToolEnd = {
  phase: "end"
  id: string
  turn: string
  name: string
  title: string
  server: string
  source: ToolSource
  client: string
  ok: boolean
  ms: number
  result: string
  error: string // the first error line when not ok
  attempts: number
  images?: string[] // pictures it returned (data URLs), when Orion sends them
}

/** An operation waits for your yes (`confirm`): the trace shows the question with Yes and No. */
export type ToolAsk = {
  phase: "ask"
  id: string
  turn: string
  name: string
  question: string // the confirmation's id (answered with a `confirm` message)
  text: string
}

export type ToolEvent = ToolStart | ToolEnd | ToolAsk

export interface ModelPull {
  name: string
  status: string // Ollama's own words while it downloads, then "done" or "error"
  completed?: number
  total?: number
  error?: string
}

type Ev<T extends string, F = object> = { type: T; ts?: number } & F

/** Every event the server sends on /ws ("init" always comes first, after every (re)connect). */
export type ServerEvent =
  | InitEvent
  | Ev<"boot", Boot>
  | Ev<"state", { state: AssistantState; model?: string }>
  | Ev<"wake">
  | Ev<"listen", { purpose: "follow_up" | "command" | "answer"; seconds: number }>
  | Ev<"hearing", { active: boolean }>
  | Ev<"level", { source: "mic" | "voice"; value: number; bands: number[] }>
  | Ev<"caption", { text: string }>
  | Ev<"transcript", { text: string; purpose: "filler" | "stop" | "answer" }>
  | Ev<"user_message", { text: string; source: "hud" | "voice"; turn?: string; device?: string; images?: string[] }>
  // a question before a risky action, and its end (answered yes or no, or timed out) everywhere
  | Ev<"confirm", ConfirmRequest>
  | Ev<"confirm_done", { id: string; answer: "yes" | "no" | "timeout" }>
  // the devices: the HUDs connected, and who carries his voice
  | Ev<"surfaces", { items: Surface[] }>
  | Ev<"voice_holder", VoiceHolder>
  // to this tab alone: something its device should do (a frame of what it shares, its clipboard, start sharing)
  | Ev<
      "request",
      {
        id: string
        kind: "frame" | "clipboard_read" | "clipboard_write" | "share"
        what?: "screen" | "camera"
        text?: string
      }
    >
  // to this tab alone: his voice stopped (drop what the player holds up to this generation)
  | Ev<"speaker", { gen: number }>
  // scheduled jobs: the list, and a run starting or ending
  | Ev<"jobs", { items: Job[] }>
  | Ev<"job_run", { id: number; name?: string; phase: "start" | "end"; ok?: boolean; text?: string; manual?: boolean }>
  // watchers: the active alerts, and a new one
  | Ev<"alerts", { items: Alert[] }>
  | Ev<"alert", { item: Alert }>
  // an answer; one an instant command gave (matched without the model) carries `instant`
  | Ev<"assistant_start", { id: string; conv?: number; channel?: "voice" | "text"; instant?: boolean }>
  | Ev<"assistant_delta", { id: string; text: string }>
  | Ev<"assistant_done", { id: string; text: string; error?: boolean; ms?: number; rounds?: number; instant?: boolean }>
  | Ev<"assistant_interrupted", { id?: string }>
  | Ev<"tool", ToolEvent>
  | Ev<"recall", { turn: string; items: { id: string; text: string }[]; ms: number }>
  | Ev<
      "memory_update",
      {
        id: string
        turn: string
        call: string
        target: string // "user" | "orion" | "notes"
        action: "add" | "update" | "remove" | "save"
        status: string
        text: string
        result: string
        ok: boolean
        profile: boolean // it entered USER.md or MEMORY.md (false: kept out by where it came from)
        source: string
      }
    >
  | Ev<"memory_review", { changes: string[]; error?: string; pending?: number }>
  | Ev<"memory_indexed", { version: number; files: number; turns: number }>
  | Ev<"skill_used", { name: string; turn?: string; call?: string }>
  | Ev<"skill_changed", { name: string; action: string }>
  | Ev<"reminders", { items: Reminder[] }>
  | Ev<"todos", { items: Todo[] }>
  | Ev<"weather", { weather: Weather }>
  | Ev<"news", { items: Headline[] }>
  | Ev<"stats", { stats: Stats }>
  | Ev<"announcement", { id?: string; title: string; text: string }>
  | Ev<"voice", { on: boolean }>
  | Ev<"mic", { muted: boolean }>
  | Ev<"capabilities", Capabilities>
  | Ev<"settings", { changed: string[]; restart: string[]; source?: "hud" | "ai" | "file" }>
  | Ev<"settings_problem", { problems: SettingsProblem[]; broken: SettingsProblem | null }>
  | Ev<"toast", { title: string; text: string }>
  | Ev<"shutdown", { restart?: boolean }>
  | Ev<"conversation_reset", { conv?: number; reason?: "new_chat" | "idle_gap" }>
  | Ev<"model_pull", ModelPull>
  // show or hide parts of the HUD, show one if hidden and hide it if shown (toggle), or send the newest away (dismiss)
  | Ev<"summon", { action: SummonAction; panels: string[]; source: string }>
  // a hand gesture he acted on (ORION hands, an MCP server): what it did, which gesture and hand, and how it went
  | Ev<
      "intent",
      {
        id?: number
        intent: string // the action ("media", "toggle"…); "asleep" for a gesture made while gestures sleep
        args?: Record<string, unknown>
        gesture?: string
        hand?: string
        source?: string // the server it came from ("hands")
        ok?: boolean
        text?: string // "Swipe right → next track"
        result?: string // what happened, or why not
        awake?: number // while a gesture wakes the others: how many seconds more they stay awake
        showing?: number // a held hand shape: the id of its showing (its repeats share it)
        again?: boolean // a repeat of that showing (a thumb still up): update what shows, don't announce it anew
      }
    >
  // a frame of a gesture remote's live view (only while this tab shows it: hands_view)
  | Ev<"hands", { server: string; frame: HandsFrame }>
  // what he did and who allowed it, as each entry is recorded
  | Ev<"activity", { item: Activity }>
  // the connectors and the plugins, whenever one changes (status, settings, a sign-in, an install)
  | Ev<"connectors", { items: Connector[] }>
  | Ev<"plugins", { items: Plugin[] }>
  // a turn's stages, measured, once it has spoken (or written) its first word
  | Ev<"timing", Timing>
  // OpenRouter's cost so far today, after each paid request
  | Ev<"spend", SpendToday>
  // a benchmark run, as it goes
  | Ev<"bench", BenchRun>
  // a backup starting, done or failed
  | Ev<"backup", { phase: "start" | "done" | "failed"; name: string; size?: number; error?: string }>
  // your documents' index, as it changes
  | Ev<"documents", DocumentsStatus>

export type ServerEventType = ServerEvent["type"]
export type EventOf<T extends ServerEventType> = Extract<ServerEvent, { type: T }>
export type LevelEvent = EventOf<"level">

export type SummonAction = "show" | "hide" | "toggle" | "dismiss"

/** One hand as the gesture camera sees it in a frame of its live view. */
export interface HandView {
  track: number // the same number while it stays in view
  hand: string // "Left" | "Right": the user's own
  shape: string | null // the hand shape it holds (null while it moves or holds none)
  score: number
  held: number // seconds it has held that shape
  pts: [number, number][] // its 21 landmarks: x and y from 0 to 1 in the mirrored image (as in a mirror)
}

/** A frame of a gesture remote's live view (orion://hands, or orion://hands/camera with the picture). */
export interface HandsFrame {
  seq: number
  ts?: number // when the camera saw it (epoch seconds, the Pi's clock)
  w?: number
  h?: number
  fps?: number
  mode?: string // the recognizer: "search" (no hand), "one", "two"
  hands: HandView[]
  jpg?: string // the camera's picture, base64 JPEG (orion://hands/camera only; left out while it hasn't changed)
  jpg_seq?: number // the frame that picture came with
  apart?: number | null // how far apart two hands are, in palm lengths (null without two)
  link?: { connected: boolean; error: string; camera: boolean; paused: boolean; tracker: string }
}

/** What the HUD may send on /ws (binary frames go apart: lib/ws.ts sendBinary). */
export type ClientMessage =
  | { type: "chat"; text: string; images?: string[] }
  | { type: "voice"; on: boolean }
  | { type: "stop" }
  | { type: "mute"; value: boolean }
  | { type: "reset" }
  | { type: "hands_view"; on: boolean; picture?: boolean } // this tab shows the hands' live view (or stops)
  | { type: "hello"; device: string; caps: DeviceCap[] } // what this browser offers, after each init
  | { type: "sharing"; what: ("screen" | "camera")[] } // what this tab shares now
  | { type: "confirm"; id: string; yes: boolean } // the answer to a question before a risky action
  | ({ type: "answer"; id: string } & Record<string, unknown>) // the answer to a request

// ---------- REST (/api) ----------
export interface Voice {
  id: string
  name: string
  accent: string
  gender: string
}

/** The settings as GET /api/settings shows them (prompts left out, secrets as "" or "(hidden)"). */
export interface Config {
  user: { name: string; honorific: string; city: string }
  brain: { provider: Provider; think: boolean; temperature: number; max_tool_rounds: number; history_chars: number }
  claude: { model: string }
  ollama: { model: string; num_ctx: number; host: string }
  openrouter: { model: string; api_key: string }
  voice: {
    enabled: boolean
    name_wake: boolean
    barge_in: boolean
    wake_model: string
    wake_threshold: number
    follow_up_seconds: number
    end_silence_ms: number
    max_utterance_s: number
    chime: boolean
    input_device: string | number | null
    output_device: string | number | null
  }
  tts: { voice: string; speed: number }
  stt: { model: string; device: string }
  memory: { review: boolean; review_after_idle_s: number; recall: boolean; user_chars: number; self_chars: number }
  tools: { max_per_turn: number }
  hud: HudConfig
  server: { port: number }
  data_dir: string
  [section: string]: unknown
}

/** A change to the settings: sections of options, or flat keys ("tts.speed"). */
export type SettingsPatch = Record<string, unknown>

/** How the Settings pages render one key (the server's schema). */
export interface FieldSpec {
  kind: "bool" | "int" | "float" | "text" | "secret" | "choice" | "list" | "device"
  label: string
  help: string
  min?: number
  max?: number
  step?: number
  unit?: string
  choices?: { value: string | number; label: string }[]
  restart: boolean
  default: unknown
}

export type SettingsSchema = Record<string, FieldSpec>

/** GET /api/settings */
export interface SettingsResponse {
  config: Config
  schema: SettingsSchema
  restart: string[]
  problems: SettingsProblem[]
  broken: SettingsProblem | null
  secrets: Record<string, "vault" | "config" | "env" | ""> // where each secret option's value comes from
  devices: { input: string[]; output: string[] }
  voices: Voice[]
  loading: boolean // the voice is still loading (no voices to choose from yet)
  stt_models: SttOption[]
  claude_models: { value: string; detail: string }[]
  paths: { config: string; data: string; memory: string }
}

/** PUT /api/settings, the tool and server switches: what changed, and what waits for a restart. */
export interface SavedSettings {
  changed: string[]
  restart: string[]
  deferred?: string[]
}

export interface InstalledModel {
  name: string
  size: number // bytes
  params: string
  quant: string
  capabilities: string[] // Ollama's words: "completion", "tools", "thinking", "vision", "embedding"…
  loaded: boolean
}

export interface SttOption {
  name: string
  size: string
  note: string
  downloaded: boolean
}

/** GET /api/models */
export interface ModelsResponse {
  llm: {
    embed: string
    installed: InstalledModel[]
    error: string
    brain_error: string
    ollama: { installed: boolean; running: boolean }
  }
  stt: {
    model?: string
    device?: string
    compute_type?: string
    loaded?: boolean
  }
  gpu: GpuStats | null
}

export interface OpenRouterModel {
  id: string
  name: string
  context: number
  free: boolean
  price: string
}

/** GET /api/claude */
export interface ClaudeStatus {
  installed: boolean
  logged_in: boolean
  plan: string
  method: string
}

/** POST /api/tools/call */
export interface ToolCallResult {
  ok: boolean
  result: string
  ms: number
}

export type NeuralKind = "you" | "orion" | "concept" | "fact" | "insight" | "unverified" | "day" | "note" | "skill"

/** What one gesture does (orion/gestures.py): the action, its options flat beside it, then hold and repeat. */
export interface GestureBinding {
  do: string
  hold?: number
  repeat?: boolean
  [option: string]: unknown
}

/** One gesture a remote recognises. */
export interface GestureInfo {
  name: string
  title: string
  kind: "shape" | "motion" | "two_hands" | string
  how: string
}

/** One value an option can take, in words. */
export interface ActionChoice {
  value: string | number
  label: string
}

/** One option of an action. */
export interface ActionParam {
  name: string
  kind: "panels" | "panel" | "choice" | "text" | "minutes" | "seconds" | "skill" | "server" | "tool" | "args"
  choices: ActionChoice[] // a choice's values; the timers and waking times config.yaml offers; [] for a free one
  required: boolean
  default: unknown
}

/** Something a gesture can do. */
export interface GestureAction {
  title: string
  group: string
  repeats: boolean // doing it again while a shape is held makes sense (the volume)
  params: ActionParam[]
  start: GestureBinding | null // what a gesture newly given it starts with; null: it can't have one yet (no skills)
}

/** A server that sends gestures (intents: true), with its gestures and what each does. */
export interface GestureRemote {
  name: string
  status: "connecting" | "connected" | "reconnecting" | "error" | "disabled" // its server's
  enabled: boolean
  error: string
  custom: boolean // the user chose its bindings (else the defaults)
  gestures: GestureInfo[] // the gestures it recognises (what it said last while it can't say: vocabulary_error)
  hold_steps: number[] // the holds it reports a held shape at, in seconds
  static_hold: number | null // how long a shape is held before it counts (null: it doesn't say)
  two_hands: { together: number; apart: number } | null // the distances (palm lengths) the two-hand gestures need
  vocabulary_error: string
  bindings: Record<string, GestureBinding>
  problems: Record<string, string> // bindings in config.yaml that are wrong, and why
  words: Record<string, string> // each binding in a few words ("next track")
  awake: number
}

/** A tool a gesture can use. */
export interface GestureTool {
  server: string
  name: string
  title: string
  description: string
  destructive: boolean
  schema: Record<string, unknown> // its arguments' JSON schema
}

/** A device paired with Orion (GET /api/pairing, on this PC). */
export interface PairedDevice {
  id: string
  name: string
  paired: number // epoch seconds
  seen: number // last used, to the hour
}

/** GET /api/pairing: whether this device may use the HUD; on this PC, the devices paired and the code waiting. */
export interface PairingState {
  local: boolean // this PC's own page
  paired: boolean // this PC, or a device paired with it
  open: boolean // server.host lets your network in (after a restart, if just changed)
  tls: boolean // served over HTTPS
  url: string // where a device opens the HUD
  devices: PairedDevice[]
  code_ends: number | null // when the code shown last stops working (epoch seconds)
}

/** GET /api/gestures */
export interface GesturesPage {
  remotes: GestureRemote[]
  defaults: Record<string, GestureBinding>
  actions: Record<string, GestureAction>
  parts: { value: string; label: string }[]
  skills: string[]
  tools: GestureTool[]
  pause_minutes: number // how long Pause stops a remote's gestures
}

/** GET /api/neural: his memory as a graph. */
export interface NeuralGraph {
  nodes: { id: string; label: string; kind: NeuralKind; degree: number }[]
  links: { source: string; target: string }[]
  counts: Partial<Record<NeuralKind, number>>
}

/** GET /api/neural/node/{id}: one node with its text, and when and how a memory was learned. */
export interface NeuralNode {
  id: string
  label: string
  kind: NeuralKind
  text?: string
  path?: string
  learned_at?: number
  source?: string
}

// ---------- the activity log, the vault, the safety rules ----------
/** What an activity entry records. */
export type ActivityKind =
  | "tool"
  | "ask"
  | "deny"
  | "block"
  | "alert"
  | "job"
  | "backup"
  | "connector"
  | "plugin"
  | "skill"
  | "secret"
  | "instant"
  | "login"
  | "bench"
  | "system"

/** Where an activity entry came from. */
export type ActivitySource = "voice" | "hud" | "discord" | "job" | "watcher" | "gesture" | "app" | "user" | "system"

/** One entry of the activity log: what he did (or was stopped from doing), and who allowed it (redacted). */
export interface Activity {
  id: number
  at: number // epoch seconds
  kind: ActivityKind
  source: ActivitySource
  actor: string // who: "you", "Orion", a device, an app
  connector: string
  tool: string
  title: string
  detail: string
  outcome: string // "ok", "failed", "yes", "no", "timeout", "denied"… ("" when it has none)
  turn: string
  ms: number
}

/** GET /api/activity: a page of entries, newest first, and the id to ask for the page before it. */
export interface ActivityPage {
  items: Activity[]
  next: number | null
}

/** GET /api/secrets: where secrets are kept, and their names (never their values). */
export interface SecretsState {
  store: "keyring" | "files"
  ok: boolean
  error: string
  items: { name: string; set: boolean; used_by: string[] }[]
}

/** A channel his requests come in on, for its limits. */
export type Channel = "voice" | "hud" | "discord" | "job" | "app"

/** What a channel may never use, and what it must always ask about first: tool names, `<connector>:*`, or classes
 *  (`@read`, `@change`, `@destroy`, `@open_world`). */
export interface ChannelRules {
  never: string[]
  confirm: string[]
}

/** How much a tool can do: read, change things, or destroy them. */
export type Risk = "read" | "change" | "destroy"

/** GET /api/safety */
export interface SafetyState {
  channels: Record<Channel, ChannelRules>
  sandbox: {
    available: "bwrap" | "container" | "" // "": this machine can't make a sandbox (`why` says what is missing)
    commands: boolean // commands run in one (off: with his full access, each asking you first)
    network: boolean // a command may reach the network
    network_ok?: boolean // false: this machine's kernel can't give a sandbox with the network its safeguards
    why?: string // what is missing for a sandbox, in words
    hidden: string[]
    writable: string[]
  }
  choices: { tools: { name: string; title: string; connector: string; risk: Risk }[]; classes: string[] }
}

// ---------- connectors, skills, plugins (Customize) ----------
export type ConnectorStatus =
  | "connected"
  | "needs_setup"
  | "needs_sign_in"
  | "connecting"
  | "error"
  | "off"
  | "unavailable"

/** One setting of a connector (its own schema, not config.yaml's): a secret's value is never sent, only `set`. */
export interface ConnectorField {
  key: string
  label: string
  kind: "text" | "secret" | "select" | "switch" | "number" | "list"
  help: string
  placeholder: string
  options: string[]
  required: boolean
  secret: boolean
  set?: boolean
}

/** What a connector's own settings are: fields, the gesture editor, the documents' folders, or nothing. */
export type ConnectorSettings =
  | { kind: "fields"; fields: ConnectorField[]; values: Record<string, unknown> }
  | { kind: "gestures" }
  | { kind: "folders"; path: string }
  | { kind: "none" }

/** One of a connector's tools. */
export interface ConnectorTool {
  name: string
  title: string
  description: string
  risk: Risk
  open_world: boolean // it reaches the internet
  enabled: boolean
}

/** Anything that gives him tools: his own groups, a server from the directory, one a plugin brings, one you added. */
export interface Connector {
  id: string
  title: string
  description: string
  icon: string // a lucide-react icon's name
  kind: "builtin" | "catalog" | "plugin" | "custom"
  plugin?: string
  trust: "orion" | "reviewed" | "unreviewed"
  sandbox: "none" | "bwrap"
  status: ConnectorStatus
  error: string
  needs: string[] // what this machine lacks for it, in words
  auth: "none" | "fields" | "oauth"
  signed_in: boolean
  local: boolean // this HUD is on Orion's own machine (a sign-in happens in a browser there)
  tools: ConnectorTool[]
  settings: ConnectorSettings
  secrets: { name: string; set: boolean }[]
  removable: boolean
  calls: number
}

/** GET /api/connectors/{id}: a connector and what it did lately. */
export type ConnectorDetail = Connector & { activity: Activity[] }

/** One connector the directory offers (GET /api/connectors/directory). */
export interface DirectoryItem {
  id: string
  title: string
  description: string
  icon: string
  category: string
  source: "orion" | "claude-directory"
  auth: "none" | "fields" | "oauth"
  fields: ConnectorField[]
  installed: boolean
  docs: string
}

/** What adding a connector says: one from the directory with its fields' values, or one of your own. */
export type NewConnector =
  | { from: "directory"; id: string; values: Record<string, unknown> }
  | {
      from: "custom"
      name: string
      command?: string
      args?: string[]
      env?: Record<string, string>
      url?: string
      headers?: Record<string, string>
    }

/** A skill: an Agent Skills folder (SKILL.md), his own, learned, yours or a plugin's. */
export interface Skill {
  name: string
  description: string
  source: "builtin" | "learned" | "yours" | "plugin"
  plugin?: string
  enabled: boolean
  triggers: string[]
  tools: string[]
  files: string[]
  editable: boolean
}

/** GET /api/skills/{name}: a skill with its SKILL.md. */
export type SkillDetail = Skill & { content: string }

/** A skill to add from its sources (GET /api/skills/discover). */
export interface SkillListing {
  name: string
  description: string
  source: string
  url: string
  installed: boolean
}

/** A plugin's author: a name, or plugin.json's object. */
export type PluginAuthor = string | { name: string; email?: string; url?: string }

/** A Claude plugin he has: its skills and commands become skills, its MCP servers connectors; the rest goes unused. */
export interface Plugin {
  name: string
  version: string
  description: string
  author: PluginAuthor
  homepage: string
  source: { marketplace?: string; url?: string; sha?: string }
  enabled: boolean
  installed_at: number
  trust: "reviewed" | "unreviewed" // reviewed: it came from Anthropic's directory
  components: { skills: string[]; commands: string[]; connectors: string[]; unused: string[] }
  update?: { version: string; sha: string }
}

/** A plugin a marketplace offers (GET /api/plugins/discover). */
export interface PluginListing {
  name: string
  description: string
  category: string
  marketplace: string
  author: PluginAuthor
  homepage: string
  installed: boolean
  components_known: boolean
}

/** A plugin marketplace (a git repository with .claude-plugin/marketplace.json). */
export interface Marketplace {
  name: string
  url: string
  plugins: number
  updated_at: number // when it was last read, epoch seconds; 0 when it never was
  error: string // why it couldn't be read, "" when it could
}

// ---------- speed, money, benchmarks ----------
/** One turn's stages, in ms: hearing you (speech recognition), thinking, his tools, and speaking his first words. */
export interface StageTimes {
  heard_ms: number
  think_ms: number
  tools_ms: number
  speak_ms: number
  total_ms: number
}

/** A turn, measured (the `timing` event, GET /api/timings). */
export interface Timing extends StageTimes {
  turn: string
  at: number
  source: string
  brain: string
  instant: boolean
}

/** GET /api/timings: the latest turns, and each brain's median and 95th percentile over its last 200. */
export interface TimingsResponse {
  items: Timing[]
  stats: { brain: string; turns: number; p50: StageTimes; p95: StageTimes }[]
}

/** What OpenRouter cost today, and the daily budget (0: none). */
export interface SpendToday {
  today: number
  budget: number
}

/** GET /api/spend?days=N */
export interface SpendResponse extends SpendToday {
  currency: "USD"
  by_day: { day: string; cost: number }[]
  by_purpose: { purpose: string; cost: number }[]
  // OpenRouter's own count for the key: its day is UTC's, and its limit starts again daily, weekly or monthly
  key: {
    limit: number | null
    limit_remaining: number | null
    usage_daily: number
    limit_reset: "daily" | "weekly" | "monthly" | null
  } | null
}

/** Where a brain's requests are paid: your Claude plan's allowance, OpenRouter's credit, or nowhere (free). */
export type Paid = "plan" | "money" | ""

/** One brain of a benchmark, as planned: what it costs and whether it can run. */
export interface BenchBrainPlan {
  brain: string // as asked: "claude", "ollama:qwen3.5:9b", "openrouter:<model>", "script:<path>"
  label: string // its name for people
  paid: Paid
  where: string // where it runs, in words
  cloud: boolean // your requests go to someone else's servers
  requests: { typical: number; most: number } // what it makes over the whole suite
  ready: boolean // it can run (an OpenRouter budget already spent can't)
  why: string // why not, when it can't
}

/** What a benchmark will do, said before it starts (a run keeps its own). */
export interface BenchPlanView {
  suite: "builtin" | "history"
  cases: number // the requests every brain is asked
  dry_run: true // no tool really runs
  skipped: { say: string; why: string }[] // requests left out: their tools aren't here
  brains: BenchBrainPlan[]
  paid_requests: { typical: number; most: number } // the sum over the brains that cost something and can run
}

/** POST /api/bench/plan: the plan, and the same in plain English as the command line says it. Nothing starts. */
export interface BenchPlan extends BenchPlanView {
  text: string
}

/**
 * How one brain did in a benchmark run. Every figure is a number: 0 where nothing was measured (`cases` says whether
 * anything was asked, `answered` whether anything came back to time).
 */
export interface BenchResult {
  brain: string
  label: string
  paid: Paid
  cases: number // asked so far
  answered: number // the ones that got an answer (the rest errored)
  passed: number
  tool_accuracy: number // 0 to 1
  args_accuracy: number // 0 to 1
  forbidden: number // calls it should never have made
  rounds: number // tool rounds per request, on average
  p50_first_token_ms: number
  p95_first_token_ms: number
  p50_total_ms: number
  p95_total_ms: number
  cost: number // US dollars (OpenRouter's, for a paid model)
  errors: number
  skipped: number // the requests it never got to (it couldn't go on)
  error: string // why it couldn't run, or go on; "" when it could
  failures: { say: string; expected: unknown; got: unknown }[]
}

/** A benchmark run (GET /api/bench, the `bench` event). */
export interface BenchRun {
  id: number
  at: number // when it started, epoch seconds
  suite: "builtin" | "history"
  brains: string[]
  status: "running" | "done" | "failed" | "stopped" // stopped: you ended it, its scores so far are kept
  progress: number // 0 to 1
  results: BenchResult[]
  plan: BenchPlanView
  error: string
}

// ---------- backups, documents ----------
/** GET /api/backups */
export interface BackupsState {
  folder: string
  schedule: string
  keep: number
  restic: { configured: boolean; available: boolean; error?: string } // error: why its snapshots can't be listed
  items: { name: string; at: number; size: number; where: "local" | "restic" }[]
}

/** GET /api/documents, the `documents` event: the folders he indexes, and how far he got. */
export interface DocumentsStatus {
  folders: { path: string; files: number; chunks: number; errors: number; indexing: boolean; error?: string }[] // error: why the folder itself can't be read
  last_error: string
}
