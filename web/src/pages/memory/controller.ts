/**
 * What the neural map does by itself, beside React: the forces, the camera with its fits and flights, the frame loop,
 * the links' colours and the pulses that run along them, the sparks, the flashes, the dust, and which names show.
 *
 * It wakes with whatever happens (you move the camera or point at a node, his state changes, he learns something):
 * the view drifts round, every star breathes and turns its glyph, and sparks fire along the links like synapses, as his
 * state says (now and then on standby; outward from you while he listens; from him while he speaks; everywhere, fast,
 * while he thinks). 30 frames a second (60 while you move the camera), and a few seconds after the last thing that
 * happened it rests: nothing is drawn until the next. None in a hidden tab.
 *
 * Links rest in his state's tint, quieter towards the two hubs. A pulse lights a link and lets it go: it rises in
 * 90 ms and falls away over 510 ms. Selecting a node sends pulses out through its neighbourhood a hop at a time; a
 * memory he has just learned gets one from its hub out to it, and flashes when it arrives. With reduced motion there
 * are no pulses or sparks, and the flash is a steady glow.
 */
import type { ForceGraphMethods } from "react-force-graph-3d"
import * as THREE from "three"
import type { OrbitControls } from "three/addons/controls/OrbitControls.js"
import { mix, num } from "@/lib/palette"
import type { ShownState } from "@/lib/types"
import { isHub, type Kind, linkBetween, type MapGraph, type MapLink, type MapNode, pathFromHub, pinAll } from "./graph"
import {
  asLight,
  fade,
  glow,
  glowMap,
  type Look,
  linkView,
  live,
  type NodeView,
  paint,
  shade,
  update,
  viewOf,
} from "./nodes"

const IDLE = 6000 // ms the view drifts on after the last touch, then rests
const DRIFT = 0.3 // the slow turn while awake: a whole turn in about three minutes
const FLASH = 1800 // ms a node he just learned swells and settles (its name shows twice as long)
const HOP = 260 // ms between the hops of a pulse
const RISE = 90 // ms a pulse takes to light a link
const FALL = 510 // ms it takes to let go of it again
const PEAK = "rigel-200" // a link at the height of a pulse
const MAX_DPR = 1.5
const SPARKS = 64 // sparks in flight at most
const DUST = 1400 // specks of dust round the graph

/** How often a spark fires (a second), how long it takes along its link (ms), its colour, and where it comes from:
 *  anywhere, or out of one of the hubs' neighbourhoods. */
const ACTIVITY: Record<ShownState, { rate: number; ms: number; token: string; from: "you" | "orion" | null }> = {
  idle: { rate: 1.4, ms: 1300, token: "rigel-200", from: null },
  listening: { rate: 4.5, ms: 900, token: "betel-200", from: "you" },
  thinking: { rate: 9, ms: 700, token: "nebula-200", from: null },
  speaking: { rate: 5, ms: 900, token: "aurora-200", from: "orion" },
  loading: { rate: 0.8, ms: 1400, token: "dusk-300", from: null },
  offline: { rate: 0.2, ms: 1800, token: "steel-500", from: null },
}

/** A spark on its way along a link, from one end to the other. */
interface Spark {
  from: MapNode
  to: MapNode
  start: number
  ms: number
  color: THREE.Color
}

/** What lies over the map, in px from its edges: the page's search and legend, and with `panel` the selected node's
 *  panel. The view is fitted and centred in what they leave uncovered. */
export type Cover = (width: number, height: number, panel: boolean) => { top: number; right: number; bottom: number }

export type Graph = ForceGraphMethods<MapNode, MapLink>

/** The camera as you left it, for your next visit (the nodes keep their places in the page's graph). */
let kept: { position: THREE.Vector3; target: THREE.Vector3 } | undefined

/**
 * The library's object for a node, once it has made it.
 * @param n the node
 * @returns its object
 */
const objectOf = (n: MapNode | undefined) => (n as (MapNode & { __threeObj?: THREE.Object3D }) | undefined)?.__threeObj

/**
 * A link end's id, whether the layout has swapped it for its node yet or not.
 * @param end the id or the node
 * @returns the id
 */
export const endId = (end: string | MapNode) => (typeof end === "string" ? end : end.id)

