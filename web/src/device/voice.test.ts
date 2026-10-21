import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { VoiceAudio, VoiceAudioEvents } from "@/audio/engine"
import { encodeVoiceFrame } from "@/audio/frames"
import type { PlayerStats } from "@/audio/player"
import type { DeviceSend } from "./index"
import { VOICE_TIMING, VoiceCarrier } from "./voice"

class FakeAudio implements VoiceAudio {
  played: number[] = [] // the first sample of each frame played
  flushes = 0
  resumes = 0
  closed = false
  readonly context = {} as AudioContext
  play(pcm: Int16Array) {
    this.played.push(pcm[0])
  }
  flush() {
    this.flushes++
  }
  resume() {
    this.resumes++
  }
  stats() {
    return Promise.resolve({} as PlayerStats)
  }
  close() {
    this.closed = true
  }
}

/** A sample as the microphone's frames carry it (int16, rounded). */
const q = (value: number) => Math.round(value * 32767)

/** A stand-in for the server's connection, and the audio the carrier opens (when the test says so). */
function setup(why: () => string | null = () => null) {
  const json: Record<string, unknown>[] = []
  const frames: Int16Array[] = []
  const net = { online: true }
  const send: DeviceSend = {
    json: (message) => net.online && json.push(message) > 0,
    binary: (data) => net.online && frames.push(new Int16Array(data.slice(1))) > 0, // [0x03][int16 LE]
  }
  const audios: FakeAudio[] = []
  let events: VoiceAudioEvents | undefined
  let open: (() => void) | undefined
  let fail: ((error: Error) => void) | undefined
  const opener = vi.fn((e: VoiceAudioEvents) => {
    events = e
    const audio = new FakeAudio()
    audios.push(audio)
    return new Promise<VoiceAudio>((resolve, reject) => {
      open = () => resolve(audio)
      fail = reject
    })
  })
  const voice = new VoiceCarrier(send, opener, why)
  const mic = {
    /** A 40 ms block whose samples all hold `value` (so the test can tell blocks apart). */
    block: (value: number) => events?.block(new Float32Array(640).fill(value)),
    level: (rms: number) => events?.level(rms),
    ended: () => events?.ended("unplugged"),
  }
  /** Take the voice, let the audio open, and settle. */
  const take = async () => {
    const taking = voice.take()
    open?.()
    await taking
  }
  return {
    voice,
    json,
    frames,
    net,
    /** The audio opened last. */
    get audio(): FakeAudio {
      const audio = audios.at(-1)
      if (!audio) throw new Error("no audio was opened")
      return audio
    },
    mic,
    take,
    opener,
    open: () => open?.(),
    fail: (error: Error) => fail?.(error),
    sent: (type: string) => json.filter((m) => m.type === type),
  }
}

const takes = (json: Record<string, unknown>[]) => json.filter((m) => m.type === "voice_take").map((m) => m.on)

