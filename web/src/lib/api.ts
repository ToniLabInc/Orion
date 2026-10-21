// Orion's REST API (/api), with TanStack Query: one hook per endpoint. Live events keep the data fresh (lib/ws.ts
// invalidates the matching keys), so nothing refetches on window focus or on a timer.
import {
  infiniteQueryOptions,
  keepPreviousData,
  MutationCache,
  QueryClient,
  queryOptions,
  useInfiniteQuery,
  useMutation,
  useQuery,
} from "@tanstack/react-query"
import { ask } from "@/ui/ask"
import { toast } from "@/ui/toast"
import { type ActivityFilter, activityParams } from "./activity"
import { useLive } from "./store"
import type {
  ActivityPage,
  BackupsState,
  BenchPlan,
  BenchRun,
  CalendarResponse,
  Channel,
  ChannelRules,
  ClaudeStatus,
  Connector,
  ConnectorDetail,
  DirectoryItem,
  DocumentsStatus,
  GestureBinding,
  GesturesPage,
  Job,
  JobInput,
  Marketplace,
  ModelsResponse,
  NeuralGraph,
  NeuralNode,
  NewConnector,
  OpenRouterModel,
  PairingState,
  Plugin,
  PluginListing,
  SafetyState,
  SavedSettings,
  SecretsState,
  SettingsPatch,
  SettingsResponse,
  Skill,
  SkillDetail,
  SkillListing,
  SpendResponse,
  TimingsResponse,
  ToolCallResult,
  WatchersResponse,
} from "./types"

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** fetch() for Orion's API: JSON in and out, and his own error message (FastAPI's "detail") when it fails. */
export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = init
  const response = await fetch(path, {
    ...rest,
    headers: json === undefined ? rest.headers : { "Content-Type": "application/json", ...rest.headers },
    body: json === undefined ? rest.body : JSON.stringify(json),
  })
  if (!response.ok) {
    const detail = await response
      .json()
      .then((body) => body?.detail)
      .catch(() => undefined)
    throw new ApiError(
      response.status,
      typeof detail === "string" ? detail : `${response.status} ${response.statusText}`,
    )
  }
  return response.headers.get("content-type")?.includes("application/json") ? response.json() : (response as T)
}

const put = <T>(path: string, json: unknown) => api<T>(path, { method: "PUT", json })
const post = <T>(path: string, json?: unknown) => api<T>(path, { method: "POST", json })
const patch = <T>(path: string, json: unknown) => api<T>(path, { method: "PATCH", json })
const del = <T>(path: string) => api<T>(path, { method: "DELETE" })
const part = encodeURIComponent

declare module "@tanstack/react-query" {
  interface Register {
    // title: the toast's title when the change fails; quiet: the mutation shows its own errors
    mutationMeta: { title?: string; quiet?: boolean }
  }
}

/**
 * Whether an endpoint isn't offered by this Orion (an older one): asking again won't change that.
 * @param error what failed
 * @returns true for a 404 or 405
 */
export const missing = (error: unknown) => error instanceof ApiError && (error.status === 404 || error.status === 405)

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: (count, error) => !missing(error) && count < 1,
    },
  },
  // a failed change says why in a toast
  mutationCache: new MutationCache({
    onError: (error, _vars, _ctx, mutation) => {
      if (!mutation.meta?.quiet)
        toast.add({ title: mutation.meta?.title ?? "That didn't work", description: error.message, type: "error" })
    },
  }),
})

/** A settings change, applied with the restart list it returns. */
const remember = (saved: SavedSettings) => useLive.setState({ restart: saved.restart })

/**
 * A change shown before the server confirms it: a query's fetches are cancelled and its data changed at once
 * (`restore` puts it back when the server refuses).
 * @param key the query
 * @param change its data, changed
 * @returns what `restore` needs
 */
async function optimistic<T>(key: readonly unknown[], change: (data: T) => T) {
  await queryClient.cancelQueries({ queryKey: key, exact: true })
  const before = queryClient.getQueryData<T>(key)
  if (before !== undefined) queryClient.setQueryData<T>(key, change(before))
  return { key, before }
}

/** A mutation's onError after `optimistic`: the query's data as it was. */
const restore = (_error: unknown, _vars: unknown, context?: { key: readonly unknown[]; before?: unknown }) => {
  if (context?.before !== undefined) queryClient.setQueryData(context.key, context.before)
}

// ---------- settings ----------
export const settingsQuery = queryOptions({
  queryKey: ["settings"],
  queryFn: () => api<SettingsResponse>("/api/settings"),
})
export const useSettings = () => useQuery(settingsQuery)

