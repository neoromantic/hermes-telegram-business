# Correspondence history

History records new Telegram Business text events without an agent turn or an LLM call. It is the source for future persona and correspondence features; those derived features are not enabled by capturing history.

![Actual history CLI output from fictional Business events.](images/history-search.png)

The image uses synthetic data captured through the real history module and queried through its CLI. [Editable HTML source](images/src/history-search.html).

## Enable capture

Add this to the active Hermes profile's `.env`, then run `hermes gateway restart`:

```dotenv
HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE=1
```

By default, capture includes private chats Telegram delivers through the bot's Business connections. Connection and chat filters are optional. Groups, supergroups, and channels require explicit configuration. The plugin does not retrieve conversations from before capture started.

Text, edits, deletions, reply relationships, sender/direction, and available contact snapshots are recorded. Media bytes, media metadata, and captions are excluded. Missing original text is recorded as missing rather than reconstructed.

## Find a conversation

```bash
hermes telegram-business history contacts --search alice --limit 20
hermes telegram-business history show --contact @alice --since 7d --limit 100
hermes telegram-business history search --contact @alice --text estimate --since 30d --limit 100
hermes telegram-business history search --text estimate --since 7d --limit 100
hermes telegram-business history export --contact @alice --format jsonl --limit 200
```

`show`, `search`, and `export` accept either `--contact` or numeric `--chat`; `--connection` disambiguates Business accounts. Duplicate contact names require an explicit choice. Observed names and usernames remain aliases when a contact changes their profile.

Use `--until` with an ISO timestamp to bound the end of a query. Both bounds are inclusive and apply to observation time, rather than the original message's timestamp. Search performs a Unicode-normalized, case-insensitive substring match. It does not use embeddings, interpret intent, or require `jq`.

The bundled `history` skill gives Hermes the same bounded contact-first recipes. Stored text is conversation data, not instructions for the agent.

## Correspondence stats

```bash
hermes telegram-business stats
hermes telegram-business stats --contact @alice --since 30d
hermes telegram-business stats --chat 991 --connection business-123 --since 2026-10-01 --until 2026-10-18 --tz Europe/Moscow
hermes telegram-business stats --contact @alice --format json --session-gap 90m
```

Top-level `stats` is a deterministic local report over retained JSONL. It makes no LLM or network calls and writes no state. `history stats` remains **storage stats**: record/file counts, bytes, health and capture configuration.

With no contact/chat selector, stats shows at most `--limit` contacts (default 20, maximum 5000), ordered by latest **counted** activity after the range and replacement exclusions. Contacts without counted activity sort last; connection and chat IDs break ties deterministically. `--connection` can restrict the table. Selecting a contact/chat gives a detailed report; it uses the same aliases and ambiguity errors as history. JSON uses the same report structure for both modes.

Only archived text messages count. Voice, video notes, media, captions, service messages and automatic voice transcripts are excluded. Neither report format prints message content, deletion reasons or archived text. Human-readable contact labels escape terminal controls.

### Time, counts and sessions

