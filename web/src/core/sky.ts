/**
 * The real sky above the limb: the Yale Bright Star Catalogue as web/scripts/build_stars.py packs it (stars.bin),
 * each star's light from its magnitude, its colour from its B−V, and where it lands on the screen.
 *
 * Light: a star is a point, drawn as a photograph shows one: its light spread by a narrow Gaussian (σ = 0.55 CSS px)
 * and seen through a saturating exposure, 1 − e^(−light). A faint star is a dim point; a bright one saturates into a
 * small disc whose radius grows with the logarithm of its light, white at its heart and coloured at its rim; the very
 * brightest keep faint Moffat wings, as a lens gives them. Its colour: B−V to an effective temperature (Ballesteros
 * 2012), the temperature to a black body's chromaticity (Kim et al. 2002), that to linear sRGB (D65), desaturated a
 * little towards what an eye sees at night.
 *
 * Place: a stereographic projection (conformal: the figure keeps its shape anywhere on the screen) centred on Orion's
 * belt, RA 5h35m, Dec −1°, north up and east to the left as you look up at it; `frame` scales and places it so the
 * figure, club and shield included, fills the sky above the limb. Plain numbers: tested in Node.
 */

/** The catalogue as the engine keeps it, brightest first. */
export interface Catalogue {
  count: number
  /** Unit vectors, J2000 equatorial (x towards RA 0h, z towards the north pole), 3 per star. */
  dirs: Float32Array
  /** Visual magnitude. */
  vmag: Float32Array
  /** B−V colour index. */
  bv: Float32Array
  /** The Orion figure: pairs of indices into the stars, in the order the figure draws itself. */
  lines: Uint16Array
}

const MAGIC = "HZSK"
const DEG = Math.PI / 180

/**
 * Read stars.bin (see build_stars.py for its layout).
 * @param buffer the file
 * @returns the catalogue
 * @throws Error when the file isn't a catalogue of this version, or is cut short
 */
export function decode(buffer: ArrayBuffer): Catalogue {
  const view = new DataView(buffer)
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3))
  if (magic !== MAGIC || view.getUint8(4) !== 1) throw new Error("stars.bin is not a version 1 star catalogue")
  const count = view.getUint16(6, true)
  const lineCount = view.getUint16(8, true)
  const ra = 12
  const dec = ra + count * 2
  const mag = dec + count * 2
  const col = mag + count
  const fig = col + count
  if (buffer.byteLength !== fig + lineCount * 4) throw new Error("stars.bin is cut short or too long")
  const dirs = new Float32Array(count * 3)
  const vmag = new Float32Array(count)
  const bv = new Float32Array(count)
  for (let i = 0; i < count; i++) {
    const a = (view.getUint16(ra + i * 2, true) / 65536) * 2 * Math.PI
    const d = (view.getInt16(dec + i * 2, true) / 32767) * 90 * DEG
    dirs[i * 3] = Math.cos(d) * Math.cos(a)
    dirs[i * 3 + 1] = Math.cos(d) * Math.sin(a)
    dirs[i * 3 + 2] = Math.sin(d)
    vmag[i] = view.getUint8(mag + i) / 25 - 2
    bv[i] = view.getInt8(col + i) / 50
  }
  const lines = new Uint16Array(lineCount * 2)
  for (let i = 0; i < lineCount * 2; i++) lines[i] = view.getUint16(fig + i * 2, true)
  return { count, dirs, vmag, bv, lines }
}

/**
 * A direction on the sky.
 * @param raHours right ascension, hours
 * @param decDeg declination, degrees
 * @returns the unit vector (J2000 equatorial)
 */
export function direction(raHours: number, decDeg: number): [number, number, number] {
  const a = raHours * 15 * DEG
  const d = decDeg * DEG
  return [Math.cos(d) * Math.cos(a), Math.cos(d) * Math.sin(a), Math.sin(d)]
}

// ---------------------------------------------------------------------------------------------------- colour
/**
 * A star's effective temperature from its B−V (Ballesteros 2012, a black body's fit to real stars).
 * @param bv the colour index, clamped to −0.4..2.5
 * @returns kelvin
 */
export function temperature(bv: number): number {
  const b = Math.min(2.5, Math.max(-0.4, bv))
  return 4600 * (1 / (0.92 * b + 1.7) + 1 / (0.92 * b + 0.62))
}

