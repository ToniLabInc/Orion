// How the command palette ranks what you type against its commands: a label that starts with your words first, then
// one with a word that does, then one that holds them anywhere, then its keywords; every word you type must be found.
// Pure, for the tests.

export interface Rankable {
  label: string
  keywords?: string
}

/**
 * How well a command matches what was typed.
 * @param item the command
 * @param query what was typed
 * @returns 0 for no match, higher for a better one
 */
export function score(item: Rankable, query: string): number {
  const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean)
  if (!words.length) return 1
  const label = item.label.toLowerCase()
  const keywords = (item.keywords ?? "").toLowerCase()
  let total = 0
  for (const word of words) {
    if (label.startsWith(word)) total += 8
    else if (new RegExp(`\\b${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`).test(label)) total += 5
    else if (label.includes(word)) total += 3
    else if (keywords.includes(word)) total += 1
    else return 0
  }
  return total
}

/**
 * The commands that match, best first (the palette's order kept among equals).
 * @param items the commands
 * @param query what was typed
 * @returns the matches
 */
export function rank<T extends Rankable>(items: T[], query: string): T[] {
  return scored(items, query).map((x) => x.item)
}

/**
 * The commands that match with their scores, best first (the palette's order kept among equals).
 * @param items the commands
 * @param query what was typed
 * @returns the matches and their scores
 */
function scored<T extends Rankable>(items: T[], query: string): { item: T; s: number }[] {
  return items
    .map((item, i) => ({ item, i, s: score(item, query) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s || a.i - b.i)
}

/**
 * The palette's groups for what was typed: each with its matches best first, and the group with the best match first
 * (the palette's order kept among equals), so "we" puts Weather above an action whose keywords hold "we".
 * @param groups the groups
 * @param query what was typed
 * @returns the groups that match
 */
export function rankGroups<T extends Rankable, G extends { items: T[] }>(groups: G[], query: string): G[] {
  return groups
    .map((group, i) => {
      const matches = scored(group.items, query)
      return { group: { ...group, items: matches.map((x) => x.item) }, i, best: matches[0]?.s ?? 0 }
    })
    .filter((x) => x.best > 0)
    .sort((a, b) => b.best - a.best || a.i - b.i)
    .map((x) => x.group)
}
