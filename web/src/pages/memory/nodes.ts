/**
 * How the map draws a node and a link in 3D.
 *
 * A node is a star: a soft glow and a white-hot core in its kind's shade, and round it its kind's glyph in thin lines,
 * turning slowly (so the kinds differ without colour too: You a diamond in a diamond shell, a fact a diamond, a note a
 * hexagon, a skill a triangle, a journal day a cube, an unverified memory a dashed diamond), and Orion the
 * stage's reactor in small, its electrons running round its three orbits. Each breathes at its own pace. An invisible
 * sphere round it is easier to point at, and its name is real text over the canvas (three's CSS2DRenderer), so it
 * stays crisp: text alone, lit in its kind's shade, no box. Geometries and the glow are shared; each node has its own
 * materials, so it can dim and flash on its own.
 *
 * A link is a 1 px line of light with its own material, so a pulse can light one link without the library restyling
 * them all; the controller sets the colours.
 */
import * as THREE from "three"
import { CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js"
import { hex } from "@/lib/palette"
import { ORBITS, orbitPoint } from "./atom"
import { isHub, KINDS, type MapLink, type MapNode, type Shape } from "./graph"

const shades = new Map<string, THREE.Color>()

/**
 * A palette token as a three colour, converted once. Shared: copy it, never change it.
 * @param token a palette token, for example `aurora-300`
 * @returns the colour
 */
export function shade(token: string): THREE.Color {
  let color = shades.get(token)
  if (!color) {
    color = new THREE.Color(hex(token))
    shades.set(token, color)
  }
  return color
}

/** What a selected node and a flash turn to. */
const WHITE = "steel-50"

/**
 * Make a material pure light: its colour adds to what is under it and it never writes the canvas's alpha, so where
 * there is little light the page shows through untouched (the room behind the map), instead of being darkened by an
 * alpha that additive blending would pile up. The reactor writes its light the same way.
 * @param material the material
 * @returns the same material
 */
export function asLight<M extends THREE.Material>(material: M): M {
  material.transparent = true
  material.depthWrite = false
  material.blending = THREE.CustomBlending
  material.blendEquation = THREE.AddEquation
  material.blendSrc = THREE.SrcAlphaFactor
  material.blendDst = THREE.OneFactor
  material.blendSrcAlpha = THREE.ZeroFactor
  material.blendDstAlpha = THREE.OneFactor
  return material
}

let glowTexture: THREE.Texture | null = null

/**
 * The soft round light every star, spark and speck of dust is drawn with: a radial falloff to nothing well inside its
 * square, so no edge ever shows. A grey mask (the material gives it its colour), made once.
 * @returns the texture
 */
export function glowMap(): THREE.Texture {
  if (glowTexture) return glowTexture
  const size = 128
  const canvas = document.createElement("canvas")
  canvas.width = canvas.height = size
  const ctx = canvas.getContext("2d")
  if (ctx) {
    const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
    for (const [at, alpha] of [
      [0, 1],
      [0.1, 0.9],
      [0.25, 0.42],
      [0.45, 0.14],
      [0.7, 0.035],
      [0.96, 0],
    ])
      g.addColorStop(at, `rgba(255, 255, 255, ${alpha})`)
    ctx.fillStyle = g
    ctx.fillRect(0, 0, size, size)
  }
  glowTexture = new THREE.CanvasTexture(canvas)
  return glowTexture
}

/**
 * The edges of each geometry as line segments, in one buffer.
 * @param parts the geometries
 * @returns their edges
 */
function outline(...parts: THREE.BufferGeometry[]) {
  const position = parts.flatMap((g) => [...new THREE.EdgesGeometry(g).getAttribute("position").array])
  return new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(position, 3))
}

/** The scale that brings the reactor's outermost orbit to radius 1. */
const ORBIT_SCALE = 1 / ORBITS[ORBITS.length - 1].radius

/**
 * The stage's reactor in lines: the atom's three orbits (the same circles, tipped and rolled as the reactor draws them
 * at rest), the outermost of radius 1.
 * @returns their edges
 */
function orbits() {
  const position: number[] = []
  const round = 72
  for (const orbit of ORBITS)
    for (let k = 0; k < round; k++) {
      const [a, b] = [orbitPoint(orbit, k / round), orbitPoint(orbit, (k + 1) / round)]
      position.push(...a.map((v) => v * ORBIT_SCALE), ...b.map((v) => v * ORBIT_SCALE))
    }
  return new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(position, 3))
}

const diamond = new THREE.OctahedronGeometry(1)

/** Each kind's glyph, in thin lines round its star. */
const glyphs: Record<Shape, THREE.BufferGeometry> = {
  octahedron: outline(diamond, new THREE.OctahedronGeometry(0.55)),
  core: orbits(),
  sphere: outline(new THREE.IcosahedronGeometry(1, 0)),
  diamond: outline(diamond),
  // each segment carries its length, so the dashed material can break it
  dashed: new THREE.LineSegments(outline(diamond)).computeLineDistances().geometry,
  hollow: outline(diamond),
  cube: outline(new THREE.BoxGeometry(1.3, 1.3, 1.3)),
  hexagon: outline(new THREE.CylinderGeometry(1, 1, 0.7, 6).rotateX(Math.PI / 2)),
  triangle: outline(new THREE.TetrahedronGeometry(1.2)),
}

