import { afterEach, describe, expect, it, vi } from "vitest"
import type { DeviceRequest } from "./index"
import { BrowserDevice, type DeviceEnv } from "./link"

const FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"

function track() {
  const listeners = new Map<string, () => void>()
  return {
    stop: vi.fn(),
    addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
    /** The browser's own "Stop sharing". */
    end: () => listeners.get("ended")?.(),
  }
}

function stream(t: ReturnType<typeof track>) {
  return { getTracks: () => [t], getVideoTracks: () => [t] } as unknown as MediaStream
}

function domError(name: string, message: string) {
  return new DOMException(message, name)
}

/** A browser that can share and copy, as far as the link can tell; `opts` takes things away. */
function setup(opts: { secure?: boolean; screen?: boolean; video?: [number, number] } = {}) {
  const sent: Record<string, unknown>[] = []
  const screenTrack = track()
  const cameraTrack = track()
  const media = {
    getDisplayMedia: opts.screen === false ? undefined : vi.fn(async () => stream(screenTrack)),
    getUserMedia: vi.fn(async () => stream(cameraTrack)),
    enumerateDevices: async () => [{ kind: "audioinput" }, { kind: "videoinput" }],
  }
  const [width, height] = opts.video ?? [3200, 1800]
  const video = {
    muted: false,
    playsInline: false,
    autoplay: false,
    srcObject: null as unknown,
    paused: false,
    readyState: 4,
    videoWidth: width,
    videoHeight: height,
    play: vi.fn(() => Promise.resolve()),
    pause: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }
  const drawn: { width: number; height: number }[] = []
  const g = { drawImage: vi.fn(), imageSmoothingEnabled: false, imageSmoothingQuality: "low" }
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => g,
    toDataURL: vi.fn((type: string, quality: number) => {
      drawn.push({ width: canvas.width, height: canvas.height })
      return `data:${type};q=${quality};base64,/9j/AAAA`
    }),
  }
  const clipboard = { readText: vi.fn(async () => "copied text"), writeText: vi.fn(async (_text: string) => {}) }
  const env: DeviceEnv = {
    globals: {
      isSecureContext: opts.secure ?? true,
      navigator: { userAgent: FIREFOX, mediaDevices: media, clipboard, maxTouchPoints: 0 },
      AudioWorkletNode: class {},
    },
    openAudio: vi.fn(() => Promise.reject(new Error("no audio here"))),
    share: {
      mediaDevices: () => media as unknown as MediaDevices,
      video: () => video as unknown as HTMLVideoElement,
      canvas: () => canvas as unknown as HTMLCanvasElement,
    },
    clipboard: () => clipboard as unknown as Clipboard,
  }
  const link = new BrowserDevice({ json: (m) => sent.push(m) > 0, binary: () => true }, env)
  const ask = (kind: DeviceRequest["kind"], extra: Partial<DeviceRequest> = {}) =>
    link.handle({ id: "r1", kind, ...extra })
  return { link, sent, media, video, canvas, g, drawn, clipboard, screenTrack, cameraTrack, ask, env }
}

const sharing = (sent: Record<string, unknown>[]) => sent.filter((m) => m.type === "sharing").map((m) => m.what)

afterEach(() => {
  vi.restoreAllMocks()
})

describe("what the device is", () => {
  it("names itself and offers what works here", async () => {
    const t = setup()
    await Promise.resolve() // the camera's listing
    expect(t.link.name()).toBe("Firefox on Linux")
    expect(t.link.caps()).toEqual(["mic", "speaker", "screen", "camera", "clipboard"])
  })

  it("can't carry the voice outside a secure context, and says how to fix it", async () => {
    const t = setup({ secure: false })
    await expect(t.link.takeVoice()).rejects.toThrow("Open the HUD over HTTPS (or localhost)")
    expect(t.env.openAudio).not.toHaveBeenCalled()
  })
})

describe("frames", () => {
  it("are refused when nothing is shared", async () => {
    const t = setup()
    await expect(t.ask("frame", { what: "screen" })).rejects.toThrow("the screen isn't shared")
    await expect(t.ask("frame", { what: "camera" })).rejects.toThrow("the camera isn't shared")
    await expect(t.ask("frame")).rejects.toThrow("the screen isn't shared")
  })

  it("are JPEG data URLs of the current picture, the longest side at most 1600 px", async () => {
    const t = setup()
    await t.link.share("screen")
    const answer = await t.ask("frame", { what: "screen" })
    expect(answer.image).toMatch(/^data:image\/jpeg;q=0.85;base64,/)
    expect(t.drawn).toEqual([{ width: 1600, height: 900 }])
    expect(t.g.drawImage).toHaveBeenCalledWith(t.video, 0, 0, 1600, 900)
  })

  it("never upscale a small picture", async () => {
    const t = setup({ video: [640, 480] })
    await t.link.share("camera")
    await t.ask("frame", { what: "camera" })
    expect(t.drawn).toEqual([{ width: 640, height: 480 }])
  })
})

