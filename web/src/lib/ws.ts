// The one live connection to Orion (/ws), shared by every page. It reconnects by itself with a short backoff, and
// each "init" it receives (always the first message) rebuilds the store and puts the link online; this tab then says
// what its browser offers (`hello`) and what it shares. What else an event does (a toast, fresh data for the REST
// queries, this tab's device answering a request) happens here, so the store stays plain state: an event that carries
// the new data (the connectors, the plugins, a benchmark's progress, an activity entry) puts it straight into the
// queries that show it, and one that only says something changed has them read again.
//
// Binary frames go both ways, and the device link reads and writes them itself: every binary frame that comes (his
// voice) goes to its player, and it sends the microphone through `sendBinary`. Audio levels never enter React:
// listeners take them (`onLevel`), the Core among them.
import type { InfiniteData } from "@tanstack/react-query"
import ReconnectingWebSocket from "partysocket/ws"
import { createDeviceLink, type DeviceLink, type DeviceRequest } from "@/device"
import { toast } from "@/ui/toast"
import { matches, withLive } from "./activity"
import { queryClient, settingsQuery } from "./api"
import { applyEvent, setLink, useLive } from "./store"
import type {
  Activity,
  ActivityPage,
  BenchRun,
  ClientMessage,
  Connector,
  ConnectorDetail,
  EventOf,
  LevelEvent,
  ServerEvent,
  ServerEventType,
} from "./types"

