/**
 * A connector's own settings, on its page: what they are depends on the connector. A form of the fields it declares
 * (saved together, a secret only when something is typed in it, and its value never comes back); the folders of your
 * documents he indexes, each with how far he got; or, for a gesture remote, what each hand gesture does (its own
 * editor, loaded when asked for). Orion's Settings holds none of these.
 */
import { Check, Folder, FolderPlus, Hand, LoaderCircle, Trash2, TriangleAlert } from "lucide-react"
import { motion } from "motion/react"
import { type FormEvent, lazy, Suspense, useEffect, useState } from "react"
import { missing, useDocuments, useSaveConnector } from "@/lib/api"
import { changedKeys, formProblems, formValues, openDraft } from "@/lib/connectors"
import type { Connector, ConnectorSettings, DocumentsStatus } from "@/lib/types"
import { Button, IconButton } from "@/ui/button"
import { Input } from "@/ui/input"
import { Empty, Pending } from "@/ui/states"
import { ARRIVE } from "../frame"
import { FoldPanel, Note, Panel, SectionFields, useSection, useSetting } from "../settings/fields"
import { ConnectorForm } from "./form"

/** The gesture editor: its own chunk (its icons and bindings stay out of every other page). */
const GestureEditor = lazy(() => import("./gestures").then((m) => ({ default: m.GestureEditor })))

/**
 * The tab.
 * @param props the connector (`c`)
 * @returns what its settings are
 */
export function SettingsTab({ c }: { c: Connector }) {
  switch (c.settings.kind) {
    case "fields":
      return <FieldsForm key={savedKey(c.settings)} c={c} settings={c.settings} />
    case "folders":
      return <Folders path={c.settings.path} />
    case "gestures":
      return (
        <Suspense fallback={<Pending label="Asking the gesture camera" delay={150} page />}>
          <GestureEditor connector={c} />
        </Suspense>
      )
    case "none":
      return (
        <div className="glass">
          <Empty icon={<Hand />}>Nothing to set: it works as it is.</Empty>
        </div>
      )
  }
}

/**
 * What identifies the saved state of a form: its values and which secrets are set, so a form opens again, fresh, after
 * a save (and not when only the connector's status changes).
 * @param settings the connector's fields and values
 * @returns a key
 */
const savedKey = (settings: Extract<ConnectorSettings, { kind: "fields" }>) =>
  JSON.stringify([settings.values, settings.fields.map((f) => [f.key, f.set])])

/**
 * The form of a connector's fields.
 * @param props the connector (`c`) and its `settings`
 * @returns the form
 */
function FieldsForm({ c, settings }: { c: Connector; settings: Extract<ConnectorSettings, { kind: "fields" }> }) {
  const { fields, values } = settings
  const [draft, setDraft] = useState(() => openDraft(fields, values))
  const [shown, setShown] = useState(false) // problems show once a save was tried
  const [saved, setSaved] = useState(false)
  const save = useSaveConnector()
  const changed = changedKeys(fields, values, draft)
  const problems = formProblems(fields, draft)
  const wrong = Object.keys(problems).length > 0
  useEffect(() => {
    if (!saved) return
    const timer = setTimeout(() => setSaved(false), 1600)
    return () => clearTimeout(timer)
  }, [saved])
  if (!fields.length)
    return (
      <div className="glass">
        <Empty icon={<Hand />}>Nothing to set: it works as it is.</Empty>
      </div>
    )
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setShown(true)
    if (wrong || !changed.length) return
    save.mutate({ id: c.id, values: formValues(fields, values, draft) }, { onSuccess: () => setSaved(true) })
  }
  return (
    <motion.form variants={ARRIVE} onSubmit={submit} noValidate data-testid="connector-form">
      <div className="glass grid gap-5 p-5">
        {c.status === "needs_setup" && (
          <p className="flex items-start gap-2.5 text-sm text-dusk-100">
            <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-dusk-400" />
            It needs the fields marked required before it can connect.
          </p>
        )}
        <ConnectorForm
          fields={fields}
          draft={draft}
          onChange={(next) => {
            setDraft(next)
            setSaved(false)
          }}
          problems={shown ? problems : {}}
          disabled={save.isPending}
        />
        <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-4">
          {saved && (
            <span role="status" className="mr-auto flex items-center gap-1.5 text-sm text-ok-300">
              <Check aria-hidden="true" className="size-4" />
              Saved
            </span>
          )}
          <Button
            type="button"
            tone="ghost"
            disabled={!changed.length || save.isPending}
            onClick={() => {
              setDraft(openDraft(fields, values))
              setShown(false)
            }}
          >
            Discard
          </Button>
          <Button
            type="submit"
            tone="primary"
            disabled={!changed.length || save.isPending}
            data-testid="connector-save"
          >
            {save.isPending && <LoaderCircle aria-hidden="true" className="animate-spin" />}
            Save
          </Button>
        </div>
      </div>
    </motion.form>
  )
}

