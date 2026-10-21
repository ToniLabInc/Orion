/**
 * Skills: routines he follows step by step with his tools, each a folder with a SKILL.md (the Agent Skills format).
 * "Yours" lists them by where they come from (yours, the ones he learned when you taught him, a plugin's, his own),
 * each with a switch and the words that start it. "Discover" offers more from his sources (Anthropic's skills by
 * default). A skill is made here from a template, or added from a Git or GitHub address, or a .zip. A skill's page
 * shows it whole: what starts it, what it uses, its SKILL.md (editable when it is yours or learned), and its files.
 */
import { FileText, LoaderCircle, Plus, Sparkles, Trash2, Upload } from "lucide-react"
import { type FormEvent, useDeferredValue, useId, useRef, useState } from "react"
import { useLocation } from "wouter"
import {
  missing,
  useDeleteSkill,
  useImportSkill,
  useSaveSkill,
  useSkill,
  useSkillDiscover,
  useSkills,
  useSwitchSkill,
  useTool,
} from "@/lib/api"
import { groupSkills } from "@/lib/connectors"
import { useSettled } from "@/lib/hooks"
import { SKILL_TEMPLATE, skillNameOf, skillProblem } from "@/lib/skills"
import type { Skill, SkillDetail } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ask } from "@/ui/ask"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { CopyButton } from "@/ui/copy-button"
import { Dialog } from "@/ui/dialog"
import { Input, Textarea } from "@/ui/input"
import { SearchField } from "@/ui/search"
import { Empty, Failed, Pending } from "@/ui/states"
import { Switch } from "@/ui/switch"
import { toast } from "@/ui/toast"
import { PageHeader } from "../frame"
import { Note, Panel } from "../settings/fields"
import { BackLink, Grid, GroupHeading, ItemCard, useView, ViewSwitch } from "./parts"

const href = (name: string) => `/customize/skills/${encodeURIComponent(name)}`

/** The source of a skill, in words. */
const SOURCE_WORDS: Record<Skill["source"], string> = {
  yours: "Yours",
  learned: "He learned it",
  plugin: "From a plugin",
  builtin: "His own",
}

/**
 * The Skills tab.
 * @returns the tab
 */
export function Skills() {
  const [view, setView] = useView()
  const [writing, setWriting] = useState(false)
  const [importing, setImporting] = useState(false)
  return (
    <>
      <PageHeader
        title="Skills"
        lead="Routines he follows step by step, with his tools. He also learns new ones when you teach him: “when I say movie night, turn the volume down and open Netflix”."
        actions={
          <>
            <ViewSwitch view={view} onChange={setView} label="Skills" />
            <Button size="sm" onClick={() => setWriting(true)} data-testid="skill-new">
              <Plus aria-hidden="true" />
              New skill
            </Button>
            <Button size="sm" onClick={() => setImporting(true)} data-testid="skill-import">
              <Upload aria-hidden="true" />
              Import
            </Button>
          </>
        }
      />
      {view === "yours" ? <Yours /> : <Discover />}
      <WriteDialog open={writing} onClose={() => setWriting(false)} />
      <ImportDialog open={importing} onClose={() => setImporting(false)} />
    </>
  )
}

// ---------- yours ----------
/**
 * The skills you have, by source, narrowed by what is typed.
 * @returns the groups
 */
function Yours() {
  const skills = useSkills()
  const [typed, setTyped] = useState("")
  const q = useDeferredValue(typed)
  const switchSkill = useSwitchSkill()
  if (skills.isError)
    return <Failed title="His skills can't be read" error={skills.error} onRetry={() => void skills.refetch()} />
  if (!skills.data) return <Pending label="Reading his skills" delay={150} page />
  const groups = groupSkills(skills.data, q)
  return (
    <div className="grid gap-5">
      <SearchField
        value={typed}
        onChange={setTyped}
        label="Search your skills"
        placeholder="Search your skills…"
        className="max-w-sm"
        data-testid="skills-search"
      />
      {groups.length ? (
        groups.map((g) => (
          <section key={g.title} aria-label={g.title}>
            <GroupHeading title={g.title} count={g.items.length} />
            <Grid label={g.title}>
              {g.items.map((s) => (
                <ItemCard
                  key={s.name}
                  href={href(s.name)}
                  icon={Sparkles}
                  title={s.name}
                  mono
                  description={s.description}
                  dim={!s.enabled}
                  footer={<Triggers skill={s} />}
                  action={
                    <Switch
                      size="sm"
                      checked={s.enabled}
                      onCheckedChange={(enabled) => switchSkill.mutate({ name: s.name, enabled })}
                      aria-label={`${s.name}: on or off`}
                      data-testid={`skill-switch-${s.name}`}
                    />
                  }
                  testId={`skill-${s.name}`}
                />
              ))}
            </Grid>
          </section>
        ))
      ) : (
        <div className="glass">
          <Empty icon={<Sparkles />}>{q ? "No skill matches." : "No skills yet: write one, or see Discover."}</Empty>
        </div>
      )}
    </div>
  )
}