beforeEach(() => {
  vi.spyOn(console, "warn").mockImplementation(() => {})
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe("taking the voice", () => {
  it("asks the server, and sends no microphone until it agrees", async () => {
    const t = setup()
    await t.take()
    expect(takes(t.json)).toEqual([true])
    expect(t.voice.state).toBe("waiting")
    expect(t.voice.carrying).toBe(false)
    t.mic.block(0.1)
    t.mic.block(0.2)
    expect(t.frames).toHaveLength(0)
    t.voice.holder(true)
    expect(t.voice.carrying).toBe(true)
    // what was said while waiting goes first, in order, then the live blocks
    t.mic.block(0.3)
    expect(t.frames.map((f) => f[0])).toEqual([0.1, 0.2, 0.3].map(q))
    expect(t.frames[0]).toHaveLength(640)
  })

  it("keeps only the last second while it waits", async () => {
    const t = setup()
    await t.take()
    for (let i = 0; i < 50; i++) t.mic.block(i / 100)
    t.voice.holder(true)
    expect(t.frames).toHaveLength(VOICE_TIMING.earlyKeepMs / 40)
    expect(t.frames[0][0]).toBe(q(0.25))
    expect(t.frames.at(-1)?.[0]).toBe(q(0.49))
  })

  it("ignores a word about someone else while it waits (older news, or the take is still loading)", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(false)
    expect(t.voice.state).toBe("waiting")
    expect(t.audio.closed).toBe(false)
    t.voice.holder(true)
    expect(t.voice.carrying).toBe(true)
  })

  it("opens the audio inside the call (the user's click), once however often it is called", async () => {
    const t = setup()
    const first = t.voice.take()
    const second = t.voice.take()
    expect(t.opener).toHaveBeenCalledTimes(1)
    expect(t.voice.state).toBe("opening")
    t.open()
    await Promise.all([first, second])
    await t.voice.take() // waiting: nothing new
    t.voice.holder(true)
    await t.voice.take() // carrying: nothing new
    expect(takes(t.json)).toEqual([true])
    expect(t.opener).toHaveBeenCalledTimes(1)
  })

  it("refuses with the reason when this browser can't", async () => {
    const t = setup(() => "Open the HUD over HTTPS (or localhost) to talk from this device.")
    await expect(t.voice.take()).rejects.toThrow("Open the HUD over HTTPS")
    expect(t.opener).not.toHaveBeenCalled()
  })

  it("passes on why the microphone didn't open, and can try again", async () => {
    const t = setup()
    const taking = t.voice.take()
    t.fail(new Error("The microphone is blocked for this site: allow it in the browser's address bar."))
    await expect(taking).rejects.toThrow("The microphone is blocked")
    expect(t.voice.state).toBe("off")
    expect(t.json).toHaveLength(0)
    await t.take()
    expect(t.voice.state).toBe("waiting")
  })

  it("can't ask while Orion isn't connected: the microphone closes again", async () => {
    const t = setup()
    t.net.online = false
    await expect(t.take()).rejects.toThrow("isn't connected")
    expect(t.audio.closed).toBe(true)
    expect(t.voice.state).toBe("off")
  })

  it("gives up when the server never agrees (it said why in a toast)", async () => {
    vi.useFakeTimers()
    const t = setup()
    await t.take()
    vi.advanceTimersByTime(VOICE_TIMING.takeWaitMs + 1)
    expect(t.voice.state).toBe("off")
    expect(t.audio.closed).toBe(true)
    expect(takes(t.json)).toEqual([true, false])
  })

  it("says no to a late agreement to a take it gave up", () => {
    const t = setup()
    t.voice.holder(true)
    expect(takes(t.json)).toEqual([false])
  })
})

describe("letting go", () => {
  it("tells the server and closes the microphone and the player", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.voice.release()
    expect(takes(t.json)).toEqual([true, false])
    expect(t.audio.closed).toBe(true)
    t.mic.block(0.5)
    expect(t.frames).toHaveLength(0)
  })

  it("lets go here when the server moved the voice elsewhere (it already let go of this tab)", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.voice.holder(false)
    expect(t.voice.state).toBe("off")
    expect(t.audio.closed).toBe(true)
    expect(takes(t.json)).toEqual([true])
    t.mic.block(0.5) // a late block from the closing worklet
    expect(t.frames).toHaveLength(0)
  })

  it("while the microphone opens: closes it once open, says AbortError, and never asked the server", async () => {
    const t = setup()
    const taking = t.voice.take()
    t.voice.release()
    t.open()
    await expect(taking).rejects.toMatchObject({ name: "AbortError" })
    expect(t.audio.closed).toBe(true)
    expect(t.json).toHaveLength(0)
    expect(t.voice.state).toBe("off")
  })

  it("when the microphone stops by itself (unplugged)", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.mic.ended()
    expect(t.voice.state).toBe("off")
    expect(takes(t.json)).toEqual([true, false])
  })
})

describe("talking", () => {
  it("is sent at once while carrying, and wakes a suspended context", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.voice.talk()
    expect(t.sent("talk")).toHaveLength(1)
    expect(t.audio.resumes).toBeGreaterThan(0)
  })

  it("waits for the server's agreement, then goes before what was said meanwhile", async () => {
    const t = setup()
    const taking = t.voice.take()
    t.voice.talk() // the talk button: take and talk in one click
    t.open()
    await taking
    t.mic.block(0.7)
    expect(t.sent("talk")).toHaveLength(0)
    const order: string[] = []
    const json = t.json.length
    t.voice.holder(true)
    for (const m of t.json.slice(json)) order.push(String(m.type))
    expect(order).toEqual(["talk"])
    expect(t.frames.map((f) => f[0])).toEqual([q(0.7)])
  })

  it("is nothing when this tab doesn't carry the voice", () => {
    const t = setup()
    t.voice.talk()
    expect(t.json).toHaveLength(0)
  })
})

