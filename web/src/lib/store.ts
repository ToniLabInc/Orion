// His live state, as the WebSocket tells it: one zustand store for the whole HUD. Components read slices of it
// (useLive((s) => s.state)); lib/ws.ts feeds it every event through applyEvent(). Audio levels never come here: they
// arrive about 15 times a second per source, so they go to listeners (onLevel in lib/ws.ts) instead.
//
// The conversation keeps each answer's OPERATIONS trace: the tools he ran, what he remembered, the skill he followed
// and what he recalled, as steps on the answer, and each turn's stages as they were measured (`timings`, by turn).
// Other AI apps' calls and background work (the memory review) are small "ops" rows of their own. reduce() is pure
// and exported, so the rules are tested without a socket.
import { create } from "zustand"
import type {
  Alert,
  Boot,
  Capabilities,
  ConfirmRequest,
  Headline,
  HistoryMessage,
  HostProfile,
  HudConfig,
  Job,
  MessageSource,
  ModelPull,
  Reminder,
  ServerEvent,
  ShownState,
  SpendToday,
  Stats,
  StepKind,
  StepRecord,
  Surface,
  Timing,
  Todo,
  ToolStart,
  VoiceHolder,
  Weather,
} from "./types"

export type { ShownState, StepKind }
export type Link = "connecting" | "online" | "offline" | "refused" | "unpaired"
export type StepStatus = "running" | "ok" | "failed"

/** One operation on an answer's trace (or on an ops row). */
export interface Step {
  id: string // the tool call's id | `m-${memory id}` | `s-${turn}-${name}` | `r-${turn}` | `rv-${time}`
  kind: StepKind
  name: string // the tool's model-visible name | memory target | skill name | "recall"
  title: string // the tool's title ("Search the web"); "Memory saved" | "Noted" | "Skill" | "Recalled" | "Review"
  server: string
  args: Record<string, unknown>
  at: number // start, ms epoch
  ms?: number // duration, set at the end
  status: StepStatus
  result?: string // what it answered; the first error line when it failed
  images?: string[] // pictures it returned (data URLs)
  ask?: { question: string; text: string } // it waits for your yes: the question's id and words
}

export interface ChatMessage {
  id: string // answers: the turn id; ops rows: `x-${first step id}`; dividers: `d-${time}`
  role: "user" | "assistant" | "ops" | "divider"
  text: string
  source: MessageSource
  title?: string // an announcement's: "Timer", "Reminder"
  client?: string // an ops row's: the other app ("Claude Code"), or "Memory" / "ORION" for background work
  at: number // ms
  streaming?: boolean
  error?: boolean
  interrupted?: boolean
  instant?: boolean // an instant command answered it, without the model
  steps?: Step[]
  images?: string[] // the pictures you sent with it (data URLs)
  device?: string // the HUD it was typed on (its id)
}

export interface Live {
  link: Link
  state: ShownState
  loading: string // the model loading, while state is "loading"
  boot: Boot | null // startup progress (null until init)
  muted: boolean
  voiceAvailable: boolean // he has a microphone
  voice: boolean // his replies are spoken on the PC's speakers
  honorific: string // "" until init: the HUD holds no setting defaults
  userName: string
  brainError: string
  restart: string[] // settings that wait for a restart
  conv: number
  messages: ChatMessage[]
  stepHome: Record<string, string> // step id → the message that holds it
  reminders: Reminder[]
  todos: Todo[]
  weather: Weather | null
  news: Headline[] | null // the top headlines, once he has fetched them
  stats: Stats | null
  vitals: Stats[] // the last 80 s of readings, oldest first
  capabilities: Capabilities | null
  hud: HudConfig | null
  hearing: boolean // speech on standby, before he knows it's for him
  heard: string // what you said aloud, while he works on it
  listenUntil: number // when the conversation window closes (ms; 0 when it's closed)
  caption: { who: "you" | "orion"; text: string; at: number } | null
  transcript: { text: string; purpose: "filler" | "stop" | "answer"; at: number } | null
  pulls: Record<string, ModelPull> // model downloads
  shutdown: { restart: boolean } | null
  surface: string // this tab's HUD id ("" from an older Orion)
  confirms: ConfirmRequest[] // his questions waiting for an answer, oldest first
  host: HostProfile | null // what Orion's machine has
  surfaces: Surface[] // the HUDs connected now
  voiceHolder: VoiceHolder // who carries his voice ("" device: Orion's own machine)
  jobs: Job[] | null // his scheduled jobs (null: this Orion has none to offer)
  running: number[] // the jobs running now
  alerts: Alert[] // the watchers' active alerts
  spend: SpendToday | null // OpenRouter's cost today and the daily budget (null: this Orion doesn't count it)
  timings: Record<string, Timing> // each turn's stages, by turn id (the latest TIMINGS_KEPT)
}

