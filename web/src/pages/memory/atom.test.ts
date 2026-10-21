import { expect, test } from "vitest"
import { ORBITS, orbitPoint } from "./atom"

test("the Orion node's orbits keep their radius as they turn", () => {
  for (const orbit of ORBITS)
    for (const t of [0, 0.3, 0.77]) expect(Math.hypot(...orbitPoint(orbit, t, 2.5))).toBeCloseTo(orbit.radius, 5)
})
