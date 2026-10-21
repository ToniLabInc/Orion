/**
 * The HUD's routes and providers. The stage stays mounted (hidden) once opened, so its conversation isn't rebuilt each
 * time you come back from another page, and its effects stop while it's hidden; the other pages (memory, automations,
 * activity, customize, settings) are lazy chunks, preloaded from the top bar and the palette, behind a pending line
 * that shows only if loading takes over 150 ms. A page arrives rising a little and fading in; going back to the stage,
 * it sinks away. A page that fails shows a Reload in its place while the rest goes on working. An address that moved
 * (an older HUD's settings tab) leads to where it lives now.
 */
import { QueryClientProvider } from "@tanstack/react-query"
import { LucideProvider } from "lucide-react"
import { AnimatePresence, MotionConfig, motion } from "motion/react"
import { Activity, lazy, type ReactNode, Suspense, useRef } from "react"
import { Redirect, useLocation } from "wouter"
import { queryClient, useGestures } from "@/lib/api"
import { DUR, EXPO, SPRING } from "@/lib/motion"
import { CHUNKS, GESTURES, movedTo, type PageId, pageOf } from "@/lib/nav"
import { useLive } from "@/lib/store"
import { Shell } from "@/shell/shell"
import { ErrorBoundary, Pending } from "@/ui/states"
import { Toasts } from "@/ui/toast"
import { TooltipProvider } from "@/ui/tooltip"

/** The pairing screen, for a device on the network that isn't paired yet: its own chunk. */
const PairOverlay = lazy(() => import("@/shell/pair").then((m) => ({ default: m.PairOverlay })))

// Each page is its own chunk: three and the force graph load only with the memory map.
const Stage = lazy(CHUNKS.stage)
const PAGE_CHUNKS: Record<Exclude<PageId, "stage">, ReturnType<typeof lazy>> = {
  memory: lazy(CHUNKS.memory),
  automations: lazy(CHUNKS.automations),
  activity: lazy(CHUNKS.activity),
  customize: lazy(CHUNKS.customize),
  settings: lazy(CHUNKS.settings),
}

/**
 * A page other than the stage: under the top bar, arriving and leaving.
 * @param props its content
 * @returns the page's box
 */
function PageBox({ children }: { children: ReactNode }) {
  return (
    <motion.div
      className="absolute inset-0"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0, transition: SPRING.float }}
      exit={{ opacity: 0, y: 8, transition: { duration: DUR.base, ease: [...EXPO] } }}
    >
      <ErrorBoundary>
        <Suspense fallback={<Pending delay={150} page />}>{children}</Suspense>
      </ErrorBoundary>
    </motion.div>
  )
}

/**
 * The gestures' old address: the first gesture remote's own settings, once the server has said which (Customize's
 * connectors when there is none).
 * @returns the redirect, once known
 */
function ToGestures() {
  const gestures = useGestures()
  if (gestures.isPending) return <Pending delay={150} page />
  const remote = gestures.data?.remotes[0]?.name
  return (
    <Redirect
      to={remote ? `/customize/connectors/${encodeURIComponent(remote)}/settings` : "/customize/connectors"}
      replace
    />
  )
}

/**
 * The page for the address.
 * @returns the pages
 */
function Pages() {
  const [location] = useLocation()
  const page = pageOf(location)
  const stageOpened = useRef(false)
  const moved = movedTo(location)
  if (moved === GESTURES) return <ToGestures />
  if (moved) return <Redirect to={moved} replace />
  if (page === null) return <Redirect to="/" replace />
  stageOpened.current ||= page === "stage"
  const Page = page === "stage" ? null : PAGE_CHUNKS[page]
  return (
    <>
      {stageOpened.current && (
        <Activity mode={page === "stage" ? "visible" : "hidden"}>
          <div className="absolute inset-0">
            <ErrorBoundary>
              <Suspense fallback={<Pending delay={150} page />}>
                <Stage />
              </Suspense>
            </ErrorBoundary>
          </div>
        </Activity>
      )}
      <AnimatePresence initial={false}>
        {Page && (
          <PageBox key={page}>
            <Page />
          </PageBox>
        )}
      </AnimatePresence>
    </>
  )
}

/**
 * The whole HUD.
 * @returns the app
 */
export function App() {
  const unpaired = useLive((s) => s.link === "unpaired")
  return (
    <QueryClientProvider client={queryClient}>
      <MotionConfig reducedMotion="user">
        <LucideProvider strokeWidth={1.5}>
          <TooltipProvider>
            <ErrorBoundary>
              <Shell>
                <Pages />
              </Shell>
              {unpaired && (
                <Suspense fallback={null}>
                  <PairOverlay />
                </Suspense>
              )}
            </ErrorBoundary>
            <Toasts />
          </TooltipProvider>
        </LucideProvider>
      </MotionConfig>
    </QueryClientProvider>
  )
}
