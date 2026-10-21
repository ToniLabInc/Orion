/**
 * Pictures in the conversation: the ones you sent with a message, the ones a tool returned. A row of thumbnails
 * (data URLs: nothing is fetched); one opens full size over the page, Esc or a click closes it.
 */
import { Dialog as Base } from "@base-ui/react/dialog"
import { X } from "lucide-react"
import { useState } from "react"
import { cn } from "@/lib/utils"
import { BACKDROP } from "./dialog"

/**
 * The thumbnails, and the picture opened from them.
 * @param props the `images` (data URLs), what they are (`label`), the thumbnails' `size` in px, and classes
 * @returns the row
 */
export function Pictures({
  images,
  label,
  size = 72,
  className,
}: {
  images: string[]
  label: string
  size?: number
  className?: string
}) {
  const [open, setOpen] = useState<number | null>(null)
  return (
    <>
      <ul className={cn("flex flex-wrap gap-2", className)} aria-label={label}>
        {images.map((src, i) => (
          <li key={src.slice(-48) + String(i)}>
            <button
              type="button"
              onClick={() => setOpen(i)}
              aria-label={`${label}: picture ${i + 1}, open it`}
              className="block overflow-hidden rounded-control shadow-[inset_0_0_0_1px_var(--hairline-strong)] outline-none transition-[filter] hover:brightness-110 focus-visible:outline-2 focus-visible:outline-rigel-400"
              style={{ width: size, height: size }}
            >
              <img src={src} alt="" className="size-full object-cover" draggable={false} />
            </button>
          </li>
        ))}
      </ul>
      <Base.Root open={open !== null} onOpenChange={(next) => !next && setOpen(null)}>
        <Base.Portal>
          <Base.Backdrop className={BACKDROP} />
          <Base.Viewport className="fixed inset-0 z-50 grid place-items-center p-6">
            <Base.Popup className="relative max-h-full max-w-full outline-none transition-[opacity,translate] duration-(--dur-base) ease-(--ease-expo) data-ending-style:opacity-0 data-starting-style:translate-y-2 data-starting-style:opacity-0">
              <Base.Title className="sr-only">{label}</Base.Title>
              {open !== null && images[open] && (
                <img
                  src={images[open]}
                  alt={`${label}, ${open + 1} of ${images.length}`}
                  className="max-h-[85dvh] max-w-[90vw] rounded-card object-contain shadow-[var(--shadow-elevated)]"
                />
              )}
              <Base.Close
                aria-label="Close"
                className="glass-solid absolute -top-3 -right-3 grid size-9 place-items-center rounded-full text-steel-100 outline-none hover:text-steel-50 focus-visible:outline-2 focus-visible:outline-rigel-400"
              >
                <X aria-hidden="true" className="size-4" />
              </Base.Close>
            </Base.Popup>
          </Base.Viewport>
        </Base.Portal>
      </Base.Root>
    </>
  )
}
