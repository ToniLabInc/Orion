/**
 * Automations: what he does on his own. Jobs (requests he runs at the times you set, and where their answers go) and
 * watchers (what he keeps an eye on in his machine, and when he speaks up). The tab is in the address
 * (/automations/jobs). The routines he follows step by step, his skills, are in Customize. The page scrolls smoothly
 * (Lenis).
 */
import { motion } from "motion/react"
import { useRef } from "react"
import { useLocation, useRoute } from "wouter"
import { stagger } from "@/lib/motion"
import { AUTOMATIONS_TABS, type AutomationsTab } from "@/lib/nav"
import { useSmoothScroll } from "@/lib/scroll"
import { Tabs } from "@/ui/tabs"
import { Jobs } from "./jobs"
import { Watchers } from "./watchers"

const ITEMS = AUTOMATIONS_TABS.map((t) => ({ value: t.id, label: t.label, icon: <t.icon /> }))

/**
 * The automations page.
 * @returns the page
 */
export default function Automations() {
  const [, params] = useRoute("/automations/:tab?")
  const [, navigate] = useLocation()
  const info = AUTOMATIONS_TABS.find((t) => t.id === params?.tab) ?? AUTOMATIONS_TABS[0]
  const tab: AutomationsTab = info.id
  const scroller = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  useSmoothScroll(scroller, content)
  return (
    <div ref={scroller} className="no-scrollbar h-full overflow-y-auto overscroll-contain" data-testid="automations">
      <div ref={content} className="mx-auto grid max-w-5xl gap-5 px-3 pt-[calc(var(--bar-h)+1.25rem)] pb-16 tab:px-6">
        <header className="grid gap-4">
          <div>
            <h1 className="font-serif text-4xl text-steel-50 leading-none">Automations</h1>
            <p className="mt-2 text-sm text-steel-400">{info.title}.</p>
          </div>
          <Tabs
            value={tab}
            onChange={(next) => navigate(`/automations/${next}`)}
            items={ITEMS}
            label="Automations"
            listClassName="glass rounded-card p-1"
          >
            <motion.div
              key={tab}
              className="mt-5 grid gap-4"
              initial="hidden"
              animate="shown"
              variants={{ shown: { transition: stagger(0.05) } }}
            >
              {tab === "jobs" ? <Jobs /> : <Watchers />}
            </motion.div>
          </Tabs>
        </header>
      </div>
    </div>
  )
}
