/**
 * The same scene without an engine: quality "off", or a browser without WebGL 2. One static SVG, drawn once per size
 * and layout, that costs the page nothing while it stands: the stars of the catalogue as small discs (their colours
 * from B−V, their sizes and light from their magnitudes, as the engine shows them), M42, the airglow as one radial
 * gradient round the limb with a thin core on it, the world, and the Orion figure, shown at rest. His state only changes
 * the airglow's colour (a CSS variable). It reuses the Scene for the projection, so the stars stand where the engine
 * puts them. Writing, meteors and the aurora are the engine's alone.
 */
import { hex } from "@/lib/palette"
import { type Lin, makePalette, STATE_FAMILY } from "./colors"
import type { CoreState } from "./index"
import { Scene } from "./scene"
import { type Catalogue, flux, reach, STAR_EXPOSURE } from "./sky"
import { altitude } from "./world"

const NS = "http://www.w3.org/2000/svg"
/** The faintest star the still draws (a disc each: the engine's faintest would be eight thousand nodes). */
const STILL_FAINTEST = 5.3
/** How far above the limb the airglow's gradient reaches, CSS px, and its stops: height, opacity. */
const GLOW_REACH = 150
const GLOW_STOPS: readonly (readonly [number, number])[] = [
  [0, 0.45],
  [3, 0.2],
  [10, 0.085],
  [30, 0.03],
  [80, 0.009],
  [GLOW_REACH, 0],
]
/** The figure's opacity at rest. */
const FIGURE_OPACITY = 0.5

const encode = (c: number) => {
  const v = Math.min(1, Math.max(0, c))
  return v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055
}
const toHex = ([r, g, b]: Lin) =>
  `#${[r, g, b]
    .map((c) =>
      Math.round(encode(c) * 255)
        .toString(16)
        .padStart(2, "0"),
    )
    .join("")}`

/** An SVG element with attributes. */
function el<K extends keyof SVGElementTagNameMap>(name: K, attributes: Record<string, string | number> = {}) {
  const e = document.createElementNS(NS, name)
  for (const [k, v] of Object.entries(attributes)) e.setAttribute(k, String(v))
  return e
}

/** A gradient stop in a colour and opacity (a style, so the colour may be a CSS variable). */
function stop(offset: number, colour: string, opacity: number) {
  const s = el("stop", { offset })
  s.style.cssText = `stop-color:${colour};stop-opacity:${opacity}`
  return s
}

export interface Still {
  readonly element: SVGSVGElement
  /** The catalogue has loaded. */
  sky(catalogue: Catalogue): void
  /** The canvas's size and where the limb's top and the talk button are, CSS px. */
  layout(width: number, height: number, horizon: number, talkX: number, talkY: number): void
  state(state: CoreState): void
  rest(on: boolean): void
  dispose(): void
}

/**
 * Build the still.
 * @returns the SVG and how to change it
 * @throws Error when palette.css lacks a token Horizon reads
 */
