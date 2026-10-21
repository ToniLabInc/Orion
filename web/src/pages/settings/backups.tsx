/**
 * Backups: copies of everything he keeps (his database, memory, skills, plugins and settings; never the vault's
 * secrets, the models, caches, logs or the documents' index, which he builds again), the latest first with when, how
 * big and where (this machine, or restic's repository off it); a backup now, which shows until its `backup` event says
 * it is done; when he makes one by himself and how many he keeps (his schema's backup options); whether restic sends
 * them off this machine; and how to bring one back.
 */
import { Archive, LoaderCircle } from "lucide-react"
import { useEffect, useState } from "react"
import { missing, useBackupNow, useBackups } from "@/lib/api"
import { bytes, shortDate } from "@/lib/format"
import type { BackupsState } from "@/lib/types"
import { onEvent } from "@/lib/ws"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { Failed, Pending } from "@/ui/states"
import { Code, Note, Panel, PathValue, Row, SectionFields } from "./fields"

/** How many backups the list shows. */
const SHOWN = 6

/**
 * The panel.
 * @returns the panel (nothing on an Orion that doesn't back up)
 */
export function Backups() {
  const backups = useBackups()
  const now = useBackupNow()
  const [running, setRunning] = useState(false)
  useEffect(() => onEvent("backup", (ev) => setRunning(ev.phase === "start")), [])
  if (missing(backups.error)) return null
  const busy = running || now.isPending
  return (
    <Panel
      title="Backups"
      icon={Archive}
      description="Everything he keeps, but your secrets"
      action={
        <Button
          size="sm"
          disabled={busy}
          onClick={() => now.mutate(undefined, { onSuccess: () => setRunning(true) })}
          data-testid="backup-now"
        >
          {busy && <LoaderCircle aria-hidden="true" className="animate-spin" />}
          {busy ? "Backing up…" : "Back up now"}
        </Button>
      }
      data-testid="memory-backups"
    >
      {backups.isError ? (
        <div className="px-5 pb-4">
          <Failed title="The backups can't be read" error={backups.error} onRetry={() => void backups.refetch()} />
        </div>
      ) : !backups.data ? (
        <Pending label="Reading the backups" delay={150} className="px-5 pb-4" />
      ) : (
        <>
          <List items={backups.data.items} />
          <SectionFields section="backup" except={["backup.folder"]} />
          <Row label="Where" hint="A folder on this machine.">
            <PathValue path={backups.data.folder} />
          </Row>
          <Offsite restic={backups.data.restic} />
          <Note>
            To bring one back, stop Orion, then run <Code>orion restore latest</Code> (or a backup's name). What he has
            now moves aside first, into his data folder.
          </Note>
        </>
      )}
    </Panel>
  )
}

/**
 * The latest backups.
 * @param props the backups (`items`)
 * @returns the list
 */
function List({ items }: { items: BackupsState["items"] }) {
  const sorted = items.toSorted((a, b) => b.at - a.at)
  if (!sorted.length) return <Note className="border-t-0 pt-0">No backup yet.</Note>
  return (
    <ul className="grid grid-cols-1 gap-1 px-5 pb-4" data-testid="backup-list">
      {sorted.slice(0, SHOWN).map((b) => {
        const when = new Date(b.at * 1000)
        return (
          <li
            key={`${b.where}-${b.name}`}
            className="flex items-center gap-3 rounded-control bg-void-950/40 px-3.5 py-2 shadow-[inset_0_0_0_1px_var(--hairline)]"
          >
            <span className="min-w-0 flex-1">
              <span className="block truncate font-mono text-sm text-steel-100">{b.name}</span>
              <span className="block text-xs text-steel-400">
                {shortDate(when)} at{" "}
                {when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}
              </span>
            </span>
            <span className="shrink-0 font-mono text-xs text-steel-300 tabular-nums">{bytes(b.size)}</span>
            {b.where === "restic" ? <Badge tone="aurora">Off this machine</Badge> : <Badge>Here</Badge>}
          </li>
        )
      })}
      {sorted.length > SHOWN && (
        <li className="px-1 pt-1 text-xs text-steel-400">and {sorted.length - SHOWN} older ones</li>
      )}
    </ul>
  )
}

/**
 * Whether restic sends the backups off this machine.
 * @param props restic's state
 * @returns the row
 */
function Offsite({ restic }: { restic: BackupsState["restic"] }) {
  return (
    <Row
      label="Off this machine"
      hint={
        !restic.available ? (
          "restic isn't installed here: the backups stay on this machine."
        ) : !restic.configured ? (
          <>
            restic can keep an encrypted copy elsewhere: set <Code>backup.restic.repository</Code> in config.yaml, and
            its password with <Code>orion secret set backup.restic.password</Code>.
          </>
        ) : restic.error ? (
          `restic's repository can't be read: ${restic.error}`
        ) : (
          "Each backup also goes, encrypted, to restic's repository."
        )
      }
    >
      {restic.available && restic.configured ? (
        <Badge tone={restic.error ? "alert" : "ok"} dot>
          restic
        </Badge>
      ) : (
        <Badge>{restic.available ? "Not set up" : "Not installed"}</Badge>
      )}
    </Row>
  )
}
