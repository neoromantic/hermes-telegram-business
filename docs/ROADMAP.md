# Product roadmap

The product connects readable Telegram voice messages with useful personal correspondence history. Planned assistance builds on that history and gives the owner explicit control over what gets sent.

## Implemented in 0.8.0

- Voice, round-video, and attached-audio transcription with optional cleanup and expandable caption/reply output.
- Opt-in text history with contact names, aliases, edits, preserved deletions, and correction classification.
- Bounded contact/date/text search, export, verification, retention, and a rebuildable contact catalog.
- Supported native Telegram handler registration in place of adapter monkeypatches.
- Gemini 3.8 Flash cleanup through Hermes's host-owned LLM interface, plus Python 3.11–3.14 and real PTB contract coverage.

## Implemented in 0.9.0

- [Transcription controls #7](https://github.com/neoromantic/hermes-telegram-business/issues/7): manual per-chat `both`, `own`, `peer`, and `off` modes for voice, round video, and attached audio. Owners set them with the in-chat `/transcribe` command, which the bot confirms and then deletes, or with `hermes telegram-business mode`. The gateway reloads changes without a restart.
- [Correspondence stats #4](https://github.com/neoromantic/hermes-telegram-business/issues/4), first slice: the deterministic [`stats` command](STATS.md).
- Opt-in contact dossiers and owner persona ([#17](https://github.com/neoromantic/hermes-telegram-business/issues/17)), built in the gateway background. [Dossier guide](DOSSIERS.md).

## Unreleased

- Owner panel: Telegram's "Manage Bot" button in a Business chat opens the bot's private chat with that chat's transcription mode as buttons, visible only to the owner. Other features add per-chat views with `owner_panel.register_section` and localize them through the panel's string catalog (English and Russian). The in-chat `/transcribe` command from 0.9.0 is removed: the panel is the only control inside Telegram.

## Next

- Stats ([#4](https://github.com/neoromantic/hermes-telegram-business/issues/4)) and dossiers ([#17](https://github.com/neoromantic/hermes-telegram-business/issues/17)) as owner panel sections, so the owner sees them per chat without the contact seeing anything.
- Known limit: claimed panel taps and Business updates stop propagation before Hermes' group-99 dispatch counter, so the core "ingress healthy but deaf" warning can show a stale gap after quiet periods. The fix belongs in Hermes core (count dispatch in a group before every plugin group).

## Priorities and requirements

| Order | Work | Result |
|---|---|---|
| 1 | [History #2](https://github.com/neoromantic/hermes-telegram-business/issues/2) | Finish integration review and prove new capture, restart, search, and deletion behavior. |
| 2 | [Owner/contact dossiers #17](https://github.com/neoromantic/hermes-telegram-business/issues/17) | First slice implemented: opt-in [dossiers](DOSSIERS.md) for all private chats and an owner persona, with evidence, corrections, freshness, and forgetting. |
| 3 | [Contextual drafts #18](https://github.com/neoromantic/hermes-telegram-business/issues/18) | Private suggestions using conversation context and approved dossiers; owner Send / Edit / Discard. |
| 4 | [Automatic replies #19](https://github.com/neoromantic/hermes-telegram-business/issues/19) | Explicit per-chat activation, reply limits, pause, and owner handover. |

The [assistant design](ASSISTANT.md) explains the owner experience, modes, context boundaries, and delivery lifecycle. Each issue is a set of requirements rather than a prescribed storage framework.

Deferred from the #17 first slice: using dossiers in drafts (#18) and automatic replies (#19), a Telegram-side dossier UI, voice transcripts as a dossier source, per-contact exclusion, and cross-contact persona leakage filters.

## Independent improvements

| Issue | First useful slice |
|---|---|
| [Raw-first transcripts #8](https://github.com/neoromantic/hermes-telegram-business/issues/8) | Show raw STT immediately, then edit the same caption/reply after cleanup; start with one output target. |
| [Transcription controls #7](https://github.com/neoromantic/hermes-telegram-business/issues/7) | Implemented in 0.9.0: manual per-chat `both`, `own`, `peer`, and `off`. Automatic peer detection remains a separate investigation. |
| [Scheduled sending #3](https://github.com/neoromantic/hermes-telegram-business/issues/3) | Recover the existing static-text send CLI; Hermes cron owns timing, with explicit targets and no ledger. |
| [Correspondence insights #4](https://github.com/neoromantic/hermes-telegram-business/issues/4) | First slice in 0.9.0: the deterministic [`stats` command](STATS.md) with per-contact counts, active days, sessions, latest activity, and deletions. Remaining: in-chat stats for the owner, response-time and turn metrics, then opt-in owner notifications for unexplained deletions and overdue incoming turns. |
| [Voice bursts #9](https://github.com/neoromantic/hermes-telegram-business/issues/9) | Opt-in same-sender grouping with ordering and explicit boundaries; avoid delaying every single note. |
| [Release and launch #10](https://github.com/neoromantic/hermes-telegram-business/issues/10) | Align versions, verify installation, refresh visual docs, and publish accurate release/demo material. |

Existing work should be recovered rather than reimplemented: [history PR #15](https://github.com/neoromantic/hermes-telegram-business/pull/15) and the [scheduled-send branch](https://github.com/neoromantic/hermes-telegram-business/tree/feat/issue-3-scheduled-business-messages). The old launch PRs [#14](https://github.com/neoromantic/hermes-telegram-business/pull/14) and [#16](https://github.com/neoromantic/hermes-telegram-business/pull/16) overlap; their presence does not imply launch readiness.

## Design constraints

Capture and deterministic queries do not use an LLM. Dossier generation, drafting, and automation have separate opt-ins. Reuse canonical JSONL and the contact catalog; add indexes or external services only when measurements justify them. Use Hermes-owned models, credentials, and scheduling facilities.

Telegram's Business scope and delivery permissions remain authoritative. Ordinary bot messages are a different channel; blocked Business sending must not fall back to them. Telegram-side deletion preserves an event in local history; actual archive removal and derived-data forgetting are explicit operations.

Keep the public repository install source explicit and the runtime plugin ID `telegram-business-voice-transcriber` stable.