describe("sharing", () => {
  it("asks the browser for the screen alone, keeps it, and tells the server and the listeners", async () => {
    const t = setup()
    const told: unknown[] = []
    t.link.onSharing((shared) => told.push(shared))
    await t.link.share("screen")
    expect(t.media.getDisplayMedia).toHaveBeenCalledWith({ video: true, audio: false })
    expect(t.link.sharing()).toEqual(["screen"])
    expect(t.link.stream("screen")).not.toBeNull()
    expect(sharing(t.sent)).toEqual([["screen"]])
    expect(told).toEqual([["screen"]])
    expect(t.video.muted && t.video.playsInline).toBe(true)
  })

  it("opens the front camera", async () => {
    const t = setup()
    await t.link.share("camera")
    expect(t.media.getUserMedia).toHaveBeenCalledWith({ video: { facingMode: "user" }, audio: false })
    expect(t.link.sharing()).toEqual(["camera"])
  })

  it("notices the browser's own Stop sharing", async () => {
    const t = setup()
    await t.link.share("screen")
    t.screenTrack.end()
    expect(t.link.sharing()).toEqual([])
    expect(sharing(t.sent)).toEqual([["screen"], []])
    await expect(t.ask("frame", { what: "screen" })).rejects.toThrow("the screen isn't shared")
  })

  it("stops one kind, or everything", async () => {
    const t = setup()
    await t.link.share("screen")
    await t.link.share("camera")
    t.link.stopShare("camera")
    expect(t.cameraTrack.stop).toHaveBeenCalled()
    expect(t.link.sharing()).toEqual(["screen"])
    t.link.stopShare()
    expect(t.screenTrack.stop).toHaveBeenCalled()
    expect(sharing(t.sent).at(-1)).toEqual([])
  })

  it("says why it couldn't (the picker closed)", async () => {
    const t = setup()
    t.media.getDisplayMedia?.mockRejectedValueOnce(domError("NotAllowedError", "Permission denied"))
    await expect(t.link.share("screen")).rejects.toThrow("The screen wasn't shared")
    expect(t.link.sharing()).toEqual([])
  })
})

describe("a share Orion asks for", () => {
  it("waits for the user's Share, then answers {} once shared", async () => {
    const t = setup()
    let changes = 0
    t.link.onPendingShare(() => changes++)
    const answer = t.ask("share", { what: "screen" })
    expect(t.link.pendingShare?.what).toBe("screen")
    expect(changes).toBe(1)
    expect(t.media.getDisplayMedia).not.toHaveBeenCalled() // only the user's click may start it
    await t.link.pendingShare?.accept()
    expect(t.link.pendingShare).toBeNull()
    expect(await answer).toEqual({})
    expect(t.link.sharing()).toEqual(["screen"])
  })

  it("answers the user's no", async () => {
    const t = setup()
    const answer = t.ask("share", { what: "camera" })
    t.link.pendingShare?.decline()
    await expect(answer).rejects.toThrow("the user said no")
    expect(t.link.pendingShare).toBeNull()
  })

  it("answers at once when it is already shared", async () => {
    const t = setup()
    await t.link.share("screen")
    expect(await t.ask("share", { what: "screen" })).toEqual({})
    expect(t.link.pendingShare).toBeNull()
  })

  it("asks once for two requests of the same thing", async () => {
    const t = setup()
    const first = t.ask("share", { what: "screen" })
    const second = t.ask("share", { what: "screen" })
    await t.link.pendingShare?.accept()
    expect(await Promise.all([first, second])).toEqual([{}, {}])
    expect(t.media.getDisplayMedia).toHaveBeenCalledTimes(1)
  })

  it("asks for the latest thing when Orion changes his mind", async () => {
    const t = setup()
    const screen = t.ask("share", { what: "screen" })
    const camera = t.ask("share", { what: "camera" })
    await expect(screen).rejects.toThrow("Orion asked to see the camera instead")
    expect(t.link.pendingShare?.what).toBe("camera")
    await t.link.pendingShare?.accept()
    expect(await camera).toEqual({})
  })

  it("is answered when the user shares another way meanwhile (the composer's button)", async () => {
    const t = setup()
    const answer = t.ask("share", { what: "screen" })
    await t.link.share("screen")
    expect(await answer).toEqual({})
    expect(t.link.pendingShare).toBeNull()
  })

  it("passes on why sharing failed after the click", async () => {
    const t = setup()
    t.media.getDisplayMedia?.mockRejectedValueOnce(domError("NotAllowedError", "Permission denied"))
    const answer = t.ask("share", { what: "screen" })
    await expect(t.link.pendingShare?.accept()).rejects.toThrow("The screen wasn't shared")
    await expect(answer).rejects.toThrow("The screen wasn't shared")
  })

  it("is refused when this browser has no such thing to share", async () => {
    const t = setup({ screen: false })
    await expect(t.ask("share", { what: "screen" })).rejects.toThrow("this browser can't share its screen")
    expect(t.link.pendingShare).toBeNull()
  })
})

