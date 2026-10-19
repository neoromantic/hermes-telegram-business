"""Per-chat history and dossier switches: "Don't save" and "No dossier" for one chat.

The owner panel and ``hermes telesec chat set`` turn history or the dossier off for one chat. Only
"off" is stored, per (Business owner, chat) like transcription modes, in
``$HERMES_HOME/data/telesec/chat-settings.json``; "on" is the default and removes the value. The file
is separate from ``transcription-modes.json`` because older versions drop unknown keys there on their
next write, so a rollback would lose these pauses. Readers stat the file on every call and re-read it
only when it changed, so a change applies to the next update without a restart.

A pause fails closed: a damaged file keeps the last valid settings, and with none (a cold start)
history capture and dossiers pause for every chat until the file is repaired. Off is not delete:
saved history and the stored dossier stay until the owner erases them (``history_support.forget_chat``).
Stdlib-only; the file never holds message text or names.

Up to 0.12, TELESEC_HISTORY_CONNECTIONS, _CHATS and _CHAT_TYPES decided which chats history recorded.
``migrate_history_scope()`` stores "Don't save" once for every known chat they kept out, and they are
never read again.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import global_settings as _settings
from . import history_support as _history
from . import transcription_modes as _modes

logger = logging.getLogger(__name__)

FEATURES = ("history", "dossier")
OFF = "off"
UPDATED_VIA = ("chat", "cli")
SCHEMA_VERSION = 1
SETTINGS_FILENAME = "chat-settings.json"
MAX_CHATS_PER_OWNER = _modes.MAX_CHATS_PER_OWNER
CHAT_USAGE = "Usage: hermes telesec chat <list|get|set>"
REPAIR_HINT = "'hermes telesec chat set <chat> ... --owner ID' backs it up and starts empty"

_OWNER_KEY_RE = re.compile(r"^\d+$")
_CHAT_KEY_RE = re.compile(r"^-?\d+$")


class ChatSettingsFileCorrupt(ValueError):
    """chat-settings.json exists but cannot be parsed; only a CLI ``chat set --owner`` replaces it."""


def chat_settings_path() -> Path:
    return _history.get_hermes_home() / "data" / "telesec" / SETTINGS_FILENAME


@dataclass(frozen=True)
class ChatEntry:
    history: bool = True
    dossier: bool = True
    updated_at: str | None = None
    updated_via: str | None = None

    def on(self, feature: str) -> bool:
        return self.history if feature == "history" else self.dossier


@dataclass(frozen=True)
class ChatSnapshot:
    """owner -> chat -> entry, plus each feature's chat ids that some owner turned off."""

    owners: Mapping[str, Mapping[str, ChatEntry]] = field(default_factory=dict)
    off: Mapping[str, frozenset[str]] = field(default_factory=lambda: {f: frozenset() for f in FEATURES})
    # The file can't be read and no earlier read was valid: history and dossiers pause for every chat.
    blind: bool = False


_EMPTY = ChatSnapshot()
_BLIND = ChatSnapshot(blind=True)


@dataclass(frozen=True)
class ChatWriteResult:
    entry: ChatEntry | None  # None: nothing is off for the chat any more
    changed: bool
    backup_path: Path | None = None


def _check_feature(feature: str) -> None:
    if feature not in FEATURES:
        raise ValueError(f"feature must be one of {', '.join(FEATURES)}, got {feature!r}")


def _parse_entry(value: Any) -> ChatEntry:
    if not isinstance(value, dict):
        raise ChatSettingsFileCorrupt("chat entry is not an object")
    flags = {}
    for feature in FEATURES:
        flag = value.get(feature, "on")
        if flag not in ("on", OFF):
            # An unknown value must not quietly resume capture: the whole file counts as damaged.
            raise ChatSettingsFileCorrupt(f"invalid {feature} value")
        flags[feature] = flag != OFF
    at, via = value.get("updated_at"), value.get("updated_via")
    return ChatEntry(**flags, updated_at=at if isinstance(at, str) else None,
                     updated_via=via if isinstance(via, str) else None)


