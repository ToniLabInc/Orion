// Smooth scrolling for the long pages (settings, automations) with Lenis, in wrapper mode: the page's own scroller,
// never the conversation (which must stick to its bottom crisply). Lenis is driven by hand: a frame loop starts with a
// wheel or a touch and stops as soon as the scroll has come to rest, so nothing runs while nothing scrolls. Under
// reduced motion it isn't used at all: the browser's own scrolling.
import Lenis from "lenis"
import { type RefObject, useEffect } from "react"
import { reducedMotion } from "./motion"

/**
 * Scroll an element smoothly while it is mounted.
 * @param wrapper the scrolling element
 * @param content its content (its direct child)
 */
export function useSmoothScroll(wrapper: RefObject<HTMLElement | null>, content: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const el = wrapper.current
    const inner = content.current
    if (!el || !inner || reducedMotion()) return
    const lenis = new Lenis({ wrapper: el, content: inner, autoRaf: false, smoothWheel: true, lerp: 0.12 })
    let frame = 0
    const tick = (time: number) => {
      lenis.raf(time)
      frame = lenis.isScrolling ? requestAnimationFrame(tick) : 0
    }
    const kick = () => {
      if (!frame) frame = requestAnimationFrame(tick)
    }
    const events = ["wheel", "touchstart", "keydown"] as const
    for (const event of events) el.addEventListener(event, kick, { passive: true })
    return () => {
      for (const event of events) el.removeEventListener(event, kick)
      cancelAnimationFrame(frame)
      lenis.destroy()
    }
  }, [wrapper, content])
}
