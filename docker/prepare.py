"""Before `orion run` in the container (docker/entrypoint.sh): what ./install.sh and `orion setup` do natively,
without the systemd units and app-menu entry they also write, which mean nothing in a container.

- The starter skills go into a data folder that has no skills folder yet (one you empty later stays empty).
- His secrets are kept in files of his data folder (`secrets.store: files`): a container has no desktop keyring. The
  folder is private to his user; nobody who can read the data volume is stopped by it, which is the one place those
  secrets are as safe as the volume is.
- The models he needs but doesn't fetch by himself are downloaded once: Kokoro's voice and the memory-search embedder
  (about 480 MB). Whisper downloads itself the first time the microphone listens. A failed download is reported and he
  starts anyway, showing what is missing; ORION_FETCH_MODELS=0 skips this step.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

from orion.config import Settings
from orion.inference import ensure, registry

STARTER_SKILLS = Path(__file__).with_name("starter-skills")


def say(text: str) -> None:
    """One line in the container's log."""
    print(f"[docker] {text}", file=sys.stderr, flush=True)


def progress(name: str, done: int, total: int) -> None:
    """A line when a download starts and one when it ends (no progress bars in a log)."""
    if done == 0:
        say(f"  downloading {name}" + (f" ({total / 1e6:.0f} MB)" if total else ""))
    elif done == total:
        say(f"  done: {name}")


def use_the_files_vault(settings: Settings) -> None:
    """Keep his secrets as files in the data folder: a container has no keyring to keep them in."""
    if settings.cfg.secrets.store == "files":
        return
    if not settings.writable:
        say("config.yaml can't be written, so his secrets can't be kept: set secrets.store to files in it")
        return
    asyncio.run(settings.commit(settings.validate({"secrets": {"store": "files"}}, actor="hud", own=True)))
    say("secrets.store is files: a container has no keyring")


def main() -> None:
    """Keep his secrets as files, copy the starter skills and download the missing models."""
    settings = Settings.open()  # he writes the rest of config.yaml himself when he starts
    use_the_files_vault(settings)
    cfg = settings.cfg
    if not cfg.skills_path.exists() and STARTER_SKILLS.is_dir():
        shutil.copytree(STARTER_SKILLS, cfg.skills_path)
        say(f"starter skills copied to {cfg.skills_path}")
    if os.environ.get("ORION_FETCH_MODELS", "1") == "0":
        return
    files = [item for group in registry(cfg).values() for item in group if not item.present]
    if not files:
        return
    say(f"first start: downloading his models into {cfg.models_dir}")
    try:
        ensure(files, progress)
    except Exception as e:  # noqa: BLE001 (no internet, say: he starts anyway and the HUD shows what is missing)
        say(f"a download failed ({e}); starting without it")


if __name__ == "__main__":
    main()
