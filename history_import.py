"""Import the past messages of one private chat from a Telegram Desktop export into history.

``hermes telesec history import result.json --chat ID`` reads the JSON file that Telegram
Desktop's "Export chat history" writes and stores its text messages as ordinary history
records marked ``source: "import"``. Stats, the panel and dossiers then see the time before
capture started. Only messages older than the chat's first captured record are imported, so
capture stays the source for everything it saw or was told not to save. Stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Attribute access only (``_history._x(...)``), like correspondence_stats: no import cycle,
# and monkeypatches of history_support helpers apply here too.
from . import chat_settings as _chat_settings
from . import history_support as _history

IMPORT_SOURCE = "import"
EXPORT_CHAT_TYPE = "personal_chat"
VOICE_MEDIA_TYPES = frozenset({"voice_message", "video_message", "audio_file"})
# A message with any of these has media or a non-text payload; capture records text only.
MEDIA_KEYS = (
    "photo",
    "file",
    "media_type",
    "sticker_emoji",
    "poll",
    "location_information",
    "contact_information",
    "game_title",
    "invoice_information",
    "place_name",
    "todo_list",
    "giveaway_information",
)
# The owner's in-chat `/stats` (same pattern as the plugin's command) and `/dossier` are control
# traffic that capture never records.
_STATS_COMMAND_RE = re.compile(r"^/stats(?:@\w+)?(?:\s+(7d|30d|90d|all))?\s*$", re.IGNORECASE)
# Skip reasons in summary order, with their labels for one and for several.
SKIP_LABELS = (
    ("saved", "already saved", "already saved"),
    ("newer", "newer than saved history", "newer than saved history"),
    ("retention", "older than retention", "older than retention"),
    ("voice", "voice, round video or audio (not transcribed)", "voice, round video or audio (not transcribed)"),
    ("media", "other media", "other media"),
    ("service", "service message", "service messages"),
    ("command", "Telesec command", "Telesec commands"),
    ("other_sender", "from a bot or a third account", "from a bot or a third account"),
    ("empty", "without text", "without text"),
    ("unreadable", "unreadable", "unreadable"),
)


@dataclass
class ImportResult:
    business_connection_id: str
    chat_id: int
    messages: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)
    first_at: datetime | None = None
    last_at: datetime | None = None
    skipped: Counter[str] = field(default_factory=Counter)


def load_export(path: Path, chat_id: int) -> list[dict[str, Any]]:
    """The messages of a one-to-one chat export; raises ValueError for any other file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} is not a JSON export: {exc}") from exc
    if isinstance(raw, dict) and "chats" in raw:
        raise ValueError("this is a full account export; export the one chat with Export chat history")
    if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
        raise ValueError(  # noqa: TRY004 - bad file content
            f"{path.name} is not a Telegram Desktop chat export (result.json in JSON format)")
    if raw.get("type") != EXPORT_CHAT_TYPE:
        raise ValueError(f"only a one-to-one chat can be imported; this export is a {raw.get('type')}")
    if raw.get("id") != chat_id:
        raise ValueError(f"this export is chat {raw.get('id')}, not {chat_id}")
    return [message for message in raw["messages"] if isinstance(message, dict)]


def _user_id(from_id: Any) -> int | None:
    if isinstance(from_id, str) and from_id.startswith("user") and from_id[4:].isdigit():
        return int(from_id[4:])
    return None


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _text(message: dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, str):
        return text
    if isinstance(text, list):  # plain strings mixed with {"type": ..., "text": ...} entities
        return "".join(part if isinstance(part, str) else str(part.get("text") or "")
                       for part in text if isinstance(part, str | dict))
    return ""


def _skip_reason(message: dict[str, Any], text: str, outbound: bool) -> str | None:
    if message.get("type") != "message":
        return "service"
    if message.get("media_type") in VOICE_MEDIA_TYPES:
        return "voice"
    if any(key in message for key in MEDIA_KEYS):
        return "media"
    if not text.strip():
        return "empty"
    plain = all(message.get(key) is None for key in ("forwarded_from", "via_bot"))
    if outbound and plain and (_history.is_dossier_command_text(text) or _STATS_COMMAND_RE.match(text)):
        return "command"
    return None


def export_owner(messages: Iterable[dict[str, Any]], chat_id: int, known_owner: str | None) -> int | None:
    """The account that made the export: the connection's known owner, else the contact's most frequent partner.

    A bot that posts in the chat (such as this one) is a third sender.
    """
    senders = Counter(
        sender
        for message in messages
        if (sender := _user_id(message.get("from_id"))) is not None and sender != chat_id
    )
    if known_owner is None:
        return senders.most_common(1)[0][0] if senders else None
    if senders and int(known_owner) not in senders:
        raise ValueError("the export was made by another account, not the owner of this connection")
    return int(known_owner)


