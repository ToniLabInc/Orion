/// <reference types="node" />
import { readFileSync } from "node:fs"
import { expect, test } from "vitest"
import { makeNoise, NOISE_SIZE } from "./noise"
import {
  type Catalogue,
  CENTRE,
  decode,
  direction,
  FAINTEST,
  FIGURE_BOTTOM,
  FIGURE_TOP,
  figureSpan,
  flux,
  frame,
  GALACTIC_POLE,
  lightAt,
  MW_CELL,
  MW_MARGIN,
  milkyWay,
  planck,
  project,
  reach,
  STAR_CUTOFF,
  starColor,
  temperature,
  view,
  WIDEST,
} from "./sky"

/** The real catalogue, as build_stars.py packed it. */
function load(): Catalogue {
  const file = readFileSync(new URL("./stars.bin", import.meta.url))
  return decode(file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength) as ArrayBuffer)
}
const sky = load()

const angle = (a: ArrayLike<number>, ai: number, b: ArrayLike<number>, bi: number) =>
  (Math.acos(Math.min(1, a[ai] * b[bi] + a[ai + 1] * b[bi + 1] + a[ai + 2] * b[bi + 2])) * 180) / Math.PI

/** The catalogue star nearest a place on the sky, with how far off it is (degrees). */
function nearest(ra: number, dec: number): { i: number; off: number } {
  const [x, y, z] = direction(ra, dec)
  let best = { i: -1, off: 180 }
  for (let i = 0; i < sky.count; i++) {
    const off = angle(sky.dirs, i * 3, [x, y, z], 0)
    if (off < best.off) best = { i, off }
  }
  return best
}

test("the catalogue: 8 404 stars to magnitude 6.5, brightest first, true unit directions, Orion's figure in 21 lines", () => {
  expect(sky.count).toBe(8404)
  expect(sky.lines).toHaveLength(42)
  for (let i = 1; i < sky.count; i++) expect(sky.vmag[i]).toBeGreaterThanOrEqual(sky.vmag[i - 1])
  expect(sky.vmag[0]).toBeCloseTo(-1.46, 1) // Sirius
  expect(sky.vmag[sky.count - 1]).toBeLessThanOrEqual(FAINTEST + 0.02)
  for (let i = 0; i < sky.count; i += 97) {
    expect(Math.hypot(sky.dirs[i * 3], sky.dirs[i * 3 + 1], sky.dirs[i * 3 + 2])).toBeCloseTo(1, 5)
    expect(sky.bv[i]).toBeGreaterThanOrEqual(-0.4)
    expect(sky.bv[i]).toBeLessThanOrEqual(2.5)
  }
  // Sirius is where Sirius is: RA 6h45m09s, Dec −16°43′
  expect(angle(sky.dirs, 0, direction(6 + 45 / 60 + 9 / 3600, -(16 + 43 / 60)), 0)).toBeLessThan(0.02)
})

test("a file that isn't a catalogue, or is cut short, is refused with a plain error", () => {
  const file = readFileSync(new URL("./stars.bin", import.meta.url))
  const whole = file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength) as ArrayBuffer
  expect(() => decode(whole.slice(0, whole.byteLength - 3))).toThrow(/cut short/)
  const wrong = new Uint8Array(whole.slice(0))
  wrong[0] = 0x58
  expect(() => decode(wrong.buffer)).toThrow(/not a version 1 star catalogue/)
})

test("the figure's stars are the real ones: the belt, Betelgeuse, Rigel, the head and the sword", () => {
  const named: [string, number, number, number][] = [
    ["Betelgeuse", 5 + 55 / 60 + 10.3 / 3600, 7 + 24 / 60 + 25 / 3600, 0.5], // magnitudes as the BSC lists them
    ["Rigel", 5 + 14 / 60 + 32.3 / 3600, -(8 + 12 / 60 + 6 / 3600), 0.12],
    ["Alnitak", 5 + 40 / 60 + 45.5 / 3600, -(1 + 56 / 60 + 33 / 3600), 2.05],
    ["Alnilam", 5 + 36 / 60 + 12.8 / 3600, -(1 + 12 / 60 + 7 / 3600), 1.7],
    ["Mintaka", 5 + 32 / 60 + 0.4 / 3600, -(17 / 60 + 57 / 3600), 2.23],
    ["Meissa", 5 + 35 / 60 + 8.3 / 3600, 9 + 56 / 60 + 3 / 3600, 3.54],
    ["Hatysa", 5 + 35 / 60 + 26 / 3600, -(5 + 54 / 60 + 36 / 3600), 2.77],
  ]
  const onLine = new Set(sky.lines)
  for (const [name, ra, dec, mag] of named) {
    const { i, off } = nearest(ra, dec)
    expect(off, name).toBeLessThan(0.05)
    expect(sky.vmag[i], name).toBeCloseTo(mag, 1)
    expect(onLine.has(i), `${name} is on a line of the figure`).toBe(true)
  }
})

