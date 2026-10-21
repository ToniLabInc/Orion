// What every card's body is given: a way to put a short note in its header (a place, an uptime, a count).
import type { ReactNode } from "react"

export interface CardProps {
  setNote: (note: ReactNode) => void
}