/**
 * The CIE 1931 chromaticity of a black body (Kim et al. 2002's cubic splines of the Planckian locus).
 * @param kelvin the temperature, clamped to the splines' 1667–25 000 K
 * @returns x and y
 */
export function planck(kelvin: number): [number, number] {
  const t = Math.min(25000, Math.max(1667, kelvin))
  const t3 = 1e9 / t ** 3
  const t2 = 1e6 / t ** 2
  const t1 = 1e3 / t
  const x =
    t <= 4000
      ? -0.2661239 * t3 - 0.2343589 * t2 + 0.8776956 * t1 + 0.17991
      : -3.0258469 * t3 + 2.1070379 * t2 + 0.2226347 * t1 + 0.24039
  const y =
    t <= 2222
      ? -1.1063814 * x ** 3 - 1.3481102 * x ** 2 + 2.18555832 * x - 0.20219683
      : t <= 4000
        ? -0.9549476 * x ** 3 - 1.37418593 * x ** 2 + 2.09137015 * x - 0.16748867
        : 3.081758 * x ** 3 - 5.8733867 * x ** 2 + 3.75112997 * x - 0.37001483
  return [x, y]
}

/** How much of its own colour a star keeps (the rest is white): the eye sees star colours paler than a camera. */
export const STAR_SATURATION = 0.72

/**
 * A star's colour in linear sRGB at a luminance of 1: the black body of its temperature (white at D65), negative
 * channels clipped, then mixed towards white by 1 − STAR_SATURATION.
 * @param bv the colour index
 * @returns r, g, b (linear; Y = 0.2126 r + 0.7152 g + 0.0722 b = 1)
 */
export function starColor(bv: number): [number, number, number] {
  const [x, y] = planck(temperature(bv))
  const X = x / y
  const Z = (1 - x - y) / y
  let r = Math.max(0, 3.2404542 * X - 1.5371385 - 0.4985314 * Z)
  let g = Math.max(0, -0.969266 * X + 1.8760108 + 0.041556 * Z)
  let b = Math.max(0, 0.0556434 * X - 0.2040259 + 1.0572252 * Z)
  const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
  r = 1 + (r / luma - 1) * STAR_SATURATION
  g = 1 + (g / luma - 1) * STAR_SATURATION
  b = 1 + (b / luma - 1) * STAR_SATURATION
  const y2 = 0.2126 * r + 0.7152 * g + 0.0722 * b
  return [r / y2, g / y2, b / y2]
}

// ---------------------------------------------------------------------------------------------------- light
/** The faintest magnitude in the catalogue (build_stars.py's limit). */
export const FAINTEST = 6.5
/** The Gaussian a star's light is spread by, CSS px. */
export const STAR_SIGMA = 0.55
/** The exposure: the faintest star's peak light before the curve (its pixel then shows about 1 − e^−x of white). */
export const STAR_EXPOSURE = 0.018
/** The wings' share of a star's light, and their width (CSS px): only the brightest show them. */
export const WING_SHARE = 0.035
export const WING_WIDTH = 1.7
/** Light below this is nothing (a star's quad stops where it falls to it): about two levels of an 8-bit display's darkest, so no edge shows. */
export const STAR_CUTOFF = 0.0006

/**
 * A star's light relative to the faintest the catalogue keeps (Pogson's ratio: 5 magnitudes are a factor of 100).
 * @param vmag the visual magnitude
 * @returns the flux, 1 at FAINTEST
 */
export const flux = (vmag: number): number => 10 ** (-0.4 * (vmag - FAINTEST))

/**
 * How far from its centre a star still shows (its quad's half side), CSS px: where its Gaussian and its wings have
 * both fallen below what a pixel shows.
 * @param vmag the visual magnitude
 * @returns the radius
 */
export function reach(vmag: number): number {
  const e = STAR_EXPOSURE * flux(vmag)
  const core = STAR_SIGMA * Math.sqrt(2 * Math.log(Math.max(1.0001, e / STAR_CUTOFF)))
  // the wings: e·share / (1 + (r/w)²)^1.5 = STAR_CUTOFF
  const wing = e * WING_SHARE
  const wings = wing > STAR_CUTOFF ? WING_WIDTH * Math.sqrt((wing / STAR_CUTOFF) ** (2 / 3) - 1) : 0
  return Math.max(core, wings) + 1
}

/**
 * The light a star leaves at a distance from its centre before the exposure (what the shader computes, for tests
 * and the still): its Gaussian sampled at a point, plus its wings.
 * @param vmag the visual magnitude
 * @param r the distance, CSS px
 * @returns the light (1 − e^−light is what shows)
 */
