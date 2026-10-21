# Contributing to Orion

Thanks for helping. Orion is a personal voice assistant with full access to the PC it runs on, so most of the care in
this repository goes into what he is *not* allowed to do. Bug reports, fixes, new tools and connectors, translations of
the HUD's words, and better tests are all welcome. Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md). For a security problem, don't open an issue: see [SECURITY.md](SECURITY.md).

## Before you start

For anything bigger than a fix, open an issue first so we can agree on the shape of it. Changes that touch the safety
design (the vault, the redactor, the sandbox, channel limits, confirmation) need that conversation most: read
[docs/SECURITY.md](docs/SECURITY.md) first, it says which guarantee each piece keeps and which test proves it.

## Setting up

Linux is the supported desktop (built on Ubuntu with GNOME, PipeWire and systemd); Docker runs him anywhere
([docker/README.md](docker/README.md)). You need [uv](https://docs.astral.sh/uv/), which also fetches the right Python
(3.12). No microphone, GPU or model download is needed for the tests.

```bash
git clone https://github.com/ToniLabInc/Orion.git && cd Orion
uv sync                                   # the Python packages, exactly as uv.lock pins them
uv run pytest                             # the Python tests (about 1,240); some skip without bubblewrap or restic
uvx ruff check orion tests                # lint
```

The HUD (`web/`, React and Vite) gets Node from PyPI through uv, so nothing else is installed:

```bash
uv run --only-group web npm --prefix web ci          # the packages package-lock.json says
uv run --only-group web npm --prefix web run lint    # Biome
uv run --only-group web npm --prefix web run typecheck
uv run --only-group web npm --prefix web test        # Vitest
uv run --only-group web npm --prefix web run build   # builds into orion/hud/dist
```

`uv run --only-group web` leaves the environment holding only Node: run `uv sync` again before the Python tests. Set
`UV_PROJECT_ENVIRONMENT` to another folder for the web commands to keep both.

**The HUD's build is committed** (`orion/hud/dist`), so installing Orion needs no Node. If you change anything under
`web/src`, run the build and commit the result with your change; CI rebuilds it and fails if it differs. To look at the
HUD without a model or your own data, `web/scripts/mock_orion.py` serves it with made-up data
([web/README.md](web/README.md) says how).

## How the code is laid out

The README's [Development](README.md#development) table maps every module. Two things worth knowing first:

- Every tool goes through one hub (`orion/mcp.py`): limits, confirmation, scrubbing of secrets and the activity log
  happen there, not in each tool. A new tool is a function under `@group.tool(...)` in `orion/tools/*.py`; its
  docstring and type hints become its MCP description and schema.
- The settings are one typed schema (`orion/config.py`) with factory values in `orion/default_config.yaml`; the HUD
  draws its Settings pages from that schema, so a new option is declared there and appears in the HUD by itself.

## Conventions

- **Python 3.12**, type hints everywhere (PEP 695 `type` statements are used), lines up to 120 columns, Google-style
  docstrings (`Args`, `Returns`, `Raises`), comments that say *why*. `uvx ruff check` is the linter.
- **Tests** sit in `tests/test_<module>.py`. A fix comes with a test that fails without it. Tests never touch your real
  `data/`, keyring or network: use `tmp_path`, fakes (`tests/conftest.py`, `tests/fake_oauth.py`) and made-up values.
- **The HUD** takes colours from `web/src/palette.css` only, sizes from its type scale, and is formatted by Biome. Add an
  npm package with an exact version (`npm --prefix web i -E name@x.y.z`), and only MIT, Apache 2.0, BSD or ISC ones; the
  built HUD runs under a strict CSP (no CDN, no inline script).
- **The database** (`data/orion.db`, `orion/memory/store.py`) is versioned with `PRAGMA user_version`.
  `CREATE TABLE IF NOT EXISTS` never changes a table that exists, so changing an existing table (a column, a `CHECK`)
  needs a new `CURRENT_VERSION`, an entry in `UPGRADES` that rebuilds or alters it, and a test that opens a database
  made the old way (see `tests/test_store.py`). Other people's databases are old ones.
- **Dependencies** are pinned in `uv.lock` and `web/package-lock.json`: commit the lock files with the change that needs
  them, and mention any licence that isn't MIT, Apache 2.0 or BSD. After changing the HUD's npm packages, regenerate
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md): `python3 web/scripts/third_party_notices.py > THIRD_PARTY_NOTICES.md`.

## The safety code

- A secret must never reach a model, the HUD, Discord, a log, memory or the activity log, and must never be
  sent out in a tool call. If your change adds a new place text can leave Orion, it goes through the redactor.
- Nothing a model can call may loosen a limit, the sandbox, the vault or an install. Settings that govern safety are the
  user's alone (G7 in docs/SECURITY.md); a new tool that edits settings must not reach them.
- What the user says "no" to, or a channel may never do, is absolute: no `yes`, no model and no setting a model can reach
  lifts it.
- Use only made-up keys and tokens in tests and fixtures, and keep real ones out of the repository, its history, and
  the issues and pull requests you open.

## Pull requests

- One change per pull request, with what it does, why, and how you tried it (natively? Docker? which brain?).
- `uv run pytest`, `uvx ruff check orion tests` and, for the HUD, lint, typecheck, test and the committed build must pass;
  CI runs them all.
- Update the README or [docs/SECURITY.md](docs/SECURITY.md) when behaviour a user, or the safety design, changes, and add
  a line to [CHANGELOG.md](CHANGELOG.md) under "Unreleased".
- Commit messages say what changed for someone reading the history, in plain words.

## Licence of your contribution

Orion is MIT ([LICENSE](LICENSE)). By opening a pull request you agree that your contribution is released under the same
licence.