test("colour from B−V: Rigel blue-white, the Sun pale yellow, Betelgeuse orange-red; each at a luminance of one", () => {
  expect(temperature(0.65)).toBeGreaterThan(5600) // the Sun's colour index
  expect(temperature(0.65)).toBeLessThan(6000)
  expect(temperature(-0.03)).toBeGreaterThan(10000) // Rigel (the fit is a black body's: its real 12 000 K is hotter)
  expect(temperature(1.85)).toBeLessThan(3700) // Betelgeuse
  for (let bv = -0.4; bv < 2.5; bv += 0.1) expect(temperature(bv + 0.1)).toBeLessThan(temperature(bv))
  const [rr, rg, rb] = starColor(-0.03)
  expect(rb).toBeGreaterThan(rr * 1.15)
  const [br, , bb] = starColor(1.85)
  expect(br).toBeGreaterThan(bb * 2)
  const [sr, , sb] = starColor(0.65)
  expect(Math.abs(sr - sb)).toBeLessThan(0.3)
  for (const bv of [-0.3, 0, 0.65, 1.2, 2]) {
    const [r, g, b] = starColor(bv)
    expect(0.2126 * r + 0.7152 * g + 0.0722 * b).toBeCloseTo(1, 6)
    expect(Math.min(r, g, b)).toBeGreaterThanOrEqual(0)
  }
  expect(rg).toBeGreaterThan(0)
})

test("the black body's chromaticity: D65 white at 6 500 K, an incandescent orange at 3 000 K", () => {
  const [x, y] = planck(6500)
  expect(x).toBeCloseTo(0.3135, 2)
  expect(y).toBeCloseTo(0.3237, 2)
  const [x3, y3] = planck(3000)
  expect(x3).toBeCloseTo(0.4369, 2)
  expect(y3).toBeCloseTo(0.4041, 2)
})

test("light: five magnitudes are a factor of a hundred; a brighter star reaches further; the reach is where the light is gone", () => {
  expect(flux(FAINTEST)).toBe(1)
  expect(flux(FAINTEST - 5)).toBeCloseTo(100, 6)
  let last = 0
  for (const mag of [6.5, 5, 4, 3, 2, 1, 0, -1.46]) {
    const r = reach(mag)
    expect(r).toBeGreaterThan(last)
    last = r
    // a pixel before the reach it is still lit (more than the quad's cut), at it, it isn't
    expect(lightAt(mag, r)).toBeLessThan(STAR_CUTOFF)
  }
  expect(lightAt(0, 0)).toBeGreaterThan(lightAt(0, 1))
  // the faintest star's brightest pixel shows, the brightest star's quad stays small
  expect(lightAt(FAINTEST, 0)).toBeGreaterThan(0.012)
  expect(reach(-1.46)).toBeLessThan(24) // the brightest star's quad, where its wing has fallen to the display's darkest
})

test("the projection is stereographic: the centre at the origin, 2·tan(θ/2) at an angle θ, north up, east to the left", () => {
  const m = view(CENTRE.ra, CENTRE.dec)
  const [cx, cy, cz] = direction(CENTRE.ra, CENTRE.dec)
  const centre = project(m, cx, cy, cz)
  expect(centre?.[0]).toBeCloseTo(0, 6)
  expect(centre?.[1]).toBeCloseTo(0, 6)
  for (const theta of [5, 20, 60, 120]) {
    const north = direction(CENTRE.ra, CENTRE.dec + theta)
    const p = project(m, ...north)
    expect(p?.[0]).toBeCloseTo(0, 6)
    expect(p?.[1]).toBeCloseTo(2 * Math.tan((theta * Math.PI) / 360), 5)
  }
  expect(project(m, ...direction(CENTRE.ra + 12, -CENTRE.dec))).toBeNull() // behind the viewer
  // Betelgeuse is up and to the left of Rigel; the belt runs from Alnitak on the left to Mintaka on the right
  const at = (ra: number, dec: number) => project(m, ...direction(ra, dec)) as [number, number]
  const betelgeuse = at(5.919, 7.407)
  const rigel = at(5.242, -8.2)
  expect(betelgeuse[0]).toBeLessThan(rigel[0])
  expect(betelgeuse[1]).toBeGreaterThan(rigel[1])
  expect(at(5.679, -1.943)[0]).toBeLessThan(at(5.533, -0.299)[0])
})