/** A patch as sections of options: {"tts.speed": 1.2} becomes {tts: {speed: 1.2}} (top-level keys stay). */
export function nested(patch: SettingsPatch): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(patch)) {
    const dot = key.indexOf(".")
    if (dot < 0) {
      out[key] =
        typeof value === "object" && value !== null && !Array.isArray(value) && typeof out[key] === "object"
          ? { ...(out[key] as object), ...(value as object) }
          : value
      continue
    }
    const section = key.slice(0, dot)
    out[section] = { ...((out[section] as object) ?? {}), [key.slice(dot + 1)]: value }
  }
  return out
}

/** Merge a nested patch into the config, as the server does. */
function merge<T>(base: T, patch: unknown): T {
  if (typeof patch !== "object" || patch === null || Array.isArray(patch)) return patch as T
  const out: Record<string, unknown> = { ...(base as Record<string, unknown>) }
  for (const [key, value] of Object.entries(patch)) out[key] = merge(out[key], value)
  return out as T
}

/** Save a change at once: the page shows it straight away and puts the old value back if Orion refuses it. The
 * result's `changed` says which keys really changed (the SAVED tick). */
export const useSaveSettings = () =>
  useMutation({
    mutationKey: ["settings", "save"],
    meta: { title: "NOT SAVED" },
    mutationFn: (patch: SettingsPatch) => put<SavedSettings>("/api/settings", patch),
    onMutate: (patch) =>
      optimistic<SettingsResponse>(["settings"], (before) => ({
        ...before,
        config: merge(before.config, nested(patch)),
      })),
    onError: restore,
    onSuccess: remember,
  })

// ---------- models ----------
export const useModels = () => useQuery({ queryKey: ["models"], queryFn: () => api<ModelsResponse>("/api/models") })

export const useOpenRouterModels = (enabled = true) =>
  useQuery({
    queryKey: ["openrouter"],
    queryFn: () => api<{ models: OpenRouterModel[]; current: string }>("/api/openrouter/models"),
    staleTime: 600_000, // the server keeps them ten minutes too
    enabled,
  })

/** Claude Code's sign-in and the models of the plan. */
export const useClaude = () => useQuery({ queryKey: ["claude"], queryFn: () => api<ClaudeStatus>("/api/claude") })

/** Download an Ollama model: progress arrives as model_pull events (useLive((s) => s.pulls)). */
export const usePullModel = () =>
  useMutation({ mutationFn: (name: string) => post<{ ok: true }>("/api/models/pull", { name }) })

export const useDeleteModel = () =>
  useMutation({
    mutationFn: (name: string) => del<{ ok: true }>(`/api/models/llm/${name.split("/").map(part).join("/")}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }),
  })

/** A few words in one of his voices, as a WAV to play: new Audio(URL.createObjectURL(blob)). */
export async function previewVoice(voice: string, speed?: number, text?: string): Promise<Blob> {
  const response = await api<Response>("/api/voice/preview", { method: "POST", json: { voice, speed, text } })
  return response.blob()
}

/** Run a tool yourself, without him (the same full access he has). Its failures are results, not errors. */
export const useCallTool = () =>
  useMutation({
    meta: { quiet: true },
    mutationFn: ({ server, tool, args = {} }: { server: string; tool: string; args?: Record<string, unknown> }) =>
      post<ToolCallResult>("/api/tools/call", { server, tool, args }),
  })

/** What a tool answered: whether it was done, and its words. */
export interface ToolAnswer {
  ok: boolean
  result: string
}

/**
 * A tool of one of his servers, run for you (``useCallTool``): `run` resolves to what it answered, a request that
 * fails included, and, given `say`, says it in a toast titled `say.title` (a failure always, a success too with
 * `success`).
 * @returns `run`, and whether a call is under way
 */
export function useTool() {
  const call = useCallTool()
  const run = async (
    server: string,
    tool: string,
    args: Record<string, unknown>,
    say?: { title: string; success?: boolean; timeout?: number },
  ): Promise<ToolAnswer> => {
    let answer: ToolAnswer
    try {
      answer = await call.mutateAsync({ server, tool, args })
    } catch (error) {
      answer = { ok: false, result: (error as Error).message }
    }
    if (say && (!answer.ok || say.success)) {
      const timeout = answer.ok ? say.timeout : undefined // a failure stays its usual time
      toast.add({ title: say.title, description: answer.result, type: answer.ok ? "info" : "error", timeout })
    }
    return answer
  }
  return { run, pending: call.isPending }
}

/** run(tool, args, title): runs one of his organizer's tools from a widget; true when it did, otherwise a toast titled
 * `title` says why. The lists update themselves (the organizer sends "reminders" and "todos" events). */
export function useOrganizer() {
  const { run } = useTool()
  return async (tool: string, args: Record<string, unknown>, title: string) =>
    (await run("organizer", tool, args, { title })).ok
}

/** news(): asks his web tools for the top headlines, which arrive as a "news" event (the NEWS panel). Resolves to null
 * when they came, or to what he answered instead (no internet, no news found). */
export function useNews() {
  const { run } = useTool()
  return async (): Promise<string | null> => {
    const { ok, result } = await run("web", "get_news", {})
    return ok && result.startsWith("- ") ? null : result
  }
}

// ---------- pairing ----------
/** Whether this device may use the HUD; on this PC, the devices paired (Settings, General, Devices). */
export const usePairing = () => useQuery({ queryKey: ["pairing"], queryFn: () => api<PairingState>("/api/pairing") })

/** A new pairing code, to show on this PC (any code before stops working). */
export const useStartPairing = () =>
  useMutation({
    meta: { title: "NO CODE" },
    mutationFn: () => post<{ code: string; ends: number }>("/api/pairing/code"),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["pairing"] }),
  })

/** Unpair one device (its id), or every device (null). */
export const useUnpair = () =>
  useMutation({
    meta: { title: "NOT UNPAIRED" },
    mutationFn: (id: string | null) =>
      del<{ unpaired: number }>(id === null ? "/api/pairing/devices" : `/api/pairing/devices/${part(id)}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["pairing"] }),
  })

