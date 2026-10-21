// Seeded random numbers for what the Core builds on the CPU (the constellation, the morphs, the first particles): the
// same seed, the same swarm, so tests and screenshots repeat.

/**
 * mulberry32: a quick random sequence, the same for the same seed.
 * @param seed where it starts
 * @returns the next number, 0 (included) to 1 (excluded)
 */
export function mulberry32(seed: number): () => number {
  let state = seed >>> 0 || 1
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