describe("the clipboard", () => {
  it("reads the text", async () => {
    const t = setup()
    expect(await t.ask("clipboard_read")).toEqual({ text: "copied text" })
  })

  it("passes on the browser's reason when reading fails", async () => {
    const t = setup()
    t.clipboard.readText.mockRejectedValueOnce(domError("NotAllowedError", "Document is not focused."))
    await expect(t.ask("clipboard_read")).rejects.toThrow("the HUD's tab isn't focused: click into it, then ask again")
    t.clipboard.readText.mockRejectedValueOnce(domError("NotAllowedError", "Read permission denied."))
    await expect(t.ask("clipboard_read")).rejects.toThrow("Read permission denied.")
  })

  it("writes the text", async () => {
    const t = setup()
    expect(await t.ask("clipboard_write", { text: "hello" })).toEqual({})
    expect(t.clipboard.writeText).toHaveBeenCalledWith("hello")
    expect(t.link.pendingCopy).toBeNull()
  })

  it("waits for the user's click when the browser wants one, then answers {}", async () => {
    const t = setup()
    let changes = 0
    t.link.onPendingCopy(() => changes++)
    t.clipboard.writeText.mockRejectedValueOnce(
      domError("NotAllowedError", "Clipboard write was blocked due to lack of user activation."),
    )
    const answer = t.ask("clipboard_write", { text: "the address" })
    await vi.waitFor(() => expect(t.link.pendingCopy?.text).toBe("the address"))
    await t.link.pendingCopy?.copy()
    expect(await answer).toEqual({})
    expect(t.clipboard.writeText).toHaveBeenCalledTimes(2)
    expect(t.link.pendingCopy).toBeNull()
    expect(changes).toBe(2)
  })

  it("answers the user's dismissal", async () => {
    const t = setup()
    t.clipboard.writeText.mockRejectedValueOnce(domError("NotAllowedError", "Document is not focused."))
    const answer = t.ask("clipboard_write", { text: "x" })
    await vi.waitFor(() => expect(t.link.pendingCopy).not.toBeNull())
    t.link.pendingCopy?.dismiss()
    await expect(answer).rejects.toThrow("the user didn't copy it")
  })

  it("passes on other failures at once", async () => {
    const t = setup()
    t.clipboard.writeText.mockRejectedValueOnce(domError("DataError", "Bad data"))
    await expect(t.ask("clipboard_write", { text: "x" })).rejects.toThrow("the browser refused it (Bad data)")
    expect(t.link.pendingCopy).toBeNull()
  })
})

describe("closing", () => {
  it("answers what waits, and stops sharing", async () => {
    const t = setup()
    await t.link.share("camera")
    const share = t.ask("share", { what: "screen" })
    t.clipboard.writeText.mockRejectedValueOnce(domError("NotAllowedError", "no"))
    const copy = t.ask("clipboard_write", { text: "x" })
    await vi.waitFor(() => expect(t.link.pendingCopy).not.toBeNull())
    t.link.dispose()
    await expect(share).rejects.toThrow("the HUD closed")
    await expect(copy).rejects.toThrow("the HUD closed")
    expect(t.cameraTrack.stop).toHaveBeenCalled()
  })

  it("refuses a request it doesn't know", async () => {
    const t = setup()
    await expect(t.link.handle({ id: "x", kind: "teleport" } as unknown as DeviceRequest)).rejects.toThrow(
      'this device doesn\'t know the request "teleport"',
    )
  })
})
