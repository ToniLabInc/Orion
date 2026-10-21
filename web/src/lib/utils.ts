/**
 * `cn`: join class names and resolve Tailwind conflicts so that the later class wins (clsx and tailwind-merge in one,
 * from the `cn` package). It runs on tables that cn's own CLI compiles from styles.css (`npm run tables`, run again
 * before every build), so it knows the HUD's type scale: on the stock tables `text-micro` reads as a colour, and
 * `cn("text-micro", "text-steel-400")` silently drops the size. Import `cn` from here, never from the package.
 */
import { createCn } from "cn/engine"
import tables from "./cn-tables"

/**
 * Join class names (strings, arrays and objects of flags; falsy values are skipped) and keep only the last class of
 * each conflicting group, so a `className` passed to a component overrides its defaults.
 */
export const cn = createCn(tables)
