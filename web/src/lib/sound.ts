// The HUD's sounds, made on the spot with Web Audio (no files to load): a rising, glassy shimmer when a card arrives, a
// falling one when it goes, a soft tick as the palette opens or closes and a fainter one for a gesture, and two rising
// ticks when he asks before a risky action. Quiet and short. The browser keeps audio shut until the page has been
// clicked or typed on, so nothing sounds before that; nothing at all while hud.sounds is off. (His wake has its own
// chime, on his own speakers.)
import { useLive } from "./store"

export type Sound = "summon" | "dismiss" | "open" | "close" | "tick" | "ask"

let context: AudioContext | null = null
let out: AudioNode | null = null

/** A click, a tap or a key press opens the audio (browsers allow it only then), or wakes it after the tab slept. */
function unlock(): void {
  if (context) {
    if (context.state === "suspended") void context.resume().catch(() => {})
    return
  }
  try {
    context = new AudioContext({ latencyHint: "interactive" })
    const soften = context.createBiquadFilter() // no harsh highs
    soften.type = "lowpass"
    soften.frequency.value = 7000
    const level = context.createGain()
    level.gain.value = 0.16
    soften.connect(level).connect(context.destination)
    out = soften
  } catch {
    context = null // no Web Audio here: silence
  }
}
if (typeof window !== "undefined")
  for (const type of ["pointerdown", "pointerup", "keydown"])
    addEventListener(type, unlock, { capture: true, passive: true })

/** One voice of a sound: an oscillator gliding from one pitch to another under a quick swell and a long fade. */
interface Voice {
  type: OscillatorType
  from: number // Hz
  to: number
  at?: number // s after the sound starts
  attack: number // s
  decay: number // s
  gain: number
  detune?: number // cents
}

const SOUNDS: Record<Sound, Voice[]> = {
  summon: [
    { type: "triangle", from: 420, to: 980, attack: 0.012, decay: 0.32, gain: 0.5 },
    { type: "sine", from: 840, to: 1960, attack: 0.02, decay: 0.26, gain: 0.18, detune: 8 },
    { type: "sine", from: 2960, to: 2900, at: 0.06, attack: 0.004, decay: 0.5, gain: 0.06 }, // the glass's ring
  ],
  dismiss: [
    { type: "triangle", from: 760, to: 300, attack: 0.01, decay: 0.22, gain: 0.4 },
    { type: "sine", from: 1520, to: 600, attack: 0.01, decay: 0.18, gain: 0.12 },
  ],
  open: [
    { type: "sine", from: 1250, to: 1400, attack: 0.003, decay: 0.07, gain: 0.18 },
    { type: "sine", from: 190, to: 150, attack: 0.004, decay: 0.09, gain: 0.25 },
  ],
  close: [{ type: "sine", from: 1100, to: 950, attack: 0.003, decay: 0.06, gain: 0.15 }],
  tick: [{ type: "sine", from: 2400, to: 2300, attack: 0.002, decay: 0.03, gain: 0.05 }],
  ask: [
    { type: "sine", from: 1320, to: 1280, attack: 0.003, decay: 0.06, gain: 0.12 },
    { type: "sine", from: 1760, to: 1720, at: 0.11, attack: 0.003, decay: 0.08, gain: 0.12 },
  ],
}

/**
 * Play a sound (nothing before the first click or key press, while the tab is hidden, or with hud.sounds off).
 * @param sound which
 */
export function play(sound: Sound): void {
  const ctx = context
  if (!ctx || !out || document.hidden || useLive.getState().hud?.sounds === false) return
  if (ctx.state === "suspended") void ctx.resume().catch(() => {})
  const start = ctx.currentTime + 0.01
  for (const v of SOUNDS[sound]) {
    const at = start + (v.at ?? 0)
    const end = at + v.attack + v.decay
    const env = ctx.createGain()
    env.gain.setValueAtTime(0.0001, at)
    env.gain.exponentialRampToValueAtTime(v.gain, at + v.attack)
    env.gain.exponentialRampToValueAtTime(0.0001, end)
    env.connect(out)
    const osc = ctx.createOscillator()
    osc.type = v.type
    osc.detune.value = v.detune ?? 0
    osc.connect(env)
    osc.frequency.setValueAtTime(v.from, at)
    osc.frequency.exponentialRampToValueAtTime(v.to, at + v.attack + v.decay * 0.6)
    osc.start(at)
    osc.stop(end + 0.05)
  }
}