export function createStill(): Still {
  const palette = makePalette()
  const scene = new Scene(palette, true)
  const svg = el("svg", { "aria-hidden": "true", preserveAspectRatio: "none" })
  svg.style.cssText = "position:fixed;inset:0;width:100%;height:100%;pointer-events:none"
  const glow = (state: CoreState) => hex(`${STATE_FAMILY[state]}-400`)
  svg.style.setProperty("--glow", glow("idle"))
  const figure = el("path", { fill: "none", "stroke-width": 0.9, "stroke-linecap": "round" })
  figure.style.cssText = `stroke:${toHex(palette.figure)};transition:opacity 1.2s`
  let resting = false
  let size: [number, number] | null = null

  function draw() {
    svg.replaceChildren()
    if (!size || !scene.laid) return
    const [w, h] = size
    const l = scene.limb
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`)

    // the sky: very dark, a little lighter towards the limb
    const dim = el("linearGradient", { id: "still-sky", x1: 0, y1: 0, x2: 0, y2: 1 })
    dim.append(stop(0, toHex(palette.sky), 1), stop(1, toHex(palette.sky.map((c) => c * 2.4) as Lin), 1))
    svg.append(dim, el("rect", { width: w, height: h, fill: "url(#still-sky)" }))

    if (scene.sky) {
      // M42, a faint pink smudge
      const [mx, my, sx, sy] = scene.m42
      const smudge = el("radialGradient", { id: "still-m42" })
      smudge.append(stop(0, toHex(palette.nebula), 0.34), stop(1, toHex(palette.nebula), 0))
      svg.append(
        smudge,
        el("ellipse", {
          cx: scene.frame.x + mx * scene.frame.scale,
          cy: scene.frame.y - my * scene.frame.scale,
          rx: sx * scene.frame.scale * 2.6,
          ry: sy * scene.frame.scale * 2.6,
          fill: "url(#still-m42)",
        }),
      )
      // the stars, brightest last so they sit on top
      const stars = el("g")
      for (let i = scene.sky.count - 1; i >= 0; i--) {
        const mag = scene.sky.vmag[i]
        if (mag > STILL_FAINTEST) continue
        const at = scene.starAt(i)
        if (!at || at[0] < -4 || at[0] > w + 4 || at[1] < -4 || at[1] > h || altitude(l, at[0], at[1]) < 2) continue
        const c = scene.starColors
        stars.append(
          el("circle", {
            cx: at[0].toFixed(1),
            cy: at[1].toFixed(1),
            r: Math.min(3.2, Math.max(0.5, reach(mag) * 0.42)).toFixed(2),
            fill: toHex([c[i * 3], c[i * 3 + 1], c[i * 3 + 2]]),
            opacity: (1 - Math.exp(-STAR_EXPOSURE * flux(mag) * 1.4)).toFixed(2),
          }),
        )
      }
      svg.append(stars)
    }

    // the airglow: a gradient disc round the limb (the world covers its inside), then the figure over the sky
    const gradient = el("radialGradient", {
      id: "still-glow",
      cx: l.cx,
      cy: l.cy,
      r: l.r + GLOW_REACH,
      gradientUnits: "userSpaceOnUse",
    })
    for (const [height, opacity] of GLOW_STOPS)
      gradient.append(stop((l.r + height) / (l.r + GLOW_REACH), "var(--glow)", opacity))
    svg.append(gradient, el("circle", { cx: l.cx, cy: l.cy, r: l.r + GLOW_REACH, fill: "url(#still-glow)" }))
    if (scene.sky) {
      const lines = scene.sky.lines
      let d = ""
      for (let k = 0; k < lines.length; k += 2) {
        const a = scene.starAt(lines[k])
        const b = scene.starAt(lines[k + 1])
        if (a && b) d += `M${a[0].toFixed(1)} ${a[1].toFixed(1)}L${b[0].toFixed(1)} ${b[1].toFixed(1)}`
      }
      figure.setAttribute("d", d)
      figure.style.opacity = resting ? String(FIGURE_OPACITY) : "0"
      svg.append(figure)
    }

    // the world, and its edge
    svg.append(el("circle", { cx: l.cx, cy: l.cy, r: l.r, fill: toHex(palette.ground) }))
    const edge = el("circle", { cx: l.cx, cy: l.cy, r: l.r, fill: "none", "stroke-width": 1.3 })
    edge.style.stroke = "color-mix(in oklab, var(--glow) 60%, white)"
    svg.append(edge)
  }

  return {
    element: svg,
    sky(catalogue) {
      scene.setSky(catalogue)
      draw()
    },
    layout(width, height, horizon, talkX, talkY) {
      size = [width, height]
      scene.size(width, height)
      scene.layout(horizon, talkX, talkY)
      draw()
    },
    state(state) {
      svg.style.setProperty("--glow", glow(state))
    },
    rest(on) {
      resting = on
      figure.style.opacity = on ? String(FIGURE_OPACITY) : "0"
    },
    dispose() {
      svg.remove()
    },
  }
}
