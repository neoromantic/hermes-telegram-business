"""Per-chat Telegram Business transcription modes.

One mode per (Business owner, chat) decides whose voice notes, round videos,
and attached audio are transcribed: ``both`` (default), ``own``, ``peer``, or
``off``. Modes live in a small private JSON file that the gateway re-reads
whenever it changes, so a CLI write or an owner-panel button applies to the
next message without a restart. The
file never stores message text, transcripts, or counters.

This module is stdlib-only and depends only on ``history_support``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import history_support as _history

logger = logging.getLogger(__name__)

MODES = _history.TRANSCRIBE_MODES
DEFAULT_MODE = _history.TRANSCRIBE_DEFAULT_MODE
# The owner panel writes "chat" too: 0.9.0 drops entries with any other value,
# so a new one here would lose panel-set modes on a rollback.
UPDATED_VIA = ("chat", "cli")
SCHEMA_VERSION = 1
MODES_FILENAME = "transcription-modes.json"
MAX_LABEL_CHARS = 128
MAX_CHATS_PER_OWNER = 5000
MODE_USAGE = "Usage: hermes telesec mode <list|get|set|reset>"

_OWNER_KEY_RE = re.compile(r"^\d+$")
_CHAT_KEY_RE = re.compile(r"^-?\d+$")
_CONNECTION_KEY_RE = re.compile(r"^[\x21-\x7e]{1,256}\Z")
_ENTRY_FIELDS = frozenset({"mode", "updated_at", "updated_via", "label"})

# Serializes writers inside one process; the lock file serializes processes.
_WRITE_LOCK = threading.Lock()


def modes_path() -> Path:
    return _history.get_hermes_home() / "data" / "telesec" / MODES_FILENAME


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@dataclass(frozen=True)
class ModeEntry:
    mode: str
    updated_at: str
    updated_via: str
    label: str | None


@dataclass(frozen=True)
class ModeWriteResult:
    entry: ModeEntry | None
    changed: bool
    backup_path: Path | None = None


class ModeStoreError(ValueError):
    """An invalid transcription-mode write request."""


class Snapshot(dict[str, dict[str, ModeEntry]]):
    """owner -> chat -> entry, plus ``connections``: Business connection id -> owner.

    A binding is recorded whenever the gateway learns a connection's owner (a
    panel change or a successful owner lookup), so a later failed lookup
    still applies that owner's entry and never another owner's. It is routing
    metadata, never message content.
    """

    def __init__(self, *args: Any, connections: dict[str, str] | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.connections: dict[str, str] = dict(connections or {})

    def owners(self) -> list[str]:
        """Every known owner: one with stored chats or one bound to a connection."""
        return sorted({*self, *self.connections.values()}, key=int)


_Signature = tuple[int, int, int]


class _CorruptModesFile(ValueError):
    pass


class ModesFileCorrupt(ValueError):
    """The modes file exists but cannot be parsed (raised by strict CLI reads)."""


def _parse_entry(value: Any) -> ModeEntry | None:
    if not isinstance(value, dict) or set(value) - _ENTRY_FIELDS:
        return None
    mode = value.get("mode")
    updated_at = value.get("updated_at")
    updated_via = value.get("updated_via")
    label = value.get("label")
    if mode not in MODES or updated_via not in UPDATED_VIA or not isinstance(updated_at, str):
        return None
    if label is not None and not isinstance(label, str):
        return None
    return ModeEntry(mode=mode, updated_at=updated_at, updated_via=updated_via, label=label)


def _parse_payload(raw: Any) -> tuple[Snapshot, int]:
    """Return (snapshot, skipped entry count); raise _CorruptModesFile on top-level damage."""
    if not isinstance(raw, dict):
        raise _CorruptModesFile("root is not an object")
    if raw.get("version") != SCHEMA_VERSION:
        raise _CorruptModesFile("unsupported version")
    owners = raw.get("owners")
    if not isinstance(owners, dict):
        raise _CorruptModesFile("owners is not an object")
    snapshot = Snapshot()
    skipped = 0
    for owner_key, owner_value in owners.items():
        if not isinstance(owner_value, dict) or not isinstance(owner_value.get("chats"), dict):
            raise _CorruptModesFile("owner chats is not an object")
        if not isinstance(owner_key, str) or not _OWNER_KEY_RE.match(owner_key):
            skipped += 1
            continue
        chats: dict[str, ModeEntry] = {}
        for chat_key, chat_value in owner_value["chats"].items():
            entry = _parse_entry(chat_value)
            if entry is None or not isinstance(chat_key, str) or not _CHAT_KEY_RE.match(chat_key):
                skipped += 1
                continue
            chats[chat_key] = entry
        snapshot[owner_key] = chats
    connections = raw.get("connections", {})
    if not isinstance(connections, dict):
        skipped += 1
        connections = {}
    for connection_key, owner_key in connections.items():
        if (
            not isinstance(connection_key, str)
            or not _CONNECTION_KEY_RE.match(connection_key)
            or not isinstance(owner_key, str)
            or not _OWNER_KEY_RE.match(owner_key)
        ):
            skipped += 1
            continue
        snapshot.connections[connection_key] = owner_key
    return snapshot, skipped


def _decode_file(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _CorruptModesFile(type(exc).__name__) from exc


def _parse_file(data: bytes) -> tuple[Snapshot, int]:
    """Parse file bytes; an empty or whitespace-only file holds no modes, like a missing one."""
    if not data.strip():
        return Snapshot(), 0
    return _parse_payload(_decode_file(data))


def _entry_payload(entry: ModeEntry) -> dict[str, Any]:
    return {
        "mode": entry.mode,
        "updated_at": entry.updated_at,
        "updated_via": entry.updated_via,
        "label": entry.label,
    }


def _snapshot_payload(snapshot: Snapshot) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "owners": {
            owner: {"chats": {chat: _entry_payload(entry) for chat, entry in chats.items()}}
            for owner, chats in snapshot.items()
        },
    }
    if snapshot.connections:
        payload["connections"] = dict(snapshot.connections)
    return payload


def _signature(st: os.stat_result) -> _Signature:
    # os.replace always installs a new inode, so a write from another process
    # is detected even when it lands within the same mtime tick and size.
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _format_timestamp(now: datetime | None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_owner(owner: Any) -> str:
    text = str(owner if owner is not None else "").strip()
    if not _OWNER_KEY_RE.match(text):
        raise ModeStoreError(f"owner must be a numeric Telegram user id, got {text!r}")
    return text


def _validate_chat(chat: Any) -> str:
    text = str(chat if chat is not None else "").strip()
    if not _CHAT_KEY_RE.match(text):
        raise ModeStoreError(f"chat must be a numeric Telegram chat id, got {text!r}")
    return text


def _validate_connection(connection: Any) -> str:
    text = str(connection if connection is not None else "")
    if not _CONNECTION_KEY_RE.match(text):
        raise ModeStoreError("business connection id must be 1-256 printable ASCII characters")
    return text


def _write_private_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a unique 0600 temp file, then atomically replace the target."""
    descriptor, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".transcription-modes.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        _history._fsync_parent_directory(path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    with _WRITE_LOCK:
        _history._ensure_private_dir(path.parent)
        lock_path = _lock_path(path)
        _history._ensure_private_file(lock_path)
        with _history._locked_existing_path(lock_path, shared=False):
            yield


