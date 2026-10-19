# Changelog

## 0.11.0 - 2026-10-19

- Add the panel's Dossier section ([#17](https://github.com/neoromantic/telesec/issues/17)): the chat's dossier as a Telegram rich message in the owner's app language (Russian or English), with an overview, a fact list, fact cards with current history text, Pin/Unpin, Remove with confirmation, and a background Refresh. The persona and the on/off switch are offered only to an owner of every Business connection the bot knows about (history, stored dossiers, the persona's chats, recorded owners, and connections seen since the gateway started). Stale buttons never edit a rebuilt dossier, long dossiers degrade to fit Telegram's limits, and a rejected rich message falls back to plain text.
- Add the owner's in-chat `/dossier`, deliberately visible to the contact: the bot deletes the bare command and posts that contact's dossier into the chat, without owner notes and never the persona. The post leaves out current history text, change marks, and the quotes of deleted, edited, or gone messages, and skips unsupported facts and facts with no source left in history (every source deleted, gone, or deleted and resent). When history cannot be read to check the sources, nothing is posted. Telegram allows it only if the contact wrote in the last 24 hours. When there is nothing to post, nothing appears in the chat and the bot tells the owner why in their private chat with it. A contact's, forwarded, inline-bot, or longer `/dossier …` stays ordinary text; the command and the post are kept out of history and away from the agent.
- Turn dossiers on and off from the owner panel or with `hermes telesec dossier enable|disable`. The choice is stored in `dossiers/settings.json`. No gateway restart is needed: the worker checks the switch every minute and a panel switch wakes it.
- Remove `TELESEC_DOSSIER_ENABLE` and the Desktop `dossier_enable` setting: dossiers are no longer switched by configuration. On first start, a setup that turned them on that way gets the stored switch set to on once (shown as `via migration`), so its dossiers keep running; after that the variable and the setting are ignored.
- Owner panel sections can return rich message HTML (`PanelView.html`, with a plain-text fallback).
- A worker cycle that finds another cycle running retries a minute later instead of after a full interval, and plugin unload also stops a running panel refresh.

## 0.10.1 - 2026-10-19

- Fix loading on Hermes 0.22+, which no longer installs PyYAML: the one-time config migration now reads YAML only when the config still names the old plugin, and uses Hermes's ruamel.yaml when PyYAML is missing.

## 0.10.0 - 2026-10-19

