# Correspondence history

History records new Telegram Business text events without an agent turn or an LLM call. It is the source for [contact dossiers and the owner persona](DOSSIERS.md) and for future correspondence features; capturing history does not enable them. Dossiers have their own opt-in.

![Actual history CLI output from fictional Business events.](images/history-search.png)

The image uses synthetic data captured through the real history module and queried through its CLI. [Editable HTML source](images/src/history-search.html).

## Enable capture

History is off by default. Turn it on in [Settings](../README.md#settings): send `/telesec` in your private chat with the bot (or tap **⚙️ Settings** on any chat's panel) and tap **Save history**. Or from the terminal:

```bash
hermes telesec settings set history on
```

No gateway restart is needed: capture starts with the next update. **Don't save** or `hermes telesec settings set history off` stops capture and keeps what was stored; retention and delayed deletion classification keep running. How long history is kept is set in Settings too ([retention](#retention-and-removal)). Up to 0.11.0, `TELESEC_HISTORY_ENABLE=1` or the Desktop `history_enable` setting turned history on. On the first start after upgrading, an "on" there is stored in Settings once and the gateway log says so (a variable set to off over a Desktop "on" is stored as off); after that they are ignored. Remove them once you no longer plan to go back to 0.11.0 or older.

Capture includes every private chat Telegram delivers through the bot's Business connections, except chats set to [Don't save](#one-chat). Your own chat with the bot (where the owner panel and your agent chat live) is never captured: Telegram also delivers it as Business updates, but it is not a Business chat. The plugin knows it by the bot's id (from the running bot, or from the digits before `:` in `TELEGRAM_BOT_TOKEN`), and readers (history, stats, dossiers, the panel) also skip such a chat if an older version captured it. Groups, supergroups, and channels are never recorded. The plugin does not fetch conversations from before capture started, but you can [import a chat's earlier messages](#import-past-messages) from a Telegram Desktop export.

Text, edits, deletions, reply relationships, sender/direction, and available contact snapshots are recorded. Media bytes, media metadata, and captions are excluded. Missing original text is recorded as missing rather than reconstructed. The owner's in-chat [`/stats` command](STATS.md#in-a-business-chat-stats), its card or notice, and their edits and deletion are control traffic. They are not recorded while the gateway remembers their message IDs: 24 hours, in memory. After that, or after a gateway restart, an edit or deletion of the card is recorded as an edit or deletion of a message that is not in history.

## One chat

To stop history for one chat, tap **Manage Bot** in that chat and then **Don't save** in your private chat with the bot, or run:

```bash
hermes telesec chat set 991 --history off
hermes telesec chat get 991
hermes telesec chat list
```

**Save history** or `--history on` turns it back on; it is the default, so nothing is stored for it. While a chat is set to Don't save, its new messages, edits, and deletions are not recorded, and its dossier pauses too. What was saved before stays until you [erase it](#erase-one-chat), and retention and the size cap still prune it. Edits and deletions of older messages that arrive in the meantime are not recorded, so turning history back on leaves a gap. The setting is stored per owner in `chat-settings.json` and never touches another owner's chat with the same contact. The check fails closed: if the file was never readable, or the chat id is paused by some owner and the owner of the update's connection cannot be found, nothing is recorded. See [per-chat history and dossier](../README.md#per-chat-history-and-dossier).

Up to 0.12, `TELESEC_HISTORY_CONNECTIONS`, `TELESEC_HISTORY_CHATS`, and `TELESEC_HISTORY_CHAT_TYPES` chose the recorded chats. On the first 0.13 start with any of them set, the old filter is kept as a stored rule under `legacy_scope` in `chat-settings.json`, as 0.12 read it, and the variables are never read again; until it is stored, they keep applying. The rule works per connection and chat as before: a chat it keeps out, a new one too, saves no history, and its panel shows **Don't save** with "•" and `History: off by the old server scope`. To turn one chat on, tap **Save history** there or run `hermes telesec chat set 991 --history on`. `hermes telesec chat scope` shows the rule, and `hermes telesec chat scope clear --yes` drops it, so the chats it kept out start saving history, except those set to Don't save. [Moving from the history environment variables](../README.md#moving-from-the-history-environment-variables) has the details.

## Erase one chat

While history holds messages from a chat that saves nothing now, its panel shows **🗑 Erase saved history**: the chat is set to Don't save, the old server scope keeps it out, or history is off in Settings (then the button is under the `History: off in Settings` line). It asks first (`Erase 12 saved messages from this chat? Its dossier is deleted too. This cannot be undone.`), then deletes:

- every record of that chat on your Business connection, whatever wrote it (the whole chat directory),
- the chat's entry in the contact catalog (`contacts.json`),
- pending deletion classification for the chat,
- then the chat's dossier. A running dossier refresh stops first, and history goes before the dossier, so nothing can rebuild the dossier from it.

Stats then show no history for the chat. The chat keeps its setting, so nothing new is recorded. The button refuses while the chat saves history or while chat settings can't be read, so a stale or repeated tap erases nothing, and only the verified owner of the chat's connection sees it. Another owner's chat with the same contact is never touched. If the history erase fails, nothing is forgotten and the button stays, so you can try again. If only the dossier step fails, `hermes telesec dossier forget` finishes it. When the contact catalog can't be rebuilt after an erase, the erased chat still leaves it and the catalog is marked for a rebuild.

From the terminal:

```bash
hermes telesec history forget --chat 991
hermes telesec history forget --contact @alice --yes
hermes telesec history forget --all --yes
```

`forget` takes the same selectors as `show`. Without `--yes` it prints what it would erase and exits with code 1. With `--yes` it erases the chat's history and dossier the same way as the button. It also works while the chat still saves history; then it says new messages are saved again. `--all --yes` erases all saved history and every dossier, including the owner persona; Telegram has no button for that. Persona facts learned from an erased chat stay until you remove them in the persona view or run `dossier forget --persona`. A running gateway notices the erase the next time it reads the chat; no restart is needed.

## Find a conversation

```bash
hermes telesec history contacts --search alice --limit 20
hermes telesec history show --contact @alice --since 7d --limit 100
hermes telesec history search --contact @alice --text estimate --since 30d --limit 100
hermes telesec history search --text estimate --since 7d --limit 100
hermes telesec history export --contact @alice --format jsonl --limit 200
```

`show`, `search`, and `export` accept either `--contact` or numeric `--chat`; `--connection` disambiguates Business accounts. Duplicate contact names require an explicit choice. Observed names and usernames remain aliases when a contact changes their profile.

Use `--until` with an ISO timestamp to bound the end of a query. Both bounds are inclusive and apply to observation time, rather than the original message's timestamp. Search performs a Unicode-normalized, case-insensitive substring match. It does not use embeddings, interpret intent, or require `jq`.

The bundled `history` skill gives Hermes the same bounded contact-first recipes. Stored text is conversation data, not instructions for the agent.

## Import past messages

History starts when capture starts. To add a chat's earlier messages, export the chat from Telegram Desktop and import the file:

1. In Telegram Desktop, open the private chat, then **⋮ → Export chat history**. Choose the **JSON** format. Media is not needed, so you can untick photos, videos, voice messages, and files.
2. Copy `result.json` to the Hermes host, check what would happen, then import:

```bash
hermes telesec history import ~/result.json --chat 991 --dry-run
hermes telesec history import ~/result.json --chat 991
```

```text
Imported 1200 messages (1310 records) into chat 991 (connection business-123), sent 2025-11-02 to 2026-07-18 (UTC).
Skipped: 85 already saved, 40 newer than saved history, 30 voice, round video or audio (not transcribed), 64 other media, 3 service messages.
```

The output above is fictional. `--chat` is the contact's chat id (`hermes telesec history contacts` shows it), and it must match the export. `--dry-run` prints the same summary and writes nothing. The summary has counts and dates only, never message text. When the chat has no saved history yet, the command uses the bot's only Business connection, or asks for `--connection`; that connection must have saved history or a known owner.

What is imported:

- Text messages older than the chat's first captured record, from the contact (received) and from you (sent), with their Telegram send time and reply links. The time capture covers is never imported, even what capture did not save: media, Telesec commands, and anything sent while history was off or the chat was set to Don't save.
- An edited message becomes a message without text at its send time and an edit with the final text at the edit time. The export has no earlier text.
- Skipped and counted in the summary: messages already in history, voice messages, round videos, and audio (not transcribed), photos, files, stickers, and other media (captions too), service messages such as calls and pins, your `/stats` and `/dossier` commands, and posts by a third sender, such as a bot. Messages older than the retention period are skipped too.

Rules:

- History must be on in Settings, and the chat must not be set to Don't save. Otherwise the command refuses and writes nothing. Only a one-to-one chat export can be imported; group, channel, and full account exports are refused, and so is an export made by an account other than the connection's owner. Your private chat with the bot is never imported.
- Imported messages are never duplicated. Running the same file again adds nothing, and a newer export adds only messages that are not saved yet. What history already has wins.
- Imported records are ordinary history records with `"source":"import"`. They go into the chat's normal monthly files, placed by send time, and the contact catalog is updated, so stats, the 📊 panel, `history show`, and search see them. Retention, the size cap, and removal treat them like captured history.
- An existing dossier reads only messages newer than its window. To build it again with imported history, run `hermes telesec dossier forget` for it; the next cycle rebuilds it within the backfill window (90 days and 500 messages by default, see [dossiers](DOSSIERS.md)).

## Edits and deletions

```bash
hermes telesec history deletions --status unexplained --limit 20
```

Edits append the full updated text. Deletions append a tombstone while preserving earlier text. After the correction window, a deterministic classification is appended:

| Classification | Meaning |
|---|---|
| `likely_duplicate` | Strong normalized resend evidence. |
| `likely_correction` | A nearby, highly similar small edit. |
| `unexplained` | No strong replacement evidence. |
| `unclassifiable` | Original text or sender evidence was unavailable. |

These are observable text-similarity categories, not judgments about a person's intent. Delayed classification runs in the gateway and recovers overdue work at startup. Notifications about deletions are planned separately.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `TELESEC_HISTORY_CORRECTION_WINDOW` | `120` | Post-delete replacement window and classification delay, in seconds. |
| `TELESEC_HISTORY_NEARBY_BEFORE_SECONDS` | `15` | Pre-delete replacement window, in seconds. |
| `TELESEC_HISTORY_MAX_BYTES` | `1073741824` | Disk-safety limit for operators: above this total size the oldest closed months are pruned, whatever the retention in Settings. |

Retention is not a variable: it is set in [Settings](#retention-and-removal). `TELESEC_HISTORY_RETENTION_DAYS` is no longer read; on the first start a value other than `0` is stored once as the retention and the gateway log says so. A `settings set` that creates or repairs `settings.json` stores retention as forever, so the variable is not moved in after that.

An update without a chat type uses the type history already has for that chat; with none, it is not recorded. No member-count requests run on capture.

History has no time zone setting: it stores UTC timestamps. [Correspondence stats](STATS.md#time-and-time-zone) show them in the Hermes time zone (`HERMES_TIMEZONE`, or `timezone` in Hermes's `config.yaml`), else UTC, and `--tz` overrides it for one report.

## Storage and recovery

```text
$HERMES_HOME/data/telesec/history/
  <connection-key>/<chat-id>/YYYY-MM.jsonl
  contacts.json
```

JSONL is canonical. Events have durable identities and are appended under a lock with `fsync`; retries do not append the same event twice. An [import](#import-past-messages) writes its records under the same locks and keeps each monthly file in observation order: it rewrites a file that already has records through a temporary file and an atomic rename. For imported records the observation time is the Telegram send time (the edit time for an edit). The contact catalog is a small derived directory, not a second message database. Monthly partitions follow observation time. Contact queries select the relevant conversation and date partitions before streaming records.

```bash
hermes telesec history stats
hermes telesec history catalog
hermes telesec history catalog --rebuild
hermes telesec history verify
hermes telesec history verify --repair-tails
hermes telesec history maintain
```

`history stats` shows storage stats: records, bytes, and settings. For correspondence stats per contact (text messages received and sent, active days, sessions, and deletions by Telegram send time), use `hermes telesec stats`. See [Correspondence stats](STATS.md).

Read commands do not repair canonical files. `verify --repair-tails` explicitly repairs a torn final line; invalid interior records need investigation. Startup recovery and maintenance operate on persisted history, and the catalog can be rebuilt from retained JSONL. Query results and verification diagnostics are bounded. Human-readable output escapes terminal control characters; JSONL export retains raw text.

## Retention and removal

Telegram-side deletion does **not** erase the archive. The archive deliberately preserves the earlier text to make deletion inspection possible.

History is kept forever by default. To keep less, use the **Forever · 1 year · 90 days · 30 days** row in [Settings](../README.md#settings) (`/telesec` in your private chat with the bot). A shorter time asks first; a longer one applies at once. From the terminal:

```bash
hermes telesec settings set retention 90
hermes telesec settings set retention forever
hermes telesec settings --format json   # retention.days, 0 is forever
```

`settings set retention N` takes any number of days from 0 to 36500. The value is stored in `settings.json` and read by every maintenance run, so a change needs no restart. Maintenance (the cleanup) runs when the gateway starts, when any `hermes` command loads Telesec (such as `hermes telesec history show`), and at most once an hour when a Business update arrives, also while history is off. So a shorter retention can delete months at the next `hermes telesec` command, not only in the gateway.

Automatic retention and size pruning remove whole closed monthly partitions. Partitions follow observation time. A month is deleted once all of it is older than the retention: with 30 days, September goes at the first cleanup on or after October 31. The current month and partitions needed for pending deletion classification are preserved, so neither retention nor `TELESEC_HISTORY_MAX_BYTES` is a hard storage ceiling. The contact catalog is rebuilt from retained evidence after pruning.

Don't save for one chat stops capture but keeps what that chat already has. [Erase one chat](#erase-one-chat) deletes one chat's history and dossier; there is no command to erase single messages. `hermes telesec history forget --all --yes` erases all history and every dossier. To remove an entire local archive by hand, turn history off in Settings, stop the gateway, back up anything you need, and remove the intended profile-local history directory before restarting. Include any external backups or exports in your own removal process. Removing history by hand does not remove dossiers; use `hermes telesec dossier forget` for those ([storage and privacy](DOSSIERS.md#storage-and-privacy)). Future reply state must support scoped forgetting before it ships.

## Runtime boundary

The native Telegram observer records eligible Business text before ordinary Hermes handlers. It also receives deleted-message updates, which have no effective message. Capture does not change the ordinary Hermes authorization or agent routing of text messages. Turn history off in Settings to stop capturing new messages; use Hermes's normal configuration to control agent replies.

The owner's `/dossier` command (once the bot confirms the sender as the connection's owner) and the dossier the bot posts are never recorded. A `/dossier` whose owner cannot be confirmed is stored as ordinary text, and an owner's edit of a stored message into `/dossier` is recorded as an edit. Dossiers read history only through its read locks and never modify it. `dossier forget` deletes dossier data only and never touches history; [erasing a chat](#erase-one-chat) deletes both. When retention or size pruning removes a message a dossier cites, the dossier drops that source. A fact with no sources left becomes unsupported, or `source gone` if the owner pinned or corrected it ([source changes](DOSSIERS.md#source-changes)).

The separate voice module still deletes transient audio after processing. This version does not insert voice transcripts into the text archive automatically.
