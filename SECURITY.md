# Security policy

Orion has full access to the PC he runs on, so security reports are taken seriously, and the design is written down:
[docs/SECURITY.md](docs/SECURITY.md) lists each guarantee (G1 to G9), where the code keeps it and which test proves it.

## Reporting a vulnerability

Please report it privately: on this repository's **Security** tab choose **Report a vulnerability** (GitHub shows the
report to the maintainers alone). Don't open a public issue or pull request for it, and don't post it on Discord or
elsewhere until it is fixed. Say what you found, the version (`git rev-parse --short HEAD`, or the image tag), how you
run him (natively or in Docker) and how to reproduce it; a failing test is the best report there is. You'll get an answer
as soon as a maintainer can, and credit in the fix unless you'd rather not have it.

Only `main` and the latest release are supported.

## What counts

Anything that breaks a guarantee of [docs/SECURITY.md](docs/SECURITY.md) is a vulnerability, for example:

- a secret (a vault value, a token the redactor knows) reaching a model, the HUD, Discord, a log, memory or the activity
  log, or leaving in a tool call;
- a command escaping the sandbox, or reading what the sandbox hides (your keys, sign-ins, Orion's own folders);
- a model, or text a model read (a web page, an e-mail, a document, another app's answer), changing a limit, the
  sandbox, the vault, a connector or a plugin without your yes, or doing what a channel may never do;
- a web page, another device or another user reaching the HUD or its API (Host, Origin, pairing, the LAN check);
- a plugin, connector or document converter executing outside its box.

## What doesn't

The [residual risks](docs/SECURITY.md#residual-risks) are known and written down: malware already running as you, a
connector misusing the token you gave it, a secret in a screenshot (the redactor reads text), anything you say yes to,
and a model that says something wrong without acting. A report that one of them exists isn't a vulnerability, though a
way to make them worse is.

## Keeping your own copy safe

`data/` holds your memory, settings and conversation history, and the vault's files if the system has no keyring. It is
git-ignored; keep it that way, and don't put it in an issue, a backup you share, or a screenshot.
