/**
 * The pieces Customize's lists and pages share: the card of a connector, a skill or a plugin (the whole card opens its
 * page; its own controls sit above that link), the grid they sit in, the Yours / Discover switch (in the address:
 * ?view=discover), a back link, and a section's heading.
 */
import { ArrowLeft, type LucideIcon } from "lucide-react"
import type { ReactNode } from "react"
import { Link, useSearchParams } from "wouter"
import { cn } from "@/lib/utils"
import { Segmented } from "@/ui/tabs"

/**
 * An address part, decoded (wouter may have done it already: a name holds no "%").
 * @param part the part
 * @returns the text
 */
export function decoded(part: string): string {
  try {
    return decodeURIComponent(part)
  } catch {
    return part
  }
}

/**
 * The tile an item's icon sits in.
 * @param props the `icon`, its `size` class and `tone`
 * @returns the tile
 */
export function IconTile({
  icon: Icon,
  className,
  tone = "text-rigel-300",
}: {
  icon: LucideIcon
  className?: string
  tone?: string
}) {
  return (
    <span
      className={cn(
        "grid size-10 shrink-0 place-items-center rounded-control bg-steel-50/6 shadow-[inset_0_0_0_1px_var(--hairline)] [&_svg]:size-5",
        tone,
        className,
      )}
    >
      <Icon aria-hidden="true" />
    </span>
  )
}

/**
 * A card in one of Customize's grids.
 * @param props where it leads (`href`, none for a card of Discover), its `icon`, `title` (`mono` for a skill's name),
 *   `description`, a `badge` at its top right, a `footer` line, an `action` in its corner (above the link), a test id,
 *   and whether it is `dim` (off)
 * @returns the card
 */
export function ItemCard({
  href,
  icon,
  title,
  mono = false,
  description,
  badge,
  footer,
  action,
  testId,
  dim = false,
}: {
  href?: string
  icon: LucideIcon
  title: string
  mono?: boolean
  description: string
  badge?: ReactNode
  footer?: ReactNode
  action?: ReactNode
  testId: string
  dim?: boolean
}) {
  const name = cn("truncate font-medium text-base text-steel-50", mono && "font-mono text-md")
  return (
    <li
      className={cn(
        "glass group/card relative flex flex-col gap-3 p-4 transition-shadow duration-(--dur-quick) has-focus-visible:outline-2 has-focus-visible:outline-rigel-400",
        href && "hover:shadow-[var(--shadow-elevated),inset_0_0_0_1px_var(--hairline-strong)]",
      )}
      data-testid={testId}
    >
      <div className="flex items-start gap-3">
        <IconTile icon={icon} tone={dim ? "text-steel-500" : "text-rigel-300"} />
        <div className="min-w-0 flex-1">
          <h3 className="flex min-w-0 items-center gap-2">
            {href ? (
              <Link href={href} className={cn(name, "outline-none after:absolute after:inset-0 after:rounded-card")}>
                {title}
              </Link>
            ) : (
              <span className={name}>{title}</span>
            )}
          </h3>
          <p className="mt-0.5 line-clamp-2 text-sm text-steel-400">{description}</p>
        </div>
        {badge && <span className="shrink-0">{badge}</span>}
      </div>
      {(footer || action) && (
        <div className="mt-auto flex items-center gap-2 text-xs text-steel-400">
          <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">{footer}</span>
          {action && <span className="relative z-1 flex shrink-0 items-center gap-2">{action}</span>}
        </div>
      )}
    </li>
  )
}

/**
 * The grid the cards sit in: one column on a phone, two where there is room (the container's width, not the screen's).
 * @param props the cards (`children`) and an aria `label`
 * @returns the grid
 */
export function Grid({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div className="@container">
      <ul aria-label={label} className="grid grid-cols-1 gap-3 @2xl:grid-cols-2">
        {children}
      </ul>
    </div>
  )
}

/**
 * A group's heading, with what it holds.
 * @param props the `title`, a `note`, and how many (`count`)
 * @returns the heading
 */
export function GroupHeading({ title, note, count }: { title: string; note?: string; count?: number }) {
  return (
    <div className="flex items-baseline gap-3 px-1 pb-2">
      <h2 className="font-serif text-2xl text-steel-50">{title}</h2>
      {count !== undefined && <span className="font-mono text-xs text-steel-400 tabular-nums">{count}</span>}
      {note && <span className="min-w-0 truncate text-sm text-steel-500 max-tab:hidden">{note}</span>}
    </div>
  )
}

/**
 * Words with the addresses in them made links (a service's page to make an app on); the address a sign-in returns to
 * stays as text to copy, since it leads nowhere.
 * @param props the `text`
 * @returns the words
 */
export function Prose({ text }: { text: string }) {
  let links = 0
  return text.split(/(https?:\/\/[^\s)]+)/).map((part, i) => {
    if (i % 2 === 0) return part // (the words between the addresses)
    const key = `address-${links++}`
    return part.includes("/oauth/callback") ? (
      <code
        key={key}
        className="select-all rounded-[5px] bg-steel-50/8 px-1 py-0.5 font-mono text-[0.92em] text-rigel-100"
      >
        {part}
      </code>
    ) : (
      <a
        key={key}
        href={part}
        target="_blank"
        rel="noreferrer noopener"
        className="text-rigel-300 underline-offset-2 hover:underline"
      >
        {part}
      </a>
    )
  })
}

/** Which view of a list is open: the things you have, or what you could add. */
type View = "yours" | "discover"

/**
 * The open view, from the address, and the way to change it (the other params of the address stay).
 * @returns the view and its setter
 */
export function useView(): [View, (view: View) => void] {
  const [params, setParams] = useSearchParams()
  const view: View = params.get("view") === "discover" ? "discover" : "yours"
  const set = (next: View) =>
    setParams(
      (before) => {
        const out = new URLSearchParams(before)
        if (next === "discover") out.set("view", "discover")
        else out.delete("view")
        return out
      },
      { replace: true },
    )
  return [view, set]
}

const VIEWS = [
  { value: "yours" as const, label: "Yours" },
  { value: "discover" as const, label: "Discover" },
]

/**
 * The Yours / Discover switch.
 * @param props the open `view`, `onChange`, and what the views are of (`label`)
 * @returns the switch
 */
export function ViewSwitch({ view, onChange, label }: { view: View; onChange: (view: View) => void; label: string }) {
  return <Segmented value={view} onChange={onChange} choices={VIEWS} label={label} id="view-switch" />
}

/**
 * A link back to the list a page belongs to.
 * @param props where (`href`) and to what (`label`)
 * @returns the link
 */
export function BackLink({ href, label }: { href: string; label: string }) {
  return (
    <Link
      href={href}
      className="-ml-1 flex w-fit items-center gap-1.5 rounded-chip px-1 py-0.5 text-sm text-steel-400 outline-none transition-colors hover:text-steel-100 focus-visible:outline-2 focus-visible:outline-rigel-400"
    >
      <ArrowLeft aria-hidden="true" className="size-3.5" />
      {label}
    </Link>
  )
}