/**
 * The words that start a skill, as a few chips (or what it is, when it has none).
 * @param props the skill
 * @returns the chips
 */
function Triggers({ skill }: { skill: Skill }) {
  if (!skill.triggers.length)
    return (
      <span>
        {skill.source === "plugin" && skill.plugin ? `From ${skill.plugin}` : "He picks it from its description"}
      </span>
    )
  return (
    <>
      {skill.triggers.slice(0, 2).map((t) => (
        <span
          key={t}
          className="max-w-full truncate rounded-full bg-betel-400/10 px-2 py-0.5 text-betel-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-betel-400)_22%,transparent)]"
        >
          “{t}”
        </span>
      ))}
      {skill.triggers.length > 2 && <span className="font-mono">+{skill.triggers.length - 2}</span>}
    </>
  )
}

// ---------- discover ----------
/**
 * Skills to add, from his sources.
 * @returns the listing
 */
function Discover() {
  const [typed, setTyped] = useState("")
  const q = useSettled(typed)
  const found = useSkillDiscover(q)
  const add = useImportSkill()
  const [adding, setAdding] = useState("")
  if (found.isError && !found.data)
    return <Failed title="His sources can't be read" error={found.error} onRetry={() => void found.refetch()} />
  if (!found.data) return <Pending label="Reading his sources" delay={150} page />
  return (
    <div className="grid gap-4">
      <SearchField
        value={typed}
        onChange={setTyped}
        label="Search for skills"
        placeholder="Search for skills…"
        className="max-w-sm"
        data-testid="skills-discover-search"
      />
      {found.data.length ? (
        <Grid label="Skills to add">
          {found.data.map((s) => (
            <ItemCard
              key={s.url}
              icon={Sparkles}
              title={s.name}
              mono
              description={s.description}
              footer={<span className="truncate">{s.source}</span>}
              badge={
                s.installed ? (
                  <Badge tone="ok" dot>
                    Added
                  </Badge>
                ) : undefined
              }
              action={
                !s.installed && (
                  <Button
                    size="xs"
                    disabled={add.isPending}
                    onClick={() => {
                      setAdding(s.url)
                      add.mutate(
                        { url: s.url },
                        { onSuccess: () => toast.add({ title: "Skill added", description: s.name, type: "success" }) },
                      )
                    }}
                    data-testid={`skill-add-${s.name}`}
                  >
                    {add.isPending && adding === s.url && <LoaderCircle aria-hidden="true" className="animate-spin" />}
                    Add
                  </Button>
                )
              }
              testId={`discover-skill-${s.name}`}
            />
          ))}
        </Grid>
      ) : (
        <div className="glass">
          <Empty icon={<Sparkles />}>No skill matches.</Empty>
        </div>
      )}
    </div>
  )
}

// ---------- writing and importing ----------
/**
 * The dialog that writes a new skill from the template.
 * @param props whether it is `open`, and `onClose`
 * @returns the dialog
 */
function WriteDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      kicker="Skills"
      title="A new skill"
      description="Its SKILL.md: front matter that names it and says when to use it, then the steps."
      testId="skill-dialog"
      wide
    >
      {open && <Editor onDone={onClose} />}
    </Dialog>
  )
}

/**
 * The dialog that adds skills from an address or a .zip.
 * @param props whether it is `open`, and `onClose`
 * @returns the dialog
 */
function ImportDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      kicker="Skills"
      title="Add skills"
      description="From a Git or GitHub address, or a .zip with skill folders in it. A script in a skill runs in the sandbox, when he runs it."
      testId="import-dialog"
    >
      {open && <ImportForm onClose={onClose} />}
    </Dialog>
  )
}

/**
 * The form: an address, or a file.
 * @param props `onClose`
 * @returns the form
 */