def _parse_payload(raw: Any) -> ChatSnapshot:
    """The snapshot of a decoded file; unknown fields are allowed (and kept by writers)."""
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        raise ChatSettingsFileCorrupt("unsupported version")
    owners_raw = raw.get("owners", {})
    if not isinstance(owners_raw, dict):
        raise ChatSettingsFileCorrupt("owners is not an object")
    owners: dict[str, dict[str, ChatEntry]] = {}
    off: dict[str, set[str]] = {feature: set() for feature in FEATURES}
    for owner, owner_value in owners_raw.items():
        if not _OWNER_KEY_RE.match(str(owner)) or not isinstance(owner_value, dict):
            raise ChatSettingsFileCorrupt("invalid owner")
        chats_raw = owner_value.get("chats", {})
        if not isinstance(chats_raw, dict):
            raise ChatSettingsFileCorrupt("owner chats is not an object")
        chats: dict[str, ChatEntry] = {}
        for chat, value in chats_raw.items():
            if not _CHAT_KEY_RE.match(str(chat)):
                raise ChatSettingsFileCorrupt("invalid chat id")
            entry = _parse_entry(value)
            chats[chat] = entry
            for feature in FEATURES:
                if not entry.on(feature):
                    off[feature].add(chat)
        owners[owner] = chats
    return ChatSnapshot(owners, {feature: frozenset(chats) for feature, chats in off.items()})


def _decode(data: bytes) -> Any:
    """The decoded file; an empty file holds nothing, like a missing one."""
    if not data.strip():
        return {"version": SCHEMA_VERSION, "owners": {}}
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChatSettingsFileCorrupt(type(exc).__name__) from exc


def _fallback_note(snapshot: ChatSnapshot) -> str:
    if snapshot.blind:
        return f"history and dossiers are paused for every chat until it is repaired; {REPAIR_HINT}"
    return f"keeping the last valid chat settings; {REPAIR_HINT}"


