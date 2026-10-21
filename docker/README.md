# Docker

Orion can run in a container instead of from `./install.sh`. The image holds the Python backend exactly as
`uv.lock` pins it and the HUD's committed build. Your settings, memory, skills, models and logs live in a volume, never
in the image. Run him in Docker or natively, not both at once: they would want the same port (8765).

```sh
./docker/up.sh                    # look at this machine, build and start; the first start downloads his models
docker compose logs -f            # follow him; "ready in …" means every part has loaded
docker compose down               # stop (his data stays in the volume)
git pull && ./docker/up.sh        # update
```

Then open <http://localhost:8765>. The container uses the host's network, so his server still binds `127.0.0.1` only,
on the machine's own loopback, and still answers only his own pages. No port is published to your network.

## What `up.sh` looks at

The same repo runs on a laptop, a desktop or a server, so `up.sh` checks what this one has each time it runs:

| It looks for | When it's there | When it isn't |
|---|---|---|
| your user (`id -u`, `id -g`) | his files in the data folder are yours | |
| an NVIDIA GPU that Docker can hand over (NVIDIA's container toolkit) | the image gets CUDA's cuBLAS and Whisper runs on the GPU (`large-v3-turbo`) | Whisper runs on the CPU (`small.en`) |
| a sound server for your user (PipeWire or PulseAudio) | the microphone and speakers: he listens for his name and speaks | the HUD's talk button: your browser's microphone and speakers |
| the time zone (`/etc/localtime`) | your local time for the clock, timers and reminders | UTC |

It writes what it found to `compose.override.yaml`, which Docker Compose merges into `compose.yaml` by itself (so
`docker compose logs`, `down` and the rest keep working), then builds and starts him. That file belongs to the machine
(git ignores it): run `up.sh` again whenever the machine changes. Options:

- `--dry-run` writes the file and shows it, without starting anything.
- `--cpu` leaves the GPU out; `--no-voice` leaves the microphone and speakers out.
- `--dbus` also hands him your desktop session's D-Bus, for media keys and the screen lock. It can do anything your
  session can, and Ubuntu's D-Bus refuses AppArmor-confined containers, so the container then runs without AppArmor.
- `ORION_DATA=~/orion-data ./docker/up.sh` keeps his data in a folder of yours instead of the named volume.

A plain `docker compose up -d --build`, without `up.sh`, runs the portable defaults in `compose.yaml`: the CPU, no
sound, user 1000.

## An NVIDIA GPU

Docker can only hand a GPU to a container through NVIDIA's container toolkit. Install it once on the machine
([NVIDIA's guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)), let it
configure Docker, and run `up.sh` again:

```sh
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
./docker/up.sh                    # now builds with cuBLAS (about 800 MB more) and hands him the GPU
```

The GPU also brings its readings to VITALS. With an AMD or Intel GPU, Whisper runs on the CPU (CTranslate2's GPU
builds are CUDA only). The HUD's reactor is drawn by the browser, with the graphics of whatever machine you open the
HUD on, so it doesn't care what the container has.

## On a server

On a machine without a desktop there is no sound server, so `up.sh` starts him without a microphone of his own (the
HUD carries his voice: below), and the HUD is on the server's own loopback, out of reach of the network by design
(he has full access to the machine). Open it from your laptop or PC through SSH, which keeps it private:

```sh
ssh -N -L 8765:127.0.0.1:8765 you@your-server     # leave it running, then open http://localhost:8765 here
```

The HUD then runs in your browser as if he were local; he only sees requests for `localhost`, so his checks pass. An
NVIDIA GPU on the server works as above, and so does an Ollama running there.

## His data

`/app/data` holds what `data/` holds natively. compose.yaml mounts the named volume `orion_data` there, which
starts empty: he writes `config.yaml` at the first start, and the four starter skills are copied in. Change settings in
the HUD (Settings). To edit the files yourself, keep them in a folder of yours (`ORION_DATA`, above). Your native
install's folder works too (`ORION_DATA=./data`), but only while the native Orion is stopped.

The first start downloads Kokoro's voice and the memory-search model into the data folder (about 480 MB). Whisper
downloads itself the first time the microphone opens. Set `ORION_FETCH_MODELS=0` to skip the download step. The
image itself is about 1 GB (about 1.8 GB with cuBLAS).

## Keeping him in bounds

The container is his sandbox: his commands and the integrations run inside it with an environment of their own, never
his (so `env` shows no token), and see only what you mounted. His secrets (a calendar password, a connector's token) are
kept as files in `/app/data/vault`, private to his user, since a container has no desktop keyring (`orion secret list`
shows their names). `CLAUDE_CODE_OAUTH_TOKEN` stays an environment variable because that is how Claude Code signs in:
he cuts it, and every other secret, out of whatever a model, a log or a channel receives. Whoever can read the data
volume, or run `docker exec` as his user, can read the vault: mount nothing of yours that you wouldn't give him.
[docs/SECURITY.md](../docs/SECURITY.md) has the rest.

## His brain

- **Claude** (the default): Claude Code is in the image. Sign it in with a token: on any machine with a browser,
  run `claude setup-token` (Claude Pro or Max), and put the token in a file named `.env` beside `compose.yaml`:

  ```sh
  CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
  ```

  `up.sh` says whether it found one. Without it, the HUD shows the brain as not ready (and the stand-in brain answers,
  when one is set up).
- **OpenRouter**: put `OPENROUTER_API_KEY=...` in `.env`, or the key in Settings. It is also the stand-in when the
  Claude plan's limit is reached (Settings → Brain).
- **Ollama**: set Settings → Brain → `ollama`. He reaches an Ollama on the same machine at `http://127.0.0.1:11434`,
  but the container can't start or stop it: run it yourself (`ollama serve`).

## Talking to him without a sound server

On a server, in Docker Desktop, or anywhere without PipeWire or PulseAudio, the HUD carries his voice: press the
talk button and the browser's microphone and speakers become his (his name wakes him from there too, and replies
are spoken there). Browsers allow the microphone only on `localhost` or over HTTPS: through the SSH tunnel below the
HUD is `localhost`; on your network, give him a certificate (`server.tls_cert`, `server.tls_key`). The same goes for
sharing your screen or camera with him, and for your clipboard.

## Integrations

Settings → Abilities lists what he can be connected to (Home Assistant, e-mail, Google, Microsoft, calendars,
Discord, a browser). They run inside the container through `uvx`; what they download is kept in the `uv-cache`
volume. The browser runs in a container of its own:

```sh
./docker/up.sh --browser          # or: docker compose --profile browser up -d
```

Its port is on this machine's loopback only, and it has no password: never publish it to your network.

## Docker Desktop (macOS, Windows)

The container uses host networking, so turn it on once: Docker Desktop → Settings → Resources → Network → Enable
host networking. Then `docker compose up -d --build` (without `up.sh`, which is for Linux) and open
<http://localhost:8765>. He has no desktop there to open apps in or control the volume of: those tools aren't offered,
and the HUD is his microphone, speakers, screen and clipboard.

## What a container changes

His tools that work on the machine itself act inside the container:

- `run_command` runs in the container's shell.
- The file tools see only the container and what you mount.
- Opening apps, files and links (xdg-open), volume control (wpctl or pactl), power actions (systemd) and desktop
  notifications aren't there, so those tools aren't offered (Settings → Abilities says why).
- He sees only the container's processes.

Everything else works the same: chat, memory, skills, timers, reminders, web search, the weather and the HUD.
RESTART in the HUD restarts him inside the container. SHUT DOWN stops the container, and it stays stopped
(`restart: on-failure`). `/mcp` serves other AI apps on the same machine as usual, and
`docker exec -i orion orion mcp-serve` lends his tools over stdio.

`orion start`, `stop`, `status` and `setup` drive systemd, so use `docker compose` instead.
`docker compose exec orion orion check` diagnoses the container.
