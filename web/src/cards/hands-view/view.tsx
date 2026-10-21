/**
 * The live view of your hands, as the gesture camera sees them: the camera's field framed by its corner brackets,
 * each hand drawn in your colour (Betelgeuse's orange: your words, your voice, now your hands), its bones and
 * fingertips gliding between the camera's frames, and beside it the shape it holds with what that does. A shape
 * bound to something fills a ring round the palm as it is held, in his teal, and when he acts on a gesture its
 * words rise under the hands. The fingertips leave short trails as they move, and two hands are joined by a tether
 * marked where they count as together and as apart (the tracker's own measure), lit by which they count as now. With
 * the picture switched on, what the camera sees shows dimmed behind them.
 *
 * Mounting it is what turns the view on (lib/hands.ts): frames reach the canvas outside React, about 15 a second,
 * and it draws on animation frames only while there is something to move.
 */
import { useEffect, useMemo, useRef } from "react"
import { useGestures } from "@/lib/api"
import { gestureWords, latestHands, STALE_MS, type statusOf, useHandsStatus, useHandsView } from "@/lib/hands"
import { reducedMotion } from "@/lib/motion"
import { hex, mix } from "@/lib/palette"
import type { EventOf, GestureBinding, HandView } from "@/lib/types"
import { cn } from "@/lib/utils"
import { onEvent, onHands } from "@/lib/ws"
import {
  BONES,
  type Box,
  ease,
  extend,
  fieldBox,
  holdNeeded,
  palmCentre,
  place,
  progress,
  TIPS,
  TRAIL_MS,
  type Trail,
  tether,
} from "./draw"

const EASE_MS = 20 // a drawn hand closes 63% of its way to the latest frame in this long (whatever the screen's rate)
const FADE_MS = 300 // a hand that left fades out this long
const SAID_MS = 1800 // what he did for a gesture stays under the hands this long

/** What the canvas needs to label a shape (its name and what it does), and what the remote counts by. */
interface Labels {
  title: (shape: string) => string
  binding: (shape: string) => GestureBinding | undefined
  words: (shape: string) => string
  counts: number // how long a shape is held before the tracker counts it (0: it doesn't say)
  pair: { together: number; apart: number } | null // the distances the two-hand gestures need
}

/** A hand as drawn: where it is now, easing towards where the camera last saw it, and its fingertips' trails. */
interface Drawn {
  pts: [number, number][]
  view: HandView
  seen: number // performance.now() of the frame that last showed it
  trails: Map<number, Trail>
}

/**
 * The view.
 * @param props the remote (`server`, else any), whether to show the camera's `picture`, and extra classes
 * @returns the view
 */
