# Verification

## Automated checks

```bash
uv sync
uv run pytest -q
uv run ruff check .
```

CI runs the suite on Python 3.11–3.14. Telegram, STT, and LLM network calls are simulated. Real PTB objects and request serialization are exercised separately from the lightweight unit fixtures.

History checks must cover one-time opt-in, private-chat defaults, the stored old scope rule (per connection and chat, fail closed until stored, variables still set applying with it, empty variables as unset, an owner's explicit on, a repair keeping it, `chat scope clear`), create/edit/delete capture, correction classification, restart/retry behavior, partial final writes, contact renames and ambiguity, bounded date/text queries, retention, catalog rebuilds, and CLI failure exits.

Native-handler checks must demonstrate that every Business message/edit/deletion update is consumed after history capture, including owner/contact text, disabled history/transcription, failed history writes, and rejected media. Real PTB updates must also prove that `TELESEC_AGENT_ENABLE=1` permits unhandled updates through, ordinary bot messages continue normally, and text history observation does not spend an LLM call. The transcript surfaces must retain the intended `business_connection_id`, complete text, and UTF-16 entity positions.

## 0.13.0 release and live smoke: 2026-10-20

Before tagging, `hermes plugins validate --install-deps --json` ran on an isolated Hermes v0.22.2 (`d923a5eb487a17c0a68801bba9ef0f56c81040a8`, Python 3.14). It passed with exit 0, `ok: true`, and all 14 checks. The security scan was `safe`, and the dependency environment resolved with no "unsatisfiable" in stderr. Before [#78](https://github.com/neoromantic/telesec/pull/78), the scan said `caution` (`invisible_unicode`) about the literal left-to-right mark in `stats_render.py`. The one warning is the known private imports (`transcribe_audio`, `hermes_time`). 1219 tests passed locally on Python 3.13, and CI passed on Python 3.11–3.14.

The production gateway (Hermes 0.21.6) got a plugin-only update. The plugin checkout was fast-forwarded to `v0.13.0` (`e3452dca925d03cc9220766901faabde5f5c8d62`) and the gateway restarted. It wired the `telesec` native handlers, started the dossier worker, and connected to Telegram. `hermes plugins list` shows `telesec` 0.13.0 enabled. `hermes telesec settings` shows transcription, history, and dossiers on, and retention forever (the default). The log shows no Telesec error.

- **Panel.** In the owner's private chat with the bot, a controlled chat's panel already showed the Stats view. Tapping 30d edited that message into the new rich message report. Telegram reports the message as rich and edited, with the period buttons and « Back. The report has the balance bar, the per-side table, who is waiting, the heat maps of days and hours, the peak, the tiles, and the footer. The gateway logged the action, with no fallback and no error. Nothing appeared in the Business chat.
- **History import.** Importing one chat from a Telegram Desktop export on production ([#76](https://github.com/neoromantic/telesec/pull/76)) added 11,442 messages. Running it again added 0.
- **Not checked live.** The in-chat `/stats` card, because the contact would see it. The documentation screenshots of the card are its exact HTML, sent to the owner's private chat ([documentation images](#documentation-images)).

No message text, contact names, or raw contact IDs are included here.

## Stats redesign live check: 2026-10-20

The production gateway (Hermes 0.21.6) runs `v0.12.2` (`b8190d2`), which includes the stats redesign ([#64](https://github.com/neoromantic/telesec/pull/64)). The plugin checkout was fast-forwarded and the gateway restarted with it, so no extra restart was needed.

- **Panel.** From the owner's account in the bot DM, 📊 Stats on a controlled Business chat's panel edited the panel into the new HTML report: title, dates and time zone, share bars and the per-side table in one monospace block, sparklines and the summary in another, the footer, and the period buttons with « Back. In Telegram for macOS both blocks were aligned, with one copy bar each.
- **Periods.** 7d, 90d, All, and 30d each re-rendered the report with the selected button marked. 90d grouped activity by week. Retained history started that day, so 7d, 30d, and 90d said "period partial" and All covered one day.
- **Time zone.** The header showed the Hermes time zone from `config.yaml`; the plugin no longer has its own setting.
- **Not checked live.** The in-chat `/stats` card: the contact would see it, so it stays with the owner.
- **Logs.** The gateway logged the five panel actions and no Telesec error or rich-edit fallback. No message appeared in the Business chat, and the panel was left on 30d.

No message text, contact names, or raw contact IDs are included here.

## 0.12.2 release: 2026-10-20

Before tagging, `hermes plugins validate --install-deps --json` on an isolated Hermes v0.22.2 (`d923a5eb487a17c0a68801bba9ef0f56c81040a8`, Python 3.14) passed: exit 0, `ok: true`, all 14 checks, security scan `safe`, and the dependency environment resolved with no "unsatisfiable" in stderr. The warnings are the known private imports (`transcribe_audio`, `hermes_time`). 1177 tests passed locally on Python 3.12. CI passed on Python 3.11–3.14.

The production gateway (Hermes 0.21.6) was updated plugin-only: the plugin checkout was fast-forwarded to `v0.12.2` (`b8190d2022c5deeffdc1ae38d279defd6bb38aac`), the service drop-in that set `TELESEC_VOICE_BYPASS_AUTH=1` was removed, and the gateway restarted. It wired the `telesec` native handlers, started the dossier worker, and connected to Telegram; the gateway process no longer has the variable, `hermes plugins list` shows `telesec` 0.12.2 enabled, and `hermes telesec settings` kept transcription, history, and dossiers on. The log shows no Telesec error.

- **Not run live:** a voice note from a contact outside the DM allowlist. It needs a real contact's message; `test_native_business_media_dispatch` covers allowlisted and non-allowlisted senders.

## 0.12.0 and 0.12.1 release and live smoke: 2026-10-19

Before tagging, `hermes plugins validate --install-deps` on a clean Hermes v0.22.2 passed for 0.12.0. CI ran 1026 tests on Python 3.11–3.14.

The production gateway (Hermes 0.21.6) was updated plugin-only.

- **0.12.0 and migration.** The plugin checkout was fast-forwarded to `v0.12.0`, and the gateway restarted at 20:26 UTC. The one-time migration stored history as on (`via migration`) from the old environment variable and logged a warning. Transcription stayed on by default, and the 0.11.0 dossier switch stayed on.
- **Incident.** About one minute later, a scheduled Hermes maintenance run rebuilt the plugin dependency environment and could not resolve it: the 0.12.0 dev dependency group asked for a newer ruamel.yaml than Hermes 0.21.6 pins. Hermes then disabled Telesec in the config. The running gateway kept working, but the next restart would not have loaded the plugin. `hermes plugins validate` had printed ok; the resolver error was only in stderr.
- **0.12.1 fix.** [0.12.1 (#66)](https://github.com/neoromantic/telesec/pull/66) makes the dev group name packages only, without versions. After a fast-forward to `v0.12.1`, `hermes plugins enable telesec` resolved the dependency environment. The gateway restarted at 20:35 UTC, wired the `telesec` handlers, started the dossier worker, and connected to Telegram. `hermes telesec settings` showed transcription on (default), history on (via migration), and dossiers on (via migration).
- **Live smoke** from the owner's account in the bot DM. `/telesec` opened Settings with the three rows, all on. `/start bizChat<chat_id>` opened the panel with the transcription row, Reset, and the new Save history / Don't save and Keep dossier / No dossier rows. Don't save answered "History: off for this chat", stored one entry, and hid the dossier row behind "Dossier: needs history". Save history answered "History: on for this chat" and removed the entry. ⚙️ Settings opened Settings with « Back. No new message appeared in the Business chat, and the four bot-DM fixtures were deleted. The gateway logged no rich-edit fallback and no Telesec error.
- **Screenshots.** `settings.png`, `owner-panel.png`, and `dossier-panel.png` were retaken on 0.12.1 right after the smoke, with no switch or mode changed ([Documentation images](#documentation-images)).

No message text, contact names, or raw contact IDs are included here.

## 0.11.0 release and live smoke: 2026-10-19

Before tagging, `hermes plugins validate --install-deps --json` on an isolated Hermes v0.22.2 (`d923a5eb487a17c0a68801bba9ef0f56c81040a8`) passed: exit 0, `ok: true`, all 14 checks, security scan `safe`. The only warning is the known private STT import. CI passed on Python 3.11–3.14.

The production gateway (Hermes 0.21.6) was updated plugin-only to `v0.11.0` (`cbca9743ad7b4662a8b4f3aae684282afb241015`) and restarted.

- **Switch migration.** Before the update, dossiers were on through the old environment variable, and no switch was stored. On the first start the switch was stored as on with `updated_via: migration` and file mode `0600`. The variable was then removed from the gateway's service environment, and the gateway was restarted again. Dossiers stayed on, and the worker started with `enabled=True`.
- **Owner panel.** `/start bizChat<chat_id>` from the owner's account opened the panel with a new Dossier button next to Stats. Dossier opened the overview with Manage facts, Refresh, My persona, Disable (the switch shows on for an owner of every known connection), and « Back. Manage facts listed the chat's facts. A fact card had Pin, Remove, and ‹ Facts. ‹ Dossier, My persona (Manage facts, ‹ Dossier), and « Back to the main panel worked. The gateway logged no "rich edit failed" warning, so the views were rich messages, not the plain fallback. The panel language was English because the test client does not report a Russian Telegram language. Nothing appeared in the Business chat, and the two bot-DM fixtures were deleted.
- **Not run live:** the owner's `/dossier` in a Business chat. It posts into a real contact's chat, so it needs a chat the owner picks; tests cover it.
- **Finding:** the migration's info log line is missing from the gateway log, because `register()` runs before Hermes sets up its file logging. The stored switch is correct; the next release logs it as a warning.

No message text, contact names, or raw contact IDs are included here.

## 0.10.1 fix: 2026-10-19

The rename added a top-level PyYAML import in `migration.py`. Hermes 0.22 depends on ruamel.yaml and does not install PyYAML, so `hermes plugins validate` on Hermes v0.22.2 failed the capability probe with `No module named 'yaml'` for 0.10.0. 0.10.1 reads YAML only when the config still names the old plugin, with a ruamel.yaml fallback. With the fix, `hermes plugins validate --install-deps` on an isolated Hermes v0.22.2 (`d923a5eb487a17c0a68801bba9ef0f56c81040a8`) passed: capability probe, declared hooks, security scan `safe`, no core override, and the known private STT import warning. The production gateway (Hermes 0.21.6) has PyYAML and was not affected.

On the production gateway the plugin checkout was fast-forwarded to `v0.10.0` and then to `v0.10.1` (`365ee6665187a3a534ee69b484db95f023a3994d`), with a gateway restart each time. After the last restart the gateway wired the `telesec` native handlers, started the dossier worker, and connected to Telegram at **16:30:53 UTC**. The code differs from the earlier live smoke only by the lazy YAML import, so no new Telegram smoke was run.

## Telesec rename checks: 2026-10-19

Before production cutover:

- [CI passed on Python 3.11–3.14](https://github.com/neoromantic/telesec/actions/runs/39276103429), including migration, conflicting destinations, private permissions, deprecated aliases, and the five settings.
- The production Hermes 0.21.6 CLI validated the candidate in isolation: manifest v2, config schema, registration/capabilities, security scan, and core-override checks passed. This version reported no warnings; newer Hermes versions' private STT warning remains documented in the catalog readiness checks below.
- In a separate temporary profile on the production host, the real `PluginContext.set_config` persisted all five settings and `get_config` returned them to the registered plugin. History, dossier enablement, cleanup style, timezone, and agent passthrough used the saved values; an explicit environment value overrode the form. The config stayed `0600`; production config was untouched.
- A private copy of the current production data/config migrated to `telesec`: all 13 file SHA-256 hashes stayed identical, every file was `0600`, and the second migration left config bytes unchanged. History, the contact catalog, transcription modes, and any dossier/state files share the same directory migration.

Production cutover and live Telegram acceptance are recorded below.

## Telesec production cutover: 2026-10-19

[PR #42](https://github.com/neoromantic/telesec/pull/42) installed `fe29e489bd8559699ed353e068ed07b1b742469c` as `telesec` on the production gateway; the GitHub repository and local origin now use `neoromantic/telesec`. No tag or release was created in this cutover. The Python code passed all **714 tests**, and CI passed on Python 3.11–3.14 before the final docs-only rebase.

- With the gateway stopped, all **13 data files** had identical SHA-256 hashes before and immediately after migration. Canonical history stayed at **13 events**, and dossiers at **one file**. Migrated files remained `0600`. The complete old config entry, including LLM grants, moved unchanged to `plugins.entries.telesec`; the enabled list retained the same selections under the new name.
- The supported remove/install path was used. Hermes's launcher automatically completed pending dependency/runtime preparation without changing core `818c13be1dc4fd28987e1e881a9408224afd4535` (0.21.6), Python 3.14.7 and PTB 22.8; no core update or maintenance wrapper was invoked. The first headless install declined dependency consent; retrying with `--yes-deps` succeeded. The upgrade script now includes this flag.
- First startup connected Telegram at **15:35:59 UTC** with both old service environment overrides byte-for-byte unchanged. The legacy history/dossier flags enabled the dossier worker and history verification. Then those overrides and three cleanup variables moved to `TELESEC_*`; after the immediate restart, all three platforms connected by **15:38:36 UTC**. The gateway's initial environment contained no old plugin variable names, and its session store reported `ok`.
- `hermes telesec mode list --format json` read the migrated owner modes. The real owner opened `/start bizChat<id>`, saw the mode controls and Stats section, and selected `both`. A six-second synthetic owner voice received its complete transcript in its original caption at **15:39:32 UTC** (`cleaned=True`, `caption_outcome=attached`). The voice and the two bot-DM fixtures were deleted; the controlled chat returned to stored/effective `off`.
- A **natural incoming contact text** at **15:37:34.969446 UTC** was captured in canonical history. The exact Telegram update appeared in the plugin's consume log, and `state.db` contained **zero new Telegram user/assistant messages for that chat since cutover**. This closes the earlier fresh-contact isolation check. No incoming contact message was fabricated for this check.
- The owner also confirmed a real Telegram **Manage Bot** tap: its panel chat ID matched the Business chat ID in the gateway log. The real-payload question from the earlier smoke is closed.
- After live traffic and deletion of the voice fixture, `history verify` returned `ok=True chats=3 files=3 records=17 repaired_files=0 errors=0 warnings=0`. The increase from 13 to 17 happened after migration during normal capture and smoke cleanup.
- Exactly four install-path literals in the two host maintenance scripts changed to `plugins/telesec`; Bash/Python syntax checks passed. Source and private backup paths are recorded in the maintainer's private host notes.

Evidence is content-free here: no contact names, message text, or raw contact IDs are included. Private before/after manifests, config/override backups, and the natural-contact history/log/database receipt remain on the owner host.

## Production Business text investigation: 2026-10-19

No recent Business-text agent turns were found on the production gateway: a separate profile-local `telegram-business-dm-passive-guard` 0.2.0 was already protecting it. The plugin under test (`de52645`) alone passed ordinary Business text through; this was reproduced with real PTB updates before adding the default consume rule.

Read-only checks of the gateway service, its `gateway.log`, `state.db`, and `sessions/sessions.json` found:

- The local guard was enabled and its disable flag was unset. Hermes core was `818c13be1dc4fd28987e1e881a9408224afd4535`.
- Between October 16 and October 19 at 16:32:55 Europe/Berlin (UTC+02:00), the gateway logged 359 `pre_gateway_dispatch skip: reason=telegram_business_dm_passive_guard` events across 24 Business chats: 97, 129, 53, and 80 per day.
- None of those chat IDs had Telegram user or assistant messages in the session database since October 16 UTC. Five retained session-index entries predated that window; none had an active turn. The recent Telegram turns belonged to other chats.
- Core `gateway/run_inbound.py` returns immediately on that skip result, before authorization and agent dispatch. Thus the production protection came from the separate local guard, not this distributable plugin.

No message text, contact names, or raw contact IDs are included here.

## Catalog readiness checks: 2026-10-19

Used fresh Python 3.14.5 virtual environments and temporary `HERMES_HOME` directories, without the installed Hermes 0.21.2 or production credentials:

| Hermes source | Result |
|---|---|
| `v0.21.6`, commit `818c13be1dc4fd28987e1e881a9408224afd4535` | Minimum supported version: CLI validation passed; real PluginManager/PluginContext discovery bound the host `PluginLlm` and registered the CLI, skills, hook, and native factory. |
| `v0.22.2`, commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8` | CLI validation passed; security scan `safe`; no core override. |
| Current `main`, commit `493208879a3248c6a4b844981d2c31c78ae00929` | `hermes plugins validate /path/to/plugin --install-deps --json` passed, including dependency preparation and capability matching. Security scan `safe`; no core override. |

The minimum-version smoke used a real PTB Application and an in-memory Telegram transport. All three handlers registered in group `-100`; text history persisted one synthetic event, mode `off` claimed a voice note without download or reply, the panel matched `tb:` callbacks, and an unrelated callback reached core. Importing the configured STT entrypoint succeeded. No Telegram or model request left the process, and no gateway was restarted.

The full plugin suite passed **700 tests**. PTB emitted 28 existing duration deprecation warnings for voice/video/audio objects; these are separate from catalog admission.

The `tools.plugin_guard.scan_plugin` admission scanner uses the `tools.skills_guard` engine. Release and current-main scans returned **SAFE / ALLOWED**, with no high or critical findings. Current main reported 12 medium and 4 low findings: disclosed ffmpeg/ffprobe subprocesses, a concurrency-test subprocess, escaped control-character handling/test fixtures, documentation install commands, developer instruction files, and the 1.4 MiB illustration. No scanner exclusions were added.

One validation warning remains on 0.22.2 and current main: `__init__.py:1687 tools.transcription_tools.transcribe_audio` is an undocumented core import. No documented consumer STT facade exists in the checked API; the [submission draft](CATALOG_SUBMISSION.md) explains why this import remains. Isolation is informationally `in_process`: native Telegram handlers need the live Application, and the CLI registration does not cross the plugin-host boundary.

The validator's recording context lacks the documented `ctx.llm` property. The plugin tolerates its absence during registration; the minimum-version smoke confirmed a real context still supplies `PluginLlm`. This does not change model-provider or trust-gate behavior.

## Live gateway smoke: Business update isolation, 2026-10-19

[PR #37](https://github.com/neoromantic/telesec/pull/37) deployed plugin `d2201a8bf88f2743e0f48d38d6c5461bfc741d60` to the production gateway. [All 700 tests passed on Python 3.11–3.14 with PTB 22.8](https://github.com/neoromantic/telesec/actions/runs/39272026538). The separate local `telegram-business-dm-passive-guard` was disabled; its files remain installed. `HERMES_TELEGRAM_BUSINESS_AGENT_ENABLE` was not enabled.

- The gateway service restarted at 14:51:08 UTC. Telegram, API, and webhook reported `connected`, the gateway reported `running`, and its session store reported `ok`.
- Hermes core remained `818c13be1dc4fd28987e1e881a9408224afd4535` (0.21.6). The `hermes plugins disable` launcher unexpectedly completed a pending source-update dependency tail before disabling the guard. The running gateway used managed Python 3.14.7 and PTB 22.8; `uv pip check` reported all 152 installed packages compatible in that active environment. The older Python 3.11.16 venv also passed `python -m pip check`. No core source upgrade was requested.
- The real owner account sent the Manage Bot `/start bizChat<id>` payload in the bot DM. The panel showed the chat label, stored `off` mode, all four modes, Reset, and Refresh. Selecting `own` returned `Transcription: own`; after the voice check, selecting `off` returned `Transcription: off` and restored the selected button.
- A six-second synthetic owner voice note in the existing controlled Business conversation was accepted with `mode=own direction=own decision=transcribe reason=own_message`. It received the complete transcript in its original caption. The test voice was then deleted for both parties; the bot-DM fixture and its generated replies were also removed.
- A separate panel-follow-up deployment advanced the plugin to `0b4ea669f6035e5ef2d559b31219873f7cbd3010` and restarted the gateway at 15:16:44 UTC; all platforms reconnected by 15:17:29. That revision includes `d2201a8` as an ancestor and retains the Business-update isolation; the local passive guard remains disabled. Exclude this 45-second interruption from the traffic observation.
- Observation from 14:51:43 to 15:22:59 UTC covered more than 30 healthy minutes after excluding that interruption. The plugin consumed eight updates across the bot-DM Business mirror and the controlled voice/deletion fixture; the old local guard recorded zero skips, and `state.db` had zero new Telegram user/assistant messages for those consumed Business chat IDs. No natural human-contact text arrived in this window, so fresh incoming-contact isolation is **pending**, not a passed live check. By coordinator decision, the post-rename deployment smoke will collect that evidence.

The separate local guard can be restored with `hermes plugins enable telegram-business-dm-passive-guard` in the gateway's profile and a gateway restart. Keep `HERMES_TELEGRAM_BUSINESS_AGENT_ENABLE` unset for this plugin's default protection.

## Live gateway smoke: owner panel follow-ups, 2026-10-19

Plugin `0b4ea66` (main after #40, #43–#46) ran on the owner's gateway with Hermes `0.21.6`. Only the plugin was updated, then the gateway was restarted.

- Sending `/start bizChat<chat_id>` from the owner's account opened the panel. The header showed the chat label and `chat <chat_id>`. The mode line, the four mode buttons with the current mode marked, Reset to default, and Refresh were present. The test client does not report a Russian Telegram language, so the panel was in English. The Russian strings are covered by tests only.
- Refresh answered "Refreshed". No mode was changed.
- Telegram also sends the owner's private chat with the bot as Business updates. After the restart, the `/start` message and the panel reply were not written to history. `history contacts` and `stats --format json` do not list that chat. Records from before this fix stay on disk, but readers skip them.
- `hermes telegram-business mode get <chat_id> --format json` returned the mode, `effective_mode`, `source: stored`, the label, and `business_connection_id`. `mode list --format json` returned the default mode, `transcription_enabled`, and the chats. On this host the history opt-in is set only in the gateway's service environment, so the CLI run needed it in its environment too.
- The host rendered the plugin's `telegram-business.reference` system prompt section (1218 characters).

This earlier smoke typed `/start` by hand. The owner later confirmed the real Manage Bot payload; see the [Telesec cutover](#telesec-production-cutover-2026-10-19).

No message text, contact names, or raw contact IDs are included here.

## Live gateway smoke: owner panel, 2026-10-19

On the owner's gateway at `de52645`: sending `/start bizChat<chat_id>` to the bot from the owner's account (what "Manage Bot" sends) opened the panel with the chat label, the current mode (`off`), the four mode buttons, Reset, and Refresh. Tapping the current mode answered with the toast "Transcription: off" and kept the mode. Nothing appeared in the Business chat.

## Live gateway smoke: 0.9.0, 2026-10-19

Plugin `0.9.0` ran on the owner's production gateway (Hermes `0.21.6`) with a real Business connection. In a real private chat, `/transcribe off` was confirmed in place and the next voice note was skipped silently (`decision=skip reason=mode_off`). `/transcribe both` restored transcription, and the next voice note got its transcript caption. `/transcribe`, `/transcribe reset` and an unknown argument returned the expected status lines. The modes file was written `0600` and listed by `hermes telesec mode list`. The live run showed that the contact could see command messages, so 0.9.0 deletes the command and its status about 5 seconds after confirming.

## Current-core smoke: 2026-10-18

Plugin `0.8.0` at [`0e758ffc`](https://github.com/neoromantic/telesec/commit/0e758ffc1bbbc759b6b81e8f6bd557927a15ec7a) was installed and enabled through the actual CLI in an isolated profile using tagged Hermes `v0.22.2`, Python `3.14.5`, and PTB `22.8`. [CI passed 243 tests on every Python version from 3.11 through 3.14](https://github.com/neoromantic/telesec/actions/runs/39147890386).

- Real `PluginManager`/`PluginContext` discovery registered the hook, CLI, history skill, and one Telegram handler factory. Unload and forced rediscovery removed and restored the registrations without duplication.
- Real `TelegramAdapter` and `TelegramApplication` processed an in-memory Telegram transport. History opt-out, create/edit/retry/delete capture, and delayed classification produced eight canonical events, including `likely_duplicate` and `unexplained` deletions. Text capture made no model calls; handled voice updates did not reach the ordinary handler.
- Separate CLI processes resolved the synthetic contact, showed and searched its history, listed unexplained deletions, and returned `ok=True ... records=8 ... errors=0 warnings=0` from `history verify`.
- Synthetic incoming and outgoing voice notes passed through Hermes's real OpenAI STT backend and host-owned `PluginLlm` cleanup (`gpt-4.1-mini-2025-04-14`, temporary profile overrides). The simulated Telegram transport received one reply and one caption edit; amounts, time, and negation survived, and transient audio was removed.
- The default `gemini` / `gemini-3.8-flash` passed the real trust gate, provider resolver, native Gemini adapter, and structured-output parsing with simulated Google HTTP. The generated request targeted `/v1beta/models/gemini-3.8-flash:generateContent`, requested JSON schema output, and omitted sampling parameters. This used a synthetic credential placeholder and sent no request to Google.

No real Telegram conversation was contacted and no production gateway was restarted. Actual Business delivery/client rendering and live Gemini 3.8 quality, latency, and credential availability remain to be checked with the controlled smoke below.

## Controlled live smoke

Use an explicitly selected test Business conversation and the intended Hermes profile. These steps contact Telegram; the automated suite does not execute them.

1. Install the branch into the selected profile, enable the plugin, and restart its gateway. Check registration and errors in that profile's logs.
2. With history disabled, send a text message and verify that no history record is created.
3. Enable history. Send and edit text from each side; confirm `history show --contact ...` reports the events and contact names.
4. Delete an original message and separately delete/resend a small correction. After the configured window, inspect `history deletions` for distinct classifications.
5. Restart the gateway and verify existing records remain readable without duplicate events. Check `history verify` and catalog recovery.
6. Send short incoming/outgoing voice notes and a round video note. Verify one logical transcript per source and outgoing caption behavior in Telegram clients.
7. Send an attached speech recording and a tagged music file. Check that the speech is transcribed and music is skipped without an agent turn.
8. Exercise cleanup with the configured Gemini model and trust allowlist. Compare Russian, English, and mixed-language examples containing names, dates, amounts, negations, and long paragraphs against raw STT. Record fidelity and time to visible text.
9. Open Settings with `/telesec`. Check the retention row, the `Keep history:` line, and the model lines (no credentials). Tap `30 days`, check the question text, and tap Cancel; `hermes telesec settings` must still show the old retention. Tap the value in effect and check that it applies with no question. Never confirm a shorter value on a profile with real history: check "a longer time applies at once" only in tests or on a test `HERMES_HOME`.
10. Disable/remove the plugin and restart. Confirm ordinary Hermes Telegram behavior still works.

Keep evidence free of real contact details and message content. Record Hermes/PTB/Python/model versions and distinguish simulated checks from live Telegram/model results.

## Documentation images

`voice-transcript.png` and `stats-panel.png` are real Telegram for macOS screenshots from the production gateway on 2026-10-19 (code identical to 0.10.0). The voice note was a synthetic 10-second English recording sent by the owner in a controlled Business chat. Its transcript was added to the caption about 4 seconds later. The Stats view was opened from the owner's private chat with the bot. Contact names, usernames, and chat IDs are blurred. Afterwards the chat's mode was set back to `off`, and the test voice and panel messages were deleted. `banner.png` (1200x600) is rendered from `docs/images/src/banner.html` and uses the voice screenshot.

`settings.png`, `owner-panel.png`, and `dossier-panel.png` are real Telegram for macOS screenshots from the production gateway on 2026-10-19 running 0.12.1. Settings was opened with `/telesec`, and the panel with `/start bizChat<chat_id>` for a controlled Business chat, both from the owner's private chat with the bot; the Dossier overview was opened from the panel's Dossier button. Settings shows all three switches on. The contact's name, username, and chat ID are blurred; the overview's fact sections are collapsed, so no fact text is visible. Headings, counts, and buttons are not blurred. No switch, mode, or per-chat row was tapped, nothing was sent in the Business chat, and the commands and panel messages were deleted afterwards.

`stats-panel-en.png`, `stats-panel-ru.png`, `stats-card-en.png`, and `stats-card-ru.png` (and their `@2x` files) are real Telegram for macOS screenshots taken on 2026-10-20 of the rich stats views over fictional history. `scripts/render-stats-screenshots.py` wrote the exact rich message HTML of each view, and the production bot sent it with `sendRichMessage` to the owner's private chat with it; the panel screenshots carry inert copies of the period and Back buttons. Each tall panel is two captures of the same message joined where they match pixel for pixel. Nothing shows a real contact, no Business chat was touched, and the messages were deleted afterwards. `stats-panel.png` still shows the old plain-text view, because the current catalog entry links it.

`dossier-fact.png` is a real Telegram for macOS screenshot from the production gateway on 2026-10-19 running 0.11.0, a fact card opened from the same chat's Dossier overview. The contact's username, the fact text, the message ID, and the source quote are blurred; headings and buttons are not. `/dossier` was not sent in the Business chat, and the panel messages were deleted afterwards.

The transcript illustration depicts implemented delivery shapes. The history image contains actual CLI output captured from synthetic Business updates. The assistant illustration is explicitly a future-design concept. All use fictional content and have editable HTML sources in `docs/images/src/`.

Render with `agent-browser --engine chrome`, inspect the image, and check for clipped text before replacing PNGs. A design illustration is not evidence of Telegram client rendering or successful delivery.
