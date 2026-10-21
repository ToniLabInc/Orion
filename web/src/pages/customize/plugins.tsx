/**
 * Plugins: Claude plugins. His skills come from a plugin's skills and commands (a command is a skill he can follow when
 * you name it), its connectors from its MCP servers; what else a plugin carries (agents, hooks, output styles, LSP
 * servers, mods) he never runs, and says so. "Yours" lists the ones installed, each with a switch and an Update when its
 * marketplace has a newer version. "Discover" searches the marketplaces (Anthropic's directory by default) by words
 * and category; a plugin can also be added from a .zip or .plugin file. A plugin's page lists what it brings, tab by
 * tab. Installing only unpacks a plugin: nothing in it runs.
 */
import { ExternalLink, FolderPlus, LoaderCircle, Puzzle, RefreshCw, Store, Trash2, Upload } from "lucide-react"
import { type FormEvent, useRef, useState } from "react"
import { Link, useLocation } from "wouter"
import {
  useAddMarketplace,
  useConnectors,
  useInstallPlugin,
  useMarketplaces,
  usePluginDiscover,
  usePlugins,
  useRefreshMarketplaces,
  useRemoveMarketplace,
  useRemovePlugin,
  useSwitchPlugin,
  useUpdatePlugin,
} from "@/lib/api"
import { authorName, pluginParts } from "@/lib/connectors"
import { agoInWords } from "@/lib/format"
import { useSettled } from "@/lib/hooks"
import type { Plugin, PluginListing } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Badge } from "@/ui/badge"
import { Button, IconButton } from "@/ui/button"
import { Dialog } from "@/ui/dialog"
import { Input } from "@/ui/input"
import { SearchField } from "@/ui/search"
import { SelectBox } from "@/ui/select"
import { Empty, Failed, Pending } from "@/ui/states"
import { Switch } from "@/ui/switch"
import { Tabs } from "@/ui/tabs"
import { toast } from "@/ui/toast"
import { PageHeader } from "../frame"
import { Note, Panel } from "../settings/fields"
import { BackLink, Grid, ItemCard, useView, ViewSwitch } from "./parts"

const href = (name: string) => `/customize/plugins/${encodeURIComponent(name)}`

/**
 * The Plugins tab.
 * @returns the tab
 */
export function Plugins() {
  const [view, setView] = useView()
  const [shops, setShops] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  const [, navigate] = useLocation()
  const install = useInstallPlugin()
  return (
    <>
      <PageHeader
        title="Plugins"
        lead="Claude plugins: their skills and commands become his skills, their MCP servers his connectors. What else is in one, he never runs."
        actions={
          <>
            <ViewSwitch view={view} onChange={setView} label="Plugins" />
            <Button size="sm" onClick={() => setShops(true)} data-testid="plugin-marketplaces">
              <Store aria-hidden="true" />
              Marketplaces
            </Button>
            <Button
              size="sm"
              disabled={install.isPending}
              onClick={() => file.current?.click()}
              data-testid="plugin-upload"
            >
              {install.isPending ? (
                <LoaderCircle aria-hidden="true" className="animate-spin" />
              ) : (
                <Upload aria-hidden="true" />
              )}
              Upload
            </Button>
            <input
              ref={file}
              type="file"
              accept=".zip,.plugin,application/zip"
              hidden
              onChange={(e) => {
                const chosen = e.target.files?.[0]
                e.target.value = ""
                if (chosen)
                  install.mutate(chosen, {
                    onSuccess: (plugin) => {
                      toast.add({ title: "Plugin added", description: plugin.name, type: "success" })
                      navigate(href(plugin.name))
                    },
                  })
              }}
            />
          </>
        }
      />
      {view === "yours" ? <Yours /> : <Discover />}
      <Marketplaces open={shops} onClose={() => setShops(false)} />
    </>
  )
}

// ---------- yours ----------
/**
 * The plugins you have.
 * @returns the cards
 */
