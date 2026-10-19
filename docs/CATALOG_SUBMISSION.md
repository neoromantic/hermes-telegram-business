# Catalog submission draft

Not submitted. Keep the repository, manifest name, and runtime ID unchanged in this readiness PR. The public name will be chosen in a separate rename change.

Replace `NAME`, `RELEASE_VERSION`, and `FULL_40_CHARACTER_RELEASE_COMMIT_SHA` after the rename and batched release. Re-run validation on that exact commit and replace the validation excerpt below. Add an actual owner-panel screenshot before submission; the current transcript illustration and synthetic history CLI image are labelled examples, not live Telegram evidence.

References: [catalog policy](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/README.md), [submission guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission), [plugin API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/), and the [stickers README disclosure example](https://github.com/churnast/hermes-telegram-stickers#-privacy-and-safety).

## Catalog YAML

Draft file: `plugin-catalog/NAME.yaml`. Only this YAML file belongs in the upstream catalog PR.

```yaml
name: NAME
repo: https://github.com/neoromantic/hermes-telegram-business
sha: FULL_40_CHARACTER_RELEASE_COMMIT_SHA
description: Telegram Business transcription, opt-in text history, correspondence stats, and contact dossiers.
maintainer: neoromantic
tier: community
category: voice
requires_hermes: ">=0.21.6"
docs_url: https://hermes.sergeybaus.com
version: "RELEASE_VERSION"
title: "NAME"
screenshots:
  - https://raw.githubusercontent.com/neoromantic/hermes-telegram-business/FULL_40_CHARACTER_RELEASE_COMMIT_SHA/docs/images/hermes-telegram-business@2x.png
  - https://raw.githubusercontent.com/neoromantic/hermes-telegram-business/FULL_40_CHARACTER_RELEASE_COMMIT_SHA/docs/images/history-search.png
readme: true
known_issues:
  - Requires in-process plugin mode for native Telegram handlers.
  - Uses the undocumented host STT entrypoint tools.transcription_tools.transcribe_audio.
  - Settings use environment variables; the Desktop settings form is not wired yet.
capabilities:
  provides_tools: []
  provides_hooks: [pre_gateway_dispatch]
  provides_middleware: []
  requires_env: []
```

The final catalog name and manifest identity must agree with the chosen rename plan. Do not submit `NAME` or a branch/tag in place of the full commit pin. The existing images must be pinned to that same commit. Add the owner-panel screenshot to `screenshots` once captured.

## PR body

### Summary

Add **NAME** as a community voice plugin, version **RELEASE_VERSION**, pinned to **FULL_40_CHARACTER_RELEASE_COMMIT_SHA**. I own the plugin repository. This PR adds one catalog YAML file.

The plugin transcribes Telegram Business voice notes, round videos, and speech attachments without a tool-agent turn. Owners control each chat from Telegram's Manage Bot panel. Separate opt-ins enable text history with edits and preserved deletions, deterministic correspondence stats, and evidence-backed contact dossiers plus an owner writing persona.

It uses `ctx.register_telegram_handler`, `pre_gateway_dispatch`, `ctx.register_cli_command`, `ctx.register_skill`, `ctx.register_system_prompt_section` (a short neutral reference, when the host has it), `ctx.on_unload`, and the host-owned `ctx.llm` facade. It registers no agent tools or middleware and does not override Hermes core. All native PTB handlers use group `-100`; owner-panel callbacks match only `tb:`. No catch-all `CallbackQueryHandler` is installed.

**Disclosure:** Telegram Bot API calls download media, resolve Business owners, edit captions, send transcripts and status messages, and answer panel interactions. Audio reaches Hermes's configured STT backend, which may be local or external. Cleanup sends transcript text through `ctx.llm`; opt-in dossiers send retained private-chat text through the same facade. The plugin uses host-owned credentials and has no separate credential store. Local `ffprobe`/`ffmpeg` subprocesses check and normalize attached audio with argument lists and no shell. Transcription runs asynchronously; history schedules classification/maintenance; dossiers use an opt-in gateway background thread. Profile-local storage retains modes and owner mappings, opt-in JSONL text history/contact metadata, and opt-in dossier facts, quotes, revisions, persona, and worker state. Transient media is deleted after processing; stored history and derived data follow the documented retention/removal controls. History defaults to no age pruning and a 1 GiB target for closed partitions. Removing the plugin leaves user data. **No telemetry, analytics, usage reporting, self-updater, or credential-file reads.**

**Undocumented STT dependency:** The only private core import is `tools.transcription_tools.transcribe_audio`. We use it to invoke the user's existing STT configuration, provider selection, credentials, and transcription hooks without implementing a second backend client. `ctx.register_transcription_provider` registers a backend; it does not expose the configured transcription operation to a consumer plugin, and `transcribe_audio` is not a registered tool for `ctx.dispatch_tool`. No documented consumer facade exists in the checked release/API guide. We retain this read-only import, disclose the warning, and would adopt a public host STT facade when available. No Hermes function or registry is replaced or mutated.

**Settings limitation:** Current user settings are environment variables, including history/dossier enablement, cleanup style, retention/scope limits, and stats timezone. Non-secret `config_schema` fields save to `plugins.entries.<id>.settings`, without an automatic environment binding. The schema stays empty rather than advertising controls the plugin does not read. Wiring these existing settings through `ctx.get_config` is a follow-up for the rename wave; no new settings or telemetry toggle are planned.

**Validator probe limitation:** Hermes v0.22.2's recording context omits the documented `ctx.llm` property. Registration tolerates its absence with `getattr(ctx, "llm", None)`, so the capability probe can finish without model access. Real `PluginContext` supplies the host facade. Missing model access never causes the plugin to create its own client or bypass the host trust gate.

**UI evidence:** Include the pinned images from the YAML and label their synthetic/illustrative content. Add the actual owner-panel screenshot captured for the final release.

### What it does

- Transcribes Business voice notes, round videos, and speech attachments without an agent turn, with per-chat owner controls.
- Preserves opt-in JSONL text history, including edits and deletions, with contact-first search and export.
- Computes correspondence stats without an LLM or additional stored state.
- Builds opt-in, editable contact dossiers and an owner writing persona with source citations.

Reply drafts and automatic replies are planned, not shipped. PTB takes one matching handler per group; interoperability with other Telegram handler plugins has not been tested.

### Validation

Readiness validation is recorded in [VERIFY.md](VERIFY.md#catalog-readiness-checks-2026-10-19). Before submitting, paste the exact release-pin output here, including the private-import warning; do not describe a warning-bearing run as warning-free.

```text
Readiness check, current Hermes main 493208879a3248c6a4b844981d2c31c78ae00929:
hermes plugins validate /path/to/plugin --install-deps --json -> exit 0, ok: true
capability probe: register() ran in isolation
declared tools/hooks/middleware: matches registrations
security scan: safe
no core override: no runtime rebinds of Hermes core
warning: __init__.py:1687 tools.transcription_tools.transcribe_audio is a private core import
isolation: in_process (native Telegram Application required)
plugin_guard / skills_guard: SAFE / ALLOWED; 0 critical, 0 high, 12 medium, 4 low
pytest: 700 passed (28 existing PTB duration deprecation warnings)
Hermes v0.21.6: validation passed; real-context/PTB minimum-version smoke PASS
Hermes v0.22.2: validation passed; security scan safe
```

The plugin security scanner uses `tools.plugin_guard.scan_plugin`, which applies the `tools.skills_guard` engine with plugin-specific treatment of subprocesses, tests, and documentation. A raw skill-directory scan is not the plugin admission check.

The scanner findings are the disclosed local media processes, concurrency-test process, escaped control-character handling/test fixtures, documentation install commands, developer instruction files, and large illustration. No scan exclusions were added. The only admission warning is the STT import disclosed above.

Minimum Hermes version: `0.21.6`, verified with the isolated minimum-version check. Python runtime for validation: 3.14.5; PTB: 22.8. No new Python runtime dependencies are declared by the plugin. No release, deployment, or upstream submission is part of this readiness PR.