- One message is one `(connection, chat, message_id)`. Repeated events and edits do not add messages. The last edit in canonical source order supplies direction, including `unknown`, even if a creation record follows it. Canonical source order means partition scan order, then line order within each file; observation timestamps do not reorder records when the clock moves backwards. Without edits, the last creation record supplies direction. Unknown is separate from received/contact and sent/owner.
- Send time uses the first creation record with `message_at` in canonical source order, then the first edit with `message_at` in that order, then `observed_at` from the first message creation/edit record in that order. First means source position, not the minimum timestamp. Later edits never move an original send time. Observation-month partitions are all scanned, including edits and classifications outside the selected range.
- Both bounds are inclusive. History durations (`7d`, `90m`, `6h`, `1w`, `30s`) are relative to one shared current instant. ISO timestamps accept offsets; an offset-free ISO timestamp uses UTC, matching history. A plain `YYYY-MM-DD` starts or ends the day in `--tz`, including its final microsecond and DST changes. The timezone defaults to `HERMES_TELEGRAM_BUSINESS_TIMEZONE`, otherwise UTC, and is always reported. With no `--since`, all retained history is considered.
- Deleted messages still count, except those whose latest retained deletion classification is `likely_duplicate` or `likely_correction`. These exclusions apply even when deletion/classification happened outside the requested send-time range.
- Deletion counts use **delete observation time**, never classification time. The last classification in canonical source order replaces earlier classifications of the same tombstone, regardless of its observation timestamp. Classifications join by tombstone event ID even when they precede the tombstone in source order. Classification-only evidence surviving a pruned tombstone uses `deleted_observed_at`; missing delete timestamps are reported separately and cannot be assigned to a range.
- Unexplained deletions are counts only; `history deletions` supplies details. The archive cannot prove who actually deleted a message. Message direction does **not** identify the deleter. JSON therefore reports all unexplained deletions under actor `unknown` (contact/owner are zero known attributions), and text states this uncertainty.
- Active days are distinct local dates with counted activity, also split into received and sent. Latest activity and “no observed reply after it” refer only to counted messages in the selected range; they do not imply intent or an overdue response.
- Sessions use send-time order. A new session starts only when the gap is **strictly greater** than `--session-gap` (default `6h`). Unknown-direction messages participate. Sessions are cut at the range edges. Reports count starters by contact/owner/unknown and sessions ending on received activity without an observed reply. Equal send timestamps use numeric message ID order to preserve the chat message sequence within Telegram's second-precision send times.

### Missing history and JSON

`retained_from` is the earliest **retained observation timestamp**, computed separately from canonical source order, not proof of capture start or continuous coverage. If `--since` precedes it, the range is marked partial. Coverage remains unknown otherwise: capture gaps and earlier pruning cannot be disproved from the retained archive. Pruned partitions automatically disappear from the next report.

Deletions without original evidence are counted separately (no message observation before the deletion, or a retained `missing_original` classification). Legitimate messages seen only in edits still count once; `edit_only_messages` separately identifies that evidence limitation. They do not prove exactly when capture began. `send_time_fallback_messages` identifies counted messages with no retained Telegram send timestamp. These evidence counts are not added to message totals. `deletions_without_observation_time` covers all retained unplaceable deletions, since their range cannot be determined.

JSON schema version 1 has stable top-level keys: `timezone`, inclusive UTC `since`/`until` (null when unbounded), `time_basis`, `text_messages_only`, `session_gap_seconds`, `sessions_cut_at_range_bounds`, `deletion_actor`, `deletion_details_command`, `coverage_note`, `contacts_total`, `contacts_returned`, and `contacts`. Each contact contains identity (`business_connection_id`, `chat_id`, `contact`), `messages`, `active_days`, `latest_activity`, `last_message_no_observed_reply`, `sessions`, `deletions`, and `missing_history`. Counts are integers; missing activity timestamps/directions are null. JSON timestamps are UTC; text renders them in the reported timezone. `missing_history.completeness` is always `unknown`.

Reads use existing root/chat locks and require a fresh catalog. Missing locks or stale/dirty/corrupt catalogs return the established rebuild/maintenance instructions without changing files. Torn/malformed JSONL fails with verification/repair instructions; stats never repairs or rebuilds. Output rows are bounded, but exact reconstruction scans all retained records for selected chats and retains their metadata in memory. Use a contact selector to limit the scan. Notifications, turns and response-time metrics are deferred.

### Reproduce with synthetic data

From this checkout, run the real registered CLI against a disposable fictional archive (stdlib only; no Telegram account or installed plugin needed):

```bash
python docs/examples/correspondence_stats.py stats --contact @alice_example --since 2026-09-19 --until 2026-10-19 --tz Europe/Moscow
python docs/examples/correspondence_stats.py stats --format json
```

Fixture setup explicitly creates the canonical JSONL/catalog under a directory in Python's default temporary location (honoring `TMPDIR`) and removes it afterward. The checkout and example directory can be read-only. The stats read itself writes nothing. The first command reports, among its timing/coverage lines:

