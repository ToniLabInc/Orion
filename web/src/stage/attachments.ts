// Pictures you send with a message, made in the browser: decoded, sized down to MAX_SIDE on their longest side, and
// encoded as a JPEG data URL (what the `chat` message carries). Nothing leaves the page before you send.
import { hex } from "@/lib/palette"

/** The longest side of a picture sent with a message. */
export const MAX_SIDE = 1600
/** Its JPEG quality. */
export const QUALITY = 0.85

/**
 * The size a picture is drawn at: as it is when it fits, else scaled down to `max` on its longest side.
 * @param width its width
 * @param height its height
 * @param max the longest side allowed
 * @returns the size, in whole pixels
 */
export function fitSize(width: number, height: number, max = MAX_SIDE): { width: number; height: number } {
  const scale = Math.min(1, max / Math.max(width, height, 1))
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) }
}

/**
 * Whether a dropped or pasted file is a picture the browser can read.
 * @param file the file
 * @returns true for an image
 */
export const isPicture = (file: { type: string }) => file.type.startsWith("image/")

/**
 * A picture as Orion receives it: decoded, sized down, a JPEG data URL.
 * @param file the picture (pasted, dropped or chosen)
 * @returns the data URL
 * @throws Error when the browser can't read it
 */
export async function compress(file: Blob): Promise<string> {
  const bitmap = await createImageBitmap(file)
  try {
    const { width, height } = fitSize(bitmap.width, bitmap.height)
    const canvas = document.createElement("canvas")
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext("2d")
    if (!ctx) throw new Error("This browser can't draw pictures")
    ctx.fillStyle = hex("black") // a transparent PNG becomes a JPEG on black, not on garbage
    ctx.fillRect(0, 0, width, height)
    ctx.imageSmoothingQuality = "high"
    ctx.drawImage(bitmap, 0, 0, width, height)
    return canvas.toDataURL("image/jpeg", QUALITY)
  } finally {
    bitmap.close()
  }
}