/** How many stats readings the store keeps: 80 s of them (40 until init brings hud.stats_interval_s). */
const vitalsCap = (hud: HudConfig | null) => (hud ? Math.max(2, Math.round(80 / hud.stats_interval_s)) : 40)
/** Another app's calls within this long of its last activity share one row. */
export const GROUP_MS = 30_000
/** How many turns' timings the store keeps (the conversation never shows more). */
export const TIMINGS_KEPT = 200

export const initial: Live = {
  link: "connecting",
  state: "offline",
  loading: "",
  boot: null,
  muted: false,
  voiceAvailable: false,
  voice: true,
  honorific: "",
  userName: "",
  brainError: "",
  restart: [],
  conv: 0,
  messages: [],
  stepHome: {},
  reminders: [],
  todos: [],
  weather: null,
  news: null,
  stats: null,
  vitals: [],
  capabilities: null,
  hud: null,
  hearing: false,
  heard: "",
  listenUntil: 0,
  caption: null,
  transcript: null,
  pulls: {},
  shutdown: null,
  surface: "",
  confirms: [],
  host: null,
  surfaces: [],
  voiceHolder: { device: "", name: "" },
  jobs: null,
  running: [],
  alerts: [],
  spend: null,
  timings: {},
}

export const useLive = create<Live>()(() => initial)

/** The connection is down, or not up yet: nothing he was doing can be seen. ("init" puts the link online.) */
export function setLink(link: Exclude<Link, "online">) {
  useLive.setState({ link, state: "offline", hearing: false, listenUntil: 0 })
}

/** Apply one server event to the store. */
export function applyEvent(ev: ServerEvent): void {
  useLive.setState((s) => reduce(s, ev, Date.now()) ?? s)
}

let counter = 0
const localId = (prefix: string) => `${prefix}${Date.now().toString(36)}${(counter++).toString(36)}`
const at = (ts: number | undefined, now: number) => (ts ? ts * 1000 : now)

/** A stored operation as a trace step. */
export function fromRecord(r: StepRecord): Step {
  return {
    id: r.id,
    kind: r.kind,
    name: r.name,
    title: r.title || r.name,
    server: r.server,
    args: r.args ?? {},
    at: r.at * 1000,
    ms: r.ms,
    status: r.ok ? "ok" : "failed",
    result: r.result,
    ...(r.images?.length && { images: r.images }),
  }
}

const stepOf = (ev: ToolStart, now: number, ts?: number): Step => ({
  id: ev.id,
  kind: "tool",
  name: ev.name,
  title: ev.title || ev.name,
  server: ev.server,
  args: ev.args ?? {},
  at: at(ts, now),
  status: "running",
})

/** The streaming answer, if one is coming in. */
const streamingIndex = (s: Live) => s.messages.findLastIndex((m) => m.role === "assistant" && m.streaming === true)

