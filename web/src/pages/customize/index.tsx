/**
 * Customize: what he is made of, in the three parts Claude's own Customize page has. Connectors (everything that gives
 * him tools: his own, the services you connect, what plugins bring), Skills (routines he follows) and Plugins (Claude
 * plugins whose skills, commands and servers he takes up). Each has the things you have ("Yours") and a directory to
 * find more ("Discover"), and each thing has a page of its own with its own settings (a connector's, here, not in
 * Orion's Settings). The address says where you are: /customize/connectors, /customize/connectors/<id>/settings,
 * /customize/skills/<name>, /customize/plugins/<name>/connectors.
 */
import { AnimatePresence, motion } from "motion/react"
import { useRoute } from "wouter"
import { useConnectors, usePlugins, useSkills } from "@/lib/api"
import { DUR, EXPO, stagger } from "@/lib/motion"
import { CUSTOMIZE_TABS, isCustomizeTab } from "@/lib/nav"
import { Frame } from "../frame"
import { ConnectorPage } from "./connector"
import { Connectors } from "./connectors"
import { decoded } from "./parts"
import { PluginPage, Plugins } from "./plugins"
import { SkillPage, Skills } from "./skills"

/**
 * The customize page.
 * @returns the page
 */
export default function Customize() {
  const [, params] = useRoute("/customize/:tab?/:id?/:section?")
  const tab = isCustomizeTab(params?.tab) ? params.tab : "connectors"
  const id = params?.id ? decoded(params.id) : undefined
  const counts = {
    connectors: useConnectors().data?.length,
    skills: useSkills().data?.length,
    plugins: usePlugins().data?.length,
  }
  const items = CUSTOMIZE_TABS.map((t) => ({
    href: `/customize/${t.id}`,
    label: t.label,
    icon: t.icon,
    count: counts[t.id],
  }))
  return (
    <Frame
      title="Customize"
      note="What he is made of: connectors, skills and plugins."
      items={items}
      active={`/customize/${tab}`}
      testId="customize"
      resetKey={`${tab}/${id ?? ""}`}
      wide
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={`${tab}/${id ?? ""}`}
          className="grid grid-cols-1 gap-4"
          initial="hidden"
          animate="shown"
          exit={{ opacity: 0, y: -6, transition: { duration: DUR.quick, ease: [...EXPO] } }}
          variants={{ shown: { transition: stagger(0.05) } }}
          data-testid={`customize-${tab}`}
        >
          {tab === "connectors" ? (
            id ? (
              <ConnectorPage id={id} section={params?.section} />
            ) : (
              <Connectors />
            )
          ) : tab === "skills" ? (
            id ? (
              <SkillPage name={id} />
            ) : (
              <Skills />
            )
          ) : id ? (
            <PluginPage name={id} section={params?.section} />
          ) : (
            <Plugins />
          )}
        </motion.div>
      </AnimatePresence>
    </Frame>
  )
}
