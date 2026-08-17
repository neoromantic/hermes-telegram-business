# Changelog

## Unreleased

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

- Rebranded the public product, repository, and package metadata as **Hermes Telegram Business** / `hermes-telegram-business`.
- Positioned voice and video-note transcription with Business-scoped replies as the first currently shipped module rather than the product boundary.
- Updated installation examples, badges, source/homepage links, and release references to the canonical repository URL.

### Compatibility

- Retained `telegram-business-voice-transcriber` as the legacy-stable Hermes runtime plugin ID, including existing config keys, update/remove commands, and cache paths.
- Retained all existing `TG_BUSINESS_VOICE_*` environment-variable namespaces.
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

[0.5.0]: https://github.com/neoromantic/hermes-telegram-business/releases/tag/v0.5.0
[0.4.1]: https://github.com/neoromantic/hermes-telegram-business/releases/tag/v0.4.1
[0.4.0]: https://github.com/neoromantic/hermes-telegram-business/releases/tag/v0.4.0
