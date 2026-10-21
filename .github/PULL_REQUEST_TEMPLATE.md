## What this changes, and why

## How I tried it

<!-- Natively? Docker? Which brain? Only the tests? -->

## Checklist

- [ ] `uv run pytest` and `uvx ruff check orion tests` pass
- [ ] I added or updated a test that fails without this change
- [ ] If I changed `web/`, I rebuilt the HUD and committed `orion/hud/dist`; lint, typecheck and tests pass
- [ ] If I changed a table of the database, I raised `CURRENT_VERSION` and added an upgrade (`orion/memory/store.py`)
- [ ] If I touched the vault, redactor, sandbox, channel limits or confirmation: no guarantee of `docs/SECURITY.md` is weaker, and its tests still cover it
- [ ] I updated the README or `docs/SECURITY.md`, and `CHANGELOG.md` (under "Unreleased"), if a user would notice
- [ ] No key, token, address or other private data of mine is in the change