class ChatSettingsStore:
    """chat-settings.json reader with an inode/mtime/size-keyed cache and locked atomic writes, like ModeStore."""

    def __init__(self, path_fn: Callable[[], Path] = chat_settings_path):
        self._path_fn = path_fn
        self._lock = threading.Lock()
        self._valid: dict[str, tuple[Any, ChatSnapshot]] = {}
        # Signature of a corrupt file already warned about; it is not parsed again until it changes.
        self._damaged: dict[str, Any] = {}
        self._unreadable: set[str] = set()
        self._warned: set[tuple[str, str]] = set()

    @property
    def path(self) -> Path:
        return self._path_fn()

    def clear_cache(self) -> None:
        with self._lock:
            self._valid.clear()
            self._damaged.clear()
            self._unreadable.clear()
            self._warned.clear()

    def _fallback(self, key: str) -> ChatSnapshot:
        with self._lock:
            valid = self._valid.get(key)
        return valid[1] if valid is not None else _BLIND

    def _mark_unreadable(self, path: Path, exc: OSError) -> ChatSnapshot:
        # An I/O error is transient, so it is never cached: the next update reads again.
        key = str(path)
        fallback = self._fallback(key)
        with self._lock:
            self._unreadable.add(key)
            first = (key, type(exc).__name__) not in self._warned
            self._warned.add((key, type(exc).__name__))
        if first:
            logger.warning("%s: chat settings file %s unreadable (%s); %s", _history.PLUGIN_NAME, path,
                           type(exc).__name__, _fallback_note(fallback))
        return fallback

    def snapshot(self) -> ChatSnapshot:
        path = self._path_fn()
        key = str(path)
        try:
            signature: Any = _modes._signature(os.stat(path))
        except FileNotFoundError:
            signature = None
        except OSError as exc:
            return self._mark_unreadable(path, exc)
        with self._lock:
            valid = self._valid.get(key)
            if valid is not None and valid[0] == signature:
                self._unreadable.discard(key)
                self._damaged.pop(key, None)
                return valid[1]
            if signature is not None and self._damaged.get(key) == signature:
                self._unreadable.discard(key)
                return valid[1] if valid is not None else _BLIND
        try:
            snapshot = _parse_payload(_decode(path.read_bytes())) if signature is not None else _EMPTY
        except FileNotFoundError:
            snapshot = _EMPTY
        except ChatSettingsFileCorrupt as exc:
            fallback = self._fallback(key)
            with self._lock:
                self._damaged[key] = signature
                self._unreadable.discard(key)
            logger.warning("%s: chat settings file %s is corrupt (%s); %s", _history.PLUGIN_NAME, path, exc,
                           _fallback_note(fallback))
            return fallback
        except OSError as exc:
            return self._mark_unreadable(path, exc)
        with self._lock:
            self._valid[key] = (signature, snapshot)
            self._damaged.pop(key, None)
            self._unreadable.discard(key)
        return snapshot

    def file_degraded(self) -> bool:
        """True while the file on disk is corrupt or unreadable (the last valid settings, or a pause, stand in)."""
        self.snapshot()
        key = str(self._path_fn())
        with self._lock:
            return key in self._damaged or key in self._unreadable

    def read_strict(self) -> ChatSnapshot:
        """CLI read: a corrupt file raises instead of reading as empty or paused."""
        path = self._path_fn()
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return _EMPTY
        try:
            return _parse_payload(_decode(data))
        except ChatSettingsFileCorrupt as exc:
            raise ChatSettingsFileCorrupt(f"{path} is corrupt ({exc}); {REPAIR_HINT}") from exc

    def get(self, owner: Any, chat: Any) -> ChatEntry | None:
        if owner is None or chat is None:
            return None
        return self.snapshot().owners.get(str(owner), {}).get(str(chat))

    def excluded_anywhere(self, feature: str, chat_id: Any) -> bool:
        """True when some owner turned ``feature`` off for this chat id, or the file can't be read (fail closed).

        The fast path for capture: when False, no owner lookup is needed.
        """
        _check_feature(feature)
        snapshot = self.snapshot()
        return snapshot.blind or (chat_id is not None and str(chat_id) in snapshot.off[feature])

    def is_off(self, owner: Any, chat_id: Any, feature: str) -> bool:
        """Is ``feature`` off for this owner's chat? An unknown owner counts as off when any owner paused the chat."""
        _check_feature(feature)
        snapshot = self.snapshot()
        if snapshot.blind:
            return True
        chat = str(chat_id)
        if owner is None:
            return chat in snapshot.off[feature]
        entry = snapshot.owners.get(str(owner), {}).get(chat)
        return entry is not None and not entry.on(feature)

    def counts(self) -> dict[str, int]:
        """Chats with each feature off (CLI: a corrupt file raises)."""
        snapshot = self.read_strict()
        return {feature: sum(1 for chats in snapshot.owners.values() for entry in chats.values()
                             if not entry.on(feature)) for feature in FEATURES}

    def set_flag(self, owner: Any, chat: Any, feature: str, on: bool, *, via: str, now: datetime | None = None,
                 repair_corrupt: bool = False) -> ChatWriteResult:
        """Turn ``feature`` on (the default: the value is removed) or off for one owner's chat.

        Idempotent: a chat already in that state is not rewritten. A corrupt file is replaced, after a
        backup, only when ``repair_corrupt`` (an explicit CLI write with --owner).
        """
        _check_feature(feature)
        if via not in UPDATED_VIA:
            raise ValueError(f"updated_via must be one of {', '.join(UPDATED_VIA)}, got {via!r}")
        owner_key = _modes._validate_owner(owner)
        chat_key = _modes._validate_chat(chat)
        path = self._path_fn()
        with _modes._writer_lock(path):
            raw, backup = self._read_for_write(path, now=now, repair_corrupt=repair_corrupt)
            owners = raw.setdefault("owners", {})
            owner_value = owners.setdefault(owner_key, {"chats": {}})
            chats = owner_value.setdefault("chats", {})
            current = chats.get(chat_key)
            is_on = current is None or current.get(feature, "on") != OFF
            if is_on is bool(on) and backup is None:
                return ChatWriteResult(entry=_parse_entry(current) if current is not None else None, changed=False)
            if current is None and len(chats) >= MAX_CHATS_PER_OWNER:
                raise ValueError(f"owner already has {MAX_CHATS_PER_OWNER} chats with a stored chat setting")
            item = dict(current or {})
            if on:
                item.pop(feature, None)
            else:
                item[feature] = OFF
            entry = None
            if any(item.get(name) == OFF for name in FEATURES):
                item.update(updated_at=_modes._format_timestamp(now), updated_via=via)
                chats[chat_key] = item
                entry = _parse_entry(item)
            else:
                chats.pop(chat_key, None)  # nothing off: the chat follows the defaults again
                if not chats and set(owner_value) == {"chats"}:
                    owners.pop(owner_key, None)
            raw["version"] = SCHEMA_VERSION
            _modes._write_private_json_atomic(path, raw)
            self._remember_write(path, raw)
        return ChatWriteResult(entry=entry, changed=True, backup_path=backup)

    def _remember_write(self, path: Path, raw: dict[str, Any]) -> None:
        try:
            signature: Any = _modes._signature(os.stat(path))
        except OSError:
            signature = None
        key = str(path)
        with self._lock:
            self._damaged.pop(key, None)
            self._unreadable.discard(key)
            if signature is None:
                self._valid.pop(key, None)
            else:
                self._valid[key] = (signature, _parse_payload(raw))

    def _read_for_write(self, path: Path, *, now: datetime | None,
                        repair_corrupt: bool) -> tuple[dict[str, Any], Path | None]:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return {"version": SCHEMA_VERSION, "owners": {}}, None
        try:
            raw = _decode(data)
            _parse_payload(raw)
            return raw, None
        except ChatSettingsFileCorrupt as exc:
            if not repair_corrupt:
                # Telegram never overwrites a damaged file: the pauses in it may be the only copy.
                raise ChatSettingsFileCorrupt(f"{path} is corrupt ({exc}); {REPAIR_HINT}") from exc
            backup = _modes._backup_corrupt_file(path, now)
            logger.warning("%s: chat settings file %s was corrupt (%s); backed up to %s and started empty",
                           _history.PLUGIN_NAME, path, exc, backup)
            return {"version": SCHEMA_VERSION, "owners": {}}, backup


