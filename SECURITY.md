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

- Telegram Business updates the plugin claims: voice messages, round video notes, attached audio, and the owner's `/dossier` and `/stats` commands.
- Owner checks: the owner panel behind Telegram's "Manage Bot" button (its dossier switch and persona only for an owner of every known Business connection), the owner's in-chat `/dossier` and `/stats` commands, and the `hermes telesec` CLI.
- Business-scoped delivery: transcript replies, caption edits, the summary posted by the owner's `/dossier`, and the stats card or notice from the owner's `/stats` (an edit of the command, or a new message when the edit fails, and the deletion of the command and notice) must stay on the same `business_connection_id` and chat. The `/dossier` post is a clean-room summary: it is written at post time by a separate model call from the chat's live messages only, never from the private dossier, its previous version, the owner note, the persona, another chat, deleted or replaced messages, or the text before an edit, and nothing is posted when an input message is deleted or edited meanwhile ([guarantees and limits](docs/DOSSIERS.md#dossier-in-a-chat)). When `/dossier` posts nothing, the reason goes only to the owner's private chat with the bot. The stats card has counts and times only: no message text, no deletions, edits, waiting, or p90.
- Local data the plugin writes under `$HERMES_HOME/data/telesec/`: transcription modes, opt-in history, and opt-in dossiers.
- Data the plugin sends to the configured STT backend, to the host model through `ctx.llm`, and, when the owner turns them on, to the dossier message check and fact extraction.

Out of scope:

- Hermes core behavior. Report it to [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
- Telegram account or Business settings, and outages of STT or model providers.
- Changes made to a local copy of the plugin.

## What the plugin sends and stores

- **No telemetry.** The plugin sends no usage data, analytics, or crash reports anywhere.
- Media goes to the STT backend configured in Hermes, which may be local or external. The downloaded file is deleted after processing.
- Transcript cleanup and dossiers use the host-owned `ctx.llm` facade. Dossiers are off until the owner turns them on in Settings or with `hermes telesec settings set dossiers on` (or `dossier enable`), and they pause while history is off; the switch is stored in `$HERMES_HOME/data/telesec/dossiers/settings.json`. No environment variable or Desktop setting turns them on.
- **Dossier processors.** Once dossiers are on, private chat text, owner notes, and dossier documents go to the `telesec_dossier` model task: by default Vercel AI Gateway, which passes them to Google with zero data retention. The owner can choose another model with `hermes model`. Two optional steps are off by default: the message check sends the current dossier and new messages to TypeSafe Jev through the same gateway, with zero data retention, and only while the dossier model is on the gateway; fact extraction sends new messages to Cloudflare Workers AI (Clairic). The owner note is never sent to either, but note-derived text in the document can reach TypeSafe. See [dossier model](docs/DOSSIERS.md#model).
- **Main-model fallback.** If the dossier model fails, Hermes may retry the same input with the task's fallback chain and then with the main model. Telesec cannot turn this off. It records which model answered, and `hermes telesec dossier status` flags an answer from another model.
- **Keys.** The plugin keeps no credential store and reads no credential files. Only the optional message check and fact extraction call a model directly, over `https` only, without following redirects, with a cap on the response size. They read `AI_GATEWAY_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, and `CLOUDFLARE_API_TOKEN` through the Hermes secret scope of the active profile when they make a call. Keys are never stored, logged, or put into an error message, and the Cloudflare request goes to a fixed host.
- History is off until the owner turns it on in Settings (`/telesec` in the private chat with the bot) or with `hermes telesec settings set history on`. It stays on the gateway host. See [retention and removal](docs/HISTORY.md#retention-and-removal) and [dossier storage](docs/DOSSIERS.md#storage-and-privacy).
- Logs record IDs, counts, and errors, never message text or transcripts. Dossier logs carry only counts, timings, codes, error type names, and a short hash of the chat: never message, document, or note text, names, raw ids, model output, request bodies, URLs, or keys.

## Keep reports clean

Never share, in a report or anywhere else in this repository:

- bot tokens, API keys, cookies, session strings, or auth headers;
- real Business connection, chat, user, or message IDs;
- real messages, transcripts, captions, audio, video, or screenshots of real chats;
- history or dossier files, provider logs, or full environment dumps.

Use synthetic values such as `business-demo`, `chat-123`, and `message-456`, and short made-up transcripts.
