// Speech-like levels from nothing but the clock, for the demo page and for screenshots (the engine's `synth` knob):
// syllables at about 4 a second, phrases rising and falling, a pause now and then, the low bands louder than the high.

/**
 * A level and its bands at time `t`.
 * @param t seconds
 * @param bands where the bands go (0..1 each)
 * @returns the level, 0..1
 */
export function synthLevel(t: number, bands: Float32Array): number {
  const syllable = Math.max(0, Math.sin(t * Math.PI * 2 * 4.3)) ** 1.5
  const phrase = 0.55 + 0.45 * Math.sin(t * Math.PI * 2 * 0.37)
  const pause = Math.sin(t * Math.PI * 2 * 0.21) > -0.7 ? 1 : 0.08
  const level = Math.min(1, (0.15 + 0.85 * syllable) * phrase * pause)
  for (let i = 0; i < bands.length; i++) {
    const f = i / bands.length
    bands[i] = Math.min(1, level * (0.9 - f * 0.6) * (0.6 + 0.4 * Math.sin(t * (3 + i * 1.7) + i)))
  }
  return level
}