/** Pair this device with the code shown on the PC: its key comes back as a cookie. */
export const pair = (code: string, name: string) =>
  post<{ paired: boolean; id: string; name: string }>("/api/pairing", { code, name })

// ---------- gestures ----------
/** The gesture remotes, what each gesture does, and what it could do (the Gestures page, the HANDS panel). */
export const useGestures = () => useQuery({ queryKey: ["gestures"], queryFn: () => api<GesturesPage>("/api/gestures") })

/** Save what each of a remote's gestures does (the whole set; null for a gesture that now does nothing): the page
 * shows it at once, and the set before comes back if Orion refuses it. */
export const useSaveGestures = () =>
  useMutation({
    meta: { title: "NOT SAVED" },
    mutationFn: ({ server, bindings }: { server: string; bindings: Record<string, GestureBinding | null> }) =>
      put<SavedSettings>(`/api/gestures/${part(server)}`, { bindings }),
    onMutate: ({ server, bindings }) => {
      const kept = Object.fromEntries(
        Object.entries(bindings).filter((entry): entry is [string, GestureBinding] => entry[1] !== null),
      )
      return optimistic<GesturesPage>(["gestures"], (before) => ({
        ...before,
        remotes: before.remotes.map((r) => (r.name === server ? { ...r, bindings: kept, custom: true } : r)),
      }))
    },
    onError: restore,
    onSuccess: (saved) => {
      remember(saved)
      void queryClient.invalidateQueries({ queryKey: ["gestures"] }) // the words of each binding, from the server
    },
  })

/** A remote's gestures do the defaults again. */
export const useResetGestures = () =>
  useMutation({
    meta: { title: "NOT RESET" },
    mutationFn: (server: string) => del<SavedSettings>(`/api/gestures/${part(server)}`),
    onSuccess: (saved) => {
      remember(saved)
      void queryClient.invalidateQueries({ queryKey: ["gestures"] })
    },
  })

// ---------- memory ----------
export const useNeural = (enabled = true) =>
  useQuery({ queryKey: ["neural"], queryFn: () => api<NeuralGraph>("/api/neural"), enabled })

/** One node of the map with its text (and when and how a memory was learned); null for no node. */
export const useNeuralNode = (id: string | null) =>
  useQuery({
    queryKey: ["neural", "node", id],
    queryFn: () => api<NeuralNode>(`/api/neural/node/${id?.split("/").map(part).join("/")}`),
    enabled: Boolean(id),
  })

export const useReviewMemory = () => useMutation({ mutationFn: () => post<{ ok: true }>("/api/memory/review") })