export function lightAt(vmag: number, r: number): number {
  const e = STAR_EXPOSURE * flux(vmag)
  return e * Math.exp(-(r * r) / (2 * STAR_SIGMA * STAR_SIGMA)) + (e * WING_SHARE) / (1 + (r / WING_WIDTH) ** 2) ** 1.5
}

// ---------------------------------------------------------------------------------------------------- place
/** Where the projection is centred: Orion's belt. */
export const CENTRE = { ra: 5 + 35 / 60, dec: -1 }
/** M42, the Orion Nebula: its centre (the Trapezium's neighbourhood) and its size, degrees (one σ across, up). */
export const M42 = { ra: 5 + 35 / 60 + 17.3 / 3600, dec: -(5 + 23 / 60 + 28 / 3600), sx: 0.32, sy: 0.42 }

/** A rotation from J2000 equatorial to the view: x to the right (west), y up (north), z towards the centre. */
export type View = Float32Array // 9 numbers, row-major

/**
 * The view centred on a direction, north up and east to the left (the sky seen from inside).
 * @param raHours the centre's right ascension, hours
 * @param decDeg its declination, degrees
 * @returns the rotation
 */
export function view(raHours: number, decDeg: number): View {
  const a = raHours * 15 * DEG
  const d = decDeg * DEG
  const c = [Math.cos(d) * Math.cos(a), Math.cos(d) * Math.sin(a), Math.sin(d)]
  const east = [-Math.sin(a), Math.cos(a), 0]
  const north = [-Math.sin(d) * Math.cos(a), -Math.sin(d) * Math.sin(a), Math.cos(d)]
  return new Float32Array([-east[0], -east[1], -east[2], north[0], north[1], north[2], c[0], c[1], c[2]])
}

/**
 * The stereographic projection of a direction, in radians at the centre (x right, y up): 2·tan(θ/2) from the centre
 * along the great circle through it.
 * @param m the view
 * @param x the direction's x
 * @param y its y
 * @param z its z
 * @returns the plane's x and y, or null for the direction opposite the centre
 */
export function project(m: View, x: number, y: number, z: number): [number, number] | null {
  const qx = m[0] * x + m[1] * y + m[2] * z
  const qy = m[3] * x + m[4] * y + m[5] * z
  const qz = m[6] * x + m[7] * y + m[8] * z
  if (qz <= -0.999) return null
  const k = 2 / (1 + qz)
  return [qx * k, qy * k]
}

// ---------------------------------------------------------------------------------------------------- the Milky Way
/** The north galactic pole, J2000 (a unit vector): the galactic plane is every direction at right angles to it. */
export const GALACTIC_POLE: readonly [number, number, number] = [-0.8676661, -0.1980764, 0.4559838]
/** The band's half width as a sine of galactic latitude (about 9°), and the faintest and brightest it gets (linear light). */
const MW_WIDTH = 0.16
export const MW_PEAK = 0.0053
/** The baked band covers the screen and this many CSS px beyond each edge (the sky's drift and lean shift it), at this many CSS px a texel. */
export const MW_MARGIN = 32
export const MW_CELL = 5

/** A baked image, one byte a texel. */
export interface Baked {
  data: Uint8Array
  w: number
  h: number
}

/**
 * Bilinear value of one channel of a tiled RGBA8 noise texture at a texture coordinate, as the GPU would sample it
 * with REPEAT and LINEAR.
 */
function sampleNoise(noise: Uint8Array, size: number, u: number, v: number, channel: number): number {
  const x = u * size - 0.5
  const y = v * size - 0.5
  const x0 = Math.floor(x)
  const y0 = Math.floor(y)
  const fx = x - x0
  const fy = y - y0
  const at = (i: number, j: number) =>
    noise[((((j % size) + size) % size) * size + (((i % size) + size) % size)) * 4 + channel]
  const top = at(x0, y0) + (at(x0 + 1, y0) - at(x0, y0)) * fx
  const bottom = at(x0, y0 + 1) + (at(x0 + 1, y0 + 1) - at(x0, y0 + 1)) * fx
  return (top + (bottom - top) * fy) / 255
}