_STORE = ChatSettingsStore()


def default_store() -> ChatSettingsStore:
    return _STORE


def reset_in_memory_caches() -> None:
    _STORE.clear_cache()
    _SCOPE_WARNED.clear()


def known_owner(business_connection_id: Any) -> str | None:
    """The connection's owner without an API call: this process's lookups, then the modes binding."""
    return (_history.cached_business_owner(business_connection_id)
            or _modes.default_store().connection_owner(business_connection_id))


def paused(business_connection_id: Any, chat_id: Any, feature: str, owner: Any = None) -> bool:
    """Is ``feature`` paused for this chat by its owner? A dossier also pauses while the chat's history is off.

    No live lookup: the owner comes from ``owner``, the cache or a binding, and an unknown owner of a
    chat id that some owner paused counts as paused (fail closed).
    """
    _check_feature(feature)
    store = default_store()
    features = ("history",) if feature == "history" else FEATURES
    if not any(store.excluded_anywhere(name, chat_id) for name in features):
        return False
    owner = owner if owner is not None else known_owner(business_connection_id)
    return any(store.is_off(owner, chat_id, name) for name in features)


def _stored_on(snapshot: ChatSnapshot, owner: Any, chat: Any, feature: str) -> bool:
    """The chat's stored value; an unknown owner counts as off when any owner turned it off (fail closed)."""
    if owner is None:
        return str(chat) not in snapshot.off[feature]
    entry = snapshot.owners.get(str(owner), {}).get(str(chat))
    return entry is None or entry.on(feature)


