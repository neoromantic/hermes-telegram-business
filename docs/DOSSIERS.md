# Contact dossiers and owner persona

This slice builds editable context; it does not send Telegram messages, draft
replies, or enable automatic replies. Dossier processing is **off by default**,
with a separate switch from history capture:

```bash
HERMES_TELEGRAM_BUSINESS_DOSSIER_ENABLE=1
```

Set this only in the intended active Hermes profile, then restart its gateway.
History capture stays LLM-free. All private Business chats already represented in
canonical history are eligible once they contain both received and sent text.
There is no contact build command or exclusion list. Groups, supergroups,
channels and unknown chat types are never dossier sources, even if explicitly
captured by history. Media, voice transcripts and captions are excluded.

## Model input and bounds

`history_support.canonical_text_messages` is the shared semantic message view
for dossiers and `history stats --messages` (optionally `--contact`, `--chat`,
`--connection`). Existing physical archive statistics stay unchanged. It deduplicates event IDs and replays one
latest text per connection/chat/message ID. Messages sort by Telegram send time,
falling back to first observation; directions are `inbound`, `outbound`, or
`unknown`. Deletions remain labelled `deleted: true`, except deletions classified
`likely_duplicate` or `likely_correction`, whose replaced texts are omitted.

A contact's first refresh includes the last 90 days and at most 500 messages.
Inputs split at 50 messages or 24,000 serialized message characters per call.
Telegram text fits this bound; an oversized malformed history message fails the
contact safely without advancing its cursor. Later calls contain only new or
changed messages, current facts, read-only owner feedback, removal tombstones,
and removed source references. Content digests detect edits and new IDs even
when timestamps are identical. An unchanged target makes no model call. If a
later chunk fails, the target's cursor does not advance and the next cycle retries
it. Successful contacts in the same cycle are committed independently.

```bash
hermes telegram-business dossier preview --contact @alice
hermes telegram-business dossier preview --persona
```

Preview prints exactly the JSON data string used in the next host structured
call, without calling the model or modifying the dossier/history. `null` means no
pending input (or insufficient initial evidence). It shows the first call of a
multi-call refresh; later calls depend on earlier model results. Manual run bypasses
the quiet period/persona cadence. The system prompt and JSON schema are fixed in
`dossier_support.py`; preview displays the data, not the host transport envelope.
Correspondence, facts and quotes are untrusted data, never model instructions.
Names and aliases come exclusively from the history contact catalog.

## Worker and configuration

The worker starts only from Hermes's supported native Telegram handler factory,
which runs at gateway adapter connection. `register()` merely registers callbacks
and CLI/skills; CLI processes never start a worker. No incoming update or media is
needed. The first cycle starts after 180 seconds and application readiness; subsequent cycles wait 30
minutes after completion. The task exits when the PTB application stops or the
flag is disabled; gateway loop shutdown cancels it. Where available, the verified
`ctx.spawn_task` API supervises it so plugin unload/force reload cancels it too. A reconnect replaces the old
application's worker. Manual and automatic cycles share a nonblocking POSIX file
lock, so only one runs across processes. A locked cycle reports `locked`.

Every due contact is processed, with no per-cycle contact cap. The rolling call
window persists across cycles, failures and process restarts. The maximum is
20 model calls per minute; a lower configured rate is respected. Contacts must
be quiet for at least 10 minutes based on the latest observed history event.
A failed target is retried next cycle. Manual runs print per-result counts and
exit nonzero when any target fails. Operational logs contain counts, hashed
target IDs, timings and exception type names, never message/fact/quote text,
exception bodies or tracebacks.

All settings below have prefix `HERMES_TELEGRAM_BUSINESS_DOSSIER_`:

| Suffix | Default | Meaning |
|---|---|---|
| `ENABLE` | off | Separate opt-in for processing |
| `BACKFILL_DAYS` | 90 | Initial rolling date window |
| `BACKFILL_MESSAGES` | 500 | Initial most recent text count |
| `INTERVAL_MINUTES` | 30 | Delay between completed cycles |
| `QUIET_MINUTES` | 10 | Automatic contact quiet period; 0 allowed |
| `MAX_CALLS_PER_MINUTE` | 20 | Rolling call limit; clamped to 1–20 |
| `PROVIDER` | cleanup provider | Host provider override |
| `MODEL` | cleanup model | Host model override |

Provider/model inherit `TG_BUSINESS_VOICE_CLEANUP_PROVIDER` and
`TG_BUSINESS_VOICE_CLEANUP_MODEL`; fallback values are `gemini` /
`gemini-3.8-flash`. Every request uses `ctx.llm.acomplete_structured`, the verified
async facade of Hermes `complete_structured`, with JSON schema/mode, timeout and
purpose. There is no independent provider client, HTTP path, or credential store.
The host's configured trust gate remains authoritative.

## Facts, evidence and owner controls

Contact sections are always present: `relationship`, `preferences`,
`shared_facts`, `commitments`, `decisions`, `open_questions`, `follow_ups`, and
`contradictions`. `preferences` covers language/tone. A commitment should identify
who promised what. Facts have stable `f1`, `f2`, … IDs, `observed`, `inferred`, or
`owner` kinds, status, sources, and first/last revision numbers. Inferred facts
carry low/medium/high confidence. The prompt prohibits personality traits, motives
and diagnoses; uncertainty and conflicting accounts should remain explicit.

Checks reject references outside the current contact, discard invalid/long
quotes, downgrade unquoted observed claims to inferred, drop facts with no valid
source, restrict sections, cap fact text at 800 characters, cap sources at 20 per
fact and generated/current facts at 100. Quotes must be exact substrings of at
most 200 characters. Source dates/directions/deletion flags come from canonical
history, never from the model. A valid quote supports that the words were said;
it does not prove the underlying claim or resolve conflicting accounts.

