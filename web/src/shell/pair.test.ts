import { describe, expect, it } from "vitest"
import { deviceName } from "./pair"

describe("deviceName", () => {
  it("guesses what a device is from its browser", () => {
    expect(deviceName("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)")).toBe("iPhone")
    expect(deviceName("Mozilla/5.0 (Linux; Android 15; Pixel 9) Mobile Safari/537.36")).toBe("Android phone")
    expect(deviceName("Mozilla/5.0 (Linux; Android 15; SM-X910) Safari/537.36")).toBe("Android tablet")
    expect(deviceName("Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0")).toBe("Linux PC")
    expect(deviceName("curl/8.0")).toBe("") // it can't tell: the name is left to type
  })
})