test("the Milky Way's baked band follows the galactic plane: brightest on it, falling as exp(−|sin b|/0.16), dust only mottling it", () => {
  const m = view(CENTRE.ra, CENTRE.dec)
  const span = figureSpan(sky)
  const f = frame(1600, 594, span)
  const size = 256
  const flat = new Uint8Array(size * size * 4).fill(255) // dust at its brightest everywhere: the band alone
  const bake = milkyWay(1600, 900, f, m, flat, size)
  expect(bake.w).toBe(Math.ceil((1600 + 2 * MW_MARGIN) / MW_CELL))
  expect(bake.h).toBe(Math.ceil((900 + 2 * MW_MARGIN) / MW_CELL))
  expect(bake.data).toHaveLength(bake.w * bake.h)
  /** The baked value at a direction, or null off the baked area. */
  const at = (d: readonly number[]) => {
    const p = project(m, d[0], d[1], d[2])
    if (!p) return null
    const x = f.x + p[0] * f.scale
    const y = f.y - p[1] * f.scale
    const i = Math.floor(((x + MW_MARGIN) / (1600 + 2 * MW_MARGIN)) * bake.w)
    const j = Math.floor(((y + MW_MARGIN) / (900 + 2 * MW_MARGIN)) * bake.h)
    return i < 0 || j < 0 || i >= bake.w || j >= bake.h ? null : bake.data[j * bake.w + i]
  }
  const [gx, gy, gz] = GALACTIC_POLE
  const centre = direction(CENTRE.ra, CENTRE.dec)
  const sinb = centre[0] * gx + centre[1] * gy + centre[2] * gz
  // the point of the plane nearest the centre: the centre with its component along the pole taken out
  const onPlane = [centre[0] - sinb * gx, centre[1] - sinb * gy, centre[2] - sinb * gz]
  const len = Math.hypot(...onPlane)
  const plane = onPlane.map((c) => c / len)
  expect(Math.abs(sinb)).toBeGreaterThan(0.2) // Orion stands about 17° off the plane
  expect(at(plane)).toBeGreaterThanOrEqual(250) // on the plane: the band's peak
  expect(at(centre)).toBeCloseTo(255 * Math.exp(-Math.abs(sinb) / 0.16), -1) // the centre, off it by its latitude
  // a point of the plane far along it is as bright as one near, wherever it is on screen
  const along = [plane[1] * gz - plane[2] * gy, plane[2] * gx - plane[0] * gz, plane[0] * gy - plane[1] * gx] // pole × plane
  const further = plane.map((c, k) => Math.cos(0.4) * c + Math.sin(0.4) * along[k])
  const seen = at(further)
  if (seen !== null) expect(seen).toBeGreaterThanOrEqual(245)
  // the dust only dims it: the real noise never makes a texel brighter than the flat one
  const real = milkyWay(1600, 900, f, m, makeNoise(), NOISE_SIZE)
  for (let i = 0; i < real.data.length; i += 37) expect(real.data[i]).toBeLessThanOrEqual(bake.data[i] + 1)
})

test("framing: Orion's figure spans its place in the sky, whatever the screen, and a wide screen shows no more than WIDEST", () => {
  const span = figureSpan(sky)
  expect(span.top).toBeGreaterThan(span.bottom)
  // a phone: the figure fits between its top and bottom marks of the sky above the limb
  const phone = frame(390, 440, span)
  expect(phone.y).toBeCloseTo(FIGURE_TOP * 440 + span.top * phone.scale, 6)
  expect(phone.y - span.bottom * phone.scale).toBeCloseTo(FIGURE_BOTTOM * 440, 6)
  expect(phone.x).toBe(195)
  // a very wide screen would show too much sky: the scale grows to keep within WIDEST across
  const wide = frame(3840, 700, span)
  const half = 3840 / 2 / wide.scale
  expect((4 * Math.atan(half / 2) * 180) / Math.PI).toBeLessThanOrEqual(WIDEST + 1e-6)
  // a desktop: the figure is a few hundred pixels tall
  const desktop = frame(1920, 700, span)
  expect((span.top - span.bottom) * desktop.scale).toBeGreaterThan(300)
  expect((span.top - span.bottom) * desktop.scale).toBeLessThan(700 * 0.8)
})
