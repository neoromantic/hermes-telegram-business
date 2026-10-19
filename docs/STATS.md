# Correspondence stats

`hermes telegram-business stats` is a deterministic report over retained [correspondence history](HISTORY.md). For each contact it counts text messages received and sent, active days, conversation sessions, latest activity, and deletions. It also says which history is missing.

It reads only what history has already retained, so capture must be enabled first. It uses no LLM, stores nothing new, and never prints message text.

## Commands

```bash
hermes telegram-business stats
hermes telegram-business stats --contact @alice --since 30d
hermes telegram-business stats --chat 991 --connection business-123 --since 2026-10-01 --until 2026-10-18 --tz Europe/Moscow
hermes telegram-business stats --contact @alice --format json
```

Without `--contact` or `--chat`, the command prints a table of contacts with activity in the range, newest first. With either option, it prints a detailed report for one contact. `--contact`, `--chat`, and `--connection` use the same contact resolver as `history show`. An ambiguous name requires an explicit choice.

| Option | Meaning |
|---|---|
| `--contact` | Contact name, `@username`, or alias from the history catalog. |
| `--chat` | Numeric Telegram chat ID. |
| `--connection` | Business connection ID. Filters the table and disambiguates `--chat` or `--contact`. |
| `--since` | Inclusive start. Default: all retained history. See [Time and time zone](#time-and-time-zone). |
| `--until` | Inclusive end. Default: now. |
| `--tz` | IANA time zone for dates, day boundaries, and active days. Default: `HERMES_TELEGRAM_BUSINESS_TIMEZONE`, otherwise `UTC`. |
| `--session-gap` | Silence that starts a new session. A whole number with `s`, `m`, `h`, `d`, or `w`, such as `90m` or `1d`. Default: `6h`. |
| `--limit` | Contacts shown in the table, from 1 to 5000. Default: 20. Ignored for one contact. |
| `--overdue-after` | Optional explicit overdue threshold for the latest retained incoming turn, such as `24h`. Without it no overdue judgment is made. |
| `--format` | `text` or `json`. Default: `text`. |

## Owner private bot DM

Start the bot, open its private DM, and send:

```text
/business_stats --connection business-123 --contact @alice --since 30d
/business_stats --connection business-123 --overdue-after 24h
```

Every request requires the exact `--connection` (available from the local CLI overview). This avoids guessing which Business account owns a contact. The namespaced command avoids Hermes's global `/stats`. The same parser, contact resolver, and report serve CLI and DM; the DM caps the overview at 50 contacts and output at eight chunks of 3500 ASCII characters. Larger reports ask you to narrow the query or use the CLI. JSON remains available with `--format json`.

Before any contact lookup, error details, or overview, the bot makes a fresh Bot API `getBusinessConnection` request for exactly that connection. It requires an enabled connection, the API's non-bot owner matching the DM sender, and `user_chat_id` matching that owner's private DM. History's owner cache, mode-store owner maps, and CLI `--owner` never grant access. An API failure or missing identity fails closed. The CLI remains an independent local read with no API calls.

The native PTB handler consumes reserved commands before history and Hermes core, including rejected peer, group, Business-chat, bot, forwarded, inline-bot, and edited attempts. It never posts stats as a Business reply. Invalid syntax or unverified requests are silently consumed; use the explicit syntax above and check gateway logs for owner-verification failures. A verified owner's history errors are returned only to that DM. A delivery permission failure logs that the owner must start/unblock the bot and retry; ambiguous sends are not retried automatically. Duplicate command deliveries are suppressed in memory for 24 hours. This native handler is the wired runtime path; the synchronous gateway hook does not independently serve stats. Dict fixtures exercise the same boundary, rather than implying another gateway route.

## Example (synthetic)

The blocks below are real CLI output over fictional history. Four made-up contacts (Alice Example, Bob Builder, Carol, and Dave) were built with the test helpers in `tests/test_stats.py`, with the clock fixed at 2026-10-19 11:32 UTC. A test checks that these blocks still match the command output.

One contact:

```bash
hermes telegram-business stats --contact @alice --since 2026-10-01 --until 2026-10-18 --tz Europe/Moscow
```

```text
range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only
Alice Example (@alice) connection=business-123 chat=991
  messages 9: received 5, sent 3, unknown direction 1
  active days 4 (received 3, sent 3)
  latest 2026-10-17 21:04 received; last sent 2026-10-17 19:30
  last message: received 2026-10-17 21:04, no observed reply after it
  sessions 4 (gap 6h): started by contact 2, by owner 2; ended on received 2
  turns: received 4, sent 3; unanswered incoming 3 by session [0, 1, 1, 1]
  response time (last preceding to first responding message): owner median 1800s (1 sample), contact median 3120s (2 samples)
  incoming: session-ended 2, unknown-interrupted 1, range-ended 0, answered after range 0; latest pending false
  deletions 4: unexplained 1 (received 1, sent 0), likely correction 1, likely duplicate 1, pending 1
  missing history: retained from 2026-10-03 (range is partial); 1 deletion of a message not in history; 1 message known only from an edit
  deletion details: hermes telegram-business history deletions --connection business-123 --chat 991 --status unexplained
```

All contacts:

```bash
hermes telegram-business stats --since 2026-10-01 --until 2026-10-18 --tz Europe/Moscow
```

```text
range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only
latest            last      msgs  recv  sent  unk  days  sess  rturn  sturn  owner_s  owner_n  contact_s  contact_n  unans  ended  pending  overdue  del  unexpl  partial  connection    chat  contact
2026-10-18 09:15  sent         2     1     1    0     1     1      1      1      900        1          -          0      0      0       no        -    0       0  yes      business-123  992   Bob Builder (@bob)
2026-10-17 21:04  received     9     5     3    1     4     4      4      3     1800        1       3120          2      3      2       no        -    4       1  yes      business-123  991   Alice Example (@alice)
-                 -            0     0     0    0     0     0      0      0        -        0          -          0      0      0       no        -    1       0  no       business-456  994   Carol
shown 3 of 4 contacts; not shown: 1 with no activity in range
missing history in shown contacts: 1 deletion of a message not in history; 1 message known only from an edit; use --contact for details
response time: median seconds, last preceding to first responding message in same session; owner_n/contact_n are sample counts; overdue threshold not set; observed text only
deletion details: hermes telegram-business history deletions --status unexplained
```

Carol has only a deletion in the range, so her row comes last. Dave wrote only in September, so he is not shown, and the footer says so.

JSON for one contact. It is trimmed here: `chat_type`, `name`, `username`, `active_days`, and `sessions` are left out. Real output always has every key.

```bash
hermes telegram-business stats --contact @alice --since 2026-10-01 --until 2026-10-18 --tz Europe/Moscow --format json
```

```json
{
  "schema_version": 1,
  "report": "correspondence_stats",
  "mode": "contact",
  "generated_at": "2026-10-19T14:32:00+03:00",
  "range": {
    "since": "2026-10-01T00:00:00+03:00",
    "until": "2026-10-18T23:59:59.999999+03:00",
    "timezone": "Europe/Moscow",
    "time_basis": "telegram_send_time",
    "deletion_time_basis": "deletion_observed_time",
    "content": "text_messages_only",
    "session_gap_seconds": 21600,
    "overdue_after_seconds": null
  },
  "contacts": [
    {
      "business_connection_id": "business-123",
      "chat_id": 991,
      "display_name": "Alice Example (@alice)",
      "messages": {
        "total": 9,
        "received": 5,
        "sent": 3,
        "unknown_direction": 1
      },
      "latest": {
        "at": "2026-10-17T21:04:00+03:00",
        "side": "received",
        "received_at": "2026-10-17T21:04:00+03:00",
        "sent_at": "2026-10-17T19:30:00+03:00",
        "observed_reply": "none"
      },
      "turns": {
        "received": 4,
        "sent": 3,
        "unanswered_incoming": 3,
        "unanswered_incoming_by_session": [
          0,
          1,
          1,
          1
        ],
        "session_ended_incoming": 2,
        "unknown_interrupted_incoming": 1,
        "range_ended_incoming": 0,
        "answered_after_range": 0,
        "pending_latest_incoming": false,
        "overdue_latest_incoming": null
      },
      "response_time": {
        "basis": "last_preceding_to_first_responding_message",
        "owner": {
          "median_elapsed_seconds": 1800.0,
          "samples": 1
        },
        "contact": {
          "median_elapsed_seconds": 3120.0,
          "samples": 2
        }
      },
      "deletions": {
        "total": 4,
        "unexplained": {
          "total": 1,
          "received": 1,
          "sent": 0,
          "unknown_direction": 0
        },
        "likely_correction": 1,
        "likely_duplicate": 1,
        "unclassifiable": 0,
        "pending": 1,
        "details_command": "hermes telegram-business history deletions --connection business-123 --chat 991 --status unexplained"
      },
      "missing_history": {
        "retained_from": "2026-10-03T11:00:05+03:00",
        "range_partial": true,
        "deletions_of_messages_not_in_history": 1,
        "messages_known_only_from_edit": 1
      }
    }
  ],
  "overview": null
}
```

## What is counted

- **Text messages only.** History stores text messages only. Voice notes, video notes, media, captions, and service messages are not in the archive and are not counted. Every report says "text messages only".
- **Message.** One message is one `(connection, chat, message_id)`. It is counted once: edits and repeated deliveries do not add messages.
- **Sent and received.** Sent is the owner side (outbound). Received is the contact side (inbound). A message with `unknown direction` is shown on its own and is not part of sent or received. The latest version of a message with a known direction decides its side, so an edit can change it. An edit with unknown direction does not erase a known side. "Latest" means the order history recorded events in, as with the other history commands, so a capture clock that stepped backwards cannot reorder them. The same rule picks a deletion's latest classification.
- **Deleted messages.** A deleted message still counts as sent or received, because it was sent. The exception is a deletion classified `likely_duplicate` or `likely_correction`. Another message replaced it, so it is not counted again. Replaced messages are also left out of active days, latest activity, and sessions.
- **Deletions.** The deletions line counts deleted messages, one per message, by status: `unexplained`, `likely correction`, `likely duplicate`, `unclassifiable`, and `pending` (not classified yet). When a message has several deletion records, the first status in this order wins: likely duplicate, likely correction, unexplained, unclassifiable, pending.
- **Deletion time.** A deletion falls into the range by the time it was first observed, not by the message's send time. A message sent before the range and deleted inside it is on the deletions line, but not in the message count.
- **Side of a deletion.** Unexplained deletions are split by the side of the deleted message: received, sent, and unknown direction when above 0. Telegram does not report who deleted a message, so the report never says who did. When there are unexplained deletions, the report prints the `history deletions` command for details. That command lists every unexplained deletion in the chat, not only those in the range.
- **Active days.** The number of distinct calendar dates in `--tz` with at least one counted message. Also shown for received and for sent. Unknown-direction messages count in the total only.
- **Latest activity.** The latest counted message and its side, plus the latest received and the latest sent message.
- **Last message.** When the latest counted message is received, a `last message:` line follows. "no observed reply after it" means history has no sent message after it. "reply observed after the range" means a sent message came after it, but after `--until`. This is an observation, not a guess about intent: a reply by voice, by phone, or outside Telegram is not in history.
- **Turns.** Consecutive counted messages of the same known side inside one session form a turn. Unknown-direction messages break adjacency and form no turn. A gap longer than the session threshold also breaks adjacency. Equal timestamps use numeric message IDs as the tie-breaker. Range boundaries cut turns, like sessions.
- **Response time.** Only adjacent opposite-side turns inside the range and the same session produce a sample. Elapsed seconds run from the **last message of the preceding turn to the first message of the responding turn**. The median and sample count are reported separately for owner and contact; JSON uses `null` when there are no samples. Zero-second tied samples are valid. No response is inferred across unknown direction, a session gap, or a range boundary. Overview `owner_s` / `contact_s` contain median seconds and `owner_n` / `contact_n` contain sample counts; `rturn` / `sturn` count contact / owner turns.
- **Unanswered incoming turns.** These are incoming turns with no adjacent observed owner turn in their session. `unanswered_incoming_by_session` lists counts in chronological session order, including zeros. `session_ended_incoming` has a later retained message beyond the session gap; `unknown_interrupted_incoming` meets an unknown-direction barrier. `range_ended_incoming` meets a later same-side retained message outside the range. `answered_after_range` has an adjacent retained owner response outside the range, within the gap; it is excluded from unanswered counts and response-time samples. `pending_latest_incoming` identifies the final retained incoming turn when its final message is also in range; it stays pending observationally regardless of its age, separately from older session endings. These categories concern retained text, not intent. The overview shows `unans`, `ended`, and `pending`.
- **Explicit overdue threshold.** Only `--overdue-after` enables `overdue_latest_incoming`; otherwise its JSON value is `null` and the overview prints `-`. With a threshold it is true only for the latest retained pending incoming turn whose last-message age is at least that threshold. Historical range endings and any later retained message prevent a pending/overdue classification, including a later outbound reply after a session gap. Quiet periods alone never assign intent. Phone, voice/media, and unretained/outside-range exchanges can change the real situation.
- **Sessions.** Counted messages in the range are sorted by send time, then by message ID. A new session starts when the gap since the previous message is longer than `--session-gap` (default 6 hours). A gap exactly equal to it stays in the same session. Unknown-direction messages take part in session boundaries. The report gives the number of sessions, who started them (contact, owner, and unknown when above 0), and how many ended on a received message. Only messages inside the range are used, so a session that crosses a range boundary is cut at that boundary.

## Missing history

Missing history is shown on its own line. It is never mixed into the counts.

- **Retained from.** The date the chat's retained history starts: its first retained observation. If `--since` is earlier, the report says "(range is partial)": history was not captured yet, or it was pruned. Without `--since`, the report is never partial.
- **Deletions of messages not in history.** A deletion observed in the range, of a message with no retained record and no known send time. It is not on the deletions line and does not make a contact active. If the deletion record still has the original send time, it counts as a normal deletion with that record's side.
- **Messages known only from an edit.** An edit of a message whose original was never captured (it was sent before capture started) or was pruned. It is listed when its send time is in the range. It is not counted.

When nothing is missing, the line reads `missing history: none (retained from <date>)`.

Stats are computed live from retained JSONL. Retention removes whole monthly partitions, so pruned messages drop out of the counts on their own. The partial marker shows where retained history begins.

## Time and time zone

- Messages are placed by Telegram's send time (`message_at`), not by the time the bot observed them. If the send time is missing, the first observation time is used.
- `--tz` takes an IANA name such as `Europe/Moscow`. The default is `HERMES_TELEGRAM_BUSINESS_TIMEZONE` when it is set, otherwise UTC. The report always prints the time zone it used. An unknown name is an error. Named zones need the system time zone database or the Python `tzdata` package; `UTC` always works.
- `--since` and `--until` accept:
  - relative values such as `30m`, `6h`, `7d`, or `2w`, counted back from now;
  - plain dates such as `2026-10-01`: the start of that day for `--since`, and the end of that day for `--until`, in `--tz`. Other ISO date forms, such as `20261001`, work too;
  - ISO timestamps. A timestamp without an offset is read in `--tz`. A timestamp with `Z` or an offset keeps it.
- Both bounds are inclusive. `--until` must not be earlier than `--since`. Without `--since`, the report covers all retained history. Without `--until`, it runs to now.
- The first line shows the range. Whole-day bounds print as dates, other bounds as `YYYY-MM-DD HH:MM` in `--tz`. Open bounds print as `start of history` and `now`.
- The report uses what is known at report time. An edit, deletion, or classification observed after `--until` still applies to a message sent inside the range.

These rules differ from `history show` and `history search`. Their bounds apply to observation time, and they read a timestamp without an offset as UTC.

To set the default time zone, add this to the active Hermes profile's `.env`:

```dotenv
HERMES_TELEGRAM_BUSINESS_TIMEZONE=Europe/Moscow
```

## Contacts table

Each row is one chat with at least one counted message or counted deletion in the range. Rows with messages come first, newest latest message first. Rows with only deletions follow. Ties are broken by connection, then chat. The same chat ID under two Business connections gives two rows.

| Column | Meaning |
|---|---|
| `latest` | Latest counted message in `--tz`, or `-`. |
| `last` | Side of the latest message: `received`, `sent`, `unknown`, or `-`. |
| `msgs`, `recv`, `sent`, `unk` | Messages: total, received, sent, unknown direction. |
| `days` | Active days. |
| `sess` | Sessions. |
| `del`, `unexpl` | Deletions, and unexplained deletions. |
| `partial` | `yes` when the range starts before retained history. |
| `connection`, `chat`, `contact` | Business connection, chat ID, and contact name. |

The footer says how many contacts were shown out of all contacts in scope, and why the rest were not shown: over `--limit`, or no activity in the range. It also sums missing history for the shown rows, and prints the `history deletions` command when a shown row has unexplained deletions. With no active contacts, the table is replaced by `No contacts with activity in range.` With no history at all, the command prints `No Telegram Business history found.`

A contact with no activity in the range is not a row, but its missing history is still reported, apart from the counts. "No activity" can mean that history for that time was never captured or was pruned, so the activity is unknown, not zero. When `--since` is earlier than a hidden contact's retained history, the footer says so:

```text
range 2026-09-01..2026-09-30 UTC, by Telegram send time; text messages only
No contacts with activity in range.
shown 0 of 3 contacts; not shown: 3 with no activity in range (range is partial for 2: retained history starts after --since)
missing history in contacts with no activity in range: 1 message known only from an edit; use --contact for details
```

Here two of the three contacts have retained history only from October, so their September activity is unknown. The last line sums deletions of messages not in history and messages known only from an edit for the hidden contacts. To find them, `history contacts` prints each contact's retained range (`range=`), and `stats --contact` prints one contact's missing history.

## JSON output

`--format json` prints one JSON document with stable keys. Both modes use the same shape. Every key is always present; a value that is not known is `null`.

| Key | Meaning |
|---|---|
| `schema_version` | `1`. |
| `report` | `correspondence_stats`. |
| `mode` | `contact` or `overview`. |
| `generated_at` | Report time. |
| `range` | `since`, `until`, `timezone`, `time_basis` (`telegram_send_time`), `deletion_time_basis` (`deletion_observed_time`), `content` (`text_messages_only`), and `session_gap_seconds`. |
| `contacts` | One object per contact. In overview mode, the shown rows in table order. |
| `overview` | `null` for one contact. Otherwise `contacts_in_scope`, `active`, `shown`, `limit`, `not_shown_over_limit`, `not_shown_no_activity`, `not_shown_no_activity_partial`, and `not_shown_no_activity_missing_history`. |

The last two keys cover contacts with no activity in the range, which are not in `contacts`. `not_shown_no_activity_partial` is how many of them have `range_partial`: their retained history starts after `--since`, so their activity in the range is unknown, not zero. `not_shown_no_activity_missing_history` sums `deletions_of_messages_not_in_history` and `messages_known_only_from_edit` over them. Neither is part of any count.

Each contact object has:

| Key | Contents |
|---|---|
| identity | `business_connection_id`, `chat_id`, `chat_type`, `display_name`, `name`, `username` |
| `messages` | `total`, `received`, `sent`, `unknown_direction` |
| `active_days` | `total`, `received`, `sent` |
| `latest` | `at`, `side` (`received`, `sent`, `unknown`, or `null`), `received_at`, `sent_at`, `observed_reply` (`none`, `after_range`, or `null`) |
| `sessions` | `total`, `started_by_contact`, `started_by_owner`, `started_by_unknown`, `ended_on_received` |
| `deletions` | `total`, `unexplained` (`total`, `received`, `sent`, `unknown_direction`), `likely_correction`, `likely_duplicate`, `unclassifiable`, `pending`, `details_command` (`null` without unexplained deletions) |
| `missing_history` | `retained_from`, `range_partial`, `deletions_of_messages_not_in_history`, `messages_known_only_from_edit` |

Timestamps are ISO 8601 in `--tz`, with the offset. The output is ASCII; other characters are escaped. Errors are a plain `error: ...` line with exit code 1, also with `--format json`.

The schema remains version 1 with additive `turns`, `response_time`, and range `overdue_after_seconds` fields; existing fields keep their meanings. Consumers must allow additional keys. `overdue_after_seconds` is `null` when omitted. All reports contain these keys:

| Block | Added keys |
|---|---|
| `turns` | `received`, `sent`, `unanswered_incoming`, `unanswered_incoming_by_session`, `session_ended_incoming`, `unknown_interrupted_incoming`, `range_ended_incoming`, `answered_after_range`, `pending_latest_incoming`, `overdue_latest_incoming` |
| `response_time` | `basis` = last preceding to first responding message; `owner` and `contact` each contain `median_elapsed_seconds`, `samples` |
| `range` | `overdue_after_seconds` |

## Storage stats

`hermes telegram-business history stats` is a different command and is unchanged. It shows storage stats: records, bytes, and settings. `hermes telegram-business stats` shows correspondence stats per contact.

## Privacy and safety

- The report never prints message text, in text or JSON.
- Contact names are untrusted. Text output escapes control characters and bidirectional marks in them.
- No LLM. The local CLI report makes no network calls and stores nothing new; the owner DM makes the explicit live ownership lookup and private Bot API sends described above.
- The command is read-only. It does not rebuild the contact catalog, repair files, or classify pending deletions. A pending deletion stays pending until the gateway or `history maintain` classifies it.
- Like the other history read commands, it fails closed. A stale or damaged catalog, a torn final line, or a missing lock is an error: the command prints `error: ...`, exits 1, and prints no partial report. Catalog and tail errors name the command that fixes them.

## Notification slice not implemented

Opt-in owner notifications for unexplained deletions and overdue incoming turns remain unfinished. They are OFF because there is no notification policy store, CLI configuration, evaluator, scheduler, or delivery path. A stats `--overdue-after` report never enables alerts. There is no executable owner digest example because no digest implementation is delivered.

The verified native-handler and unload lifecycle interfaces are available. The remaining blocker is bounded implementation and safety verification, not an absent Hermes API. Completing this separate slice requires explicit per-owner/connection/chat policies (types, overdue delay, timezone/quiet hours, immediate/digest), a canonical-source baseline that prevents enable-time floods, atomic private policy and durable delivery claims across workers/restarts, revalidation of pruning/classification/replies and fresh owner destination before sends, bounded body-free digests/source refs, uncertain-send status without automatic retry, periodic gateway evaluation without JobQueue, clean unload, DST/quiet-hour and permission/error-isolation integration tests. None of those guarantees is approximated by a placeholder here. No notifications or notification configuration should be advertised until that complete slice is reviewed.

## Not in this version
- Counting voice notes, video notes, and media. This needs a metadata-only capture change in history.
- A global `/stats` alias; use `/business_stats --connection ...` in the owner DM.
