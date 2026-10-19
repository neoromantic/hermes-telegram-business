# Telegram Business for Hermes

[![test](https://github.com/neoromantic/hermes-telegram-business/actions/workflows/test.yml/badge.svg)](https://github.com/neoromantic/hermes-telegram-business/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A Telegram Business integration for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Voice messages, round video notes, and conservatively recognized attached audio files become readable text without a full agent turn. Optional correspondence history preserves text, edits, and deletions for contact-based search.

[Product demonstration and setup](https://hermes.sergeybaus.com)

![A Telegram Business chat: a client's 0:34 voice message gets a reply with its transcript as an expandable quote, split into paragraphs and a dash list; the owner's own 5-second voice message gets its transcript appended to its caption as a quote and is marked edited.](docs/images/hermes-telegram-business@2x.png)

## Current module: voice transcription

1. A native Telegram handler intercepts Business `voice`, `video_note`, `audio`, and conservatively identified audio `document` updates before ordinary media processing; generic documents and video are consumed without transcription.
2. Requires a real Telegram Business connection and preserves its `business_connection_id`.
3. Returns `action: skip` so the ordinary auth/agent path does not process the media.
4. Downloads the media transiently and delegates speech recognition to Hermes's configured `transcribe_audio` backend. Attached files must have safe byte/duration metadata, are locally duration-checked when needed, and pass a short mono/16 kHz speech-presence probe before full transcription.
5. Optionally asks the host-owned `ctx.llm` facade for conservative proofreading or opt-in enrichment with ASR correction, filler cleanup, paragraphing, lists, and long-note titles.
6. Publishes the first structurally usable enriched candidate. Empty output or a catastrophic size change gets one feedback retry; raw STT is the last fallback when neither model result is usable.
7. For a short outgoing Business message, appends the transcript to the original voice/video-note caption as an expandable blockquote, retrying the same caption as plain text when Telegram rejects the new entity type.
8. Uses expandable Business-scoped replies for incoming, long, expired, or uneditable messages, with the equivalent plain-text retry.

Handled updates are identified from stable Telegram Business connection, chat, update/message, and relationship data, then suppressed in memory for 24 hours. A successful caption edit suppresses the separate transcript reply, so the chat never receives both forms. PTB `BadRequest` is treated as a definite Telegram-side rejection unless it is the recognized `message is not modified` or unsupported-entity case. Failed entity requests are not treated as delivered before their targeted plain-text retry, and ambiguous post-send transport/backend failures suppress the reply fallback unless remote state is verified. The plugin never runs a separate model provider client and never needs its own credentials.

## Per-chat transcription modes

Each Business chat has one mode that covers voice messages, round videos, and attached audio:

| Mode | Transcribes |
|---|---|
| `both` (default) | Your messages and the contact's. |
| `own` | Only your own messages. |
| `peer` | Only the contact's messages. |
| `off` | Nothing in this chat. |

The main control is the owner panel. In any chat where the Business bot is connected, Telegram shows you, the account owner, a "Manage Bot" button. It opens your private chat with the bot, and the bot answers there with that chat's panel:

```text
Owner panel: Casey (@casey)
Transcription: off
[both] [own] [peer] [• off]
[Reset to default]
[Refresh]
```

Tap a mode to set it, or Reset to return to the default; the panel message updates in place. Only you see the panel: it lives in your private chat with the bot and the contact sees nothing. Only a verified owner gets the panel. Anyone else's `/start bizChat…` is treated as an ordinary message, and a panel button pressed by anyone else gets a short "Not allowed" alert.

To confirm you are the owner, the bot checks the Business connections it knows: ones Telegram reported connecting since the gateway started (kept in memory only), ones where you sent `/transcribe` once, ones it already looked up the owner for, and, with history on, the connections in the history catalog. While the gateway knows no Business connection at all, Manage Bot answers with a hint to send `/transcribe` in that chat once (or, with transcription disabled, to turn on history capture). Once it knows one, the owner of a connection it does not know yet must send `/transcribe` in that chat once, which records the connection's owner, before Manage Bot opens the panel; until then their `/start` is handled as an ordinary message. Mode buttons work for the chat right away, even after a restart and before the contact writes again; modes are stored per owner, so your change never touches another owner's chat with the same contact. With `TG_BUSINESS_VOICE_TRANSCRIBER_DISABLE` set, the panel says so and shows no mode buttons. Other features add their own panel sections.

Panel taps and all Business message updates stop update propagation before Hermes' group-99 dispatch counter, so after a quiet period the core "Telegram ingress is healthy but deaf" warning can report a stale gap. The gap resets on reconnect and needs no action.

The fallback is an in-chat command. As the connected Business account owner, send it in the chat itself:

```text
/transcribe          show the current mode
/transcribe off      set both, own, peer, or off
/transcribe reset    return to the default (both)
```

The bot edits your command into a short status such as `/transcribe off → Transcription: off`, or sends a Business reply when the edit does not apply, and deletes the command and its status about 5 seconds later so they do not stay in the chat. Deleting needs the bot's "Delete messages" permission in Telegram Business > Chatbots; without it the status stays and the gateway log says so. Unknown arguments get a short usage status the same way. Only the account owner's command counts; a contact who sends `/transcribe`, or a forwarded or inline-bot `/transcribe`, is just ordinary text. Commands and their status edits are never written to history and never reach the ordinary Hermes path.

The same modes are available from the CLI:

```bash
hermes telegram-business mode list
hermes telegram-business mode get 991
hermes telegram-business mode set @alice off
hermes telegram-business mode reset 991
```

A chat is a numeric `chat_id` or, with history enabled, a `@username` or name resolved through the history contact catalog. Ambiguous names need the numeric `chat_id` or `--connection`. Without `--owner USER_ID`, a chat on a Business connection whose owner the modes file records belongs to that owner. The connection comes from `--connection`, from the history entry of a chat named by `@username` or name, or, for a numeric `chat_id`, from history when exactly one connection lists that chat. Otherwise the CLI uses the only known owner, where an owner is known once the modes file stores a mode for them or records them as a connection's owner. It does so only when history lists at most one Business connection, or only connections recorded for that owner, since a connection with no recorded owner may belong to another Business account; in every other case pass `--owner`. History does not record owner ids, so on a fresh install the first `set` or `reset` needs `--owner USER_ID`, or one in-chat `/transcribe <mode>` or `/transcribe reset` first (a bare `/transcribe` only shows the mode and records nothing); `list` and `get` report the default. `set` warns about an `--owner` it has not seen before. If the modes file cannot be read or is corrupt, the CLI reports an error instead of the default; only `set` or `reset` with `--owner` backs a corrupt file up and starts a new one.

Modes are stored per (owner, chat) in `$HERMES_HOME/data/telegram-business/transcription-modes.json` with `0600` permissions and atomic writes. Each entry holds only the mode, when and how (`chat` or `cli`; a panel change is stored as `chat` so 0.9.0 can still read it) it was set, and a display label for the chat. The file also records which owner each Business connection belongs to: an in-chat `/transcribe <mode>` or `/transcribe reset` records it, so does a panel change, and so does the gateway when it looks up the owner for media in a chat with a non-default mode. The gateway's own lookup never records a connection whose chats all use the default, and a recorded connection stays recorded after its chats return to the default. Telegram's report that the bot was connected is never recorded, since any Business account can connect the bot. It never holds message text or transcripts. The gateway re-reads the file when it changes, so a CLI change applies to the next message without a restart.

The mode is checked once, before any download, speech recognition, or cleanup. A skipped note is still consumed silently: nothing is posted and the ordinary Hermes path does not see it. Logs record the decision and its reason, never content. When something is uncertain the plugin errs toward transcribing:

- A missing modes file, or an empty one (zero bytes or only whitespace), means every chat uses `both`. If the file becomes corrupt or cannot be read, the gateway keeps the modes it last read successfully (`both` everywhere if it never read any) and logs it. An in-chat `/transcribe <mode>` or `/transcribe reset` never overwrites a corrupt file and answers with an error status, while a bare `/transcribe` shows the mode the gateway is applying. While the file is corrupt or unreadable, the gateway also stops recording connection owners. Only `set` or `reset` with `--owner` from the CLI backs a corrupt file up and starts a new one.
- Under `own` or `peer`, a message whose direction cannot be determined is transcribed as usual.
- If the Business owner lookup fails, the plugin uses the owner it last recorded for that Business connection, both to pick the mode (so `off` stays off) and to tell own from peer messages under `own`/`peer`. The owner's `/transcribe` also keeps working through that record; a message sent by the Business bot never counts as an owner command. A connection with no recorded owner falls back to transcribing, so one owner's mode never applies to another owner's chat with the same contact.

## Correspondence history

Add this to the active Hermes profile's `.env`, then restart the gateway to enable text history for private Business chats:

```bash
HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE=1
```

Restart the gateway, then search by contact:

```bash
hermes telegram-business history contacts --search alice --limit 20
hermes telegram-business history show --contact @alice --since 7d --limit 100
hermes telegram-business history search --contact @alice --text estimate --since 30d --limit 100
hermes telegram-business history deletions --status unexplained --limit 20
```

History captures new text messages, edits, and deletions as Telegram delivers them. It preserves deleted text and distinguishes likely corrections, duplicates, unexplained deletions, and missing originals. It does not import old conversations. Contact names and observed aliases are searchable; ambiguous names require an explicit choice.

Canonical storage is profile-local JSONL with a rebuildable contact catalog. Capture and search require no LLM. Media and captions are excluded from history in this version. [History configuration, retention, and CLI](docs/HISTORY.md).

## Correspondence stats

`stats` counts, per contact, text messages received and sent, active days, conversation sessions, latest activity, and deletions over retained history:

```bash
hermes telegram-business stats --since 30d
hermes telegram-business stats --contact @alice --since 2026-10-01 --tz Europe/Moscow
hermes telegram-business stats --contact @alice --format json
```

Messages are placed by Telegram send time. Only text messages are counted. The report needs no LLM, stores nothing new, and never prints message text. [Counting rules, options, and a synthetic example](docs/STATS.md).

## Contact dossiers (opt-in)

With history enabled, add `HERMES_TELEGRAM_BUSINESS_DOSSIER_ENABLE=1` to the profile's `.env` and restart the gateway. A gateway-only background worker then builds an evidence-backed dossier for every private Business contact, plus one owner writing persona, and refreshes them incrementally through the host `ctx.llm` facade. Every fact cites its source messages. You can trace, correct, pin, or remove facts, and owner edits survive refreshes.

```bash
hermes telegram-business dossier list
hermes telegram-business dossier show --contact @alice --fact f3
hermes telegram-business dossier correct --contact @alice --fact f3 --text "Prefers calls after 18:00."
```

Enabling dossiers sends private chat text to the configured model provider; history capture alone never does. Nothing is sent to contacts. [Dossiers, privacy, and a synthetic example](docs/DOSSIERS.md).

## Planned assistant

The first layer is implemented: opt-in [contact dossiers and an owner persona](docs/DOSSIERS.md) build editable, evidence-backed context from history. Contextual reply drafts with **Send / Edit / Discard**, plus explicitly enabled per-chat automatic replies with pause and human handover, are planned. Drafts and automatic replies are requirements, not runtime capabilities yet.

See the [roadmap](docs/ROADMAP.md) and [assistant design](docs/ASSISTANT.md). This project complements Nous Research's [official reply-drafting plugin](https://github.com/NousResearch/hermes-official-plugins/tree/main/telegram-business); it is a separate community integration.

## Requirements

- Hermes Agent with `ctx.register_telegram_handler`, CLI/skill registration, `pre_gateway_dispatch`, and `ctx.llm` support. Integration targets Hermes 0.22.2 and PTB 22.8.
- Python 3.11 or newer.
- A configured Telegram gateway with a Telegram Business connection.
- A working Hermes STT provider. Configure it with `hermes setup` or the [`stt` settings](https://hermes-agent.nousresearch.com/docs/user-guide/configuration).
- `ffprobe` and `ffmpeg` on `PATH` for attached-audio duration verification, prefix extraction, and normalization. Voice messages and video notes do not use these gates.

Telegram Business voice/video notes can originate from users outside the ordinary DM allowlist. Enable the plugin's narrowly scoped Business-media authorization bypass in the active profile's `.env` so its native handler can transcribe them:

```bash
HERMES_TELEGRAM_BUSINESS_VOICE_BYPASS_AUTH=1
```

The plugin registers native Telegram handlers in group `-100`, before ordinary Hermes handlers. After history capture and transcription routing, every `business_message`, `edited_business_message`, and `deleted_business_messages` update is consumed with `ApplicationHandlerStop`. Ordinary Business text from either the owner or a contact never becomes an agent turn, even with history or transcription disabled or after a history-write failure. Normal bot DMs, including the owner panel, keep their existing routing. The bypass above applies only to Business media transcription; it does not authorize ordinary bot messages.

Owners who deliberately want unhandled Business updates to continue into Hermes can set `HERMES_TELEGRAM_BUSINESS_AGENT_ENABLE=1` in the active profile's `.env` and restart the gateway. The ordinary core auth/agent rules then apply; handled media and owner controls remain consumed.

History has its own explicit enable switch and records eligible Business text updates independently of the ordinary DM allowlist. Telegram's selected Business-chat scope and the optional history filters determine what is captured.

## Compatibility and identity

- The public product and repository are **Telegram Business for Hermes** / `hermes-telegram-business`.
- The Hermes runtime plugin ID remains `telegram-business-voice-transcriber`. It is a legacy-stable internal ID used by existing install directories, enablement/config keys, update/remove commands, and cache paths.
- Existing environment-variable namespaces remain unchanged.
- Existing Git installations that retain the [old repository URL](https://github.com/neoromantic/hermes-telegram-business-voice-transcriber) continue to update through GitHub's redirect. No reinstall or config migration is required for this rebrand.

## Install

```bash
hermes plugins install neoromantic/hermes-telegram-business --enable
hermes gateway restart
```

Inspect the installation:

```bash
hermes plugins list --user
```

Plugins are profile-scoped. Set `HERMES_HOME` or use the appropriate Hermes profile before installing when the gateway does not use the default profile.

## Update and remove

```bash
hermes plugins update telegram-business-voice-transcriber
hermes gateway restart
```

```bash
hermes plugins remove telegram-business-voice-transcriber
hermes gateway restart
```

`hermes plugins update` uses the Git remote retained by the installer. Both the old redirected source URL and the canonical repository URL remain supported; do not copy the directory manually if you want supported updates. Hermes core updates and this plugin's updates are independent: the installed plugin persists across a core update, while the command above advances the plugin itself.

## Configuration

All plugin variables are optional.

| Variable | Default | Meaning |
|---|---:|---|
| `HERMES_TELEGRAM_BUSINESS_AGENT_ENABLE` | false | Allow unhandled Business updates into the ordinary Hermes auth/agent path. By default all Business message updates are consumed after history capture and transcription routing. |
| `TG_BUSINESS_VOICE_TRANSCRIBER_DISABLE` | false | Disable voice/audio transcription. `/transcribe` is then ordinary text. |
| `TG_BUSINESS_VOICE_TRANSCRIBER_SEND_ERRORS` | false | Reply with a short STT error notice. |
| `TG_BUSINESS_VOICE_CLEANUP_DISABLE` | false | Skip LLM cleanup and post raw STT text. |
| `TG_BUSINESS_VOICE_CLEANUP_PROVIDER` | `gemini` | Host provider requested for cleanup. |
| `TG_BUSINESS_VOICE_CLEANUP_MODEL` | `gemini-3.8-flash` | Host model requested for cleanup. |
| `TG_BUSINESS_VOICE_CLEANUP_STYLE` | `conservative` | Use `enriched` for filler removal, active editing, paragraph/list structure, and long-note titles. |
| `TG_BUSINESS_VOICE_CLEANUP_TIMEOUT` | `45` | Cleanup timeout in seconds. |
| `TG_BUSINESS_VOICE_CLEANUP_MIN_CHARS` | `81` | Minimum transcript length for cleanup. |
| `TG_BUSINESS_VOICE_CLEANUP_MIN_WORDS` | `1` | Minimum word count for cleanup. |
| `TG_BUSINESS_VOICE_TITLE_MIN_CHARS` | `700` | Add an enriched-mode title at this transcript length. |
| `TG_BUSINESS_VOICE_TITLE_MIN_WORDS` | `120` | Add an enriched-mode title at this word count. |
| `TG_BUSINESS_AUDIO_FILE_MAX_DURATION_SECONDS` | `300` | Maximum attached-audio duration; bounded to 1–3600 seconds. |
| `TG_BUSINESS_AUDIO_FILE_MAX_BYTES` | `20971520` | Maximum attached-audio size; bounded to 1024–104857600 bytes. Missing or zero metadata is rejected before download. |
| `TG_BUSINESS_AUDIO_FILE_PROBE_SECONDS` | `10` | Prefix duration extracted locally for speech probing; bounded to 1–60 seconds and capped at the configured maximum duration. |
| `TG_BUSINESS_AUDIO_FILE_MIN_WORDS` | `3` | Minimum lexical words in the probe transcript; bounded to 1–20. Continuous-script text uses an equivalent conservative letter threshold. |
| `HERMES_TELEGRAM_BUSINESS_DOSSIER_ENABLE` | false | Turn on [contact dossiers](docs/DOSSIERS.md) and the owner persona. |
| `HERMES_TELEGRAM_BUSINESS_DOSSIER_INTERVAL_MINUTES` | `30` | Pause between dossier cycles; bounded to 5–1440. |
| `HERMES_TELEGRAM_BUSINESS_DOSSIER_QUIET_MINUTES` | `10` | Minutes a chat must be quiet before a dossier refresh; bounded to 0–1440. |
| `HERMES_TELEGRAM_BUSINESS_DOSSIER_BACKFILL_DAYS` | `90` | First-pass and tracking window for dossiers, in days; bounded to 1–3650. |
| `HERMES_TELEGRAM_BUSINESS_DOSSIER_BACKFILL_MESSAGES` | `500` | First-pass message cap per contact (for the persona, across all chats); bounded to 10–5000. |
| `HERMES_TELEGRAM_BUSINESS_DOSSIER_MAX_CALLS_PER_MINUTE` | `20` | Dossier model calls per minute; bounded to 1–600. |
| `HERMES_TELEGRAM_BUSINESS_DOSSIER_PROVIDER` | cleanup provider | Host provider for dossiers; falls back to `TG_BUSINESS_VOICE_CLEANUP_PROVIDER`, then `gemini`. |
| `HERMES_TELEGRAM_BUSINESS_DOSSIER_MODEL` | cleanup model | Host model for dossiers; falls back to `TG_BUSINESS_VOICE_CLEANUP_MODEL`, then `gemini-3.8-flash`. |

Boolean values accept `1`, `true`, `yes`, or `on` (case-insensitive).

Attached-audio probing is deliberately a conservative speech-presence heuristic, not a perfect content classifier. Empty/failed STT, degenerate text, common no-speech hallucinations, or too little lexical content suppresses full transcription. Telegram audio tracks carrying both title and performer tags are treated as music and skipped. Untagged music with clearly recognized vocals can still be a false positive and proceed to full STT. Files no longer than the probe window reuse the successful probe transcript rather than making a duplicate STT call.

### LLM trust gate

Cleanup uses Hermes's host-owned `ctx.llm`; no provider key belongs in this repository or in plugin-specific files. Explicit provider/model overrides are fail-closed in Hermes. Permit only the provider and model you intend to use:

```yaml
plugins:
  entries:
    telegram-business-voice-transcriber:
      llm:
        allow_provider_override: true
        allowed_providers: [gemini]
        allow_model_override: true
        allowed_models: [gemini-3.8-flash]
```

The default is [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash). Cleanup does not pass sampling parameters, following [current Gemini guidance](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.8). Select another model through `TG_BUSINESS_VOICE_CLEANUP_MODEL` and update the allowlist to match; there is no automatic provider/model switch.

If the trust gate, provider, or model is unavailable, the plugin logs the cleanup failure and posts the raw STT transcript. To use different environment values, update the allowlists to match. To avoid any cleanup LLM call, set `TG_BUSINESS_VOICE_CLEANUP_DISABLE=1`.

Contact dossiers use the same facade and allowlist. By default they request the cleanup provider and model, so the allowlist above covers them; if you set `HERMES_TELEGRAM_BUSINESS_DOSSIER_PROVIDER` or `HERMES_TELEGRAM_BUSINESS_DOSSIER_MODEL`, allow those values too. A trust-gate denial stops the dossier cycle, and `hermes telegram-business dossier status` reports it. `TG_BUSINESS_VOICE_CLEANUP_DISABLE` does not affect dossiers; they make model calls only while `HERMES_TELEGRAM_BUSINESS_DOSSIER_ENABLE` is set.

`conservative` preserves conversational wording and accepts almost exclusively punctuation, paragraphing, and obvious ASR fixes. `enriched` actively edits ASR errors and filler, structures topics into paragraphs or real enumerations into lists, and adds a short title to long notes. Its prompt asks the model to preserve the complete meaning, but the runtime deliberately does not attempt token-level semantic proof: any non-empty result between 20% and 200% of the source word count is usable. Output outside that broad structural range gets one feedback retry; raw STT is used only when both candidates are structurally unusable or the cleanup call fails.

## Current module architecture

The plugin registers native Telegram handlers, a `pre_gateway_dispatch` hook, the `telegram-business` CLI, and the `history` and `dossiers` skills. Business text observations are persisted before normal dispatch; supported media is consumed directly before Hermes downloads it. The voice pipeline normalizes Telegram Business updates before its module sees them. The immutable event includes stable identity, Business connection, chat/user/message/update identifiers, known direction, message/edit timestamps, reply/edit/delete relationships, and provider-neutral media metadata. Direction remains `unknown` when Telegram supplies no outgoing marker; the voice module retains its existing asynchronous Business-owner lookup before editing a caption.

Source files:

- `__init__.py`: registration, the Telegram handlers and dispatch hook, and the voice pipeline.
- `history_support.py`: history capture and storage, the contact catalog, and the `history` CLI.
- `dossier_support.py`: the opt-in dossier worker, model calls, output validation, and dossier storage.
- `dossier_cli.py`: the `dossier` CLI.

Modules are a small ordered tuple. Each module has its own enable check, declares whether it may receive the host-owned LLM facade, and explicitly returns `handled` or `pass_through`. A pass-through result tries the next module; the native handler consumes the remaining Business update unless agent routing is explicitly enabled. A handled result returns `action: skip` immediately and schedules any slow work outside gateway dispatch:

```text
Telegram gateway event
  -> normalize Telegram Business identity and relationships
  -> enabled modules in order
       -> pass_through: try next module
       -> handled: duplicate guard + action: skip (no agent turn)
  -> unhandled Business update: stop propagation unless agent routing is enabled
  -> asynchronous module work
  -> voice/video note: unchanged transient download -> full STT
  -> attached audio: metadata gate -> authoritative getFile-size gate -> transient download -> local ffprobe verification -> ffmpeg prefix probe
  -> unsupported attached-audio container: transient mono/16 kHz WAV normalization
  -> Hermes transcribe_audio (configured host STT; probe first for attached files)
  -> optional ctx.llm structured cleanup
  -> minimal structural sanity check / one feedback retry / raw last fallback
  -> short outgoing message: edit_message_caption(..., caption_entities=[expandable], business_connection_id=...)
  -> otherwise: send_message(..., entities=[expandable], business_connection_id=...)
```

Outgoing direction is checked against the owner returned by Telegram's `getBusinessConnection`; `sender_business_bot` is also accepted as an explicit outgoing signal. Caption text is plain text and limited conservatively to Telegram's 1024 UTF-16 code-unit ceiling. An existing caption is retained unchanged at the start, including its entities, then separated from the transcript by a blank line. A UTF-16-positioned `expandable_blockquote` entity covers only the appended transcript block.

Incoming messages, transcripts that do not fit in one caption, messages outside Telegram's 48-hour Business edit window, uncertain direction, and definite caption-edit rejection, including generic PTB `BadRequest`, use the existing reply path. The first response chunk replies to the original message; continuation chunks use the same Business connection. Caption and reply surfaces follow the same capability policy: use `expandable_blockquote` first, then retry the selected surface once as plain text only after a recognized unsupported-entity rejection. Ambiguous caption-edit transport/backend failures after Telegram may already have seen the request suppress the separate reply instead of risking duplicate transcript delivery.

## Privacy and security

- Voice/video/audio data is written only to the active Hermes profile's cache while it is processed. The newly created download, attached-audio probe, and any normalized full-file WAV are deleted in a `finally` block after success or failure.
- The configured STT backend receives the media. Depending on your Hermes configuration, that backend may be local or external.
- When cleanup is enabled, the configured host LLM receives the transcript. The system prompt treats transcript contents as untrusted data and forbids following embedded instructions.
- The plugin does not log transcript text. Operational logs include media type, message/chat identifiers, character counts, and errors.
- Error replies are disabled by default. When enabled, the first line of an exception may be sent to the Business chat.
- Voice transcription uses an in-process duplicate key for 24 hours; it resets with the gateway. History uses durable event identities in its JSONL log.
- History is disabled until explicitly enabled. Once enabled, retained text includes earlier edits and deleted messages. Telegram deletion is not erasure of the local archive; [retention and removal](docs/HISTORY.md#retention-and-removal) are separate controls.
- Contact dossiers are disabled until explicitly enabled. Once enabled, private chat text from the backfill window and later messages goes to the configured host model; groups are never processed. Dossiers keep derived facts, quotes of at most 200 characters, and short message fingerprints, not a second archive. Their logs carry counts, IDs, and error types only. `dossier forget` deletes a dossier and its owner edits without touching history ([storage and privacy](docs/DOSSIERS.md#storage-and-privacy)).
- Existing Hermes/Telegram media caches are outside this plugin's ownership and are never scanned or deleted.

## Failure behavior

- Non-Telegram and non-Business events pass through untouched. Generic documents and video in Business chats are consumed without transcription unless agent routing is explicitly enabled.
- Media skipped by a chat's [transcription mode](#per-chat-transcription-modes) is claimed before download and posts nothing. A modes file that was never readable, unknown direction under `own`/`peer`, or a failed owner lookup on a connection with no recorded owner transcribe as usual.
- Disabled and pass-through modules do not consume the event's duplicate identity.
- A module enable/route exception is logged and contained, and later modules still receive the event.
- Background module exceptions are contained and cannot crash the gateway.
- Missing bot or Business connection data stops processing without invoking an agent.
- STT failure sends nothing unless error replies are enabled.
- Empty STT output sends nothing.
- Attached audio with missing/zero/oversize byte metadata, excessive known duration, unknown local duration, probe extraction/STT failure, or no meaningful probe speech is silently suppressed after being claimed; it never invokes the agent path.
- An initial cleanup call that times out or is denied by the trust gate falls back to raw STT. In `enriched` mode, empty output or a catastrophic size change gets one feedback retry. Ordinary shortening, semantic edits, missing numbers, changed negations, and absent paragraph breaks are not validator failures; the first structurally usable enhancement is published.
- Caption direction checks, length checks, edit-window checks, and definite caption-edit rejections fall back to a separate expandable transcript reply.
- A recognized unsupported-entity response retries the selected caption or reply surface once without the new entity; PTB `BadRequest` counts as a definite caption rejection unless it is the recognized not-modified or unsupported-entity case, while `TimedOut`, other `NetworkError` failures, and unrelated exceptions suppress the reply fallback to avoid duplicates.
- A successful caption edit never also sends a transcript reply.

## Testing

Tests cover the history CLI and disk recovery, transcript behavior, and real PTB handler/HTTP contracts with simulated Telegram responses. They need no Telegram token, provider credential, network call, or running gateway.

```bash
python -m pip install pytest pytest-asyncio pyyaml python-telegram-bot==22.8
python -m pytest -q
```

Live Telegram client rendering and provider cleanup quality require a separate configured smoke test. The [verification guide](docs/VERIFY.md) records the boundary.

## License

[MIT](LICENSE)
