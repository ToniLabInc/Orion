import { describe, expect, it } from "vitest"
import { signalOf } from "./link"

describe("signalOf", () => {
  it("says nothing while the link is up, or while a blip has yet to count", () => {
    expect(signalOf("online", null, false)).toBeNull()
    expect(signalOf("offline", null, false)).toBeNull()
    expect(signalOf("connecting", null, false)).toBeNull()
  })

  it("calls a link that stays down lost", () => {
    expect(signalOf("offline", null, true)).toBe("lost")
    expect(signalOf("connecting", null, true)).toBe("lost")
  })

  it("says a shutdown or a restart at once, even before the link drops", () => {
    expect(signalOf("online", { restart: false }, false)).toBe("shutdown")
    expect(signalOf("offline", { restart: false }, true)).toBe("shutdown")
    expect(signalOf("online", { restart: true }, false)).toBe("restart")
  })

  it("leaves a device that isn't paired yet to the pairing screen", () => {
    expect(signalOf("unpaired", null, true)).toBeNull()
  })

  it("says a page from another PC is refused, whatever else", () => {
    expect(signalOf("refused", null, false)).toBe("refused")
    expect(signalOf("refused", { restart: true }, true)).toBe("refused")
  })
})
