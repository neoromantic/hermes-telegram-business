# Security policy

## Supported versions

Security fixes go into the latest release and `main`. Update with `hermes plugins update telegram-business-voice-transcriber` and restart the gateway. Older releases do not get backports.

## Reporting a vulnerability

Report privately through GitHub: open the [Security tab](https://github.com/neoromantic/hermes-telegram-business/security) and choose **Report a vulnerability**. Do not open a public issue for a security problem.

Please include:

- the plugin and Hermes versions (`hermes plugins list --user`, `hermes --version`);
- which feature is affected: transcription, transcription modes and the owner panel, history, stats, or dossiers;
- the smallest steps that reproduce it, with synthetic data.

The maintainer replies as soon as possible, agrees on a fix and a disclosure date with you, and credits you in the advisory unless you ask not to be named.

## Scope

In scope:

- Telegram Business updates the plugin claims: voice messages, round video notes, and attached audio.
- Owner checks: the owner panel behind Telegram's "Manage Bot" button, the in-chat `/transcribe` command, and the `hermes telegram-business` CLI.
- Business-scoped delivery: transcript replies and caption edits must stay on the same `business_connection_id` and chat.
- Local data the plugin writes under `$HERMES_HOME/data/telegram-business/`: transcription modes, opt-in history, and opt-in dossiers.
- Data the plugin sends to the configured STT backend and to the host model through `ctx.llm`.

Out of scope:

- Hermes core behavior. Report it to [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
- Telegram account or Business settings, and outages of STT or model providers.
- Changes made to a local copy of the plugin.

## What the plugin sends and stores

- **No telemetry.** The plugin sends no usage data, analytics, or crash reports anywhere.
- Media goes to the STT backend configured in Hermes, which may be local or external. The downloaded file is deleted after processing.
- Transcript cleanup and dossiers use the host-owned `ctx.llm` facade. The plugin has no credentials of its own. Dossiers are off until `HERMES_TELEGRAM_BUSINESS_DOSSIER_ENABLE=1` is set.
- History is off until `HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE=1` is set. It stays on the gateway host. See [retention and removal](docs/HISTORY.md#retention-and-removal) and [dossier storage](docs/DOSSIERS.md#storage-and-privacy).
- Logs record IDs, counts, and errors, never message text or transcripts.

## Keep reports clean

Never share, in a report or anywhere else in this repository:

- bot tokens, API keys, cookies, session strings, or auth headers;
- real Business connection, chat, user, or message IDs;
- real messages, transcripts, captions, audio, video, or screenshots of real chats;
- history or dossier files, provider logs, or full environment dumps.

Use synthetic values such as `business-demo`, `chat-123`, and `message-456`, and short made-up transcripts.
