// The user's clipboard on this device, for Orion's clipboard tools. Reading may need the tab focused and the
// browser's permission (the error says which). Writing without a click is refused by some browsers (Firefox, Safari)
// and by any when the tab isn't focused: then the copy waits for the user's click (`pending`), and the request is
// answered once it is done.
import { messageOf, nameOf } from "@/audio/errors"

/** A copy waiting for the user's click: the UI shows a "Copy" button whose click calls `copy()`. */
export interface PendingCopy {
  text: string
  /** Copy now (call it inside the click); rejects with the browser's reason if it still can't. */
  copy(): Promise<void>
  /** The user doesn't want it. */
  dismiss(): void
}

interface Waiting extends PendingCopy {
  resolve(): void
  reject(error: Error): void
}

export class ClipboardAccess {
  private readonly clipboard: () => Clipboard | undefined
  private readonly changed: () => void
  private waiting: Waiting | null = null

  /**
   * @param changed - told whenever `pending` changes
   * @param clipboard - the browser's clipboard (navigator.clipboard by default)
   */
  constructor(changed: () => void, clipboard: () => Clipboard | undefined = browserClipboard) {
    this.changed = changed
    this.clipboard = clipboard
  }

  /** The copy waiting for the user's click, or null. */
  get pending(): PendingCopy | null {
    return this.waiting
  }

  /**
   * The text on the clipboard.
   * @returns the text
   */
  async read(): Promise<string> {
    const clipboard = this.clipboard()
    if (typeof clipboard?.readText !== "function") throw new Error("this browser can't read its clipboard")
    try {
      return await clipboard.readText()
    } catch (error) {
      throw readError(error)
    }
  }

  /**
   * Put text on the clipboard; if the browser wants a click for it, wait for the user's.
   * @param text - the text
   * @returns resolves once copied
   */
  async write(text: string): Promise<void> {
    const clipboard = this.clipboard()
    if (typeof clipboard?.writeText !== "function") throw new Error("this browser can't copy to its clipboard")
    try {
      await clipboard.writeText(text)
    } catch (error) {
      if (!wantsClick(error)) throw writeError(error)
      await this.ask(text)
    }
  }

  /** Give up on what waits (the HUD closes). */
  drop(why: string): void {
    const waiting = this.waiting
    if (!waiting) return
    this.waiting = null
    this.changed()
    waiting.reject(new Error(why))
  }

  private ask(text: string): Promise<void> {
    this.drop("a newer copy took its place")
    return new Promise<void>((resolve, reject) => {
      const waiting: Waiting = {
        text,
        resolve,
        reject,
        copy: () => {
          if (this.waiting !== waiting) return Promise.resolve()
          const clipboard = this.clipboard()
          const copied = clipboard ? clipboard.writeText(text) : Promise.reject(new Error("no clipboard"))
          return copied.then(
            () => {
              this.settle(waiting)
              resolve()
            },
            (error: unknown) => {
              const why = writeError(error)
              this.settle(waiting)
              reject(why)
              throw why
            },
          )
        },
        dismiss: () => {
          if (this.waiting !== waiting) return
          this.settle(waiting)
          reject(new Error("the user didn't copy it"))
        },
      }
      this.waiting = waiting
      this.changed()
    })
  }

  private settle(waiting: Waiting): void {
    if (this.waiting !== waiting) return
    this.waiting = null
    this.changed()
  }
}

function browserClipboard(): Clipboard | undefined {
  return typeof navigator === "undefined" ? undefined : navigator.clipboard
}

/** Whether the browser refused for want of a click or of focus (a click on a "Copy" button gives both). */
function wantsClick(error: unknown): boolean {
  const name = nameOf(error)
  return name === "NotAllowedError" || name === "SecurityError"
}

/** Reading failed: the browser's reason, in words for the user. */
export function readError(error: unknown): Error {
  const message = messageOf(error)
  if (/focus/i.test(message)) return new Error("the HUD's tab isn't focused: click into it, then ask again")
  if (nameOf(error) === "NotAllowedError" || nameOf(error) === "SecurityError")
    return new Error(`the browser didn't allow it (${message}): allow clipboard access for this site`)
  return new Error(message || "the browser couldn't read it")
}

/** Writing failed: the browser's reason, in words for the user. */
export function writeError(error: unknown): Error {
  return new Error(`the browser refused it (${messageOf(error) || nameOf(error) || "no reason given"})`)
}