/** What the library's callbacks and the frame loop read: always the latest render's. */
export interface State {
  graph: MapGraph
  selected: string | null
  focus: Map<string, number> | null // the selected node's neighbourhood, by hop
  named: Set<string> // whose names show
  hidden: ReadonlySet<Kind>
  still: boolean // reduced motion
  mood: ShownState // his state: how the sparks fire
  tint: string // his state's tint (a palette token): the links rest in it
  hovered: string | null
  flashes: Map<string, number> // node → when its flash begins (0 until it is drawn)
  pulses: Map<MapLink, number> // link → when its pulse sets out
  cover: Cover
  onSelect: (id: string | null) => void
}

/**
 * Whether a node's name shows: one of the names of the moment (outside the selected neighbourhood only the hubs'), the
 * node under the pointer, or one he just learned.
 * @param s the state
 * @param n the node
 * @returns true when its name shows
 */
const named = (s: State, n: MapNode) =>
  (s.named.has(n.id) && (!s.focus || s.focus.has(n.id) || isHub(n.kind))) || n.id === s.hovered || s.flashes.has(n.id)

/**
 * How a node is painted now.
 * @param s the state
 * @param n the node
 * @returns its look
 */
export const lookOf = (s: State, n: MapNode): Look => ({
  selected: n.id === s.selected,
  dim: Boolean(s.focus && !s.focus.has(n.id)),
  label: named(s, n),
})

/** The colours a link rests at in one of his states. */
interface LinkLook {
  hub: THREE.Color // to one of the hubs, which everything joins: quieter
  link: THREE.Color // between two memories, or a memory and a concept
  near: THREE.Color // inside the selected node's neighbourhood
  far: THREE.Color // beyond it
}

const linkLooks = new Map<string, LinkLook>()

/**
 * The link colours for a tint, mixed once (towards the page's colour, so a link fades without being see-through).
 * @param tint his state's tint
 * @returns the colours
 */
function linkLook(tint: string): LinkLook {
  let look = linkLooks.get(tint)
  if (!look) {
    look = {
      hub: new THREE.Color(mix(tint, "void-950", 0.72)),
      link: new THREE.Color(mix(tint, "void-950", 0.55)),
      near: shade(tint),
      far: new THREE.Color(mix(tint, "void-950", 0.9)),
    }
    linkLooks.set(tint, look)
  }
  return look
}

/**
 * Set the colour a link rests at, and show it unless a pulse is passing along it (the next frame shows that).
 * @param s the state
 * @param l the link
 * @returns the link's material, for the library
 */
export function paintLink(s: State, l: MapLink): THREE.LineBasicMaterial {
  const view = linkView(l)
  const look = linkLook(s.tint)
  const [a, b] = [endId(l.source), endId(l.target)]
  const hub = (id: string) => isHub(s.graph.byId.get(id)?.kind ?? "concept")
  view.rest.copy(
    s.focus ? (s.focus.has(a) && s.focus.has(b) ? look.near : look.far) : hub(a) || hub(b) ? look.hub : look.link,
  )
  if (!s.pulses.has(l)) view.material.color.copy(view.rest)
  return view.material
}

/**
 * How lit a link is by a pulse.
 * @param start when the pulse set out (performance.now())
 * @param now the time now
 * @returns 0 (resting) to 1 (at its height)
 */
function level(start: number, now: number) {
  const t = now - start
  if (t <= 0 || t >= RISE + FALL) return 0
  return t < RISE ? t / RISE : (1 - (t - RISE) / FALL) ** 2
}

/**
 * Which names win where they would overlap: the selected node's, the one under the pointer, a new one, the hubs, then
 * the better connected.
 * @param s the state
 * @param n the node
 * @returns its rank, lower first
 */
const rank = (s: State, n: MapNode) =>
  (n.id === s.selected ? 0 : n.id === s.hovered ? 1 : s.flashes.has(n.id) ? 2 : isHub(n.kind) ? 3 : 4) * 1000 - n.degree

/**
 * About how wide a name is drawn (Oxanium at 11 px, the hubs' in spaced capitals), without asking the page.
 * @param n the node
 * @returns its width in px
 */
const labelWidth = (n: MapNode) => Math.min(240, n.label.length * (isHub(n.kind) ? 8.8 : 6.2) + 18)

