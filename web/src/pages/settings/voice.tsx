/**
 * Voice: everything about hearing and speaking. How he listens, his ears (speech recognition, with what is downloaded
 * already), his voice (hear a sample of each), his machine's own microphone and speakers (and the echo canceller that
 * lets you interrupt him on its speakers), the instant commands answered without his brain, and, folded away, a
 * wake-word model of your own. Every option and range comes from the schema: a part this Orion doesn't have isn't drawn.
 */
import { Radio } from "@base-ui/react/radio"
import { RadioGroup } from "@base-ui/react/radio-group"
import { AudioLines, Ear, Headphones, LoaderCircle, Mic, Play, SlidersHorizontal, Square, Zap } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { previewVoice, useModels } from "@/lib/api"
import type { SettingsResponse, Voice } from "@/lib/types"
import { Pending } from "@/ui/states"
import { toast } from "@/ui/toast"
import { type Choice, Code, Field, Fields, FoldPanel, Panel, SectionFields, useSection, useSetting } from "./fields"

/** What the speech recogniser's "runs on" choices say. */
const RUNS_ON: Record<string, string> = { auto: "Auto", cuda: "GPU", cpu: "CPU" }

/**
 * The Voice tab.
 * @param props the `settings`
 * @returns the tab
 */
export function VoiceTab({ settings }: { settings: SettingsResponse }) {
  return (
    <>
      <Panel title="Listening" icon={Mic}>
        <Fields paths={["voice.enabled", "voice.name_wake", "voice.barge_in"]} />
        <Field path="voice.follow_up_seconds" format={(s) => (Number(s) ? `${s} s` : "Off")} />
        <Fields paths={["voice.end_silence_ms", "voice.max_utterance_s", "voice.chime"]} />
      </Panel>

      <Panel title="Ears" icon={Ear} description="Speech recognition">
        <Ears settings={settings} />
      </Panel>

      <Panel title="His voice" icon={AudioLines}>
        {settings.loading ? (
          <Pending label="His voice is loading" className="px-5 pb-4" />
        ) : (
          <Voices voices={settings.voices} />
        )}
        <Field path="tts.speed" />
      </Panel>

      <Panel title="His machine's audio" icon={Headphones} description="When no browser carries his voice">
        <Field path="voice.input_device" hint="The microphone he listens on." />
        <Field path="voice.output_device" hint="The speakers he talks on." />
        <Fields paths={["voice.echo_cancellation", "voice.noise_suppression"]} />
      </Panel>

      <Instant />

      <FoldPanel title="Advanced" icon={SlidersHorizontal} data-testid="voice-advanced">
        <Field
          path="voice.wake_model"
          hint={
            <>
              An openWakeWord model you trained, in <Code>data/models/openwakeword</Code>. Empty: his name wakes him.
            </>
          }
        />
        {settings.config.voice.wake_model && <Field path="voice.wake_threshold" />}
      </FoldPanel>
    </>
  )
}

/**
 * Speech recognition: which Whisper model (with its size, and whether it downloads when chosen) and where it runs.
 * @param props the `settings`
 * @returns the rows
 */
function Ears({ settings }: { settings: SettingsResponse }) {
  const stt = useModels().data?.stt
  const choices: Choice[] = settings.stt_models.map((o) => ({
    value: o.name,
    label: o.name === "auto" ? "Auto" : o.name,
    detail: [o.size, o.note, o.downloaded ? "" : "downloads when chosen"].filter(Boolean).join(" · "),
  }))
  const now = stt?.model ? `Now Whisper ${stt.model}, on the ${stt.device === "cuda" ? "GPU" : "CPU"}.` : undefined
  return (
    <>
      <Field path="stt.model" choices={choices.length ? choices : undefined} hint={now} />
      <Field path="stt.device" format={(v) => RUNS_ON[v] ?? String(v)} hint="Auto uses the GPU when there is one." />
    </>
  )
}

/**
 * Plays one voice's sample at a time: which one is loading or playing, and play/stop.
 * @returns `now` (the voice and whether it plays yet), `play` and `stop`
 */
function usePreview() {
  const [now, setNow] = useState<{ id: string; playing: boolean } | null>(null)
  const audio = useRef<HTMLAudioElement | null>(null)
  const turn = useRef(0) // a newer click wins over a sample still on its way

  const stop = useCallback(() => {
    turn.current++
    if (audio.current) {
      audio.current.pause()
      URL.revokeObjectURL(audio.current.src)
      audio.current = null
    }
    setNow(null)
  }, [])

  const play = async (id: string, speed?: number) => {
    stop()
    const mine = turn.current
    setNow({ id, playing: false })
    try {
      const wav = await previewVoice(id, speed)
      if (mine !== turn.current) return
      const sample = new Audio(URL.createObjectURL(wav))
      audio.current = sample
      sample.onended = () => audio.current === sample && stop()
      await sample.play()
      setNow({ id, playing: true })
    } catch (error) {
      if (mine !== turn.current) return
      setNow(null)
      toast.add({ title: "Voice", description: error instanceof Error ? error.message : String(error), type: "error" })
    }
  }

  useEffect(() => stop, [stop])
  return { now, play, stop }
}

