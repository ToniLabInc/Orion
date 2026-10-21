/**
 * Pairing this device: what a device on your network sees until it is paired (Orion closed its link with 4401, and
 * answers it nothing but his files and pairing). The code comes from his machine (Settings, Devices, Pair a device);
 * typed here with a name to know the device by, it pairs it, and the HUD starts again, paired. Its own chunk: only a
 * device that isn't paired ever loads it.
 */
import { Link2 } from "lucide-react"
import { motion } from "motion/react"
import { type FormEvent, useId, useState } from "react"
import { pair } from "@/lib/api"
import { SPRING } from "@/lib/motion"
import { Mark } from "@/ui/brand"
import { Button } from "@/ui/button"
import { Input } from "@/ui/input"

/**
 * A first guess at what to call this device, from what the browser says it runs on ("" when it can't tell).
 * @param agent the browser's user agent
 * @returns the name
 */
export function deviceName(agent: string): string {
  const kinds: [RegExp, string][] = [
    [/iPhone/, "iPhone"],
    [/iPad/, "iPad"],
    [/Android.*Mobile/, "Android phone"],
    [/Android/, "Android tablet"],
    [/Macintosh/, "Mac"],
    [/Windows/, "Windows PC"],
    [/CrOS/, "Chromebook"],
    [/Linux/, "Linux PC"],
  ]
  return kinds.find(([pattern]) => pattern.test(agent))?.[1] ?? ""
}

/**
 * The pairing screen.
 * @returns the screen
 */
export function PairOverlay() {
  const [code, setCode] = useState("")
  const [name, setName] = useState(() => deviceName(navigator.userAgent))
  const [problem, setProblem] = useState("")
  const [busy, setBusy] = useState(false)
  const id = useId()

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setProblem("")
    try {
      await pair(code.replace(/\s/g, ""), name)
      location.reload() // paired: the HUD starts again, with its key
    } catch (error) {
      setProblem((error as Error).message)
      setBusy(false)
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${id}-title`}
      className="fixed inset-0 z-[95] grid place-items-center overflow-y-auto bg-void-950/80 p-4"
      data-testid="pair"
    >
      <motion.form
        onSubmit={submit}
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={SPRING.float}
        className="glass-solid grid w-full max-w-md gap-6 rounded-sheet p-7"
        noValidate
      >
        <div className="grid justify-items-start gap-3">
          <Mark className="size-10 text-rigel-300" />
          <p className="caps flex items-center gap-1.5 text-rigel-300">
            <Link2 aria-hidden="true" className="size-3.5" />
            Pair this device
          </p>
          <h2 id={`${id}-title`} className="font-serif text-3xl text-steel-50">
            Hello. Who's this?
          </h2>
          <p className="text-sm text-steel-400">
            He answers his own machine, and the devices paired with it. On that machine open Settings, Devices, choose
            Pair a device, and type here the code it shows.
          </p>
        </div>
        <label className="grid gap-2 text-sm text-steel-200" htmlFor={`${id}-code`}>
          The code
          <Input
            id={`${id}-code`}
            value={code}
            onChange={(e) => {
              setCode(e.target.value)
              setProblem("")
            }}
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            spellCheck={false}
            className="h-14 text-center font-mono text-2xl tracking-[0.4em]"
            data-testid="pair-code"
          />
        </label>
        <label className="grid gap-2 text-sm text-steel-200" htmlFor={`${id}-name`}>
          This device
          <Input
            id={`${id}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Living room tablet"
            autoComplete="off"
            data-testid="pair-name"
          />
        </label>
        {problem && (
          <p role="alert" className="text-alert-300 text-sm">
            {problem}
          </p>
        )}
        <Button
          type="submit"
          tone="primary"
          size="lg"
          disabled={busy || !code.trim() || !name.trim()}
          data-testid="pair-submit"
        >
          Pair
        </Button>
      </motion.form>
    </div>
  )
}