/** How bright each part of a star is at rest: its glow, its core and its glyph (his notes and an unverified memory
 *  are mostly glyph, their core dim). */
const LIGHT: Record<Shape, [glow: number, core: number, glyph: number]> = {
  octahedron: [0.85, 1, 0.75],
  core: [0.9, 1, 0.8],
  sphere: [0.6, 0.9, 0.45],
  diamond: [0.55, 0.9, 0.6],
  dashed: [0.3, 0.35, 0.8],
  hollow: [0.4, 0.45, 0.8],
  cube: [0.4, 0.75, 0.55],
  hexagon: [0.55, 0.9, 0.6],
  triangle: [0.55, 0.9, 0.65],
}

/** The dashes of an unverified memory's outline, in the diamond's own units (each edge is about 1.4 long). */
const DASH = { dashSize: 0.24, gapSize: 0.16 }

const hitShape = new THREE.SphereGeometry(1, 8, 6)
const hitMaterial = new THREE.MeshBasicMaterial()

const LABEL =
  "pointer-events-none max-w-60 truncate px-1 leading-none [text-shadow:0_0_2px_var(--color-black),0_1px_3px_var(--color-black)]"
const TEXT = "text-xs font-medium text-steel-100 data-selected:text-white"
const HUB_TEXT = "font-semibold text-micro uppercase tracking-[0.32em] text-steel-50"

/**
 * A node's size: a concept grows with the memories that mention it.
 * @param n the node
 * @returns its radius in the map's units
 */
export const radius = (n: MapNode) =>
  KINDS[n.kind].size * (n.kind === "concept" ? 1 + Math.min(1.2, Math.max(0, n.degree - 1) * 0.15) : 1)

/** A node as drawn. */
export interface NodeView {
  body: THREE.Group // the star and its glyph: what a flash swells
  glow: THREE.SpriteMaterial
  core: THREE.SpriteMaterial
  glyph: THREE.LineBasicMaterial
  turner: THREE.Object3D // the glyph, which turns
  electrons: THREE.Sprite[] // Orion's only
  hit: THREE.Mesh
  label: CSS2DObject
  light: [glow: number, core: number, glyph: number] // as painted, before fading and breathing
  fade: number // 1, or less when the camera comes very close
  phase: number // its own breath and turn
  flash: number // 0, or a flash's strength
}

/** How a node is painted at the moment. */
export interface Look {
  selected: boolean
  dim: boolean // outside the selected node's neighbourhood
  label: boolean
}

/**
 * A small fixed number per id, 0 to 1: a node's own pace.
 * @param id the node's id
 * @returns the number
 */
function seed(id: string) {
  let h = 2166136261
  for (let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 16777619)
  return (h >>> 0) / 2 ** 32
}

const lightSprite = (color: THREE.Color) => asLight(new THREE.SpriteMaterial({ map: glowMap(), color }))

/**
 * The 3D object for a node (the graph library calls this for each node it shows).
 * @param n the node
 * @param look how it is painted now
 * @returns its object, with its view in `userData.view`
 */
export function makeNode(n: MapNode, look: Look): THREE.Object3D {
  const { shape, token } = KINDS[n.kind]
  const color = shade(token)
  const glow = lightSprite(color)
  const core = lightSprite(color.clone().lerp(shade(WHITE), 0.65))
  const glyph = asLight(
    shape === "dashed" ? new THREE.LineDashedMaterial({ color, ...DASH }) : new THREE.LineBasicMaterial({ color }),
  )
  const halo = new THREE.Sprite(glow)
  halo.scale.setScalar(shape === "core" ? 4.6 : 5.2)
  const heart = new THREE.Sprite(core)
  heart.scale.setScalar(shape === "core" ? 1.3 : 1.5)
  const turner = new THREE.LineSegments(glyphs[shape], glyph)
  turner.scale.setScalar(shape === "core" ? 1.05 : 0.9)
  const electrons =
    shape === "core"
      ? ORBITS.map(() => {
          const e = new THREE.Sprite(lightSprite(shade("rigel-100")))
          e.scale.setScalar(0.55)
          return e
        })
      : []
  if (electrons.length) turner.add(...electrons) // they ride the orbits as the glyph turns
  const body = new THREE.Group().add(halo, heart, turner)
  const hit = new THREE.Mesh(hitShape, hitMaterial)
  hit.visible = false // never drawn, still hit by the pointer
  const element = document.createElement("div")
  element.className = `${LABEL} ${isHub(n.kind) ? HUB_TEXT : TEXT}`
  const label = new CSS2DObject(element)
  label.center.set(0.5, -0.4) // under the node
  const view: NodeView = {
    body,
    glow,
    core,
    glyph,
    turner,
    electrons,
    hit,
    label,
    light: LIGHT[shape],
    fade: 1,
    phase: seed(n.id),
    flash: 0,
  }
  update(n, view)
  paint(n, look, view)
  const group = new THREE.Group().add(body, hit, label)
  group.userData.view = view
  return group
}

