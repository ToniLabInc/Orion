// The voice's binary frames over /ws (orion/server.py, orion/voice/tts.py):
//   microphone → server: [0x03][int16 LE samples, 16 kHz mono]
//   server → speakers:   [0x02][u32 LE generation][int16 LE samples, 24 kHz mono]
import { MIC_FRAME_KIND, VOICE_FRAME_KIND } from "./constants"

const LITTLE_ENDIAN = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1
const VOICE_HEADER = 5

/**
 * A block of the microphone as the frame the server reads: int16, rounded, clipped to −1…1.
 * @param samples - 16 kHz mono samples, −1…1
 * @returns the frame
 */
export function encodeMicFrame(samples: Float32Array): ArrayBuffer {
  const frame = new ArrayBuffer(1 + samples.length * 2)
  const view = new DataView(frame)
  view.setUint8(0, MIC_FRAME_KIND)
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(1 + i * 2, Math.round(s * 32767), true)
  }
  return frame
}

export interface VoiceFrame {
  generation: number
  /** 24 kHz mono samples (their own buffer: it may be transferred). */
  pcm: Int16Array
}

/**
 * A frame of his voice, or null for anything else (another kind, a broken length).
 * @param frame - the binary message
 * @returns its generation and samples
 */
export function decodeVoiceFrame(frame: ArrayBuffer): VoiceFrame | null {
  if (frame.byteLength < VOICE_HEADER || (frame.byteLength - VOICE_HEADER) % 2) return null
  const view = new DataView(frame)
  if (view.getUint8(0) !== VOICE_FRAME_KIND) return null
  const generation = view.getUint32(1, true)
  if (LITTLE_ENDIAN) return { generation, pcm: new Int16Array(frame.slice(VOICE_HEADER)) }
  const pcm = new Int16Array((frame.byteLength - VOICE_HEADER) / 2)
  for (let i = 0; i < pcm.length; i++) pcm[i] = view.getInt16(VOICE_HEADER + i * 2, true)
  return { generation, pcm }
}

/**
 * A frame of his voice as the server makes it (for tests and the demo's stand-in server).
 * @param generation - its generation
 * @param pcm - 24 kHz mono samples
 * @returns the frame
 */
export function encodeVoiceFrame(generation: number, pcm: Int16Array): ArrayBuffer {
  const frame = new ArrayBuffer(VOICE_HEADER + pcm.length * 2)
  const view = new DataView(frame)
  view.setUint8(0, VOICE_FRAME_KIND)
  view.setUint32(1, generation, true)
  for (let i = 0; i < pcm.length; i++) view.setInt16(VOICE_HEADER + i * 2, pcm[i], true)
  return frame
}
