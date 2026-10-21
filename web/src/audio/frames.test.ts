import { describe, expect, it } from "vitest"
import { decodeVoiceFrame, encodeMicFrame, encodeVoiceFrame } from "./frames"

describe("the microphone's frames", () => {
  it("are [0x03] then the samples as int16 little-endian, rounded and clipped (what orion/server.py reads)", () => {
    const samples = new Float32Array([0, 0.5, -0.25, 1, -1, 1.5, -2])
    const frame = encodeMicFrame(samples)
    expect(frame.byteLength).toBe(1 + 2 * samples.length)
    const view = new DataView(frame)
    expect(view.getUint8(0)).toBe(0x03)
    const got = Array.from({ length: samples.length }, (_, i) => view.getInt16(1 + 2 * i, true))
    expect(got).toEqual([0, 16384, -8192, 32767, -32767, 32767, -32767])
  })

  it("encode a view into a bigger buffer (only its own samples)", () => {
    const big = new Float32Array([9, 9, 0.5, 0.25, 9])
    const frame = encodeMicFrame(big.subarray(2, 4))
    expect(frame.byteLength).toBe(5)
    expect(new DataView(frame).getInt16(1, true)).toBe(16384)
    expect(new DataView(frame).getInt16(3, true)).toBe(8192)
  })
})

describe("his voice's frames", () => {
  /** A frame as RemoteOutput.frame builds it: bytes((2,)) + struct.pack("<I", gen) + int16 LE samples. */
  function serverFrame(gen: number, samples: number[]): ArrayBuffer {
    const bytes = new Uint8Array(5 + 2 * samples.length)
    bytes[0] = 2
    bytes[1] = gen & 0xff
    bytes[2] = (gen >>> 8) & 0xff
    bytes[3] = (gen >>> 16) & 0xff
    bytes[4] = (gen >>> 24) & 0xff
    samples.forEach((s, i) => {
      const u = s & 0xffff
      bytes[5 + 2 * i] = u & 0xff
      bytes[6 + 2 * i] = u >> 8
    })
    return bytes.buffer
  }

  it("decode the server's bytes: generation, then 16-bit samples", () => {
    const decoded = decodeVoiceFrame(serverFrame(258, [0, 1, -1, 32767, -32768, 1000]))
    expect(decoded?.generation).toBe(258)
    expect(Array.from(decoded?.pcm ?? [])).toEqual([0, 1, -1, 32767, -32768, 1000])
  })

  it("carry the largest generation", () => {
    expect(decodeVoiceFrame(serverFrame(0xffffffff, [5]))?.generation).toBe(0xffffffff)
  })

  it("round-trip through encodeVoiceFrame", () => {
    const pcm = new Int16Array(960).map((_, i) => Math.round(10_000 * Math.sin(i / 7)))
    const decoded = decodeVoiceFrame(encodeVoiceFrame(7, pcm))
    expect(decoded?.generation).toBe(7)
    expect(decoded?.pcm).toEqual(pcm)
  })

  it("give samples in a buffer of their own (it can be transferred to the worklet)", () => {
    const decoded = decodeVoiceFrame(serverFrame(1, [1, 2, 3]))
    expect(decoded?.pcm.byteOffset).toBe(0)
    expect(decoded?.pcm.buffer.byteLength).toBe(6)
  })

  it("refuse anything else", () => {
    expect(decodeVoiceFrame(new ArrayBuffer(0))).toBeNull()
    expect(decodeVoiceFrame(serverFrame(1, [1]).slice(0, 4))).toBeNull() // shorter than the header
    expect(decodeVoiceFrame(serverFrame(1, [1, 2]).slice(0, 8))).toBeNull() // half a sample
    expect(decodeVoiceFrame(encodeMicFrame(new Float32Array(4)))).toBeNull() // a microphone frame
  })

  it("may be empty (a header alone)", () => {
    expect(decodeVoiceFrame(serverFrame(3, []))?.pcm.length).toBe(0)
  })
})