/** One per scene: map.tsx makes it once the library has started, and disposes of it with the scene. */
export class Controller {
  private until = 0 // stay awake until then (performance.now())
  private fast = 0 // and draw 60 frames a second until then (the camera moving), 30 otherwise
  private layout = true // the layout is still settling
  private asleep = false
  private raf = 0
  private last = 0 // when the last frame was drawn
  private touched = false // you moved the camera yourself: no more automatic fitting
  private fitFirst: boolean // fit the view once the first nodes are drawn
  private refit: boolean // and again when the first layout settles
  private framed: boolean // the view shows the graph (worth keeping for the next visit)
  private shift = { x: 0, y: 0 } // the view's centre, moved off the panel (eased)
  private fog = new THREE.Fog(num("void-950"), 1, 2) // the far side of the graph fades into the page
  private clock = 0 // seconds the map has lived, for the stars' breath
  private ticked = 0 // when the clock last moved (performance.now())
  private sparks: Spark[] = []
  private due = 0 // sparks owed (fractions add up between frames)
  private sparkPoints: THREE.Points
  private dust: THREE.Points
  private controls: OrbitControls
  private camera: THREE.PerspectiveCamera
  private g: Graph
  private host: HTMLElement
  private s: State

  /**
   * Take over the library's forces, camera and frame loop.
   * @param g the library's methods
   * @param host the element the map is drawn in
   * @param s the state, which map.tsx keeps up to date
   */
  constructor(g: Graph, host: HTMLElement, s: State) {
    this.g = g
    this.host = host
    this.s = s
    g.d3Force("link")?.distance((l: MapLink) => (isHub(this.kind(l.source)) || isHub(this.kind(l.target)) ? 44 : 24))
    g.d3Force("charge")
      ?.strength((n: MapNode) => (isHub(n.kind) ? -220 : -40))
      .distanceMax(320)
    g.lights([]) // every material is unlit
    g.scene().fog = this.fog
    this.sparkPoints = sparksObject()
    this.dust = dustObject()
    g.scene().add(this.sparkPoints, this.dust)
    g.renderer().setPixelRatio(Math.min(devicePixelRatio, MAX_DPR))
    this.camera = g.camera() as THREE.PerspectiveCamera
    this.controls = g.controls() as OrbitControls
    Object.assign(this.controls, { enableDamping: true, dampingFactor: 0.08, rotateSpeed: 0.6, minDistance: 20 })
    this.controls.maxDistance = 6000
    const view = kept
    this.fitFirst = this.refit = !view
    this.framed = Boolean(view)
    if (view) g.cameraPosition(view.position, view.target, 0)
    g.scene().onBeforeRender = () => this.frame()
    host.addEventListener("pointerdown", this.press, { passive: true })
    host.addEventListener("pointerup", this.release, { passive: true })
    host.addEventListener("pointercancel", this.release, { passive: true })
    host.addEventListener("wheel", this.zoom, { passive: true })
    host.addEventListener("pointermove", this.nudge, { passive: true })
    document.addEventListener("visibilitychange", this.visibility)
    g.pauseAnimation() // the library's own loop stops: this one decides when it draws
    this.asleep = true
    this.wake(view ? 1500 : undefined) // coming back to a map that has settled: a moment is enough
  }

  /**
   * Where a node is now.
   * @param n the node
   * @param v where to write it
   * @returns `v`
   */
  private at(n: MapNode, v = new THREE.Vector3()) {
    return v.set(n.x ?? 0, n.y ?? 0, n.z ?? 0)
  }

  private kind = (end: string | MapNode) => this.s.graph.byId.get(endId(end))?.kind ?? "concept"
  private press = () => {
    this.touched = true
    this.fast = Number.POSITIVE_INFINITY
    this.wake()
  }
  private release = () => {
    this.fast = performance.now() + 700 // the camera glides on a little
  }
  private zoom = () => {
    this.touched = true
    this.fast = performance.now() + 500
    this.wake()
  }
  private nudge = () => this.wake()
  private visibility = () => {
    if (!document.hidden) return this.wake(1000)
    cancelAnimationFrame(this.raf)
    this.asleep = true
  }

  /**
   * Draw again, for at least `ms` (long enough to drift a while; with reduced motion nothing drifts).
   * @param ms how long to stay awake
   */
  wake(ms = this.s.still ? 1500 : IDLE) {
    this.until = Math.max(this.until, performance.now() + ms)
    if (!this.asleep || document.hidden) return
    this.asleep = false
    this.host.dataset.awake = ""
    this.raf = requestAnimationFrame(this.loop)
  }

