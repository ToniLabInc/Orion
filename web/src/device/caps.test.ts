import { describe, expect, it } from "vitest"
import {
  type BrowserGlobals,
  cameraSeen,
  capsOf,
  deviceName,
  isMobile,
  type Platform,
  platformOf,
  whyNoVoice,
} from "./caps"

const UA = {
  firefoxLinux: "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
  firefoxUbuntu: "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
  chromeWindows:
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
  edgeWindows:
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
  operaWindows:
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 OPR/120.0.0.0",
  safariMac:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
  chromeMac:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
  safariIphone:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
  chromeIphone:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0.0.0 Mobile/15E148 Safari/604.1",
  firefoxIphone:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/131.0 Mobile/15E148 Safari/605.1.15",
  chromeAndroid:
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
  samsungAndroid:
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36",
  firefoxAndroid: "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0",
  chromeOs:
    "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
  headless:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36",
}

describe("the device's name", () => {
  it.each([
    [UA.firefoxLinux, "Firefox on Linux"],
    [UA.firefoxUbuntu, "Firefox on Linux"],
    [UA.chromeWindows, "Chrome on Windows"],
    [UA.edgeWindows, "Edge on Windows"],
    [UA.operaWindows, "Opera on Windows"],
    [UA.safariMac, "Safari on Mac"],
    [UA.chromeMac, "Chrome on Mac"],
    [UA.safariIphone, "Safari on iPhone"],
    [UA.chromeIphone, "Chrome on iPhone"],
    [UA.firefoxIphone, "Firefox on iPhone"],
    [UA.chromeAndroid, "Chrome on Android"],
    [UA.samsungAndroid, "Samsung Internet on Android"],
    [UA.firefoxAndroid, "Firefox on Android"],
    [UA.chromeOs, "Chrome on ChromeOS"],
    [UA.headless, "Chrome on Linux"],
  ])("%s → %s", (ua, name) => {
    expect(deviceName(ua)).toBe(name)
  })

  it("knows an iPad that says it is a Mac (it has touch)", () => {
    expect(deviceName(UA.safariMac, undefined, 5)).toBe("Safari on iPad")
    expect(deviceName(UA.safariMac, undefined, 0)).toBe("Safari on Mac")
  })

  it("takes the brand from the client hints (Brave is only there)", () => {
    const brave = { brands: [{ brand: "Brave" }, { brand: "Chromium" }, { brand: "Not=A?Brand" }], platform: "Windows" }
    expect(deviceName(UA.chromeWindows, brave)).toBe("Brave on Windows")
    const chrome = { brands: [{ brand: "Not;A=Brand" }, { brand: "Google Chrome" }, { brand: "Chromium" }] }
    expect(deviceName(UA.chromeMac, chrome)).toBe("Chrome on Mac")
    const chromium = { brands: [{ brand: "Chromium" }, { brand: "Not_A Brand" }] }
    expect(deviceName(UA.headless, chromium)).toBe("Chromium on Linux")
    const headless = { brands: [{ brand: "Chromium" }, { brand: "HeadlessChrome" }, { brand: "Not?A_Brand" }] }
    expect(deviceName(UA.headless, headless)).toBe("Chrome on Linux")
  })

  it("falls back on the client hints' platform, then on the browser alone", () => {
    expect(deviceName("Mozilla/5.0 Chrome/141.0.0.0", { platform: "macOS" })).toBe("Chrome on Mac")
    expect(deviceName("SomethingElse/1.0")).toBe("A browser")
  })
})

const desktop: Platform = {
  secure: true,
  getUserMedia: true,
  getDisplayMedia: true,
  audioWorklet: true,
  clipboard: true,
  mobile: false,
  camera: undefined,
}

describe("what the browser offers", () => {
  it("offers everything on a desktop browser over HTTPS", () => {
    expect(capsOf(desktop)).toEqual(["mic", "speaker", "screen", "camera", "clipboard"])
  })

  it("offers no media outside a secure context", () => {
    expect(capsOf({ ...desktop, secure: false, clipboard: false })).toEqual([])
  })

  it("has no screen to share on a phone", () => {
    expect(capsOf({ ...desktop, mobile: true })).toEqual(["mic", "speaker", "camera", "clipboard"])
    expect(capsOf({ ...desktop, getDisplayMedia: false })).not.toContain("screen")
  })

  it("can't carry the voice without an AudioWorklet", () => {
    expect(capsOf({ ...desktop, audioWorklet: false })).toEqual(["screen", "camera", "clipboard"])
  })

  it("offers the camera unless the devices are listed and none is one", () => {
    expect(capsOf({ ...desktop, camera: true })).toContain("camera")
    expect(capsOf({ ...desktop, camera: false })).not.toContain("camera")
  })

  it("says why the voice can't be carried here", () => {
    expect(whyNoVoice({ ...desktop, secure: false })).toBe(
      "Open the HUD over HTTPS (or localhost) to talk from this device.",
    )
    expect(whyNoVoice({ ...desktop, getUserMedia: false })).toMatch(/microphone/)
    expect(whyNoVoice({ ...desktop, audioWorklet: false })).toMatch(/AudioWorklet/)
    expect(whyNoVoice(desktop)).toBeNull()
  })
})

describe("reading the browser", () => {
  const fn = () => {}

  it("reads the globals", () => {
    const g: BrowserGlobals = {
      isSecureContext: true,
      navigator: {
        userAgent: UA.firefoxLinux,
        mediaDevices: { getUserMedia: fn, getDisplayMedia: fn },
        clipboard: {},
        maxTouchPoints: 0,
      },
      AudioWorkletNode: class {},
    }
    expect(platformOf(g, undefined)).toEqual({ ...desktop, camera: undefined })
  })

  it("sees a phone, and a browser over plain HTTP", () => {
    const phone: BrowserGlobals = {
      isSecureContext: false,
      navigator: { userAgent: UA.chromeAndroid, mediaDevices: undefined },
    }
    const p = platformOf(phone, true)
    expect(p).toMatchObject({ secure: false, getUserMedia: false, mobile: true, audioWorklet: false, clipboard: false })
    expect(capsOf(p)).toEqual([])
  })

  it("tells phones by their client hints too", () => {
    expect(isMobile(UA.chromeWindows, { mobile: true })).toBe(true)
    expect(isMobile(UA.chromeWindows)).toBe(false)
  })

  it("looks for a camera among the devices", async () => {
    const listing = (kinds: string[]) =>
      ({ enumerateDevices: async () => kinds.map((kind) => ({ kind })) }) as unknown as MediaDevices
    expect(await cameraSeen(listing(["audioinput", "videoinput"]))).toBe(true)
    expect(await cameraSeen(listing(["audioinput", "audiooutput"]))).toBe(false)
    expect(await cameraSeen(listing([]))).toBeUndefined()
    expect(await cameraSeen(undefined)).toBeUndefined()
    const failing = {
      enumerateDevices: async () => {
        throw new Error("no")
      },
    } as unknown as MediaDevices
    expect(await cameraSeen(failing)).toBeUndefined()
  })
})
