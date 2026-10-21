/**
 * Connectors: everything that gives him tools. "Yours" lists them in groups (his own, the ones added by you or from the
 * directory, the ones plugins bring), each a card that opens its page, with its status (Connected, Not connected, Needs
 * setup, Off…), how many tools it has, and a mark if its code isn't reviewed. "Discover" is the directory: ours, and
 * the connectors of Claude's plugin directory, searchable and by category; adding one asks for what it needs (fields,
 * or a sign-in in a browser on this PC). "Add your own" takes a command or a URL. /customize/connectors?add=custom
 * opens that form.
 */
import { ExternalLink, LoaderCircle, Plug, Plus, ShieldAlert } from "lucide-react"
import { type FormEvent, type ReactNode, useDeferredValue, useState } from "react"
import { useLocation, useSearchParams } from "wouter"
import { openSignInTab, signIn, useAddConnector, useConnectors, useDirectory } from "@/lib/api"
import {
  customConnector,
  directoryNeeds,
  formProblems,
  formValues,
  groupConnectors,
  isUrl,
  openDraft,
  STATUS,
  toolsWords,
} from "@/lib/connectors"
import { useSettled } from "@/lib/hooks"
import { connectorIcon } from "@/lib/icons"
import type { Connector, DirectoryItem } from "@/lib/types"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { Dialog } from "@/ui/dialog"
import { Input, Textarea } from "@/ui/input"
import { SearchField } from "@/ui/search"
import { Empty, Failed, Pending } from "@/ui/states"
import { toast } from "@/ui/toast"
import { PageHeader } from "../frame"
import { ConnectorForm } from "./form"
import { Grid, GroupHeading, ItemCard, Prose, useView, ViewSwitch } from "./parts"

const href = (id: string) => `/customize/connectors/${encodeURIComponent(id)}`

/**
 * Whether this HUD is on Orion's own machine (a sign-in happens in a browser there). The server says it for each
 * connector, the same for all of them (for the HUD asking, in the list and in its events); until the list is here a
 * HUD is taken to be local.
 * @returns true when it is
 */
function useLocalHud(): boolean {
  return useConnectors().data?.[0]?.local ?? true
}

/**
 * The Connectors tab.
 * @returns the tab
 */
export function Connectors() {
  const [view, setView] = useView()
  const [params, setParams] = useSearchParams()
  const adding = params.get("add") === "custom"
  const setAdding = (on: boolean) =>
    setParams(
      (before) => {
        const next = new URLSearchParams(before)
        if (on) next.set("add", "custom")
        else next.delete("add")
        return next
      },
      { replace: true },
    )
  return (
    <>
      <PageHeader
        title="Connectors"
        lead="Everything that gives him tools: his own, the services you connect, and what plugins bring. Each has its own settings on its page."
        actions={
          <>
            <ViewSwitch view={view} onChange={setView} label="Connectors" />
            <Button size="sm" onClick={() => setAdding(true)} data-testid="add-connector">
              <Plus aria-hidden="true" />
              Add your own
            </Button>
          </>
        }
      />
      {view === "yours" ? <Yours /> : <Discover />}
      <CustomDialog open={adding} onClose={() => setAdding(false)} />
    </>
  )
}

// ---------- yours ----------
/**
 * The connectors you have, in groups, narrowed by what is typed.
 * @returns the groups
 */
function Yours() {
  const connectors = useConnectors()
  const [typed, setTyped] = useState("")
  const q = useDeferredValue(typed)
  if (connectors.isError)
    return (
      <Failed
        title="The connectors can't be listed"
        error={connectors.error}
        onRetry={() => void connectors.refetch()}
      />
    )
  if (!connectors.data) return <Pending label="Asking his connectors" delay={150} page />
  const groups = groupConnectors(connectors.data, q)
  return (
    <div className="grid gap-5">
      <SearchField
        value={typed}
        onChange={setTyped}
        label="Search your connectors"
        placeholder="Search your connectors…"
        className="max-w-sm"
        data-testid="connectors-search"
      />
      {groups.length ? (
        groups.map((g) => (
          <section key={g.title} aria-label={g.title}>
            <GroupHeading title={g.title} note={g.about} count={g.items.length} />
            <Grid label={g.title}>
              {g.items.map((c) => (
                <ConnectorCard key={c.id} c={c} />
              ))}
            </Grid>
          </section>
        ))
      ) : (
        <div className="glass">
          <Empty icon={<Plug />}>{q ? "No connector matches." : "No connector yet: see Discover."}</Empty>
        </div>
      )}
    </div>
  )
}

/**
 * One connector's card.
 * @param props the connector (`c`)
 * @returns the card
 */
