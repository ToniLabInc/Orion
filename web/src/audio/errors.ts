// What went wrong with the browser's media, in words for the user (said by the HUD, or by Orion when he asked).

/**
 * An error's message, whatever was thrown.
 * @param error - what was thrown
 * @returns its message
 */
export function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === "object" && error !== null && "message" in error) return String(error.message)
  return String(error)
}

/**
 * An error's name ("NotAllowedError"…), whatever was thrown ("" for none).
 * @param error - what was thrown
 * @returns its name
 */
export function nameOf(error: unknown): string {
  if (typeof error === "object" && error !== null && "name" in error) return String(error.name)
  return ""
}

/**
 * Opening the microphone failed: why, for the user.
 * @param error - what getUserMedia threw
 * @returns the error to show
 */
export function micError(error: unknown): Error {
  const message = messageOf(error)
  switch (nameOf(error)) {
    case "NotAllowedError":
    case "PermissionDeniedError":
    case "SecurityError":
      return new Error(
        /system/i.test(message)
          ? "The system doesn't let this browser use the microphone: allow it in the system's privacy settings."
          : "The microphone is blocked for this site: allow it in the browser's address bar.",
      )
    case "NotFoundError":
    case "DevicesNotFoundError":
    case "OverconstrainedError":
      return new Error("No microphone was found on this device.")
    case "NotReadableError":
    case "TrackStartError":
    case "AbortError":
      return new Error("The microphone couldn't start: another app may be using it.")
    default:
      return new Error(`The microphone couldn't open: ${message}`)
  }
}