/** Put a step on its turn's answer. `create`: make the answer if it hasn't arrived yet (tool and recall starts). */
function onAnswer(s: Live, turn: string, step: Step, now: number, create: boolean): Partial<Live> | undefined {
  let messages = s.messages
  let i = turn ? messages.findLastIndex((m) => m.id === turn) : -1
  if (i < 0) i = streamingIndex(s)
  if (i < 0) {
    if (!create || !turn) return undefined
    messages = [
      ...messages,
      { id: turn, role: "assistant", text: "", source: "hud", at: now, streaming: true, steps: [] },
    ]
    i = messages.length - 1
  }
  const m = messages[i]
  if (m.steps?.some((st) => st.id === step.id)) return messages === s.messages ? undefined : { messages }
  const next = { ...m, steps: [...(m.steps ?? []), step] }
  return { messages: messages.with(i, next), stepHome: { ...s.stepHome, [step.id]: next.id } }
}

/** Another app's call, or background work: on the last row when it is the same client's and still recent. */
function onGroup(s: Live, source: "app" | "background", client: string, step: Step, now: number): Partial<Live> {
  const last = s.messages.at(-1)
  if (last?.role === "ops" && last.client === client) {
    const busy = (last.steps ?? []).reduce((t, x) => Math.max(t, x.at + (x.ms ?? now - x.at)), last.at)
    if (now - busy < GROUP_MS) {
      const next = { ...last, steps: [...(last.steps ?? []), step] }
      return { messages: s.messages.with(-1, next), stepHome: { ...s.stepHome, [step.id]: next.id } }
    }
  }
  const group: ChatMessage = { id: `x-${step.id}`, role: "ops", text: "", source, client, at: step.at, steps: [step] }
  return { messages: [...s.messages, group], stepHome: { ...s.stepHome, [step.id]: group.id } }
}

/** Change one step wherever it is. */
function patchStep(s: Live, id: string, change: (st: Step) => Step): Partial<Live> | undefined {
  const home = s.stepHome[id]
  const i = home ? s.messages.findLastIndex((m) => m.id === home) : -1
  if (i < 0) return undefined // started before this HUD connected: init.live_turn covered it
  const m = s.messages[i]
  return { messages: s.messages.with(i, { ...m, steps: m.steps?.map((st) => (st.id === id ? change(st) : st)) }) }
}

/** A step that no longer waits for an answer. */
const withoutAsk = ({ ask: _ask, ...st }: Step): Step => st

/** Change one message by id. */
function patchMessage(s: Live, i: number, change: Partial<ChatMessage>): Partial<Live> {
  return { messages: s.messages.with(i, { ...s.messages[i], ...change }) }
}

/** The conversation as init rebuilds it: messages 1:1, consecutive ops rows of one app within GROUP_MS folded. */
export function groupHistory(history: HistoryMessage[]): ChatMessage[] {
  const out: ChatMessage[] = []
  for (const h of history) {
    const steps = h.steps?.map(fromRecord)
    const last = out.at(-1)
    if (h.role === "ops" && last?.role === "ops" && last.client === h.client && steps?.length) {
      const busy = (last.steps ?? []).reduce((t, x) => Math.max(t, x.at + (x.ms ?? 0)), last.at)
      if (h.ts * 1000 - busy < GROUP_MS) {
        out[out.length - 1] = { ...last, steps: [...(last.steps ?? []), ...steps] }
        continue
      }
    }
    out.push({
      id: h.id,
      role: h.role,
      text: h.content,
      source: h.source,
      at: h.ts * 1000,
      ...(h.client && { client: h.client }),
      ...(steps?.length && { steps }),
    })
  }
  return out
}

/** Where each step lives, for a fresh list of messages. */
function homes(messages: ChatMessage[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of messages) for (const st of m.steps ?? []) out[st.id] = m.id
  return out
}