def chat_state(owner: Any, chat: Any, *, chat_type: Any = None,
               snapshot: ChatSnapshot | None = None) -> tuple[str, str]:
    """(history, dossier) for one owner's chat, as the panel shows them. Blocking.

    history: "on" | "off" (this chat) | "settings" (off for all chats) | "unreadable" (chat-settings.json
    can't be read, everything paused).
    dossier: "on" | "off" (this chat) | "settings" (dossiers off for all chats) | "needs_history" (this
    chat's history is off) | "no_history" (history is not on for the chat) | "private_only".
    """
    snapshot = snapshot or default_store().snapshot()
    if not _settings.enabled("history"):
        history = "settings"
    elif snapshot.blind:
        history = "unreadable"
    else:
        history = "on" if _stored_on(snapshot, owner, chat, "history") else "off"
    if history not in ("on", "off"):
        return history, "no_history"
    if not _settings.enabled("dossiers"):
        return history, "settings"
    if history == "off":
        return history, "needs_history"
    if chat_type is not None and chat_type != "private":
        return history, "private_only"
    return history, "on" if _stored_on(snapshot, owner, chat, "dossier") else "off"


@dataclass(frozen=True)
class ChatScope:
    """What the CLI knows of each chat for ``chat_state``: its type, from the history catalog."""

    chat_types: Mapping[str, str] = field(default_factory=dict)

    def state(self, owner: str | None, chat: str, *, snapshot: ChatSnapshot | None = None) -> tuple[str, str]:
        return chat_state(owner, chat, chat_type=self.chat_types.get(chat), snapshot=snapshot)


def cli_scope() -> ChatScope:
    """One read of the history catalog for a CLI call."""
    chat_types: dict[str, str] = {}
    for entry in _modes._history_entries():
        chat_type = _history._normalize_chat_type(entry.get("chat_type"))
        if chat_type:
            chat_types.setdefault(str(entry.get("chat_id")), chat_type)
    return ChatScope(chat_types)


# ---------------------------------------------------------------------------
# One-time migration of the 0.12 history scope (TELESEC_HISTORY_CONNECTIONS / _CHATS / _CHAT_TYPES)
# ---------------------------------------------------------------------------

SCOPE_ENV = (_history.HISTORY_CONNECTIONS_ENV, _history.HISTORY_CHATS_ENV, _history.HISTORY_CHAT_TYPES_ENV)
SCOPE_MIGRATION = "history_scope"
MIGRATION_VIA = "migration"
SCOPE_HINT = "New chats are recorded unless you tap Manage Bot in the chat and choose Don't save."
_SCOPE_WARNED: set[str] = set()


def _legacy_scope() -> Callable[[str | None, str, str | None, Iterable[str]], bool]:
    """The 0.12 filter as it read the variables: does it let (connection, chat, chat type) into history?

    Without the chat's connection, an owner none of whose bound connections is allowed is out.
    """
    connections = _history._parse_scope(os.environ.get(_history.HISTORY_CONNECTIONS_ENV))
    chats = _history._parse_scope(os.environ.get(_history.HISTORY_CHATS_ENV))
    chat_types = _history._parse_chat_types(os.environ.get(_history.HISTORY_CHAT_TYPES_ENV))

    def allows(connection: str | None, chat: str, chat_type: str | None, owner_connections: Iterable[str]) -> bool:
        if connections is not None:
            known = (connection,) if connection else tuple(owner_connections)
            if known and not any(connections.matches(item) for item in known):
                return False
        if chats is not None and not chats.matches(chat):
            return False
        return (chats is not None and chats.contains_exact(chat)) or (chat_type or "private") in chat_types

    return allows