  /** Every display frame while awake: has the library draw one of its frames, 30 a second or 60 while the camera
   *  moves (resumeAnimation() draws a frame at once; pausing again cancels the next one it asks for). */
  private loop = (time: number) => {
    this.raf = requestAnimationFrame(this.loop)
    if (time < this.fast || time - this.last >= 1000 / 30 - 4) {
      this.last = time
      this.g.resumeAnimation()
      this.g.pauseAnimation()
    }
  }

  /** New data went to the library: its layout starts again (only new nodes move; it ends at once otherwise). */
  loaded() {
    this.layout = true
    this.wake(this.s.graph.settled ? 1500 : undefined)
  }

  /** The layout settled: every node keeps its place from now on. */
  settled() {
    this.layout = false
    pinAll(this.s.graph)
    if (this.refit && !this.touched) this.fit(900)
    this.refit = false
  }

  /**
   * Everything shown, as large as the view allows.
   * @param ms how long the camera takes to get there
   */
  fit(ms = 700) {
    const s = this.s
    const shown = s.graph.nodes.filter((n) => n.x !== undefined && !s.hidden.has(n.kind)).map((n) => this.at(n))
    if (!shown.length) return
    const box = new THREE.Box3().setFromPoints(shown)
    const middle = box.getCenter(new THREE.Vector3())
    this.dust.position.copy(middle)
    this.dust.scale.setScalar(Math.max(260, box.getSize(new THREE.Vector3()).length() * 1.4))
    this.look(middle, shown, Math.min(64, this.host.clientWidth / 12), ms, 220)
  }

  /**
   * Fly to a node, close enough that its neighbours fill the view round it.
   * @param id the node
   */
  flyTo(id: string) {
    const n = this.s.graph.byId.get(id)
    if (n?.x === undefined) return
    const near = (this.s.graph.near.get(id) ?? []).filter((m) => m.x !== undefined && !this.s.hidden.has(m.kind))
    const where = (m: MapNode) => this.at(m)
    this.look(where(n), [where(n), ...near.map(where)], Math.min(96, this.host.clientWidth / 8), 1100, 60, true)
    this.touched = true
  }

