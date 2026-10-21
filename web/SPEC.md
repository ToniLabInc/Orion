# ORION HUD 2 — design and build spec

Owner of the design: the lead (Claude, main session). This is binding for every frontend agent. When something is
unclear, choose what serves the principles below and say what you chose in your report.

**HUD 3 changed parts of this: §9 says what, and wins where it differs** (no Now rail, no Core orb, a Customize page,
an Activity page, new Settings tabs). The visual language (§2) stands.

Repository: `/home/you/Documents/GitHub/Orion`, HUD source in `web/`, built into `orion/hud/dist/` (served by
Orion's Python server). Read `web/README.md` first: it explains the build, the strict CSP and the current code.

## 0. What the user asked for

"Change the UI and the whole layout of Orion. Take all the frameworks, UI, interfaces and components and decide. Use
my website (Everything-Web) that has the tools. Impress me, go really hard and crazy, show the best capabilities you
can do on frontend." The user's own picks on that site: **Motion**, **Anime.js**, **Lenis**, **Spline**,
**Unicorn Studio** (WebGL shader scenes), **Kokonut UI** (animated shadcn components, MIT), **Bklit UI** (visx +
Motion charts, MIT), **Layers** (cinematic WebGL sections). They love cinematic WebGL, animated components and
design-engineered charts. We can't author Spline/Unicorn scenes (GUI tools), so we write our own shaders to that
standard.

## 1. Principles

1. **Spectacle that means something.** Every motion encodes real state: his state, a voice level, a tool call, a
   memory recalled, a timer running. Nothing moves "for decoration".
2. **The Core is alive.** A GPU particle entity (the "Core") is Orion's body: it breathes at rest, leans toward you
   while you speak, swirls while he thinks, pulses with his voice, throws comets to the cards his tools fill, and
   rests as the Orion constellation when nobody talks to him.
3. **Answers materialize.** A card that a tool fills arrives on a comet from the Core; a recall draws particles in.
4. **Calm at rest.** Hidden tab = no frames. Idle = low frame rate. `prefers-reduced-motion` = still frames and
   fades. The user games on this PC: the HUD must cost almost nothing when nothing happens.
5. **One way to show each thing.** A panel, readout or control exists once.
6. **Everywhere.** Phone, tablet, laptop, ultrawide; touch and keyboard; Orion may run on a server and the HUD is
   the user's microphone, speakers, screen and clipboard (see §6).
7. **Crisp.** Text never scales or blurs. Glass is rendered by WebGL behind the DOM, not by CSS blur (CSS
   `backdrop-filter` only as the fallback when the engine is off).

## 2. Visual language

### Colour (dark only; OKLCH; `src/palette.css` stays the only place colours are defined)
Keep the semantic token families and their meaning (they are good): `void` (space, backgrounds), `steel` (text,
lines), `rigel` (him: blue-white), `nebula` (thinking: magenta-violet), `aurora` (speaking: teal), `betel` (you:
Betelgeuse orange), `dusk` (loading: gold), `alert` (danger: red), and add `ok` (success: green). New values (each
family 50…900 like today; the 400 is the neon):

| family | hue | 400 | notes |
|---|---|---|---|
| void | 266 | — | 950 `oklch(0.085 0.02 268)` 900 `0.11 0.024 268` 850 `0.13 0.027 266` 800 `0.155 0.03 264` 700 `0.19 0.034 262` 600 `0.235 0.036 260` |
| steel | 255 | `oklch(0.675 0.02 255)` | 50 `.975/.006` … 900 `.21/.012` |
| rigel | 232 | `oklch(0.80 0.11 232)` | him at rest, primary accent |
| nebula | 315 | `oklch(0.70 0.20 315)` | thinking |
| aurora | 185 | `oklch(0.83 0.13 185)` | speaking |
| betel | 50 | `oklch(0.75 0.17 50)` | you, listening |
| dusk | 85 | `oklch(0.82 0.13 85)` | loading |
| alert | 25 | `oklch(0.68 0.21 25)` | warnings, failures, a risky action |
| ok | 150 | `oklch(0.78 0.15 150)` | done, connected |

`<html data-state>` drives `--state` (idle→rigel, listening→betel, thinking→nebula, speaking→aurora,
loading→dusk) as today, so a state change re-renders nothing. Code that draws reads colours via `lib/palette.ts`.

### Type (self-hosted via Fontsource; nothing from a CDN)
- UI: **Geist** (`@fontsource-variable/geist` 5.3.0), 400/500/600, `font-feature-settings: "ss01","cv11"`.
- Machine values: **Geist Mono** (`@fontsource-variable/geist-mono` 5.3.0), tabular numbers.
- Display & his voice: **Instrument Serif** (`@fontsource/instrument-serif` 5.3.0) — his spoken captions in italic,
  big headings and the clock/temperature numerals in regular. The ORION wordmark: Geist 600, tracking 0.42em.
- Scale (px / line-height): micro 11/14, xs 12/16, sm 13/18, base 14.5/21, md 16/24, lg 19/26, xl 24/30,
  2xl 32/36, 3xl 44/48, 4xl 64/64, display 96/92. Nothing under 11 px.

### Shape, depth, glass
- Radius: 6 (chips), 10 (inputs, buttons), 14 (menus), 20 (cards), 28 (sheets).
- Hairlines: 1 px `color-mix(in oklch, var(--color-steel-300) 12%, transparent)`, with a 1 px top inner highlight.
- Glass card: DOM background `color-mix(in oklch, var(--color-void-850) 58%, transparent)`; the blur/refraction of the
  living scene behind it is drawn by the Core engine at the card's rect (§4.6). Fallback when the engine isn't
  running: `backdrop-filter: blur(18px) saturate(1.4)`.
- Elevation shadow: `0 30px 80px -24px oklch(0 0 0 / 0.65)`.

### Motion
- Libraries: **Motion** (`motion` 13.5.0, already a dependency) for layout, presence, gestures and springs;
  **Anime.js 4** (`animejs` 4.5.0) for timelines, text splitting/scramble and SVG line drawing; **Lenis**
  (`lenis` 1.3.26) for smooth scrolling of long pages (Memory list, Automations, Settings) in wrapper mode — never on
  the conversation (it must stick to the bottom crisply).
- Tokens in `lib/motion.ts`: durations instant 90 ms, quick 160, base 240, slow 420, ambient 1200; easing
  `out-expo` `cubic-bezier(0.16,1,0.3,1)`; springs snappy {420, 32}, gentle {180, 24}, float {90, 18}.
- Reduced motion: every animation becomes instant or a 120 ms fade; the Core renders still frames.

### Icons
**lucide-react** 1.55.0 (ISC) replaces Tabler. 1.5 px strokes at 16/20 px.

### Sound
Keep `lib/sound.ts` (soft UI sounds, `hud.sounds` setting). Add a subtle tick for confirmations.

## 3. Layout and information architecture

### Routes (wouter; each page a lazy chunk)
- `/` **Stage**: the Core, the conversation, the composer, the Now rail, the cards.
- `/memory` **Memory**: the 3D neural map (port the current one: react-force-graph-3d) restyled, with its inspector.
- `/automations` **Automations**: scheduled jobs, watchers (sensors and their alerts), skills (moved here from
  settings).
- `/settings/:tab` **Settings**: general, brain, voice, eyes & safety, abilities (MCP servers and the integrations
  catalog), devices (paired devices, the devices connected now, this machine's profile), gestures.
- Pairing (unpaired device) and link states (connecting, offline, refused) as full-screen overlays, as today.

### Stage, desktop (≥ 1100 px)
```
┌ top bar ─────────────────────────────────────────────────────────────────────────────────────┐
│ ORION · state label (animated)              ⌘K search…              device chips · 12:41 · ⚙ │
├──────────┬─────────────────────────────────────────────────────────────────────┬─────────────┤
│ NOW rail │                    the Core (full-bleed canvas behind all)          │  card stack │
│ 260 px   │                                                                     │  380 px     │
│ confirms │      conversation column (max 720 px, centred, bottom-anchored)     │  weather    │
│ alerts   │      messages as glass cards; older ones fade upward                │  calendar   │
│ next up  │                                                                     │  vitals …   │
├──────────┴───────────────────── composer dock (max 760 px) ────────────────────┴─────────────┤
│   [● talk]  [ type to Orion…  (attachments)            📎  🖥  📷 ]  [send]   brain · voice  │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```
- The conversation column can collapse to "captions only" (his last reply in Instrument Serif italic under the
  Core) — the ambient mode when nobody types; it expands when you scroll up or type.
- Cards are summoned (voice: "show me the weather", the ⌘K palette, the Now rail) or pinned; Esc dismisses the
  newest; a card can be pinned (stays) or transient (leaves after 90 s).

### Stage, tablet (700–1099 px)
Rail collapses to an icon strip; cards open as a right sheet (Vaul `vaul` 1.1.2 drawer, or Base UI dialog).

### Stage, phone (< 700 px)
Core as a header (40 vh, shrinks as the conversation grows); conversation fills; composer fixed at the bottom with a
**big press-and-hold talk button**; Now and cards are bottom sheets opened by swipe or chips.

### Command palette (⌘K, also `/`)
One navigator for everything: cards, pages, settings tabs, skills, jobs, memory search, actions (new chat, mute,
take the voice, share screen…). Built on Base UI (Dialog + Combobox/Autocomplete), no cmdk.

## 4. The Core engine (`web/src/core/`)

Raw **WebGL 2** in a **worker** (OffscreenCanvas; fallback on the main thread when unsupported), gl-matrix for
matrices, no three.js here (three stays for the Memory page). One full-viewport canvas, fixed behind the DOM.

### 4.1 API (main thread)
```ts
const core = createCore(canvas, { quality: "auto" | "high" | "low" | "off", reducedMotion })
core.state(s: "idle" | "listening" | "thinking" | "speaking" | "loading" | "error")
core.level(source: "mic" | "voice", value: number, bands: number[])   // ≤ 30 Hz, never through React
core.pointer(x, y)                     // normalized −1..1, for parallax and the Core's gaze
core.panels(rects: { id, x, y, w, h, r }[])   // CSS px; the glass is drawn behind each (§4.6)
core.comet({ to: { x, y } | { panel: id }, hue: "rigel" | "nebula" | "aurora" | "betel" | "alert" | "ok", label? })
core.recall(count: number)             // particles drawn in from the edges (memories arriving)
core.morph({ text?: string, shape?: "ring" | "constellation" | "question", ms: number })
core.rest(on: boolean)                 // the constellation (nobody talking for a while)
core.visibility(visible: boolean); core.dispose()
```
React only calls these through `useCore()` actions; levels go straight from the WS layer (no React renders).

### 4.2 Simulation (GPGPU)
- Ping-pong RGBA32F (or RGBA16F when `EXT_color_buffer_float` is missing) textures: position.xyz + life, velocity.xyz
  + seed. 256² = 65 536 particles (high), 160² (medium), 96² (low/mobile).
- Forces in the simulation fragment shader: 3D curl noise (simplex derivatives); attraction to the current target
  (a sphere shell whose radius breathes; or a morph target texture); state terms — listening: a lean toward the
  viewer/bottom scaled by mic level and an orange rim; thinking: orbital swirl around 2–3 tilted axes + faster
  noise; speaking: radial pressure waves from voice bands; loading: slow gold contraction; error: a short red
  shudder. Damping, speed caps, respawn by life.
- Rest ("constellation"): after 60 s idle with no conversation open, attractors move to the **Orion constellation**
  (Betelgeuse, Bellatrix, Meissa, Alnitak, Alnilam, Mintaka, Saiph, Rigel; the sword with M42 as a soft nebula
  cloud), particles cluster on the stars (Betelgeuse orange, Rigel blue-white) and flow along the figure's lines.
  On wake they rush back into the Core sphere in ~800 ms.

### 4.3 Rendering
Background (starfield 2 layers with pointer parallax + nebula fog at ¼ res, refreshed at 12 fps) → particles as
GL_POINTS (additive, size by depth, colour by state + speed, soft disc) into a half-res HDR target → bloom (dual
Kawase, 3 passes) → composite with tone mapping and a hint of film grain → glass (§4.6) → comets.

### 4.4 Comets
A head (bright point + glow) on a cubic Bézier from the Core to the target with a fading trail of ~40 samples;
360–700 ms; on arrival a ring ripple on the target panel's edge. Max 6 alive.

### 4.5 Morph
Text or a shape is rasterised on an OffscreenCanvas 2D inside the worker (Geist loaded with `FontFace` in the
worker; fall back to sans-serif), N points sampled from filled pixels, uploaded as a target texture; particles blend
to it over 600 ms, hold, release. Used sparingly: the time when asked the time, the temperature for the weather,
"?" while a confirmation waits, the timer duration when one starts.

### 4.6 Glass
Each frame, the composited scene (before glass) is mip-blurred once; for each panel rect: a rounded-rect SDF, sample
the blurred scene with a small refraction offset along the SDF normal near the edges, a 1–2 px chromatic split at
the rim, a soft top highlight and a faint state-coloured rim light. The DOM card above has a translucent
background and its content. Rects come from `core.panels()`, driven by a `useGlass(ref, id)` hook (ResizeObserver +
a rAF loop only while a Motion animation runs).

### 4.7 Performance
- Hidden tab or off-screen: no frames. `visibility` stops the loop.
- Idle and still: 24 fps with half the simulation steps; active: 60 fps.
- Adaptive quality: average frame time > 18 ms for 2 s → step down (particles, bloom passes, DPR 1.5 → 1.25 → 1);
  10 s of headroom → step up. Settings → General → This screen: quality auto/high/low/off (a HUD preference stored in
  localStorage, like today's `prefs.ts`).
- No allocations in the frame loop; uniforms updated in place; one draw call per pass.

## 5. Data layer (keep and extend `web/src/lib/`)
Keep `ws.ts`, `store.ts`, `api.ts`, `types.ts`, `prefs.ts`, `format.ts`, settings schema fields logic, neural graph
logic, hands drawing logic, streaming Markdown (`features/comms/markdown.tsx`), and their tests (move/rename as
needed; keep them green).

### 5.1 WebSocket `/ws` — events (server → HUD), new or changed
- `init` gains: `surface` (this tab's device id), `confirms` (questions waiting), `host` (this machine's profile:
  platform, container, session, desktop, dbus, opener, sound, power, battery, temperatures, nvidia, clipboard,
  screen), `surfaces` (devices connected), `voice_holder` {device, name}, `jobs` (§5.3), `alerts` (§5.4).
- `confirm` {id, turn, tool, title, server, action, detail, reason ("content"|"always"), source, text,
  source_kind, expires (epoch s)} → show the confirmation (§7.4). `confirm_done` {id, answer: yes|no|timeout}.
- `tool` gains `phase: "ask"` {id, turn, name, question, text}: the operation waits for the user's yes.
- `user_message` gains `device` and `images` (data URLs of the pictures sent).
- `capabilities.models` gains `fallback` (label of the stand-in brain, "" for none), `standing_in` (bool), `reason`.
- `surfaces` {items: [{id, device, local, caps[], sharing[], seen}]}; `voice_holder` {device, name}.
- `listen` gains `purpose: "answer"`; `transcript` gains `purpose: "answer"`.
- Targeted to one tab (only that tab receives them): `request` {id, kind: "frame"|"clipboard_read"|
  "clipboard_write"|"share", what?: "screen"|"camera", text?}; `speaker` {gen}; binary frames of his voice
  `[0x02][u32 LE generation][int16 LE PCM, 24 kHz mono]`.
- `jobs` {items}, `job_run` {id, phase: "start"|"end", ok?, text?} (§5.3). `alerts` {items}, `alert` {item} (§5.4).
- Existing events stay as they are (see `types.ts`).

### 5.2 WebSocket — messages (HUD → server), new or changed
- `hello` {device: "Firefox on Linux", caps: ["mic","speaker","screen","camera","clipboard"]} — right after `init`;
  a cap only when the browser can (secure context, API present; `getDisplayMedia` absent on phones).
- `chat` {text, images?: [data URL]} (at most `hud.max_pictures`, JPEG ≤ 1600 px long side, made in the browser).
- `voice_take` {on: bool} — this tab carries the voice (its microphone and speakers) or lets go.
- `talk` {} — record a request now, without his name (the talk button; only while this tab carries the voice).
- binary `[0x03][int16 LE samples, 16 kHz mono]` — the microphone, only while this tab carries the voice (the server
  also reads `[0x01][float32 LE]`).
- `sharing` {what: ["screen"] | ["camera"] | []} — what this tab shares now.
- `answer` {id, …result} or {id, error} — the answer to a `request` (frame: {image: data URL JPEG};
  clipboard_read: {text}; clipboard_write: {}; share: {} once sharing started, {error} if the user declined).
- `confirm` {id, yes: bool}.
- Existing messages stay (`chat`, `voice`, `mute`, `stop`, `reset`, `hands_view`).

### 5.3 Jobs (scheduled work) — REST + events
`GET /api/jobs` → `Job[]`; `POST /api/jobs` {name, schedule (cron), prompt, deliver: ("speak"|"hud"|"discord")[],
enabled} → Job; `PUT /api/jobs/{id}` (same fields, partial) → Job; `DELETE /api/jobs/{id}`; `POST /api/jobs/{id}/run`.
`Job` = {id, name, schedule, explain (English, e.g. "At 07:30 on weekdays"), prompt, deliver[], enabled, next_run
(epoch s | null), last_run (epoch s | null), last_status ("ok"|"error"|""), last_text, created}.
`POST /api/jobs/explain` {schedule} → {explain} or 400 {detail} (live validation while typing).

### 5.4 Watchers and alerts
`GET /api/watchers` → {sensors: {battery?: {percent, plugged, secs_left}, temperatures?: {cpu?, gpu?} (°C), disk:
{percent, free_gb}, memory: {percent}}, rules: [{id, title, enabled, available, detail}]}. Thresholds are settings
(`watchers.*`, edited through the schema-driven Settings fields). Alert = {id, rule, level: "info"|"warn"|"critical",
title, text, at}; `alerts` {items} (the active ones), `alert` {item} (a new one).

### 5.5 Other REST (new)
- `GET /api/calendar?days=7` → {sources: [{name, ok, error}], events: [{id, source, calendar, title, start, end,
  all_day, location}]} (from the built-in calendar group; empty when none is set up).
- `GET /api/catalog` → integrations offered (§7.9): [{id, title, description, kind ("mcp"|"builtin"), fields:
  [{key, label, secret, placeholder, help}], installed, docs}]; `POST /api/catalog/{id}` {values} → installs it
  (adds the MCP server or fills the built-in settings).
- Existing endpoints stay (`api.ts`).

## 6. The browser as a device (`web/src/device/`, `web/src/audio/`)
- `hello`: detect caps — `mic`/`speaker` when `navigator.mediaDevices?.getUserMedia` exists and `isSecureContext`;
  `screen` when `getDisplayMedia` exists (desktop browsers); `camera` when a videoinput may exist; `clipboard` when
  `navigator.clipboard` exists. Device name from the UA ("Firefox on Linux", "Safari on iPhone").
- **Voice**: the talk button takes the voice (`voice_take`), opens the mic with `{echoCancellation, noiseSuppression,
  autoGainControl: true}`, an AudioWorklet resamples to 16 kHz mono and posts 40 ms blocks; send each as a binary
  int16 frame. Playback: an AudioWorklet ring buffer at 24 kHz (resampled to the context rate), 120 ms jitter
  target; a `speaker {gen}` or a frame with a newer generation drops what it holds; frames of an older generation
  are dropped. Mic level drives the Core (`core.level("mic")`) and the talk button's ring. Hold (or tap) the talk
  button → `talk`. When not a secure context, the talk button explains: "Open the HUD over HTTPS (or localhost) to
  talk from this device".
- **Share**: screen (`getDisplayMedia({video: true})`) or camera (`getUserMedia({video: {facingMode: "user"}})`)
  into a hidden `<video>`; a frame = draw to a canvas, `toDataURL("image/jpeg", 0.85)`, longest side ≤ 1600 px.
  A `request` kind `share` shows a prompt card ("Orion wants to see your screen — Share / Not now"): browsers need
  the user's click to start sharing; answer `{}` once sharing or `{error: "the user said no"}`. Show a live
  thumbnail chip while sharing, with a stop button; send `sharing` on every change.
- **Clipboard**: `clipboard_read` → `navigator.clipboard.readText()` (may need focus/permission; answer the error
  text if it fails); `clipboard_write` → `writeText`; if writing fails without a user gesture, show a toast with a
  "Copy" button and answer `{}` after the click.

## 7. Features

### 7.1 Conversation
Port the current comms logic (grouping, streaming deltas at ≤ 25 Hz, Markdown of the last block only, the
OPERATIONS trace). Messages are glass cards: yours right-aligned with a betel hairline, his left with a rigel one;
his reply streams with a soft caret; pictures you sent show as thumbnails; the trace shows each tool as a compact
chip (icon by server, title, duration, ok/failed), expandable (args, result preview, and **pictures returned**).
A step in phase "ask" glows alert-orange with the question and the two buttons inline.

### 7.2 Composer
Auto-growing textarea, Enter sends, Shift+Enter newline, paste/drop images (compressed in the browser), attachment
chips; buttons: talk (with level ring), share screen, camera, attach; shows the brain label (and a "stand-in" badge
with the reason when `standing_in`), the voice holder chip, and a stop button while he speaks or thinks.

### 7.3 State label and captions
Top-left: ORION wordmark + the state in words ("At rest", "Listening", "Thinking · Search the web", "Speaking"),
animated with Anime.js text split (letters slide/fade on change). His spoken caption (`caption` events) appears under
the Core in Instrument Serif italic, word by word, in ambient mode.

### 7.4 Confirmations
`confirm` → a glass dialog pinned top-centre with an alert rim: the question (`text`), the action, the detail
(monospace, scrollable), why ("this request used content from …"), a countdown ring to `expires`, **Yes** /
**No** (keyboard Y / N), sound tick; the Core morphs a "?" while it waits; `confirm_done` closes it everywhere.

### 7.5 Now rail
One vertical timeline: waiting confirmations, active alerts, then upcoming items in time order — timers (live
countdown rings), reminders, calendar events (from `/api/calendar`), next job runs (from `jobs`). Click → the
matching card or action. Empty: "Nothing ahead."

### 7.6 Cards (each a glass card; summon ids in parentheses — keep the existing ids)
weather (`weather`: hourly curve with visx, 7-day strip), vitals (`vitals`: CPU/GPU/memory live line charts in
the Bklit style with visx + Motion, temps, battery, top programs), calendar (`calendar`, new: agenda by day),
timers & reminders (`timers`), tasks (`tasks`), news (`news`), jobs (`jobs`, new: list with next run, run-now,
toggle, last result), hands (`hands`: port the live view), eyes (`eyes`, new: the shared screen/camera preview with
stop), devices (`devices`, new: connected devices, who carries the voice, take/give it). Charts: smooth curves,
gradient fills, Motion-animated paths on data change, hover crosshair with values.

### 7.7 Automations page
Tabs: Jobs (list + editor: name, schedule with a live English explanation from `/api/jobs/explain`, prompt,
deliver checkboxes, enabled; run now; last result), Watchers (sensor readouts + each rule's switch and threshold via
the schema fields of `watchers.*`), Skills (move the current skills editor here).

### 7.8 Memory page
Port the neural map (react-force-graph-3d) with the new palette, node inspector as a glass side sheet, search via
the palette. Keep its "draws only while something happens" behaviour.

### 7.9 Settings
Schema-driven as today (`<Field path=…/>`; keep `fields.tsx` logic): General (incl. This screen quality), Brain
(provider, fallback brain + its status), Voice, Eyes & Safety (`eyes.*`, `safety.confirm_after_content`), Abilities
(MCP servers and tools as today, each tool's `unavailable` reason shown greyed; plus the **Integrations catalog**
from `/api/catalog`: Home Assistant, Browser control, Mail, Calendar, Google Workspace, Microsoft 365, Discord —
each a card with its fields and an Install button), Devices (pairing as today + connected devices + this machine's
profile from `init.host`), Gestures (port).

### 7.10 Boot and link
A short boot sequence (≤ 1.6 s, skippable, once per session): the Core condenses from the starfield while the boot
steps tick (from `boot` events). Link lost: the Core dims and desaturates, a top banner says so, retrying.

## 8. Engineering rules
- TypeScript strict; Biome lint/format (`npm run lint`); Vitest for pure logic (`*.test.ts` next to the code).
- Exact versions only (`npm --prefix web i -E name@x.y.z`), licences MIT/Apache/BSD/ISC (fonts: OFL).
- The CSP stays strict: no CDN, no inline script, no WebAssembly, workers and worklets from our own origin.
- Never `#rrggbb` in .ts/.tsx; colours from `palette.css` / `lib/palette.ts`.
- Nothing loops on the main thread at rest; levels never enter React state; panels move by transform.
- Accessibility: focus rings (rigel), aria labels on icon buttons, dialogs trap focus, contrast ≥ 4.5:1 for text.
- **Resource caps (the user games on this machine):** every build, test or dev server runs inside
  `systemd-run --user --scope -p MemoryMax=3G -p CPUQuota=300% nice -n 10 …`. Never run a headless browser except
  one at a time inside that scope with `MemoryMax=2G`. Commands go through uv's Node:
  `uv run --only-group web npm --prefix web <cmd>`.
- Do **not** commit `orion/hud/dist/` (the lead builds it at the end). Verify bundling with
  `uv run --only-group web npx --prefix web vite build --outDir /tmp/<scratch>/dist` (or `npm run typecheck` +
  `npm test`).
- Commit in your worktree with clear messages ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## 9. HUD 3: what changed

The principles, the visual language (§2), the data layer (§5), the browser as a device (§6) and the engineering rules
(§8) stand. What changed:

- **No Now rail, no orb.** The rail is gone; what is live moves to the top bar's chips (the next timer counting down, his
  questions waiting, the watchers' alerts: each opens what it counts) and to a **Today** card (`today`: the calendar's
  events, reminders, timers and the next job runs for today and tomorrow; "show me today", the palette). The Core is
  replaced by **Horizon** (`src/core`, its own spec): the night sky, the world's limb and his light as an airglow, not
  an object. The composer sits on the limb, its talk button where the voice plucks the light; the conversation above it,
  folded to captions as before. A meteor from the limb reaches a card or a message when a tool answers; the engine draws
  no glass, so `glass` is CSS alone (a tint and a blur).
- **Customize** (`/customize/connectors|skills|plugins`, a page for each thing) mirrors Claude's: each tab has "Yours"
  and "Discover" (search, categories). A connector is anything that gives him tools (his own groups, the services you
  add, what plugins bring): its page has Overview, Tools (each with its risk and a switch), Settings (its own: fields,
  your documents' folders, a gesture remote's gestures), Security (whose code it is, the sandbox, its secrets, which
  channels may use it) and Activity. A skill's page edits its SKILL.md and reads its files; a plugin's lists its skills,
  connectors and commands, and what Orion never runs.
- **Activity** (`/activity`): what he did and who allowed it, by day, live, filtered by kind and source, searched, with
  "his own" (jobs, watchers, Discord), each entry opening to its record. The alert chip opens it filtered.
- **Settings**: General · Brain (provider and stand-in; what OpenRouter costs against a daily budget; how fast each
  brain answers, by stage; a benchmark) · Voice (+ echo cancellation, noise suppression, instant commands) · Memory (+
  backups) · Safety (asking first; what each channel may do; the sandbox; the vault) · Devices. Abilities and Gestures
  are gone from it (a connector's own settings are on its page); Skills moved from Automations to Customize.
- **Each turn's stages** (heard · think · tools · speak, in ms) show under his answer as a thin bar and figures; an
  answer an instant command gave carries an Instant mark.
- **Routes**: `/settings/abilities` and `/settings/eyes` lead to Customize's connectors and Safety, `/settings/gestures`
  to the first gesture remote's own page, `/automations/skills` to Customize's skills.