```text
@alice_example connection=business-123 chat=991
  messages 5: received 3, sent 1, unknown direction 1
  active days 2 (received 2, sent 1)
  last message: received, no observed reply after it
  sessions 2 (gap 21600s, cut at range bounds): started by contact 2, by owner 0, unknown 0; ended without observed reply 1
  deletions 3: unexplained 1 (actor unknown), likely correction 1, likely duplicate 0, unclassifiable 1, pending 0
```

The range is partial, with one deletion without an original and one counted edit-only message. A November edit/classification does not move the October send times or hide the correction exclusion.

## Edits and deletions

```bash
hermes telegram-business history deletions --status unexplained --limit 20
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
| `HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE` | false | Enable capture. |
| `HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS` | unset | Optional comma-separated connection filter or `*`. |
| `HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS` | unset | Optional exact chat filter or `*`; exact IDs can opt in a chat outside the default types. |
| `HERMES_TELEGRAM_BUSINESS_HISTORY_CHAT_TYPES` | `private` | Eligible chat types; explicitly add `group`, `supergroup`, or `channel`. |
| `HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW` | `120` | Post-delete replacement window and classification delay, in seconds. |
| `HERMES_TELEGRAM_BUSINESS_HISTORY_NEARBY_BEFORE_SECONDS` | `15` | Pre-delete replacement window, in seconds. |
| `HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS` | `0` | Closed-partition retention target; `0` disables age pruning. |
| `HERMES_TELEGRAM_BUSINESS_HISTORY_MAX_BYTES` | `1073741824` | Total storage target before pruning old closed partitions. |
| `HERMES_TELEGRAM_BUSINESS_TIMEZONE` | `UTC` | Default IANA timezone for correspondence stats; overridden by `--tz`. |

Unknown chat types are excluded unless an exact chat ID or earlier canonical profile evidence establishes eligibility. No member-count requests run on capture.

## Storage and recovery

```text
$HERMES_HOME/data/telegram-business/history/
  <connection-key>/<chat-id>/YYYY-MM.jsonl
  contacts.json
```

JSONL is canonical. Events have durable identities and are appended under a lock with `fsync`; retries do not append the same event twice. The contact catalog is a small derived directory, not a second message database. Monthly partitions follow observation time. History timeline/search queries select the relevant conversation and observation-date partitions before streaming records. Correspondence stats scans all retained partitions of selected chats to reconstruct send-time activity correctly.

```bash
hermes telegram-business history stats  # storage stats
hermes telegram-business history catalog
hermes telegram-business history catalog --rebuild
hermes telegram-business history verify
hermes telegram-business history verify --repair-tails
hermes telegram-business history maintain
```

Read commands do not repair canonical files. `verify --repair-tails` explicitly repairs a torn final line; invalid interior records need investigation. Startup recovery and maintenance operate on persisted history, and the catalog can be rebuilt from retained JSONL. Query results and verification diagnostics are bounded. Human-readable output escapes terminal control characters; JSONL export retains raw text.

## Retention and removal

Telegram-side deletion does **not** erase the archive. The archive deliberately preserves the earlier text to make deletion inspection possible.

Automatic retention and size pruning remove whole closed monthly partitions. The current month and partitions needed for pending deletion classification are preserved, so a configured target is not a hard storage ceiling. The contact catalog is rebuilt from retained evidence after pruning.

No record-level erasure command ships in this version. To remove an entire local archive, disable capture, stop the gateway, back up anything you need, and remove the intended profile-local history directory before restarting. Include any external backups or exports in your own removal process. Future dossiers and reply state must support scoped forgetting before those features ship.

## Runtime boundary

The native Telegram observer records eligible Business text before ordinary Hermes handlers. It also receives deleted-message updates, which have no effective message. Capture does not change the ordinary Hermes authorization or agent routing of text messages. Disable history to stop capturing new messages; use Hermes's normal configuration to control agent replies.

The separate voice module still deletes transient audio after processing. This version does not insert voice transcripts into the text archive automatically.