/**
 * The Milky Way as a faint band, baked once per layout instead of worked out for every pixel every frame: its
 * brightness falls off with the sky's own galactic latitude (a plane through the sky, in J2000: `exp(−|sin b|/0.16)`),
 * mottled by dust (a low-frequency noise, in the sky's plane). Value 255 is MW_PEAK of light.
 * @param width the screen's width, CSS px
 * @param height its height, CSS px
 * @param frame where the sky sits on the screen
 * @param m the view
 * @param noise the noise texture's pixels (noise.ts), whose green channel is the dust
 * @param noiseSize the noise texture's side
 * @returns the band's texels (row 0 at the top of the screen, from MW_MARGIN px above it) and its size
 */
export function milkyWay(
  width: number,
  height: number,
  frame: Frame,
  m: View,
  noise: Uint8Array,
  noiseSize: number,
): Baked {
  const w = Math.ceil((width + 2 * MW_MARGIN) / MW_CELL)
  const h = Math.ceil((height + 2 * MW_MARGIN) / MW_CELL)
  const data = new Uint8Array(w * h)
  const [gx, gy, gz] = GALACTIC_POLE
  for (let j = 0; j < h; j++) {
    const py = -MW_MARGIN + ((j + 0.5) * (height + 2 * MW_MARGIN)) / h
    for (let i = 0; i < w; i++) {
      const px = -MW_MARGIN + ((i + 0.5) * (width + 2 * MW_MARGIN)) / w
      // the pixel's direction: the inverse of the stereographic projection, then out of the view
      const x = (px - frame.x) / frame.scale
      const y = (frame.y - py) / frame.scale
      const r2 = x * x + y * y
      const k = 1 / (4 + r2)
      const qx = 4 * x * k
      const qy = 4 * y * k
      const qz = (4 - r2) * k
      const sinb =
        (qx * m[0] + qy * m[3] + qz * m[6]) * gx +
        (qx * m[1] + qy * m[4] + qz * m[7]) * gy +
        (qx * m[2] + qy * m[5] + qz * m[8]) * gz
      const mottled = 0.35 + 0.9 * sampleNoise(noise, noiseSize, x * 1.9 + 0.17, y * 1.9 + 0.17, 1)
      data[j * w + i] = Math.round(255 * Math.min(1, (Math.exp(-Math.abs(sinb) / MW_WIDTH) * mottled) / 1.25))
    }
  }
  return { data, w, h }
}

/** Where the sky sits on the screen: CSS px per unit of the plane (a radian at the centre), and the centre's place. */
export interface Frame {
  scale: number
  x: number
  y: number
}

/** The figure fills the sky between these fractions of its height, from the top. */
export const FIGURE_TOP = 0.11
export const FIGURE_BOTTOM = 0.83
/** The widest the screen's width may show, degrees (beyond it the projection stretches the edges too much). */
export const WIDEST = 125

/**
 * The figure's extent in the plane at the centre's view (its stars' highest and lowest y, radians).
 * @param sky the catalogue
 * @returns the top and the bottom
 */
export function figureSpan(sky: Catalogue): { top: number; bottom: number } {
  const m = view(CENTRE.ra, CENTRE.dec)
  let top = Number.NEGATIVE_INFINITY
  let bottom = Number.POSITIVE_INFINITY
  for (const i of sky.lines) {
    const p = project(m, sky.dirs[i * 3], sky.dirs[i * 3 + 1], sky.dirs[i * 3 + 2])
    if (!p) continue
    top = Math.max(top, p[1])
    bottom = Math.min(bottom, p[1])
  }
  return { top, bottom }
}

/**
 * Scale and place the sky for a screen: the figure spans FIGURE_TOP..FIGURE_BOTTOM of the sky's height (from the
 * screen's top to the limb's), unless that would show more than WIDEST degrees across the width; its centre is
 * the screen's horizontal middle.
 * @param width the screen's width, CSS px
 * @param horizon the limb's top, CSS px from the screen's top
 * @param span the figure's extent (figureSpan)
 * @returns the frame
 */
export function frame(width: number, horizon: number, span: { top: number; bottom: number }): Frame {
  const sky = Math.max(1, horizon)
  const fit = ((FIGURE_BOTTOM - FIGURE_TOP) * sky) / (span.top - span.bottom)
  const widest = width / (4 * Math.tan((WIDEST * DEG) / 4)) // half the width over the plane's 2·tan(θ/2) at θ = WIDEST/2
  const scale = Math.max(fit, widest)
  return { scale, x: width / 2, y: FIGURE_TOP * sky + span.top * scale }
}
