# The HUD's source

A React app built with Vite into `../orion/hud/dist`, which Orion's server serves and git keeps (installing Orion
needs no Node). Node comes from PyPI through uv's `web` group, so run everything from the repo root:

```sh
uv run --only-group web npm --prefix web ci          # install exactly what package-lock.json says
uv run --only-group web npm --prefix web run dev     # http://127.0.0.1:5173, live against the running Orion (:8765)
ORION_URL=http://127.0.0.1:8770 uv run --only-group web npm --prefix web run dev   # …or against another Orion
uv run --only-group web npm --prefix web run build   # class tables, typecheck, then build into orion/hud/dist (commit it)
uv run --only-group web npm --prefix web run lint    # Biome (format: `run format`); typecheck: `run typecheck`
uv run --only-group web npm --prefix web test        # Vitest: the unit tests (*.test.ts), in Node
```

Build, test and run inside a capped scope (`systemd-run --user --scope -p MemoryMax=3G -p CPUQuota=300% nice -n 10 …`).
Add a package with an exact version (`npm --prefix web i -E name@x.y.z`), MIT/Apache/BSD/ISC only. The built HUD runs
under a strict CSP (orion/server.py): no CDN, no inline script, no WebAssembly, workers from its own origin only.
`SPEC.md` is the design: what the HUD shows and why.

## Where things are

