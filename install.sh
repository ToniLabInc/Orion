#!/usr/bin/env bash
# Installs, updates and uninstalls Orion for the current user — no sudo needed.
#   ./install.sh               install, or update after a `git pull`; then `orion start` / `orion stop`
#   ./install.sh --local-brain also install Ollama, so he can think on this PC (about 2 GB more)
#   ./install.sh --autostart   also launch him automatically on login
#   ./install.sh --uninstall   remove his service, app-menu entry and command; his folder (and data/) stays
# His systemd service, Ollama's (for the local brain) and the app-menu entry are written by `orion setup` below, from
# the templates in orion/service.py; `orion start` keeps them current after an update.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
UNITS="$HOME/.config/systemd/user"
OLLAMA_VERSION="${OLLAMA_VERSION:-latest}"
LOCAL_BRAIN=no
AUTOSTART=no
UNINSTALL=no
for arg in "$@"; do
  case "$arg" in
    --local-brain) LOCAL_BRAIN=yes ;;
    --autostart) AUTOSTART=yes ;;
    --uninstall) UNINSTALL=yes ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (see ./install.sh --help)" >&2; exit 2 ;;
  esac
done
say() { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }

# Everything install.sh and `orion setup` put outside this folder, and only that: Ollama's unit only when it is the one
# Orion wrote, the command only when it points here. His memory, settings and models stay in data/.
uninstall() {
  say "Stopping Orion"
  "$ROOT/.venv/bin/orion" stop 2>/dev/null || systemctl --user stop orion.service 2>/dev/null || true
  systemctl --user disable orion.service 2>/dev/null || true
  say "Removing his service, app-menu entry and command"
  rm -f "$UNITS/orion.service"
  if grep -qs "while Orion runs" "$UNITS/ollama.service"; then
    systemctl --user stop ollama.service 2>/dev/null || true
    rm -f "$UNITS/ollama.service"
  fi
  systemctl --user daemon-reload 2>/dev/null || true
  rm -f "$HOME/.local/share/applications/orion-hud.desktop"
  if [ "$(readlink "$BIN/orion" 2>/dev/null)" = "$ROOT/.venv/bin/orion" ]; then rm -f "$BIN/orion"; fi
  say "Done."
  cat <<MSG
  His folder is still here, with his memory, settings and models in data/:
    $ROOT
  Delete it to remove him completely. If Ollama was installed only for him:
    rm -rf ~/.local/bin/ollama ~/.local/lib/ollama ~/.ollama
MSG
}
if [ "$UNINSTALL" = yes ]; then uninstall; exit 0; fi

mkdir -p "$BIN"
export PATH="$BIN:$PATH"

say "Python toolchain (uv)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
uv --version

# Only the local brain needs Ollama: your Claude plan and OpenRouter think elsewhere, and memory search runs inside Orion.
if [ "$LOCAL_BRAIN" = yes ] && ! command -v ollama >/dev/null; then
  say "Ollama (runs a language model on this PC)"
  if [ "$OLLAMA_VERSION" = latest ]; then
    url="https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst"
  else
    url="https://github.com/ollama/ollama/releases/download/${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst"
  fi
  curl -fL --progress-bar "$url" | tar --zstd -x -C "$HOME/.local"
  ollama --version 2>/dev/null | tail -1 || true
fi
say "Python dependencies"
cd "$ROOT"
unset VIRTUAL_ENV
# What he runs on, exactly as locked; the tests' packages stay out (uv sync --group dev). Keep openWakeWord if a wake-word
# model of yours is set, by name or path (a plain sync would uninstall the optional extra on every update): his own
# settings code says, or, before he is installed, any value given to wake_model in the file.
wake_model() {
  "$ROOT/.venv/bin/python" -c 'from orion.config import load_config; print(load_config().voice.wake_model)' 2>/dev/null ||
    grep -E "^[[:space:]]*wake_model:[[:space:]]*([^[:space:]#\"']|\"[^\"]|'[^'])" "$ROOT/data/config.yaml" 2>/dev/null
}
extras=()
if [ -n "$(wake_model)" ]; then extras=(--extra wake); fi
uv sync --locked --no-dev "${extras[@]}"

say "Settings (data/config.yaml), models (voices, Whisper, memory search; a local LLM if he thinks here), his service"
"$ROOT/.venv/bin/orion" setup

say "The orion command"
ln -sf "$ROOT/.venv/bin/orion" "$BIN/orion"
# his names before (HEISENBERG, ULTRON): those commands pointed here (their services and launchers went with `orion setup`)
for old in heisenberg ultron; do
  if [ "$(readlink "$BIN/$old" 2>/dev/null)" = "$ROOT/.venv/bin/$old" ]; then rm -f "$BIN/$old"; fi
done

if [ "$AUTOSTART" = yes ]; then
  say "Launching Orion on login"
  systemctl --user enable orion.service
fi

say "Done."
cat <<'MSG'
  orion start --open   start him and open the HUD (or click ORION in the app menu)
  orion stop           stop him (or right-click the ORION icon → Stop Orion)
  orion status         is he running?

  He thinks with your Claude plan by default (nothing to load, so he answers at once): install Claude Code
  (curl -fsSL https://claude.ai/install.sh | bash), run `claude` once and sign in. No plan? Run
  ./install.sh --local-brain, then pick THIS PC on Settings → Brain: a model that runs on your own GPU.
MSG
