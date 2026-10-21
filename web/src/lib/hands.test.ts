import { describe, expect, it } from "vitest"
import { gestureWords, received, statusOf } from "./hands"

describe("the hands' live view", () => {
  it("names a gesture its remote didn't", () => {
    expect(gestureWords("swipe_right")).toBe("Swipe right")
  })

  it("waits for its first frame", () => {
    expect(statusOf("nobody").state).toBe("waiting")
  })

  it("counts a frame as new only when its number is, and keeps a picture sent once", () => {
    const first = received(undefined, { seq: 1, hands: [], jpg: "AAAA", jpg_seq: 1 }, 100)
    expect(first.at).toBe(100)
    const again = received(
      first,
      {
        seq: 1,
        hands: [],
        jpg_seq: 1,
        link: { connected: false, error: "", camera: false, paused: false, tracker: "" },
      },
      900,
    )
    expect(again.at).toBe(100) // the same frame, only its link changed: no news of the camera
    expect(again.frame.jpg).toBe("AAAA") // and its picture stays
    const next = received(again, { seq: 2, hands: [], jpg_seq: 1 }, 950)
    expect([next.at, next.frame.jpg]).toEqual([950, "AAAA"])
    const picture = received(next, { seq: 3, hands: [], jpg: "BBBB", jpg_seq: 3 }, 1000)
    expect(picture.frame.jpg).toBe("BBBB")
    expect(received(picture, { seq: 4, hands: [] }, 1100).frame.jpg).toBeUndefined() // the picture was switched off
  })
})