- Rename the product, runtime plugin ID, catalog key, CLI, skills, and repository to Telesec.
- Automatically migrate history, the contact catalog, transcription modes, dossiers, plugin state/cache, and Hermes config entries without changing content or private permissions. See the README upgrade script for the required name-based reinstall, including explicit dependency consent for headless installation.
- Use `TELESEC_*` environment variables; old names and `hermes telegram-business` remain deprecated aliases for this release.
- Expose history, dossiers, cleanup style, timezone, and agent passthrough in Hermes Desktop settings, with environment overrides.
- Consume all Business message, edit, and deletion updates after history capture and transcription routing so ordinary contact/owner text cannot start an agent turn. Set `TELESEC_AGENT_ENABLE=1` to allow unhandled updates through the ordinary Hermes auth/agent path. Bot DMs and the owner panel keep their existing routing.
- Prepare manifest v2 metadata with a tested Hermes 0.21.6 minimum and catalog submission notes; disclose network calls, local processes, background work, storage, retention, and the absence of telemetry.
- Allow the catalog validator's recording context to register the plugin without its missing `llm` property; real Hermes continues to supply `ctx.llm`.
- Add the owner panel: Telegram's "Manage Bot" button in a Business chat opens the owner's private chat with the bot, which answers `/start bizChat<id>` with that chat's transcription mode as buttons (`both`, `own`, `peer`, `off`, reset). Only a verified Business owner gets it, nothing is visible to the contact, and changes are stored like the chat command's (`updated_via: chat`, readable by 0.9.0).
- Remove the in-chat `/transcribe` command: the owner panel is the only control inside Telegram. A `/transcribe` typed in a Business chat is now ordinary text (consumed like any Business text and kept in history when history is on). Without `--owner`, the first CLI `set`/`reset` on a fresh install now needs one panel mode change. Modes and owner bindings stored by 0.9.0 keep working.
- Add a small section registry (`owner_panel.register_section`) so stats and dossiers can show per-chat views in the panel.
- Verify the panel owner from any Business message seen in the chat since the gateway started, so `/transcribe` is no longer needed to unlock Manage Bot. A cached "not the owner" answer is dropped as soon as a new connection is seen.
- Never guess a chat's Business connection in the panel: sections get one only when history lists it for the chat or a message was seen on it.
- Show the owner panel in Russian to a Telegram client set to Russian (`ru*`) and in English otherwise. All panel strings live in one catalog in `owner_panel.py`; sections get `PanelContext.language_code` and add their own strings with `owner_panel.register_strings`, and a `section.<key>` string localizes a section's button.
- Always end the panel header with `chat <id>`, so a reply to a panel message tells the agent which chat it is about.
- Add `--format json` to `hermes telesec mode list` and `mode get`, with `mode`, `effective_mode`, and `source` (`stored`, `default`, or `disabled`).
- Add a neutral system prompt section (`telesec.reference`, Hermes 0.21.5+) that points the agent to the `mode` and `stats` JSON commands and the bundled skills, and tells it that a reply to a panel message is about the panel's `chat <id>`.
- Ignore the Business copies of the owner's own chat with the bot (the panel and the agent chat): they are no longer captured into history, transcribed, or counted, and history, stats, dossiers, and the panel skip such a chat that an older version captured. The bot's id comes from the running bot or from `TELEGRAM_BOT_TOKEN`.
- Add a 📊 Stats view to the owner panel: the one-chat `stats` report for the last 7, 30, or 90 days or all history, shown only in the owner's private chat with the bot. Read-only, no LLM, nothing stored ([#4](https://github.com/neoromantic/telesec/issues/4)).
- Add real Telegram screenshots of a transcribed voice note, the owner panel, and its Stats view, plus a 1200x600 catalog banner. State that Telesec is not affiliated with Nous Research.

## 0.9.0 - 2026-10-19