export function HandsView({
  server,
  picture = false,
  className,
}: {
  server?: string
  picture?: boolean
  className?: string
}) {
  useHandsView(picture)
  const canvas = useRef<HTMLCanvasElement>(null)
  const gestures = useGestures()
  const remote = gestures.data?.remotes.find((r) => !server || r.name === server)
  const labels = useRef<Labels>({
    title: gestureWords,
    binding: () => undefined,
    words: () => "",
    counts: 0,
    pair: null,
  })
  labels.current = useMemo<Labels>(() => {
    const titles = new Map(remote?.gestures.map((g) => [g.name, g.title]))
    return {
      title: (shape) => titles.get(shape) ?? gestureWords(shape),
      binding: (shape) => remote?.bindings[shape],
      words: (shape) => remote?.words[shape] ?? "",
      counts: remote?.static_hold ?? 0,
      pair: remote?.two_hands ?? null,
    }
  }, [remote])
  const status = useHandsStatus(server)
  const pictureOn = useRef(picture)
  pictureOn.current = picture

  useEffect(() => {
    const el = canvas.current
    const ctx = el?.getContext("2d")
    if (!el || !ctx) return
    const colours = {
      bone: hex("betel-400"),
      glow: hex("betel-500"),
      joint: hex("betel-200"),
      tip: hex("betel-100"),
      trail: hex("betel-300"),
      frame: mix("rigel-500", "void-950", 0.45),
      grid: mix("rigel-700", "void-950", 0.78),
      ring: hex("rigel-300"),
      ringTrack: mix("rigel-500", "void-950", 0.6),
      label: hex("steel-50"),
      muted: hex("steel-400"),
      said: hex("rigel-200"),
      chip: mix("void-950", "void-900", 0.5),
    }
    const drawn = new Map<number, Drawn>()
    const image = new Image()
    let imageSeq = -1
    let said: { text: string; ok: boolean; at: number } | null = null
    let frame = 0
    let painted = 0 // performance.now() of the last paint (0: none yet)
    let size = { w: 0, h: 0, ratio: 1 }

    const resize = () => {
      const box = el.getBoundingClientRect()
      const ratio = Math.min(2, devicePixelRatio || 1)
      size = { w: box.width, h: box.height, ratio }
      el.width = Math.max(1, Math.round(box.width * ratio))
      el.height = Math.max(1, Math.round(box.height * ratio))
      schedule()
    }

    const take = () => {
      const got = latestHands(server)
      if (!got) return
      const { frame: f } = got
      for (const hand of f.hands) {
        const before = drawn.get(hand.track)
        drawn.set(hand.track, {
          pts: before?.pts ?? hand.pts.map(([x, y]) => [x, y]),
          view: hand,
          seen: got.at,
          trails: before?.trails ?? new Map(),
        })
      }
      const picture = f.jpg_seq ?? f.seq
      if (pictureOn.current && f.jpg && picture !== imageSeq) {
        imageSeq = picture // each picture decoded once
        image.src = `data:image/jpeg;base64,${f.jpg}`
      }
    }

    const paint = () => {
      frame = 0
      const now = performance.now()
      const k = 1 - Math.exp(-Math.min(100, now - (painted || now - 16)) / EASE_MS) // this paint's share of the way
      painted = now
      const { w, h, ratio } = size
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
      ctx.clearRect(0, 0, w, h)
      const got = latestHands(server)
      const f = got?.frame
      const box = fieldBox(w, h, f?.w && f?.h ? f.w / f.h : 4 / 3, 6)
      drawField(ctx, box, colours)
      if (pictureOn.current && image.complete && image.naturalWidth && got && now - got.at < STALE_MS) {
        ctx.save()
        ctx.globalAlpha = 0.55
        ctx.drawImage(image, box.x, box.y, box.w, box.h)
        ctx.restore()
      }
      const still = reducedMotion()
      let moving = false
      for (const [track, hand] of drawn) {
        const age = now - hand.seen
        const gone = !f?.hands.some((x) => x.track === track) || (got && now - got.at > STALE_MS)
        if (gone && age > FADE_MS) {
          drawn.delete(track)
          continue
        }
        const target = hand.view.pts
        const next = still ? target.map(([x, y]) => [x, y] as [number, number]) : ease(hand.pts, target, k)
        if (
          next.some(([x, y], i) => Math.abs(x - (target[i]?.[0] ?? x)) + Math.abs(y - (target[i]?.[1] ?? y)) > 0.0005)
        )
          moving = true
        hand.pts = next
        const alpha = gone ? Math.max(0, 1 - age / FADE_MS) : 1
        if (gone) moving = true
        if (!still) {
          for (const tip of TIPS) {
            const trail = hand.trails.get(tip) ?? []
            const at = next[tip]
            // a hand that left only lets its trails fade
            hand.trails.set(tip, gone || !at ? trail.filter((p) => now - p.at < TRAIL_MS) : extend(trail, at, now))
          }
          if ([...hand.trails.values()].some((trail) => trail.length > 1)) moving = true // a trail still fading
          drawTrails(ctx, hand.trails, box, alpha, now, colours)
        }
        drawHand(ctx, hand, box, alpha, labels.current, colours)
      }
      const pair = labels.current.pair
      const both = [...drawn.entries()].filter(([track]) => f?.hands.some((x) => x.track === track))
      if (pair && both.length === 2 && typeof f?.apart === "number") {
        const [a, b] = both.map(([, hand]) => palmCentre(hand.pts.map((pt) => place(pt, box))))
        if (a && b) drawTether(ctx, a, b, f.apart, pair, box, colours)
      }
      if (said) {
        const left = SAID_MS - (now - said.at)
        if (left > 0) {
          drawSaid(ctx, box, said.text, said.ok, Math.min(1, left / 400), colours)
          moving = true
        } else said = null
      }
      if (moving || (got && now - got.at < STALE_MS)) schedule()
    }

    function schedule() {
      if (!frame) frame = requestAnimationFrame(paint)
    }

    const offFrames = onHands((ev) => {
      if (server && ev.server !== server) return
      queueMicrotask(() => {
        take()
        schedule()
      })
    })
    const offSaid = onEvent("intent", (ev: EventOf<"intent">) => {
      if (server && ev.source !== server) return
      const text = ev.text ?? ""
      if (!text || ev.again) return // a repeat (a thumb still up) doesn't rise again: the chip counts it
      said = { text, ok: ev.ok !== false, at: performance.now() }
      schedule()
    })
    image.onload = schedule
    const observer = new ResizeObserver(resize)
    observer.observe(el)
    resize()
    take()
    return () => {
      observer.disconnect()
      offFrames()
      offSaid()
      if (frame) cancelAnimationFrame(frame)
    }
  }, [server])

  const words = statusWords(status.state, status.error)
  return (
    <div className={cn("relative min-h-0", className)} data-testid="hands-view" data-state={status.state}>
      <canvas ref={canvas} className="absolute inset-0 size-full" aria-label="Your hands, as the camera sees them" />
      {words && (
        <p
          role="status"
          className="pointer-events-none absolute inset-x-6 top-1/2 -translate-y-1/2 text-center text-small text-steel-300"
        >
          {words}
        </p>
      )}
    </div>
  )
}

