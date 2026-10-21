/**
 * News: the top headlines (his get_news tool, with no topic). Opening the card asks him for them, and again every
 * REFRESH_MS while it stays open (his tool keeps an answer a while, so reopening soon asks the web nothing); they
 * arrive as a "news" event, so headlines he fetched for you by voice show here too. A headline opens its article.
 */
import { ArrowUpRight, RefreshCw } from "lucide-react"
import { motion } from "motion/react"
import { useEffect, useRef, useState } from "react"
import { useNews } from "@/lib/api"
import { useNow } from "@/lib/hooks"
import { stagger } from "@/lib/motion"
import { useLive } from "@/lib/store"
import { cn } from "@/lib/utils"
import { IconButton } from "@/ui/button"
import { Skeleton } from "@/ui/states"
import type { CardProps } from "./card"

const REFRESH_MS = 15 * 60_000

/**
 * How long ago a headline was published, in a word or two.
 * @param iso when (ISO)
 * @param now the time now
 * @returns "12 min ago", "3 h ago", "2 d ago" (or nothing for a date it can't read)
 */
export function ago(iso: string, now: number): string {
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return ""
  const minutes = Math.max(0, Math.round((now - at) / 60_000))
  if (minutes < 60) return `${minutes} min ago`
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`
  return `${Math.round(minutes / 1440)} d ago`
}

/**
 * The news card.
 * @param props the card's
 * @returns the body
 */
export default function News({ setNote }: CardProps) {
  const news = useLive((s) => s.news)
  const online = useLive((s) => s.link === "online")
  const fetchNews = useNews()
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const now = useNow(60_000).getTime()

  const refresh = async () => {
    setBusy(true)
    setProblem(await fetchNews())
    setBusy(false)
  }
  const latest = useRef(refresh) // the header's button calls the newest refresh, without making a new note each render
  latest.current = refresh

  // biome-ignore lint/correctness/useExhaustiveDependencies: asked on opening and on reconnecting, then on a timer
  useEffect(() => {
    if (!online) return
    void refresh()
    const timer = setInterval(() => void refresh(), REFRESH_MS)
    return () => clearInterval(timer)
  }, [online])

  useEffect(
    () =>
      setNote(
        <IconButton
          label="Refresh the headlines"
          size="xs"
          disabled={busy || !online}
          onClick={() => void latest.current()}
          data-testid="news-refresh"
        >
          <RefreshCw className={cn(busy && "animate-spin")} />
        </IconButton>,
      ),
    [busy, online, setNote],
  )

  return (
    <div className="px-2 pb-3" data-testid="widget-news">
      {news?.length ? (
        <motion.ol
          className="grid"
          initial="hidden"
          animate="shown"
          variants={{ shown: { transition: stagger(0.04) } }}
        >
          {news.slice(0, 8).map((item) => (
            <motion.li
              key={item.url || item.title}
              variants={{ hidden: { opacity: 0, y: 6 }, shown: { opacity: 1, y: 0 } }}
            >
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                className="group/headline grid grid-cols-[1fr_auto] gap-x-2 rounded-control px-2 py-2 outline-none transition-colors hover:bg-steel-50/5 focus-visible:outline-2 focus-visible:outline-rigel-400"
              >
                <span className="line-clamp-2 text-sm text-steel-100 leading-snug group-hover/headline:text-steel-50">
                  {item.title}
                </span>
                <ArrowUpRight
                  aria-hidden="true"
                  className="mt-0.5 size-3.5 text-steel-600 transition-colors group-hover/headline:text-rigel-300"
                />
                <span className="mt-0.5 flex gap-1.5 text-micro text-steel-500">
                  <span className="truncate">{item.source}</span>
                  {ago(item.date, now) && <span className="shrink-0">· {ago(item.date, now)}</span>}
                </span>
              </a>
            </motion.li>
          ))}
        </motion.ol>
      ) : problem && !busy ? (
        <p className="px-2 pb-1 text-sm text-steel-400">{problem}</p>
      ) : (
        <Skeleton lines={4} className="px-2" />
      )}
    </div>
  )
}