/**
 * His voices by accent and gender: picking one saves it and says a few words in it; ▶ plays one without picking it.
 * @param props the `voices`
 * @returns the picker
 */
function Voices({ voices }: { voices: Voice[] }) {
  const { value: current, save } = useSetting<string>("tts.voice")
  const { value: speed } = useSetting<number>("tts.speed")
  const preview = usePreview()
  const accents = [...new Set(voices.map((v) => v.accent))]
  return (
    <div className="border-t px-5 pt-4 pb-4">
      <p className="mb-4 text-sm text-steel-400">
        Pick his voice: he says a few words in it. ▶ plays one without picking it.
      </p>
      <RadioGroup
        value={current}
        onValueChange={(id) => {
          save(id as string)
          void preview.play(id as string, speed)
        }}
        aria-label="His voice"
        className="grid gap-x-8 gap-y-5 @xl:grid-cols-2"
        data-testid="field-tts-voice"
      >
        {accents.map((accent) => (
          <div key={accent} className="grid content-start gap-3">
            <p className="caps text-steel-500">{accent}</p>
            {["female", "male"].map((gender) => {
              const list = voices.filter((v) => v.accent === accent && v.gender === gender)
              return (
                list.length > 0 && (
                  <div key={gender} className="grid gap-1.5">
                    <p className="text-xs text-steel-500 capitalize">{gender}</p>
                    <div className="flex flex-wrap gap-1.5">
                      {list.map((v) => (
                        <VoiceChip
                          key={v.id}
                          voice={v}
                          state={preview.now?.id === v.id ? (preview.now.playing ? "playing" : "loading") : undefined}
                          onPreview={() => (preview.now?.id === v.id ? preview.stop() : preview.play(v.id, speed))}
                        />
                      ))}
                    </div>
                  </div>
                )
              )
            })}
          </div>
        ))}
      </RadioGroup>
    </div>
  )
}

/**
 * One voice: ▶ (or ■ while its sample plays) and its name, which picks it.
 * @param props the `voice`, whether its sample is `loading` or `playing`, and `onPreview`
 * @returns the chip
 */
function VoiceChip({
  voice,
  state,
  onPreview,
}: {
  voice: Voice
  state?: "loading" | "playing"
  onPreview: () => void
}) {
  return (
    <div className="flex h-9 items-stretch overflow-hidden rounded-full bg-steel-50/5 shadow-[inset_0_0_0_1px_var(--hairline)] transition-[background-color,box-shadow] has-data-checked:bg-rigel-400/14 has-data-checked:shadow-[inset_0_0_0_1px_color-mix(in_oklch,var(--color-rigel-400)_50%,transparent)]">
      <button
        type="button"
        onClick={onPreview}
        aria-label={state ? `Stop ${voice.name}` : `Hear ${voice.name}`}
        className="grid w-9 place-items-center text-steel-400 outline-none transition-colors hover:bg-steel-50/8 hover:text-rigel-200 focus-visible:bg-steel-50/10 focus-visible:text-rigel-200"
        data-testid="voice-preview"
        data-voice={voice.id}
      >
        {state === "loading" ? (
          <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" />
        ) : state === "playing" ? (
          <Square aria-hidden="true" className="size-3 fill-current text-rigel-300" />
        ) : (
          <Play aria-hidden="true" className="size-3 fill-current" />
        )}
      </button>
      <Radio.Root
        value={voice.id}
        className="flex cursor-pointer items-center pr-3.5 pl-1 text-sm text-steel-200 outline-none hover:text-steel-50 focus-visible:bg-steel-50/10 data-checked:text-steel-50"
        aria-label={`${voice.name}, ${voice.accent} ${voice.gender}`}
      >
        {voice.name}
      </Radio.Root>
    </div>
  )
}

/**
 * Instant commands: timers, the volume, media, lights and the time, answered at once without his brain (and still
 * there when it is down); what of them this Orion lets the HUD set.
 * @returns the panel, or nothing on an Orion without them
 */
function Instant() {
  const shown = useSection("instant").length > 0
  if (!shown) return null
  return (
    <Panel
      title="Instant commands"
      icon={Zap}
      description="Timers, the volume, media, lights and the time, in under half a second, without his brain"
      data-testid="voice-instant"
    >
      <SectionFields section="instant" />
    </Panel>
  )
}