/**
 * What the view says over the field while there are no hands to draw.
 * @param state the view's state
 * @param error why the link is down
 * @returns the words, or "" for none
 */
export function statusWords(state: ReturnType<typeof statusOf>["state"], error = ""): string {
  switch (state) {
    case "waiting":
      return "Waking the camera…"
    case "down":
      return error ? `No link to the camera: ${error}` : "No link to the camera."
    case "paused":
      return "Gestures are paused."
    case "stale":
      return "No signal from the camera."
    default:
      return ""
  }
}

type Colours = Record<
  | "bone"
  | "glow"
  | "joint"
  | "tip"
  | "trail"
  | "frame"
  | "grid"
  | "ring"
  | "ringTrack"
  | "label"
  | "muted"
  | "said"
  | "chip",
  string
>

/** The camera's field: a faint grid and its corner brackets. */
function drawField(ctx: CanvasRenderingContext2D, box: Box, c: Colours) {
  ctx.save()
  ctx.strokeStyle = c.grid
  ctx.lineWidth = 1
  ctx.beginPath()
  for (let i = 1; i < 4; i++) {
    const x = Math.round(box.x + (box.w * i) / 4) + 0.5
    const y = Math.round(box.y + (box.h * i) / 4) + 0.5
    ctx.moveTo(x, box.y)
    ctx.lineTo(x, box.y + box.h)
    ctx.moveTo(box.x, y)
    ctx.lineTo(box.x + box.w, y)
  }
  ctx.stroke()
  const arm = Math.min(18, box.w / 8)
  ctx.strokeStyle = c.frame
  ctx.lineWidth = 1.5
  ctx.beginPath()
  for (const [x, y, dx, dy] of [
    [box.x, box.y, 1, 1],
    [box.x + box.w, box.y, -1, 1],
    [box.x, box.y + box.h, 1, -1],
    [box.x + box.w, box.y + box.h, -1, -1],
  ] as const) {
    ctx.moveTo(x + dx * arm, y)
    ctx.lineTo(x, y)
    ctx.lineTo(x, y + dy * arm)
  }
  ctx.stroke()
  ctx.restore()
}

