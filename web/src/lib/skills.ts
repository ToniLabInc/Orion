// A skill's SKILL.md as the editor needs it, kept pure for the tests: the Agent Skills format's rule for a name (the
// folder's name too), the name a file gives itself, and the template a new skill starts from.
// See https://agentskills.io/specification.

/** The longest a skill's name may be. */
export const SKILL_NAME_MAX = 64

/** Lower-case letters and digits, joined by single hyphens: none at either end, never two in a row. */
const NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

/** What a new skill starts from. */
export const SKILL_TEMPLATE = `---
name: my-routine
description: One sentence: what it does, and when to use it.
allowed-tools: volume open
metadata:
  triggers: my routine, start my routine
---

1. Call volume with action "set" and level 30.
2. Call open with target "youtube.com".
3. Tell the user it's ready, in one short sentence.
`

/**
 * The name a SKILL.md gives itself: the `name:` line of its front matter.
 * @param content the SKILL.md
 * @returns the name as written, "" for none
 */
export function skillNameOf(content: string): string {
  const front = /^---\r?\n([\s\S]*?)\r?\n---/.exec(content)?.[1] ?? ""
  const line = /^name:[ \t]*(.*)$/m.exec(front)?.[1] ?? ""
  return line
    .trim()
    .replace(/^(["'])(.*)\1$/, "$2")
    .trim()
}

/**
 * Why a name isn't one, in words.
 * @param name the name
 * @returns the problem, "" when it is a name
 */
export function skillNameProblem(name: string): string {
  if (!name) return "Give it a name, on the second line."
  if (name.length > SKILL_NAME_MAX) return `A name of at most ${SKILL_NAME_MAX} characters.`
  if (!NAME.test(name)) return "A name of lower-case letters and digits, joined by single hyphens."
  return ""
}

/**
 * What stops a SKILL.md from being saved.
 * @param content the text
 * @param existing the name of the skill being edited (none for a new one: its name is its own)
 * @param taken the names of the other skills
 * @returns the problem, "" when it can be saved
 */
export function skillProblem(content: string, existing: string | undefined, taken: readonly string[]): string {
  const name = skillNameOf(content)
  const bad = skillNameProblem(name)
  if (bad) return bad
  if (existing !== undefined) return name === existing ? "" : `The name stays ${existing}: it is the folder's name.`
  return taken.includes(name) ? `There is already a skill called ${name}.` : ""
}