const url = () => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`
const REFUSED = 4403 // he only answers his own pages, on this PC or a paired device: retrying won't change that
const UNPAIRED = 4401 // a device on the network that isn't paired yet: it pairs first (shell/pair.tsx)
const TOO_SLOW = 4408 // this tab fell too far behind: reconnect at once, to a fresh init

let socket: ReconnectingWebSocket | undefined
let link: DeviceLink | undefined

/** Open the connection (once; main.tsx does it). */
export function connect() {
  if (socket) return
  setLink("connecting")
  const ws = new ReconnectingWebSocket(url, [], {
    minReconnectionDelay: 400,
    maxReconnectionDelay: 2400,
    reconnectionDelayGrowFactor: 1.5,
    maxEnqueuedMessages: 0, // a "stop" sent while offline must not fire minutes later
    shouldReconnectOnClose: (e) => e.code !== REFUSED && e.code !== UNPAIRED,
  })
  ws.binaryType = "arraybuffer"
  socket = ws
  ws.addEventListener("close", (e) => {
    setLink(e.code === REFUSED ? "refused" : e.code === UNPAIRED ? "unpaired" : "offline")
    if (e.code === TOO_SLOW) ws.reconnect()
  })
  ws.addEventListener("message", (e) => {
    if (typeof e.data !== "string") {
      deviceLink().playFrame(e.data as ArrayBuffer)
      return
    }
    try {
      receive(JSON.parse(e.data) as ServerEvent)
    } catch (error) {
      console.error("ORION: a live event failed", error)
    }
  })
}

/** Send a message to Orion; false when there is no connection (nothing is queued). */
export function send(message: ClientMessage): boolean {
  return sendJson(message)
}

/** Send any JSON message (the device link speaks the same protocol, with messages of its own). */
function sendJson(message: Record<string, unknown>): boolean {
  if (socket?.readyState !== WebSocket.OPEN) return false
  socket.send(JSON.stringify(message))
  return true
}

/** Send a binary frame (the microphone's samples); false when there is no connection. */
export function sendBinary(data: ArrayBuffer): boolean {
  if (socket?.readyState !== WebSocket.OPEN) return false
  socket.send(data)
  return true
}

/**
 * This browser as one of Orion's devices (src/device): its microphone and speakers, its screen or camera, its
 * clipboard. Made on first use and kept for the page's life.
 * @returns the link
 */
export function deviceLink(): DeviceLink {
  link ??= createDeviceLink({ json: sendJson, binary: sendBinary })
  return link
}

/** Ask him something, with pictures (cut to hud.max_message_chars, as the server would). */
export const say = (text: string, images?: string[]) => {
  const limit = useLive.getState().hud?.max_message_chars
  return send({ type: "chat", text: limit ? text.slice(0, limit) : text, ...(images?.length && { images }) })
}
export const stop = () => send({ type: "stop" })
export const newChat = () => send({ type: "reset" })
export function setMuted(value: boolean) {
  if (send({ type: "mute", value })) useLive.setState({ muted: value }) // the server confirms with "mic"
}
export function setVoice(on: boolean) {
  if (send({ type: "voice", on })) useLive.setState({ voice: on }) // confirmed with "voice"
}
/** Answer one of his questions before a risky action (every HUD closes it on confirm_done). */
export const answerConfirm = (id: string, yes: boolean) => send({ type: "confirm", id, yes })

// ---------- audio levels: about 15 a second per source, kept out of React ----------
type LevelListener = (level: LevelEvent) => void
const levelListeners = new Set<LevelListener>()

/** Call fn with every audio level (the Core, the talk button's ring); returns the unsubscribe function. */
export function onLevel(fn: LevelListener) {
  levelListeners.add(fn)
  return () => {
    levelListeners.delete(fn)
  }
}

// ---------- the hands' live view: about 15 frames a second while a view shows it, kept out of React ----------
type HandsListener = (frame: EventOf<"hands">) => void
const handsListeners = new Set<HandsListener>()

/** Call fn with every frame of the hands' live view (lib/hands.ts keeps the latest); returns the unsubscribe. */
export function onHands(fn: HandsListener) {
  handsListeners.add(fn)
  return () => {
    handsListeners.delete(fn)
  }
}

// ---------- other events, for code that reacts to a moment rather than to state ----------
type AnyListener = (event: ServerEvent) => void
const listeners = new Map<ServerEventType, Set<AnyListener>>()

/** Call fn on each event of one type (a comet, a morph, a neural map reload…); returns the unsubscribe function. */
export function onEvent<T extends ServerEventType>(type: T, fn: (event: EventOf<T>) => void) {
  const set = listeners.get(type) ?? new Set()
  listeners.set(type, set)
  set.add(fn as AnyListener)
  return () => {
    set.delete(fn as AnyListener)
  }
}

// ---------- the answer's text, 25 times a second at most ----------
// A model sends a delta per token (up to 150 a second): gathered and applied every 40 ms, the text reads just as live
// and React renders a third as often as once a frame would, which matters while his voice synthesis loads the CPU.
const pending = new Map<string, string>()
const DELTA_MS = 40
let timer = 0

function flushDeltas() {
  if (timer) clearTimeout(timer)
  timer = 0
  for (const [id, text] of pending) applyEvent({ type: "assistant_delta", id, text })
  pending.clear()
}

let brainErrorShown = false

/** Take one event from Orion (exported for tests and the QA hook, which hand it events without a socket). */
export function receive(ev: ServerEvent) {
  if (ev.type === "level") {
    for (const fn of levelListeners) fn(ev)
    return
  }
  if (ev.type === "hands") {
    for (const fn of handsListeners) fn(ev)
    return
  }
  if (ev.type === "assistant_delta") {
    pending.set(ev.id, (pending.get(ev.id) ?? "") + ev.text)
    if (!timer) timer = window.setTimeout(flushDeltas, DELTA_MS)
    return
  }
  if (pending.size) flushDeltas() // keep the order: deltas before whatever follows them
  applyEvent(ev)
  sideEffects(ev)
  for (const fn of listeners.get(ev.type) ?? []) fn(ev)
}

declare global {
  interface Window {
    /** QA (web/README.md, screenshots): canned events without a server. */
    __orion?: { receive: typeof receive }
  }
}
if (typeof window !== "undefined") window.__orion = { ...window.__orion, receive }

// ---------- this tab as a device ----------
/**
 * After each init, in this order: say what this browser offers and what it shares (a new connection knows nothing of
 * it; the device link says each later change of what it shares itself); take the voice again if this tab carried it
 * and nobody else took it meanwhile; then tell the link who carries it now.
 * @param init the init event
 */
function introduce(init: EventOf<"init">) {
  const device = deviceLink()
  send({ type: "hello", device: device.name(), caps: device.caps() })
  send({ type: "sharing", what: device.sharing() })
  if (device.carrying && !init.voice_holder?.device) void device.takeVoice().catch(() => {}) // (it says why itself)
  device.holder(Boolean(init.surface) && init.voice_holder?.device === init.surface)
}

/** A request to this tab alone: the device answers it (a `share` waits for the user's click), or says why not. */
function answerRequest(request: DeviceRequest) {
  deviceLink()
    .handle(request)
    .then((result) => send({ type: "answer", ...result, id: request.id }))
    .catch((error: unknown) =>
      send({ type: "answer", id: request.id, error: error instanceof Error ? error.message : String(error) }),
    )
}

/** How many of a connector's latest entries its page keeps as more arrive. */
const CONNECTOR_ACTIVITY = 50

/**
 * A new activity entry, into every log already read whose filter shows it, and onto its connector's page.
 * @param item the entry
 */
function liveActivity(item: Activity): void {
  for (const [key, data] of queryClient.getQueriesData<InfiniteData<ActivityPage, number | null>>({
    queryKey: ["activity"],
  })) {
    if (!data) continue
    const [, kind, source, q] = key as [string, string, string, string]
    const filter = { kind: kind ? kind.split(",") : [], source: source ? source.split(",") : [], q }
    if (matches(item, filter)) queryClient.setQueryData(key, withLive(data, item))
  }
  if (item.connector)
    queryClient.setQueryData<ConnectorDetail>(["connector", item.connector], (data) =>
      data && !data.activity.some((a) => a.id === item.id)
        ? { ...data, activity: [item, ...data.activity].slice(0, CONNECTOR_ACTIVITY) }
        : data,
    )
}

/**
 * The connectors as the server lists them now, into the list and onto each page already read.
 * @param items the connectors
 */
function liveConnectors(items: Connector[]): void {
  queryClient.setQueryData(["connectors"], { items })
  for (const c of items)
    queryClient.setQueryData<ConnectorDetail>(["connector", c.id], (data) => data && { ...data, ...c })
}

/**
 * A benchmark run as it goes, into the list of runs (newest first).
 * @param run the run
 */
function liveBench(run: BenchRun): void {
  queryClient.setQueryData<{ items: BenchRun[] }>(
    ["bench"],
    (data) => data && { items: [run, ...data.items.filter((r) => r.id !== run.id)].sort((a, b) => b.at - a.at) },
  )
}

function sideEffects(ev: ServerEvent) {
  const refresh = (...keys: string[][]) => {
    for (const queryKey of keys) void queryClient.invalidateQueries({ queryKey })
  }
  switch (ev.type) {
    case "init":
      // after a reconnect anything may have changed: what shows is read again, the rest when it next shows
      refresh(
        ["neural"],
        ["settings"],
        ["models"],
        ["gestures"],
        ["calendar"],
        ["watchers"],
        ["connectors"],
        ["connector"],
        ["skills"],
        ["plugins"],
        ["safety"],
        ["secrets"],
        ["activity"],
        ["backups"],
        ["documents"],
        ["spend"],
        ["timings"],
        ["bench"],
      )
      introduce(ev)
      if (ev.brain_error && !brainErrorShown) {
        brainErrorShown = true
        toast.add({ title: "His brain", description: ev.brain_error, type: "warning", timeout: 12000 })
      }
      break
    case "voice_holder": {
      const surface = useLive.getState().surface
      deviceLink().holder(Boolean(surface) && ev.device === surface)
      break
    }
    case "request":
      answerRequest({ id: ev.id, kind: ev.kind, what: ev.what, text: ev.text })
      break
    case "speaker":
      deviceLink().flushSpeaker(ev.gen)
      break
    case "toast":
      toast.add({ title: ev.title, description: ev.text, type: "info" })
      break
    case "settings_problem": {
      const words = [ev.broken?.message, ...ev.problems.map((p) => p.message)].filter(Boolean)
      if (words.length)
        toast.add({
          title: "config.yaml",
          description: [...new Set(words)].join(" · "),
          type: ev.broken ? "error" : "warning",
          timeout: 12000,
        })
      refresh(["settings"])
      break
    }
    // (a "shutdown" is the store's: the link overlay says he's restarting or how to start him)
    case "settings": {
      refresh(["settings"])
      const touched = (...prefixes: string[]) => ev.changed.some((key) => prefixes.some((p) => key.startsWith(p)))
      if (touched("mcp_servers", "plugins.")) refresh(["gestures"], ["connectors"], ["connector"])
      if (touched("watchers.")) refresh(["watchers"])
      if (touched("documents.")) refresh(["documents"])
      if (touched("backup.")) refresh(["backups"])
      if (touched("safety.")) refresh(["safety"])
      if (touched("skills.")) refresh(["skills"], ["skill"])
      if (touched("openrouter.")) refresh(["spend"])
      // your name or what he calls you (the composer says them), or the HUD's own
      if (ev.changed.some((key) => key === "user.name" || key === "user.honorific" || key.startsWith("hud.")))
        queryClient
          .fetchQuery(settingsQuery)
          .then(({ config }) =>
            useLive.setState((s) => ({
              userName: config.user.name,
              honorific: config.user.honorific,
              hud: s.hud && { ...s.hud, ...config.hud }, // every hud.* key applies at once
            })),
          )
          .catch(() => {}) // the next "init" brings them
      if (ev.changed.some((key) => key.startsWith("brain.") || key.endsWith(".model"))) refresh(["models"], ["claude"])
      break
    }
    case "capabilities":
      refresh(["models"], ["connectors"], ["gestures"])
      break
    case "skill_changed":
      refresh(["skills"], ["skill"], ["neural"])
      break
    case "activity":
      liveActivity(ev.item)
      break
    case "connectors":
      liveConnectors(ev.items)
      break
    case "plugins":
      queryClient.setQueryData(["plugins"], { items: ev.items })
      break
    case "bench": {
      const { type: _type, ts: _ts, ...run } = ev
      liveBench(run)
      break
    }
    case "backup":
      if (ev.phase !== "start") refresh(["backups"])
      if (ev.phase === "failed")
        toast.add({ title: "The backup failed", description: ev.error ?? ev.name, type: "error", timeout: 12000 })
      else if (ev.phase === "done" && ev.error)
        toast.add({
          title: "Backed up, but not off this machine",
          description: ev.error,
          type: "warning",
          timeout: 12000,
        })
      break
    case "documents": {
      const { type: _type, ts: _ts, ...status } = ev
      queryClient.setQueryData(["documents"], status)
      break
    }
    case "spend":
      refresh(["spend"])
      break
    case "timing":
      refresh(["timings"])
      break
    case "memory_indexed": // (a sync of conversation turns alone changes nothing on the map)
      if (ev.files) refresh(["neural"])
      break
    case "memory_review":
      refresh(["neural"])
      break
    case "alert":
      toast.add({
        title: ev.item.title,
        description: ev.item.text,
        type: ev.item.level === "info" ? "info" : ev.item.level === "warn" ? "warning" : "error",
        timeout: ev.item.level === "critical" ? 0 : 8000,
      })
      break
    case "model_pull":
      if (ev.status === "done") {
        toast.add({ title: "Downloaded", description: ev.name, type: "success" })
        refresh(["models"])
      } else if (ev.status === "error") {
        toast.add({ title: "Download failed", description: `${ev.name}: ${ev.error ?? ""}`, type: "error" })
      }
      break
  }
}