/** A hand's fingertip trails: short strokes fading behind each tip, thinner and fainter the older they are. */
function drawTrails(
  ctx: CanvasRenderingContext2D,
  trails: Map<number, Trail>,
  box: Box,
  alpha: number,
  now: number,
  c: Colours,
) {
  const scale = Math.max(0.6, Math.min(1.6, box.w / 420))
  ctx.save()
  ctx.lineCap = "round"
  ctx.strokeStyle = c.trail
  for (const trail of trails.values()) {
    for (let i = 1; i < trail.length; i++) {
      const p = trail[i - 1] as Trail[number]
      const q = trail[i] as Trail[number]
      const fresh = 1 - (now - q.at) / TRAIL_MS
      if (fresh <= 0) continue
      const [x0, y0] = place([p.x, p.y], box)
      const [x1, y1] = place([q.x, q.y], box)
      ctx.globalAlpha = alpha * fresh * 0.55
      ctx.lineWidth = (0.6 + 2 * fresh) * scale
      ctx.beginPath()
      ctx.moveTo(x0, y0)
      ctx.lineTo(x1, y1)
      ctx.stroke()
    }
  }
  ctx.restore()
}

/** The tether between two palms: a dashed line, a tick where they count as together and where as apart, lit by which
 *  they count as now. */
function drawTether(
  ctx: CanvasRenderingContext2D,
  a: [number, number],
  b: [number, number],
  apart: number,
  counts: { together: number; apart: number },
  box: Box,
  c: Colours,
) {
  const line = tether(a, b, apart, counts)
  if (!line) return
  const scale = Math.max(0.6, Math.min(1.6, box.w / 420))
  const lit = line.state === "between" ? c.muted : line.state === "together" ? c.ring : c.trail
  ctx.save()
  ctx.strokeStyle = lit
  ctx.globalAlpha = line.state === "between" ? 0.55 : 0.9
  ctx.lineWidth = 1.5 * scale
  ctx.setLineDash([5 * scale, 5 * scale])
  ctx.beginPath()
  ctx.moveTo(a[0], a[1])
  ctx.lineTo(b[0], b[1])
  ctx.stroke()
  ctx.setLineDash([])
  const [nx, ny] = [-line.dir[1], line.dir[0]] // across the line
  for (const [marks, length, colour] of [
    [line.together, 6, c.ring],
    [line.apart, 9, c.trail],
  ] as const) {
    ctx.strokeStyle = colour
    ctx.globalAlpha = 0.8
    ctx.beginPath()
    for (const [x, y] of marks) {
      if (x < box.x || x > box.x + box.w || y < box.y || y > box.y + box.h) continue
      ctx.moveTo(x - nx * length * scale, y - ny * length * scale)
      ctx.lineTo(x + nx * length * scale, y + ny * length * scale)
    }
    ctx.stroke()
  }
  ctx.restore()
}