function Yours() {
  const plugins = usePlugins()
  const switchPlugin = useSwitchPlugin()
  if (plugins.isError)
    return <Failed title="The plugins can't be listed" error={plugins.error} onRetry={() => void plugins.refetch()} />
  if (!plugins.data) return <Pending label="Reading the plugins" delay={150} page />
  if (!plugins.data.length)
    return (
      <div className="glass">
        <Empty icon={<Puzzle />}>No plugin yet: see Discover, or upload one.</Empty>
      </div>
    )
  return (
    <Grid label="Your plugins">
      {plugins.data
        .toSorted((a, b) => a.name.localeCompare(b.name))
        .map((p) => (
          <ItemCard
            key={p.name}
            href={href(p.name)}
            icon={Puzzle}
            title={p.name}
            description={p.description}
            dim={!p.enabled}
            badge={
              p.update ? (
                <Badge tone="dusk" dot>
                  Update
                </Badge>
              ) : (
                <span className="font-mono text-xs text-steel-400">v{p.version}</span>
              )
            }
            footer={
              <>
                <span>{pluginParts(p)}</span>
                {authorName(p.author) && <span className="truncate">{authorName(p.author)}</span>}
                {p.trust === "unreviewed" && <span className="text-dusk-200">Not reviewed</span>}
              </>
            }
            action={
              <Switch
                size="sm"
                checked={p.enabled}
                onCheckedChange={(enabled) => switchPlugin.mutate({ name: p.name, enabled })}
                aria-label={`${p.name}: on or off`}
                data-testid={`plugin-switch-${p.name}`}
              />
            }
            testId={`plugin-${p.name}`}
          />
        ))}
    </Grid>
  )
}

// ---------- discover ----------
/**
 * The marketplaces' plugins: search, a category, a marketplace.
 * @returns the listing
 */
function Discover() {
  const [typed, setTyped] = useState("")
  const q = useSettled(typed)
  const [category, setCategory] = useState("")
  const [marketplace, setMarketplace] = useState("")
  const markets = useMarketplaces()
  const found = usePluginDiscover(q, category, marketplace)
  const [, navigate] = useLocation()
  const install = useInstallPlugin()
  const [installing, setInstalling] = useState("")
  if (found.isError && !found.data)
    return <Failed title="The marketplaces can't be read" error={found.error} onRetry={() => void found.refetch()} />
  if (!found.data) return <Pending label="Reading the marketplaces" delay={150} page />
  const categories = found.data.categories
  const names = (markets.data ?? []).map((m) => m.name)
  const add = (p: PluginListing) => {
    setInstalling(`${p.marketplace}/${p.name}`)
    install.mutate(
      { marketplace: p.marketplace, name: p.name },
      {
        onSuccess: (plugin) => {
          toast.add({ title: "Plugin added", description: plugin.name, type: "success" })
          navigate(href(plugin.name))
        },
      },
    )
  }
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <SearchField
          value={typed}
          onChange={setTyped}
          label="Search the plugins"
          placeholder="Search the plugins…"
          className="min-w-48 flex-1 tab:max-w-sm"
          data-testid="plugins-search"
        />
        {names.length > 1 && (
          <SelectBox
            value={marketplace}
            onChange={setMarketplace}
            label="Marketplace"
            choices={[{ value: "", label: "Every marketplace" }, ...names.map((n) => ({ value: n, label: n }))]}
            className="w-52"
          />
        )}
      </div>
      {categories.length > 1 && (
        <fieldset className="m-0 flex flex-wrap gap-1.5 border-0 p-0" aria-label="Category">
          {["", ...categories].map((c) => (
            <button
              key={c || "all"}
              type="button"
              aria-pressed={category === c}
              onClick={() => setCategory(c)}
              className={cn(
                "h-8 rounded-full px-3 text-sm outline-none transition-colors focus-visible:outline-2 focus-visible:outline-rigel-400",
                category === c
                  ? "bg-rigel-400/16 text-rigel-100 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_45%,transparent)]"
                  : "bg-steel-50/5 text-steel-300 shadow-[inset_0_0_0_1px_var(--hairline)] hover:text-steel-100",
              )}
            >
              {c || "All"}
            </button>
          ))}
        </fieldset>
      )}
      {found.data.items.length ? (
        <Grid label="Plugins to add">
          {found.data.items.map((p) => (
            <ItemCard
              key={`${p.marketplace}/${p.name}`}
              href={p.installed ? href(p.name) : undefined}
              icon={Puzzle}
              title={p.name}
              description={p.description}
              badge={
                p.installed ? (
                  <Badge tone="ok" dot>
                    Added
                  </Badge>
                ) : undefined
              }
              footer={
                <>
                  {p.category && <span>{p.category}</span>}
                  {names.length > 1 && <span className="truncate">{p.marketplace}</span>}
                  {authorName(p.author) && <span className="truncate">{authorName(p.author)}</span>}
                  {!p.components_known && <span>What it brings shows after you add it</span>}
                </>
              }
              action={
                <>
                  {p.homepage && (
                    <a
                      href={p.homepage}
                      target="_blank"
                      rel="noreferrer noopener"
                      aria-label={`${p.name}: its page`}
                      className="grid size-7 place-items-center rounded-chip text-steel-400 outline-none transition-colors hover:bg-steel-50/8 hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400"
                    >
                      <ExternalLink aria-hidden="true" className="size-3.5" />
                    </a>
                  )}
                  {!p.installed && (
                    <Button
                      size="xs"
                      disabled={install.isPending}
                      onClick={() => add(p)}
                      data-testid={`plugin-add-${p.name}`}
                    >
                      {install.isPending && installing === `${p.marketplace}/${p.name}` && (
                        <LoaderCircle aria-hidden="true" className="animate-spin" />
                      )}
                      Add
                    </Button>
                  )}
                </>
              }
              testId={`discover-plugin-${p.name}`}
            />
          ))}
        </Grid>
      ) : (
        <div className="glass">
          <Empty icon={<Puzzle />}>No plugin matches.</Empty>
        </div>
      )}
    </div>
  )
}