def _scope_targets() -> tuple[set[tuple[str, str]], int]:
    """(owner, chat) pairs the old scope kept out of history, and how many such chats have no known owner.

    Chats come from the history catalog (rebuilt when stale), transcription modes and chat-settings.json.
    Only private chats count: Business chats are private, and history never records another type.
    """
    allows = _legacy_scope()
    try:
        entries = _history._catalog_entries_for_cli()
    except ValueError:
        entries = _history.rebuild_contact_catalog().get("contacts", [])
    modes = _modes.default_store().read_strict()
    owner_connections: dict[str, list[str]] = {}
    for connection, owner in modes.connections.items():
        owner_connections.setdefault(str(owner), []).append(str(connection))
    targets: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    no_owner = 0
    for entry in entries:
        connection, chat = str(entry.get("business_connection_id") or ""), str(entry.get("chat_id"))
        chat_type = _history._normalize_chat_type(entry.get("chat_type"))
        if not connection or chat_type not in (None, "private"):
            continue
        owner = known_owner(connection)
        if owner is not None:
            seen.add((owner, chat))
        if allows(connection, chat, chat_type, ()):
            continue
        if owner is None:
            no_owner += 1
        else:
            targets.add((owner, chat))
    stored = [(owner, chat) for owner, chats in modes.items() for chat in chats]
    stored += [(owner, chat) for owner, chats in default_store().read_strict().owners.items() for chat in chats]
    for owner, chat in stored:
        if (owner, chat) not in seen and not allows(None, chat, None, owner_connections.get(owner, ())):
            targets.add((owner, chat))
    # A key the file would reject must never be written: it would pause every chat.
    return {(o, c) for o, c in targets if _OWNER_KEY_RE.match(o) and _CHAT_KEY_RE.match(c)}, no_owner


def _scope_migrated(raw: dict[str, Any]) -> bool:
    migrations = raw.get("migrations")
    return isinstance(migrations, dict) and SCOPE_MIGRATION in migrations


def _store_scope_targets(store: ChatSettingsStore, targets: set[tuple[str, str]], no_owner: int, *,
                         now: datetime | None) -> int | None:
    """Write "Don't save" for ``targets`` and the migration record in one locked write; None when already done."""
    path = store.path
    stamp = _modes._format_timestamp(now)
    changed = 0
    with _modes._writer_lock(path):
        raw, _backup = store._read_for_write(path, now=now, repair_corrupt=False)
        if _scope_migrated(raw):
            return None
        owners = raw.setdefault("owners", {})
        for owner, chat in sorted(targets):
            chats = owners.setdefault(owner, {"chats": {}}).setdefault("chats", {})
            item = chats.get(chat) or {}
            if item.get("history") != OFF:
                chats[chat] = {**item, "history": OFF, "updated_at": stamp, "updated_via": MIGRATION_VIA}
                changed += 1
        if not isinstance(raw.get("migrations"), dict):
            raw["migrations"] = {}
        raw["migrations"][SCOPE_MIGRATION] = {"at": stamp, "chats_off": changed, "no_owner": no_owner}
        raw["version"] = SCHEMA_VERSION
        _modes._write_private_json_atomic(path, raw)
        store._remember_write(path, raw)
    return changed


def migrate_history_scope(*, now: datetime | None = None) -> list[str]:
    """Store "Don't save" once for every chat the old TELESEC_HISTORY_* variables kept out of history.

    Runs only while one of them is set and chat-settings.json has no record of this migration; a
    damaged file, catalog or modes file leaves it for the next start. Records the run (counts only)
    under "migrations" in chat-settings.json. Returns the variables it moved, for ``warn_ignored_scope_env``.
    """
    names = [name for name in SCOPE_ENV if name in os.environ]
    if not names:
        return []
    store = default_store()
    path = store.path
    try:
        if _scope_migrated(store._read_for_write(path, now=now, repair_corrupt=False)[0]):
            return []
        targets, no_owner = _scope_targets()
        changed = _store_scope_targets(store, targets, no_owner, now=now)
    except (OSError, ValueError) as exc:
        logger.warning("%s: %s not moved to per-chat settings yet (%s); retrying at the next start",
                       _history.PLUGIN_NAME, ", ".join(names), type(exc).__name__)
        return []
    if changed is None:
        return []  # another process just ran it
    # WARNING, not INFO: register() runs before the gateway configures logging. Counts only, never ids or names.
    logger.warning("%s: %s no longer read. Chats outside the old scope now set to Don't save: %d. %s",
                   _history.PLUGIN_NAME, ", ".join(names) + (" is" if len(names) == 1 else " are"), changed,
                   SCOPE_HINT)
    if no_owner:
        logger.warning("%s: chats outside the old scope with no known owner: %d. They are recorded from now "
                       "on; tap Manage Bot in each and choose Don't save.", _history.PLUGIN_NAME, no_owner)
    return names