function ImportForm({ onClose }: { onClose: () => void }) {
  const [url, setUrl] = useState("")
  const urlId = useId()
  const file = useRef<HTMLInputElement>(null)
  const add = useImportSkill()
  const done = (added: Skill[]) => {
    toast.add({
      title: added.length === 1 ? "Skill added" : `${added.length} skills added`,
      description: added.map((s) => s.name).join(", "),
      type: "success",
    })
    onClose()
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (url.trim()) add.mutate({ url: url.trim() }, { onSuccess: done })
  }
  return (
    <form onSubmit={submit} className="grid gap-4" noValidate>
      <label htmlFor={urlId} className="grid gap-1.5 text-sm text-steel-200">
        An address
        <Input
          id={urlId}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://github.com/anthropics/skills/tree/main/skills/pdf"
          spellCheck={false}
          autoComplete="off"
          className="font-mono"
          data-testid="import-url"
        />
      </label>
      <input
        ref={file}
        type="file"
        accept=".zip,application/zip"
        hidden
        onChange={(e) => {
          const chosen = e.target.files?.[0]
          e.target.value = ""
          if (chosen) add.mutate(chosen, { onSuccess: done })
        }}
      />
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button type="button" tone="ghost" onClick={() => file.current?.click()} disabled={add.isPending}>
          <Upload aria-hidden="true" />
          Choose a .zip
        </Button>
        <Button type="submit" tone="primary" disabled={!url.trim() || add.isPending} data-testid="import-confirm">
          {add.isPending && <LoaderCircle aria-hidden="true" className="animate-spin" />}
          Add
        </Button>
      </div>
    </form>
  )
}

// ---------- the editor ----------
/**
 * The whole SKILL.md as text, saved under the name it gives itself (the name of an existing skill stays).
 * @param props the `skill` being edited (none for a new one) and `onDone`
 * @returns the editor
 */
function Editor({ skill, onDone }: { skill?: SkillDetail; onDone: () => void }) {
  const [, navigate] = useLocation()
  const skills = useSkills()
  const [content, setContent] = useState(skill?.content ?? SKILL_TEMPLATE)
  const save = useSaveSkill()
  const others = skills.data?.map((s) => s.name) ?? []
  const problem = skillProblem(content, skill?.name, others)
  const name = skillNameOf(content)
  const dirty = content !== skill?.content
  const submit = () =>
    save.mutate(
      { name: skill?.name ?? name, content },
      {
        onSuccess: () => {
          onDone()
          if (!skill) navigate(href(name))
        },
      },
    )
  return (
    <div className="grid gap-3">
      <Textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        spellCheck={false}
        rows={16}
        aria-label={skill ? `${skill.name}'s SKILL.md` : "The new skill's SKILL.md"}
        className="field-sizing-fixed min-h-72 resize-y font-mono text-sm"
        data-testid="skill-editor"
      />
      <div className="flex flex-wrap items-center justify-end gap-2">
        <span className={cn("mr-auto min-w-0 truncate text-sm", problem ? "text-alert-300" : "text-steel-400")}>
          {problem || `data/skills/${skill?.name ?? name}/SKILL.md`}
        </span>
        {skill && (
          <Button tone="ghost" disabled={!dirty} onClick={() => setContent(skill.content)}>
            Discard
          </Button>
        )}
        {!skill && (
          <Button tone="ghost" onClick={onDone}>
            Cancel
          </Button>
        )}
        <Button
          tone="primary"
          disabled={Boolean(problem) || save.isPending || (skill !== undefined && !dirty)}
          onClick={submit}
          data-testid="skill-save"
        >
          {save.isPending && <LoaderCircle aria-hidden="true" className="animate-spin" />}
          Save
        </Button>
      </div>
    </div>
  )
}

// ---------- a skill's page ----------
/**
 * A skill's page.
 * @param props the skill's `name`
 * @returns the page
 */
export function SkillPage({ name }: { name: string }) {
  const skill = useSkill(name)
  const [, navigate] = useLocation()
  const switchSkill = useSwitchSkill()
  const remove = useDeleteSkill()
  const back = <BackLink href="/customize/skills" label="Skills" />
  if (skill.isError)
    return (
      <>
        {back}
        {missing(skill.error) ? (
          <div className="glass">
            <Empty icon={<Sparkles />}>There is no skill called {name} (any more).</Empty>
          </div>
        ) : (
          <Failed title="The skill can't be read" error={skill.error} onRetry={() => void skill.refetch()} />
        )}
      </>
    )
  const s = skill.data
  if (!s)
    return (
      <>
        {back}
        <Pending label="Reading the skill" delay={150} page />
      </>
    )
  const askDelete = async () => {
    const text = "Its folder is deleted from his skills. He can't follow it any more."
    if (await ask({ title: `Delete the skill ${s.name}?`, text, ok: "Delete", danger: true, kicker: "Skills" }))
      remove.mutate(s.name, { onSuccess: () => navigate("/customize/skills") })
  }
  return (
    <>
      <PageHeader
        back={back}
        title={<span className="font-mono text-3xl">{s.name}</span>}
        lead={s.description}
        actions={
          <>
            <Badge tone={s.source === "yours" ? "betel" : "neutral"}>
              {s.plugin ? `From ${s.plugin}` : SOURCE_WORDS[s.source]}
            </Badge>
            {s.editable && (
              <Button
                tone="danger"
                size="sm"
                disabled={remove.isPending}
                onClick={() => void askDelete()}
                data-testid="skill-delete"
              >
                <Trash2 aria-hidden="true" />
                Delete
              </Button>
            )}
            {/* biome-ignore lint/a11y/noLabelWithoutControl: the switch inside is the control */}
            <label className="flex items-center gap-2 text-sm text-steel-300">
              On
              <Switch
                checked={s.enabled}
                onCheckedChange={(enabled) => switchSkill.mutate({ name: s.name, enabled })}
                aria-label={`${s.name}: on or off`}
                data-testid="skill-page-switch"
              />
            </label>
          </>
        }
      />
      <When s={s} />
      <Panel
        title="SKILL.md"
        icon={FileText}
        description={s.editable ? "Yours to change: it saves as the file" : "Read only"}
        action={!s.editable && <CopyButton text={s.content} label="Copy SKILL.md" />}
        data-testid="skill-file"
      >
        <div className="px-5 pb-5">
          {s.editable ? (
            <Editor skill={s} onDone={() => {}} />
          ) : (
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-control bg-void-950/45 p-3.5 font-mono text-xs text-steel-200 leading-relaxed shadow-[inset_0_0_0_1px_var(--hairline)]">
              {s.content}
            </pre>
          )}
        </div>
      </Panel>
      <Files s={s} />
    </>
  )
}