def plan_import(
    messages: Iterable[dict[str, Any]],
    *,
    business_connection_id: str,
    chat_id: int,
    owner_id: int | None,
    saved_ids: set[str],
    history_start: datetime | None,
    retention_cutoff: datetime | None,
    chat_profile: dict[str, Any] | None,
) -> ImportResult:
    """Pure: the records to write, oldest first, and why the other messages are skipped.

    An edited message becomes a ``message.created`` without text at its send time, then a
    ``message.edited`` with the final text at its edit time: the export has no earlier text.
    """
    result = ImportResult(business_connection_id=business_connection_id, chat_id=chat_id)
    dated = []
    for message in messages:
        at = _timestamp(message.get("date_unixtime"))
        if at is None or not isinstance(message.get("id"), int):
            result.skipped["unreadable"] += 1
            continue
        dated.append((at, message))
    dated.sort(key=lambda item: (item[0], item[1]["id"]))
    for at, message in dated:
        message_id = message["id"]
        sender = _user_id(message.get("from_id"))
        text = _text(message)
        reason = _skip_reason(message, text, outbound=sender == owner_id)
        if reason is None and sender is None:
            reason = "unreadable"
        if reason is None and sender not in (chat_id, owner_id):
            reason = "other_sender"
        if reason is None and str(message_id) in saved_ids:
            reason = "saved"
        if reason is None and history_start is not None and at >= history_start:
            reason = "newer"
        if reason is None and retention_cutoff is not None and at < retention_cutoff:
            reason = "retention"
        if reason is not None:
            result.skipped[reason] += 1
            continue
        edited_at = _timestamp(message.get("edited_unixtime"))
        if edited_at is not None:
            edited_at = max(edited_at, at)
            if history_start is not None and edited_at >= history_start:
                edited_at = None  # an edit capture did not save: keep only the message
        base = {
            "source": IMPORT_SOURCE,
            "telegram_update_id": None,
            "business_connection_id": business_connection_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "message_at": at,
            "sender_id": sender,
            "direction": "inbound" if sender == chat_id else "outbound",
            "reply_to_message_id": message.get("reply_to_message_id"),
        }
        result.records.append(_history._build_event(
            event_type="message.created",
            observed_at=at,
            text=text if message.get("edited_unixtime") is None else None,
            chat_profile=chat_profile,
            **base,
        ))
        if edited_at is not None:
            result.records.append(_history._build_event(
                event_type="message.edited", observed_at=edited_at, text=text, chat_profile=chat_profile, **base))
        result.messages += 1
        result.first_at = result.first_at or at
        result.last_at = at
    result.records.sort(key=_observed_at)
    return result


def _observed_at(record: dict[str, Any]) -> datetime:
    return _history._parse_datetime(record.get("observed_at")) or datetime.min.replace(tzinfo=UTC)


def _saved_history(chat_dir: Path) -> tuple[set[str], datetime | None]:
    """Message ids already in this chat's history, and its first captured (not imported) record."""
    ids: set[str] = set()
    starts: list[datetime] = []

    def consume(record: dict[str, Any]) -> None:
        if record.get("message_id") is not None:
            ids.add(str(record["message_id"]))
        observed = _history._parse_datetime(record.get("observed_at"))
        if record.get("source") != IMPORT_SOURCE and observed is not None:
            starts[:] = [min(starts[0], observed)] if starts else [observed]

    _history._scan_records(chat_dir, repair_tails=False, on_record=consume)
    return ids, (starts[0] if starts else None)