| | |
|---|---|
| `src/main.tsx`, `src/app.tsx` | start-up and the routes (`/`, `/memory`, `/automations/:tab`, `/activity`, `/customize/...`, `/settings/:tab`), each page its own lazy chunk; an address that moved (`/settings/abilities`, `/settings/gestures`, `/automations/skills`) is sent where it lives now (`lib/nav.ts`, `movedTo`) |
| `src/palette.css`, `src/styles.css` | every colour (OKLCH tokens, `--state` from `<html data-state>`); the type scale, radii, shadows, motion tokens and the few utilities (`glass`, `glass-solid`, `caps`, `fade-top`) |
| `src/core/` | Horizon (its own engineers): `createCore` draws the sky, the world's limb and his light with WebGL 2 in a worker on a full-screen canvas. The HUD mounts it once (`shell/core-host.tsx`) and talks to it through `lib/core.ts` only (`core-demo.html` shows it alone) |
| `src/device/`, `src/audio/` | this browser as one of his devices (`createDeviceLink`): microphone, speaker, a shared screen or camera, the clipboard. `lib/ws.ts` and `lib/device.ts` wire it |
| `src/shell/` | the frame round every page: `core-host.tsx`, `top-bar.tsx` (the live chips: next timer, questions waiting, alerts) and `state-label.tsx`, `palette.tsx` (⌘K, ranked by `rank.ts`), `boot.tsx`, `link.tsx` (link lost, restarting), `pair.tsx`, `gesture.tsx` |
| `src/stage/` | the home page: `index.tsx` (desktop, tablet and phone layouts; the stage's keys), the conversation (`conversation.tsx`, `message.tsx`, `trace.tsx`, `stages.tsx` (a turn's measured stages), `markdown.tsx`, `captions.tsx`), the composer (`composer.tsx`, `talk.tsx`, `attachments.ts`, `sharing.tsx`), `cards.tsx`, `confirmation.tsx` |
| `src/cards/` | one module per card (today, weather, vitals, calendar, timers, tasks, news, jobs, hands, eyes, devices), `agenda.ts` (the Today card's agenda, pure), `chart.tsx` (the visx charts' parts), `globe.tsx`, `hands-view/` |
| `src/pages/` | `frame.tsx` (the frame Settings and Customize share: a column of links, a smooth-scrolling page), `memory/` (the 3D map), `automations/` (jobs, watchers), `activity/` (the log), `customize/` (connectors, a connector's page with its settings, security and activity, skills, plugins, the gesture editor), `settings/` (general, brain with speed, spend and benchmark, voice, memory with backups, safety with channel limits, sandbox and vault, devices; schema-driven through `fields.tsx`) |
| `src/ui/` | the kit on Base UI in the HUD's look: buttons, dialogs, sheets, select, switch, slider, tabs, search field, toasts, tooltips, pending/empty/failed states |
| `src/lib/` | `ws.ts` (the live connection), `store.ts` (his state, the conversation), `stage.ts` (the cards summoned), `api.ts` (one hook per REST endpoint), `types.ts` (every event and response), `core.ts`, `device.ts`, `nav.ts` (pages, tabs, moved addresses), `prefs.ts` (this browser's preferences), the pure logic with its tests (`activity.ts`, `bench.ts`, `connectors.ts`, `rules.ts`, `skills.ts`, `stages.ts`, `format.ts`, `horizon.ts`,
`light.ts` (what is written in light), `palette.ts`, `hands.ts`), `icons.ts` (a connector's icon from its name), `hooks.ts`, `motion.ts`, `sound.ts`, `scroll.ts` |
| `scripts/mock_orion.py` | a stand-in Orion for looking at the HUD (below), with its data in `mock_data.py` and its state in `mock_state.py` |

## Rules

- **Colours from palette.css only**: `void` (backgrounds), `steel` (text: informative text is steel-500 or brighter),
  `rigel` (him), `nebula` (thinking), `aurora` (speaking), `dusk` (loading), `betel` (you, and only you), `alert`
  (warnings and failures), `ok` (done, healthy). Code that draws takes them from `lib/palette.ts`; a `#rrggbb` in a
  .ts/.tsx file is a mistake.
- **Type**: Geist for the interface, Geist Mono for machine values and times, Instrument Serif for the big numbers and
  the titles; sizes from the scale only.
- **Glass**: `glass` (cards) and `glass-solid` (what floats over everything) are CSS alone: a translucent tint over
  the sky with a blur behind it. A message is flat glass (`data-flat`: the tint without the blur, its list fades out).
  The engine knows nothing of any of it; a meteor reaches a card or a message through `useMeteorTarget` (an element
  registers its id, and the point is measured as the meteor flies).
- **Where the limb is**: the stage's talk button sits on the world's limb. `useHorizon` (lib/core.ts) tells the
  engine where it is, when the button mounts and when its dock or the window changes size, and says nothing at rest.
  On any other page (a direct visit to Settings) the engine is told the place the stage would put it
  (`lib/horizon.ts`, checked against the stage's real geometry at nine screen sizes), so the sky is always there.
- **Motion** answers a change, with the tokens in `lib/motion.ts`; under reduced motion it is instant or a short fade
  (Motion's `MotionConfig`, no Lenis, no boot).
- **One way to show a thing**: a card, a readout or a control exists once. Before adding one, look for it.

## Keeping it light

- **Nothing loops on the main thread at rest.** Audio levels never enter React (`onLevel`, `onMicLevel`: the Core and
  the talk button's ring take them by direct writes). `useNow` readers share one timer per period and tick only while
  read, at the coarsest period that shows (a timer's chip in the bar ticks while a timer runs). Lenis runs only while a
  page scrolls. A state change re-renders no card: CSS reads `<html data-state>`. The activity log's long list is
  skipped by the browser's layout and paint while off screen (`content-visibility`), and pages in as you reach its end.
- **Streaming text**: deltas are applied 25 times a second at most, and only the last Markdown block of an answer is
  parsed again.
- **The first load stays small**: every page, the gesture chip and the command palette (with its connector icons) are
  their own chunks; the palette's is fetched a few seconds after start, so ⌘K still opens it at once.
- **The memory map** (react-force-graph-3d on three.js, its own chunk) draws only while something happens.

## Data

- Read narrow slices of the store: `useLive((s) => s.state)`, `useShallow` for several fields. One-off moments:
  `onEvent("confirm", fn)`.
- Send with `say(text, images)`, `stop()`, `newChat()`, `setMuted(v)`, `setVoice(v)`, `answerConfirm(id, yes)`;
  timers, reminders and tasks change through his organizer's tools (`useOrganizer()`).
- This browser's voice: `pressTalk()` (takes the voice here first if it isn't), `takeVoice()`, `releaseVoice()`,
  `share("screen" | "camera")`, `stopShare()`.
- Settings: every option comes from the server's schema (`<Field path="voice.chime" />` picks its control, label,
  range and choices; `<SectionFields section="backup" />` all of a section's); the HUD keeps no defaults of its own, and
  a panel whose options the schema lacks isn't drawn. A connector's own settings are not in the schema: they come with
  the connector (`GET /api/connectors/{id}`, its `settings`) and are shown on its page.
- Connectors, skills, plugins, the activity log, secrets and channel limits: one hook per endpoint in `lib/api.ts`; the
  `connectors`, `plugins`, `activity`, `bench` and `documents` events put their data straight into those queries
  (`lib/ws.ts`), so nothing polls. The vault never sends a value to the HUD: it holds names and whether each is set.
- A benchmark is planned first (`POST /api/bench/plan`: nothing starts); one that costs (your Claude plan, paid
  OpenRouter) is shown to you and started with `confirm: true`, and can be stopped; its progress is the `bench` event.
- Cards: `present(id)`, `dismiss(id)`, `pin(id, on)`; `summoned(action, parts)` takes the server's "summon" event
  ("show me today" summons the Today card).

## Tests and screenshots

Unit tests sit next to the code (pure functions, in Node).

`scripts/mock_orion.py` serves a built HUD with realistic data and a `/ws` that plays a conversation, so the HUD can be
looked at without a model or your own data. Every endpoint and event of the connectors, skills, plugins, activity,
vault, channel limits, timings, spend, benchmark, backup and documents pages exists (`mock_state.py`) and keeps what you
change until it is restarted:

```sh
uv run --only-group web npm --prefix web run build -- --outDir /tmp/hud-dist --emptyOutDir
uv run --no-project --with pydantic --with pyyaml --with cronsim --with tzlocal --with google-re2 python web/scripts/mock_orion.py --dist /tmp/hud-dist
# http://127.0.0.1:8790/ (live), or a scene: ?scene=idle, conversation, cards, today, chips, instant, phone, offline, unpaired
# any page with any scene: /customize/connectors?scene=idle, /activity?scene=idle, /settings/brain?scene=idle
```

A page without a scene gets the mock's live `/ws`: it plays the conversation (`--live idle` keeps it quiet), and a
benchmark started there advances and can be stopped, with its `bench` events.

A scene feeds its events through `window.__orion.receive(event)`, which takes any server event:

```js
window.__orion.receive({type: "summon", action: "show", panels: ["today", "weather"], source: "voice"})
```

Headless browsers one at a time, inside the capped scope with `MemoryMax=2G`. Under `--virtual-time-budget` a headless
shell draws no frames, so animations stay at their first frame: drive it over the DevTools protocol in real time instead.