/** One server event's change to the state (undefined: nothing to change). Pure: the time comes in `now`. */
export function reduce(s: Live, ev: ServerEvent, now: number): Partial<Live> | undefined {
  switch (ev.type) {
    case "init": {
      // after every (re)connect: all of it, fresh
      let messages = groupHistory(ev.history)
      if (ev.live_turn) {
        const live = ev.live_turn
        messages = [
          ...messages.filter((m) => m.id !== live.id),
          {
            id: live.id,
            role: "assistant",
            text: live.text,
            source: "hud",
            at: now,
            streaming: true,
            steps: live.steps.map(fromRecord),
          },
        ]
      }
      return {
        ...initial,
        link: "online",
        vitals: ev.stats_history?.length
          ? ev.stats_history.slice(-vitalsCap(ev.hud))
          : ev.stats
            ? [...s.vitals, ev.stats].slice(-vitalsCap(ev.hud))
            : s.vitals,
        state: ev.state,
        loading: ev.loading,
        boot: ev.boot,
        muted: ev.muted,
        voiceAvailable: ev.voice_available,
        voice: ev.voice,
        honorific: ev.honorific,
        userName: ev.user_name,
        brainError: ev.brain_error,
        restart: ev.restart,
        conv: ev.conv,
        messages,
        stepHome: homes(messages),
        reminders: ev.reminders,
        todos: ev.todos,
        weather: ev.weather,
        news: ev.news ?? null,
        stats: ev.stats,
        capabilities: ev.capabilities,
        hud: ev.hud,
        surface: ev.surface ?? "",
        confirms: ev.confirms ?? [],
        host: ev.host ?? null,
        surfaces: ev.surfaces ?? [],
        voiceHolder: ev.voice_holder ?? initial.voiceHolder,
        jobs: ev.jobs ?? null,
        alerts: ev.alerts ?? [],
        spend: ev.spend ?? null,
        timings: s.timings, // a turn's stages stay true across a reconnect
      }
    }
    case "boot": {
      // init says whether a microphone listener exists, but the listener only opens once his ears have loaded: a HUD
      // that connected while he was starting learns it here (ears "ok": listening; an error: no microphone after all)
      const ears = ev.steps.find((st) => st.name === "ears")
      const heard = ears && (ears.status === "ok" || ears.status === "error" || ears.status === "skipped")
      return { boot: { ready: ev.ready, steps: ev.steps }, ...(heard && { voiceAvailable: ears.status === "ok" }) }
    }
    case "state":
      return {
        state: ev.state,
        loading: ev.state === "loading" ? (ev.model ?? s.loading) : "",
        ...(ev.state === "idle" && { heard: "" }),
      }
    case "wake":
      return { hearing: false, listenUntil: 0, state: "listening" }
    case "listen":
      return { listenUntil: now + ev.seconds * 1000 }
    case "hearing":
      return { hearing: ev.active }
    case "caption":
      return { caption: { who: "orion", text: ev.text, at: now } }
    case "transcript":
      return { transcript: { text: ev.text, purpose: ev.purpose, at: now } }
    case "user_message": {
      const message: ChatMessage = {
        id: ev.turn ? `u-${ev.turn}` : localId("u"),
        role: "user",
        text: ev.text,
        source: ev.source,
        at: at(ev.ts, now),
        ...(ev.images?.length && { images: ev.images }),
        ...(ev.device && { device: ev.device }),
      }
      return {
        messages: [...s.messages, message],
        ...(ev.source === "voice" && { heard: ev.text, caption: { who: "you" as const, text: ev.text, at: now } }),
      }
    }
    case "assistant_start": {
      const i = s.messages.findLastIndex((m) => m.id === ev.id)
      if (i >= 0) return ev.instant ? patchMessage(s, i, { instant: true }) : undefined // an early step made it
      const message: ChatMessage = {
        id: ev.id,
        role: "assistant",
        text: "",
        source: "hud",
        at: at(ev.ts, now),
        streaming: true,
        steps: [],
        ...(ev.instant && { instant: true }),
      }
      return { messages: [...s.messages, message] }
    }
    case "assistant_delta": {
      const i = s.messages.findLastIndex((m) => m.id === ev.id)
      if (i < 0)
        return {
          messages: [
            ...s.messages,
            { id: ev.id, role: "assistant", text: ev.text, source: "hud", at: now, streaming: true, steps: [] },
          ],
        }
      return patchMessage(s, i, { text: s.messages[i].text + ev.text })
    }
    case "assistant_done": {
      // the whole answer: also how one arrives after a reconnect in the middle of it
      let i = s.messages.findLastIndex((m) => m.id === ev.id)
      if (i < 0) i = streamingIndex(s)
      const instant = ev.instant ? { instant: true } : {}
      if (i < 0)
        return {
          messages: [
            ...s.messages,
            { id: ev.id, role: "assistant", text: ev.text, source: "hud", at: now, error: ev.error, ...instant },
          ],
        }
      return patchMessage(s, i, { text: ev.text, error: ev.error, streaming: false, ...instant })
    }
    case "assistant_interrupted": {
      let i = ev.id ? s.messages.findLastIndex((m) => m.id === ev.id) : -1
      if (i < 0) i = streamingIndex(s)
      return i < 0 ? undefined : patchMessage(s, i, { interrupted: true, streaming: false })
    }
    case "tool": {
      if (ev.phase === "ask")
        return patchStep(s, ev.id, (st) => ({ ...st, ask: { question: ev.question, text: ev.text } }))
      if (ev.phase === "end")
        return patchStep(s, ev.id, (step) => ({
          ...withoutAsk(step),
          status: ev.ok ? "ok" : "failed",
          ms: ev.ms ?? at(ev.ts, now) - step.at,
          result: ev.ok ? ev.result : ev.error || ev.result,
          ...(ev.images?.length && { images: ev.images }),
        }))
      if (ev.source === "hud") return undefined // the widget you used shows it already
      const step = stepOf(ev, now, ev.ts)
      if (ev.source === "app") return onGroup(s, "app", ev.client || "Another app", step, now)
      return onAnswer(s, ev.turn, step, now, true) ?? onGroup(s, "background", "ORION", step, now)
    }
    case "recall": {
      const texts = ev.items.map((item) => item.text)
      const step: Step = {
        id: `r-${ev.turn}`,
        kind: "recall",
        name: "recall",
        title: "Recalled",
        server: "",
        args: { memories: texts.join(" · ") },
        at: at(ev.ts, now) - ev.ms,
        ms: ev.ms,
        status: "ok",
        result: texts.map((t) => `• ${t}`).join("\n"),
      }
      return onAnswer(s, ev.turn, step, now, true)
    }
    case "memory_update": {
      const title = ev.profile ? "Memory saved" : "Noted"
      if (ev.call && s.stepHome[ev.call])
        return patchStep(s, ev.call, (st) => ({ ...st, kind: "memory", title, args: { text: ev.text } }))
      const step: Step = {
        id: `m-${ev.id || localId("")}`,
        kind: "memory",
        name: ev.target,
        title,
        server: "memory",
        args: { text: ev.text },
        at: at(ev.ts, now),
        ms: 0,
        status: ev.ok ? "ok" : "failed",
        result: ev.result,
      }
      if (ev.turn) return onAnswer(s, ev.turn, step, now, false)
      return onGroup(s, "background", "Memory", step, now)
    }
    case "skill_used": {
      if (ev.call && s.stepHome[ev.call])
        // use_skill: the tool's own row becomes the skill's
        return patchStep(s, ev.call, (st) => ({ ...st, kind: "skill", title: "Skill", args: { name: ev.name } }))
      const turn = ev.turn ?? ""
      const step: Step = {
        id: `s-${turn}-${ev.name}`,
        kind: "skill",
        name: ev.name,
        title: "Skill",
        server: "",
        args: { name: ev.name },
        at: at(ev.ts, now),
        ms: 0,
        status: "ok",
      }
      return onAnswer(s, turn, step, now, false)
    }
    case "memory_review": {
      if (!ev.changes.length && !ev.error) return undefined
      const step: Step = {
        id: localId("rv-"),
        kind: "memory",
        name: "memory_review",
        title: "Review",
        server: "memory",
        args: { changes: ev.changes.length },
        at: at(ev.ts, now),
        ms: 0,
        status: ev.error ? "failed" : "ok",
        result: ev.error ?? ev.changes.join("\n"),
      }
      return onGroup(s, "background", "Memory", step, now)
    }
    case "reminders":
      return { reminders: ev.items }
    case "todos":
      return { todos: ev.items }
    case "weather":
      return { weather: ev.weather }
    case "news":
      return { news: ev.items }
    case "stats":
      return { stats: ev.stats, vitals: [...s.vitals, ev.stats].slice(-vitalsCap(s.hud)) }
    case "announcement": {
      const message: ChatMessage = {
        id: ev.id ?? localId("a"),
        role: "assistant",
        text: ev.text,
        source: "announcement",
        title: ev.title,
        at: at(ev.ts, now),
      }
      return { messages: [...s.messages, message] }
    }
    case "voice":
      return { voice: ev.on }
    case "mic":
      return { muted: ev.muted }
    case "capabilities": {
      const { type: _type, ts: _ts, ...capabilities } = ev
      return { capabilities }
    }
    case "settings":
      return { restart: ev.restart }
    case "shutdown":
      return { shutdown: { restart: Boolean(ev.restart) } }
    case "conversation_reset":
      if (ev.reason === "idle_gap")
        // a long pause: the log stays, with a line where the new conversation starts
        return {
          conv: ev.conv ?? s.conv,
          messages: [...s.messages, { id: localId("d-"), role: "divider", text: "", source: "background", at: now }],
        }
      return { conv: ev.conv ?? s.conv, messages: [], stepHome: {}, heard: "", caption: null }
    case "model_pull":
      return { pulls: { ...s.pulls, [ev.name]: ev } }
    case "confirm": {
      const { type: _type, ts: _ts, ...question } = ev
      return { confirms: [...s.confirms.filter((c) => c.id !== question.id), question] }
    }
    case "confirm_done":
      return {
        confirms: s.confirms.filter((c) => c.id !== ev.id),
        // the trace's step that asked stops asking (its end comes on its own)
        messages: s.messages.map((m) =>
          m.steps?.some((st) => st.ask?.question === ev.id)
            ? { ...m, steps: m.steps.map((st) => (st.ask?.question === ev.id ? withoutAsk(st) : st)) }
            : m,
        ),
      }
    case "surfaces":
      return { surfaces: ev.items }
    case "voice_holder":
      return { voiceHolder: { device: ev.device, name: ev.name } }
    case "jobs":
      return { jobs: ev.items }
    case "job_run":
      return {
        running:
          ev.phase === "start"
            ? [...s.running.filter((id) => id !== ev.id), ev.id]
            : s.running.filter((id) => id !== ev.id),
      }
    case "alerts":
      return { alerts: ev.items }
    case "alert":
      return { alerts: [...s.alerts.filter((a) => a.id !== ev.item.id), ev.item] }
    case "spend":
      return { spend: { today: ev.today, budget: ev.budget } }
    case "timing": {
      const { type: _type, ts: _ts, ...timing } = ev
      const kept = Object.entries(s.timings).filter(([turn]) => turn !== timing.turn)
      return { timings: Object.fromEntries([...kept.slice(-(TIMINGS_KEPT - 1)), [timing.turn, timing]]) }
    }
    case "request": // this tab's device answers it (lib/ws.ts)
    case "speaker": // the player drops what it holds (lib/ws.ts)
    case "memory_indexed": // lib/ws.ts reads the neural map again
    case "settings_problem": // shown by lib/ws.ts
    case "skill_changed":
    case "summon": // the stage's own store (lib/stage.ts)
    case "level": // never stored: see onLevel in lib/ws.ts
    case "hands": // never stored either: see onHands in lib/ws.ts
    case "intent": // the gesture chip's (shell/gesture.tsx)
    case "toast": // shown by lib/ws.ts
    case "activity": // the activity page's own (lib/ws.ts puts it in the queries)
    case "connectors":
    case "plugins":
    case "bench":
    case "backup":
    case "documents":
      return undefined
  }
}