/**
 * A node's view, once the library has made its object.
 * @param n the node
 * @returns its view, or undefined while it isn't drawn
 */
export const viewOf = (n: MapNode) =>
  (n as MapNode & { __threeObj?: THREE.Object3D }).__threeObj?.userData.view as NodeView | undefined

/**
 * Bring a node's object up to date with its data (a concept mentioned more often grows; a name can change).
 * @param n the node
 * @param view its view
 */
export function update(n: MapNode, view = viewOf(n)) {
  if (!view) return
  const r = radius(n)
  view.body.scale.setScalar(r)
  view.hit.scale.setScalar(Math.max(r * 1.6, 4.5))
  view.label.position.set(0, -r * 1.3, 0)
  view.label.element.textContent = isHub(n.kind) ? n.label.toUpperCase() : n.label
}

/**
 * Paint a node: selected (white-hot), dimmed outside the selected neighbourhood, or as it is; its name shown or not.
 * @param n the node
 * @param look how to paint it
 * @param view its view
 */
export function paint(n: MapNode, look: Look, view = viewOf(n)) {
  if (!view) return
  const [glow, core, glyph] = LIGHT[KINDS[n.kind].shape]
  const k = look.dim ? 0.16 : look.selected ? 1.5 : 1
  view.light = [Math.min(1, glow * k), Math.min(1, core * (look.dim ? 0.3 : 1)), Math.min(1, glyph * k)]
  view.glyph.color.copy(shade(look.selected ? WHITE : KINDS[n.kind].token))
  view.label.visible = look.label
  view.label.element.style.opacity = look.dim ? "0.35" : ""
  view.label.element.toggleAttribute("data-selected", look.selected)
  light(view, 1)
}

/**
 * Set a node's light: as painted, faded when the camera is close, breathing by `breath` and flaring by its flash.
 * @param view the node's view
 * @param breath 1 at rest, a little more or less as it breathes
 */
function light(view: NodeView, breath: number) {
  const [glow, core, glyph] = view.light
  const f = view.fade
  const lit = view.flash
  view.glow.opacity = Math.min(1, glow * breath * f + lit * 0.8)
  view.core.opacity = Math.min(1, core * f + lit)
  view.glyph.opacity = Math.min(1, glyph * f + lit * 0.6)
  view.body.visible = f > 0.02
}

/**
 * Fade a node the camera has come too close to: up close it would fill the view.
 * @param view the node's view
 * @param k from 1 (as painted) down to 0 (gone)
 * @param always repaint even when `k` barely changed
 */
export function fade(view: NodeView, k: number, always = false) {
  if (!always && Math.abs(k - view.fade) < 0.02) return
  view.fade = k
  light(view, 1)
}

/**
 * A flash: the node swells and goes white-hot.
 * @param n the node
 * @param k its strength, 0 to 1
 */
export function glow(n: MapNode, k: number) {
  const view = viewOf(n)
  if (!view) return
  view.flash = k
  view.body.scale.setScalar(radius(n) * (1 + 0.9 * k))
  view.glyph.color.copy(shade(KINDS[n.kind].token)).lerp(shade(WHITE), k)
  light(view, 1)
}

/**
 * A node's life between events: its glyph turns, its glow breathes, and Orion's electrons run round their orbits.
 * Called each frame for each drawn node (nothing with reduced motion).
 * @param view the node's view
 * @param seconds the map's clock
 */
export function live(view: NodeView, seconds: number) {
  const p = view.phase
  const atom = view.electrons.length > 0 // Orion's orbits sway, as the reactor's do, rather than spin
  view.turner.rotation.y = atom ? Math.sin(seconds * 0.3) * 0.5 : seconds * (0.12 + 0.18 * p) + p * 6.28
  view.turner.rotation.x = Math.sin(seconds * 0.21 + p * 9) * (atom ? 0.15 : 0.35)
  light(view, 0.86 + 0.14 * Math.sin(seconds * (0.9 + p) + p * 20))
  view.electrons.forEach((e, i) => {
    const orbit = ORBITS[i]
    const [x, y, z] = orbitPoint(orbit, (seconds * 0.22 * orbit.speed + orbit.phase) % 1)
    e.position.set(x * ORBIT_SCALE, y * ORBIT_SCALE, z * ORBIT_SCALE)
  })
}

/** A link as drawn: its own material, and the colour it rests at between pulses. */
export interface LinkView {
  material: THREE.LineBasicMaterial
  rest: THREE.Color
}

const links = new WeakMap<MapLink, LinkView>()

/**
 * A link's view, made the first time it is asked for (the library asks for the material when it first draws the link,
 * and again whenever it brings it back). Its light adds up where links cross, like the reactor's. The library disposes
 * of the material with a link it removes; three sets a disposed material up again if it comes back.
 * @param l the link
 * @returns its view
 */
export function linkView(l: MapLink): LinkView {
  let view = links.get(l)
  if (!view) {
    view = {
      material: asLight(new THREE.LineBasicMaterial()),
      rest: new THREE.Color(),
    }
    links.set(l, view)
  }
  return view
}