describe("a dropped connection", () => {
  it("holds the microphone, still carrying for the HUD, and sends nothing meanwhile", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.net.online = false
    t.mic.block(0.1) // can't be sent: the connection is gone
    expect(t.voice.state).toBe("lost")
    expect(t.voice.carrying).toBe(true)
    expect(t.audio.closed).toBe(false)
    t.voice.talk()
    t.mic.block(0.2)
    expect(t.frames).toHaveLength(0)
  })

  it("takes the voice again on the fresh init, when nobody has it", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.net.online = false
    t.mic.block(0.1)
    t.net.online = true
    // the HUD on the new connection's init (voice_holder: nobody): takeVoice, then holder
    await t.voice.take()
    t.voice.holder(false)
    expect(t.voice.state).toBe("waiting")
    expect(takes(t.json)).toEqual([true, true])
    expect(t.opener).toHaveBeenCalledTimes(1) // the microphone stayed open
    t.voice.holder(true)
    expect(t.voice.carrying).toBe(true)
    t.mic.block(0.3)
    expect(t.frames.map((f) => f[0])).toEqual([q(0.3)])
  })

  it("lets go when someone else took the voice meanwhile", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.net.online = false
    t.mic.block(0.1)
    t.net.online = true
    t.voice.holder(false) // the init names another device: the HUD didn't take it back
    expect(t.voice.state).toBe("off")
    expect(t.audio.closed).toBe(true)
    expect(takes(t.json)).toEqual([true])
  })

  it("lets go when it doesn't come back", async () => {
    vi.useFakeTimers()
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.net.online = false
    t.mic.block(0.1)
    vi.advanceTimersByTime(VOICE_TIMING.lostWaitMs + 1)
    expect(t.voice.state).toBe("off")
    expect(t.audio.closed).toBe(true)
  })
})

describe("the microphone's level", () => {
  it("is told while carrying, smoothed and scaled like the server's, and drops to 0 at the end", async () => {
    const t = setup()
    const levels: number[] = []
    t.voice.onLevel((v) => levels.push(v))
    await t.take()
    t.mic.level(0.1) // waiting: not told
    expect(levels).toEqual([])
    t.voice.holder(true)
    t.mic.level(0.1)
    t.mic.level(0.1)
    t.mic.level(0)
    expect(levels[0]).toBeCloseTo(0.36) // 0.1 × 6, 60 % of the way at once
    expect(levels[1]).toBeGreaterThan(levels[0])
    expect(levels[2]).toBeLessThan(levels[1])
    expect(levels[2]).toBeGreaterThan(0) // it falls slower than it rises
    t.voice.release()
    expect(levels.at(-1)).toBe(0)
  })
})

describe("his voice", () => {
  const frame = (gen: number, first: number) => encodeVoiceFrame(gen, new Int16Array([first, 0, 0]))

  it("plays from the moment the take is asked (frames can come before the server's word)", async () => {
    const t = setup()
    t.voice.play(frame(0, 1)) // not taking: nothing to play on
    await t.take()
    t.voice.play(frame(0, 2))
    t.voice.play(frame(0, 3))
    expect(t.audio.played).toEqual([2, 3])
  })

  it("drops what is held for a newer generation, and drops late frames of an older one", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.voice.play(frame(1, 1))
    t.voice.play(frame(2, 2))
    t.voice.play(frame(1, 3))
    expect(t.audio.played).toEqual([1, 2])
    expect(t.audio.flushes).toBe(1)
    t.voice.flush(3) // `speaker {gen: 3}`
    expect(t.audio.flushes).toBe(2)
    t.voice.flush(3)
    expect(t.audio.flushes).toBe(2)
    t.voice.play(frame(2, 4))
    t.voice.play(frame(3, 5))
    expect(t.audio.played).toEqual([1, 2, 5])
  })

  it("plays the fresh output after letting go and taking the voice again at once", async () => {
    const t = setup()
    await t.take()
    t.voice.holder(true)
    t.voice.play(frame(4, 1)) // he speaks
    t.voice.release()
    await t.take() // the server's new output numbers from 0; the old one's tail is still in flight
    t.voice.flush(5) // the old output's stop, heard before any frame of this take: ignored
    t.voice.play(frame(0, 2))
    t.voice.holder(true)
    t.voice.play(frame(0, 3))
    expect(t.audio.played).toEqual([2, 3])
  })

  it("ignores what isn't a frame of his voice", async () => {
    const t = setup()
    await t.take()
    t.voice.play(new ArrayBuffer(3))
    t.voice.play(new Uint8Array([1, 0, 0, 0, 0, 0, 0]).buffer)
    expect(t.audio.played).toEqual([])
  })
})
