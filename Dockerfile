# syntax=docker/dockerfile:1
# ORION in a container: the Python backend exactly as uv.lock pins it, and the HUD's committed build (no Node).
#
#   docker compose up -d --build                      build and start (compose.yaml; docker/README.md explains it)
#   docker build -t orion .                      the image alone
#   docker build -t orion --build-arg GPU=1 .    with CUDA's cuBLAS, for Whisper on an NVIDIA GPU (~800 MB more;
#                                                     the host then needs nvidia-container-toolkit)
#   --build-arg UID=… --build-arg GID=…               if your `id -u` / `id -g` aren't 1000
#   --build-arg CLAUDE=0                         without Claude Code (for a brain on OpenRouter or Ollama only)
#
# Your settings, memory, skills, models and logs live in /app/data, a volume: nothing personal is in the image.
# Claude Code signs in with a token (`claude setup-token` on any machine with a browser; CLAUDE_CODE_OAUTH_TOKEN).

ARG PYTHON_IMAGE=python:3.12-slim-trixie

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

# ---------------------------------------------------------------------------------------------------- the venv
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ARG GPU=0
# The image's own Python, never a downloaded one; packages copied (not linked) out of the cache, compiled once here.
ENV UV_PYTHON=python3.12 UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# The dependencies first, in their own layer: changing Orion's code doesn't download them again. cuBLAS (580 MB)
# stays out unless GPU=1: without nvidia-container-toolkit no container can reach the GPU, and Whisper runs on the CPU.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project \
        $([ "$GPU" = 1 ] || echo --no-install-package nvidia-cublas-cu12)
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY orion ./orion
# Orion itself, editable as uv installs it natively: his code stays in /app/orion, so his data folder
# (data_dir, relative to that folder's parent) is /app/data. Compiled now, since he can't write /app when he runs.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev $([ "$GPU" = 1 ] || echo --no-install-package nvidia-cublas-cu12) \
 && python -m compileall -q --invalidation-mode unchecked-hash orion

# ---------------------------------------------------------------------------------------------------- sound
# ALSA's PulseAudio plugin alone, out of its package: the package brings 136 MB of ffmpeg codecs that PortAudio never
# uses. With it, sounddevice (PortAudio over ALSA) reaches your PipeWire or PulseAudio through its mounted socket.
FROM ${PYTHON_IMAGE} AS sound
WORKDIR /tmp
RUN apt-get update && apt-get download libasound2-plugins && dpkg-deb -x libasound2-plugins_*.deb pkg && cd pkg \
 && mkdir -p /out/etc/alsa/conf.d && cp usr/share/alsa/alsa.conf.d/50-pulseaudio.conf /out/etc/alsa/conf.d/ \
 && cp --parents usr/lib/*/alsa-lib/libasound_module_pcm_pulse.so usr/lib/*/alsa-lib/libasound_module_ctl_pulse.so /out/

# ---------------------------------------------------------------------------------------------------- the image
FROM ${PYTHON_IMAGE}
ARG UID=1000
ARG GID=1000
ARG CLAUDE=1
ARG CLAUDE_VERSION=2.1.286
# PortAudio for sounddevice and libpulse for the plugin above: every other native library comes inside the wheels.
# curl fetches Claude Code's installer. /etc/localtime becomes a file, not a link to the UTC zone: compose.yaml mounts
# your /etc/localtime over it, and over a link the mount would land on Etc/UTC itself (UTC would then mean your zone).
RUN apt-get update && apt-get install -y --no-install-recommends libportaudio2 libpulse0 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && rm /etc/localtime && cp /usr/share/zoneinfo/Etc/UTC /etc/localtime \
 && groupadd --gid "$GID" orion && useradd --uid "$UID" --gid "$GID" --create-home --shell /bin/bash orion
# uv and uvx: the integrations he sets up run through them (Python servers, and Node ones with Node from PyPI).
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=sound /out/ /
COPY docker/asound.conf /etc/asound.conf
COPY --from=build /app /app
COPY data/skills /app/docker/starter-skills
COPY docker/entrypoint.sh docker/prepare.py /app/docker/
# The data folder belongs to him, so a new named volume mounted here does too; so does uv's cache.
RUN install -d -o "$UID" -g "$GID" /app/data /home/orion/.cache /home/orion/.cache/uv
# Like his systemd unit: logs straight out, and idle OpenMP threads sleep instead of spinning on a core.
ENV PATH=/home/orion/.local/bin:/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 OMP_WAIT_POLICY=PASSIVE HOME=/home/orion
USER orion
# Claude Code (pinned to what the Agent SDK expects), for brain.provider claude; the installer must not run from /.
RUN if [ "$CLAUDE" = 1 ]; then cd /tmp && curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_VERSION" \
    && claude --version; fi
# Installed: from here on it neither updates itself nor sends telemetry (the image pins its version).
ENV DISABLE_UPDATES=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /app
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["run", "--no-voice"]