- Add per-chat transcription modes (`both` default, `own`, `peer`, `off`) for voice messages, round videos, and attached audio, keyed by Business owner and chat ([#7](https://github.com/neoromantic/telesec/issues/7)).
- Add the owner-only in-chat `/transcribe [both|own|peer|off|reset]` command. The bot edits the command into a short status, with a Business reply as fallback, and deletes the command and its status about 5 seconds later (needs the bot's "Delete messages" Business permission; without it the status stays and a hint is logged). Commands are kept out of history and away from the ordinary Hermes path, and a contact's, forwarded, or inline-bot `/transcribe` stays ordinary text. Only the plugin's own status line is kept out of history, not other bot-sent text that starts with `/transcribe`.
- Add `hermes telesec mode list|get|set|reset` with numeric or history-resolved chat selectors. Without `--owner`, a chat on a connection with a recorded owner (from `--connection`, or from history for a named chat or a numeric `chat_id` that one connection lists) uses that owner, and otherwise the one known owner is used only when every Business connection in history is recorded for it (or history has just one); the first write before any owner is known needs `--owner` or an in-chat `/transcribe <mode>` or `reset`. The CLI reports an unreadable or corrupt modes file instead of the default.
- Store modes in a private, atomically written `transcription-modes.json` that the gateway reloads on change without a restart. Entries hold no message text. An empty file reads as no modes. A corrupt or unreadable file keeps the last valid modes in the gateway, transient read errors are retried instead of cached, an in-chat `/transcribe <mode>` or `reset` never overwrites a corrupt file (a bare `/transcribe` shows the mode in effect), the gateway stops recording connection owners while it is corrupt or unreadable, and only an explicit CLI `set`/`reset --owner` backs it up and starts fresh.
- Check the mode once before any download or STT. Skipped media is claimed silently, and decisions are logged with a reason but no content. An unknown direction under `own`/`peer` transcribes as usual. A failed owner lookup uses the owner last recorded for that Business connection, for the mode, own/peer direction, and owner `/transcribe` authorization, and otherwise transcribes, so one owner's mode never applies to another owner's chat. With `TELESEC_DISABLE` set, `/transcribe` stays ordinary text.
- Share one Business owner/direction helper between transcription and history.
- Add `hermes telesec stats`: deterministic per-contact text-message counts, active days, sessions, latest activity, deletions, and missing-history notes over retained history, by Telegram send time, as text or JSON. No LLM and no new stored state.
- Add `TELESEC_TIMEZONE` as the default `--tz` for `stats`; `history stats` is now described as storage stats.
- Document stats in `docs/STATS.md` and add stats recipes to the bundled `history` skill.
- Add opt-in contact dossiers for private Business chats and an owner writing persona, built and refreshed incrementally by a gateway-only background worker through the host LLM facade (`TELESEC_DOSSIER_ENABLE=1`).
- Add `hermes telesec dossier` commands to inspect, trace facts to source messages, preview, correct, pin, remove, note, review changes, export, run and forget dossiers; owner edits survive refreshes.
- Validate model output deterministically (chat-local references, exact quotes of at most 200 characters, fixed sections, caps) and track edits, deletions and retention pruning as source changes; keep message content out of logs.
- Add the bundled `dossiers` skill and docs/DOSSIERS.md with a synthetic example.

## 0.8.0 - 2026-10-18

- Finish opt-in Business text history with contact/date/text search, aliases, edits, preserved deletions, and correction classification in canonical JSONL.
- Add the history CLI/skill, bounded verification/export, retention, and rebuildable contact catalog.
- Replace adapter monkeypatches and compatibility loaders with supported native Telegram handlers and standard package imports; avoid duplicate media downloads.
- Keep retry identities stable across owner-lookup recovery and pruning; move history I/O off the gateway event loop and preserve profile scope for delayed classification.
- Use Gemini 3.8 Flash cleanup without deprecated sampling parameters; extend CI to Python 3.14 and real PTB contracts.
- Add history guidance, assistant requirements/roadmap, and clearly labelled synthetic visual examples.
- Replace the brittle enriched semantic-fidelity validator with a minimal structural sanity check: publish the first non-empty candidate between 20% and 200% of source length, retry only catastrophic output once, and use raw STT only as the last fallback.

## 0.7.2 - 2026-08-16

- Reject locally omitted source clauses and truncated prefixes/suffixes even when aggregate word retention remains above the global floor.
- Canonicalize equivalent English negation forms such as `can not` and `can't` before polarity-loss checks.
- Treat missing paragraph breaks as a repairable structure-only signal; after the single repair pass, accept a lexically faithful candidate rather than discarding it as semantically lossy.

## 0.7.1 - 2026-08-16

- Make enriched cleanup fidelity-first: preserve every clause, aside, negation, relationship observation, explanation, and awkward or secondary thought instead of inferring and compressing a main point.
- Remove the half-length guidance and permit deletion only for isolated filler sounds, exact stutters, and exact duplicated fragments.
- Raise the enriched retention floor to 80%, preserve negation/polarity tokens alongside numbers, and fall back to raw STT when the feedback repair is still lossy.
- Increase cleanup completion headroom so provider reasoning cannot truncate the JSON transcript before the full edited text is returned.

## 0.7.0 - 2026-08-16

- Add Telegram Business `audio` and conservatively identified audio-document transcription while leaving voice/video-note processing unchanged.
- Claim every candidate attached audio before the normal agent path, including files rejected by metadata, duration, local probing, or speech-presence checks.
- Enforce bounded 300-second and 20 MiB defaults, reject unsafe message and authoritative `getFile` byte metadata before download, and verify every downloaded candidate's actual duration locally with `ffprobe`.
- Extract a bounded 10-second mono/16 kHz prefix with `ffmpeg`; require conservative lexical content (three-word default with continuous-script handling); reject degenerate/common no-speech hallucinations and explicitly title/performer-tagged music before full transcription; short files reuse the probe transcript.
- Normalize attached-audio containers unsupported by the host STT extension gate to transient mono/16 kHz WAV and recognize Unicode speech tokens beyond Latin/Cyrillic scripts.
- Delete the transient download, probe, and any normalized full-file WAV on every path; document that recognized vocals can produce a heuristic false positive.
- Extend the opt-in Business adapter auth bypass only to the same attached-audio candidates.
- Retry enriched cleanup once with exact validator feedback instead of immediately discarding the edit.
- Treat substantial shortening, including roughly half-length output, as a quality signal rather than automatic failure; after the retry, fall back to raw STT only for catastrophic candidates.
- Isolate cleanup environment variables in unit tests so live deployment overrides cannot corrupt default-behavior assertions.

## 0.6.2 - 2026-08-04

- Add opt-in `enriched` transcript editing with filler removal, mandatory paragraphing, list formatting, long-note titles, and a summary-rejection guard.
- Keep conservative copy editing as the default for existing installations.
- Normalize Telegram Business identifiers, direction, timestamps, relationships, and media before routing events through independently enabled modules.
- Add explicit handled/pass-through results, per-module LLM opt-in, stable retry identity, and failure isolation without adding persistence or provider clients.
- Run the existing voice/video-note transcriber as the first module on the shared non-agent pipeline while preserving its Business-scoped behavior.
- Attach fitting transcripts to outgoing Business voice/video-note captions without a duplicate reply.
- Preserve existing caption text and entities, and fall back to Business-scoped replies for incoming, long, expired, uncertain, or uneditable messages.
- Render transcript caption blocks and reply chunks with native `expandable_blockquote` entities using UTF-16 offsets.
- Keep long and emoji-heavy transcripts complete with UTF-16-safe chunking, and align caption/reply fallback by retrying the selected surface as plain text when Telegram rejects expandable entities.

## 0.6.1 - 2026-07-18

- Resolve the active Telegram adapter through Hermes's platform registry.
- Support Hermes 0.18.2's isolated `hermes_plugins.telegram_platform` loader while retaining the legacy import fallback.
- Test that the shim patches the registered adapter class rather than a duplicate legacy module.

## 0.6.0 - 2026-07-18

- Move the Telegram Business adapter compatibility into the persistent user plugin.
- Handle Business updates exposed through `effective_message` without mutating PTB update objects.
- Register round video notes with Hermes's media handler.
- Keep the auth exception opt-in and limited to real Business voice/video-note media.
- Remove the need to patch Hermes core during or after normal updates.

All notable changes to this project are documented here.

## [0.5.0] - 2026-07-17

### Changed

- Rebranded the public product, repository, and package metadata as **Telesec** / `telesec`.
- Positioned voice and video-note transcription with Business-scoped replies as the first currently shipped module rather than the product boundary.
- Updated installation examples, badges, source/homepage links, and release references to the canonical repository URL.

### Compatibility

- Kept the runtime plugin ID, config keys, update/remove commands, and cache paths unchanged during the 0.5.0 rebrand.
- Kept existing environment-variable namespaces during the 0.5.0 rebrand.
- Existing installations that use the old GitHub URL continue through GitHub's repository redirect; no reinstall is required.

## [0.4.1] - 2026-07-17

### Fixed

- Made standalone source imports and credential-free tests portable outside a full Hermes installation.
- Added the missing `pytest-asyncio` CI dependency so async tests run across supported Python versions.

## [0.4.0] - 2026-07-17

### Added

- Public, Git-installable Hermes Agent plugin distribution.
- Telegram Business voice-message and round-video-note interception through `pre_gateway_dispatch`.
- Host-configured STT delegation and optional host-owned LLM copy editing.
- Conservative cleanup validation with raw-transcript fallback.
- Duplicate-update suppression, Telegram-safe chunking, and `business_connection_id`-aware replies.
- Credential-free unit tests and multi-version Python CI.

### Security

- Treat transcript text as untrusted LLM input and instruct cleanup models not to follow embedded commands.
- Delete each transiently downloaded media file after the transcription attempt.
- Keep cleanup failures fail-safe: the raw transcript is posted rather than dropped.

[0.5.0]: https://github.com/neoromantic/telesec/releases/tag/v0.5.0
[0.4.1]: https://github.com/neoromantic/telesec/releases/tag/v0.4.1
[0.4.0]: https://github.com/neoromantic/telesec/releases/tag/v0.4.0