function ConnectorCard({ c }: { c: Connector }) {
  const status = STATUS[c.status]
  return (
    <ItemCard
      href={href(c.id)}
      icon={connectorIcon(c.icon)}
      title={c.title}
      description={c.description}
      badge={
        c.status === "connected" ? <Working label={status.label} /> : <Badge tone={status.tone}>{status.label}</Badge>
      }
      footer={
        <>
          <span>{toolsWords(c)}</span>
          {c.trust === "unreviewed" && (
            <span className="flex items-center gap-1 text-dusk-300">
              <ShieldAlert aria-hidden="true" className="size-3" />
              Not reviewed
            </span>
          )}
          {c.plugin && <span className="truncate">from {c.plugin}</span>}
        </>
      }
      dim={c.status === "off" || c.status === "unavailable"}
      testId={`connector-${c.id}`}
    />
  )
}

/**
 * A connector that works: a quiet green dot, so a list of them isn't a wall of badges (the words are for a screen
 * reader and the tooltip; a badge says what needs a look).
 * @param props the status's `label`
 * @returns the dot
 */
function Working({ label }: { label: string }) {
  return (
    <span className="grid size-5 place-items-center" title={label}>
      <span aria-hidden="true" className="size-2 rounded-full bg-ok-400" />
      <span className="sr-only">{label}</span>
    </span>
  )
}

// ---------- discover ----------
/**
 * The directory: search, categories, and the connectors on offer.
 * @returns the directory
 */
function Discover() {
  const [typed, setTyped] = useState("")
  const q = useSettled(typed)
  const [category, setCategory] = useState("")
  const everything = useDirectory("", "")
  const found = useDirectory(q, category)
  const [adding, setAdding] = useState<DirectoryItem | null>(null)
  const categories = [...new Set((everything.data ?? []).map((i) => i.category))].toSorted()
  if (found.isError && !found.data)
    return <Failed title="The directory can't be read" error={found.error} onRetry={() => void found.refetch()} />
  if (!found.data) return <Pending label="Reading the directory" delay={150} page />
  return (
    <div className="grid gap-4">
      <SearchField
        value={typed}
        onChange={setTyped}
        label="Search the directory"
        placeholder="Search the directory…"
        className="max-w-sm"
        data-testid="directory-search"
      />
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
      {found.data.length ? (
        <Grid label="The directory">
          {found.data.map((item) => (
            <ItemCard
              key={item.id}
              href={item.installed ? href(item.id) : undefined}
              icon={connectorIcon(item.icon)}
              title={item.title}
              description={item.description}
              badge={
                item.installed ? (
                  <Badge tone="ok" dot>
                    Added
                  </Badge>
                ) : undefined
              }
              footer={
                <>
                  <span>{item.category}</span>
                  <span>{directoryNeeds(item)}</span>
                  {item.source === "claude-directory" && <span>Claude's directory</span>}
                </>
              }
              action={
                <>
                  {isUrl(item.docs) && (
                    <a
                      href={item.docs}
                      target="_blank"
                      rel="noreferrer noopener"
                      aria-label={`${item.title}: how it works`}
                      className="grid size-7 place-items-center rounded-chip text-steel-400 outline-none transition-colors hover:bg-steel-50/8 hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400"
                    >
                      <ExternalLink aria-hidden="true" className="size-3.5" />
                    </a>
                  )}
                  {!item.installed && (
                    <Button size="xs" onClick={() => setAdding(item)} data-testid={`add-${item.id}`}>
                      Add
                    </Button>
                  )}
                </>
              }
              testId={`directory-${item.id}`}
            />
          ))}
        </Grid>
      ) : (
        <div className="glass">
          <Empty icon={<Plug />}>No connector matches.</Empty>
        </div>
      )}
      <AddDialog item={adding} onClose={() => setAdding(null)} />
    </div>
  )
}

// ---------- adding ----------
/**
 * The dialog that adds a connector from the directory.
 * @param props the `item` (none: closed) and `onClose`
 * @returns the dialog
 */
function AddDialog({ item, onClose }: { item: DirectoryItem | null; onClose: () => void }) {
  return (
    <Dialog
      open={item !== null}
      onOpenChange={(next) => !next && onClose()}
      kicker="Connectors"
      title={item ? `Add ${item.title}` : ""}
      description={item?.description}
      testId="add-dialog"
    >
      {item && <AddForm item={item} onClose={onClose} />}
    </Dialog>
  )
}

/**
 * Its fields, and Add (with the sign-in for one that has it).
 * @param props the `item` and `onClose`
 * @returns the form
 */
function AddForm({ item, onClose }: { item: DirectoryItem; onClose: () => void }) {
  const [, navigate] = useLocation()
  const [draft, setDraft] = useState(() => openDraft(item.fields, {}))
  const [problems, setProblems] = useState<Record<string, string>>({})
  const add = useAddConnector()
  const local = useLocalHud()
  const oauth = item.auth === "oauth"
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const found = formProblems(item.fields, draft)
    setProblems(found)
    if (Object.keys(found).length) return
    const tab = oauth && local ? openSignInTab() : null // inside the click: a browser allows it only now
    let added: Connector
    try {
      added = await add.mutateAsync({ from: "directory", id: item.id, values: formValues(item.fields, {}, draft) })
    } catch {
      tab?.close() // (the toast says why)
      return
    }
    if (oauth && local) {
      try {
        await signIn(added.id, tab)
      } catch (error) {
        toast.add({ title: "Not connected", description: (error as Error).message, type: "error" })
      }
    }
    onClose()
    navigate(href(added.id))
  }
  return (
    <form onSubmit={submit} className="grid grid-cols-1 gap-4" noValidate>
      {item.docs && !isUrl(item.docs) && (
        <p className="text-sm text-steel-300" data-testid="add-notes">
          <Prose text={item.docs} />
        </p>
      )}
      {item.fields.length > 0 && (
        <ConnectorForm fields={item.fields} draft={draft} onChange={setDraft} problems={problems} />
      )}
      {oauth && (
        <p className="text-sm text-steel-300">
          {local
            ? "A page of the service opens in a new tab to sign in. Orion keeps the sign-in in his vault."
            : "Signing in opens a page on Orion's own machine: add it here, then connect it from the PC."}
        </p>
      )}
      {item.fields.length === 0 && !oauth && <p className="text-sm text-steel-300">There is nothing to fill in.</p>}
      {isUrl(item.docs) && (
        <a
          href={item.docs}
          target="_blank"
          rel="noreferrer noopener"
          className="flex w-fit items-center gap-1.5 text-sm text-rigel-300 outline-none hover:underline focus-visible:outline-2 focus-visible:outline-rigel-400"
        >
          How it works
          <ExternalLink aria-hidden="true" className="size-3.5" />
        </a>
      )}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button type="button" tone="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" tone="primary" disabled={add.isPending} data-testid="add-confirm">
          {add.isPending && <LoaderCircle aria-hidden="true" className="animate-spin" />}
          {oauth && local ? "Add and sign in" : "Add"}
        </Button>
      </div>
    </form>
  )
}