def _line(record: dict[str, Any]) -> str:
    # The same serialization as history_support._append_record.
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _write_month(path: Path, records: list[dict[str, Any]]) -> None:
    """Add records (oldest first) to one month file so the file stays in observation order.

    A new file is written in one append. An existing one is rewritten through a temporary file
    and an atomic rename: each imported record goes before the first record observed after it.
    """
    if not path.exists() or path.stat().st_size == 0:
        _history._ensure_private_file(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.writelines(_line(record) for record in records)
            handle.flush()
            os.fsync(handle.fileno())
        return
    pending = deque(records)
    merged: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        observed = _history._parse_datetime(json.loads(line).get("observed_at")) if line.strip() else None
        while pending and observed is not None and _observed_at(pending[0]) < observed:
            merged.append(_line(pending.popleft()))
        merged.append(line)
    merged.extend(_line(record) for record in pending)
    tmp_path = path.with_name(f".{path.name}.import-tmp")  # not *.jsonl, so readers never see it
    descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.writelines(merged)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _history._fsync_parent_directory(path)


def _write_records(chat_dir: Path, records: list[dict[str, Any]]) -> None:
    by_month: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_month.setdefault(_history._month_filename(_observed_at(record)), []).append(record)
    for name in sorted(by_month):
        _write_month(chat_dir / name, by_month[name])


def _resolve_connection(chat_id: int, connection: str | None) -> tuple[str, bool]:
    """The chat's Business connection, and whether the chat already has saved history."""
    entries = _history._filter_catalog_entries(business_connection_id=connection, chat_id=str(chat_id))
    if len(entries) == 1:
        return str(entries[0].get("business_connection_id")), True
    if entries:
        raise ValueError(f"chat {chat_id} has history under several connections; pass --connection")
    known = {str(entry.get("business_connection_id")) for entry in _history._filter_catalog_entries()}
    if connection is not None:
        if connection not in known and _chat_settings.known_owner(connection) is None:
            raise ValueError(f"unknown connection {connection}: it has no saved history and no known owner")
        return connection, False
    if len(known) == 1:
        return known.pop(), False
    raise ValueError(f"chat {chat_id} has no saved history; pass --connection")


def run_import(path: Path, *, chat: str, connection: str | None = None, dry_run: bool = False,
               now: datetime | None = None) -> ImportResult:
    """Check the settings and the export, then import it (or, with ``dry_run``, only plan it)."""
    try:
        chat_id = int(chat)
    except ValueError:
        raise ValueError(f"--chat must be a numeric chat id, not {chat!r}") from None
    if _history.is_own_bot_chat(chat_id):
        raise ValueError("your chat with the bot is never saved, so it cannot be imported")
    config = _history.history_config_from_env()
    if not config.enabled:
        raise ValueError("history is off in Settings; turn on Save history first")
    messages = load_export(path, chat_id)
    business_connection_id, has_history = _resolve_connection(chat_id, connection)
    if _chat_settings.paused(business_connection_id, chat_id, "history"):
        raise ValueError(f"chat {chat_id} is set to Don't save; turn Save history on for it first")
    owner_id = export_owner(messages, chat_id, _chat_settings.known_owner(business_connection_id))
    current = now or datetime.now(UTC)
    retention_cutoff = current - timedelta(days=config.retention_days) if config.retention_days > 0 else None
    # Without saved history nothing names the chat type, so imported messages carry it.
    chat_profile = None if has_history else {"id": chat_id, "type": "private"}
    chat_dir = _history._history_chat_dir_path(business_connection_id, chat_id)

    def plan(saved_ids: set[str], history_start: datetime | None) -> ImportResult:
        return plan_import(messages, business_connection_id=business_connection_id, chat_id=chat_id,
                           owner_id=owner_id, saved_ids=saved_ids, history_start=history_start,
                           retention_cutoff=retention_cutoff, chat_profile=chat_profile)

    if chat_dir.exists():
        with _history._root_read_lock(), _history._chat_read_lock(chat_dir):
            preview = plan(*_saved_history(chat_dir))
    else:
        preview = plan(set(), None)
    if dry_run or not preview.records:
        return preview

    def mutate(state: _history.ChatState, catalog_records: list[dict[str, Any]]) -> ImportResult:
        result = plan(*_saved_history(chat_dir))
        _write_records(chat_dir, result.records)
        for record in result.records:
            _history._apply_record_to_state(state, record)
        catalog_records.extend(result.records)
        return result

    result, refresh_exc, _dirty_exc = _history._mutate_chat_history_transactionally(
        chat_dir, dirty_reason="history_import_refresh_failed", mutate=mutate)
    if refresh_exc is not None:
        raise ValueError(f"imported, but the contact catalog was not updated: {refresh_exc}; "
                         f"{_history._catalog_rebuild_instruction()}")
    return result


def _count(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def summary_lines(result: ImportResult, *, dry_run: bool) -> list[str]:
    """Counts and dates only, never message text."""
    target = f"chat {result.chat_id} (connection {result.business_connection_id})"
    if result.messages:
        verb = "Would import" if dry_run else "Imported"
        lines = [(f"{verb} {_count(result.messages, 'message', 'messages')} "
                  f"({_count(len(result.records), 'record', 'records')}) into {target}, "
                  f"sent {result.first_at:%Y-%m-%d} to {result.last_at:%Y-%m-%d} (UTC).")]
    else:
        lines = [f"Nothing to import into {target}."]
    skipped = [_count(result.skipped[key], one, many) for key, one, many in SKIP_LABELS if result.skipped[key]]
    if skipped:
        lines.append("Skipped: " + ", ".join(skipped) + ".")
    if dry_run:
        lines.append("Dry run: nothing was written.")
    return lines


def add_parser(history_subs: Any) -> None:
    parser = history_subs.add_parser("import", help="Import past messages of one chat from a Telegram Desktop export")
    parser.add_argument("file", help="result.json from Telegram Desktop's Export chat history, in JSON format")
    parser.add_argument("--chat", required=True, help="The chat id; it must match the export")
    parser.add_argument("--connection", help="The Business connection, when the chat has no saved history")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported; write nothing")
    parser.set_defaults(_history_action="import")


def handle_cli(args: argparse.Namespace) -> int:
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        result = run_import(Path(args.file).expanduser(), chat=str(args.chat),
                            connection=getattr(args, "connection", None), dry_run=dry_run)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    for line in summary_lines(result, dry_run=dry_run):
        print(line)
    return 0