// ---------- marketplaces ----------
/**
 * The marketplaces: where plugins come from (git repositories with a marketplace.json).
 * @param props whether it is `open`, and `onClose`
 * @returns the dialog
 */
function Marketplaces({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      kicker="Plugins"
      title="Marketplaces"
      description="Where plugins come from: a git repository with a .claude-plugin/marketplace.json. Anthropic's directory is the one he starts with."
      testId="marketplaces-dialog"
      wide
    >
      {open && <MarketplaceList />}
    </Dialog>
  )
}

/**
 * The marketplaces, and a field to add one.
 * @returns the list and the form
 */
function MarketplaceList() {
  const markets = useMarketplaces()
  const add = useAddMarketplace()
  const remove = useRemoveMarketplace()
  const refresh = useRefreshMarketplaces()
  const [url, setUrl] = useState("")
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (url.trim()) add.mutate(url.trim(), { onSuccess: () => setUrl("") })
  }
  const askRemove = async (name: string) => {
    const text = "Its plugins can't be found any more. The ones you installed from it stay."
    if (await ask({ title: `Remove ${name}?`, text, ok: "Remove", danger: true, kicker: "Marketplaces" }))
      remove.mutate(name)
  }
  return (
    <div className="grid grid-cols-1 gap-4">
      {markets.isError ? (
        <Failed title="The marketplaces can't be listed" error={markets.error} onRetry={() => void markets.refetch()} />
      ) : !markets.data ? (
        <Pending label="Reading the marketplaces" delay={150} />
      ) : (
        <ul className="grid grid-cols-1 gap-1.5" data-testid="marketplace-list">
          {markets.data.map((m) => (
            <li
              key={m.name}
              className="flex items-center gap-3 rounded-control bg-void-950/40 px-3.5 py-2.5 shadow-[inset_0_0_0_1px_var(--hairline)]"
            >
              <Store aria-hidden="true" className="size-4 shrink-0 text-steel-400" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm text-steel-50">{m.name}</span>
                <span className="block truncate font-mono text-xs text-steel-400" title={m.url}>
                  {m.url}
                </span>
                <span className="block text-xs text-steel-400">
                  {m.plugins} plugin{m.plugins === 1 ? "" : "s"}
                  {m.updated_at ? ` · read ${agoInWords(m.updated_at, new Date())}` : " · not read yet"}
                </span>
                {m.error && <span className="block break-words text-alert-200 text-xs">{m.error}</span>}
              </span>
              <IconButton label={`Remove ${m.name}`} onClick={() => void askRemove(m.name)}>
                <Trash2 />
              </IconButton>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-3">
        <p className="min-w-0 flex-1 text-steel-400 text-xs">Orion reads them once a day.</p>
        <Button
          size="xs"
          disabled={refresh.isPending}
          onClick={() => refresh.mutate()}
          data-testid="marketplace-refresh"
        >
          {refresh.isPending ? (
            <LoaderCircle aria-hidden="true" className="animate-spin" />
          ) : (
            <RefreshCw aria-hidden="true" />
          )}
          Read them now
        </Button>
      </div>
      <form onSubmit={submit} className="flex gap-2">
        <Input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://github.com/anthropics/claude-plugins-official"
          aria-label="A marketplace's address"
          spellCheck={false}
          autoComplete="off"
          className="min-w-0 flex-1 font-mono"
          data-testid="marketplace-url"
        />
        <Button type="submit" disabled={!url.trim() || add.isPending} data-testid="marketplace-add">
          {add.isPending ? (
            <LoaderCircle aria-hidden="true" className="animate-spin" />
          ) : (
            <FolderPlus aria-hidden="true" />
          )}
          Add
        </Button>
      </form>
    </div>
  )
}

// ---------- a plugin's page ----------
const SECTIONS = ["skills", "connectors", "commands", "unused"] as const
type Section = (typeof SECTIONS)[number]

/**
 * A plugin's page: what it is, switch it, update it, remove it, and what it brings.
 * @param props the plugin's `name` and the `section` open
 * @returns the page
 */
export function PluginPage({ name, section }: { name: string; section?: string }) {
  const plugins = usePlugins()
  const [, navigate] = useLocation()
  const update = useUpdatePlugin()
  const switchPlugin = useSwitchPlugin()
  const remove = useRemovePlugin()
  const back = <BackLink href="/customize/plugins" label="Plugins" />
  if (plugins.isError)
    return (
      <>
        {back}
        <Failed title="The plugins can't be listed" error={plugins.error} onRetry={() => void plugins.refetch()} />
      </>
    )
  if (!plugins.data)
    return (
      <>
        {back}
        <Pending label="Reading the plugin" delay={150} page />
      </>
    )
  const p = plugins.data.find((x) => x.name === name)
  if (!p)
    return (
      <>
        {back}
        <div className="glass">
          <Empty icon={<Puzzle />}>There is no plugin called {name} (any more).</Empty>
        </div>
      </>
    )
  const open: Section = SECTIONS.find((s) => s === section) ?? firstWith(p)
  const items = [
    { value: "skills" as const, label: "Skills", count: p.components.skills.length },
    { value: "connectors" as const, label: "Connectors", count: p.components.connectors.length },
    { value: "commands" as const, label: "Commands", count: p.components.commands.length },
    { value: "unused" as const, label: "Not used by Orion", count: p.components.unused.length },
  ]
  const askRemove = async () => {
    const text = `Its ${pluginParts(p).toLowerCase()} leave Orion with it. You can install it again any time.`
    if (await ask({ title: `Remove ${p.name}?`, text, ok: "Remove", danger: true, kicker: "Plugins" }))
      remove.mutate(p.name, { onSuccess: () => navigate("/customize/plugins") })
  }
  const author = authorName(p.author)
  return (
    <>
      <PageHeader
        back={back}
        title={p.name}
        lead={p.description}
        actions={
          <>
            <span className="font-mono text-sm text-steel-400">v{p.version}</span>
            {p.update && (
              <Button
                size="sm"
                tone="primary"
                disabled={update.isPending}
                onClick={() =>
                  update.mutate(p.name, {
                    onSuccess: (done) =>
                      toast.add({ title: "Updated", description: `${done.name} v${done.version}`, type: "success" }),
                  })
                }
                data-testid="plugin-update"
              >
                {update.isPending && <LoaderCircle aria-hidden="true" className="animate-spin" />}
                Update to v{p.update.version}
              </Button>
            )}
            <Button
              tone="danger"
              size="sm"
              disabled={remove.isPending}
              onClick={() => void askRemove()}
              data-testid="plugin-remove"
            >
              <Trash2 aria-hidden="true" />
              Remove
            </Button>
            {/* biome-ignore lint/a11y/noLabelWithoutControl: the switch inside is the control */}
            <label className="flex items-center gap-2 text-sm text-steel-300">
              On
              <Switch
                checked={p.enabled}
                onCheckedChange={(enabled) => switchPlugin.mutate({ name: p.name, enabled })}
                aria-label={`${p.name}: on or off`}
                data-testid="plugin-page-switch"
              />
            </label>
          </>
        }
      />
      <p className="-mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-sm text-steel-400">
        {author && <span>By {author}</span>}
        {p.source.marketplace && <span>From {p.source.marketplace}</span>}
        <span className={p.trust === "reviewed" ? "text-ok-200" : "text-dusk-200"}>
          {p.trust === "reviewed" ? "Reviewed: Anthropic's directory" : "Not reviewed"}
        </span>
        {p.source.sha && <span className="font-mono text-xs">{p.source.sha.slice(0, 7)}</span>}
        {p.homepage && (
          <a
            href={p.homepage}
            target="_blank"
            rel="noreferrer noopener"
            className="flex items-center gap-1 text-rigel-300 outline-none hover:underline focus-visible:outline-2 focus-visible:outline-rigel-400"
          >
            Its page
            <ExternalLink aria-hidden="true" className="size-3" />
          </a>
        )}
      </p>
      <Tabs
        value={open}
        onChange={(next) => navigate(`${href(p.name)}/${next}`)}
        items={items}
        label={`${p.name}: what it brings`}
        listClassName="glass rounded-card p-1"
        className="grid gap-4"
      >
        <div className="mt-4" data-testid={`plugin-${open}`}>
          <Brings plugin={p} section={open} />
        </div>
      </Tabs>
    </>
  )
}

/** The first section with something in it (skills when none has). */
function firstWith(p: Plugin): Section {
  if (p.components.skills.length) return "skills"
  if (p.components.connectors.length) return "connectors"
  if (p.components.commands.length) return "commands"
  return "skills"
}

/**
 * One section of what a plugin brings.
 * @param props the `plugin` and the `section`
 * @returns the list
 */
function Brings({ plugin: p, section }: { plugin: Plugin; section: Section }) {
  const connectors = useConnectors()
  const names = p.components[section]
  const words: Record<Section, string> = {
    skills: "Skills he can follow, from the plugin's skills folder.",
    connectors:
      "MCP servers the plugin starts, as connectors with their own page. Not reviewed, unless the marketplace is Anthropic's.",
    commands: "Slash commands of the plugin: he treats each as a skill, which he follows when you name it.",
    unused: "Orion never runs a plugin's agents, hooks, output styles, LSP servers or mods.",
  }
  return (
    <Panel title={SECTION_TITLE[section]} icon={Puzzle} description={words[section]}>
      {names.length ? (
        <ul className="border-t">
          {names.map((n) => {
            const title = section === "connectors" ? (connectors.data?.find((c) => c.id === n)?.title ?? n) : n
            const to =
              section === "skills" || section === "commands"
                ? `/customize/skills/${encodeURIComponent(n)}`
                : section === "connectors"
                  ? `/customize/connectors/${encodeURIComponent(n)}`
                  : null
            return (
              <li key={n} className="border-b px-5 py-3 last:border-b-0">
                {to ? (
                  <Link href={to} className="font-mono text-sm text-rigel-300 underline-offset-2 hover:underline">
                    {title}
                  </Link>
                ) : (
                  <span className="font-mono text-sm text-steel-100">{title}</span>
                )}
              </li>
            )
          })}
        </ul>
      ) : (
        <Note className="border-t-0 pt-0">{section === "unused" ? "Nothing: Orion uses all of it." : "None."}</Note>
      )}
    </Panel>
  )
}

const SECTION_TITLE: Record<Section, string> = {
  skills: "Skills",
  connectors: "Connectors",
  commands: "Commands",
  unused: "Not used by Orion",
}
