// The worker the Core draws in (client.ts starts it, from our own origin): it starts the engine on the canvas the page
// hands over, then passes the page's messages on. Messages that arrive before `init` wait in the worker's own queue.
import { type Engine, startEngine } from "./engine"
import type { EngineInit, ToEngine } from "./protocol"

type Init = { type: "init"; canvas: OffscreenCanvas; init: EngineInit }

let engine: Engine | undefined

addEventListener("message", (event: MessageEvent<Init | ToEngine>) => {
  const message = event.data
  if (message.type !== "init") {
    engine?.handle(message)
    if (message.type === "dispose") close()
    return
  }
  try {
    engine = startEngine(message.canvas, message.init, (out) => postMessage(out))
  } catch (error) {
    postMessage({ type: "failed", error: String((error as Error)?.message ?? error) })
  }
})