/**
 * The dialog that adds a connector of your own: a command or a URL.
 * @param props whether it is `open`, and `onClose`
 * @returns the dialog
 */
function CustomDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      kicker="Connectors"
      title="Add your own"
      description="A command that starts an MCP server, or the URL of one. Its code is someone else's: it runs in the sandbox, and gets only the environment or headers you give it."
      testId="custom-dialog"
      wide
    >
      {/* a fresh form each time it opens */}
      {open && <CustomForm onClose={onClose} />}
    </Dialog>
  )
}

/**
 * The form: name, command or URL, and environment or headers.
 * @param props `onClose`
 * @returns the form
 */
function CustomForm({ onClose }: { onClose: () => void }) {
  const [, navigate] = useLocation()
  const connectors = useConnectors()
  const [name, setName] = useState("")
  const [where, setWhere] = useState("")
  const [extra, setExtra] = useState("")
  const [problem, setProblem] = useState("")
  const add = useAddConnector()
  const url = isUrl(where)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const made = customConnector({ name, where, extra }, connectors.data?.map((c) => c.id) ?? [])
    if ("problem" in made) return setProblem(made.problem)
    add.mutate(made.body, {
      onSuccess: (added) => {
        onClose()
        navigate(href(added.id))
      },
    })
  }
  const edit = (set: (text: string) => void) => (text: string) => {
    set(text)
    setProblem("")
  }
  return (
    <form onSubmit={submit} className="grid gap-4" noValidate>
      <Labelled label="Name">
        <Input
          value={name}
          onChange={(e) => edit(setName)(e.target.value)}
          placeholder="e.g. my-notes"
          autoComplete="off"
          className="font-mono"
          data-testid="custom-name"
        />
      </Labelled>
      <Labelled label="Command or URL">
        <Input
          value={where}
          onChange={(e) => edit(setWhere)(e.target.value)}
          placeholder="uvx mcp-server-time, or https://…"
          autoComplete="off"
          spellCheck={false}
          className="font-mono"
          data-testid="custom-where"
        />
      </Labelled>
      <Labelled label={url ? "Headers" : "Environment"} optional>
        <Textarea
          value={extra}
          onChange={(e) => edit(setExtra)(e.target.value)}
          placeholder={url ? "Authorization: Bearer …" : "API_KEY=…"}
          rows={3}
          spellCheck={false}
          className="min-h-20 font-mono"
          data-testid="custom-extra"
        />
      </Labelled>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {problem && (
          <p role="alert" className="mr-auto text-alert-300 text-sm">
            {problem}
          </p>
        )}
        <Button type="button" tone="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" tone="primary" disabled={add.isPending} data-testid="custom-save">
          {add.isPending && <LoaderCircle aria-hidden="true" className="animate-spin" />}
          Add and connect
        </Button>
      </div>
    </form>
  )
}

/**
 * A form field with its label above it.
 * @param props the `label`, whether it is `optional`, and the control (`children`)
 * @returns the field
 */
function Labelled({ label, optional, children }: { label: string; optional?: boolean; children: ReactNode }) {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: the control is inside
    <label className="grid gap-1.5 text-sm text-steel-200">
      <span>
        {label}
        {optional && <span className="ml-2 text-steel-500">optional</span>}
      </span>
      {children}
    </label>
  )
}