  /**
   * Move the camera, keeping the direction it looks in, so that it looks at `target` and sees every point with `pad`
   * px to spare (but stays at least `nearest` away), clear of what covers the map: the search, the legend and the
   * panel if one is (or is about to be) open. The library's zoomToFit does this for a bounding box, which leaves the
   * graph small.
   * @param target what to look at
   * @param points what must be seen
   * @param pad the margin round them, in px
   * @param ms how long the camera takes
   * @param nearest the closest it may come
   * @param panel whether the panel covers the right (or the bottom) of the map
   */
  private look(
    target: THREE.Vector3,
    points: THREE.Vector3[],
    pad: number,
    ms: number,
    nearest: number,
    panel = Boolean(this.s.selected),
  ) {
    const camera = this.camera
    const forward = camera.getWorldDirection(new THREE.Vector3())
    const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize()
    const up = new THREE.Vector3().crossVectors(right, forward)
    const [w, h] = [this.host.clientWidth, this.host.clientHeight]
    const cover = this.s.cover(w, h, panel)
    const tan = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))
    // (at least a quarter of the view, however little a small screen leaves)
    const [tanV, tanH] = [
      tan * (Math.max(h / 4, h - cover.top - cover.bottom - 2 * pad) / h),
      tan * camera.aspect * (Math.max(w / 4, w - cover.right - 2 * pad) / w),
    ]
    let distance = nearest
    for (const p of points) {
      const d = p.clone().sub(target)
      distance = Math.max(
        distance,
        Math.max(Math.abs(d.dot(up)) / tanV, Math.abs(d.dot(right)) / tanH) - d.dot(forward),
      )
    }
    const time = this.s.still ? 0 : ms
    this.g.cameraPosition(target.clone().addScaledVector(forward, -distance), target, time)
    this.fast = Math.max(this.fast, performance.now() + time)
    this.wake(time + 500)
  }

  /**
   * Pulses out from a node through its neighbourhood, a hop at a time: each link sets out when the hop of its inner end
   * comes round (links between two nodes of the same hop stay out of it).
   * @param layers the neighbourhood, by hop
   */
  burst(layers: Map<string, number>) {
    if (this.s.still) return
    const now = performance.now()
    let last = -1
    for (const l of this.s.graph.links) {
      const [a, b] = [layers.get(endId(l.source)), layers.get(endId(l.target))]
      if (a === undefined || b === undefined || a === b) continue
      const hop = Math.min(a, b)
      this.s.pulses.set(l, now + hop * HOP)
      last = Math.max(last, hop)
    }
    if (last >= 0) this.wake(last * HOP + RISE + FALL + 200)
  }

  /**
   * A pulse from a hub out along `path`, a link per hop.
   * @param path the ids from the hub to the node at the end
   * @param now the time now
   * @returns in how many ms it reaches the node (its last link at its height)
   */
  private trace(path: string[], now: number) {
    for (let i = 1; i < path.length; i++) {
      const l = linkBetween(this.s.graph, path[i - 1], path[i])
      if (l) this.s.pulses.set(l, now + (i - 1) * HOP)
    }
    return path.length > 1 ? (path.length - 2) * HOP + RISE : 0
  }

  /**
   * Light the links a pulse is passing along, and let go of the ones it has passed.
   * @param now the time now
   */
  private pulse(now: number) {
    const peak = shade(PEAK)
    for (const [l, start] of this.s.pulses) {
      const view = linkView(l)
      view.material.color.copy(view.rest)
      if (now >= start + RISE + FALL) this.s.pulses.delete(l)
      else view.material.color.lerp(peak, level(start, now))
    }
  }

  /**
   * Fire new sparks as his state says, and move the ones on their way (each lights its link as it passes).
   * @param now the time now
   */
  private fire(now: number) {
    const s = this.s
    if (s.still) return
    const act = ACTIVITY[s.mood]
    if (now < this.until) this.due = Math.min(4, this.due + (act.rate * Math.min(100, now - this.ticked + 33)) / 1000)
    while (this.due >= 1 && this.sparks.length < SPARKS) {
      this.due -= 1
      const spark = this.spark(act, now)
      if (spark) this.sparks.push(spark)
    }
    const position = this.sparkPoints.geometry.getAttribute("position") as THREE.BufferAttribute
    const color = this.sparkPoints.geometry.getAttribute("color") as THREE.BufferAttribute
    const [from, to] = [new THREE.Vector3(), new THREE.Vector3()]
    this.sparks = this.sparks.filter((sp) => now < sp.start + sp.ms)
    for (let i = 0; i < SPARKS; i++) {
      const sp = this.sparks[i]
      if (!sp) {
        color.setXYZ(i, 0, 0, 0)
        position.setXYZ(i, 0, -1e6, 0) // and far out of sight
        continue
      }
      const t = (now - sp.start) / sp.ms
      const k = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2 // eased in and out along the link
      this.at(sp.from, from).lerp(this.at(sp.to, to), k)
      position.setXYZ(i, from.x, from.y, from.z)
      const light = Math.sin(Math.PI * t)
      color.setXYZ(i, sp.color.r * light, sp.color.g * light, sp.color.b * light)
    }
    position.needsUpdate = true
    color.needsUpdate = true
  }

  /**
   * A new spark on a link worth it: inside the selected neighbourhood if there is one, out of the hub his state calls
   * for, or anywhere; never on a hidden kind.
   * @param act how sparks fire in his state
   * @param now the time now
   * @returns the spark, or null when there is no link to fire on
   */
  private spark(act: (typeof ACTIVITY)[ShownState], now: number): Spark | null {
    const s = this.s
    const shown = (n: MapNode | undefined) => n !== undefined && n.x !== undefined && !s.hidden.has(n.kind)
    const near = act.from ? s.graph.near.get(act.from) : undefined
    for (let tries = 0; tries < 6; tries++) {
      let a: MapNode | undefined
      let b: MapNode | undefined
      if (s.focus) {
        const ids = [...s.focus.keys()]
        a = s.graph.byId.get(ids[Math.floor(Math.random() * ids.length)])
        const around = a ? (s.graph.near.get(a.id) ?? []).filter((m) => s.focus?.has(m.id)) : []
        b = around[Math.floor(Math.random() * around.length)]
      } else if (near?.length && Math.random() < 0.8) {
        // out of the hub, or one step further out from it
        const first = near[Math.floor(Math.random() * near.length)]
        const onward = (s.graph.near.get(first.id) ?? []).filter((m) => !isHub(m.kind))
        if (onward.length && Math.random() < 0.45) {
          a = first
          b = onward[Math.floor(Math.random() * onward.length)]
        } else {
          a = s.graph.byId.get(act.from ?? "")
          b = first
        }
      } else {
        const l = s.graph.links[Math.floor(Math.random() * s.graph.links.length)]
        if (!l) return null
        ;[a, b] = [s.graph.byId.get(endId(l.source)), s.graph.byId.get(endId(l.target))]
        if (Math.random() < 0.5) [a, b] = [b, a]
      }
      if (!shown(a) || !shown(b) || !a || !b) continue
      const l = linkBetween(s.graph, a.id, b.id)
      if (l && !s.pulses.has(l)) s.pulses.set(l, now)
      return { from: a, to: b, start: now, ms: act.ms * (0.8 + Math.random() * 0.4), color: shade(act.token) }
    }
    return null
  }

  /** Every frame, just before it is drawn. */
  private frame() {
    const now = performance.now()
    const s = this.s
    this.clock += Math.min(0.1, Math.max(0, (now - (this.ticked || now)) / 1000))
    this.ticked = now
    for (const [id, began] of s.flashes) {
      const n = s.graph.byId.get(id)
      if (!n || s.hidden.has(n.kind)) {
        s.flashes.delete(id)
        if (n) {
          update(n)
          paint(n, lookOf(s, n))
        }
        continue
      }
      if (!viewOf(n)) continue // not drawn yet
      if (!began) {
        // a pulse runs out to it from its hub, and it flashes when the pulse arrives
        const arrives = s.still ? 0 : this.trace(pathFromHub(s.graph, id, s.hidden), now)
        s.flashes.set(id, now + arrives)
        paint(n, lookOf(s, n))
        this.wake(arrives + 2 * FLASH)
        continue
      }
      if (now < began) continue // the pulse is still on its way
      const t = (now - began) / FLASH
      if (t < 2) glow(n, s.still ? 0.5 : t < 0.12 ? t / 0.12 : Math.max(0, 1 - (t - 0.12) / 0.88) ** 2)
      else {
        s.flashes.delete(id)
        update(n)
        paint(n, lookOf(s, n))
      }
    }
    this.fire(now)
    this.pulse(now)
    // a slow turn while you're here (easing out before it rests, when it rests); none while you look at a node
    this.controls.autoRotate = !s.still && !s.selected && !s.hovered
    this.controls.autoRotateSpeed = DRIFT * THREE.MathUtils.clamp((this.until - now) / 1500, 0, 1)
    if (this.fitFirst && s.graph.nodes.some((n) => viewOf(n))) {
      this.fitFirst = false
      this.framed = true
      this.fit(0)
    }
    this.place()
    const moving = this.aim()
    const busy = this.layout || moving || s.flashes.size > 0 || s.pulses.size > 0 || this.sparks.length > 0
    if (!this.asleep && !busy && now >= this.until) this.rest()
  }

  /**
   * Centre the view on what the search, the legend and the panel leave uncovered (easing there), and fade the graph's
   * far side into the page.
   * @returns true while the view is still on its way
   */
  private aim() {
    const [w, h] = [this.host.clientWidth, this.host.clientHeight]
    const cover = this.s.cover(w, h, Boolean(this.s.selected))
    const [x, y] = [cover.right / 2, (cover.bottom - cover.top) / 2]
    const ease = this.s.still ? 1 : 0.18
    this.shift.x += Math.abs(x - this.shift.x) < 0.5 ? x - this.shift.x : (x - this.shift.x) * ease
    this.shift.y += Math.abs(y - this.shift.y) < 0.5 ? y - this.shift.y : (y - this.shift.y) * ease
    const view = this.camera.view
    if (this.shift.x || this.shift.y) {
      if (!view?.enabled || view.offsetX !== this.shift.x || view.offsetY !== this.shift.y || view.fullWidth !== w)
        this.camera.setViewOffset(w, h, this.shift.x, this.shift.y, w, h)
    } else if (view?.enabled) this.camera.clearViewOffset()
    const reach = this.camera.position.distanceTo(this.controls.target)
    this.fog.near = reach * 0.9
    this.fog.far = reach * 2.6
    return this.shift.x !== x || this.shift.y !== y
  }

  /** Fade what the camera has come too close to, and show the names that don't overlap a more important one. */
  private place() {
    const s = this.s
    const eye = this.camera.position
    const reach = eye.distanceTo(this.controls.target)
    const [w, h] = [this.host.clientWidth, this.host.clientHeight]
    const point = new THREE.Vector3()
    const names: MapNode[] = []
    for (const n of s.graph.nodes) {
      const view = viewOf(n)
      const object = objectOf(n)
      if (!view || !object) continue
      fade(view, THREE.MathUtils.smoothstep(eye.distanceTo(object.position), reach * 0.1, reach * 0.4))
      if (!s.still) live(view, this.clock)
      if (named(s, n) && view.fade > 0.6) names.push(n)
      else view.label.visible = false
    }
    const taken: number[][] = []
    for (const n of names.sort((a, b) => rank(s, a) - rank(s, b))) {
      const label = (viewOf(n) as NodeView).label
      label.getWorldPosition(point).project(this.camera)
      const [x, y, width] = [((point.x + 1) * w) / 2, ((1 - point.y) * h) / 2, labelWidth(n)]
      // centred under its node, but kept inside the map at its edges (none for a node out of view)
      const left = THREE.MathUtils.clamp(x - width / 2, 4, Math.max(4, w - width - 4))
      label.center.x = (x - left) / width
      const box = [left, y + 6, left + width, y + 26]
      const inView = point.z < 1 && x >= 0 && x <= w && y >= 0 && y <= h
      label.visible = inView && !taken.some((b) => b[0] < box[2] && box[0] < b[2] && b[1] < box[3] && box[1] < b[3])
      if (label.visible) taken.push(box)
    }
  }

  /** Nothing moves: stop drawing, and remember the view. */
  private rest() {
    this.asleep = true
    cancelAnimationFrame(this.raf)
    delete this.host.dataset.awake
    this.keep()
  }

  /** Remember the view for the next visit, once it shows the graph. */
  private keep() {
    if (this.framed) kept = { position: this.camera.position.clone(), target: this.controls.target.clone() }
  }

  /** The scene goes: stop drawing, let go of the page, and remember the view. */
  dispose() {
    cancelAnimationFrame(this.raf)
    this.host.removeEventListener("pointerdown", this.press)
    this.host.removeEventListener("pointerup", this.release)
    this.host.removeEventListener("pointercancel", this.release)
    this.host.removeEventListener("wheel", this.zoom)
    this.host.removeEventListener("pointermove", this.nudge)
    document.removeEventListener("visibilitychange", this.visibility)
    this.keep()
    this.g.scene().remove(this.sparkPoints, this.dust)
    this.g.scene().onBeforeRender = () => {}
  }
}