```bash
hermes telegram-business dossier status
hermes telegram-business dossier list
hermes telegram-business dossier show --contact @alice --format text
hermes telegram-business dossier show --contact @alice --fact f3 --format json
hermes telegram-business dossier show --persona
hermes telegram-business dossier correct --contact @alice --fact f3 --text 'We agreed Wednesday, not Tuesday.'
hermes telegram-business dossier pin --contact @alice --fact f3
hermes telegram-business dossier unpin --contact @alice --fact f3
hermes telegram-business dossier remove --contact @alice --fact f3
hermes telegram-business dossier note --contact @alice --section preferences --text 'Use short replies.'
hermes telegram-business dossier changes --contact @alice
hermes telegram-business dossier export --contact @alice --format json
hermes telegram-business dossier export --contact @alice --format markdown
hermes telegram-business dossier run
hermes telegram-business dossier run --contact @alice
hermes telegram-business dossier run --persona
```

Use `--chat <id> --connection <id>` instead of `--contact` to disambiguate through
the existing history resolver. `--persona` selects the owner persona. Explicit
chat/connection IDs still reach stored dossiers after retention removes their
catalog row. Missing/ambiguous aliases are never guessed. Human-readable display
escapes terminal control characters; JSON remains machine readable.

`show` reports coverage (processed message versions, date range, backfill cutoff),
last refresh and model, statuses, and contradictions. `show --fact` includes its
canonical source messages for owner inspection; these are read on demand, never
copied into dossier storage. Viewing stale/unsupported facts does not write to
history or the dossier.

Corrections become owner facts. Owner notes and corrections cannot be changed or
retired by the model, and pins protect generated facts. Removals create persistent
"do not re-add" tombstones, checked by normalized text similarity (SequenceMatcher
ratio >= 0.8) as well as provided in the prompt. Similarity is lexical, not semantic.
`changes` records additions, changes, retirements and invalidations with reasons,
plus owner corrections/pins/removals/notes. Refresh rebases over owner changes made
during a model call, rather than overwriting them. Forgetting invalidates an
in-flight generation even during the very first refresh.

## Persona

The persona summarizes only sent text from all private chats, using the same
canonical/deletion and initial backfill rules. Its sections cover `language`,
`tone`, `vocabulary`, `formatting`, `reply_length`, `greetings`, and `boundaries`.
The display includes a plain summary and source references qualified with a
stable connection/chat key, so `m2` in two chats cannot collide. There are no
additional cross-contact persona leakage filters in this slice, by owner choice.

After its first revision, automatic persona refresh waits for 20 new sent
messages, or 24 hours with at least one new sent message. Changed/deleted/pruned
sources reconcile without waiting for that cadence. No new/changed sources means
no call even after 24 hours. Explicit owner controls also accept `--persona`.

## Source changes, reset and storage

An edit is sent as changed. `show` marks affected facts stale without mutation.
At refresh the model re-evaluates them; unsupported generated claims are removed.
Deleting text preserves the source with its deletion flag. Later duplicate or
correction classification drops it. Pruning drops removed sources. A generated
fact with no sources becomes unsupported and is removed on refresh. Pins and
owner corrections survive as `source gone`; they are never silently rewritten.
Stored targets whose entire catalog row was pruned are still reconciled.

```bash
hermes telegram-business dossier forget --contact @alice
hermes telegram-business dossier forget --persona
hermes telegram-business dossier forget --all
```

Forget deletes the target snapshot, feedback/tombstones and revision log, leaving
canonical history untouched. While enabled, the next worker cycle rebuilds it;
forget is a reset. To stop processing, turn the dossier flag off, then forget all.

Storage is `${HERMES_HOME}/data/telegram-business/dossiers/`, with one hashed
connection/chat directory and `persona/`, each containing `dossier.json` and
`changes.jsonl`. Directories are 0700, files 0600, including temporary files and
lock/rate metadata. Atomic replacement and fsync prevent partial snapshots; target
locks synchronize inspection and owner mutations. Stable lock files outside the
removable target directory prevent a concurrent reset from splitting the lock.
The revision log publishes before the authoritative snapshot; readers ignore an
uncommitted log tail after interruption. Snapshots also carry the last revision
record to recover a missing log tail. Only bounded quotes/facts and digest cursors are stored; there is no
second full-message archive, SQLite/FTS, embedding or external memory service.

## Synthetic example

This is fictional fixture history, not a real conversation:

```json
{"messages":[
  {"ref":"m1","date":"2026-10-18T18:00:00+00:00","direction":"inbound","deleted":false,"text":"Let's meet Tuesday."},
  {"ref":"m2","date":"2026-10-18T18:01:00+00:00","direction":"outbound","deleted":false,"text":"Sure, Tuesday works!"}
]}
```

A valid generated `shared_facts` entry might be `f1`, observed, "Meeting proposed
for Tuesday", citing `m1` with exact quote "Let's meet Tuesday.". Inspect with
`show --contact @alice --fact f1 --format json`. Then correct it with
`correct --contact @alice --fact f1 --text 'The owner changed the meeting to Wednesday.'`.
A later refresh preserves that owner correction even if the model attempts to
retire `f1`. Removing it adds a tombstone; forgetting the dossier clears the
correction and tombstone and permits a fresh backfill.

Validation uses only synthetic histories and mocked host LLMs. Telegram delivery,
provider trust/availability and actual model summary quality require separate
operator-authorized runtime verification. This slice does not implement #18/#19.
