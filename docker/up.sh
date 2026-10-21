#!/usr/bin/env bash
# Starts ORION in Docker on whatever machine this is. It looks at the host (your user, an NVIDIA GPU that Docker
# can hand over, a sound server, the time zone), writes compose.override.yaml to match (Docker Compose merges that file
# into compose.yaml by itself, so `docker compose logs`, `down` and the rest keep working), then builds and starts him.
# Run it again whenever the machine changes: a GPU toolkit installed, a sound server gone, another user.
#
#   ./docker/up.sh               look, write compose.override.yaml, build and start
#   ./docker/up.sh --dry-run     only write compose.override.yaml, and show it
#   ./docker/up.sh --cpu         leave the GPU out: Whisper runs on the CPU
#   ./docker/up.sh --no-voice    no microphone or speakers, even with a sound server here
#   ./docker/up.sh --browser     also start the headless browser he drives (Settings, Abilities, Browser control)
#   ./docker/up.sh --dbus        also hand him your desktop session's D-Bus, for media keys and the screen lock (it can
#                                do anything your session can, and the container then runs without AppArmor)
#   ORION_DATA=~/orion-data ./docker/up.sh   his data in a folder of yours instead of the named volume
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY=no
CPU=no
VOICE=yes
DBUS=no
BROWSER=no
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=yes ;;
    --cpu) CPU=yes ;;
    --no-voice) VOICE=no ;;
    --dbus) DBUS=yes ;;
    --browser) BROWSER=yes ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (see ./docker/up.sh --help)" >&2; exit 2 ;;
  esac
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker isn't running here, or this user may not use it (add yourself to the docker group)." >&2
  exit 1
fi

say() { printf '  %-7s %s\n' "$1" "$2"; }
echo "ORION in Docker, on $(hostname):"

uid="$(id -u)"
gid="$(id -g)"
run_dir="${XDG_RUNTIME_DIR:-/run/user/$uid}"
say user "$uid:$gid"

# An NVIDIA GPU, and whether Docker can hand it to a container: nvidia-container-toolkit's runtime, or its CDI spec.
gpu=none
if [ "$CPU" = yes ]; then
  say GPU "left out (--cpu): Whisper runs on the CPU"
elif command -v nvidia-smi >/dev/null 2>&1 && name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1)" && [ -n "$name" ]; then
  if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    gpu=runtime
  elif compgen -G "/etc/cdi/nvidia*" >/dev/null || compgen -G "/var/run/cdi/nvidia*" >/dev/null; then
    gpu=cdi
  fi
  if [ "$gpu" = none ]; then
    say GPU "$name, but Docker can't reach it: Whisper runs on the CPU until nvidia-container-toolkit is installed"
    say "" "(docker/README.md, \"An NVIDIA GPU\"), then run this again"
  else
    say GPU "$name: Whisper runs on it"
  fi
else
  say GPU "no NVIDIA GPU: Whisper runs on the CPU"
fi

sound=no
if [ "$VOICE" = no ]; then
  say voice "off (--no-voice): text only, in the HUD"
elif [ -S "$run_dir/pulse/native" ]; then
  sound=yes
  say voice "the sound server at $run_dir/pulse/native: microphone and speakers"
else
  say voice "no sound server for this user (PipeWire or PulseAudio): text only, in the HUD"
fi

bus=no
if [ "$DBUS" = yes ] && [ -S "$run_dir/bus" ]; then
  bus=yes
  say D-Bus "your session's bus: media keys and the screen lock"
elif [ "$DBUS" = yes ]; then
  say D-Bus "no session bus at $run_dir/bus: media keys and the screen lock stay off"
else
  say D-Bus "off (--dbus hands him your session's bus, for media keys and the screen lock)"
fi

data=""
if [ -n "${ORION_DATA:-}" ]; then
  mkdir -p "$ORION_DATA" # made by you, so it is yours rather than root's
  data="$(cd "$ORION_DATA" && pwd)"
  say data "$data"
else
  say data "the volume orion_data"
fi

tz=no
[ -e /etc/localtime ] && tz=yes

# His brain: Claude Code signs in with a token from .env (CLAUDE_CODE_OAUTH_TOKEN), made by `claude setup-token`.
if [ -f .env ] && grep -q '^CLAUDE_CODE_OAUTH_TOKEN=.' .env; then
  say brain "your Claude plan, signed in with the token in .env"
else
  say brain "no CLAUDE_CODE_OAUTH_TOKEN in .env: run \`claude setup-token\` and put it there (or use OpenRouter)"
fi
[ "$BROWSER" = yes ] && say browser "the headless browser he drives starts too (http://127.0.0.1:8931)"

# What this machine has, as an override of compose.yaml.
{
  echo "# Written by docker/up.sh for $(hostname) on $(date +%F): what this machine has. Run ./docker/up.sh again after"
  echo "# it changes, rather than editing this file; anything else belongs in compose.yaml."
  echo "services:"
  echo "  orion:"
  echo "    build:"
  echo "      args: {UID: \"$uid\", GID: \"$gid\", GPU: \"$([ "$gpu" = none ] && echo 0 || echo 1)\"}"
  echo "    user: \"$uid:$gid\""
  if [ "$sound" = yes ]; then echo '    command: ["run"]'; else echo '    command: ["run", "--no-voice"]'; fi
  if [ "$sound" = yes ] || [ "$bus" = yes ]; then
    echo "    environment:"
    [ "$sound" = yes ] && echo "      PULSE_SERVER: unix:/run/host/pulse-native"
    [ "$bus" = yes ] && echo "      DBUS_SESSION_BUS_ADDRESS: unix:path=/run/host/bus"
  fi
  if [ -n "$data" ] || [ "$tz" = yes ] || [ "$sound" = yes ] || [ "$bus" = yes ]; then
    echo "    volumes:"
    [ -n "$data" ] && echo "      - {type: bind, source: \"$data\", target: /app/data}"
    [ "$tz" = yes ] && echo "      - {type: bind, source: /etc/localtime, target: /etc/localtime, read_only: true}"
    [ "$sound" = yes ] && echo "      - {type: bind, source: \"$run_dir/pulse/native\", target: /run/host/pulse-native}"
    [ "$bus" = yes ] && echo "      - {type: bind, source: \"$run_dir/bus\", target: /run/host/bus}"
  fi
  if [ "$bus" = yes ]; then
    echo "    security_opt: [\"apparmor=unconfined\"] # D-Bus refuses AppArmor-confined containers"
  fi
  if [ "$gpu" = runtime ]; then
    echo "    deploy:"
    echo "      resources:"
    echo "        reservations:"
    echo "          devices: [{driver: nvidia, count: all, capabilities: [gpu]}]"
  elif [ "$gpu" = cdi ]; then
    echo "    devices: [\"nvidia.com/gpu=all\"]"
  fi
} > compose.override.yaml

docker compose config --quiet # the two files must make a valid project
if [ "$DRY" = yes ]; then
  echo
  cat compose.override.yaml
  exit 0
fi

echo
if [ "$BROWSER" = yes ]; then
  docker compose --profile browser up -d --build
else
  docker compose up -d --build
fi
echo
echo "Started. Follow him with: docker compose logs -f   (the first start downloads his models, about 480 MB)"
echo "The HUD: http://localhost:8765 (or the port in his settings). On a server, see docker/README.md."
