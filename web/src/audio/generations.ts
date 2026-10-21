// Which frames of his voice still count. The server numbers each stretch of speech it may cut short: an instant stop
// moves it to a new generation and says so (`speaker {gen}`), and a frame of a newer generation means the same thing
// (frames go out ahead of events, so they can come first). What is held from before is dropped, and so is a late
// frame of an older generation.
//
// One instance per take of the voice: each take opens a fresh output on the server, numbered from 0 again. On one
// connection the old output's last frames and its stop can still be in flight then (letting go and taking the voice
// again at once), so a stop heard before any frame is ignored (it stops nothing here), and a step back to generation
// 0 is the fresh output starting, once.

export type FrameAction = "play" | "flush-play" | "drop"

export class Generations {
  private current = -1
  private restartable = true

  /**
   * A frame of generation `g` arrived.
   * @param g - its generation
   * @returns play it; drop what is held, then play it; or drop it
   */
  frame(g: number): FrameAction {
    if (g === this.current) return "play"
    if (this.current < 0) {
      this.current = g // the take's first frame: nothing is held yet
      return "play"
    }
    if (g > this.current || (g === 0 && this.restartable)) {
      if (g < this.current) this.restartable = false
      this.current = g
      return "flush-play"
    }
    return "drop"
  }

  /**
   * The server stopped his voice (`speaker {gen}`): frames before generation `g` are void.
   * @param g - the generation that follows the stop
   * @returns whether what is held must be dropped
   */
  flush(g: number): boolean {
    if (this.current < 0 || g <= this.current) return false
    this.current = g
    return true
  }
}