/** One hand: its bones and joints, its hold ring, and its label. */
function drawHand(ctx: CanvasRenderingContext2D, hand: Drawn, box: Box, alpha: number, labels: Labels, c: Colours) {
  const pts = hand.pts.map((p) => place(p, box))
  const scale = Math.max(0.6, Math.min(1.6, box.w / 420))
  ctx.save()
  ctx.globalAlpha = alpha
  ctx.lineCap = "round"
  ctx.lineJoin = "round"
  ctx.shadowColor = c.glow
  ctx.shadowBlur = 10 * scale
  ctx.strokeStyle = c.bone
  ctx.lineWidth = 2.4 * scale
  ctx.beginPath()
  for (const [a, b] of BONES) {
    const pa = pts[a]
    const pb = pts[b]
    if (!pa || !pb) continue
    ctx.moveTo(pa[0], pa[1])
    ctx.lineTo(pb[0], pb[1])
  }
  ctx.stroke()
  ctx.shadowBlur = 0
  pts.forEach(([x, y], i) => {
    ctx.fillStyle = TIPS.has(i) ? c.tip : c.joint
    ctx.beginPath()
    ctx.arc(x, y, (TIPS.has(i) ? 3.2 : 2.2) * scale, 0, Math.PI * 2)
    ctx.fill()
  })
  const shape = hand.view.shape
  const [cx, cy] = palmCentre(pts)
  if (shape) {
    const binding = labels.binding(shape)
    const needed = holdNeeded(binding, labels.counts)
    const done = progress(hand.view.held, needed)
    if (needed !== null) {
      const radius = 22 * scale
      ctx.lineWidth = 2.5 * scale
      ctx.strokeStyle = c.ringTrack
      ctx.beginPath()
      ctx.arc(cx, cy, radius, 0, Math.PI * 2)
      ctx.stroke()
      ctx.strokeStyle = c.ring
      ctx.shadowColor = c.ring
      ctx.shadowBlur = done >= 1 ? 12 * scale : 0
      ctx.beginPath()
      ctx.arc(cx, cy, radius, -Math.PI / 2, -Math.PI / 2 + done * Math.PI * 2)
      ctx.stroke()
      ctx.shadowBlur = 0
    }
    const words = labels.words(shape)
    drawLabel(
      ctx,
      labels.title(shape).toUpperCase(),
      words ? `→ ${words}` : "not bound",
      box,
      pts,
      scale,
      Boolean(words),
      c,
    )
  } else {
    drawLabel(ctx, hand.view.hand === "Left" ? "LEFT HAND" : "RIGHT HAND", "", box, pts, scale, false, c)
  }
  ctx.restore()
}

/** A hand's label: its shape and what it does, in a small dark chip above the hand (kept inside the field). */
function drawLabel(
  ctx: CanvasRenderingContext2D,
  title: string,
  detail: string,
  box: Box,
  pts: [number, number][],
  scale: number,
  bound: boolean,
  c: Colours,
) {
  const top = Math.min(...pts.map(([, y]) => y))
  const left = Math.min(...pts.map(([x]) => x))
  const size = Math.round(10.5 * Math.min(scale, 1.2))
  ctx.font = `600 ${size}px "Geist Variable", system-ui, sans-serif`
  const titleW = ctx.measureText(title).width
  ctx.font = `${size}px "Geist Mono Variable", monospace`
  const detailW = detail ? ctx.measureText(detail).width : 0
  const w = Math.max(titleW, detailW) + 14
  const h = detail ? size * 2 + 12 : size + 10
  const x = Math.min(Math.max(box.x + 2, left), box.x + box.w - w - 2)
  const y = Math.max(box.y + 2, top - h - 8)
  ctx.fillStyle = c.chip
  ctx.globalAlpha *= 0.85
  ctx.fillRect(x, y, w, h)
  ctx.globalAlpha /= 0.85
  ctx.fillStyle = c.label
  ctx.textBaseline = "top"
  ctx.font = `600 ${size}px "Geist Variable", system-ui, sans-serif`
  ctx.fillText(title, x + 7, y + 5)
  if (detail) {
    ctx.fillStyle = bound ? c.said : c.muted
    ctx.font = `${size}px "Geist Mono Variable", monospace`
    ctx.fillText(detail, x + 7, y + size + 8)
  }
}

/** What he just did for a gesture, under the hands, fading. */
function drawSaid(ctx: CanvasRenderingContext2D, box: Box, text: string, ok: boolean, alpha: number, c: Colours) {
  ctx.save()
  ctx.globalAlpha = alpha
  const size = Math.round(Math.max(11, Math.min(15, box.w / 34)))
  ctx.font = `600 ${size}px "Geist Variable", system-ui, sans-serif`
  ctx.textAlign = "center"
  ctx.textBaseline = "bottom"
  ctx.fillStyle = ok ? c.said : c.muted
  ctx.shadowColor = c.ring
  ctx.shadowBlur = ok ? 8 : 0
  ctx.fillText(text.toUpperCase(), box.x + box.w / 2, box.y + box.h - 10)
  ctx.restore()
}
