/**
 * The frame of a page with tabs of its own (Settings, Customize): a glass column of links on the left on a wide screen
 * (a pill of light glides to the one open), a row along the top otherwise (kept scrolled to the one open), and beside
 * or under it the page's own column, which scrolls smoothly (Lenis) and is as wide as reading wants. A page's header is
 * its name in Instrument Serif, what it holds, and a live word on it.
 */
import type { LucideIcon } from "lucide-react"
import { motion } from "motion/react"
import { type ReactNode, useEffect, useRef } from "react"
import { Link } from "wouter"
import { useHash } from "@/lib/hooks"
import { SPRING } from "@/lib/motion"
import { useSmoothScroll } from "@/lib/scroll"
import { cn } from "@/lib/utils"
import { Badge } from "@/ui/badge"

/** One link of a page's navigation. */
export interface NavItem {
  href: string
  label: string
  icon: LucideIcon
  count?: number
}

/**
 * The frame.
 * @param props the page's `title`, a `note` under it, its `items`, the one `active` (its href), a `footer` for the
 *   column, the content (`children`), the page's test id, how wide its column may be (`wide`), and a `resetKey` whose
 *   change takes the column back to its top (another page within it)
 * @returns the page
 */
export function Frame({
  title,
  note,
  items,
  active,
  footer,
  children,
  testId,
  wide = false,
  resetKey,
}: {
  title: string
  note: string
  items: NavItem[]
  active: string
  footer?: ReactNode
  children: ReactNode
  testId: string
  wide?: boolean
  resetKey?: string
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  useSmoothScroll(scroller, content)
  // biome-ignore lint/correctness/useExhaustiveDependencies: another page within the column starts at its top
  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 }) // (not returned: an effect's return value is its cleanup)
  }, [resetKey])
  // an address with an #anchor goes to that panel (twice: what loads above it moves it)
  const anchor = useHash()
  // biome-ignore lint/correctness/useExhaustiveDependencies: another page within the column looks for it again
  useEffect(() => {
    if (!anchor) return
    const go = () => document.getElementById(anchor)?.scrollIntoView({ block: "start" })
    const timers = [setTimeout(go, 60), setTimeout(go, 700)]
    return () => {
      for (const timer of timers) clearTimeout(timer)
    }
  }, [anchor, resetKey])
  return (
    <div
      className="mx-auto flex h-full max-w-[1400px] flex-col gap-4 px-3 pt-[calc(var(--bar-h)+0.75rem)] desk:flex-row desk:gap-6 desk:px-6 desk:pb-6"
      data-testid={testId}
    >
      <SideNav title={title} note={note} items={items} active={active} footer={footer} />
      <div ref={scroller} className="no-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div ref={content} className={cn("mx-auto grid grid-cols-1 gap-4 pb-16", wide ? "max-w-5xl" : "max-w-3xl")}>
          {children}
        </div>
      </div>
    </div>
  )
}

/**
 * The page's links: a glass column on a wide screen, a row along the top otherwise.
 * @param props the page's `title` and `note`, its `items`, the one `active`, and a `footer`
 * @returns the navigation
 */
function SideNav({
  title,
  note,
  items,
  active,
  footer,
}: {
  title: string
  note: string
  items: NavItem[]
  active: string
  footer?: ReactNode
}) {
  const nav = useRef<HTMLElement>(null)
  useEffect(() => {
    nav.current?.querySelector(`[href="${active}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" })
  }, [active])
  return (
    <div className="glass flex shrink-0 flex-col desk:w-64 desk:self-start">
      <div className="hidden px-5 pt-5 pb-3 desk:block">
        <p className="font-serif text-2xl text-steel-50">{title}</p>
        <p className="mt-1 text-xs text-steel-400">{note}</p>
      </div>
      <nav
        ref={nav}
        aria-label={title}
        className="no-scrollbar flex gap-0.5 overflow-x-auto p-1.5 desk:flex-col desk:overflow-visible desk:px-2 desk:pb-2"
        data-testid="page-nav"
      >
        {items.map((item) => {
          const open = item.href === active
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={open ? "page" : undefined}
              data-testid={`tab-${item.href.split("/").pop()}`}
              className={cn(
                "group/tab relative flex h-10 shrink-0 items-center gap-2 rounded-control px-2.5 text-sm outline-none tab:gap-3 tab:px-3 transition-colors duration-(--dur-quick) focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-rigel-400",
                open ? "text-steel-50" : "text-steel-400 hover:text-steel-100",
              )}
            >
              {open && (
                <motion.span
                  layoutId={`${title}-tab`}
                  aria-hidden="true"
                  transition={SPRING.snappy}
                  className="absolute inset-0 rounded-control bg-steel-50/9 shadow-[inset_0_0_0_1px_var(--hairline),inset_0_1px_0_0_var(--highlight)]"
                />
              )}
              <item.icon
                aria-hidden="true"
                className={cn("relative size-4 shrink-0 max-tab:hidden", open && "text-rigel-300")}
              />
              <span className="relative whitespace-nowrap">{item.label}</span>
              {item.count !== undefined && item.count > 0 && (
                <span className="relative ml-auto rounded-full bg-steel-50/8 px-1.5 font-mono text-micro text-steel-300 tabular-nums">
                  {item.count}
                </span>
              )}
            </Link>
          )
        })}
      </nav>
      {footer}
    </div>
  )
}

/** How a page's parts arrive: rising one after another (the page's container staggers them). */
export const ARRIVE = {
  hidden: { opacity: 0, y: 10 },
  shown: { opacity: 1, y: 0, transition: SPRING.gentle },
} as const

/**
 * A page's header: its name, what it holds, a live word on it, and actions.
 * @param props the `title`, a `lead` under it, a `status` word, `actions` on the right, and a `back` link above
 * @returns the header
 */
export function PageHeader({
  title,
  lead,
  status,
  actions,
  back,
}: {
  title: ReactNode
  lead?: ReactNode
  status?: string
  actions?: ReactNode
  back?: ReactNode
}) {
  return (
    <motion.header variants={ARRIVE} className="grid gap-1.5 px-1 pt-2 pb-2" data-testid="page-header">
      {back}
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
        <h1 className="min-w-0 font-serif text-4xl text-steel-50 leading-none">{title}</h1>
        {(status || actions) && (
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            {status && (
              <Badge tone="rigel" dot className="h-6 px-2.5 text-xs">
                {status}
              </Badge>
            )}
            {actions}
          </div>
        )}
      </div>
      {lead && <p className="max-w-2xl text-sm text-steel-400">{lead}</p>}
    </motion.header>
  )
}
