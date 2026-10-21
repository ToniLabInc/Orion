/**
 * Asking you before the HUD does something that can't be undone: `if (await ask({ title: "Forget everything?" })) …`.
 * One dialog serves the whole HUD (`<AskHost />`, in app.tsx). Esc or Cancel answers no, and a newer question answers
 * the one still open no. (His own questions before a risky action are the Confirmation, stage/confirmation.tsx.)
 */
import { AlertDialog } from "@base-ui/react/alert-dialog"
import { TriangleAlert } from "lucide-react"
import type { ReactNode } from "react"
import { create } from "zustand"
import { cn } from "@/lib/utils"
import { Button } from "./button"
import { BACKDROP } from "./dialog"

/** A question for you. */
export interface Question {
  /** The question itself. */
  title: string
  /** What answering yes does, in a sentence or two. */
  text?: string
  /** More to read before answering (a list of what yes sets going), under the text. */
  body?: ReactNode
  /** The confirming button's word ("Confirm" by default). */
  ok?: string
  /** Whether yes loses something: a warning sign and a red button. */
  danger?: boolean
  /** The small word above the title. */
  kicker?: string
}

interface Asking {
  question: Question | null
  open: boolean
  answer?: (yes: boolean) => void
}

const useAsking = create<Asking>(() => ({ question: null, open: false }))

/**
 * Ask a question.
 * @param question what to ask
 * @returns true for yes, false for no (also when a newer question replaces this one)
 */
export function ask(question: Question): Promise<boolean> {
  return new Promise((answer) => {
    useAsking.getState().answer?.(false)
    useAsking.setState({ question, open: true, answer })
  })
}

/**
 * Answer the open question and close the dialog.
 * @param yes the answer
 */
function reply(yes: boolean): void {
  useAsking.getState().answer?.(yes)
  useAsking.setState({ open: false, answer: undefined })
}

/**
 * The dialog: render it once.
 * @returns the dialog
 */
export function AskHost() {
  const { question, open } = useAsking()
  return (
    <AlertDialog.Root open={open} onOpenChange={(next) => !next && reply(false)}>
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className={BACKDROP} />
        <AlertDialog.Viewport className="fixed inset-0 z-50 grid grid-cols-[minmax(0,1fr)] place-items-center p-4">
          <AlertDialog.Popup
            data-testid="ask"
            className={cn(
              "glass-solid grid w-full max-w-md grid-cols-1 gap-4 rounded-card p-6 outline-none",
              "transition-[opacity,translate] duration-(--dur-base) ease-(--ease-expo) data-ending-style:translate-y-2 data-ending-style:opacity-0 data-starting-style:translate-y-2 data-starting-style:opacity-0",
              question?.danger &&
                "shadow-[var(--shadow-elevated),inset_0_0_0_1px_color-mix(in_oklch,var(--color-alert-400)_35%,transparent)]",
            )}
          >
            {question?.kicker && (
              <p className={cn("caps", question.danger ? "text-alert-300" : "text-rigel-300")}>{question.kicker}</p>
            )}
            <AlertDialog.Title className="flex items-start gap-2.5 font-semibold text-lg text-steel-50">
              {question?.danger && <TriangleAlert aria-hidden="true" className="mt-1 size-5 shrink-0 text-alert-400" />}
              {question?.title}
            </AlertDialog.Title>
            {question?.text && (
              <AlertDialog.Description className="text-sm text-steel-300 leading-relaxed">
                {question.text}
              </AlertDialog.Description>
            )}
            {question?.body}
            <div className="mt-1 flex justify-end gap-2">
              <AlertDialog.Close render={<Button />} data-testid="ask-cancel">
                Cancel
              </AlertDialog.Close>
              <Button tone={question?.danger ? "danger" : "primary"} onClick={() => reply(true)} data-testid="ask-ok">
                {question?.ok ?? "Confirm"}
              </Button>
            </div>
          </AlertDialog.Popup>
        </AlertDialog.Viewport>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  )
}