/** Forget one memory of the map for good (an entry, a journal day or a note); the map reads itself again. */
export const useForgetNode = () =>
  useMutation({
    meta: { title: "FORGET" },
    mutationFn: (id: string) => del<{ ok: true; message: string }>(`/api/neural/node/${part(id)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["neural"] }),
  })

/** Trust an unverified memory: it moves to the file it was meant for. */
export const useConfirmNode = () =>
  useMutation({
    meta: { title: "CONFIRM" },
    mutationFn: (id: string) => post<{ ok: true; message: string }>(`/api/neural/node/${part(id)}/confirm`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["neural"] }),
  })

/** Forget everything he knows: he restarts with a blank memory (settings, timers, tasks and skills stay). */
export const useForgetEverything = () =>
  useMutation({ meta: { title: "FORGET EVERYTHING" }, mutationFn: () => post<{ ok: true }>("/api/memory/forget") })

// ---------- power ----------
export const useRestart = () =>
  useMutation({ meta: { title: "RESTART" }, mutationFn: () => post<{ ok: true }>("/api/restart") })
export const useShutdown = () =>
  useMutation({ meta: { title: "SHUT DOWN" }, mutationFn: () => post<{ ok: true }>("/api/shutdown") })

/** power(what): restart Orion or shut him down, once you say yes (the palette's actions). */
export function usePower() {
  const restart = useRestart()
  const shutdown = useShutdown()
  return async (what: "restart" | "shutdown") => {
    const question =
      what === "restart"
        ? { title: "Restart Orion now?", text: "He's back in a moment.", ok: "Restart", kicker: "Power" }
        : {
            title: "Shut down Orion?",
            text: "He stops listening and frees the GPU. Start him again from the app menu or with “orion start”.",
            ok: "Shut down",
            danger: true,
            kicker: "Power",
          }
    if (await ask(question)) (what === "restart" ? restart : shutdown).mutate()
  }
}

// ---------- jobs: scheduled work ----------
/**
 * His scheduled jobs: live from the connection (init and the `jobs` event), or read once from the REST API when this
 * Orion's init doesn't carry them.
 * @returns the jobs (null while unknown), why they couldn't be read, and whether they are being read
 */
export function useJobs() {
  const live = useLive((s) => s.jobs)
  const query = useQuery({ queryKey: ["jobs"], queryFn: () => api<Job[]>("/api/jobs"), enabled: live === null })
  return {
    jobs: live ?? query.data ?? null,
    error: live === null ? query.error : null,
    pending: live === null && query.isPending,
    retry: () => void query.refetch(),
  }
}

/** A new job, or a change to one (`id` given): the list updates with the `jobs` event. */
export const useSaveJob = () =>
  useMutation({
    meta: { title: "Job not saved" },
    mutationFn: ({ id, job }: { id?: number; job: Partial<JobInput> }) =>
      id === undefined ? post<Job>("/api/jobs", job) : put<Job>(`/api/jobs/${id}`, job),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  })

export const useDeleteJob = () =>
  useMutation({
    meta: { title: "Job not deleted" },
    mutationFn: (id: number) => del<{ ok: true }>(`/api/jobs/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  })

/** Run a job now (its run shows as job_run events). */
export const useRunJob = () =>
  useMutation({
    meta: { title: "Job didn't start" },
    mutationFn: (id: number) => post<{ ok: true }>(`/api/jobs/${id}/run`),
  })

/**
 * A schedule in English, or why it isn't one (while you type it).
 * @param schedule the cron expression
 * @returns {explain} or throws an ApiError whose message says what is wrong
 */
export const explainSchedule = (schedule: string) =>
  post<{ schedule: string; explain: string; next_run: number | null }>("/api/jobs/explain", { schedule })

// ---------- watchers ----------
/** The sensors now and each watcher rule. */
export const useWatchers = () =>
  useQuery({ queryKey: ["watchers"], queryFn: () => api<WatchersResponse>("/api/watchers"), staleTime: 15_000 })

// ---------- calendar ----------
/** The next `days` of the calendars he reads (empty when none is set up). */
export const useCalendar = (days = 7) =>
  useQuery({
    queryKey: ["calendar", days],
    queryFn: () => api<CalendarResponse>(`/api/calendar?days=${days}`),
    staleTime: 5 * 60_000,
    refetchInterval: 15 * 60_000, // calendars change elsewhere: look again now and then while it shows
    refetchIntervalInBackground: false,
  })

// ---------- the activity log ----------
/** The log for a filter, newest first, a page at a time; new entries come live (lib/ws.ts puts them in). */
const activityQuery = (filter: ActivityFilter) =>
  infiniteQueryOptions({
    queryKey: ["activity", filter.kind.join(","), filter.source.join(","), filter.q.trim()],
    queryFn: ({ pageParam }) => api<ActivityPage>(`/api/activity?${activityParams(filter, pageParam)}`),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next,
    staleTime: Number.POSITIVE_INFINITY, // nothing changes in it but what arrives live
  })

export const useActivity = (filter: ActivityFilter) =>
  useInfiniteQuery({ ...activityQuery(filter), placeholderData: keepPreviousData })

// ---------- the vault and the safety rules ----------
/** Where secrets are kept and their names (never a value). */
export const useSecrets = () => useQuery({ queryKey: ["secrets"], queryFn: () => api<SecretsState>("/api/secrets") })

/** What knows whether a secret is set: the vault, the connectors, the settings. */
const secretsChanged = () => {
  for (const queryKey of [["secrets"], ["connectors"], ["connector"], ["settings"]])
    void queryClient.invalidateQueries({ queryKey })
}

/** Put a secret in the vault (its value goes there and nowhere else). */
export const useSetSecret = () =>
  useMutation({
    meta: { title: "Secret not saved" },
    mutationFn: ({ name, value }: { name: string; value: string }) =>
      put<{ ok: true }>(`/api/secrets/${part(name)}`, { value }),
    onSuccess: secretsChanged,
  })

export const useDeleteSecret = () =>
  useMutation({
    meta: { title: "Secret not removed" },
    mutationFn: (name: string) => del<{ ok: true }>(`/api/secrets/${part(name)}`),
    onSuccess: secretsChanged,
  })

/** The channels' limits, the sandbox, and what a limit can name. */
export const useSafety = () => useQuery({ queryKey: ["safety"], queryFn: () => api<SafetyState>("/api/safety") })

/** A channel's limits, shown at once (the ones before come back if Orion refuses them). */
export const useSaveChannel = () =>
  useMutation({
    meta: { title: "Limits not saved" },
    mutationFn: ({ channel, rules }: { channel: Channel; rules: ChannelRules }) =>
      put<ChannelRules>(`/api/safety/channels/${channel}`, rules),
    onMutate: ({ channel, rules }) =>
      optimistic<SafetyState>(["safety"], (before) => ({
        ...before,
        channels: { ...before.channels, [channel]: rules },
      })),
    onError: restore,
  })

// ---------- connectors ----------
export const useConnectors = () =>
  useQuery({
    queryKey: ["connectors"],
    queryFn: () => api<{ items: Connector[] }>("/api/connectors"),
    select: (data) => data.items,
  })

/** A connector's page: the connector and what it did lately. */
export const useConnector = (id: string) =>
  useQuery({ queryKey: ["connector", id], queryFn: () => api<ConnectorDetail>(`/api/connectors/${part(id)}`) })

/**
 * A connector as the server now says it is, in the list and on its page (whose activity stays).
 * @param connector the connector
 */
function adoptConnector(connector: Connector): void {
  queryClient.setQueryData<{ items: Connector[] }>(["connectors"], (data) =>
    data?.items.some((c) => c.id === connector.id)
      ? { items: data.items.map((c) => (c.id === connector.id ? connector : c)) }
      : data && { items: [...data.items, connector] },
  )
  queryClient.setQueryData<ConnectorDetail>(["connector", connector.id], (data) => data && { ...data, ...connector })
}

/**
 * A change to a connector shown at once, in the list and on its page.
 * @param id the connector
 * @param change it, changed
 * @returns what `restoreAll` needs
 */
async function optimisticConnector(id: string, change: (connector: Connector) => Connector) {
  return [
    await optimistic<{ items: Connector[] }>(["connectors"], (data) => ({
      items: data.items.map((c) => (c.id === id ? change(c) : c)),
    })),
    await optimistic<ConnectorDetail>(["connector", id], (data) => ({ ...data, ...change(data) })),
  ]
}

/** onError after `optimisticConnector`: both as they were. */
const restoreAll = (error: unknown, vars: unknown, contexts?: { key: readonly unknown[]; before?: unknown }[]) => {
  for (const context of contexts ?? []) restore(error, vars, context)
}

/** Save a connector's own settings: the values typed (a secret left empty keeps the one it has). */
export const useSaveConnector = () =>
  useMutation({
    meta: { title: "Not saved" },
    mutationFn: ({ id, values }: { id: string; values: Record<string, unknown> }) =>
      put<Connector>(`/api/connectors/${part(id)}/settings`, { values }),
    onSuccess: adoptConnector,
  })

/** Switch a connector on or off, or its sandbox. */
export const useSetConnector = () =>
  useMutation({
    meta: { title: "Not changed" },
    mutationFn: ({ id, change }: { id: string; change: { enabled?: boolean; sandbox?: boolean } }) =>
      patch<Connector>(`/api/connectors/${part(id)}`, change),
    onMutate: ({ id, change }) =>
      optimisticConnector(id, (c) => ({
        ...c,
        ...(change.enabled !== undefined && { status: change.enabled ? "connecting" : "off" }),
        ...(change.sandbox !== undefined && { sandbox: change.sandbox ? "bwrap" : "none" }),
      })),
    onError: restoreAll,
    onSuccess: adoptConnector,
  })

/** Let him use one of a connector's tools, or not (nothing restarts). */
export const useSwitchConnectorTool = () =>
  useMutation({
    meta: { title: "Not changed" },
    mutationFn: ({ id, tool, enabled }: { id: string; tool: string; enabled: boolean }) =>
      patch<Connector>(`/api/connectors/${part(id)}/tools/${part(tool)}`, { enabled }),
    onMutate: ({ id, tool, enabled }) =>
      optimisticConnector(id, (c) => ({ ...c, tools: c.tools.map((t) => (t.name === tool ? { ...t, enabled } : t)) })),
    onError: restoreAll,
    onSuccess: adoptConnector,
  })

/**
 * A tab for a sign-in page, opened blank: a browser lets a page open one only inside a click, and the page's address
 * isn't known until Orion has answered (`signIn` points it there).
 * @returns the tab (null when the browser refused)
 */
export const openSignInTab = () => window.open("about:blank", "_blank")

/**
 * Connect a connector (again). One that signs in answers with the address of its sign-in page, which the tab opened for
 * it shows (Orion's own machine hears the answer: the redirect leads to it); one that doesn't comes back connected.
 * @param id the connector
 * @param tab the blank tab opened inside the click, if any
 */
export async function signIn(id: string, tab: Window | null): Promise<void> {
  try {
    const result = await post<{ auth_url: string } | Connector>(`/api/connectors/${part(id)}/connect`)
    if ("auth_url" in result) {
      if (tab) {
        tab.opener = null
        tab.location.href = result.auth_url
      } else window.open(result.auth_url, "_blank", "noopener,noreferrer")
    } else {
      tab?.close()
      adoptConnector(result)
    }
  } catch (error) {
    tab?.close()
    throw error
  }
}

/** Connect a connector (again): `mutate({ id, tab })`, with the tab `openSignInTab` gave for one that signs in. */
export const useConnect = () =>
  useMutation({
    meta: { title: "Not connected" },
    mutationFn: ({ id, tab }: { id: string; tab: Window | null }) => signIn(id, tab),
  })

/** Sign out of a connector (its tokens leave the vault). */
export const useDisconnect = () =>
  useMutation({
    meta: { title: "Not signed out" },
    mutationFn: (id: string) => post<Connector>(`/api/connectors/${part(id)}/disconnect`),
    onSuccess: adoptConnector,
  })

export const useAddConnector = () =>
  useMutation({
    meta: { title: "Not added" },
    mutationFn: (connector: NewConnector) => post<Connector>("/api/connectors", connector),
    onSuccess: (connector) => {
      adoptConnector(connector)
      void queryClient.invalidateQueries({ queryKey: ["directory"] })
    },
  })

export const useRemoveConnector = () =>
  useMutation({
    meta: { title: "Not removed" },
    mutationFn: (id: string) => del<{ ok: true }>(`/api/connectors/${part(id)}`),
    onSuccess: (_result, id) => {
      queryClient.setQueryData<{ items: Connector[] }>(
        ["connectors"],
        (data) => data && { items: data.items.filter((c) => c.id !== id) },
      )
      queryClient.removeQueries({ queryKey: ["connector", id] })
      void queryClient.invalidateQueries({ queryKey: ["directory"] })
    },
  })

/** The connectors the directory offers, found by words and a category. */
export const useDirectory = (q: string, category: string) =>
  useQuery({
    queryKey: ["directory", q, category],
    queryFn: () => api<{ items: DirectoryItem[] }>(`/api/connectors/directory?${new URLSearchParams({ q, category })}`),
    select: (data) => data.items,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
  })

// ---------- skills ----------
export const useSkills = () =>
  useQuery({ queryKey: ["skills"], queryFn: () => api<{ items: Skill[] }>("/api/skills"), select: (d) => d.items })

/** A skill's page: the skill and its SKILL.md. */
export const useSkill = (name: string) =>
  useQuery({ queryKey: ["skill", name], queryFn: () => api<SkillDetail>(`/api/skills/${part(name)}`) })

/** Everything that lists skills, read again (the skill_changed event does the same). */
const skillsChanged = () => {
  for (const queryKey of [["skills"], ["skill"], ["skills-discover"]]) void queryClient.invalidateQueries({ queryKey })
}

/** Save a SKILL.md as written (a new name makes a new skill). */
export const useSaveSkill = () =>
  useMutation({
    meta: { title: "Skill not saved" },
    mutationFn: ({ name, content }: { name: string; content: string }) =>
      put<unknown>(`/api/skills/${part(name)}`, { content }),
    onSuccess: skillsChanged,
  })

/** Let him use a skill, or not. */
export const useSwitchSkill = () =>
  useMutation({
    meta: { title: "Not changed" },
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      patch<unknown>(`/api/skills/${part(name)}`, { enabled }),
    onMutate: async ({ name, enabled }) => [
      await optimistic<{ items: Skill[] }>(["skills"], (data) => ({
        items: data.items.map((s) => (s.name === name ? { ...s, enabled } : s)),
      })),
      await optimistic<SkillDetail>(["skill", name], (data) => ({ ...data, enabled })),
    ],
    onError: restoreAll,
    onSuccess: skillsChanged,
  })

export const useDeleteSkill = () =>
  useMutation({
    meta: { title: "Skill not deleted" },
    mutationFn: (name: string) => del<{ ok: true }>(`/api/skills/${part(name)}`),
    onSuccess: skillsChanged,
  })

/** Add skills from a Git or GitHub URL, or from a .zip you choose. */
export const useImportSkill = () =>
  useMutation({
    meta: { title: "Not added" },
    mutationFn: (from: { url: string } | File) => {
      if (!(from instanceof File)) return post<Skill[]>("/api/skills/import", from)
      const body = new FormData()
      body.append("file", from)
      return api<Skill[]>("/api/skills/import", { method: "POST", body })
    },
    onSuccess: skillsChanged,
  })

/** Skills to add, from his sources (Anthropic's skills by default). */
export const useSkillDiscover = (q: string) =>
  useQuery({
    queryKey: ["skills-discover", q],
    queryFn: () => api<{ items: SkillListing[] }>(`/api/skills/discover?${new URLSearchParams({ q })}`),
    select: (data) => data.items,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
  })

// ---------- plugins ----------
export const usePlugins = () =>
  useQuery({ queryKey: ["plugins"], queryFn: () => api<{ items: Plugin[] }>("/api/plugins"), select: (d) => d.items })

/**
 * A plugin as the server now says it is; what it brings (skills, connectors) is read again.
 * @param plugin the plugin
 */
function adoptPlugin(plugin: Plugin): void {
  queryClient.setQueryData<{ items: Plugin[] }>(["plugins"], (data) =>
    data?.items.some((p) => p.name === plugin.name)
      ? { items: data.items.map((p) => (p.name === plugin.name ? plugin : p)) }
      : data && { items: [...data.items, plugin] },
  )
  for (const queryKey of [["plugins-discover"], ["skills"], ["connectors"]])
    void queryClient.invalidateQueries({ queryKey })
}

/** The plugins the marketplaces offer, found by words, a category and a marketplace. */
export const usePluginDiscover = (q: string, category: string, marketplace: string) =>
  useQuery({
    queryKey: ["plugins-discover", q, category, marketplace],
    queryFn: () =>
      api<{ items: PluginListing[]; categories: string[] }>(
        `/api/plugins/discover?${new URLSearchParams({ q, category, marketplace })}`,
      ),
    placeholderData: keepPreviousData,
    staleTime: 5 * 60_000,
  })

/** Install a plugin from a marketplace, or from a .zip or .plugin you choose (nothing of it runs at install). */
export const useInstallPlugin = () =>
  useMutation({
    meta: { title: "Not installed" },
    mutationFn: (from: { marketplace: string; name: string } | File) => {
      if (!(from instanceof File)) return post<Plugin>("/api/plugins", from)
      const body = new FormData()
      body.append("file", from)
      return api<Plugin>("/api/plugins", { method: "POST", body })
    },
    onSuccess: adoptPlugin,
  })

export const useSwitchPlugin = () =>
  useMutation({
    meta: { title: "Not changed" },
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      patch<Plugin>(`/api/plugins/${part(name)}`, { enabled }),
    onMutate: ({ name, enabled }) =>
      optimistic<{ items: Plugin[] }>(["plugins"], (data) => ({
        items: data.items.map((p) => (p.name === name ? { ...p, enabled } : p)),
      })),
    onError: restore,
    onSuccess: adoptPlugin,
  })

export const useUpdatePlugin = () =>
  useMutation({
    meta: { title: "Not updated" },
    mutationFn: (name: string) => post<Plugin>(`/api/plugins/${part(name)}/update`),
    onSuccess: adoptPlugin,
  })

export const useRemovePlugin = () =>
  useMutation({
    meta: { title: "Not removed" },
    mutationFn: (name: string) => del<{ ok: true }>(`/api/plugins/${part(name)}`),
    onSuccess: (_result, name) => {
      queryClient.setQueryData<{ items: Plugin[] }>(
        ["plugins"],
        (data) => data && { items: data.items.filter((p) => p.name !== name) },
      )
      for (const queryKey of [["plugins-discover"], ["skills"], ["connectors"]])
        void queryClient.invalidateQueries({ queryKey })
    },
  })

export const useMarketplaces = () =>
  useQuery({
    queryKey: ["marketplaces"],
    queryFn: () => api<{ items: Marketplace[] }>("/api/plugins/marketplaces"),
    select: (data) => data.items,
  })

const marketplacesChanged = () => {
  for (const queryKey of [["marketplaces"], ["plugins-discover"]]) void queryClient.invalidateQueries({ queryKey })
}

export const useAddMarketplace = () =>
  useMutation({
    meta: { title: "Marketplace not added" },
    mutationFn: (url: string) => post<Marketplace>("/api/plugins/marketplaces", { url }),
    onSuccess: marketplacesChanged,
  })

/** Read the marketplaces again now (otherwise Orion does it daily). */
export const useRefreshMarketplaces = () =>
  useMutation({
    meta: { title: "The marketplaces weren't read" },
    mutationFn: () => post<{ items: Marketplace[] }>("/api/plugins/refresh"),
    onSuccess: (data) => {
      queryClient.setQueryData(["marketplaces"], data)
      void queryClient.invalidateQueries({ queryKey: ["plugins-discover"] })
    },
  })

export const useRemoveMarketplace = () =>
  useMutation({
    meta: { title: "Marketplace not removed" },
    mutationFn: (name: string) => del<{ ok: true }>(`/api/plugins/marketplaces/${part(name)}`),
    onSuccess: marketplacesChanged,
  })

// ---------- speed, money, benchmarks ----------
/** The latest turns' stages, and each brain's medians. */
export const timingsQuery = (limit = 50) =>
  queryOptions({
    queryKey: ["timings", limit],
    queryFn: () => api<TimingsResponse>(`/api/timings?limit=${limit}`),
    staleTime: 15_000,
  })
export const useTimings = (limit = 50) => useQuery(timingsQuery(limit))

/** What OpenRouter cost, by day and by purpose, and the key's own limits. */
export const useSpend = (days = 30) =>
  useQuery({ queryKey: ["spend", days], queryFn: () => api<SpendResponse>(`/api/spend?days=${days}`) })

export const useBenchRuns = () =>
  useQuery({ queryKey: ["bench"], queryFn: () => api<{ items: BenchRun[] }>("/api/bench"), select: (d) => d.items })

/** What a benchmark asks for: the brains ("claude", "ollama:qwen3.5:9b"), the suite, how many of your requests at most. */
interface BenchRequest {
  brains: string[]
  suite: "builtin" | "history"
  limit?: number
}

/** What a run would do and cost, said before it starts: nothing starts. */
export const usePlanBench = () =>
  useMutation({
    meta: { title: "The benchmark can't be planned" },
    mutationFn: (run: BenchRequest) => post<BenchPlan>("/api/bench/plan", run),
  })

/**
 * Score brains on a suite (a dry run: no tool really runs); `confirm` is your yes to one that costs (without it the
 * server refuses, 409, and starts nothing). Its progress comes as `bench` events.
 */
export const useRunBench = () =>
  useMutation({
    meta: { title: "The benchmark didn't start" },
    mutationFn: (run: BenchRequest & { confirm: boolean }) => post<{ id: number }>("/api/bench", run),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["bench"] }),
  })

/** Stop a run under way: what it scored so far is kept. */
export const useStopBench = () =>
  useMutation({
    meta: { title: "The benchmark didn't stop" },
    mutationFn: (id: number) => post<{ ok: true }>(`/api/bench/${id}/stop`),
  })

// ---------- backups and documents ----------
export const useBackups = () => useQuery({ queryKey: ["backups"], queryFn: () => api<BackupsState>("/api/backups") })

/** Back up now (`backup` events say when it is done). */
export const useBackupNow = () =>
  useMutation({ meta: { title: "No backup" }, mutationFn: () => post<{ name: string }>("/api/backups") })

/** The folders of your documents he indexes, and how far he got. */
export const useDocuments = () =>
  useQuery({ queryKey: ["documents"], queryFn: () => api<DocumentsStatus>("/api/documents") })
