# Product roadmap

The product connects readable Telegram voice messages with useful personal correspondence history. Planned assistance builds on that history and gives the owner explicit control over what gets sent.

## Implemented in 0.8.0

- Voice, round-video, and attached-audio transcription with optional cleanup and expandable caption/reply output.
- Opt-in text history with contact names, aliases, edits, preserved deletions, and correction classification.
- Bounded contact/date/text search, export, verification, retention, and a rebuildable contact catalog.
- Supported native Telegram handler registration in place of adapter monkeypatches.
- Gemini 3.8 Flash cleanup through Hermes's host-owned LLM interface, plus Python 3.11–3.14 and real PTB contract coverage.

## Implemented in 0.9.0

- [Transcription controls #7](https://github.com/neoromantic/telesec/issues/7): manual per-chat `both`, `own`, `peer`, and `off` modes for voice, round video, and attached audio. Owners set them with the in-chat `/transcribe` command, which the bot confirms and then deletes, or with `hermes telesec mode`. The gateway reloads changes without a restart.
- [Correspondence stats #4](https://github.com/neoromantic/telesec/issues/4), first slice: the deterministic [`stats` command](STATS.md).
- Opt-in contact dossiers and owner persona ([#17](https://github.com/neoromantic/telesec/issues/17)), built in the gateway background. [Dossier guide](DOSSIERS.md).

## Implemented in 0.10.0

- Renamed to **Telesec**, with automatic private data/config migration and Hermes Desktop settings.
- Business text never starts an agent turn unless `TELESEC_AGENT_ENABLE=1` is set.
- Owner panel: Telegram's "Manage Bot" button in a Business chat opens the bot's private chat with that chat's transcription mode as buttons, visible only to the owner. Other features add per-chat views with `owner_panel.register_section` and localize them through the panel's string catalog (English and Russian). The in-chat `/transcribe` command from 0.9.0 is removed: the panel is the only control inside Telegram.
- Stats in the owner panel ([#4](https://github.com/neoromantic/telesec/issues/4)): a 📊 Stats section with the one-chat `stats` report for the last 7, 30, or 90 days or all history. [Owner panel stats](STATS.md#in-telegram-owner-panel).

## Implemented in 0.11.0

- Dossiers in Telegram ([#17](https://github.com/neoromantic/telesec/issues/17)): the panel's Dossier section (rich, Russian or English, with pin, unpin, remove, refresh, the persona, and the on/off switch), and the owner's `/dossier` that posts a contact's dossier into the chat (the one in-chat control besides the panel). Dossiers are switched only in the panel or with `dossier enable`/`disable`; the old environment variable and Desktop setting are moved into that switch once. [Dossiers in Telegram](DOSSIERS.md#in-telegram).

## Implemented in 0.12.0

- Settings in the private chat with the bot: `/telesec` or the ⚙️ Settings button turns transcription, history, and dossiers on or off for all chats, and `hermes telesec settings` does the same from the terminal. The old environment variables and the Desktop `history_enable` setting are moved into Settings once. [Settings](../README.md#settings).
- Per-chat history and dossier controls: each chat's owner panel has **Save history** / **Don't save** and **Keep dossier** / **No dossier** rows in the style of transcription modes, and `hermes telesec chat` does the same from the terminal. Off pauses the chat and keeps what was saved. [Per-chat history and dossier](../README.md#per-chat-history-and-dossier).

## Implemented in 0.12.2

- Business voice messages, round videos, and audio are transcribed from every contact; `TELESEC_VOICE_BYPASS_AUTH` is removed.

- Stats redesign ([#4](https://github.com/neoromantic/telesec/issues/4)): the panel's 📊 Stats section is a compact report in Russian or English with bars and sparklines. The owner's in-chat `/stats` (all history, or `7d`, `30d`, `90d`) edits the command into a shareable card that the contact sees too, without owner-only details; the command and card never reach history. With `/dossier`, it is one of the two owner commands typed in a Business chat. [Owner panel stats](STATS.md#in-telegram-owner-panel), [in-chat `/stats`](STATS.md#in-a-business-chat-stats).
- Conversation metrics in `stats` ([#28](https://github.com/neoromantic/telesec/issues/28)): share, turns, reply times (median and p90), waiting, message length, edits, hours and weekdays, activity, streaks, the longest silence, and a comparison with the previous period. JSON `schema_version` 2. The stats time zone now defaults to the Hermes time zone. [Conversation metrics](STATS.md#conversation-metrics).

## Implemented in 0.13.0

- Stats as Telegram rich messages ([#4](https://github.com/neoromantic/telesec/issues/4)): the panel's 📊 Stats view and the in-chat `/stats` card have one balance bar, one table with a column for each side, emoji heat maps of when you talk, tiles, and a short footer. [Owner panel stats](STATS.md#in-telegram-owner-panel).
- Import a chat's past messages from a Telegram Desktop export with `hermes telesec history import`, so stats and new dossiers see the time before capture started. [Import past messages](HISTORY.md#import-past-messages).
- History retention in Settings: **Forever · 1 year · 90 days · 30 days**, also `hermes telesec settings set retention`, replacing `TELESEC_HISTORY_RETENTION_DAYS`. Settings also shows the dossier and voice cleanup models, read-only. [Settings](../README.md#settings).

## Implemented in 0.14.0

- History is switched per chat in Telegram, not by environment variables: the owner taps **Save history** or **Don't save**. `TELESEC_HISTORY_CONNECTIONS`, `TELESEC_HISTORY_CHATS`, and `TELESEC_HISTORY_CHAT_TYPES` are kept once as a stored rule that still keeps the same chats out until the owner turns one on or runs `hermes telesec chat scope clear --yes`; while the variables stay set they apply too, so going back to 0.13 keeps working. While a chat saves no history, **🗑 Erase saved history** deletes its saved history and its dossier after a confirmation; `hermes telesec history forget` does the same from the terminal, and `--all` erases everything. [Moving from the history environment variables](../README.md#moving-from-the-history-environment-variables), [erase one chat](HISTORY.md#erase-one-chat).

## Implemented in 0.15.0

- Dossiers as documents ([#17](https://github.com/neoromantic/telesec/issues/17)): each contact dossier is one short Markdown document that a model rewrites as the chat goes on, and the owner corrects it with a note. The persona is rebuilt from scratch from the owner's own messages and resets when a chat it read is erased. `/dossier` posted a fresh summary written from the chat's live messages only; it now posts the dossier itself (below). Dossiers use their own Hermes model task (Vercel AI Gateway → Google, zero data retention by default), with an optional message check (TypeSafe Jev) and optional fact extraction (Cloudflare), both off by default. [Dossier guide](DOSSIERS.md).

## Implemented, not released yet

- `/dossier` posts the contact's dossier again ([#17](https://github.com/neoromantic/telesec/issues/17)): the stored document without the owner note, refreshed once first when it is behind the chat or the note changed. The separate summary is gone. [`/dossier` in a chat](DOSSIERS.md#dossier-in-a-chat).

## Next

- Owner note in Telegram: a note button on the panel's Dossier screen, to write or clear the owner note without the CLI. Today notes are written with `hermes telesec dossier note`.
- Message check and fact extraction in Settings: one Settings line that shows and switches the dossier message check and fact extraction, which only the CLI switches today (`hermes telesec dossier gate`, `hermes telesec dossier clairic`).
- Measure fact extraction (Clairic): run the eval's `v2-clairic` arm once a Cloudflare token is available, on Russian and English chats. Keep it only if it halves date, amount, and contact-detail errors, gets at least 90% of Russian fields right, and adds less than $0.001 and 1.5 seconds (p95) per refresh; otherwise remove it and say why in the [dossier guide](DOSSIERS.md#cost-and-quality).
- Voice notes in stats: history first needs a separate, metadata-only capture change (send time, side, and duration; no audio). Stats then add a voice row to the panel and the card. Until then, stats count text messages only.

- Known limit: claimed panel taps and Business updates stop propagation before Hermes' group-99 dispatch counter, so the core "ingress healthy but deaf" warning can show a stale gap after quiet periods. The fix belongs in Hermes core (count dispatch in a group before every plugin group).

## Priorities and requirements

| Order | Work | Result |
|---|---|---|
| 1 | [History #2](https://github.com/neoromantic/telesec/issues/2) | Finish integration review and prove new capture, restart, search, and deletion behavior. |
| 2 | [Owner/contact dossiers #17](https://github.com/neoromantic/telesec/issues/17) | Implemented: opt-in [dossiers](DOSSIERS.md) for all private chats and an owner persona, as short documents that a model keeps current, with owner notes, forgetting, and `/dossier`, which shares the dossier with the contact. |
| 3 | [Contextual drafts #18](https://github.com/neoromantic/telesec/issues/18) | Private suggestions using conversation context and approved dossiers; owner Send / Edit / Discard. |
| 4 | [Automatic replies #19](https://github.com/neoromantic/telesec/issues/19) | Explicit per-chat activation, reply limits, pause, and owner handover. |

The [assistant design](ASSISTANT.md) explains the owner experience, modes, context boundaries, and delivery lifecycle. Each issue is a set of requirements rather than a prescribed storage framework.

Deferred from #17: using dossiers in drafts (#18) and automatic replies (#19), owner notes from Telegram (CLI only for now), voice notes themselves as a dossier source (only their transcripts are), and cross-contact persona leakage filters.

## Independent improvements

| Issue | First useful slice |
|---|---|
| [Raw-first transcripts #8](https://github.com/neoromantic/telesec/issues/8) | Show raw STT immediately, then edit the same caption/reply after cleanup; start with one output target. |
| [Transcription controls #7](https://github.com/neoromantic/telesec/issues/7) | Implemented in 0.9.0: manual per-chat `both`, `own`, `peer`, and `off`. Automatic peer detection remains a separate investigation. |
| [Scheduled sending #3](https://github.com/neoromantic/telesec/issues/3) | Recover the existing static-text send CLI; Hermes cron owns timing, with explicit targets and no ledger. |
| [Correspondence insights #4](https://github.com/neoromantic/telesec/issues/4) | First slice in 0.9.0: the deterministic [`stats` command](STATS.md) with per-contact counts, active days, sessions, latest activity, and deletions. The same one-chat report is in the [owner panel](STATS.md#in-telegram-owner-panel) since 0.10.0. Response-time and turn metrics [#28](https://github.com/neoromantic/telesec/issues/28) and the in-chat `/stats` card shipped in 0.12.2; 0.13.0 redraws the panel view and the card as rich messages. Remaining: opt-in owner notifications [#27](https://github.com/neoromantic/telesec/issues/27), an all-contacts overview in the panel [#29](https://github.com/neoromantic/telesec/issues/29), and voice notes in stats (after the metadata capture change above). |
| [Voice bursts #9](https://github.com/neoromantic/telesec/issues/9) | Opt-in same-sender grouping with ordering and explicit boundaries; avoid delaying every single note. |
| [Release and launch #10](https://github.com/neoromantic/telesec/issues/10) | Align versions, verify installation, refresh visual docs, and publish accurate release/demo material. |

Existing work should be recovered rather than reimplemented: [history PR #15](https://github.com/neoromantic/telesec/pull/15) and the [scheduled-send branch](https://github.com/neoromantic/telesec/tree/feat/issue-3-scheduled-business-messages). The old launch PRs [#14](https://github.com/neoromantic/telesec/pull/14) and [#16](https://github.com/neoromantic/telesec/pull/16) overlap; their presence does not imply launch readiness.

## Design constraints

Capture and deterministic queries do not use an LLM. Dossier generation, drafting, and automation have separate opt-ins. Reuse canonical JSONL and the contact catalog; add indexes or external services only when measurements justify them. Use Hermes-owned models, credentials, and scheduling facilities.

Telegram's Business scope and delivery permissions remain authoritative. Ordinary bot messages are a different channel; blocked Business sending must not fall back to them. Telegram-side deletion preserves an event in local history; actual archive removal and derived-data forgetting are explicit operations.
