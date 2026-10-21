/**
 * Your secrets: where Orion keeps them (the system keyring, or files only you can read), whether that store answers,
 * and each secret by its name: set or not, what uses it, a way to set it (typed into a field that never shows it, and
 * sent to the vault alone) and to remove it. Their values never come back to the HUD, nor go to a model.
 */
import { KeyRound, LockKeyhole, TriangleAlert } from "lucide-react"
import { type FormEvent, useState } from "react"
import { missing, useConnectors, useDeleteSecret, useSecrets, useSetSecret } from "@/lib/api"
import { secretUser } from "@/lib/connectors"
import type { SecretsState } from "@/lib/types"
import { ask } from "@/ui/ask"
import { Badge } from "@/ui/badge"
import { Button } from "@/ui/button"
import { Input } from "@/ui/input"
import { Failed, Pending } from "@/ui/states"
import { Code, Note, Panel } from "./fields"

const STORES: Record<SecretsState["store"], string> = {
  keyring: "In this machine's keyring",
  files: "In files only your account can read",
}

/**
 * The panel.
 * @returns the panel (nothing on an Orion without a vault)
 */
export function Vault() {
  const secrets = useSecrets()
  if (missing(secrets.error)) return null
  const data = secrets.data
  return (
    <Panel
      title="Your secrets"
      icon={KeyRound}
      description={data ? STORES[data.store] : "Keys, tokens and passwords"}
      action={
        data &&
        (data.ok ? (
          <Badge tone="ok" dot>
            Unlocked
          </Badge>
        ) : (
          <Badge tone="alert" dot>
            Not answering
          </Badge>
        ))
      }
      data-testid="safety-vault"
    >
      {secrets.isError ? (
        <div className="px-5 pb-4">
          <Failed title="The vault can't be read" error={secrets.error} onRetry={() => void secrets.refetch()} />
        </div>
      ) : !data ? (
        <Pending label="Reading the vault" delay={150} className="px-5 pb-4" />
      ) : (
        <>
          {!data.ok && (
            <p className="flex items-start gap-2.5 px-5 pb-4 text-sm text-alert-100">
              <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-alert-400" />
              {data.error || "The vault doesn't answer: secrets can't be read or saved."}
            </p>
          )}
          {data.items.length ? (
            <ul className="border-t">
              {data.items.map((item) => (
                <Secret key={item.name} item={item} disabled={!data.ok} />
              ))}
            </ul>
          ) : (
            <Note>No secret yet: a key you give a connector or a setting goes here.</Note>
          )}
          <Note>
            config.yaml only names them (<Code>{`\${secret:NAME}`}</Code>). From a terminal:{" "}
            <Code>orion secret set NAME</Code>.
          </Note>
        </>
      )}
    </Panel>
  )
}

/**
 * One secret: its name, whether it is set, what uses it, and setting or removing it.
 * @param props the secret (`item`), and whether the vault is `disabled`
 * @returns the row
 */
function Secret({ item, disabled }: { item: SecretsState["items"][number]; disabled: boolean }) {
  const [typing, setTyping] = useState(false)
  const [value, setValue] = useState("")
  const save = useSetSecret()
  const remove = useDeleteSecret()
  const connectors = useConnectors().data
  const users = item.used_by.map((place) => secretUser(place, new Map((connectors ?? []).map((c) => [c.id, c.title]))))
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!value) return
    save.mutate(
      { name: item.name, value },
      {
        onSuccess: () => {
          setValue("")
          setTyping(false)
        },
      },
    )
  }
  const askRemove = async () => {
    const text = `${users.length ? `${users.join(", ")} won't have it any more. ` : ""}It is deleted for good.`
    if (await ask({ title: `Remove ${item.name}?`, text, ok: "Remove", danger: true, kicker: "Secrets" }))
      remove.mutate(item.name)
  }
  return (
    <li className="grid gap-3 border-b px-5 py-3.5 last:border-b-0" data-testid={`secret-${item.name}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <LockKeyhole aria-hidden="true" className="size-4 shrink-0 text-steel-400" />
        <span className="min-w-0 flex-1 truncate font-mono text-sm text-steel-50">{item.name}</span>
        {item.set ? <Badge tone="ok">Set</Badge> : <Badge>Not set</Badge>}
        <div className="flex gap-1.5">
          <Button size="xs" disabled={disabled} aria-expanded={typing} onClick={() => setTyping((t) => !t)}>
            {item.set ? "Replace" : "Set"}
          </Button>
          {item.set && (
            <Button size="xs" tone="ghost" disabled={disabled || remove.isPending} onClick={() => void askRemove()}>
              Remove
            </Button>
          )}
        </div>
      </div>
      {users.length > 0 && <p className="pl-6.5 text-xs text-steel-400">Used by {users.join(", ")}</p>}
      {typing && (
        <form onSubmit={submit} className="flex gap-2 pl-6.5">
          <Input
            type="password"
            autoComplete="off"
            autoFocus
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Paste it here: it goes to the vault and nowhere else"
            aria-label={`${item.name}'s new value`}
            className="min-w-0 flex-1 font-mono"
          />
          <Button type="submit" tone="primary" disabled={!value || save.isPending}>
            Save
          </Button>
        </form>
      )}
    </li>
  )
}
