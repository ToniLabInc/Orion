# Security

Orion has full access to your PC, so the question is not whether a model can be trusted but what it can reach. A local
model, a cloud model and a model steered by a web page are all treated the same way: nothing here depends on it
behaving. The limits are in code that no model can change, secrets are kept where no model can read them, and what runs
is boxed. [The picture](architecture/security.svg) shows it; this page says exactly where each promise is kept, and which
test proves it.

The short answer to "if I put in a Discord token or a Microsoft account, can the model steal it?": the token is in the
vault, not in `config.yaml`. Only the connector that needs it receives it, as an environment variable of its own process.
A model never reads it (every text it receives has secrets cut out), can't send it (a call carrying one is blocked), and
can't run a command that finds it (commands run in a sandbox where your keys, sign-ins and Orion's own folders don't
exist, with an environment that holds none).

## What he defends against

Protected: your secrets (the vault, the tokens he runs with), your files and accounts, and what leaves the PC.

| Threat | What stands in the way |
|---|---|
| A model that misbehaves, or one steered by text it read (a web page, an e-mail, another app's answer) | the hub's limits, the redactor, the sandbox, your yes after untrusted content |
| A connector or plugin you add | an environment of its own, only its own secrets, the sandbox when nobody reviewed it |
| Someone on Discord, or an app that reaches the HUD | per-place limits that no yes lifts; pairing for other devices |
| A web page in your browser | the HUD answers only its own pages on this PC (Host, Origin, no framing) |

Not defended against: malware already running as you, a malicious kernel, someone at the PC, a connector you gave a
token misusing that token, and you saying yes. [Residual risks](#residual-risks) lists what is left and why.

## The guarantees

| | Guarantee |
|---|---|
| G1 | A secret never reaches a model, the HUD, Discord, a log, memory or the activity log. |
| G2 | A secret can't be sent out: a tool call whose arguments carry one is blocked. |
| G3 | Commands run in a sandbox: the PC read-only, secrets and sign-ins hidden, a clean environment, no desktop. |
| G4 | A connector gets only its own secrets and an environment of its own; one nobody reviewed runs in the sandbox. |
| G5 | What a place may never do is absolute: no yes, no model and no setting the model can reach lifts it. |
| G6 | After untrusted content, anything risky waits for your yes. |
| G7 | A model can't weaken any of this: not the limits, the sandbox, the vault or the installs. |
| G8 | Everything he does, and who allowed it, is recorded, with no secret in the record. |
| G9 | The HUD answers only you: this PC's own pages, or a device you paired. |

### G1: secrets never reach a model

Where it is enforced:
- `orion/vaulting.py: secure`, `scrub_backup`: `config.yaml` and its `.bak` hold `${secret:NAME}`, never a value.
- `orion/redact.py: Redactor.redact`, `Stream`: one automaton for every vault value, its encodings (base64, URL, hex,
  JSON) and the shapes of 30 kinds of token (`safety.secret_patterns`); a reply is let out a word at a time so a secret is
  seen whole before any of it shows.
- `orion/mcp.py: MCPHub.call_rich`, `describe_error`, `preview_args` (every tool result, error and trace preview) and
  `orion/tools/__init__.py: Calls.intercept_tool_call` (the tools process, and tools lent to another app).
- `orion/brain/__init__.py: TurnRunner.handle` (what you say, before it is logged, shown or read by the model),
  `without_secrets` (what the model says), `Brain.run_task` (a job's request), `TurnRunner.announce`, `_failed`.
- `orion/server.py: encode` (every HUD event), `orion/discord_bridge.py: Bridge.reply`, `send`, `confirm`,
  `orion/redact.py: guard_logs` (log lines and tracebacks), `orion/activity.py: Activity.record`,
  `orion/memory/writer.py: scan` (memory refuses a secret), `orion/memory/review.py: _review_window`, `orion/bench.py`
  (replayed results, through `DryHub`).

Proved by: `tests/test_redact.py` (values, encodings, token shapes, look-alikes, the streaming property test, logs,
speed), `test_vaulting.py::test_a_config_written_with_secrets_is_left_holding_none_and_so_is_its_backup`,
`test_guard.py::test_what_comes_back_carries_no_secret_and_neither_does_the_trace`,
`test_guard.py::test_error_texts_and_argument_previews_are_redacted_before_they_are_cut`,
`test_security_api.py::test_every_event_a_hud_receives_is_redacted_field_by_field`,
`test_brain.py::test_a_secret_the_model_says_is_not_shown_spoken_logged_nor_handed_back_to_it`,
`test_brain.py::test_a_secret_the_user_says_goes_no_further_than_the_turns_start`,
`test_brain.py::test_an_error_that_names_a_secret_is_apologised_for_without_it`,
`test_discord.py::test_a_direct_message_carries_no_secret_even_one_split_across_the_cut`,
`test_writer.py::test_a_secret_is_never_kept_in_memory_whoever_says_it`,
`test_review.py::test_a_review_never_shows_a_secret_to_the_model_nor_writes_one_to_the_journal`,
`test_tools_process.py::test_what_a_tool_answers_is_redacted_before_it_leaves_the_process`,
`test_activity.py::test_entries_are_redacted_published_and_listed_newest_first`.

### G2: secrets can't be sent out

Enforced in `orion/mcp.py: MCPHub._guarded`: every argument of every call, at any depth, in any of the redactor's forms.
A blocked call does nothing, tells the model not to try another way, and is recorded. Your own HUD may send a token
(you typed it); a word that only looks like a secret is not one. The Discord bridge holds a second lock of its own.

Proved by: `test_guard.py::test_a_call_carrying_a_secret_is_blocked_in_any_form_and_recorded`,
`test_guard.py::test_the_user_may_send_a_token_from_the_hud_and_a_secret_shaped_word_is_not_a_secret`,
`test_redact.py::test_a_tool_calls_arguments_are_searched_to_any_depth`.

### G3: commands run in a sandbox

`orion/sandbox.py: wrap`, `run`, `jail_for` build and run a bubblewrap jail: the PC read-only but for the folders in
`safety.sandbox_writable`, Orion's own code read-only even there, `safety.sandbox_hidden` (keys, keyrings, browser
profiles, cloud and Claude logins...) and Orion's data folder, vault and settings not there at all, `/tmp` and `/run`
empty (no X, Wayland, D-Bus or Docker socket), processes, users and IPC of its own, no new privileges, a memory limit, and
the environment of G4. With the network on, `orion/landlock.py` shuts the doors to Orion's own services first (the HUD's
port, Ollama, loopback servers) and to the desktop's abstract sockets; with it off the jail shares nothing. A machine that
can't make a sandbox refuses to run a command instead of running it bare. `orion/tools/system.py: run_command` is the user
of it, and `_kept_out` makes the file tools keep out of what the sandbox hides and away from his code and the files that
start programs. Inside a container, the container is the box.

Proved by (the ones that really run a jail are skipped where bubblewrap can't): `test_sandbox.py::test_a_command_can_not_read_what_is_hidden_and_does_not_see_orions_environment`,
`test_sandbox.py::test_a_command_writes_only_where_it_may`,
`test_sandbox.py::test_his_own_code_stays_read_only_even_inside_a_folder_that_may_be_written`,
`test_sandbox.py::test_orions_own_folder_and_vault_are_hidden_but_his_skills_are_readable`,
`test_sandbox.py::test_with_the_network_on_orions_services_and_the_desktops_sockets_stay_out_of_reach`,
`test_sandbox.py::test_no_network_means_no_network_and_nothing_shared_with_the_pc`,
`test_sandbox.py::test_a_command_gets_no_privileges_no_sessions_and_a_limit_on_memory`,
`test_system.py::test_a_command_runs_in_the_sandbox_with_no_token_and_no_key`,
`test_system.py::test_the_file_tools_do_not_read_what_the_sandbox_hides`,
`test_system.py::test_the_file_tools_do_not_change_his_code_his_skills_or_what_starts_programs`,
`test_system.py::test_a_machine_that_cannot_make_a_sandbox_says_so_instead_of_running_without_one`,
`test_system.py::test_another_app_cannot_ask_for_a_command_outside_the_sandbox`.

### G4: a connector gets only its own

A command or server he starts inherits only the variables in `safety.env_allow` (`orion/secrets.py: inherited`), plus
`env_desktop` for one he trusts; never a variable that looks secret, so `env` shows no token, in Docker too
(`CLAUDE_CODE_OAUTH_TOKEN` stays in his own environment). A connector's settings may refer only to its own secrets,
`connector.<id>.*`, filled in as it connects and never kept (`orion/connectors.py: Connectors._prepare`,
`orion/mcp.py: MCPHub.prepared`). A plugin's or your own program that nobody at Orion reviewed runs in the sandbox with its
own folder as home (`orion/connectors.py: sandboxed`, `orion/mcp.py: MCPHub._launch`); you can switch that off for one,
the model never can.

Proved by: `test_launch.py::test_a_trusted_server_starts_with_its_own_environment_and_the_desktop`,
`test_launch.py::test_the_tools_process_gets_no_variable_that_looks_secret`,
`test_launch.py::test_a_server_fills_in_only_its_own_secrets_when_it_connects`,
`test_launch.py::test_with_the_connectors_layer_a_server_still_fills_in_only_its_own_secrets`,
`test_launch.py::test_a_server_nobody_reviewed_starts_in_the_sandbox_with_a_home_of_its_own`,
`test_launch.py::test_a_server_nobody_reviewed_cannot_start_where_no_sandbox_can_be_made`,
`test_launch.py::test_a_sandboxed_server_really_runs_with_its_own_home_and_sees_nothing_of_orions`,
`test_secrets.py::test_what_commands_and_servers_inherit_is_an_allow_list`,
`test_secrets.py::test_a_connector_fills_in_only_its_own_secrets`,
`test_system.py::test_what_a_command_inherits_is_the_allow_list_and_a_prefix_never_lets_a_secret_in`.

### G5: what a place may never do

`safety.channels` says, for each place a request comes from (voice, the HUD, Discord, jobs, other apps), what he may never
do there and what he always asks about. The hub checks it before anything else, for every origin (the model's calls,
your own instant commands, a gesture, another app's), and a "never" is never lifted by a yes. An entry is a tool, a
connector (`<connector>:*`) or a class (`@read`, `@change`, `@destroy`, `@open_world`).

| Place | Never | Asks first |
|---|---|---|
| voice | nothing | `power` |
| HUD | nothing | nothing |
| Discord | `power`, `run_command`, `mcp_servers`, `plugins`, `settings_set`, `brain_switch`, `orion_restart`, `orion_shutdown`, `local_models`, `benchmark_brains` | `@destroy` |
| jobs | `power`, `mcp_servers`, `plugins`, `benchmark_brains` | `@destroy` |
| other apps | `mcp_servers`, `plugins` | nothing |

The tools new to this round are classed by what they declare: `openrouter_spend` and `benchmark_results` only read;
`benchmark_brains` spends money or your plan, so Discord and jobs never have it. Tools lent straight to another app
(`orion mcp-serve`, no hub) are held to the "app" limits by the tools process itself
(`orion/tools/__init__.py: _lent_refusal`).

Enforced in `orion/tools/policy.py: channel_verdict`, `check_channels` and `orion/mcp.py: MCPHub._guarded`. Proved by:
`test_guard.py::test_what_a_channel_may_never_do_is_refused_whoever_says_yes`,
`test_guard.py::test_a_never_holds_for_every_origin_and_a_channel_without_one_is_free`,
`test_guard.py::test_the_users_own_command_obeys_a_channels_never_and_is_never_asked_about`,
`test_policy.py::test_never_beats_confirm_and_a_class_or_connector_can_be_named`,
`test_policy.py::test_the_default_limits_of_each_channel`,
`test_policy.py::test_the_channels_must_be_complete_and_well_formed`,
`test_tools_process.py::test_a_tool_lent_straight_to_another_app_obeys_the_app_channels_limits`,
`test_security_api.py::test_a_channels_limits_are_set_checked_saved_and_recorded`.

### G6: after untrusted content, a yes

Once a request has read a web page, an e-mail or another app's answer (a tool that reads the open world, or any
external server's tool, taints its turn; so does a skill that is someone else's text), anything that changes or sends
something waits for your yes: in every HUD, aloud if you spoke, or with buttons on Discord. No answer in 60 seconds
means no, and the model learns only the outcome. A call that would run a command outside the sandbox always asks and can't
come from Discord, a job or another app. What it saves to memory goes to `unverified.md`, never recalled.

Enforced in `orion/tools/policy.py: confirm_reason`, `orion/mcp.py: MCPHub._guarded`, `orion/confirm.py`,
`orion/memory/writer.py`. Proved by: `test_guard.py::test_after_web_content_a_risky_call_asks_as_before`,
`test_guard.py::test_running_outside_the_sandbox_asks_every_time_and_is_impossible_from_unattended_places`,
`test_guard.py::test_a_channel_that_always_asks_asks_even_when_nothing_was_read`,
`test_confirm.py::test_the_hub_holds_a_risky_call_after_web_content_until_the_user_says_yes`,
`test_confirm.py::test_no_answer_is_no_and_a_late_answer_changes_nothing`,
`test_policy.py::test_why_a_call_asks_first`, `test_writer.py::test_scan_route_and_duplicates`.

### G7: a model can't weaken it

`orion/tools/policy.py: NEVER_AI` lists what the model never writes whatever the allow-list says: `safety.`,
`secrets.`, `activity.`, `plugins.`, `script.`, `bench.`, prompts, paths, hosts, keys, `openrouter.daily_budget_usd`; it reads some of them,
never changes any. It switches things off, never back on; it can't set a server's tokens, make one read the environment,
hand it a secret of the vault, or choose whether it runs in the sandbox (`server_change_refusal`).

Installs are asked about once, by the tools themselves (`orion/tools/core.py: Controls.ask_first`, for `mcp_servers` and
`plugins`): your yes, asked where you spoke, never from Discord or a job, no answer is no. The hub does not ask a second
time; it only enforces the channels' "never" above, which also keeps another app from changing your connectors. A
connector the model adds is sandboxed by default, and what you set stays when it changes something else.

Enforced in `orion/tools/policy.py: settings_refusal`, `server_change_refusal`, `orion/config.py: validate_patch`,
`orion/tools/core.py: Controls.save_server`, `ask_first`. Proved by:
`test_policy.py::test_never_ai_holds_even_if_the_allow_list_slips`,
`test_policy.py::test_the_new_sections_are_never_the_ais`,
`test_policy.py::test_the_ai_cannot_change_a_servers_sandbox_or_hand_it_a_secret_of_the_vault`,
`test_config.py::test_the_user_edits_safety_through_the_hud_but_the_ai_never_does`,
`test_core_security.py::test_a_server_the_model_adds_runs_in_the_sandbox_and_only_the_user_changes_that`,
`test_ai_connectors.py::test_the_ai_adds_a_server_with_a_yes_and_takes_no_name_orion_keeps`,
`test_ai_connectors.py::test_no_answer_is_no_and_discord_and_jobs_are_not_even_asked`,
`test_ai_connectors.py::test_a_plugin_is_installed_by_the_ai_only_with_the_users_yes`.

### G8: everything is recorded

`orion/activity.py: Activity.record` writes one row per tool call (who asked: you, the brain, a job, Discord, a gesture,
another app), question and its answer (and who gave it), refusal, block, alert, job run, backup, connector, plugin,
skill, secret change (by name only), login and pairing. Texts pass the redactor first, and rows older than
`activity.keep_days` (90) are forgotten daily. The HUD's Activity page reads it live (`/api/activity`), and so can he, with
titles and outcomes only (`activity_recent`: what a call carried is never handed back to a model).

Enforced in `orion/activity.py`, `orion/mcp.py: MCPHub._record`, `orion/runtime.py: _activity_event`, `keep_secret`,
`forget_secret`, `orion/__main__.py: _note_secret`. Proved by: `test_activity.py` (all three),
`test_guard.py::test_every_call_is_in_the_log_with_who_asked_and_how_it_went`,
`test_guard.py::test_who_answered_is_what_the_log_says`,
`test_security_api.py::test_keeping_and_forgetting_a_secret_is_recorded_by_name_and_hidden_at_once`,
`test_security_api.py::test_pairings_and_refused_codes_are_in_the_log`,
`test_cli_secret.py::test_a_change_is_recorded_by_name_only`,
`test_core_security.py::test_the_model_can_read_what_he_did_lately_without_what_the_calls_carried`.

### G9: the HUD answers only you

By default the server listens on `127.0.0.1` and answers its own pages there: a web page elsewhere in your browser can't
drive him (Host and Origin must be his own: no DNS rebinding, no other site, no framing). With `server.host` open to your
network, a device must also be paired with an 8-digit code (one use, 2 minutes, 5 tries). Enforced in
`orion/server.py: Envelope` and `orion/pairing.py`. Proved by: `test_server.py::test_only_his_own_pages_on_this_pc`,
`test_pairing.py::test_a_code_pairs_one_device_once`, `test_pairing.py::test_nobody_else_is_answered_at_all`,
`test_pairing.py::test_pairing_over_http`.

## The vault

Secrets live in one place, `secrets.store`: your desktop's keyring (GNOME Keyring, KWallet; the default) or `files`, a
folder only his user can open, for a server or a container. Nothing falls back from one to the other: no keyring is an
error that says what to set. `config.yaml` says `${secret:NAME}` where a secret goes. At start, a value you typed into it
is moved into the vault and `config.yaml.bak` is scrubbed too (a backup that can't be made clean is deleted). The HUD's
secret fields write to the vault the same way.

```sh
orion secret list                      # names, whether each is set, what uses it: never a value
orion secret set connector.discord.discord_token    # the value is asked for hidden, or read from stdin; never an argument
orion secret delete NAME
```

Names are `openrouter.api_key`, `calendar.<source>.<field>` and `connector.<id>.<field>`. A sign-in (OAuth) is kept in
the vault as JSON; the redactor learns the tokens inside it. In Docker the vault is files in the data volume
(`docker/prepare.py` sets it), and `CLAUDE_CODE_OAUTH_TOKEN` stays an environment variable of Orion alone.

## What it costs

Measured on this machine (the redactor against 50 secrets with their forms and the 30 shipped patterns):

| | |
|---|---|
| Redacting 100 KB of text | 0.14 ms clean, 0.34 ms with a secret in it |
| Redacting a short line | 1.5 microseconds |
| Learning a changed vault | 11 ms |
| A command's start in the sandbox | 32 ms without the network, 83 ms with it (a helper shuts the doors first) |

A streamed reply holds back the word it is in until the next piece arrives, so a secret can't show in halves; with nothing
to hide it holds nothing.

## Residual risks

- A connector you give a token can misuse that token: it has to use it. Prefer reviewed ones; unreviewed ones are
  sandboxed but can still talk to the network.
- A screenshot can show a secret: the redactor reads text, not pictures.
- Voice is whoever is in the room, within the voice limits (`power` asks first).
- The `files` vault is protected by permissions, not encryption: anything running as your user outside a sandbox can
  read it. The keyring is encrypted at rest, but any process in your session can ask it.
- The sandbox hides what `safety.sandbox_hidden` names. Your own project files and dotfiles are readable to a command, so
  a password in one is read and shown to the model unless its shape is known; add such folders to the list.
- G2 finds secrets he knows, or whose shape he knows, in the usual encodings. It does not find one split across
  arguments, reversed, encrypted, or typed digit by digit.
- A file the model writes in a writable folder can be run by you later (a script, a git hook). The settings refuse
  writable folders that hold startup files, but not what you run yourself.
- A sandbox with the network needs Linux 6.12 or later (Landlock 6). On an older kernel he refuses to run one rather than
  run it without the doors shut: turn the network off for commands, or update. There is no sandbox off Linux.
- In a container the container is the boundary: commands can read what you mounted, including the data volume and the
  vault files; their environment is still clean.
- A model steered by a page can still say something wrong. What it can't do is act past these limits unasked.

## How to verify

```sh
orion check              # the vault, the sandbox (and whether it can have the network), what each place may never use
orion secret list        # names only
```

Then try it. In the HUD ask him to run `env`, `cat ~/.ssh/id_rsa` or `ls ~/.config/discord`: no token, "No such file". On
Discord ask him to suspend the PC: refused, and a yes would not change it. Open `config.yaml` and search for a token: only
`${secret:...}`. Look at the Activity page: each of those is there with who asked.

```sh
uv run pytest tests/test_redact.py tests/test_secrets.py tests/test_vaulting.py tests/test_sandbox.py \
  tests/test_system.py tests/test_launch.py tests/test_guard.py tests/test_policy.py tests/test_security_api.py \
  tests/test_core_security.py tests/test_activity.py tests/test_tools_process.py tests/test_cli_secret.py \
  tests/test_brain.py tests/test_discord.py tests/test_writer.py tests/test_review.py
```

To add the shape of a token of your own, add an RE2 pattern to `safety.secret_patterns` (it is checked when saved).