def _backup_corrupt_file(path: Path, now: datetime | None) -> Path:
    current = now or datetime.now(UTC)
    stamp = current.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
        suffix += 1
    os.replace(path, backup)
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    return backup


def _fallback_note(snapshot: Snapshot) -> str:
    return "keeping the last valid modes" if snapshot or snapshot.connections else "every chat uses both"


class ModeStore:
    """Mode file reader with an mtime/inode-keyed cache and locked atomic writes."""

    def __init__(self, path_fn: Callable[[], Path] = modes_path):
        self._path_fn = path_fn
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[_Signature | None, Snapshot]] = {}
        self._stat_warned: set[tuple[str, str]] = set()
        # Paths whose file on disk is corrupt while the last valid modes stand in.
        self._corrupt: set[str] = set()
        # Paths whose last stat or read failed with an I/O error (never cached).
        self._unreadable: set[str] = set()

    @property
    def path(self) -> Path:
        return self._path_fn()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._stat_warned.clear()
            self._corrupt.clear()
            self._unreadable.clear()

    def snapshot(self) -> Snapshot:
        path = self._path_fn()
        key = str(path)
        try:
            signature: _Signature | None = _signature(os.stat(path))
        except FileNotFoundError:
            signature = None
        except OSError as exc:
            self._mark_unreadable(path, exc)
            return self._last_valid(key)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] == signature:
                self._unreadable.discard(key)
                return cached[1]
        try:
            snapshot = self._load(path) if signature is not None else Snapshot()
        except _CorruptModesFile as exc:
            # A damaged file keeps the last valid modes (none on a cold start).
            # Caching that under the damaged signature warns once per change.
            snapshot = self._last_valid(key)
            logger.warning(
                "transcription modes file %s unreadable (%s); %s",
                path,
                exc,
                _fallback_note(snapshot),
            )
            with self._lock:
                self._corrupt.add(key)
        except OSError as exc:
            # An I/O error (EMFILE, EACCES, EIO) is transient, so it is never
            # cached: the last valid modes apply and the next message reads again.
            self._mark_unreadable(path, exc)
            return self._last_valid(key)
        else:
            with self._lock:
                self._corrupt.discard(key)
        with self._lock:
            self._unreadable.discard(key)
            self._cache[key] = (signature, snapshot)
        return snapshot

    def file_degraded(self) -> bool:
        """True while the file on disk is corrupt or unreadable and the last valid modes stand in for it."""
        self.snapshot()
        key = str(self._path_fn())
        with self._lock:
            return key in self._corrupt or key in self._unreadable

    def _mark_unreadable(self, path: Path, exc: OSError) -> None:
        with self._lock:
            self._unreadable.add(str(path))
        self._warn_unreadable_once(path, exc)

    def _last_valid(self, key: str) -> Snapshot:
        with self._lock:
            cached = self._cache.get(key)
        return cached[1] if cached is not None else Snapshot()

    def read_strict(self) -> Snapshot:
        """CLI read: an I/O error or a corrupt file raises instead of reading as empty.

        The gateway keeps the fail-open, cached ``snapshot()``; the CLI must
        never report the default for a file it could not read.
        """
        path = self._path_fn()
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return Snapshot()
        try:
            return _parse_file(data)[0]
        except _CorruptModesFile as exc:
            raise ModesFileCorrupt(
                f"{path} is corrupt ({exc}); `mode set` or `mode reset` with --owner backs it up and starts empty"
            ) from exc

    def _warn_unreadable_once(self, path: Path, exc: OSError) -> None:
        warned = (str(path), type(exc).__name__)
        with self._lock:
            first = warned not in self._stat_warned
            self._stat_warned.add(warned)
        if first:
            logger.warning(
                "transcription modes file %s unreadable (%s); %s",
                path,
                type(exc).__name__,
                _fallback_note(self._last_valid(str(path))),
            )

    def _load(self, path: Path) -> Snapshot:
        """Parse the file; _CorruptModesFile and OSErrors other than a missing file propagate."""
        try:
            snapshot, skipped = _parse_file(path.read_bytes())
        except FileNotFoundError:
            return Snapshot()
        if skipped:
            logger.warning("transcription modes file %s: skipped %d invalid entries", path, skipped)
        return snapshot

    def get(self, owner: Any, chat: Any) -> ModeEntry | None:
        if owner is None or chat is None:
            return None
        return self.snapshot().get(str(owner), {}).get(str(chat))

    def needs_owner_lookup(self, chat_id: Any) -> bool:
        """True only when some owner set a non-default mode for this chat."""
        if chat_id is None:
            return False
        chat_key = str(chat_id)
        for chats in self.snapshot().values():
            entry = chats.get(chat_key)
            if entry is not None and entry.mode != DEFAULT_MODE:
                return True
        return False

    def known_owners(self) -> list[str]:
        return self.snapshot().owners()

    def connection_owner(self, business_connection_id: Any) -> str | None:
        """The owner last recorded for this Business connection, else None."""
        if not business_connection_id:
            return None
        return self.snapshot().connections.get(str(business_connection_id))

    def bind_connection(self, business_connection_id: Any, owner: Any) -> ModeWriteResult:
        owner_key = _validate_owner(owner)
        connection_key = _validate_connection(business_connection_id)

        def mutate(snapshot: Snapshot) -> tuple[ModeEntry | None, bool]:
            changed = snapshot.connections.get(connection_key) != owner_key
            snapshot.connections[connection_key] = owner_key
            return None, changed

        return self._write(mutate, now=None)

    def set_mode(
        self,
        owner: Any,
        chat: Any,
        mode: str,
        *,
        via: str,
        label: str | None = None,
        connection: Any = None,
        now: datetime | None = None,
        repair_corrupt: bool = False,
    ) -> ModeWriteResult:
        if mode not in MODES:
            raise ModeStoreError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        if via not in UPDATED_VIA:
            raise ModeStoreError(f"updated_via must be one of {', '.join(UPDATED_VIA)}, got {via!r}")
        owner_key = _validate_owner(owner)
        chat_key = _validate_chat(chat)
        connection_key = _validate_connection(connection) if connection is not None else None

        def mutate(snapshot: Snapshot) -> tuple[ModeEntry | None, bool]:
            if connection_key is not None:
                snapshot.connections[connection_key] = owner_key
            chats = snapshot.setdefault(owner_key, {})
            previous = chats.get(chat_key)
            if previous is None and len(chats) >= MAX_CHATS_PER_OWNER:
                raise ModeStoreError(f"owner already has {MAX_CHATS_PER_OWNER} chats with a stored mode")
            entry = ModeEntry(
                mode=mode,
                updated_at=_format_timestamp(now),
                updated_via=via,
                label=label if label is not None else (previous.label if previous is not None else None),
            )
            chats[chat_key] = entry
            return entry, True

        return self._write(mutate, now=now, repair_corrupt=repair_corrupt)

    def reset(
        self,
        owner: Any,
        chat: Any,
        *,
        connection: Any = None,
        now: datetime | None = None,
        repair_corrupt: bool = False,
    ) -> ModeWriteResult:
        owner_key = _validate_owner(owner)
        chat_key = _validate_chat(chat)
        connection_key = _validate_connection(connection) if connection is not None else None

        def mutate(snapshot: Snapshot) -> tuple[ModeEntry | None, bool]:
            if connection_key is not None:
                snapshot.connections[connection_key] = owner_key
            chats = snapshot.get(owner_key)
            if chats is None:
                # Never create an owner key on reset (a mistyped --owner).
                return None, False
            changed = chats.pop(chat_key, None) is not None
            if not chats and len(snapshot) > 1:
                # Keep a sole owner's empty key so the CLI can still infer the
                # owner later, but drop an emptied extra (e.g. mistyped) owner.
                del snapshot[owner_key]
            return None, changed

        return self._write(mutate, now=now, repair_corrupt=repair_corrupt)

    def _write(
        self,
        mutate: Callable[[Snapshot], tuple[ModeEntry | None, bool]],
        *,
        now: datetime | None,
        repair_corrupt: bool = False,
    ) -> ModeWriteResult:
        """Locked read-modify-write; a corrupt file is replaced only when ``repair_corrupt``."""
        path = self._path_fn()
        with _writer_lock(path):
            # Re-read inside the lock, ignoring the cache, so concurrent writers
            # in other processes never lose each other's updates.
            snapshot, backup_path = self._read_for_write(path, now=now, repair_corrupt=repair_corrupt)
            entry, changed = mutate(snapshot)
            _write_private_json_atomic(path, _snapshot_payload(snapshot))
            try:
                signature: _Signature | None = _signature(os.stat(path))
            except OSError:
                signature = None
            with self._lock:
                self._corrupt.discard(str(path))
                self._unreadable.discard(str(path))
                if signature is None:
                    self._cache.pop(str(path), None)
                else:
                    self._cache[str(path)] = (signature, snapshot)
        return ModeWriteResult(entry=entry, changed=changed, backup_path=backup_path)

    def _read_for_write(
        self, path: Path, *, now: datetime | None, repair_corrupt: bool
    ) -> tuple[Snapshot, Path | None]:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return Snapshot(), None
        try:
            snapshot, skipped = _parse_file(data)
        except _CorruptModesFile as exc:
            if not repair_corrupt:
                # Only an explicit CLI write may discard the damaged modes.
                raise ModesFileCorrupt(f"{path} is corrupt ({exc})") from exc
            backup = _backup_corrupt_file(path, now)
            logger.warning(
                "transcription modes file %s was corrupt (%s); backed up to %s and started empty",
                path,
                exc,
                backup,
            )
            return Snapshot(), backup
        if skipped:
            logger.warning("transcription modes file %s: dropping %d invalid entries on write", path, skipped)
        return snapshot, None