/**
 * What starts a skill, and what it uses.
 * @param props the skill (`s`)
 * @returns the panel
 */
function When({ s }: { s: Skill }) {
  return (
    <Panel title="How he uses it" icon={Sparkles}>
      <div className="grid gap-3 px-5 pb-4">
        <div className="grid gap-1.5">
          <p className="text-sm text-steel-200">Starts with</p>
          {s.triggers.length ? (
            <ul className="flex flex-wrap gap-1.5">
              {s.triggers.map((t) => (
                <li
                  key={t}
                  className="rounded-full bg-betel-400/10 px-2.5 py-0.5 text-sm text-betel-200 shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-betel-400)_22%,transparent)]"
                >
                  “{t}”
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-steel-400">No words of its own: he picks it from its description.</p>
          )}
        </div>
        <div className="grid gap-1.5">
          <p className="text-sm text-steel-200">Uses</p>
          {s.tools.length ? (
            <ul className="flex flex-wrap gap-1.5">
              {s.tools.map((t) => (
                <li
                  key={t}
                  className="rounded-chip bg-steel-50/6 px-2 py-0.5 font-mono text-xs text-steel-200 shadow-[inset_0_0_0_1px_var(--hairline)]"
                >
                  {t}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-steel-400">Any of his tools.</p>
          )}
        </div>
      </div>
    </Panel>
  )
}

/**
 * The files of a skill's folder beside its SKILL.md; one opens to read (text only).
 * @param props the skill (`s`)
 * @returns the panel, or nothing when the folder holds only SKILL.md
 */
function Files({ s }: { s: Skill }) {
  const files = s.files.filter((f) => f !== "SKILL.md")
  const { run, pending } = useTool()
  const [open, setOpen] = useState<{ path: string; text: string; ok: boolean } | null>(null)
  if (!files.length) return null
  const view = async (path: string) => {
    const answer = await run("skills", "skill_file", { skill: s.name, path })
    setOpen({ path, text: answer.result, ok: answer.ok })
  }
  return (
    <Panel
      title="Its files"
      icon={FileText}
      description="Scripts run through his shell tool, in the sandbox"
      data-testid="skill-files"
    >
      <ul className="border-t">
        {files.map((f) => (
          <li key={f} className="border-b last:border-b-0">
            <button
              type="button"
              aria-expanded={open?.path === f}
              disabled={pending}
              onClick={() => (open?.path === f ? setOpen(null) : void view(f))}
              className="flex w-full items-center gap-3 px-5 py-2.5 text-left font-mono text-sm text-steel-100 outline-none transition-colors hover:bg-steel-50/4 focus-visible:bg-steel-50/6"
            >
              <FileText aria-hidden="true" className="size-4 shrink-0 text-steel-400" />
              <span className="min-w-0 flex-1 truncate">{f}</span>
            </button>
            {open?.path === f && (
              <div className="relative px-5 pb-4">
                <pre
                  className={cn(
                    "max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-control bg-void-950/45 p-3 pr-9 font-mono text-xs leading-relaxed shadow-[inset_0_0_0_1px_var(--hairline)]",
                    open.ok ? "text-steel-300" : "text-alert-200",
                  )}
                >
                  {open.text}
                </pre>
                <CopyButton text={open.text} label="Copy the file" className="absolute top-1 right-6" />
              </div>
            )}
          </li>
        ))}
      </ul>
      <Note>Only text can be read here.</Note>
    </Panel>
  )
}