/**
 * The sparks: one point each, drawn with the stars' soft light, their colour written each frame (black is none).
 * @returns the points
 */
function sparksObject(): THREE.Points {
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(SPARKS * 3), 3))
  geometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(SPARKS * 3), 3))
  const points = new THREE.Points(
    geometry,
    asLight(new THREE.PointsMaterial({ size: 5, map: glowMap(), vertexColors: true })),
  )
  points.frustumCulled = false
  return points
}

/**
 * The dust round the graph: faint specks in a shell about it (scaled to the graph when the view fits it), so the
 * map has depth as the view turns. Never fogged: it is the far distance.
 * @returns the points
 */
function dustObject(): THREE.Points {
  const position = new Float32Array(DUST * 3)
  const color = new Float32Array(DUST * 3)
  const tints = ["rigel-400", "steel-400", "nebula-300", "betel-300"].map((t) => shade(t))
  for (let i = 0; i < DUST; i++) {
    const z = Math.random() * 2 - 1
    const phi = Math.random() * Math.PI * 2
    const r = 0.45 + Math.random() * 0.55
    const ring = Math.sqrt(1 - z * z) * r
    position.set([ring * Math.cos(phi), z * r, ring * Math.sin(phi)], i * 3)
    const tint = tints[i % 7 === 0 ? 3 : i % 3]
    const k = 0.14 + Math.random() * 0.36
    color.set([tint.r * k, tint.g * k, tint.b * k], i * 3)
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute("position", new THREE.BufferAttribute(position, 3))
  geometry.setAttribute("color", new THREE.BufferAttribute(color, 3))
  const points = new THREE.Points(
    geometry,
    asLight(
      new THREE.PointsMaterial({ size: 2.2, map: glowMap(), vertexColors: true, fog: false, sizeAttenuation: false }),
    ),
  )
  points.scale.setScalar(320)
  points.frustumCulled = false
  return points
}