/**
 * The folders of your documents: each with its files and passages, a spinner while it is read, and a note if some
 * couldn't be; add one by its path, take one out. (A browser can't browse the folders of Orion's machine: type the
 * path.)
 * @param props the setting that holds them (`path`)
 * @returns the editor
 */
function Folders({ path }: { path: string }) {
  const { value, save } = useSetting<string[]>(path)
  const docs = useDocuments()
  const [draft, setDraft] = useState("")
  const folders = value ?? []
  const status = (folder: string) => docs.data?.folders.find((f) => f.path === folder)
  const add = () => {
    const folder = draft.trim()
    if (!folder || folders.includes(folder)) return
    setDraft("")
    save([...folders, folder])
  }
  const more = useSection("documents", [path]).length > 0
  return (
    <>
      <Panel
        title="Your documents"
        icon={Folder}
        description="A folder, or an Obsidian vault: he reads what is in it, and finds it by words and by meaning"
        data-testid="documents-folders"
      >
        {folders.length ? (
          <ul className="border-t">
            {folders.map((folder) => (
              <FolderRow
                key={folder}
                folder={folder}
                status={status(folder)}
                onRemove={() => save(folders.filter((f) => f !== folder))}
              />
            ))}
          </ul>
        ) : (
          <Note>No folder yet: add one below.</Note>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault()
            add()
          }}
          className="flex gap-2 border-t px-5 py-4"
        >
          <Input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="/home/you/Documents or ~/Notes"
            aria-label="A folder to add"
            spellCheck={false}
            autoComplete="off"
            className="min-w-0 flex-1 font-mono"
            data-testid="folder-input"
          />
          <Button type="submit" disabled={!draft.trim()} data-testid="folder-add">
            <FolderPlus aria-hidden="true" />
            Add
          </Button>
        </form>
        {docs.data?.last_error && (
          <p className="flex items-start gap-2.5 border-t px-5 py-3.5 text-sm text-alert-100">
            <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-alert-400" />
            {docs.data.last_error}
          </p>
        )}
        {missing(docs.error) && <Note>This Orion doesn't tell how far the reading of your documents is.</Note>}
      </Panel>
      {more && (
        <FoldPanel title="How he reads them" icon={Folder}>
          <SectionFields section="documents" except={[path]} />
        </FoldPanel>
      )}
    </>
  )
}

/**
 * One folder.
 * @param props the `folder`, how far he got (`status`, if he says) and `onRemove`
 * @returns the row
 */
function FolderRow({
  folder,
  status,
  onRemove,
}: {
  folder: string
  status?: DocumentsStatus["folders"][number]
  onRemove: () => void
}) {
  return (
    <li className="flex items-center gap-3 border-b px-5 py-3 last:border-b-0" data-testid="folder-row">
      <Folder aria-hidden="true" className="size-4 shrink-0 text-steel-400" />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-sm text-steel-50" title={folder}>
          {folder}
        </span>
        {status?.error && <span className="block break-words text-alert-300 text-xs">{status.error}</span>}
        <span className="block text-xs text-steel-400">
          {!status
            ? "Not read yet"
            : status.indexing
              ? `Reading… ${status.files} files so far`
              : `${status.files.toLocaleString("en")} files · ${status.chunks.toLocaleString("en")} passages`}
          {status && status.errors > 0 && (
            <span className="text-alert-300">
              {" "}
              · {status.errors} {status.errors === 1 ? "file" : "files"} he couldn't read
            </span>
          )}
        </span>
      </span>
      {status?.indexing && (
        <LoaderCircle aria-label="Reading" className="size-4 shrink-0 animate-spin text-rigel-300" />
      )}
      <IconButton label={`Take ${folder} out`} onClick={onRemove}>
        <Trash2 />
      </IconButton>
    </li>
  )
}
