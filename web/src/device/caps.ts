// What this browser can offer Orion (the `hello` message), and its name for people. Only what works here: media and
// the clipboard need a secure context (HTTPS or localhost); a phone has no screen to share.
import type { Cap } from "./index"

/** What the browser has, as `capsOf` needs it (read by `platformOf`, or made up by a test). */
export interface Platform {
  secure: boolean
  getUserMedia: boolean
  getDisplayMedia: boolean
  audioWorklet: boolean
  clipboard: boolean
  mobile: boolean
  /** Whether a camera was seen: true; false (the devices are listed and none is a camera); undefined (unknown). */
  camera: boolean | undefined
}

/** The parts of `navigator.userAgentData` (Chromium's client hints) the name reads. */
export interface UAData {
  brands?: readonly { brand: string; version?: string }[]
  platform?: string
  mobile?: boolean
}

/** The globals `platformOf` reads (globalThis in a browser). */
export interface BrowserGlobals {
  isSecureContext?: boolean
  navigator?: {
    userAgent?: string
    userAgentData?: UAData
    maxTouchPoints?: number
    mediaDevices?: { getUserMedia?: unknown; getDisplayMedia?: unknown }
    clipboard?: unknown
  }
  AudioWorkletNode?: unknown
}

/**
 * What this browser offers.
 * @param p - what it has
 * @returns its caps, in the server's words
 */
export function capsOf(p: Platform): Cap[] {
  const caps: Cap[] = []
  const media = p.secure && p.getUserMedia
  if (media && p.audioWorklet) caps.push("mic", "speaker")
  if (p.secure && p.getDisplayMedia && !p.mobile) caps.push("screen")
  if (media && p.camera !== false) caps.push("camera")
  if (p.clipboard) caps.push("clipboard")
  return caps
}

/**
 * Why this browser can't carry the voice, or null when it can.
 * @param p - what it has
 * @returns the reason, for the user
 */
export function whyNoVoice(p: Platform): string | null {
  if (!p.secure) return "Open the HUD over HTTPS (or localhost) to talk from this device."
  if (!p.getUserMedia) return "This browser can't open a microphone."
  if (!p.audioWorklet) return "This browser can't record or play audio here (it has no AudioWorklet)."
  return null
}

/**
 * What the browser has.
 * @param g - its globals
 * @param camera - whether a camera was seen (`cameraSeen`)
 * @returns the platform
 */
export function platformOf(g: BrowserGlobals, camera: boolean | undefined): Platform {
  const nav = g.navigator
  const media = nav?.mediaDevices
  return {
    secure: g.isSecureContext === true,
    getUserMedia: typeof media?.getUserMedia === "function",
    getDisplayMedia: typeof media?.getDisplayMedia === "function",
    audioWorklet: typeof g.AudioWorkletNode === "function",
    clipboard: Boolean(nav?.clipboard),
    mobile: isMobile(nav?.userAgent ?? "", nav?.userAgentData, nav?.maxTouchPoints ?? 0),
    camera,
  }
}

/**
 * Whether the device lists a camera (before any permission, browsers list one device of each kind present).
 * @param media - navigator.mediaDevices
 * @returns true, false (devices listed, no camera), or undefined (can't tell)
 */
export async function cameraSeen(media: MediaDevices | undefined): Promise<boolean | undefined> {
  if (typeof media?.enumerateDevices !== "function") return undefined
  try {
    const devices = await media.enumerateDevices()
    if (devices.some((d) => d.kind === "videoinput")) return true
    return devices.length ? false : undefined
  } catch {
    return undefined
  }
}

/**
 * Whether this is a phone or a tablet.
 * @param ua - the user agent string
 * @param data - the client hints, when the browser has them
 * @param touchPoints - navigator.maxTouchPoints (an iPad says it is a Mac, with touch)
 * @returns whether it is mobile
 */
export function isMobile(ua: string, data?: UAData, touchPoints = 0): boolean {
  if (data?.mobile) return true
  return /Android|iPhone|iPad|iPod|Mobile/i.test(ua) || (/Macintosh/.test(ua) && touchPoints > 1)
}

/**
 * This device's name for people: "Firefox on Linux", "Chrome on Windows", "Safari on iPhone".
 * @param ua - the user agent string
 * @param data - the client hints, when the browser has them
 * @param touchPoints - navigator.maxTouchPoints
 * @returns the name
 */
export function deviceName(ua: string, data?: UAData, touchPoints = 0): string {
  const browser = browserName(ua, data)
  const os = systemName(ua, data, touchPoints)
  return os ? `${browser} on ${os}` : browser
}

function systemName(ua: string, data: UAData | undefined, touchPoints: number): string {
  if (/iPhone|iPod/.test(ua)) return "iPhone"
  if (/iPad/.test(ua) || (/Macintosh/.test(ua) && touchPoints > 1)) return "iPad"
  if (/Android/.test(ua)) return "Android"
  if (/CrOS/.test(ua)) return "ChromeOS"
  if (/Windows/.test(ua)) return "Windows"
  if (/Macintosh|Mac OS X/.test(ua)) return "Mac"
  if (/Linux|X11/.test(ua)) return "Linux"
  const platform = data?.platform ?? ""
  if (/^mac/i.test(platform)) return "Mac"
  if (/chrome\s?os/i.test(platform)) return "ChromeOS"
  if (/^ios/i.test(platform)) return "iPhone"
  return platform
}

const BRANDS: Record<string, string> = {
  "Google Chrome": "Chrome",
  HeadlessChrome: "Chrome",
  "Microsoft Edge": "Edge",
  "Opera GX": "Opera",
}

function browserName(ua: string, data: UAData | undefined): string {
  // on iOS every browser is Safari's engine: only their own tokens tell them apart
  if (/CriOS\//.test(ua)) return "Chrome"
  if (/FxiOS\//.test(ua)) return "Firefox"
  if (/EdgiOS\//.test(ua)) return "Edge"
  if (/OPiOS\/|OPT\//.test(ua)) return "Opera"
  if (/SamsungBrowser\//.test(ua)) return "Samsung Internet"
  if (/Edg(e|A)?\//.test(ua)) return "Edge"
  if (/OPR\//.test(ua)) return "Opera"
  if (/Vivaldi\//.test(ua)) return "Vivaldi"
  if (/YaBrowser\//.test(ua)) return "Yandex Browser"
  if (/Firefox\//.test(ua)) return "Firefox"
  const brands = (data?.brands ?? []).map((b) => b.brand).filter((b) => !/not.?a.?brand/i.test(b))
  const brand = brands.find((b) => b !== "Chromium")
  if (brand) return BRANDS[brand] ?? brand // Brave says who it is only here
  if (brands.includes("Chromium")) return "Chromium"
  if (/Chrome\//.test(ua)) return "Chrome"
  if (/Chromium\//.test(ua)) return "Chromium"
  if (/Safari\//.test(ua)) return "Safari"
  return "A browser"
}
