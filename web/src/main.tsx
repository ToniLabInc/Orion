/**
 * Start-up: the motion tokens (again whenever the reduced-motion preference changes), the live connection, a reload
 * after an update left a chunk behind, then the app.
 */
import "./styles.css"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { App } from "./app"
import { applyMotionTokens, REDUCED_MOTION, reducedMotion } from "./lib/motion"
import { connect } from "./lib/ws"

const RELOADED = "orion-reloaded" // in sessionStorage: when a missing chunk last reloaded the page

// The components' CSS reads its durations and curve from these variables: write them before the first render, and
// again whenever the reduced-motion preference changes.
applyMotionTokens(reducedMotion())
matchMedia(REDUCED_MOTION).addEventListener("change", (e) => applyMotionTokens(e.matches))

connect()

// After an update the old build's chunks are gone: a page opened for the first time since then loads the new HUD
// instead. Once only: a chunk that is really missing ends at the page's Reload, not in a loop.
addEventListener("vite:preloadError", () => {
  try {
    if (Date.now() - Number(sessionStorage.getItem(RELOADED)) < 10_000) return
    sessionStorage.setItem(RELOADED, String(Date.now()))
    location.reload()
  } catch {} // (no session storage: the Reload)
})

const root = document.getElementById("root")
if (root)
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
