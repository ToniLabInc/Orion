# Changelog

All notable changes to Orion. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Before the
first public release the history is in git (`git log`).

## Unreleased

### Fixed
- Stopping a benchmark failed on a database made before benchmarks could be "stopped": its table only accepted
  "running", "done" and "failed". The schema is now version 6, and opening a version-5 database rebuilds that table,
  keeping its runs (a run that was stopped under the old scheme is now "stopped" too).
- The README's example of a connector's secret named one Orion refuses: a server's secrets are
  `connector.<server>.<field>`.
- The HUD's stand-in Orion (`web/scripts/mock_orion.py`) recorded a stopped benchmark as "failed", so the HUD showed it
  as failed.

### Security
- Pillow 13.0.0 (the 12.3 series has known vulnerabilities in image decoding) and certifi 2026.10.19; `pillow>=13.0` is
  now the lowest version Orion accepts.

### Added
- Contributing guide, code of conduct, security policy, issue and pull request templates, Dependabot, and CI that runs
  the Python and HUD tests and linters, checks the committed HUD build and the shell scripts, and builds the Docker image.

### Changed
- The README, the HUD's stand-in data and the tests use made-up people and places.
