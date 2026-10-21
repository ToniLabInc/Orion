/**
 * Toasts (Base UI): short messages stacked at the top right (along the top on a phone). `toast.add` works from
 * anywhere, React or not; `<Toasts />` draws them. A toast rises 8 px as it fades in and leaves with a fade, also when
 * swiped away; the ones behind the newest peek out below it without being scaled, so their text stays crisp. Its icon
 * and a tinted glow at its edge say what it is: information, success (green), a warning or an error (red). An action
 * (`actionProps`) shows as a button.
 */
import { Toast as Base } from "@base-ui/react/toast"
import { CircleAlert, CircleCheck, Info, LoaderCircle, OctagonAlert, X } from "lucide-react"
import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { buttonClass } from "./button"

/** The HUD's toast manager: `toast.add({ title, description, type, timeout })`, usable outside React. */
export const toast = Base.createToastManager()

const ICONS: Record<string, ReactNode> = {
  success: <CircleCheck aria-hidden="true" className="text-ok-400" />,
  info: <Info aria-hidden="true" className="text-rigel-300" />,
  warning: <CircleAlert aria-hidden="true" className="text-dusk-400" />,
  error: <OctagonAlert aria-hidden="true" className="text-alert-400" />,
  loading: <LoaderCircle aria-hidden="true" className="animate-spin text-rigel-300" />,
}

/**
 * Every current toast.
 * @returns the toasts
 */
function ToastList() {
  const { toasts } = Base.useToastManager()
  return toasts.map((item) => (
    <Base.Root
      key={item.id}
      toast={item}
      swipeDirection={["up", "right"]}
      className={cn(
        "glass-solid group/toast absolute top-0 right-0 z-[calc(1000-var(--toast-index))] w-full select-none rounded-menu text-steel-100 outline-none",
        "data-[type=error]:shadow-[var(--shadow-elevated),inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_40%,transparent)] data-[type=warning]:shadow-[var(--shadow-elevated),inset_0_0_0_1px_color-mix(in_oklch,var(--color-dusk-400)_35%,transparent)]",
        "[--gap:0.625rem] [--peek:0.5rem] [--height:var(--toast-frontmost-height,var(--toast-height))] [--offset-y:calc(var(--toast-offset-y)+var(--toast-index)*var(--gap)+var(--toast-swipe-movement-y))]",
        "h-(--height) [transform:translateX(var(--toast-swipe-movement-x))_translateY(calc(var(--toast-swipe-movement-y)+var(--toast-index)*var(--peek)))]",
        "transition-[transform,opacity,height] duration-(--dur-base) ease-(--ease-expo)",
        "after:absolute after:bottom-full after:left-0 after:h-[calc(var(--gap)+1px)] after:w-full after:content-['']",
        "data-expanded:h-(--toast-height) data-expanded:[transform:translateX(var(--toast-swipe-movement-x))_translateY(var(--offset-y))]",
        "data-limited:opacity-0 data-ending-style:opacity-0 data-starting-style:opacity-0 data-starting-style:[transform:translateY(-8px)]",
      )}
    >
      <Base.Content className="flex h-full items-start gap-3 overflow-hidden p-3.5 pr-2.5 transition-opacity duration-(--dur-base) data-behind:opacity-0 data-expanded:opacity-100 [&_svg]:size-4">
        {item.type && ICONS[item.type] && <span className="mt-0.5 shrink-0">{ICONS[item.type]}</span>}
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <Base.Title className="font-medium text-sm text-steel-50" />
          <Base.Description className="text-sm text-steel-300 leading-snug" />
        </div>
        <Base.Action className={buttonClass({ tone: "secondary", size: "xs", className: "mt-px" })} />
        <Base.Close
          aria-label="Close"
          className="relative -mt-0.5 grid size-6 shrink-0 place-items-center rounded-chip text-steel-500 outline-none after:absolute after:-inset-2 hover:bg-steel-50/8 hover:text-steel-50 focus-visible:outline-2 focus-visible:outline-rigel-400"
        >
          <X aria-hidden="true" className="size-3.5!" />
        </Base.Close>
      </Base.Content>
    </Base.Root>
  ))
}

/**
 * The toast stack: render it once; it draws every toast added through `toast`.
 * @param props Base UI's provider options (`timeout`, `limit`)
 * @returns the stack, in a portal
 */
export function Toasts(props: Omit<Base.Provider.Props, "toastManager" | "children">) {
  return (
    <Base.Provider toastManager={toast} limit={3} {...props}>
      <Base.Portal>
        <Base.Viewport className="pointer-events-none fixed inset-x-3 top-[calc(var(--bar-h)+0.5rem)] z-[80] mx-auto w-auto max-w-sm outline-none tab:right-5 tab:left-auto tab:mx-0 tab:w-full [&>*]:pointer-events-auto">
          <ToastList />
        </Base.Viewport>
      </Base.Portal>
    </Base.Provider>
  )
}
