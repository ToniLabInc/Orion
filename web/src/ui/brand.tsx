/**
 * ORION's brand: the mark (his constellation: Betelgeuse and Bellatrix at the shoulders, the belt, Saiph and Rigel at
 * the feet, joined by the hunter's lines) and the wordmark (Geist 600, tracked wide). Betelgeuse keeps its orange: the
 * one warm star, yours.
 */
import { cn } from "@/lib/utils"

/** The mark's lines and stars in a 32×32 view box. */
export const MARK = {
  lines: "M9.6 7.4 13.3 17.6M22.2 8.8 18.7 15.4M13.3 17.6 11.2 25.4M18.7 15.4 23.3 24.4M13.3 17.6 18.7 15.4",
  stars: [
    { x: 9.6, y: 7.4, r: 1.9, warm: true }, // Betelgeuse
    { x: 22.2, y: 8.8, r: 1.3 }, // Bellatrix
    { x: 13.3, y: 17.6, r: 1.2 }, // Alnitak
    { x: 16, y: 16.5, r: 1.2 }, // Alnilam
    { x: 18.7, y: 15.4, r: 1.2 }, // Mintaka
    { x: 11.2, y: 25.4, r: 1.25 }, // Saiph
    { x: 23.3, y: 24.4, r: 1.7 }, // Rigel
  ],
} as const

/**
 * The mark.
 * @param props size and colour classes (it draws in the current colour; Betelgeuse in your orange unless `mono`)
 * @returns the mark, hidden from screen readers
 */
export function Mark({ className, mono = false }: { className?: string; mono?: boolean }) {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true" className={cn("size-6 shrink-0", className)}>
      <path d={MARK.lines} fill="none" stroke="currentColor" strokeWidth={1} strokeLinecap="round" opacity={0.55} />
      {MARK.stars.map((s) => (
        <circle
          key={`${s.x}-${s.y}`}
          cx={s.x}
          cy={s.y}
          r={s.r}
          fill={!mono && "warm" in s ? "var(--color-betel-400)" : "currentColor"}
        />
      ))}
    </svg>
  )
}

/**
 * The wordmark.
 * @param props classes
 * @returns ORION in tracked capitals
 */
export function Wordmark({ className }: { className?: string }) {
  return <span className={cn("font-semibold text-xs tracking-[0.42em] text-steel-50", className)}>ORION</span>
}
