# Security policy

## Supported versions

Security fixes go into the latest release and `main`. Update with `hermes plugins update telesec` and restart the gateway. Older releases do not get backports.

## Reporting a vulnerability

Report privately through GitHub: open the [Security tab](https://github.com/neoromantic/telesec/security) and choose **Report a vulnerability**. Do not open a public issue for a security problem.

Please include:

- the plugin and Hermes versions (`hermes plugins list --user`, `hermes --version`);
- which feature is affected: transcription, transcription modes and the owner panel, history, stats, or dossiers;
- the smallest steps that reproduce it, with synthetic data.

The maintainer replies as soon as possible, agrees on a fix and a disclosure date with you, and credits you in the advisory unless you ask not to be named.

## Scope

In scope:

- Telegram Business updates the plugin claims: voice messages, round video notes, attached audio, and the owner's `/dossier` command.
- Owner checks: the owner panel behind Telegram's "Manage Bot" button (its dossier switch and persona only for an owner of every known Business connection), the owner's in-chat `/dossier` command, and the `hermes telesec` CLI.
- Business-scoped delivery: transcript replies, caption edits, and the dossier posted by the owner's `/dossier` must stay on the same `business_connection_id` and chat. That post is the contact view only: no owner notes, no quotes or current text of deleted or edited messages (fact texts can still reflect such a message: deletions never trigger a rewrite, and pinned or corrected facts are never rewritten), never the persona. When `/dossier` posts nothing, the reason goes only to the owner's private chat with the bot.
- Local data the plugin writes under `$HERMES_HOME/data/telesec/`: transcription modes, opt-in history, and opt-in dossiers.
- Data the plugin sends to the configured STT backend and to the host model through `ctx.llm`.

Out of scope:

- Hermes core behavior. Report it to [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
- Telegram account or Business settings, and outages of STT or model providers.
- Changes made to a local copy of the plugin.

## What the plugin sends and stores

- **No telemetry.** The plugin sends no usage data, analytics, or crash reports anywhere.
- Media goes to the STT backend configured in Hermes, which may be local or external. The downloaded file is deleted after processing.
- Transcript cleanup and dossiers use the host-owned `ctx.llm` facade. The plugin has no credentials of its own. Dossiers are off until turned on in the owner panel's Dossier section or with `hermes telesec dossier enable`; the switch is stored in `$HERMES_HOME/data/telesec/dossiers/settings.json`. No environment variable or Desktop setting turns them on.
- History is off until `TELESEC_HISTORY_ENABLE=1` is set. It stays on the gateway host. See [retention and removal](docs/HISTORY.md#retention-and-removal) and [dossier storage](docs/DOSSIERS.md#storage-and-privacy).
- Logs record IDs, counts, and errors, never message text or transcripts.

## Keep reports clean

Never share, in a report or anywhere else in this repository:

- bot tokens, API keys, cookies, session strings, or auth headers;
- real Business connection, chat, user, or message IDs;
- real messages, transcripts, captions, audio, video, or screenshots of real chats;
- history or dossier files, provider logs, or full environment dumps.

Use synthetic values such as `business-demo`, `chat-123`, and `message-456`, and short made-up transcripts.