def warn_ignored_scope_env(skip: Iterable[str] = ()) -> list[str]:
    """One WARNING per process for each old scope variable still set; ``skip`` names the ones the migration
    just logged."""
    warned = []
    for name in SCOPE_ENV:
        if name not in os.environ or name in _SCOPE_WARNED:
            continue
        _SCOPE_WARNED.add(name)
        if name in skip:
            continue
        logger.warning("%s: %s is no longer read; history is set per chat: Manage Bot > Don't save, or "
                       "'hermes telesec chat set <chat> --history off'.", _history.PLUGIN_NAME, name)
        warned.append(name)
    return warned


# ---------------------------------------------------------------------------
# CLI: hermes telesec chat list | get <chat> | set <chat> [--history on|off] [--dossier on|off]
# ---------------------------------------------------------------------------


def add_chat_parser(subs: Any) -> None:
    chat = subs.add_parser("chat", help="Show or change history and dossier for one chat")
    chat_subs = chat.add_subparsers(dest="telesec_chat_command")

    list_parser = chat_subs.add_parser("list", help="List chats with history or dossier off")
    list_parser.add_argument("--owner")
    list_parser.add_argument("--format", choices=["text", "json"], default="text")
    list_parser.set_defaults(_chat_action="list")

    get_parser = chat_subs.add_parser("get", help="Show history and dossier for one chat")
    get_parser.add_argument("chat")
    get_parser.add_argument("--owner")
    get_parser.add_argument("--connection")
    get_parser.add_argument("--format", choices=["text", "json"], default="text")
    get_parser.set_defaults(_chat_action="get")

    set_parser = chat_subs.add_parser("set", help="Turn history or the dossier on or off for one chat")
    set_parser.add_argument("chat")
    set_parser.add_argument("--history", choices=["on", "off"])
    set_parser.add_argument("--dossier", choices=["on", "off"])
    set_parser.add_argument("--owner")
    set_parser.add_argument("--connection")
    set_parser.set_defaults(_chat_action="set")


def _flag(on: bool) -> str:
    return "on" if on else "off"


def _entry_json(owner: str | None, chat: str, entry: ChatEntry | None, *, scope: ChatScope,
                connection: str | None = None, label: str | None = None,
                snapshot: ChatSnapshot | None = None) -> dict[str, Any]:
    entry_or_default = entry or ChatEntry()
    history, dossier = scope.state(owner, chat, snapshot=snapshot)
    return {
        "owner": owner,
        "chat_id": chat,
        "business_connection_id": connection,
        "label": label,
        "history": _flag(entry_or_default.history),
        "dossier": _flag(entry_or_default.dossier),
        "effective_history": "on" if history == "on" else "off",
        "effective_dossier": "on" if dossier == "on" else "off",
        "history_state": history,
        "dossier_state": dossier,
        "updated_via": entry.updated_via if entry is not None else None,
        "updated_at": entry.updated_at if entry is not None else None,
    }


def _modes_snapshot(owner_arg: str | None) -> _modes.Snapshot:
    """Owner bindings for the owner inference; a corrupt modes file needs --owner."""
    try:
        return _modes.default_store().read_strict()
    except _modes.ModesFileCorrupt:
        if owner_arg is None:
            raise ValueError(f"{_modes.MODES_FILENAME} is corrupt, so the owner can't be inferred; pass --owner")
        return _modes.Snapshot()


def _single_holder(store: ChatSettingsStore, chat: str) -> str | None:
    """The owner when exactly one stored a setting for this chat."""
    holders = [owner for owner, chats in store.read_strict().owners.items() if chat in chats]
    return holders[0] if len(holders) == 1 else None