_STORE = ModeStore()


def default_store() -> ModeStore:
    return _STORE


def reset_in_memory_caches() -> None:
    _STORE.clear_cache()


@dataclass(frozen=True)
class PolicyDecision:
    transcribe: bool
    mode: str
    direction: str
    reason: str


def decide(mode: str, direction: str) -> PolicyDecision:
    """Pure policy table: mode x direction -> transcribe or skip, with a content-free reason."""
    if mode == "both":
        return PolicyDecision(True, mode, direction, "mode_both")
    if mode == "off":
        return PolicyDecision(False, mode, direction, "mode_off")
    if mode in ("own", "peer"):
        if direction == mode:
            return PolicyDecision(True, mode, direction, f"{mode}_message")
        if direction in ("own", "peer"):
            return PolicyDecision(False, mode, direction, f"{direction}_message_skipped")
        return PolicyDecision(True, mode, direction, "direction_unknown")
    return PolicyDecision(True, mode, direction, "mode_unknown")


_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _strip_control(text: str) -> str:
    return "".join(char for char in text if unicodedata.category(char) != "Cc" and char not in _BIDI_CONTROLS)


def _clean_label_part(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(_strip_control(value).split())
    return text or None


def build_chat_label(chat: Any) -> str | None:
    """Display-only snapshot of who the chat is with; never message content."""
    title = _clean_label_part(_history._get(chat, "title"))
    username = _clean_label_part(_history._get(chat, "username"))
    name = " ".join(
        part
        for part in (
            _clean_label_part(_history._get(chat, "first_name")),
            _clean_label_part(_history._get(chat, "last_name")),
        )
        if part
    )
    if title:
        label = title
    elif name and username:
        label = f"{name} (@{username})"
    elif name:
        label = name
    elif username:
        label = f"@{username}"
    else:
        return None
    return label[:MAX_LABEL_CHARS].strip() or None


def add_mode_parser(subs: Any) -> None:
    mode = subs.add_parser("mode", help="Show or change per-chat transcription modes")
    mode_subs = mode.add_subparsers(dest="telegram_business_mode_command")

    list_parser = mode_subs.add_parser("list", help="List chats with a stored transcription mode")
    list_parser.add_argument("--owner")
    list_parser.add_argument("--format", choices=["text", "json"], default="text")
    list_parser.set_defaults(_mode_action="list")

    get_parser = mode_subs.add_parser("get", help="Show the transcription mode for one chat")
    get_parser.add_argument("chat")
    get_parser.add_argument("--owner")
    get_parser.add_argument("--connection")
    get_parser.add_argument("--format", choices=["text", "json"], default="text")
    get_parser.set_defaults(_mode_action="get")

    set_parser = mode_subs.add_parser("set", help="Set the transcription mode for one chat")
    set_parser.add_argument("chat")
    set_parser.add_argument("mode", choices=list(MODES))
    set_parser.add_argument("--owner")
    set_parser.add_argument("--connection")
    set_parser.set_defaults(_mode_action="set")

    reset_parser = mode_subs.add_parser("reset", help="Return one chat to the default mode (both)")
    reset_parser.add_argument("chat")
    reset_parser.add_argument("--owner")
    reset_parser.add_argument("--connection")
    reset_parser.set_defaults(_mode_action="reset")


def _resolve_owner(owners: list[str], owner_arg: str | None, *, action: str) -> str | None:
    if owner_arg is not None:
        return _validate_owner(owner_arg)
    if action == "list":
        return owners[0] if len(owners) == 1 else None
    if not owners:
        if action in {"set", "reset"}:
            # The history catalog records no owner id, so the first write needs
            # one owner-panel change or --owner.
            raise ValueError(
                "no Business owner known yet; change a mode once in the owner panel "
                "(Manage Bot in a Business chat), or pass --owner USER_ID"
            )
        return None
    if len(owners) > 1:
        raise ValueError(f"multiple Business owners known ({', '.join(owners)}); pass --owner")
    return owners[0]


def _history_entries() -> list[dict[str, Any]]:
    """History catalog entries (none when history is off or the catalog cannot be read)."""
    try:
        if not _history.history_config_from_env().active:
            return []
        return _history._catalog_entries_for_cli()
    except Exception:  # noqa: BLE001 - the catalog only refines owner inference
        return []


def _history_connections() -> set[str]:
    """Distinct Business connections in the history catalog (none when history is off)."""
    return {
        str(entry["business_connection_id"]) for entry in _history_entries() if entry.get("business_connection_id")
    }


def _history_chat_connection(chat: str) -> str | None:
    """The Business connection history lists for a numeric chat_id, when exactly one does."""
    connections = {
        str(entry["business_connection_id"])
        for entry in _history_entries()
        if entry.get("business_connection_id") and str(entry.get("chat_id")) == chat
    }
    return connections.pop() if len(connections) == 1 else None


def _resolve_chat_owner(
    snapshot: Snapshot, owner_arg: str | None, connection: str | None, *, action: str
) -> str | None:
    """Owner for one chat: --owner, else the chat's recorded connection owner, else the one known owner.

    A single known owner is only inferred when every Business connection in
    history (plus ``connection``) is recorded for that owner, or there is just
    one connection: a connection with no recorded owner may be another owner's.
    """
    if owner_arg is not None:
        return _validate_owner(owner_arg)
    if connection is not None:
        bound = snapshot.connections.get(connection)
        if bound is not None:
            return bound
    owner = _resolve_owner(snapshot.owners(), None, action=action)
    if owner is None:
        return None
    connections = _history_connections() | ({connection} if connection is not None else set())
    if len(connections) > 1 and any(snapshot.connections.get(item) != owner for item in connections):
        if connection is not None:
            raise ValueError(f"no owner recorded for Business connection {connection}; pass --owner")
        raise ValueError(
            f"{len(connections)} Business connections found in history, "
            f"not all recorded for owner {owner}; pass --owner"
        )
    return owner


def _resolve_chat_selector(selector: str, connection: str | None) -> tuple[str, str | None, str | None]:
    """(chat_id, label, Business connection); the connection comes from --connection or history.

    A numeric chat_id takes its connection from history only when exactly one
    Business connection lists that chat.
    """
    text = str(selector).strip()
    if _CHAT_KEY_RE.match(text):
        return text, None, connection if connection is not None else _history_chat_connection(text)
    if not _history.history_config_from_env().active:
        raise ValueError(
            f"chat selector {text!r} is not a numeric chat_id; "
            "pass the chat_id or enable history to resolve names"
        )
    try:
        entry = _history._resolve_contact_entry(business_connection_id=connection, contact=text)
    except ValueError as exc:
        if "is ambiguous" in str(exc):
            raise ValueError(f"{exc} (pass the numeric chat_id or --connection)") from exc
        raise
    entry_connection = entry.get("business_connection_id")
    return (
        str(entry["chat_id"]),
        _history._catalog_entry_display_name(entry),
        str(entry_connection) if entry_connection else connection,
    )


def _mode_json(
    owner: str | None, chat: str, entry: ModeEntry | None, *, label: str | None = None, **extra: Any
) -> dict[str, Any]:
    """One chat for ``--format json``: the stored mode and the mode the next voice note gets.

    ``source`` is ``stored`` (an owner set it), ``default`` (no stored mode), or
    ``disabled`` (transcription is off on this gateway, so nothing is transcribed).
    """
    disabled = _history._truthy_env(_history.TRANSCRIBER_DISABLE_ENV)
    if disabled:
        effective, source = "off", "disabled"
    elif entry is not None:
        effective, source = entry.mode, "stored"
    else:
        effective, source = DEFAULT_MODE, "default"
    return {
        "owner": owner,
        "chat_id": chat,
        **extra,
        "mode": entry.mode if entry is not None else None,
        "effective_mode": effective,
        "source": source,
        "updated_via": entry.updated_via if entry is not None else None,
        "updated_at": entry.updated_at if entry is not None else None,
        "label": (entry.label if entry is not None else None) or label,
    }


def _print_json(document: Any) -> None:
    # ASCII only: labels are contact names, and escapes keep terminal controls inert.
    print(json.dumps(document, ensure_ascii=True, indent=2))


def _render_label(label: str | None) -> str:
    return "" if label is None else f" label={json.dumps(label, ensure_ascii=False)}"


def _print_backup_warning(result: ModeWriteResult) -> None:
    if result.backup_path is not None:
        print(f"warning: corrupt {MODES_FILENAME} backed up to {result.backup_path}")


def handle_mode_cli(args: argparse.Namespace, *, store: ModeStore | None = None) -> int:
    action = getattr(args, "_mode_action", None)
    if action is None:
        print(MODE_USAGE)
        return 1
    store = store or default_store()
    owner_arg = getattr(args, "owner", None)
    try:
        try:
            snapshot = store.read_strict()
        except ModesFileCorrupt:
            # An explicit-owner write is the documented repair: it backs the
            # corrupt file up and starts empty. Everything else reports it.
            if action not in {"set", "reset"} or owner_arg is None:
                raise
            snapshot = Snapshot()
        known = snapshot.owners()
        as_json = getattr(args, "format", None) == "json"
        if action == "list":
            owner = _resolve_owner(known, owner_arg, action=action)
            owners = [owner] if owner is not None else known
            chats = [
                (owner_key, chat_key, entry)
                for owner_key in owners
                for chat_key, entry in sorted(snapshot.get(owner_key, {}).items(), key=lambda item: int(item[0]))
            ]
            if as_json:
                _print_json(
                    {
                        "default_mode": DEFAULT_MODE,
                        "transcription_enabled": not _history._truthy_env(_history.TRANSCRIBER_DISABLE_ENV),
                        "chats": [_mode_json(owner_key, chat_key, entry) for owner_key, chat_key, entry in chats],
                    }
                )
                return 0
            rows = [
                f"owner={owner_key} chat={chat_key} mode={entry.mode} via={entry.updated_via} "
                f"updated_at={entry.updated_at}{_render_label(entry.label)}"
                for owner_key, chat_key, entry in chats
            ]
            if not rows:
                print("No transcription modes set; every chat uses both (default).")
            for row in rows:
                print(row)
            return 0

        chat, label, connection = _resolve_chat_selector(args.chat, getattr(args, "connection", None))
        owner = _resolve_chat_owner(snapshot, owner_arg, connection, action=action)
        if action == "get":
            entry = snapshot.get(owner, {}).get(chat) if owner is not None else None
            if as_json:
                _print_json(_mode_json(owner, chat, entry, label=label, business_connection_id=connection))
                return 0
            if entry is None:
                print(f"chat={chat} mode={DEFAULT_MODE} (default)")
            else:
                print(f"chat={chat} mode={entry.mode} (via {entry.updated_via} at {entry.updated_at})")
            return 0
        if action == "set":
            if known and owner not in known:
                print(f"warning: owner {owner} is not a known Business owner (known: {', '.join(known)})")
            result = store.set_mode(
                owner, chat, args.mode, via="cli", label=label, repair_corrupt=owner_arg is not None
            )
            _print_backup_warning(result)
            print(f"chat={chat} mode={args.mode}")
            return 0
        if action == "reset":
            result = store.reset(owner, chat, repair_corrupt=owner_arg is not None)
            _print_backup_warning(result)
            print(f"chat={chat} mode={DEFAULT_MODE} (default)")
            return 0
    except (ModeStoreError, OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1

    print(f"Unknown mode action: {action}")
    return 1
