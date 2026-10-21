/**
 * His answers as Markdown (react-markdown with GitHub's tables and lists). While an answer streams in, it is split
 * into blocks at blank lines outside code fences: the finished blocks are memoised and never parsed again, and only
 * the last one, still growing, is re-rendered, so a long answer costs no more per frame than a short one. remend closes
 * whatever the last block has half-written (a `**bold`, a code fence, a link) so it doesn't flicker, and a soft caret
 * breathes at its end. An image is never fetched: the request alone would hand its address (and whatever a web page
 * he read had him write into it) to any server, so it shows as a link saying what it is and where (`imageLink`).
 */
import { ImageIcon } from "lucide-react"
import { type ComponentProps, createContext, createElement, memo, useContext } from "react"
import ReactMarkdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"
import remend from "remend"
import { imageLink } from "@/lib/format"
import { cn } from "@/lib/utils"

/** A Markdown element in the HUD's look (react-markdown's own `node` prop left out). */
const styled =
  (tag: string, className: string) =>
  ({ node: _node, ...props }: { node?: unknown }) =>
    createElement(tag, { ...props, className })

const LINK =
  "text-rigel-300 underline decoration-rigel-400/40 underline-offset-[3px] transition-colors hover:text-rigel-200 hover:decoration-rigel-300"
const InLink = createContext(false)

/**
 * A link: it opens in a new tab, which learns nothing about the HUD.
 * @param props the anchor's props
 * @returns the link
 */
function Link({ node: _node, ...props }: ComponentProps<"a"> & { node?: unknown }) {
  return (
    <InLink value>
      <a {...props} className={LINK} target="_blank" rel="noreferrer noopener" />
    </InLink>
  )
}

/**
 * An image as a link to it, with its words and its server; inside a link, or without a web address, its words alone.
 * @param props the image's props (react-markdown empties an address it distrusts)
 * @returns the link or the words
 */
function Image({ src, alt }: ComponentProps<"img">) {
  const shown = imageLink(typeof src === "string" ? src : undefined, alt, location.href, useContext(InLink))
  if ("text" in shown) return shown.text
  return (
    <a href={shown.href} title={shown.href} className={LINK} target="_blank" rel="noreferrer noopener">
      <ImageIcon aria-hidden="true" className="mr-1 inline size-3.5 align-[-2px]" />
      {shown.words} <span className="font-mono text-[0.85em] text-steel-400">{shown.host}</span>
    </a>
  )
}

const components: Components = {
  p: styled("p", "my-2.5 first:mt-0 last:mb-0"),
  ul: styled("ul", "my-2.5 list-disc pl-5 marker:text-rigel-400/70"),
  ol: styled("ol", "my-2.5 list-decimal pl-5 marker:font-mono marker:text-xs marker:text-rigel-300/80"),
  li: styled("li", "my-1 pl-1"),
  a: Link,
  img: Image,
  strong: styled("strong", "font-semibold text-steel-50"),
  em: styled("em", "text-steel-50 italic"),
  code: styled(
    "code",
    "rounded-[5px] bg-steel-50/8 px-1.5 py-0.5 font-mono text-[0.86em] text-rigel-100 [pre_&]:bg-transparent [pre_&]:p-0 [pre_&]:text-steel-100",
  ),
  pre: styled(
    "pre",
    "my-3 overflow-x-auto rounded-control bg-void-950/70 px-3.5 py-3 font-mono text-sm leading-relaxed shadow-[inset_0_0_0_1px_var(--hairline)]",
  ),
  h1: styled("h3", "mt-4 mb-2 font-semibold text-md text-steel-50 first:mt-0"),
  h2: styled("h3", "mt-4 mb-2 font-semibold text-md text-steel-50 first:mt-0"),
  h3: styled("h4", "mt-3 mb-1.5 font-semibold text-steel-100 first:mt-0"),
  blockquote: styled("blockquote", "my-3 border-rigel-400/40 border-l-2 pl-3.5 text-steel-300"),
  hr: styled("hr", "my-4 border-0 hairline-t"),
  table: styled("table", "my-3 w-full border-collapse text-sm"),
  th: styled("th", "hairline-b px-2 py-1.5 text-left font-medium text-steel-200"),
  td: styled("td", "border-steel-50/5 border-b px-2 py-1.5 align-top"),
}
const plugins = [remarkGfm]

// the caret after the last thing written (a paragraph, a list, a code block)
const CARET =
  "[&>:last-child>:last-child]:after:ml-0.5 [&>:last-child>:last-child]:after:inline-block [&>:last-child>:last-child]:after:animate-caret [&>:last-child>:last-child]:after:text-rigel-300 [&>:last-child>:last-child]:after:content-['▍']"

/**
 * Split Markdown into its blocks: at blank lines, never inside a code fence (a block ends where a fence closes too).
 * @param text the Markdown
 * @returns the blocks, in order (the last may still be growing)
 */
export function blocks(text: string): string[] {
  const out: string[] = []
  let current: string[] = []
  let fence = ""
  for (const line of text.split("\n")) {
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)?.[1]
    if (fence) {
      current.push(line)
      if (marker && marker[0] === fence[0] && marker.length >= fence.length) {
        fence = ""
        out.push(current.join("\n"))
        current = []
      }
      continue
    }
    if (marker) {
      if (current.length) out.push(current.join("\n"))
      current = [line]
      fence = marker
      continue
    }
    if (!line.trim()) {
      if (current.length) out.push(current.join("\n"))
      current = []
      continue
    }
    current.push(line)
  }
  if (current.length || fence) out.push(current.join("\n"))
  return out
}

/** One finished block: parsed once, then kept as long as its text stays the same. */
const Block = memo(function Block({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={plugins} components={components}>
      {text}
    </ReactMarkdown>
  )
})

/**
 * An answer's text as Markdown.
 * @param props the `text`, and whether it is still `streaming` in (a caret at its end, half-written syntax closed)
 * @returns the rendered text
 */
export const Markdown = memo(function Markdown({ text, streaming }: { text: string; streaming?: boolean }) {
  if (!text && streaming) return <span className="inline-block animate-caret text-rigel-300">▍</span>
  if (!streaming)
    return (
      <div className="break-words">
        <Block text={text} />
      </div>
    )
  const parts = blocks(text)
  const last = parts.pop() ?? ""
  return (
    <div className={cn("break-words", CARET)}>
      {/* lists and tables spread over blocks lose nothing: each block is whole Markdown of its own */}
      {parts.map((part, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: a block's place is its identity while the answer grows
        <div key={i} className="[&>*:last-child]:mb-2.5">
          <Block text={part} />
        </div>
      ))}
      <div>
        <ReactMarkdown remarkPlugins={plugins} components={components}>
          {remend(last, { linkMode: "text-only" })}
        </ReactMarkdown>
      </div>
    </div>
  )
})

/**
 * Markdown as one line of plain words (his captions show a reply without its marks).
 * @param text the Markdown
 * @returns the words
 */
export function plainText(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(#{1,6}|[-*+]|\d+\.|>)\s+/gm, "")
    .replace(/(\*\*|__|\*|_|~~)(.+?)\1/g, "$2")
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim()
}
