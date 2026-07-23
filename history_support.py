"""Opt-in Telegram Business text history and maintenance helpers.

This module is stdlib-only by design so the history path never grows its own
dependency surface beyond Hermes/PTB at the integration boundary.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import heapq
import inspect
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

try:
    from hermes_constants import get_hermes_home
except ModuleNotFoundError as exc:
    if exc.name != "hermes_constants":
        raise

    def get_hermes_home() -> Path:
        configured = os.getenv("HERMES_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".hermes"


logger = logging.getLogger(__name__)

PLUGIN_NAME = "telegram-business-voice-transcriber"
HISTORY_ENABLE_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE"
HISTORY_CONNECTIONS_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS"
HISTORY_CHATS_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS"
HISTORY_CHAT_TYPES_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_CHAT_TYPES"
HISTORY_CORRECTION_WINDOW_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW"
HISTORY_NEARBY_BEFORE_SECONDS_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_NEARBY_BEFORE_SECONDS"
HISTORY_RETENTION_DAYS_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS"
HISTORY_MAX_BYTES_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_MAX_BYTES"

DEFAULT_CORRECTION_WINDOW_SECONDS = 120
DEFAULT_NEARBY_BEFORE_SECONDS = 15
DEFAULT_RETENTION_DAYS = 0
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_READ_LIMIT = 200
DEFAULT_CHAT_LIMIT = 100
MAX_READ_LIMIT = 5000
MAINTENANCE_THROTTLE_SECONDS = 3600
TAIL_SCAN_CHUNK_BYTES = 64 * 1024
SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 1
CATALOG_FILENAME = "contacts.json"
CATALOG_DIRTY_FILENAME = ".contacts.json.dirty"
KNOWN_CHAT_TYPES = frozenset({"private", "group", "supergroup", "channel"})
DEFAULT_HISTORY_CHAT_TYPES = frozenset({"private"})
CATALOG_REBUILD_RETRIES = 5
# Fixed chunk size for verify's external-sort duplicate audit. Each buffered item
# stores one event_id, its deterministic scan ordinal, and the chat label.
VERIFY_EVENT_ID_CHUNK_RECORDS = 5000
# Verify's retained-tombstone join uses the same fixed-size external-sort
# pattern as the duplicate audit. Diagnostics retain only the earliest entries
# in deterministic verify order so CLI output stays bounded.
VERIFY_REFERENCE_CHUNK_RECORDS = 5000
VERIFY_DIAGNOSTIC_LIMIT = 100
VERIFY_SPILL_MERGE_FAN_IN = 32
VERIFY_EVENT_ID_TEMP_DIR_PREFIX = ".verify-event-ids-"
VERIFY_REFERENCE_TOMBSTONE_TEMP_DIR_PREFIX = ".verify-reference-tombstones-"
VERIFY_REFERENCE_CLASSIFICATION_TEMP_DIR_PREFIX = ".verify-reference-classifications-"

_OWNER_CACHE: dict[str, str] = {}
_CACHE_LOCK = threading.RLock()
_CHAT_STATE_CACHE: dict[str, "ChatStateCacheEntry"] = {}
_DELETION_TIMERS: dict[tuple[str, str], "DeletionTimerEntry"] = {}
_LAST_MAINTENANCE_AT: datetime | None = None
MutationResult = TypeVar("MutationResult")


@dataclass(frozen=True)
class Scope:
    wildcard: bool
    values: frozenset[str]

    def matches(self, value: Any) -> bool:
        if self.wildcard:
            return True
        return str(value) in self.values

    def render(self) -> str:
        if self.wildcard:
            return "*"
        return ",".join(sorted(self.values))

    def contains_exact(self, value: Any) -> bool:
        return str(value) in self.values


@dataclass(frozen=True)
class HistoryConfig:
    enabled: bool
    connections: Scope | None
    chats: Scope | None
    chat_types: frozenset[str]
    correction_window_seconds: int = DEFAULT_CORRECTION_WINDOW_SECONDS
    nearby_before_seconds: int = DEFAULT_NEARBY_BEFORE_SECONDS
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_bytes: int = DEFAULT_MAX_BYTES

    @property
    def active(self) -> bool:
        return self.enabled

    def connection_allowed(self, business_connection_id: Any) -> bool:
        return self.connections is None or self.connections.matches(business_connection_id)

    def chat_in_scope(self, chat_id: Any) -> bool:
        return self.chats is None or self.chats.matches(chat_id)

    def exact_chat_allowed(self, chat_id: Any) -> bool:
        return self.chats is not None and self.chats.contains_exact(chat_id)

    def chat_type_allowed(self, chat_type: Any) -> bool:
        normalized = _normalize_chat_type(chat_type)
        return normalized in self.chat_types

    def allows(self, business_connection_id: Any, chat_id: Any, *, chat_type: Any, known_chat_type: Any = None) -> bool:
        if not self.active or not self.connection_allowed(business_connection_id) or not self.chat_in_scope(chat_id):
            return False
        if self.exact_chat_allowed(chat_id):
            return True
        normalized_type = _normalize_chat_type(chat_type) or _normalize_chat_type(known_chat_type)
        return normalized_type in self.chat_types


@dataclass
class MessageState:
    message_id: Any
    sender_id: Any
    direction: str
    text: str | None
    message_at: str | None
    reply_to_message_id: Any
    last_event_id: str
    last_observed_at: datetime | None
    deleted: bool = False
    deleted_event_id: str | None = None


@dataclass
class PendingDeletion:
    deleted_event: dict[str, Any] | None
    original: MessageState | None
    classification: dict[str, Any] | None = None


@dataclass
class ChatState:
    seen_event_ids: set[str] = field(default_factory=set)
    messages: dict[str, MessageState] = field(default_factory=dict)
    pending_deletions: dict[str, PendingDeletion] = field(default_factory=dict)
    record_count: int = 0
    latest_chat_profile: dict[str, Any] | None = None
    latest_sender_profile: dict[str, Any] | None = None


@dataclass
class _LivePendingDeletionTracker:
    live_deleted_event_ids: set[str] = field(default_factory=set)
    max_live_pending: int = 0

    def _note_size(self) -> None:
        self.max_live_pending = max(self.max_live_pending, len(self.live_deleted_event_ids))

    def observe_deleted(self, event_id: str) -> None:
        if not event_id:
            return
        self.live_deleted_event_ids.add(event_id)
        self._note_size()

    def observe_classified(self, deleted_event_id: str) -> bool:
        if not deleted_event_id:
            return False
        matched = deleted_event_id in self.live_deleted_event_ids
        self.live_deleted_event_ids.discard(deleted_event_id)
        self._note_size()
        return matched

    @property
    def pending_count(self) -> int:
        return len(self.live_deleted_event_ids)


@dataclass
class _ChatStreamScanSummary:
    file_snapshot: tuple[tuple[Path, int], ...]
    record_count: int
    pending_count: int
    unexplained_count: int
    repaired_files: int
    max_live_pending: int

    @property
    def file_count(self) -> int:
        return len(self.file_snapshot)

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self.file_snapshot)


@dataclass
class HistoryStats:
    chat_count: int
    file_count: int
    record_count: int
    total_bytes: int
    pending_count: int
    unexplained_count: int
    cap_exceeded: bool
    cap_shortfall_bytes: int


@dataclass
class MaintenanceResult:
    classified: int = 0
    pruned_files: int = 0
    pruned_bytes: int = 0
    cap_exceeded: bool = False
    cap_shortfall_bytes: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class PruneInventory:
    total_bytes: int
    active_files: tuple[Path, ...]
    protected_closed_files: tuple[Path, ...]
    unprotected_closed_files: tuple[Path, ...]


@dataclass
class VerificationResult:
    ok: bool
    chat_count: int
    file_count: int
    record_count: int
    repaired_files: int = 0
    error_count: int = 0
    warning_count: int = 0
    suppressed_error_count: int = 0
    suppressed_warning_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_EventIdChunkEntry = tuple[str, int, str]
_ReferenceTombstoneEntry = tuple[str, str]
_ReferenceClassificationEntry = tuple[str, str, int, str, str]
_SpillEntry = tuple[Any, ...]
_DiagnosticOrderKey = tuple[int, ...]


@dataclass
class _BoundedDiagnosticCollector:
    limit: int
    _entries: list[tuple[_DiagnosticOrderKey, str]] = field(default_factory=list)
    total_count: int = 0

    def add(self, *, order_key: _DiagnosticOrderKey, message: str) -> None:
        self.total_count += 1
        if self.limit <= 0:
            return
        if len(self._entries) == self.limit and order_key >= self._entries[-1][0]:
            return
        index = len(self._entries)
        while index > 0 and order_key < self._entries[index - 1][0]:
            index -= 1
        self._entries.insert(index, (order_key, message))
        if len(self._entries) > self.limit:
            self._entries.pop()

    @property
    def suppressed_count(self) -> int:
        return max(0, self.total_count - len(self._entries))

    def messages(self) -> list[str]:
        return [message for _order_key, message in self._entries]


@dataclass
class _SortedChunkSpill:
    chunk_limit: int
    temp_dir_prefix: str
    decode_entry: Callable[[Any, Path, int], _SpillEntry]
    fan_in: int = VERIFY_SPILL_MERGE_FAN_IN
    dedupe_identical: bool = False
    _buffer: list[_SpillEntry] = field(default_factory=list)
    _chunk_paths: list[Path] = field(default_factory=list)
    _temp_dir: tempfile.TemporaryDirectory[str] | None = None

    def add(self, entry: _SpillEntry) -> None:
        self._buffer.append(entry)
        if len(self._buffer) >= max(1, int(self.chunk_limit)):
            self._flush_chunk()

    def _ensure_temp_dir(self) -> Path:
        if self._temp_dir is None:
            root = history_root()
            _ensure_private_dir(root)
            self._temp_dir = tempfile.TemporaryDirectory(
                prefix=self.temp_dir_prefix,
                dir=root,
            )
            try:
                os.chmod(self._temp_dir.name, 0o700)
            except OSError:
                pass
        return Path(self._temp_dir.name)

    def _write_entries(self, entries: Iterable[_SpillEntry], *, prefix: str) -> Path:
        chunk_dir = self._ensure_temp_dir()
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=chunk_dir,
            prefix=prefix,
            suffix=".jsonl",
            delete=False,
        ) as handle:
            last_entry: _SpillEntry | None = None
            for entry in entries:
                if self.dedupe_identical and entry == last_entry:
                    continue
                json.dump(entry, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                last_entry = entry
            chunk_path = Path(handle.name)
        try:
            os.chmod(chunk_path, 0o600)
        except OSError:
            pass
        return chunk_path

    def _flush_chunk(self) -> None:
        if not self._buffer:
            return
        self._buffer.sort()
        self._chunk_paths.append(self._write_entries(self._buffer, prefix="chunk-"))
        self._buffer.clear()

    def _iter_chunk_entries(self, path: Path) -> Iterator[_SpillEntry]:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid spill chunk {path.name}:{line_no}: {exc}") from exc
                yield self.decode_entry(payload, path, line_no)

    def _merge_chunk_paths(self) -> None:
        fan_in = max(2, int(self.fan_in))
        pass_number = 0
        while len(self._chunk_paths) > fan_in:
            next_paths: list[Path] = []
            for start in range(0, len(self._chunk_paths), fan_in):
                batch = self._chunk_paths[start : start + fan_in]
                merged = self._write_entries(
                    heapq.merge(*(self._iter_chunk_entries(path) for path in batch)),
                    prefix=f"merge-{pass_number}-",
                )
                next_paths.append(merged)
                for path in batch:
                    try:
                        path.unlink()
                    except OSError:
                        pass
            self._chunk_paths = next_paths
            pass_number += 1

    def iter_sorted_entries(self) -> Iterator[_SpillEntry]:
        self._flush_chunk()
        if not self._chunk_paths:
            return iter(())
        self._merge_chunk_paths()
        return heapq.merge(*(self._iter_chunk_entries(path) for path in self._chunk_paths))

    def close(self) -> None:
        self._buffer.clear()
        self._chunk_paths.clear()
        if self._temp_dir is None:
            return
        self._temp_dir.cleanup()
        self._temp_dir = None


def _decode_event_id_chunk_entry(payload: Any, path: Path, line_no: int) -> _EventIdChunkEntry:
    if not isinstance(payload, list) or len(payload) != 3:
        raise ValueError(f"invalid spill chunk {path.name}:{line_no}")
    event_id, ordinal, chat_label = payload
    return str(event_id), int(ordinal), str(chat_label)


def _decode_reference_tombstone_entry(payload: Any, path: Path, line_no: int) -> _ReferenceTombstoneEntry:
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError(f"invalid spill chunk {path.name}:{line_no}")
    chat_label, event_id = payload
    return str(chat_label), str(event_id)


def _decode_reference_classification_entry(payload: Any, path: Path, line_no: int) -> _ReferenceClassificationEntry:
    if not isinstance(payload, list) or len(payload) != 5:
        raise ValueError(f"invalid spill chunk {path.name}:{line_no}")
    chat_label, deleted_event_id, ordinal, event_id, deleted_observed_at = payload
    return (
        str(chat_label),
        str(deleted_event_id),
        int(ordinal),
        str(event_id),
        str(deleted_observed_at or ""),
    )


@dataclass
class _EventIdDuplicateTracker:
    chunk_limit: int = VERIFY_EVENT_ID_CHUNK_RECORDS
    _spill: _SortedChunkSpill = field(init=False)

    def __post_init__(self) -> None:
        self._spill = _SortedChunkSpill(
            chunk_limit=self.chunk_limit,
            temp_dir_prefix=VERIFY_EVENT_ID_TEMP_DIR_PREFIX,
            decode_entry=_decode_event_id_chunk_entry,
            dedupe_identical=True,
        )

    def add(self, *, event_id: str, chat_label: str, ordinal: int) -> None:
        # The scan ordinal keeps duplicate occurrences exact. Defensive dedupe
        # happens only when identical triples reach the external spill.
        self._spill.add((event_id, ordinal, chat_label))

    def iter_duplicate_warnings(self) -> Iterator[tuple[int, str]]:
        entries = self._spill.iter_sorted_entries()
        current_event_id: str | None = None
        for event_id, ordinal, chat_label in entries:
            if event_id != current_event_id:
                current_event_id = event_id
                continue
            yield ordinal, f"{chat_label}: duplicate event_id {event_id}"

    def close(self) -> None:
        self._spill.close()


@dataclass
class _RetainedTombstoneReferenceTracker:
    chunk_limit: int = VERIFY_REFERENCE_CHUNK_RECORDS
    _retained_tombstones: _SortedChunkSpill = field(init=False)
    _classification_references: _SortedChunkSpill = field(init=False)

    def __post_init__(self) -> None:
        self._retained_tombstones = _SortedChunkSpill(
            chunk_limit=self.chunk_limit,
            temp_dir_prefix=VERIFY_REFERENCE_TOMBSTONE_TEMP_DIR_PREFIX,
            decode_entry=_decode_reference_tombstone_entry,
            dedupe_identical=True,
        )
        self._classification_references = _SortedChunkSpill(
            chunk_limit=self.chunk_limit,
            temp_dir_prefix=VERIFY_REFERENCE_CLASSIFICATION_TEMP_DIR_PREFIX,
            decode_entry=_decode_reference_classification_entry,
        )

    def observe_deleted(self, *, chat_label: str, event_id: str) -> None:
        if not event_id:
            return
        self._retained_tombstones.add((chat_label, event_id))

    def observe_classified(
        self,
        *,
        chat_label: str,
        deleted_event_id: str,
        ordinal: int,
        event_id: str,
        deleted_observed_at: str | None,
    ) -> None:
        self._classification_references.add(
            (
                chat_label,
                deleted_event_id,
                ordinal,
                event_id,
                str(deleted_observed_at or ""),
            )
        )

    def iter_missing_membership_warnings(self) -> Iterator[tuple[int, str]]:
        retained_iter = self._retained_tombstones.iter_sorted_entries()
        current_retained = next(retained_iter, None)
        for chat_label, deleted_event_id, ordinal, event_id, deleted_observed_at in (
            self._classification_references.iter_sorted_entries()
        ):
            reference_key = (chat_label, deleted_event_id)
            while current_retained is not None and current_retained < reference_key:
                current_retained = next(retained_iter, None)
            if current_retained == reference_key:
                continue
            if deleted_observed_at:
                yield (
                    ordinal,
                    f"{chat_label}: classification {event_id} refers to tombstone {deleted_event_id} "
                    "outside the retained archive",
                )
                continue
            yield ordinal, f"{chat_label}: classification {event_id} refers to a missing tombstone"

    def close(self) -> None:
        self._retained_tombstones.close()
        self._classification_references.close()


@dataclass(frozen=True)
class ChatFileSignature:
    files: tuple[tuple[str, int, int], ...]


@dataclass
class ChatStateCacheEntry:
    signature: ChatFileSignature
    state: ChatState


@dataclass
class DeletionTimerEntry:
    due_at: datetime
    timer: threading.Timer


class CatalogRebuildChangedError(ValueError):
    """Canonical history changed during every contact-catalog rebuild retry."""


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _safe_part(value: Any) -> str:
    text = str(value or "unknown")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._") or "unknown"


def _path_key(value: Any) -> str:
    raw = str(value or "unknown")
    safe = _safe_part(raw)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe}--{digest}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        if name in obj:
            return obj.get(name, default)
        api_kwargs = obj.get("api_kwargs")
        if isinstance(api_kwargs, dict) and name in api_kwargs:
            return api_kwargs.get(name, default)
        return default
    value = getattr(obj, name, default)
    if value is not default:
        return value
    api_kwargs = getattr(obj, "api_kwargs", None)
    if isinstance(api_kwargs, dict) and name in api_kwargs:
        return api_kwargs.get(name, default)
    return default


def _parse_scope(raw: str | None) -> Scope | None:
    if raw is None:
        return None
    values = [part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()]
    if not values:
        return None
    if "*" in values:
        return Scope(wildcard=True, values=frozenset())
    return Scope(wildcard=False, values=frozenset(values))


def _normalize_chat_type(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in KNOWN_CHAT_TYPES else None


def _parse_chat_types(raw: str | None) -> frozenset[str]:
    if raw is None or not raw.strip():
        return DEFAULT_HISTORY_CHAT_TYPES
    values = [part.strip().casefold() for part in re.split(r"[\s,]+", raw) if part.strip()]
    if not values:
        return DEFAULT_HISTORY_CHAT_TYPES
    if "*" in values:
        return KNOWN_CHAT_TYPES
    allowed = frozenset(value for value in values if value in KNOWN_CHAT_TYPES)
    return allowed


def history_config_from_env() -> HistoryConfig:
    correction_window = max(1, _env_int(HISTORY_CORRECTION_WINDOW_ENV, DEFAULT_CORRECTION_WINDOW_SECONDS))
    nearby_before_seconds = max(0, _env_int(HISTORY_NEARBY_BEFORE_SECONDS_ENV, DEFAULT_NEARBY_BEFORE_SECONDS))
    retention_days = max(0, _env_int(HISTORY_RETENTION_DAYS_ENV, DEFAULT_RETENTION_DAYS))
    max_bytes = max(1, _env_int(HISTORY_MAX_BYTES_ENV, DEFAULT_MAX_BYTES))
    return HistoryConfig(
        enabled=_truthy_env(HISTORY_ENABLE_ENV),
        connections=_parse_scope(os.environ.get(HISTORY_CONNECTIONS_ENV)),
        chats=_parse_scope(os.environ.get(HISTORY_CHATS_ENV)),
        chat_types=_parse_chat_types(os.environ.get(HISTORY_CHAT_TYPES_ENV)),
        correction_window_seconds=correction_window,
        nearby_before_seconds=nearby_before_seconds,
        retention_days=retention_days,
        max_bytes=max_bytes,
    )


def history_root() -> Path:
    return get_hermes_home() / "data" / "telegram-business" / "history"


def skill_path() -> Path:
    return Path(__file__).resolve().parent / "skills" / "history" / "SKILL.md"


def _catalog_path() -> Path:
    root = history_root()
    _ensure_private_dir(root)
    return root / CATALOG_FILENAME


def _catalog_dirty_path() -> Path:
    root = history_root()
    _ensure_private_dir(root)
    return root / CATALOG_DIRTY_FILENAME


def _missing_path_chain(path: Path) -> list[Path]:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    missing.reverse()
    return missing


def _ensure_private_dir(path: Path) -> bool:
    missing = _missing_path_chain(path)
    if not missing:
        path.mkdir(exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return False

    for current in missing:
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            if not current.is_dir():
                raise
        _fsync_parent_directory(current)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return True


def _ensure_private_file(path: Path) -> bool:
    created = not path.exists()
    if created:
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if not path.exists():
                raise
        else:
            os.close(descriptor)
    if path.is_dir():
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if created:
        _fsync_parent_directory(path)
    return created


def _fsync_parent_directory(path: Path) -> bool:
    if os.name == "nt":
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path.parent), flags)
    except OSError:
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno in {errno.EBADF, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                return False
            raise
    finally:
        os.close(descriptor)
    return True


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _fsync_parent_directory(path)


def _history_chat_dir_path(business_connection_id: Any, chat_id: Any) -> Path:
    root = history_root()
    connection_dir = root / _path_key(business_connection_id)
    return connection_dir / _safe_part(chat_id)


def _history_chat_dir(business_connection_id: Any, chat_id: Any) -> Path:
    root = history_root()
    connection_dir = root / _path_key(business_connection_id)
    chat_dir = _history_chat_dir_path(business_connection_id, chat_id)
    _ensure_private_dir(root)
    _ensure_private_dir(connection_dir)
    _ensure_private_dir(chat_dir)
    return chat_dir


def _chat_lock_path(chat_dir: Path) -> Path:
    return chat_dir / ".lock"


def _root_lock_path() -> Path:
    root = history_root()
    _ensure_private_dir(root)
    return root / ".lock"


@contextmanager
def _locked_path(path: Path) -> Iterator[None]:
    _ensure_private_dir(path.parent)
    _ensure_private_file(path)
    with path.open("r+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _chat_lock(chat_dir: Path) -> Iterator[None]:
    with _locked_path(_chat_lock_path(chat_dir)):
        yield


@contextmanager
def _root_lock() -> Iterator[None]:
    with _locked_path(_root_lock_path()):
        yield


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    text = value.strip()
    return text or None


def _normalize_lookup_text(value: str, *, strip_username_prefix: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.strip().split()).casefold()
    if strip_username_prefix:
        normalized = normalized.lstrip("@")
    return normalized


def _extract_profile_snapshot(source: Any, fields: tuple[str, ...]) -> dict[str, Any] | None:
    snapshot: dict[str, Any] = {}
    for field_name in fields:
        value = _get(source, field_name)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        snapshot[field_name] = value
    return snapshot or None


def _normalize_profile_snapshot(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    normalized: dict[str, Any] = {}
    for field_name, value in snapshot.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        normalized[str(field_name)] = value
    if "type" in normalized:
        normalized_type = _normalize_chat_type(normalized["type"])
        if normalized_type is not None:
            normalized["type"] = normalized_type
    return normalized or None


def _extract_chat_profile(message: Any) -> dict[str, Any] | None:
    snapshot = _extract_profile_snapshot(
        _get(message, "chat"),
        ("id", "type", "title", "username", "first_name", "last_name"),
    )
    return _normalize_profile_snapshot(snapshot)


def _extract_sender_profile(message: Any) -> dict[str, Any] | None:
    snapshot = _extract_profile_snapshot(
        _get(message, "from_user"),
        ("id", "is_bot", "username", "first_name", "last_name", "language_code"),
    )
    return _normalize_profile_snapshot(snapshot)


def _profile_lookup_values(profile: dict[str, Any] | None) -> list[str]:
    if not profile:
        return []
    values: list[str] = []
    username = _clean_optional_text(profile.get("username"))
    if username is not None:
        values.extend([f"@{username}", username])
    title = _clean_optional_text(profile.get("title"))
    if title is not None:
        values.append(title)
    parts = [part for part in (_clean_optional_text(profile.get("first_name")), _clean_optional_text(profile.get("last_name"))) if part]
    if parts:
        values.append(" ".join(parts))
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_lookup_text(value, strip_username_prefix=True)
        if normalized in seen:
            continue
        deduped.append(value)
        seen.add(normalized)
    return deduped


def _best_contact_profile(
    *,
    chat_profile: dict[str, Any] | None,
    sender_profile: dict[str, Any] | None,
    chat_id: Any,
) -> dict[str, Any] | None:
    if chat_profile is not None:
        return dict(chat_profile)
    if sender_profile is None:
        return None
    sender_id = sender_profile.get("id")
    if sender_id is None or str(sender_id) != str(chat_id):
        return None
    return dict(sender_profile)


def _catalog_contact_key(business_connection_id: Any, chat_id: Any) -> tuple[str, str]:
    return (str(business_connection_id), str(chat_id))


def _catalog_contacts_map(catalog: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in catalog.get("contacts", []):
        if not isinstance(entry, dict):
            continue
        entries[_catalog_contact_key(entry.get("business_connection_id"), entry.get("chat_id"))] = dict(entry)
    return entries


def _min_iso(left: str | None, right: str | None) -> str | None:
    left_dt = _parse_datetime(left)
    right_dt = _parse_datetime(right)
    if left_dt is None:
        return right
    if right_dt is None:
        return left
    return left if left_dt <= right_dt else right


def _max_iso(left: str | None, right: str | None) -> str | None:
    left_dt = _parse_datetime(left)
    right_dt = _parse_datetime(right)
    if left_dt is None:
        return right
    if right_dt is None:
        return left
    return left if left_dt >= right_dt else right


def _catalog_entry_display_name(entry: dict[str, Any]) -> str:
    profile = entry.get("current_profile")
    if isinstance(profile, dict):
        values = _profile_lookup_values(profile)
        if values:
            return values[0]
    return f"chat:{entry.get('chat_id')}"


def _catalog_entry_aliases(entry: dict[str, Any]) -> list[str]:
    aliases = entry.get("aliases")
    if not isinstance(aliases, list):
        return []
    return [alias for alias in aliases if isinstance(alias, str) and alias.strip()]


def _append_catalog_aliases(entry: dict[str, Any], profile: dict[str, Any] | None) -> None:
    aliases = _catalog_entry_aliases(entry)
    existing = {_normalize_lookup_text(alias, strip_username_prefix=True) for alias in aliases}
    for value in _profile_lookup_values(profile):
        normalized = _normalize_lookup_text(value, strip_username_prefix=True)
        if normalized in existing:
            continue
        aliases.append(value)
        existing.add(normalized)
    entry["aliases"] = aliases


def _prune_current_aliases(entry: dict[str, Any]) -> None:
    current_tokens = {
        _normalize_lookup_text(value, strip_username_prefix=True)
        for value in _profile_lookup_values(entry.get("current_profile"))
    }
    aliases: list[str] = []
    seen: set[str] = set()
    for alias in _catalog_entry_aliases(entry):
        normalized = _normalize_lookup_text(alias, strip_username_prefix=True)
        if normalized in current_tokens or normalized in seen:
            continue
        aliases.append(alias)
        seen.add(normalized)
    entry["aliases"] = aliases


def _empty_catalog_entry(*, business_connection_id: Any, chat_id: Any) -> dict[str, Any]:
    return {
        "business_connection_id": str(business_connection_id),
        "chat_id": chat_id,
        "chat_type": None,
        "current_profile": None,
        "chat_profile": None,
        "last_sender_profile": None,
        "aliases": [],
        "first_seen_at": None,
        "last_seen_at": None,
        "first_message_at": None,
        "last_message_at": None,
        "message_count": 0,
        "edit_count": 0,
        "deleted_count": 0,
        "record_count": 0,
        "unexplained_count": 0,
    }


def _apply_record_to_catalog_entry(entry: dict[str, Any], record: dict[str, Any]) -> None:
    observed_at = record.get("observed_at")
    message_at = record.get("message_at") or observed_at
    event_type = str(record.get("event_type") or "")

    entry["record_count"] = int(entry.get("record_count", 0)) + 1
    entry["first_seen_at"] = _min_iso(entry.get("first_seen_at"), observed_at)
    entry["last_seen_at"] = _max_iso(entry.get("last_seen_at"), observed_at)

    if event_type in {"message.created", "message.edited"}:
        entry["first_message_at"] = _min_iso(entry.get("first_message_at"), message_at)
        entry["last_message_at"] = _max_iso(entry.get("last_message_at"), message_at)
        if event_type == "message.created":
            entry["message_count"] = int(entry.get("message_count", 0)) + 1
        else:
            entry["edit_count"] = int(entry.get("edit_count", 0)) + 1

        chat_profile = _normalize_profile_snapshot(record.get("chat_profile"))
        sender_profile = _normalize_profile_snapshot(record.get("sender_profile"))
        next_current_profile = _best_contact_profile(
            chat_profile=chat_profile,
            sender_profile=sender_profile,
            chat_id=entry.get("chat_id"),
        )
        current_profile = _normalize_profile_snapshot(entry.get("current_profile"))
        if next_current_profile is not None and current_profile is not None and current_profile != next_current_profile:
            _append_catalog_aliases(entry, current_profile)
        if chat_profile is not None:
            entry["chat_profile"] = chat_profile
            entry["chat_type"] = _normalize_chat_type(chat_profile.get("type")) or entry.get("chat_type")
        if sender_profile is not None:
            entry["last_sender_profile"] = sender_profile
        if next_current_profile is not None:
            entry["current_profile"] = next_current_profile
        _prune_current_aliases(entry)
        return

    if event_type == "message.deleted":
        entry["deleted_count"] = int(entry.get("deleted_count", 0)) + 1
        return

    if event_type == "deletion.classified" and str(record.get("classification") or "") == "unexplained":
        entry["unexplained_count"] = int(entry.get("unexplained_count", 0)) + 1


def _empty_history_source_signature() -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(b"").hexdigest(),
        "file_count": 0,
        "total_bytes": 0,
    }


def _history_source_signature() -> dict[str, Any]:
    root = history_root()
    if not root.exists():
        return _empty_history_source_signature()
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for chat_dir in _iter_chat_dirs():
        for path in _iter_history_files(chat_dir):
            try:
                stat_result = path.stat()
            except OSError:
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_mtime_ns).encode("ascii"))
            digest.update(b"\n")
            file_count += 1
            total_bytes += stat_result.st_size
    return {
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _normalize_history_source_signature(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    sha256 = value.get("sha256")
    file_count = value.get("file_count")
    total_bytes = value.get("total_bytes")
    if not isinstance(sha256, str) or not sha256:
        return None
    if not isinstance(file_count, int) or file_count < 0:
        return None
    if not isinstance(total_bytes, int) or total_bytes < 0:
        return None
    return {
        "sha256": sha256,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _catalog_source_signature(catalog: dict[str, Any]) -> dict[str, Any] | None:
    return _normalize_history_source_signature(catalog.get("source_signature"))


def _catalog_is_fresh_locked(catalog: dict[str, Any]) -> bool:
    stored_signature = _catalog_source_signature(catalog)
    if stored_signature is None:
        return False
    return stored_signature == _history_source_signature()


def _catalog_from_entries(
    entries: dict[tuple[str, str], dict[str, Any]],
    *,
    generated_at: datetime | None = None,
    source_signature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contacts = sorted(entries.values(), key=lambda entry: (str(entry.get("business_connection_id")), str(entry.get("chat_id"))))
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": _isoformat_utc(generated_at or _utcnow()),
        "source_signature": _normalize_history_source_signature(source_signature) or _history_source_signature(),
        "contact_count": len(contacts),
        "contacts": contacts,
    }


def _read_catalog_locked() -> dict[str, Any] | None:
    path = _catalog_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read {path.name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"invalid {path.name}: top-level value must be an object")
    contacts = raw.get("contacts")
    if not isinstance(contacts, list):
        raise ValueError(f"invalid {path.name}: contacts must be a list")
    if raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported {path.name} schema_version {raw.get('schema_version')}"
        )
    return raw


def _catalog_dirty_locked() -> bool:
    return _catalog_dirty_path().exists()


def _mark_catalog_dirty_locked(reason: str) -> None:
    _write_json_atomic(
        _catalog_dirty_path(),
        {
            "marked_at": _isoformat_utc(_utcnow()),
            "reason": str(reason),
        },
    )


def _clear_catalog_dirty_locked() -> None:
    path = _catalog_dirty_path()
    if not path.exists():
        return
    path.unlink()
    _fsync_parent_directory(path)


def _write_catalog_locked(catalog: dict[str, Any]) -> None:
    _write_json_atomic(_catalog_path(), catalog)
    _clear_catalog_dirty_locked()


def _rebuild_catalog_locked(*, repair_tails: bool) -> dict[str, Any]:
    for _attempt in range(CATALOG_REBUILD_RETRIES):
        source_signature = _history_source_signature()
        entries: dict[tuple[str, str], dict[str, Any]] = {}
        for chat_dir in _iter_chat_dirs():
            with _chat_lock(chat_dir):
                _scan_records(
                    chat_dir,
                    repair_tails=repair_tails,
                    on_record=lambda record: _apply_record_to_catalog_entry(
                        entries.setdefault(
                            _catalog_contact_key(record.get("business_connection_id"), record.get("chat_id")),
                            _empty_catalog_entry(
                                business_connection_id=record.get("business_connection_id"),
                                chat_id=record.get("chat_id"),
                            ),
                        ),
                        record,
                    ),
                )
        if _history_source_signature() != source_signature:
            continue
        catalog = _catalog_from_entries(entries, source_signature=source_signature)
        _write_catalog_locked(catalog)
        return catalog
    raise CatalogRebuildChangedError("canonical history changed during catalog rebuild")


def rebuild_contact_catalog() -> dict[str, Any]:
    with _root_lock():
        return _rebuild_catalog_locked(repair_tails=True)


def _load_contact_catalog(
    *,
    rebuild_on_missing: bool,
    rebuild_on_corrupt: bool,
    rebuild_on_dirty: bool,
    repair_tails: bool,
) -> tuple[dict[str, Any], bool]:
    with _root_lock():
        dirty = _catalog_dirty_locked()
        try:
            catalog = _read_catalog_locked()
        except ValueError:
            if not rebuild_on_corrupt:
                raise
            return _rebuild_catalog_locked(repair_tails=repair_tails), True
        if catalog is None:
            if not rebuild_on_missing:
                return _catalog_from_entries({}), False
            return _rebuild_catalog_locked(repair_tails=repair_tails), True
        if dirty and rebuild_on_dirty:
            return _rebuild_catalog_locked(repair_tails=repair_tails), True
        if not _catalog_is_fresh_locked(catalog):
            return _rebuild_catalog_locked(repair_tails=repair_tails), True
        return catalog, False


def _refresh_contact_catalog_locked(
    records: Iterable[dict[str, Any]],
    *,
    base_source_signature: dict[str, Any] | None = None,
) -> None:
    materialized = [record for record in records if isinstance(record, dict)]
    if not materialized:
        return
    try:
        catalog = _read_catalog_locked()
    except ValueError:
        _rebuild_catalog_locked(repair_tails=True)
        return
    if catalog is None:
        _rebuild_catalog_locked(repair_tails=True)
        return
    normalized_base_signature = _normalize_history_source_signature(base_source_signature)
    if normalized_base_signature is not None and _catalog_source_signature(catalog) != normalized_base_signature:
        _rebuild_catalog_locked(repair_tails=True)
        return
    entries = _catalog_contacts_map(catalog)
    for record in materialized:
        entry = entries.setdefault(
            _catalog_contact_key(record.get("business_connection_id"), record.get("chat_id")),
            _empty_catalog_entry(
                business_connection_id=record.get("business_connection_id"),
                chat_id=record.get("chat_id"),
            ),
        )
        _apply_record_to_catalog_entry(entry, record)
    _write_catalog_locked(_catalog_from_entries(entries, source_signature=_history_source_signature()))


def _refresh_contact_catalog(
    records: Iterable[dict[str, Any]],
    *,
    base_source_signature: dict[str, Any] | None = None,
) -> None:
    with _root_lock():
        _refresh_contact_catalog_locked(records, base_source_signature=base_source_signature)


def _mark_catalog_dirty(reason: str) -> None:
    with _root_lock():
        _mark_catalog_dirty_locked(reason)


def _refresh_contact_catalog_best_effort_locked(
    records: Iterable[dict[str, Any]],
    *,
    dirty_reason: str,
    base_source_signature: dict[str, Any] | None = None,
) -> tuple[Exception | None, Exception | None]:
    try:
        _refresh_contact_catalog_locked(records, base_source_signature=base_source_signature)
    except Exception as exc:  # noqa: BLE001 - caller decides how to surface catalog drift
        try:
            _mark_catalog_dirty_locked(dirty_reason)
        except Exception as dirty_exc:  # noqa: BLE001 - surface both failures to the caller
            return exc, dirty_exc
        return exc, None
    return None, None


def _refresh_contact_catalog_best_effort(
    records: Iterable[dict[str, Any]],
    *,
    dirty_reason: str,
    base_source_signature: dict[str, Any] | None = None,
) -> tuple[Exception | None, Exception | None]:
    with _root_lock():
        return _refresh_contact_catalog_best_effort_locked(
            records,
            dirty_reason=dirty_reason,
            base_source_signature=base_source_signature,
        )


def _known_chat_type_from_catalog(
    business_connection_id: Any,
    chat_id: Any,
) -> str | None:
    with _root_lock():
        if _catalog_dirty_locked():
            return None
        try:
            catalog = _read_catalog_locked()
        except ValueError:
            return None
        if catalog is None:
            return None
        if not _catalog_is_fresh_locked(catalog):
            return None
        for entry in catalog.get("contacts", []):
            if not isinstance(entry, dict):
                continue
            if _catalog_contact_key(entry.get("business_connection_id"), entry.get("chat_id")) != _catalog_contact_key(
                business_connection_id,
                chat_id,
            ):
                continue
            return _normalize_chat_type(entry.get("chat_type")) or _normalize_chat_type(
                _get(entry.get("current_profile"), "type")
            )
    return None


def _known_chat_type_from_history(
    business_connection_id: Any,
    chat_id: Any,
) -> str | None:
    chat_dir = _history_chat_dir_path(business_connection_id, chat_id)
    if not chat_dir.exists():
        return None
    with _chat_lock(chat_dir):
        try:
            state, _ = _load_chat_state(chat_dir, repair_tails=False)
        except ValueError as exc:
            raise ValueError(f"{chat_dir}: {exc}") from exc
    return _normalize_chat_type(_get(state.latest_chat_profile, "type"))


def _known_chat_type_for_capture(
    business_connection_id: Any,
    chat_id: Any,
) -> str | None:
    return _known_chat_type_from_catalog(business_connection_id, chat_id) or _known_chat_type_from_history(
        business_connection_id,
        chat_id,
    )


def _month_filename(observed_at: datetime) -> str:
    return observed_at.astimezone(timezone.utc).strftime("%Y-%m") + ".jsonl"


def _month_sort_key(path: Path) -> tuple[str, str]:
    return (path.stem, path.name)


def _iter_history_files(chat_dir: Path) -> list[Path]:
    return sorted(
        [path for path in chat_dir.iterdir() if path.is_file() and path.suffix == ".jsonl"],
        key=_month_sort_key,
    )


def _history_file_snapshot(paths: Iterable[Path]) -> tuple[tuple[Path, int], ...]:
    return tuple((path, path.stat().st_size) for path in paths)


def _cache_key(chat_dir: Path) -> str:
    return str(chat_dir.resolve())


def _copy_message_state(message: MessageState) -> MessageState:
    return MessageState(
        message_id=message.message_id,
        sender_id=message.sender_id,
        direction=message.direction,
        text=message.text,
        message_at=message.message_at,
        reply_to_message_id=message.reply_to_message_id,
        last_event_id=message.last_event_id,
        last_observed_at=message.last_observed_at,
        deleted=message.deleted,
        deleted_event_id=message.deleted_event_id,
    )


def _chat_file_signature(chat_dir: Path) -> ChatFileSignature:
    if not chat_dir.exists():
        return ChatFileSignature(files=())
    files = []
    for path in _iter_history_files(chat_dir):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        files.append((path.name, stat_result.st_size, stat_result.st_mtime_ns))
    return ChatFileSignature(files=tuple(files))


def _invalidate_chat_cache(chat_dir: Path) -> None:
    with _CACHE_LOCK:
        _CHAT_STATE_CACHE.pop(_cache_key(chat_dir), None)


def _store_chat_cache(chat_dir: Path, state: ChatState) -> None:
    with _CACHE_LOCK:
        _CHAT_STATE_CACHE[_cache_key(chat_dir)] = ChatStateCacheEntry(
            signature=_chat_file_signature(chat_dir),
            state=state,
        )


def _tail_scan_last_newline_offset(handle: Any) -> int:
    end = handle.seek(0, os.SEEK_END)
    position = end
    trailing = b""
    while position > 0:
        chunk_size = min(TAIL_SCAN_CHUNK_BYTES, position)
        position -= chunk_size
        handle.seek(position)
        chunk = handle.read(chunk_size)
        combined = chunk + trailing
        newline = combined.rfind(b"\n")
        if newline >= 0:
            return position + newline
        trailing = combined[: TAIL_SCAN_CHUNK_BYTES - 1]
    return -1


def _file_has_complete_final_line(path: Path) -> bool:
    if not path.exists():
        return True
    with path.open("rb") as handle:
        end = handle.seek(0, os.SEEK_END)
        if end == 0:
            return True
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"


def _tail_repair_instruction() -> str:
    return "run 'hermes telegram-business history verify --repair-tails'"


def _tail_repair_required_message(path: Path) -> str:
    return f"{path.name} has a torn or invalid tail; {_tail_repair_instruction()}"


def _raise_if_tail_repair_required(path: Path) -> None:
    if not _file_has_complete_final_line(path):
        raise ValueError(_tail_repair_required_message(path))


def _repair_torn_tail(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("r+b") as handle:
        end = handle.seek(0, os.SEEK_END)
        if end == 0:
            return False
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return False
        last_newline = _tail_scan_last_newline_offset(handle)
        tail_start = last_newline + 1 if last_newline >= 0 else 0
        handle.seek(tail_start)
        tail_bytes = handle.read(end - tail_start)
        repair_by_appending_newline = False
        try:
            payload = json.loads(tail_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        else:
            repair_by_appending_newline = isinstance(payload, dict)
        if repair_by_appending_newline:
            handle.seek(0, os.SEEK_END)
            handle.write(b"\n")
        else:
            handle.truncate(tail_start)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _scan_history_file(path: Path, *, on_record: Callable[[dict[str, Any]], None]) -> None:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path.name}:{line_no}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"non-object record in {path.name}:{line_no}")
            on_record(record)


def _scan_history_files(
    paths: Iterable[Path],
    *,
    repair_tails: bool,
    on_record: Callable[[dict[str, Any]], None],
) -> int:
    repaired = 0
    for path in paths:
        if repair_tails:
            if _repair_torn_tail(path):
                repaired += 1
        else:
            _raise_if_tail_repair_required(path)
        _scan_history_file(path, on_record=on_record)
    return repaired


def _scan_records(
    chat_dir: Path,
    *,
    repair_tails: bool,
    on_record: Callable[[dict[str, Any]], None],
) -> int:
    return _scan_history_files(_iter_history_files(chat_dir), repair_tails=repair_tails, on_record=on_record)


def _load_records(chat_dir: Path, *, repair_tails: bool = False) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    repaired = _scan_records(chat_dir, repair_tails=repair_tails, on_record=records.append)
    return records, repaired


def _record_signature(record: dict[str, Any]) -> dict[str, Any]:
    stable = {
        "schema_version": record["schema_version"],
        "event_type": record["event_type"],
        "source": record.get("source"),
        "telegram_update_id": record.get("telegram_update_id"),
        "business_connection_id": record["business_connection_id"],
        "chat_id": record["chat_id"],
        "message_id": record.get("message_id"),
        "message_at": record.get("message_at"),
        "sender_id": record.get("sender_id"),
        "direction": record.get("direction"),
        "reply_to_message_id": record.get("reply_to_message_id"),
        "text": record.get("text"),
        "deleted_event_id": record.get("deleted_event_id"),
        "classification": record.get("classification"),
        "replacement_message_id": record.get("replacement_message_id"),
        "classification_reason": record.get("classification_reason"),
        "classification_score": record.get("classification_score"),
    }
    return stable


def _event_id(record: dict[str, Any]) -> str:
    payload = json.dumps(_record_signature(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _legacy_replay_event_id(record: dict[str, Any]) -> str | None:
    event_id = str(record.get("event_id") or "")
    if not event_id:
        return None
    raw_direction = str(record.get("direction") or "").strip().casefold()
    if raw_direction == "incoming":
        canonical_direction = "inbound"
    elif raw_direction == "outgoing":
        canonical_direction = "outbound"
    else:
        return None
    try:
        replay_record = dict(record)
        replay_record["direction"] = canonical_direction
        replay_event_id = _event_id(replay_record)
    except KeyError:
        return None
    if replay_event_id == event_id:
        return None
    return replay_event_id


def _build_event(
    *,
    event_type: str,
    source: str,
    observed_at: datetime,
    telegram_update_id: Any,
    business_connection_id: Any,
    chat_id: Any,
    message_id: Any,
    message_at: datetime | str | None,
    sender_id: Any,
    direction: str,
    reply_to_message_id: Any,
    text: str | None = None,
    chat_profile: dict[str, Any] | None = None,
    sender_profile: dict[str, Any] | None = None,
    deleted_event_id: str | None = None,
    classification: str | None = None,
    replacement_message_id: Any = None,
    classification_reason: str | None = None,
    classification_method: str | None = None,
    classification_score: float | None = None,
    evaluated_at: datetime | None = None,
    deleted_observed_at: datetime | str | None = None,
) -> dict[str, Any]:
    if classification_reason is None:
        classification_reason = classification_method
    if classification_method is None and classification_reason is not None:
        classification_method = classification_reason
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "source": str(source),
        "observed_at": _isoformat_utc(observed_at),
        "telegram_update_id": telegram_update_id,
        "business_connection_id": str(business_connection_id),
        "chat_id": chat_id,
        "message_id": message_id,
        "message_at": _isoformat_utc(_normalize_timestamp(message_at) if not isinstance(message_at, str) else _parse_datetime(message_at)),
        "sender_id": sender_id,
        "direction": _normalize_history_direction(direction),
        "reply_to_message_id": reply_to_message_id,
    }
    if text is not None:
        record["text"] = text
    normalized_chat_profile = _normalize_profile_snapshot(chat_profile)
    if normalized_chat_profile is not None:
        record["chat_profile"] = normalized_chat_profile
    normalized_sender_profile = _normalize_profile_snapshot(sender_profile)
    if normalized_sender_profile is not None:
        record["sender_profile"] = normalized_sender_profile
    if deleted_event_id is not None:
        record["deleted_event_id"] = deleted_event_id
    if classification is not None:
        record["classification"] = classification
    if replacement_message_id is not None:
        record["replacement_message_id"] = replacement_message_id
    if classification_reason is not None:
        record["classification_reason"] = classification_reason
    if classification_method is not None:
        record["classification_method"] = classification_method
    if classification_score is not None:
        record["classification_score"] = round(float(classification_score), 4)
    if evaluated_at is not None:
        record["evaluated_at"] = _isoformat_utc(evaluated_at)
    if deleted_observed_at is not None:
        record["deleted_observed_at"] = _isoformat_utc(
            _normalize_timestamp(deleted_observed_at)
            if not isinstance(deleted_observed_at, str)
            else _parse_datetime(deleted_observed_at)
        )
    record["event_id"] = _event_id(record)
    return record


def _append_record(chat_dir: Path, record: dict[str, Any]) -> bool:
    # Low-level append primitive. Production writers go through
    # _mutate_chat_history_transactionally() so root -> chat locking and catalog
    # publish/dirty handling stay in one transaction. Tests and fixture/repair
    # helpers may still call this primitive or inject raw JSONL directly.
    observed_at = _parse_datetime(record.get("observed_at")) or _utcnow()
    path = chat_dir / _month_filename(observed_at)
    _ensure_private_file(path)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def _append_record_to_state(
    chat_dir: Path,
    state: ChatState,
    record: dict[str, Any],
    *,
    appended_records: list[dict[str, Any]] | None = None,
) -> bool:
    event_id = str(record.get("event_id") or "")
    if event_id and event_id in state.seen_event_ids:
        return False
    _append_record(chat_dir, record)
    if appended_records is not None:
        appended_records.append(record)
    _apply_record_to_state(state, record)
    return True


def _mutate_chat_history_transactionally(
    chat_dir: Path,
    *,
    dirty_reason: str,
    mutate: Callable[[ChatState, list[dict[str, Any]]], MutationResult],
) -> tuple[MutationResult, Exception | None, Exception | None]:
    # Production canonical writers must hold root before chat so every append and
    # its derived catalog publish/dirty handling become one root-scoped commit.
    with _root_lock():
        base_source_signature = _history_source_signature()
        catalog_records: list[dict[str, Any]] = []
        with _chat_lock(chat_dir):
            state, _ = _load_chat_state(chat_dir, repair_tails=True)
            result = mutate(state, catalog_records)
            if catalog_records:
                _store_chat_cache(chat_dir, state)
        if not catalog_records:
            return result, None, None
        refresh_exc, dirty_exc = _refresh_contact_catalog_best_effort_locked(
            catalog_records,
            dirty_reason=dirty_reason,
            base_source_signature=base_source_signature,
        )
        return result, refresh_exc, dirty_exc


def _apply_record_to_state(state: ChatState, record: dict[str, Any]) -> None:
    event_id = str(record.get("event_id") or "")
    if event_id:
        state.seen_event_ids.add(event_id)
        legacy_replay_event_id = _legacy_replay_event_id(record)
        if legacy_replay_event_id is not None:
            state.seen_event_ids.add(legacy_replay_event_id)
    state.record_count += 1
    event_type = str(record.get("event_type") or "")
    message_key = str(record.get("message_id"))
    observed_at = _parse_datetime(record.get("observed_at"))
    if event_type in {"message.created", "message.edited"}:
        state.messages[message_key] = MessageState(
            message_id=record.get("message_id"),
            sender_id=record.get("sender_id"),
            direction=_normalize_history_direction(record.get("direction")),
            text=record.get("text"),
            message_at=record.get("message_at"),
            reply_to_message_id=record.get("reply_to_message_id"),
            last_event_id=event_id,
            last_observed_at=observed_at,
            deleted=False,
            deleted_event_id=None,
        )
        chat_profile = _normalize_profile_snapshot(record.get("chat_profile"))
        sender_profile = _normalize_profile_snapshot(record.get("sender_profile"))
        if chat_profile is not None:
            state.latest_chat_profile = chat_profile
        if sender_profile is not None:
            state.latest_sender_profile = sender_profile
        return
    if event_type == "message.deleted":
        original = state.messages.get(message_key)
        state.pending_deletions[event_id] = PendingDeletion(
            deleted_event=record,
            original=None if original is None else _copy_message_state(original),
        )
        if original is not None:
            original.deleted = True
            original.deleted_event_id = event_id
        return
    if event_type == "deletion.classified":
        deleted_event_id = str(record.get("deleted_event_id") or "")
        if not deleted_event_id:
            return
        pending = state.pending_deletions.get(deleted_event_id)
        if pending is None:
            pending = PendingDeletion(deleted_event=None, original=None)
            state.pending_deletions[deleted_event_id] = pending
        pending.classification = record


def _build_chat_state(records: Iterable[dict[str, Any]]) -> ChatState:
    state = ChatState()
    for record in records:
        _apply_record_to_state(state, record)
    return state


def _load_chat_state_from_disk(chat_dir: Path, *, repair_tails: bool) -> tuple[ChatState, int]:
    state = ChatState()
    repaired = _scan_records(
        chat_dir,
        repair_tails=repair_tails,
        on_record=lambda record: _apply_record_to_state(state, record),
    )
    return state, repaired


def _load_chat_state(chat_dir: Path, *, repair_tails: bool = False) -> tuple[ChatState, int]:
    signature = _chat_file_signature(chat_dir)
    with _CACHE_LOCK:
        cached = _CHAT_STATE_CACHE.get(_cache_key(chat_dir))
        if cached is not None and cached.signature == signature:
            return cached.state, 0
    state, repaired = _load_chat_state_from_disk(chat_dir, repair_tails=repair_tails)
    _store_chat_cache(chat_dir, state)
    return state, repaired


def _normalize_compare_text(text: str | None) -> str:
    compact = " ".join((text or "").strip().casefold().split())
    return compact


def _normalize_history_direction(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {"outbound", "outgoing", "sent"}:
        return "outbound"
    if normalized in {"inbound", "incoming", "received"}:
        return "inbound"
    return "unknown"


def _similarity_score(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _classify_one_deletion(
    *,
    chat_state: ChatState,
    pending: PendingDeletion,
    now: datetime,
    correction_window_seconds: int,
    nearby_before_seconds: int,
) -> dict[str, Any] | None:
    if pending.classification is not None:
        return None
    deleted_event = pending.deleted_event
    if deleted_event is None:
        return None
    deleted_at = _parse_datetime(deleted_event.get("observed_at"))
    if deleted_at is None or now < deleted_at + timedelta(seconds=correction_window_seconds):
        return None

    original = pending.original
    if original is None or original.text is None or original.sender_id is None:
        return _build_event(
            event_type="deletion.classified",
            source=str(deleted_event.get("source") or "deleted_business_messages"),
            observed_at=now,
            telegram_update_id=deleted_event.get("telegram_update_id"),
            business_connection_id=deleted_event["business_connection_id"],
            chat_id=deleted_event["chat_id"],
            message_id=deleted_event.get("message_id"),
            message_at=deleted_event.get("message_at"),
            sender_id=deleted_event.get("sender_id"),
            direction=_normalize_history_direction(deleted_event.get("direction")),
            reply_to_message_id=deleted_event.get("reply_to_message_id"),
            deleted_event_id=deleted_event["event_id"],
            classification="unclassifiable",
            classification_reason="missing_original",
            evaluated_at=now,
            deleted_observed_at=deleted_at,
        )

    original_text = _normalize_compare_text(original.text)
    if not original_text:
        return _build_event(
            event_type="deletion.classified",
            source=str(deleted_event.get("source") or "deleted_business_messages"),
            observed_at=now,
            telegram_update_id=deleted_event.get("telegram_update_id"),
            business_connection_id=deleted_event["business_connection_id"],
            chat_id=deleted_event["chat_id"],
            message_id=deleted_event.get("message_id"),
            message_at=deleted_event.get("message_at"),
            sender_id=deleted_event.get("sender_id"),
            direction=_normalize_history_direction(deleted_event.get("direction")),
            reply_to_message_id=deleted_event.get("reply_to_message_id"),
            deleted_event_id=deleted_event["event_id"],
            classification="unclassifiable",
            classification_reason="missing_text",
            evaluated_at=now,
            deleted_observed_at=deleted_at,
        )

    candidate_earliest = deleted_at - timedelta(seconds=nearby_before_seconds)
    candidate_deadline = deleted_at + timedelta(seconds=correction_window_seconds)
    nearby_candidates: list[tuple[float, MessageState]] = []
    for candidate in chat_state.messages.values():
        if candidate.deleted:
            continue
        if str(candidate.message_id) == str(original.message_id):
            continue
        if candidate.sender_id is None or str(candidate.sender_id) != str(original.sender_id):
            continue
        if str(candidate.direction or "unknown") != str(original.direction or "unknown"):
            continue
        if candidate.text is None or candidate.last_observed_at is None:
            continue
        if candidate.last_observed_at < candidate_earliest or candidate.last_observed_at > candidate_deadline:
            continue
        delta = (candidate.last_observed_at - deleted_at).total_seconds()
        nearby_candidates.append((delta, candidate))

    if nearby_candidates:
        def _best_exact(candidates: list[tuple[float, MessageState]]) -> MessageState | None:
            exact = sorted(
                (
                    (abs(delta), candidate)
                    for delta, candidate in candidates
                    if _normalize_compare_text(candidate.text) == original_text
                ),
                key=lambda item: (item[0], str(item[1].message_id)),
            )
            if not exact:
                return None
            return exact[0][1]

        def _best_similar(candidates: list[tuple[float, MessageState]]) -> tuple[float, MessageState] | None:
            similarity_candidates: list[tuple[float, float, MessageState]] = []
            for delta, candidate in candidates:
                candidate_text = _normalize_compare_text(candidate.text)
                if len(original_text) < 8 or len(candidate_text) < 8:
                    continue
                ratio = _similarity_score(original_text, candidate_text)
                length_gap = abs(len(original_text) - len(candidate_text))
                max_length_gap = max(3, min(12, int(max(len(original_text), len(candidate_text)) * 0.15)))
                if ratio >= 0.93 and length_gap <= max_length_gap:
                    similarity_candidates.append((ratio, abs(delta), candidate))
            if not similarity_candidates:
                return None
            similarity_candidates.sort(key=lambda item: (-item[0], item[1], str(item[2].message_id)))
            best_ratio, _best_delta, best = similarity_candidates[0]
            return best_ratio, best

        post_delete_candidates = [(delta, candidate) for delta, candidate in nearby_candidates if delta >= 0]
        pre_delete_candidates = [(delta, candidate) for delta, candidate in nearby_candidates if delta < 0]

        best = _best_exact(post_delete_candidates)
        if best is not None:
            return _build_event(
                event_type="deletion.classified",
                source=str(deleted_event.get("source") or "deleted_business_messages"),
                observed_at=now,
                telegram_update_id=deleted_event.get("telegram_update_id"),
                business_connection_id=deleted_event["business_connection_id"],
                chat_id=deleted_event["chat_id"],
                message_id=deleted_event.get("message_id"),
                message_at=deleted_event.get("message_at"),
                sender_id=deleted_event.get("sender_id"),
                direction=_normalize_history_direction(deleted_event.get("direction")),
                reply_to_message_id=deleted_event.get("reply_to_message_id"),
                deleted_event_id=deleted_event["event_id"],
                classification="likely_duplicate",
                replacement_message_id=best.message_id,
                classification_reason="normalized_exact_duplicate",
                classification_score=1.0,
                evaluated_at=now,
                deleted_observed_at=deleted_at,
            )

        post_similar = _best_similar(post_delete_candidates)
        if post_similar is not None:
            best_ratio, best = post_similar
            return _build_event(
                event_type="deletion.classified",
                source=str(deleted_event.get("source") or "deleted_business_messages"),
                observed_at=now,
                telegram_update_id=deleted_event.get("telegram_update_id"),
                business_connection_id=deleted_event["business_connection_id"],
                chat_id=deleted_event["chat_id"],
                message_id=deleted_event.get("message_id"),
                message_at=deleted_event.get("message_at"),
                sender_id=deleted_event.get("sender_id"),
                direction=_normalize_history_direction(deleted_event.get("direction")),
                reply_to_message_id=deleted_event.get("reply_to_message_id"),
                deleted_event_id=deleted_event["event_id"],
                classification="likely_correction",
                replacement_message_id=best.message_id,
                classification_reason="high_similarity_small_edit",
                classification_score=best_ratio,
                evaluated_at=now,
                deleted_observed_at=deleted_at,
            )

        best = _best_exact(pre_delete_candidates)
        if best is not None:
            return _build_event(
                event_type="deletion.classified",
                source=str(deleted_event.get("source") or "deleted_business_messages"),
                observed_at=now,
                telegram_update_id=deleted_event.get("telegram_update_id"),
                business_connection_id=deleted_event["business_connection_id"],
                chat_id=deleted_event["chat_id"],
                message_id=deleted_event.get("message_id"),
                message_at=deleted_event.get("message_at"),
                sender_id=deleted_event.get("sender_id"),
                direction=_normalize_history_direction(deleted_event.get("direction")),
                reply_to_message_id=deleted_event.get("reply_to_message_id"),
                deleted_event_id=deleted_event["event_id"],
                classification="likely_duplicate",
                replacement_message_id=best.message_id,
                classification_reason="normalized_exact_duplicate",
                classification_score=1.0,
                evaluated_at=now,
                deleted_observed_at=deleted_at,
            )

        pre_similar = _best_similar(pre_delete_candidates)
        if pre_similar is not None:
            best_ratio, best = pre_similar
            return _build_event(
                event_type="deletion.classified",
                source=str(deleted_event.get("source") or "deleted_business_messages"),
                observed_at=now,
                telegram_update_id=deleted_event.get("telegram_update_id"),
                business_connection_id=deleted_event["business_connection_id"],
                chat_id=deleted_event["chat_id"],
                message_id=deleted_event.get("message_id"),
                message_at=deleted_event.get("message_at"),
                sender_id=deleted_event.get("sender_id"),
                direction=_normalize_history_direction(deleted_event.get("direction")),
                reply_to_message_id=deleted_event.get("reply_to_message_id"),
                deleted_event_id=deleted_event["event_id"],
                classification="likely_correction",
                replacement_message_id=best.message_id,
                classification_reason="high_similarity_small_edit",
                classification_score=best_ratio,
                evaluated_at=now,
                deleted_observed_at=deleted_at,
            )

    return _build_event(
        event_type="deletion.classified",
        source=str(deleted_event.get("source") or "deleted_business_messages"),
        observed_at=now,
        telegram_update_id=deleted_event.get("telegram_update_id"),
        business_connection_id=deleted_event["business_connection_id"],
        chat_id=deleted_event["chat_id"],
        message_id=deleted_event.get("message_id"),
        message_at=deleted_event.get("message_at"),
        sender_id=deleted_event.get("sender_id"),
        direction=_normalize_history_direction(deleted_event.get("direction")),
        reply_to_message_id=deleted_event.get("reply_to_message_id"),
        deleted_event_id=deleted_event["event_id"],
        classification="unexplained",
        classification_reason="no_strong_match",
        evaluated_at=now,
        deleted_observed_at=deleted_at,
    )


def _timer_key(chat_dir: Path, deleted_event_id: str) -> tuple[str, str]:
    return (_cache_key(chat_dir), deleted_event_id)


def _cancel_deletion_timer(chat_dir: Path, deleted_event_id: str) -> None:
    with _CACHE_LOCK:
        entry = _DELETION_TIMERS.pop(_timer_key(chat_dir, deleted_event_id), None)
    if entry is not None:
        entry.timer.cancel()


def _cancel_chat_timers(chat_dir: Path) -> None:
    chat_key = _cache_key(chat_dir)
    with _CACHE_LOCK:
        keys = [key for key in _DELETION_TIMERS if key[0] == chat_key]
        entries = [ _DELETION_TIMERS.pop(key) for key in keys ]
    for entry in entries:
        entry.timer.cancel()


def _run_scheduled_deletion_timer(chat_dir_text: str, deleted_event_id: str, due_at_text: str) -> None:
    chat_dir = Path(chat_dir_text)
    due_at = _parse_datetime(due_at_text) or _utcnow()
    key = (chat_dir_text, deleted_event_id)
    with _CACHE_LOCK:
        entry = _DELETION_TIMERS.get(key)
        if entry is None or entry.due_at != due_at:
            return
        _DELETION_TIMERS.pop(key, None)
    try:
        _classified, refresh_exc, dirty_exc = _classify_due_for_chat(
            chat_dir,
            history_config_from_env(),
            now=max(_utcnow(), due_at),
            dirty_reason="scheduled_classification_refresh_failed",
        )
        if refresh_exc is not None:
            detail = f"; catalog dirty marker failed: {dirty_exc}" if dirty_exc is not None else "; derived catalog marked dirty"
            logger.warning("%s: scheduled deletion classification catalog refresh failed: %s%s", PLUGIN_NAME, refresh_exc, detail)
    except Exception as exc:  # noqa: BLE001 - background classification must stay contained
        logger.warning("%s: scheduled deletion classification failed: %s", PLUGIN_NAME, exc, exc_info=True)


def _ensure_deletion_timer(chat_dir: Path, deleted_event_id: str, *, due_at: datetime, now: datetime) -> None:
    if due_at <= now:
        return
    key = _timer_key(chat_dir, deleted_event_id)
    with _CACHE_LOCK:
        existing = _DELETION_TIMERS.get(key)
        if existing is not None and existing.due_at == due_at:
            return
        if existing is not None:
            existing.timer.cancel()
        delay = max(0.0, (due_at - now).total_seconds())
        timer = threading.Timer(
            delay,
            _run_scheduled_deletion_timer,
            args=(chat_dir.resolve().as_posix(), deleted_event_id, _isoformat_utc(due_at) or ""),
        )
        timer.daemon = True
        _DELETION_TIMERS[key] = DeletionTimerEntry(due_at=due_at, timer=timer)
    timer.start()


def _sync_pending_deletions_for_chat(
    chat_dir: Path,
    state: ChatState,
    config: HistoryConfig,
    *,
    now: datetime,
    appended_records: list[dict[str, Any]] | None = None,
) -> int:
    appended = 0
    for deleted_event_id, pending in list(state.pending_deletions.items()):
        if pending.classification is not None:
            _cancel_deletion_timer(chat_dir, deleted_event_id)
            continue
        deleted_event = pending.deleted_event
        deleted_at = None if deleted_event is None else _parse_datetime(deleted_event.get("observed_at"))
        if deleted_at is None:
            continue
        due_at = deleted_at + timedelta(seconds=config.correction_window_seconds)
        if due_at > now:
            _ensure_deletion_timer(chat_dir, deleted_event_id, due_at=due_at, now=now)
            continue
        record = _classify_one_deletion(
            chat_state=state,
            pending=pending,
            now=now,
            correction_window_seconds=config.correction_window_seconds,
            nearby_before_seconds=config.nearby_before_seconds,
        )
        if record is None:
            continue
        if _append_record_to_state(chat_dir, state, record, appended_records=appended_records):
            appended += 1
        _cancel_deletion_timer(chat_dir, deleted_event_id)
    return appended


def _classify_due_for_chat(
    chat_dir: Path,
    config: HistoryConfig,
    *,
    now: datetime | None = None,
    dirty_reason: str,
) -> tuple[int, Exception | None, Exception | None]:
    current = now or _utcnow()

    def _mutate(state: ChatState, appended_records: list[dict[str, Any]]) -> int:
        return _sync_pending_deletions_for_chat(
            chat_dir,
            state,
            config,
            now=current,
            appended_records=appended_records,
        )

    return _mutate_chat_history_transactionally(
        chat_dir,
        dirty_reason=dirty_reason,
        mutate=_mutate,
    )


def _iter_chat_dirs() -> Iterator[Path]:
    root = history_root()
    if not root.exists():
        return
    for connection_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        for chat_dir in sorted(path for path in connection_dir.iterdir() if path.is_dir()):
            yield chat_dir


def _file_month(path: Path) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})\.jsonl", path.name)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _is_active_month_file(path: Path, now: datetime) -> bool:
    month = _file_month(path)
    if month is None:
        return True
    return month == (now.year, now.month)


def _unlink_pruned_partition(path: Path) -> int:
    with _chat_lock(path.parent):
        size = path.stat().st_size
        path.unlink()
        _fsync_parent_directory(path)
        _invalidate_chat_cache(path.parent)
        return size


def _prunable_history_files(now: datetime) -> list[Path]:
    files: list[Path] = []
    for chat_dir in _iter_chat_dirs():
        files.extend(
            path
            for path in _iter_history_files(chat_dir)
            if not _is_active_month_file(path, now)
        )
    return sorted(files, key=lambda path: (path.name, str(path.parent)))


def _history_file_bytes() -> list[Path]:
    files: list[Path] = []
    for chat_dir in _iter_chat_dirs():
        files.extend(_iter_history_files(chat_dir))
    return files


def _has_unclassified_pending_deletion(state: ChatState) -> bool:
    return any(pending.classification is None for pending in state.pending_deletions.values())


def _scan_chat_live_pending_summary(chat_dir: Path, *, repair_tails: bool) -> _ChatStreamScanSummary:
    tracker = _LivePendingDeletionTracker()
    record_count = 0
    unexplained_count = 0
    with _chat_lock(chat_dir):
        paths = _iter_history_files(chat_dir)

        def _consume(record: dict[str, Any]) -> None:
            nonlocal record_count, unexplained_count
            record_count += 1
            event_type = str(record.get("event_type") or "")
            if event_type == "message.deleted":
                tracker.observe_deleted(str(record.get("event_id") or ""))
                return
            if event_type != "deletion.classified":
                return
            if str(record.get("classification") or "") == "unexplained":
                unexplained_count += 1
            tracker.observe_classified(str(record.get("deleted_event_id") or ""))

        try:
            repaired_files = _scan_history_files(paths, repair_tails=repair_tails, on_record=_consume)
        except ValueError as exc:
            raise ValueError(f"{chat_dir}: {exc}") from exc
        if repaired_files:
            _invalidate_chat_cache(chat_dir)
        file_snapshot = _history_file_snapshot(paths)
    return _ChatStreamScanSummary(
        file_snapshot=file_snapshot,
        record_count=record_count,
        pending_count=tracker.pending_count,
        unexplained_count=unexplained_count,
        repaired_files=repaired_files,
        max_live_pending=tracker.max_live_pending,
    )


def _collect_prune_inventory(now: datetime) -> PruneInventory:
    total_bytes = 0
    active_files: list[Path] = []
    protected_closed_files: list[Path] = []
    unprotected_closed_files: list[Path] = []
    for chat_dir in _iter_chat_dirs():
        summary = _scan_chat_live_pending_summary(chat_dir, repair_tails=True)
        protect_closed = summary.pending_count > 0
        for path, size in summary.file_snapshot:
            total_bytes += size
            if _is_active_month_file(path, now):
                active_files.append(path)
            elif protect_closed:
                protected_closed_files.append(path)
            else:
                unprotected_closed_files.append(path)
    return PruneInventory(
        total_bytes=total_bytes,
        active_files=tuple(active_files),
        protected_closed_files=tuple(protected_closed_files),
        unprotected_closed_files=tuple(unprotected_closed_files),
    )


def _cap_shortfall_warning(
    *,
    shortfall_bytes: int,
    active_files: Iterable[Path],
    protected_closed_files: Iterable[Path],
) -> str:
    blockers: list[str] = []
    if any(path.exists() for path in active_files):
        blockers.append("active month files")
    if any(path.exists() for path in protected_closed_files):
        blockers.append("pending-deletion-protected closed partitions")
    preserved = " and ".join(blockers) if blockers else "remaining retained history files"
    return (
        f"history size cap exceeded: cap_exceeded=True cap_shortfall_bytes={shortfall_bytes}; "
        f"preserving {preserved}"
    )


def collect_history_stats(*, now: datetime | None = None) -> HistoryStats:
    chat_count = 0
    record_count = 0
    pending_count = 0
    unexplained_count = 0
    file_count = 0
    total_bytes = 0
    with _root_lock():
        for chat_dir in _iter_chat_dirs():
            chat_count += 1
            summary = _scan_chat_live_pending_summary(chat_dir, repair_tails=False)
            file_count += summary.file_count
            total_bytes += summary.total_bytes
            record_count += summary.record_count
            pending_count += summary.pending_count
            unexplained_count += summary.unexplained_count
    max_bytes = history_config_from_env().max_bytes
    cap_exceeded = total_bytes > max_bytes
    return HistoryStats(
        chat_count=chat_count,
        file_count=file_count,
        record_count=record_count,
        total_bytes=total_bytes,
        pending_count=pending_count,
        unexplained_count=unexplained_count,
        cap_exceeded=cap_exceeded,
        cap_shortfall_bytes=max(0, total_bytes - max_bytes),
    )


def _record_maintenance_run(now: datetime) -> None:
    global _LAST_MAINTENANCE_AT
    with _CACHE_LOCK:
        _LAST_MAINTENANCE_AT = now


def _should_run_throttled_maintenance(now: datetime) -> bool:
    with _CACHE_LOCK:
        last = _LAST_MAINTENANCE_AT
        return last is None or now >= last + timedelta(seconds=MAINTENANCE_THROTTLE_SECONDS)


def maintain_history(*, now: datetime | None = None) -> MaintenanceResult:
    config = history_config_from_env()
    current = now or _utcnow()
    result = MaintenanceResult()
    for chat_dir in _iter_chat_dirs():
        try:
            classified, refresh_exc, dirty_exc = _classify_due_for_chat(
                chat_dir,
                config,
                now=current,
                dirty_reason="maintenance_classification_refresh_failed",
            )
            result.classified += classified
            if refresh_exc is not None:
                detail = f"; catalog dirty marker failed: {dirty_exc}" if dirty_exc is not None else "; derived catalog marked dirty"
                result.warnings.append(f"classification catalog refresh failed for {chat_dir}: {refresh_exc}{detail}")
        except Exception as exc:  # noqa: BLE001 - maintenance is best effort
            result.warnings.append(f"classification failed for {chat_dir}: {exc}")
    with _root_lock():
        pruned_any = False
        inventory = _collect_prune_inventory(current)
        total_bytes = inventory.total_bytes
        if config.retention_days > 0:
            cutoff = current - timedelta(days=config.retention_days)
            retention_cutoff = datetime(cutoff.year, cutoff.month, 1, tzinfo=timezone.utc)
            for path in sorted(
                inventory.unprotected_closed_files,
                key=lambda candidate: (_file_month(candidate) or (9999, 99), str(candidate.parent)),
            ):
                if not path.exists():
                    continue
                month = _file_month(path)
                if month is None:
                    continue
                month_start = datetime(month[0], month[1], 1, tzinfo=timezone.utc)
                if month_start >= retention_cutoff:
                    continue
                try:
                    size = _unlink_pruned_partition(path)
                    total_bytes -= size
                    result.pruned_files += 1
                    result.pruned_bytes += size
                    pruned_any = True
                except OSError as exc:
                    result.warnings.append(f"retention prune failed for {path}: {exc}")

        files = sorted(
            [path for path in inventory.unprotected_closed_files if path.exists()],
            key=lambda path: (_file_month(path) or (9999, 99), str(path.parent)),
        )
        while total_bytes > config.max_bytes and files:
            path = files.pop(0)
            try:
                size = _unlink_pruned_partition(path)
                total_bytes -= size
                result.pruned_files += 1
                result.pruned_bytes += size
                pruned_any = True
            except OSError as exc:
                result.warnings.append(f"size prune failed for {path}: {exc}")

        if pruned_any:
            try:
                _rebuild_catalog_locked(repair_tails=True)
            except Exception as exc:  # noqa: BLE001 - canonical pruning is already complete
                try:
                    _mark_catalog_dirty_locked("post_prune_catalog_rebuild_failed")
                except Exception as dirty_exc:  # noqa: BLE001 - report both failures
                    result.warnings.append(
                        f"post-prune contact catalog rebuild failed: {exc}; catalog dirty marker failed: {dirty_exc}"
                    )
                else:
                    result.warnings.append(f"post-prune contact catalog rebuild failed: {exc}; derived catalog marked dirty")

        if total_bytes > config.max_bytes:
            result.cap_exceeded = True
            result.cap_shortfall_bytes = total_bytes - config.max_bytes
            result.warnings.append(
                _cap_shortfall_warning(
                    shortfall_bytes=result.cap_shortfall_bytes,
                    active_files=inventory.active_files,
                    protected_closed_files=inventory.protected_closed_files,
                )
            )
    _record_maintenance_run(current)
    return result


def _iter_duplicate_event_id_warnings(duplicate_tracker: _EventIdDuplicateTracker) -> Iterator[tuple[int, str]]:
    yield from duplicate_tracker.iter_duplicate_warnings()


def _iter_retained_tombstone_membership_warnings(
    reference_tracker: _RetainedTombstoneReferenceTracker,
) -> Iterator[tuple[int, str]]:
    yield from reference_tracker.iter_missing_membership_warnings()


def verify_history(*, repair_tails: bool = False, now: datetime | None = None) -> VerificationResult:
    _ = now or _utcnow()
    errors = _BoundedDiagnosticCollector(limit=VERIFY_DIAGNOSTIC_LIMIT)
    warnings = _BoundedDiagnosticCollector(limit=VERIFY_DIAGNOSTIC_LIMIT)
    error_serial = 0
    warning_serial = 0
    chat_count = 0
    file_count = 0
    record_count = 0
    repaired_files = 0
    total_bytes = 0
    duplicate_tracker = _EventIdDuplicateTracker(chunk_limit=VERIFY_EVENT_ID_CHUNK_RECORDS)
    reference_tracker = _RetainedTombstoneReferenceTracker(chunk_limit=VERIFY_REFERENCE_CHUNK_RECORDS)

    def _record_error(message: str) -> None:
        nonlocal error_serial
        errors.add(order_key=(error_serial,), message=message)
        error_serial += 1

    def _record_warning(ordinal: int, phase: int, message: str) -> None:
        nonlocal warning_serial
        warnings.add(order_key=(ordinal, phase, warning_serial), message=message)
        warning_serial += 1

    try:
        with _root_lock():
            for chat_dir in _iter_chat_dirs():
                chat_count += 1
                with _chat_lock(chat_dir):
                    paths = _iter_history_files(chat_dir)
                    file_count += len(paths)
                    try:
                        def _consume(record: dict[str, Any]) -> None:
                            nonlocal record_count
                            ordinal = record_count
                            record_count += 1
                            event_id = str(record.get("event_id") or "")
                            if not event_id:
                                _record_error(f"{chat_dir}: missing event_id")
                                return
                            duplicate_tracker.add(event_id=event_id, chat_label=str(chat_dir), ordinal=ordinal)
                            if record.get("schema_version") != SCHEMA_VERSION:
                                _record_warning(
                                    ordinal,
                                    1,
                                    f"{chat_dir}: unsupported schema_version {record.get('schema_version')}",
                                )
                            event_type = str(record.get("event_type") or "")
                            if event_type == "message.deleted":
                                reference_tracker.observe_deleted(chat_label=str(chat_dir), event_id=event_id)
                                return
                            if event_type != "deletion.classified":
                                return
                            reference_tracker.observe_classified(
                                chat_label=str(chat_dir),
                                deleted_event_id=str(record.get("deleted_event_id") or ""),
                                ordinal=ordinal,
                                event_id=event_id,
                                deleted_observed_at=_isoformat_utc(_parse_datetime(record.get("deleted_observed_at"))),
                            )

                        repaired = _scan_history_files(paths, repair_tails=repair_tails, on_record=_consume)
                    except Exception as exc:  # noqa: BLE001 - collect and continue
                        total_bytes += sum(size for _, size in _history_file_snapshot(paths))
                        _record_error(f"{chat_dir}: {exc}")
                        continue
                    if repaired:
                        _invalidate_chat_cache(chat_dir)
                    repaired_files += repaired
                    file_snapshot = _history_file_snapshot(paths)
                    total_bytes += sum(size for _, size in file_snapshot)

        try:
            for ordinal, message in _iter_duplicate_event_id_warnings(duplicate_tracker):
                _record_warning(ordinal, 0, message)
        except Exception as exc:  # noqa: BLE001 - collect and continue
            _record_error(f"duplicate event_id audit failed: {exc}")
        try:
            for ordinal, message in _iter_retained_tombstone_membership_warnings(reference_tracker):
                _record_warning(ordinal, 2, message)
        except Exception as exc:  # noqa: BLE001 - collect and continue
            _record_error(f"retained tombstone audit failed: {exc}")
    finally:
        duplicate_tracker.close()
        reference_tracker.close()

    max_bytes = history_config_from_env().max_bytes
    if total_bytes > max_bytes:
        _record_warning(record_count, 3, f"history size cap exceeded by {total_bytes - max_bytes} bytes")
    return VerificationResult(
        ok=errors.total_count == 0,
        chat_count=chat_count,
        file_count=file_count,
        record_count=record_count,
        repaired_files=repaired_files,
        error_count=errors.total_count,
        warning_count=warnings.total_count,
        suppressed_error_count=errors.suppressed_count,
        suppressed_warning_count=warnings.suppressed_count,
        errors=errors.messages(),
        warnings=warnings.messages(),
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _resolve_direction(message: Any, *, bot: Any = None) -> str:
    if _get(message, "sender_business_bot") is not None:
        return "outbound"
    business_connection_id = _get(message, "business_connection_id") or _get(message, "_hermes_business_connection_id")
    from_user_id = _get(_get(message, "from_user"), "id")
    if not business_connection_id or from_user_id is None or bot is None:
        return "unknown"
    connection_key = str(business_connection_id)
    owner_id = _OWNER_CACHE.get(connection_key)
    if owner_id is None:
        get_connection = getattr(bot, "get_business_connection", None)
        if not callable(get_connection):
            return "unknown"
        try:
            connection = await _maybe_await(get_connection(business_connection_id))
        except Exception:
            return "unknown"
        owner = _get(_get(connection, "user"), "id")
        if owner is None:
            return "unknown"
        owner_id = str(owner)
        _OWNER_CACHE[connection_key] = owner_id
    return "outbound" if owner_id == str(from_user_id) else "inbound"


def _extract_history_text(message: Any) -> str | None:
    text = _get(message, "text")
    if isinstance(text, str) and text.strip():
        return text
    return None


async def observe_ptb_update(update: Any, *, bot: Any = None, now: datetime | None = None) -> bool:
    config = history_config_from_env()
    if not config.active:
        return False

    current = now or _utcnow()
    payload = None
    event_type = None
    source = None
    for attribute, normalized in (
        ("edited_business_message", "message.edited"),
        ("deleted_business_messages", "message.deleted"),
        ("business_message", "message.created"),
    ):
        candidate = getattr(update, attribute, None)
        if candidate is not None:
            payload = candidate
            event_type = normalized
            source = attribute
            break
    if payload is None or event_type is None or source is None:
        return False

    business_connection_id = _get(payload, "business_connection_id") or _get(payload, "_hermes_business_connection_id")
    chat = _get(payload, "chat")
    chat_id = _get(chat, "id")
    if not business_connection_id or chat_id is None:
        return False
    chat_type = _normalize_chat_type(_get(chat, "type"))
    known_chat_type = None
    if chat_type is None and not config.exact_chat_allowed(chat_id):
        known_chat_type = _known_chat_type_for_capture(business_connection_id, chat_id)
    if not config.allows(
        business_connection_id,
        chat_id,
        chat_type=chat_type,
        known_chat_type=known_chat_type,
    ):
        return False

    chat_dir: Path | None = None
    wrote = False
    refresh_exc: Exception | None = None
    dirty_exc: Exception | None = None
    if event_type == "message.deleted":
        chat_dir = _history_chat_dir(business_connection_id, chat_id)
        message_ids = tuple(_get(payload, "message_ids") or ())

        def _mutate_deleted(state: ChatState, appended_records: list[dict[str, Any]]) -> bool:
            wrote_record = False
            for message_id in message_ids:
                original = state.messages.get(str(message_id))
                record = _build_event(
                    event_type="message.deleted",
                    source=source,
                    observed_at=current,
                    telegram_update_id=getattr(update, "update_id", None),
                    business_connection_id=business_connection_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    message_at=None if original is None else original.message_at,
                    sender_id=None if original is None else original.sender_id,
                    direction="unknown" if original is None else original.direction,
                    reply_to_message_id=None if original is None else original.reply_to_message_id,
                )
                if _append_record_to_state(chat_dir, state, record, appended_records=appended_records):
                    wrote_record = True
            wrote_classifications = _sync_pending_deletions_for_chat(
                chat_dir,
                state,
                config,
                now=current,
                appended_records=appended_records,
            )
            return wrote_record or bool(wrote_classifications)

        wrote, refresh_exc, dirty_exc = _mutate_chat_history_transactionally(
            chat_dir,
            dirty_reason="incremental_capture_refresh_failed",
            mutate=_mutate_deleted,
        )
    else:
        text = _extract_history_text(payload)
        if text is None:
            return False
        chat_dir = _history_chat_dir(business_connection_id, chat_id)
        direction = await _resolve_direction(payload, bot=bot)
        message_date = _normalize_timestamp(_get(payload, "date"))
        chat_profile = _extract_chat_profile(payload)
        sender_profile = _extract_sender_profile(payload)

        def _mutate_message(state: ChatState, appended_records: list[dict[str, Any]]) -> bool:
            record = _build_event(
                event_type=event_type,
                source=source,
                observed_at=current,
                telegram_update_id=getattr(update, "update_id", None),
                business_connection_id=business_connection_id,
                chat_id=chat_id,
                message_id=_get(payload, "message_id"),
                message_at=message_date,
                sender_id=_get(_get(payload, "from_user"), "id"),
                direction=direction,
                reply_to_message_id=_get(_get(payload, "reply_to_message"), "message_id"),
                text=text,
                chat_profile=chat_profile,
                sender_profile=sender_profile,
            )
            wrote_record = _append_record_to_state(chat_dir, state, record, appended_records=appended_records)
            wrote_classifications = _sync_pending_deletions_for_chat(
                chat_dir,
                state,
                config,
                now=current,
                appended_records=appended_records,
            )
            return wrote_record or bool(wrote_classifications)

        wrote, refresh_exc, dirty_exc = _mutate_chat_history_transactionally(
            chat_dir,
            dirty_reason="incremental_capture_refresh_failed",
            mutate=_mutate_message,
        )

    if refresh_exc is not None:  # noqa: BLE001 - derived catalog failure must never block canonical history
        detail = f"; catalog dirty marker failed: {dirty_exc}" if dirty_exc is not None else "; derived catalog marked dirty"
        logger.warning(
            "%s: history contact catalog update failed for connection=%s chat=%s: %s%s",
            PLUGIN_NAME,
            business_connection_id,
            chat_id,
            refresh_exc,
            detail,
            exc_info=(type(refresh_exc), refresh_exc, refresh_exc.__traceback__),
        )

    if _should_run_throttled_maintenance(current):
        maintenance = maintain_history(now=current)
        if maintenance.cap_exceeded:
            logger.warning(
                "%s: history size cap exceeded by %d bytes; non-prunable history files were preserved",
                PLUGIN_NAME,
                maintenance.cap_shortfall_bytes,
            )
        for warning in maintenance.warnings:
            logger.warning("%s: %s", PLUGIN_NAME, warning)
    return wrote


def run_startup_maintenance(*, now: datetime | None = None) -> MaintenanceResult:
    return maintain_history(now=now)


def _parse_time_bound(value: str | None, *, option_name: str) -> datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    if re.fullmatch(r"\d+[smhdw]", raw):
        amount = int(raw[:-1])
        unit = raw[-1]
        seconds = {
            "s": 1,
            "m": 60,
            "h": 3600,
            "d": 86400,
            "w": 7 * 86400,
        }[unit]
        return _utcnow() - timedelta(seconds=amount * seconds)
    parsed = _parse_datetime(raw)
    if parsed is None:
        raise ValueError(f"unsupported {option_name} value: {value}")
    return parsed


def _parse_since(value: str | None) -> datetime | None:
    return _parse_time_bound(value, option_name="--since")


def _month_bounds(path: Path) -> tuple[datetime, datetime] | None:
    month = _file_month(path)
    if month is None:
        return None
    year, month_value = month
    start = datetime(year, month_value, 1, tzinfo=timezone.utc)
    if month_value == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month_value + 1, 1, tzinfo=timezone.utc)
    return start, end


def _partition_intersects(path: Path, *, since: datetime | None, until: datetime | None) -> bool:
    bounds = _month_bounds(path)
    if bounds is None:
        return True
    start, end = bounds
    if since is not None and end <= since:
        return False
    if until is not None and start > until:
        return False
    return True


def _iter_selected_history_files(chat_dir: Path, *, since: datetime | None, until: datetime | None) -> Iterator[Path]:
    for path in _iter_history_files(chat_dir):
        if _partition_intersects(path, since=since, until=until):
            yield path


def _record_observed_at(record: dict[str, Any]) -> datetime | None:
    return _parse_datetime(record.get("observed_at"))


def _record_within_bounds(record: dict[str, Any], *, since: datetime | None, until: datetime | None) -> bool:
    observed_at = _record_observed_at(record)
    if observed_at is None:
        return False
    if since is not None and observed_at < since:
        return False
    if until is not None and observed_at > until:
        return False
    return True


def _normalize_search_text(value: Any) -> str:
    # Search uses Unicode NFKC plus casefold only. It intentionally preserves
    # whitespace so substring semantics stay literal apart from compatibility
    # normalization.
    text = "" if value is None else str(value)
    return unicodedata.normalize("NFKC", text).casefold()


@contextmanager
def _streamed_records_locked(
    chat_dir: Path,
    *,
    since: datetime | None,
    until: datetime | None,
    text_query: str | None = None,
) -> Iterator[Iterator[dict[str, Any]]]:
    normalized_text_query = None if text_query is None else _normalize_search_text(text_query)
    with _chat_lock(chat_dir):
        selected_files = list(_iter_selected_history_files(chat_dir, since=since, until=until))
        for path in selected_files:
            _raise_if_tail_repair_required(path)

        def _iter_records() -> Iterator[dict[str, Any]]:
            for path in selected_files:
                with path.open("r", encoding="utf-8") as handle:
                    for line_no, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise ValueError(f"{chat_dir}: invalid JSON in {path.name}:{line_no}: {exc}") from exc
                        if not isinstance(record, dict):
                            raise ValueError(f"{chat_dir}: non-object record in {path.name}:{line_no}")
                        if not _record_within_bounds(record, since=since, until=until):
                            continue
                        if normalized_text_query is not None and normalized_text_query not in _normalize_search_text(
                            record.get("text")
                        ):
                            continue
                        yield record

        yield _iter_records()


def _tail_records(records: Iterable[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    bounded = deque(maxlen=limit)
    for record in records:
        bounded.append(record)
    return list(bounded)


def _latest_records(records: Iterable[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    heap: list[tuple[float, int, dict[str, Any]]] = []
    for sequence, record in enumerate(records):
        observed_at = _record_observed_at(record)
        if observed_at is None:
            continue
        item = (observed_at.timestamp(), sequence, record)
        if len(heap) < limit:
            heapq.heappush(heap, item)
            continue
        if item[:2] <= heap[0][:2]:
            continue
        heapq.heapreplace(heap, item)
    return [item[2] for item in sorted(heap)]


def _latest_records_across_entries(
    entries: Iterable[dict[str, Any]],
    *,
    since: datetime | None,
    until: datetime | None,
    text_query: str,
    limit: int,
) -> list[dict[str, Any]]:
    heap: list[tuple[float, int, dict[str, Any]]] = []
    sequence = 0
    for entry in entries:
        with _streamed_records_locked(
            _entry_chat_dir(entry),
            since=since,
            until=until,
            text_query=text_query,
        ) as records:
            for record in records:
                observed_at = _record_observed_at(record)
                if observed_at is None:
                    continue
                item = (observed_at.timestamp(), sequence, record)
                sequence += 1
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                    continue
                if item[:2] <= heap[0][:2]:
                    continue
                heapq.heapreplace(heap, item)
    return [item[2] for item in sorted(heap)]


def _catalog_entries_for_cli() -> list[dict[str, Any]]:
    catalog, _ = _load_contact_catalog(
        rebuild_on_missing=True,
        rebuild_on_corrupt=True,
        rebuild_on_dirty=True,
        repair_tails=False,
    )
    return [entry for entry in catalog.get("contacts", []) if isinstance(entry, dict)]


def _normalize_contact_lookup(text: str) -> str:
    return _normalize_lookup_text(text, strip_username_prefix=True)


def _contact_lookup_tokens(entry: dict[str, Any]) -> set[str]:
    tokens = {
        _normalize_contact_lookup(value)
        for value in [*_profile_lookup_values(entry.get("current_profile")), *_catalog_entry_aliases(entry)]
        if isinstance(value, str) and value.strip()
    }
    return {token for token in tokens if token}


def _contact_search_haystack(entry: dict[str, Any]) -> str:
    parts = [
        _catalog_entry_display_name(entry),
        *(_profile_lookup_values(entry.get("current_profile"))),
        *(_catalog_entry_aliases(entry)),
        str(entry.get("business_connection_id")),
        str(entry.get("chat_id")),
        str(entry.get("chat_type") or ""),
    ]
    return " | ".join(_normalize_lookup_text(part) for part in parts if part)


def _filter_catalog_entries(
    *,
    business_connection_id: str | None = None,
    chat_id: str | None = None,
) -> list[dict[str, Any]]:
    entries = []
    for entry in _catalog_entries_for_cli():
        if business_connection_id is not None and str(entry.get("business_connection_id")) != str(business_connection_id):
            continue
        if chat_id is not None and str(entry.get("chat_id")) != str(chat_id):
            continue
        entries.append(entry)
    return entries


def _chat_lookup_label(*, business_connection_id: str | None, chat_id: str) -> str:
    if business_connection_id is None:
        return f"chat={chat_id}"
    return f"connection={business_connection_id} chat={chat_id}"


def _resolve_chat_entry(*, business_connection_id: str | None, chat_id: str) -> dict[str, Any]:
    matches = _filter_catalog_entries(business_connection_id=business_connection_id, chat_id=chat_id)
    if not matches:
        raise ValueError(f"no matching history chat found for {_chat_lookup_label(business_connection_id=business_connection_id, chat_id=chat_id)}")
    if len(matches) > 1:
        label = _chat_lookup_label(business_connection_id=business_connection_id, chat_id=chat_id)
        if business_connection_id is None:
            raise ValueError(f"multiple history chats match {label}; pass --connection explicitly")
        raise ValueError(f"multiple history chats match {label}; verify contacts.json and canonical history")
    return matches[0]


def _resolve_contact_entry(*, business_connection_id: str | None, contact: str) -> dict[str, Any]:
    needle = _normalize_contact_lookup(contact)
    matches = [
        entry
        for entry in _filter_catalog_entries(business_connection_id=business_connection_id)
        if needle in _contact_lookup_tokens(entry)
    ]
    if not matches:
        raise ValueError(f"no matching contact found for {contact!r}")
    if len(matches) > 1:
        labels = ", ".join(
            f"{entry.get('business_connection_id')}:{entry.get('chat_id')}={_catalog_entry_display_name(entry)}"
            for entry in sorted(matches, key=lambda entry: (str(entry.get("business_connection_id")), str(entry.get("chat_id"))))
        )
        raise ValueError(f"contact {contact!r} is ambiguous; choose one of: {labels}")
    return matches[0]


def _resolve_history_entry(
    *,
    business_connection_id: str | None,
    chat_id: str | None,
    contact: str | None,
) -> dict[str, Any]:
    if chat_id and contact:
        raise ValueError("pass either --chat or --contact, not both")
    if contact:
        return _resolve_contact_entry(business_connection_id=business_connection_id, contact=contact)
    if chat_id:
        return _resolve_chat_entry(business_connection_id=business_connection_id, chat_id=chat_id)
    raise ValueError("pass --chat or --contact")


def _entry_chat_dir(entry: dict[str, Any]) -> Path:
    return _history_chat_dir_path(entry.get("business_connection_id"), entry.get("chat_id"))


DeletionRowKey = tuple[float, str, str, str, str, str]


def _deletion_row_key(record: dict[str, Any], *, pending: bool) -> DeletionRowKey | None:
    if pending:
        sort_at = _record_observed_at(record)
    else:
        sort_at = _parse_datetime(record.get("evaluated_at")) or _record_observed_at(record)
    if sort_at is None:
        return None
    primary_id = str(record.get("deleted_event_id") or record.get("event_id") or "")
    return (
        sort_at.timestamp(),
        str(record.get("business_connection_id")),
        str(record.get("chat_id")),
        str(record.get("message_id")),
        primary_id,
        str(record.get("event_id") or ""),
    )


@dataclass
class _DeletionRowCollector:
    limit: int
    status: str
    heap: list[tuple[DeletionRowKey, dict[str, Any]]] = field(default_factory=list)
    pending_by_deleted_event_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_heap_size: int = 0
    max_live_pending: int = 0

    def _note_sizes(self) -> None:
        self.max_heap_size = max(self.max_heap_size, len(self.heap))
        self.max_live_pending = max(self.max_live_pending, len(self.pending_by_deleted_event_id))

    def _push(self, record: dict[str, Any], *, pending: bool) -> None:
        row_key = _deletion_row_key(record, pending=pending)
        if row_key is None:
            return
        item = (row_key, record)
        if len(self.heap) < self.limit:
            heapq.heappush(self.heap, item)
            self._note_sizes()
            return
        if item[0] <= self.heap[0][0]:
            return
        heapq.heapreplace(self.heap, item)
        self._note_sizes()

    def consume(self, record: dict[str, Any]) -> None:
        event_type = str(record.get("event_type") or "")
        if self.status == "pending":
            if event_type == "message.deleted":
                event_id = str(record.get("event_id") or "")
                if event_id:
                    self.pending_by_deleted_event_id[event_id] = record
                    self._note_sizes()
                return
            if event_type == "deletion.classified":
                deleted_event_id = str(record.get("deleted_event_id") or "")
                if deleted_event_id:
                    self.pending_by_deleted_event_id.pop(deleted_event_id, None)
                    self._note_sizes()
                return
            return
        if event_type != "deletion.classified" or str(record.get("classification") or "") != self.status:
            return
        self._push(record, pending=False)

    def finish_chat(self) -> None:
        if self.status != "pending":
            return
        # Runtime timers and startup maintenance should keep this live tombstone
        # set bounded to the current correction/recovery window. The CLI therefore
        # retains only tombstones plus the output heap, never full chat history.
        for record in self.pending_by_deleted_event_id.values():
            self._push(record, pending=True)
        self.pending_by_deleted_event_id.clear()
        self._note_sizes()

    def rows(self) -> list[dict[str, Any]]:
        return [item[1] for item in sorted(self.heap, key=lambda item: item[0])]


def _history_entries_for_cli(*, business_connection_id: str | None, chat_id: str | None = None) -> list[dict[str, Any]]:
    if chat_id is not None:
        return [_resolve_chat_entry(business_connection_id=business_connection_id, chat_id=chat_id)]
    return _filter_catalog_entries(business_connection_id=business_connection_id)


def _latest_deletion_rows(
    entries: Iterable[dict[str, Any]],
    *,
    status: str,
    limit: int,
) -> tuple[list[dict[str, Any]], _DeletionRowCollector]:
    collector = _DeletionRowCollector(limit=limit, status=status)
    for entry in entries:
        with _streamed_records_locked(_entry_chat_dir(entry), since=None, until=None, text_query=None) as records:
            for record in records:
                collector.consume(record)
        collector.finish_chat()
    return collector.rows(), collector


def _pending_deletion_text_line(record: dict[str, Any]) -> str:
    return (
        f"{record.get('observed_at')} message.deleted "
        f"connection={record.get('business_connection_id')} "
        f"chat={record.get('chat_id')} "
        f"message={record.get('message_id')} status=pending"
    )


def _contact_summary_line(entry: dict[str, Any]) -> str:
    profile = entry.get("current_profile") if isinstance(entry.get("current_profile"), dict) else {}
    username = _clean_optional_text(_get(profile, "username"))
    username_text = f" username=@{username}" if username else ""
    aliases = _catalog_entry_aliases(entry)
    aliases_text = f" aliases={','.join(aliases)}" if aliases else ""
    return (
        f"{_catalog_entry_display_name(entry)}{username_text} "
        f"connection={entry.get('business_connection_id')} chat={entry.get('chat_id')} "
        f"type={entry.get('chat_type') or 'unknown'} messages={entry.get('message_count', 0)} "
        f"edits={entry.get('edit_count', 0)} deleted={entry.get('deleted_count', 0)} "
        f"unexplained={entry.get('unexplained_count', 0)} "
        f"range={entry.get('first_seen_at')}..{entry.get('last_seen_at')}{aliases_text}"
    )


def _catalog_status_line(*, rebuild: bool = False) -> str:
    path = _catalog_path()
    if rebuild:
        catalog = rebuild_contact_catalog()
        return (
            f"status=rebuilt path={path} entries={catalog.get('contact_count', 0)} "
            f"generated_at={catalog.get('generated_at')}"
        )
    try:
        catalog, rebuilt = _load_contact_catalog(
            rebuild_on_missing=True,
            rebuild_on_corrupt=True,
            rebuild_on_dirty=True,
            repair_tails=True,
        )
    except (OSError, ValueError) as exc:
        return f"status=error path={path} error={exc}"
    status = "rebuilt" if rebuilt else "ok"
    return (
        f"status={status} path={path} entries={catalog.get('contact_count', 0)} "
        f"generated_at={catalog.get('generated_at')}"
    )

def _bounded_limit(raw: int | None, default: int) -> int:
    if raw is None:
        return default
    return max(1, min(int(raw), MAX_READ_LIMIT))


def _escape_terminal_text(text: str) -> str:
    escaped: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif codepoint < 0x20 or codepoint == 0x7F or 0x80 <= codepoint <= 0x9F:
            escaped.append(f"\\x{codepoint:02x}")
        else:
            escaped.append(char)
    return "".join(escaped)


def _history_text_line(record: dict[str, Any]) -> str:
    business_connection_id = record.get("business_connection_id")
    chat_id = record.get("chat_id")
    observed_at = record.get("observed_at")
    message_id = record.get("message_id")
    event_type = record.get("event_type")
    source = record.get("source")
    direction = _normalize_history_direction(record.get("direction"))
    sender_id = record.get("sender_id")
    text = record.get("text")
    if text is not None:
        text = _escape_terminal_text(text)
    suffix = ""
    if event_type == "deletion.classified":
        suffix = (
            f" status={record.get('classification')}"
            f" replacement={record.get('replacement_message_id')}"
            f" reason={record.get('classification_reason') or record.get('classification_method')}"
            f" score={record.get('classification_score')}"
        )
    elif text is not None:
        suffix = f" text={text}"
    return (
        f"{observed_at} {event_type} source={source} connection={business_connection_id} chat={chat_id} message={message_id} "
        f"direction={direction} sender={sender_id}{suffix}"
    )


def _chat_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}
    chat_state = _build_chat_state(records)
    first = records[0]
    latest = records[-1]
    unexplained = sum(
        1
        for pending in chat_state.pending_deletions.values()
        if pending.classification is not None and pending.classification.get("classification") == "unexplained"
    )
    pending = sum(1 for pending in chat_state.pending_deletions.values() if pending.classification is None)
    return {
        "business_connection_id": first.get("business_connection_id"),
        "chat_id": first.get("chat_id"),
        "records": len(records),
        "first_observed_at": first.get("observed_at"),
        "last_observed_at": latest.get("observed_at"),
        "pending_deletions": pending,
        "unexplained_deletions": unexplained,
    }


def setup_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="telegram_business_command")
    history = subs.add_parser("history", help="Read and maintain Telegram Business history")
    history_subs = history.add_subparsers(dest="telegram_business_history_command")

    chats = history_subs.add_parser("chats", help="List chats with stored history")
    chats.add_argument("--connection")
    chats.add_argument("--limit", type=int, default=DEFAULT_CHAT_LIMIT)
    chats.set_defaults(_history_action="chats")

    contacts = history_subs.add_parser("contacts", help="List or search current contacts")
    contacts.add_argument("--connection")
    contacts.add_argument("--search")
    contacts.add_argument("--limit", type=int, default=DEFAULT_CHAT_LIMIT)
    contacts.set_defaults(_history_action="contacts")

    catalog = history_subs.add_parser("catalog", help="Show or rebuild the derived contact catalog")
    catalog.add_argument("--rebuild", action="store_true")
    catalog.set_defaults(_history_action="catalog")

    stats = history_subs.add_parser("stats", help="Show aggregate history stats")
    stats.set_defaults(_history_action="stats")

    show = history_subs.add_parser("show", help="Show a bounded history timeline")
    show.add_argument("--connection")
    show.add_argument("--chat")
    show.add_argument("--contact")
    show.add_argument("--since")
    show.add_argument("--until")
    show.add_argument("--limit", type=int, default=DEFAULT_READ_LIMIT)
    show.set_defaults(_history_action="show")

    search = history_subs.add_parser("search", help="Search chat history text")
    search.add_argument("--connection")
    search.add_argument("--chat")
    search.add_argument("--contact")
    search.add_argument("--text", required=True)
    search.add_argument("--since")
    search.add_argument("--until")
    search.add_argument("--limit", type=int, default=DEFAULT_READ_LIMIT)
    search.set_defaults(_history_action="search")

    deletions = history_subs.add_parser("deletions", help="List deleted-message classifications")
    deletions.add_argument("--connection")
    deletions.add_argument("--chat")
    deletions.add_argument(
        "--status",
        choices=["pending", "likely_duplicate", "likely_correction", "unexplained", "unclassifiable"],
        default="unexplained",
    )
    deletions.add_argument("--limit", type=int, default=DEFAULT_READ_LIMIT)
    deletions.set_defaults(_history_action="deletions")

    export = history_subs.add_parser("export", help="Export bounded history records")
    export.add_argument("--connection")
    export.add_argument("--chat")
    export.add_argument("--contact")
    export.add_argument("--format", choices=["jsonl", "text"], default="jsonl")
    export.add_argument("--limit", type=int, default=DEFAULT_READ_LIMIT)
    export.add_argument("--since")
    export.add_argument("--until")
    export.set_defaults(_history_action="export")

    verify = history_subs.add_parser("verify", help="Verify canonical history files")
    verify.add_argument("--repair-tails", action="store_true")
    verify.set_defaults(_history_action="verify")

    maintain = history_subs.add_parser("maintain", help="Run classification and retention/size maintenance")
    maintain.set_defaults(_history_action="maintain")


def handle_cli(args: argparse.Namespace) -> int:
    action = getattr(args, "_history_action", None)
    if action is None:
        print("Usage: hermes telegram-business history <chats|contacts|catalog|stats|show|search|deletions|export|verify|maintain>")
        return 1
    try:
        if action == "chats":
            entries = sorted(
                _filter_catalog_entries(business_connection_id=getattr(args, "connection", None)),
                key=lambda entry: (
                    _parse_datetime(entry.get("last_seen_at")) or datetime.min.replace(tzinfo=timezone.utc),
                    str(entry.get("business_connection_id")),
                    str(entry.get("chat_id")),
                ),
                reverse=True,
            )
            limit = _bounded_limit(getattr(args, "limit", None), DEFAULT_CHAT_LIMIT)
            for entry in entries[:limit]:
                print(_contact_summary_line(entry))
            if not entries:
                print("No Telegram Business history found.")
            return 0

        if action == "contacts":
            entries = _filter_catalog_entries(business_connection_id=getattr(args, "connection", None))
            search = _clean_optional_text(getattr(args, "search", None))
            if search is not None:
                normalized_search = _normalize_lookup_text(search)
                entries = [entry for entry in entries if normalized_search in _contact_search_haystack(entry)]
            entries = sorted(
                entries,
                key=lambda entry: (
                    _parse_datetime(entry.get("last_seen_at")) or datetime.min.replace(tzinfo=timezone.utc),
                    str(entry.get("business_connection_id")),
                    str(entry.get("chat_id")),
                ),
                reverse=True,
            )
            limit = _bounded_limit(getattr(args, "limit", None), DEFAULT_CHAT_LIMIT)
            for entry in entries[:limit]:
                print(_contact_summary_line(entry))
            if not entries:
                print("No matching contacts found.")
            return 0

        if action == "catalog":
            print(_catalog_status_line(rebuild=bool(getattr(args, "rebuild", False))))
            return 0

        if action == "stats":
            config = history_config_from_env()
            stats = collect_history_stats()
            print(
                " ".join(
                    [
                        f"enabled={config.enabled}",
                        f"connections={config.connections.render() if config.connections else '<unset>'}",
                        f"chats={config.chats.render() if config.chats else '<unset>'}",
                        f"chat_types={','.join(sorted(config.chat_types))}",
                        f"correction_window={config.correction_window_seconds}s",
                        f"nearby_before={config.nearby_before_seconds}s",
                        f"retention_days={config.retention_days}",
                        f"max_bytes={config.max_bytes}",
                        f"chat_count={stats.chat_count}",
                        f"file_count={stats.file_count}",
                        f"record_count={stats.record_count}",
                        f"pending={stats.pending_count}",
                        f"unexplained={stats.unexplained_count}",
                        f"total_bytes={stats.total_bytes}",
                        f"cap_exceeded={stats.cap_exceeded}",
                    ]
                )
            )
            return 0

        if action in {"show", "search", "export"}:
            since = _parse_since(getattr(args, "since", None))
            until = _parse_time_bound(getattr(args, "until", None), option_name="--until")
            if since is not None and until is not None and until < since:
                raise ValueError("--until must be greater than or equal to --since")
            limit = _bounded_limit(getattr(args, "limit", None), DEFAULT_READ_LIMIT)
            if action == "search":
                text_query = str(args.text)
                if getattr(args, "chat", None) is None and getattr(args, "contact", None) is None:
                    records = _latest_records_across_entries(
                        _filter_catalog_entries(business_connection_id=getattr(args, "connection", None)),
                        since=since,
                        until=until,
                        text_query=text_query,
                        limit=limit,
                    )
                else:
                    entry = _resolve_history_entry(
                        business_connection_id=getattr(args, "connection", None),
                        chat_id=None if getattr(args, "chat", None) is None else str(args.chat),
                        contact=getattr(args, "contact", None),
                    )
                    with _streamed_records_locked(
                            _entry_chat_dir(entry),
                            since=since,
                            until=until,
                            text_query=text_query,
                        ) as streamed_records:
                        records = _tail_records(streamed_records, limit=limit)
            else:
                entry = _resolve_history_entry(
                    business_connection_id=getattr(args, "connection", None),
                    chat_id=None if getattr(args, "chat", None) is None else str(args.chat),
                    contact=getattr(args, "contact", None),
                )
                with _streamed_records_locked(
                        _entry_chat_dir(entry),
                        since=since,
                        until=until,
                        text_query=None,
                    ) as streamed_records:
                    records = _tail_records(streamed_records, limit=limit)
            if action == "export" and args.format == "jsonl":
                for record in records:
                    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                return 0
            if not records:
                print("No matching history records found.")
                return 0
            for record in records:
                print(_history_text_line(record))
            return 0

        if action == "deletions":
            status = str(args.status)
            limit = _bounded_limit(getattr(args, "limit", None), DEFAULT_READ_LIMIT)
            rows, _ = _latest_deletion_rows(
                _history_entries_for_cli(
                    business_connection_id=getattr(args, "connection", None),
                    chat_id=None if getattr(args, "chat", None) is None else str(args.chat),
                ),
                status=status,
                limit=limit,
            )
            if not rows:
                print("No matching deletions found.")
                return 0
            render = _pending_deletion_text_line if status == "pending" else _history_text_line
            for record in rows:
                print(render(record))
            return 0

        if action == "verify":
            result = verify_history(repair_tails=bool(getattr(args, "repair_tails", False)))
            print(
                f"ok={result.ok} chats={result.chat_count} files={result.file_count} "
                f"records={result.record_count} repaired_files={result.repaired_files} "
                f"errors={result.error_count} warnings={result.warning_count}"
            )
            for warning in result.warnings:
                print(f"warning: {warning}")
            if result.suppressed_warning_count:
                print(f"warning: suppressed {result.suppressed_warning_count} additional warnings")
            for error in result.errors:
                print(f"error: {error}")
            if result.suppressed_error_count:
                print(f"error: suppressed {result.suppressed_error_count} additional errors")
            return 0 if result.ok else 1

        if action == "maintain":
            result = maintain_history()
            print(
                f"classified={result.classified} pruned_files={result.pruned_files} "
                f"pruned_bytes={result.pruned_bytes} cap_exceeded={result.cap_exceeded} "
                f"cap_shortfall_bytes={result.cap_shortfall_bytes}"
            )
            for warning in result.warnings:
                print(f"warning: {warning}")
            return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1

    print(f"Unknown history action: {action}")
    return 1


def reset_in_memory_caches() -> None:
    global _LAST_MAINTENANCE_AT
    with _CACHE_LOCK:
        timer_entries = list(_DELETION_TIMERS.values())
        _DELETION_TIMERS.clear()
        _CHAT_STATE_CACHE.clear()
        _LAST_MAINTENANCE_AT = None
    for entry in timer_entries:
        entry.timer.cancel()
    _OWNER_CACHE.clear()


def remove_history_tree() -> None:
    shutil.rmtree(history_root(), ignore_errors=True)


def file_modes(path: Path) -> dict[str, int]:
    modes = {}
    for child in [path, *path.parents]:
        if child.exists():
            modes[str(child)] = stat.S_IMODE(child.stat().st_mode)
    return modes
