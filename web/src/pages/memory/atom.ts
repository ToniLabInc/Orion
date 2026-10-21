// The atom the map's Orion node is drawn as: three Bohr orbits round his star, as lines. Plain numbers, no three.

const TAU = Math.PI * 2

/** The three orbits: a circle of `radius` lying almost flat (tipped `tilt` radians, so it is seen nearly edge-on, a
 *  thin ellipse, never a ring facing you), turned `roll` radians round the line of sight (0°, 60°, -60°: the atom of
 *  every physics textbook), its electron going round `speed` times as fast as the first, starting `phase` of the way
 *  round, the tilt rocking by `wobble` as it spins. */
export const ORBITS = [
  { radius: 1.62, tilt: 0.26, roll: 0, speed: 1, phase: 0, wobble: 0.05 },
  { radius: 1.68, tilt: 0.26, roll: TAU / 6, speed: 0.83, phase: 0.33, wobble: 0.04 },
  { radius: 1.74, tilt: 0.26, roll: -TAU / 6, speed: 0.71, phase: 0.66, wobble: 0.05 },
] as const

export type Orbit = (typeof ORBITS)[number]

/** How far an orbit is tipped as the atom spins (its rocking). */
const orbitTilt = (orbit: Orbit, spin: number) => orbit.tilt + orbit.wobble * Math.sin(spin * 0.9 + orbit.phase * TAU)

/** How far the whole atom has turned round the vertical as it spins: a slow sway, never a full turn. */
const atomYaw = (spin: number) => 0.24 * Math.sin(spin * 0.21)

/**
 * A point on an orbit, in the atom's space: the circle tipped, rolled and swayed.
 * @param orbit the orbit
 * @param t how far round, 0 to 1
 * @param spin the spin (0 at rest)
 * @returns x, y, z
 */
export function orbitPoint(orbit: Orbit, t: number, spin = 0): [number, number, number] {
  const a = t * TAU
  let x = Math.cos(a) * orbit.radius
  let y = 0
  let z = Math.sin(a) * orbit.radius
  const tilt = orbitTilt(orbit, spin)
  ;[y, z] = [y * Math.cos(tilt) - z * Math.sin(tilt), y * Math.sin(tilt) + z * Math.cos(tilt)] // about x
  ;[x, y] = [x * Math.cos(orbit.roll) - y * Math.sin(orbit.roll), x * Math.sin(orbit.roll) + y * Math.cos(orbit.roll)] // z
  const yaw = atomYaw(spin)
  ;[x, z] = [x * Math.cos(yaw) + z * Math.sin(yaw), -x * Math.sin(yaw) + z * Math.cos(yaw)] // about y
  return [x, y, z]
}
