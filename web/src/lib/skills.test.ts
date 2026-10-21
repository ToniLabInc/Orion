import { describe, expect, it } from "vitest"
import { SKILL_NAME_MAX, SKILL_TEMPLATE, skillNameOf, skillNameProblem, skillProblem } from "./skills"

describe("the name a SKILL.md gives itself", () => {
  it("is the name: line of its front matter, quotes and spaces taken off", () => {
    expect(skillNameOf("---\nname: movie-night\ndescription: x\n---\nbody")).toBe("movie-night")
    expect(skillNameOf('---\nname:   "pc-checkup"  \n---\n')).toBe("pc-checkup")
    expect(skillNameOf("---\r\nname: crlf-skill\r\n---\r\n")).toBe("crlf-skill")
  })
  it("is nothing without front matter, or when only the body says name:", () => {
    expect(skillNameOf("name: not-front-matter")).toBe("")
    expect(skillNameOf("---\ndescription: x\n---\nname: in-the-body")).toBe("")
    expect(skillNameOf("")).toBe("")
  })
  it("ignores a nested name: such as metadata's", () => {
    expect(skillNameOf("---\nmetadata:\n  name: nested\nname: outer\n---\n")).toBe("outer")
    expect(skillNameOf("---\nmetadata:\n  name: nested\n---\n")).toBe("")
  })
})

describe("what makes a name", () => {
  it("is lower-case letters and digits joined by single hyphens, 64 characters at most", () => {
    for (const name of ["a", "movie-night", "pdf2", "a-b-c-1"]) expect(skillNameProblem(name)).toBe("")
    for (const name of ["Movie", "two--hyphens", "-lead", "trail-", "under_score", "spa ce"])
      expect(skillNameProblem(name)).toMatch(/lower-case/)
    expect(skillNameProblem("")).toMatch(/Give it a name/)
    expect(skillNameProblem("a".repeat(SKILL_NAME_MAX))).toBe("")
    expect(skillNameProblem("a".repeat(SKILL_NAME_MAX + 1))).toMatch(/64/)
  })
})

describe("what stops a SKILL.md from being saved", () => {
  const file = (name: string) => `---\nname: ${name}\ndescription: x\n---\n`
  it("lets the template through for a new skill, and refuses a name another skill has", () => {
    expect(skillNameOf(SKILL_TEMPLATE)).toBe("my-routine")
    expect(skillProblem(SKILL_TEMPLATE, undefined, [])).toBe("")
    expect(skillProblem(file("pdf"), undefined, ["pdf", "docx"])).toMatch(/already a skill called pdf/)
  })
  it("keeps the name of a skill that exists: the folder's", () => {
    expect(skillProblem(file("pdf"), "pdf", ["pdf"])).toBe("")
    expect(skillProblem(file("pdf2"), "pdf", ["pdf"])).toMatch(/stays pdf/)
    expect(skillProblem("no front matter", "pdf", [])).toMatch(/Give it a name/)
  })
})