def _resolve_owner(store: ChatSettingsStore, owner_arg: str | None, chat: str, connection: str | None,
                   action: str) -> str | None:
    """--owner, else the owner the modes bindings give, else the one owner holding this chat's setting."""
    try:
        owner = _modes._resolve_chat_owner(_modes_snapshot(owner_arg), owner_arg, connection, action=action)
    except ValueError:
        if owner_arg is not None:
            raise
        holder = _single_holder(store, chat)
        if holder is None:
            raise
        return holder
    return owner if owner is not None else _single_holder(store, chat)


def _print_json(document: Any) -> None:
    # ASCII only, like `mode`: labels are contact names, and escapes keep terminal controls inert.
    print(json.dumps(document, ensure_ascii=True, indent=2))


def _entry_text(entry: ChatEntry | None) -> str:
    entry = entry or ChatEntry()
    return f"history={_flag(entry.history)} dossier={_flag(entry.dossier)}"


def _list(args: argparse.Namespace, store: ChatSettingsStore) -> int:
    snapshot = store.read_strict()
    owner_arg = getattr(args, "owner", None)
    owner = _modes._validate_owner(owner_arg) if owner_arg is not None else None
    rows = [(owner_key, chat, entry) for owner_key in sorted(snapshot.owners, key=int)
            if owner is None or owner_key == owner
            for chat, entry in sorted(snapshot.owners[owner_key].items(), key=lambda item: int(item[0]))]
    if getattr(args, "format", None) == "json":
        scope = cli_scope()
        _print_json({"history_enabled": _settings.enabled("history"),
                     "dossiers_enabled": _settings.enabled("dossiers"),
                     "chats": [_entry_json(o, c, e, scope=scope, snapshot=snapshot) for o, c, e in rows]})
        return 0
    if not rows:
        print("No chat has history or the dossier off; every chat follows Settings.")
    for owner_key, chat, entry in rows:
        print(f"owner={owner_key} chat={chat} {_entry_text(entry)} via={entry.updated_via} "
              f"updated_at={entry.updated_at}")
    return 0


def handle_chat_cli(args: argparse.Namespace, *, store: ChatSettingsStore | None = None) -> int:
    action = getattr(args, "_chat_action", None)
    if action is None:
        print(CHAT_USAGE)
        return 1
    store = store or default_store()
    try:
        if action == "list":
            return _list(args, store)
        owner_arg = getattr(args, "owner", None)
        chat, label, connection = _modes._resolve_chat_selector(args.chat, getattr(args, "connection", None))
        owner = _resolve_owner(store, owner_arg, chat, connection, action)
        if action == "get":
            snapshot = store.read_strict()
            entry = snapshot.owners.get(owner, {}).get(chat) if owner is not None else None
            scope = cli_scope()
            if getattr(args, "format", None) == "json":
                _print_json(_entry_json(owner, chat, entry, scope=scope, connection=connection, label=label,
                                        snapshot=snapshot))
                return 0
            history, dossier = scope.state(owner, chat, snapshot=snapshot)
            stored = "" if entry is None else f" (via {entry.updated_via} at {entry.updated_at})"
            print(f"chat={chat} {_entry_text(entry)}{stored}")
            print(f"in effect: history={history} dossier={dossier}")
            return 0
        if action == "set":
            changes = [(feature, getattr(args, feature)) for feature in FEATURES
                       if getattr(args, feature, None) is not None]
            if not changes:
                raise ValueError("pass --history on|off, --dossier on|off, or both")
            entry = None
            for feature, value in changes:
                result = store.set_flag(owner, chat, feature, value == "on", via="cli",
                                        repair_corrupt=owner_arg is not None)
                if result.backup_path is not None:
                    print(f"warning: corrupt {SETTINGS_FILENAME} backed up to {result.backup_path}")
                entry = result.entry
            print(f"chat={chat} {_entry_text(entry)}")
            print("The gateway applies it to the next update; no restart is needed.")
            return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(CHAT_USAGE)
    return 1
