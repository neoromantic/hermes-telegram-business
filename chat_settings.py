"""Per-chat history and dossier switches: "Don't save" and "No dossier" for one chat.

The owner panel and ``hermes telesec chat set`` turn history or the dossier off for one chat. The
values are stored per (Business owner, chat) like transcription modes, in
``$HERMES_HOME/data/telesec/chat-settings.json``; "on" is the default and removes the value. The file
is separate from ``transcription-modes.json`` because older versions drop unknown keys there on their
next write, so a rollback would lose these pauses. Readers stat the file on every call and re-read it
only when it changed, so a change applies to the next update without a restart.

A pause fails closed: a damaged file keeps the last valid settings, and with none (a cold start)
history capture and dossiers pause for every chat until the file is repaired. Off is not delete:
saved history and the stored dossier stay until the owner erases them (``history_support.forget_chat``).
Stdlib-only; the file never holds message text or names.

Up to 0.13, TELESEC_HISTORY_CONNECTIONS, _CHATS and _CHAT_TYPES decided which chats history recorded.
``migrate_history_scope()`` stores that filter once, as 0.13 parsed it, under "legacy_scope": the old
scope rule. It still keeps chats out of history per (connection, chat), and their dossiers pause, unless
the owner turns a chat on: that one value is stored as an explicit "on". While the variables stay set
they apply too (fail closed: a rollback to 0.13 may have changed them), so either keeps a chat out.
``hermes telesec chat scope clear`` drops the stored rule.
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
ON = "on"
UPDATED_VIA = ("chat", "cli")
SCHEMA_VERSION = 1
SETTINGS_FILENAME = "chat-settings.json"
MAX_CHATS_PER_OWNER = _modes.MAX_CHATS_PER_OWNER
CHAT_USAGE = "Usage: hermes telesec chat <list|get|set|scope>"
REPAIR_HINT = "'hermes telesec chat set <chat> ... --owner ID' backs it up and starts empty"
LEGACY_SCOPE_KEY = "legacy_scope"
SCOPE_MIGRATION = "history_scope"
TURN_ON_HINT = "Manage Bot > Save history in the chat, or 'hermes telesec chat set <chat> --history on'"
SCOPE_CLEAR_COMMAND = "hermes telesec chat scope clear --yes"
DOWNGRADE_NOTE = ("Telesec 0.13 or older reads only the variables, so keep them set while you may go back to it, "
                  "and remove them after that.")

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
    # Features stored as an explicit "on": history the owner turned on over the old scope rule.
    explicit_on: frozenset[str] = frozenset()

    def on(self, feature: str) -> bool:
        return self.history if feature == "history" else self.dossier


@dataclass(frozen=True)
class OldScope:
    """The 0.13 history scope as 0.13 parsed TELESEC_HISTORY_CONNECTIONS, _CHATS and _CHAT_TYPES.

    A value 0.13 matched nothing with (a chat type typo, a stale connection) still matches nothing.
    """

    connections: _history.Scope | None = None
    chats: _history.Scope | None = None
    chat_types: frozenset[str] = _history.DEFAULT_HISTORY_CHAT_TYPES

    @classmethod
    def from_env(cls) -> OldScope:
        return cls(_history._parse_scope(os.environ.get(_history.HISTORY_CONNECTIONS_ENV)),
                   _history._parse_scope(os.environ.get(_history.HISTORY_CHATS_ENV)),
                   _history._parse_chat_types(os.environ.get(_history.HISTORY_CHAT_TYPES_ENV)))

    def allows(self, connection: Any, chat: Any, chat_type: str = "private") -> bool:
        """0.13's check for one update; an unknown connection (None) passes only a wildcard or no filter."""
        if self.connections is not None and not (
                self.connections.wildcard or (connection is not None and self.connections.contains_exact(connection))):
            return False
        if self.chats is not None and not self.chats.matches(chat):
            return False
        return (self.chats is not None and self.chats.contains_exact(chat)) or chat_type in self.chat_types

    def admits_all(self) -> bool:
        """True when it keeps no private chat out, so there is nothing to store."""
        return all(scope is None or scope.wildcard for scope in (self.connections, self.chats)) and (
            "private" in self.chat_types)

    def to_json(self) -> dict[str, Any]:
        def scope(value: _history.Scope | None) -> Any:
            return None if value is None else "*" if value.wildcard else sorted(value.values)

        return {"connections": scope(self.connections), "chats": scope(self.chats),
                "chat_types": sorted(self.chat_types)}

    @classmethod
    def from_json(cls, value: Any) -> OldScope:
        def strings(item: Any, name: str) -> list[str]:
            if not isinstance(item, list) or not all(isinstance(part, str) and part for part in item):
                raise ChatSettingsFileCorrupt(f"invalid {LEGACY_SCOPE_KEY} {name}")
            return item

        def scope(item: Any, name: str) -> _history.Scope | None:
            if item is None or item == "*":
                return None if item is None else _history.Scope(wildcard=True, values=frozenset())
            return _history.Scope(wildcard=False, values=frozenset(strings(item, name)))

        if not isinstance(value, dict):
            raise ChatSettingsFileCorrupt(f"{LEGACY_SCOPE_KEY} is not an object")
        return cls(scope(value.get("connections"), "connections"), scope(value.get("chats"), "chats"),
                   frozenset(strings(value.get("chat_types"), "chat_types")))

    def summary(self) -> str:
        """Counts only: never the connection or chat ids it lists."""
        def scope(value: _history.Scope | None, noun: str) -> str:
            if value is None or value.wildcard:
                return f"any {noun}"
            return f"{len(value.values)} {noun}{'' if len(value.values) == 1 else 's'} listed"

        types = ", ".join(sorted(self.chat_types)) or "none"
        return f"{scope(self.connections, 'connection')}, {scope(self.chats, 'chat')}, chat types: {types}"


@dataclass(frozen=True)
class ChatSnapshot:
    """owner -> chat -> entry, plus each feature's chat ids that some owner turned off."""

    owners: Mapping[str, Mapping[str, ChatEntry]] = field(default_factory=dict)
    off: Mapping[str, frozenset[str]] = field(default_factory=lambda: {f: frozenset() for f in FEATURES})
    # The file can't be read and no earlier read was valid: history and dossiers pause for every chat.
    blind: bool = False
    scope: OldScope | None = None  # the stored old scope rule
    scope_recorded: bool = False  # migrate_history_scope ran: a later start never stores the variables again


_EMPTY = ChatSnapshot()
_BLIND = ChatSnapshot(blind=True)


@dataclass(frozen=True)
class ChatWriteResult:
    entry: ChatEntry | None  # None: nothing is off for the chat any more
    changed: bool
    backup_path: Path | None = None
    # A repair's old scope rule: "kept" from the damaged file, "variables" stored again, "lost" with the backup.
    scope_repair: str | None = None


def _check_feature(feature: str) -> None:
    if feature not in FEATURES:
        raise ValueError(f"feature must be one of {', '.join(FEATURES)}, got {feature!r}")


def _parse_entry(value: Any) -> ChatEntry:
    if not isinstance(value, dict):
        raise ChatSettingsFileCorrupt("chat entry is not an object")
    flags = {}
    for feature in FEATURES:
        flag = value.get(feature)
        if flag not in (None, ON, OFF):
            # An unknown value must not quietly resume capture: the whole file counts as damaged.
            raise ChatSettingsFileCorrupt(f"invalid {feature} value")
        flags[feature] = flag != OFF
    at, via = value.get("updated_at"), value.get("updated_via")
    return ChatEntry(**flags, updated_at=at if isinstance(at, str) else None,
                     updated_via=via if isinstance(via, str) else None,
                     explicit_on=frozenset(feature for feature in FEATURES if value.get(feature) == ON))


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
    scope = raw.get(LEGACY_SCOPE_KEY)
    return ChatSnapshot(owners, {feature: frozenset(chats) for feature, chats in off.items()},
                        scope=None if scope is None else OldScope.from_json(scope), scope_recorded=_scope_recorded(raw))


def _scope_recorded(raw: dict[str, Any]) -> bool:
    migrations = raw.get("migrations")
    return isinstance(migrations, dict) and SCOPE_MIGRATION in migrations


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
        """Chats with each feature off, chats turned on over the old scope rule, and whether that rule applies
        (CLI: a corrupt file raises)."""
        snapshot = self.read_strict()
        entries = [entry for chats in snapshot.owners.values() for entry in chats.values()]
        counts = {feature: sum(1 for entry in entries if not entry.on(feature)) for feature in FEATURES}
        counts["history_on"] = sum(1 for entry in entries if "history" in entry.explicit_on)
        counts["old_scope"] = int(bool(active_scopes(snapshot)))
        return counts

    def set_flag(self, owner: Any, chat: Any, feature: str, on: bool, *, via: str, now: datetime | None = None,
                 repair_corrupt: bool = False, connections: Iterable[Any] = ()) -> ChatWriteResult:
        """Turn ``feature`` on (the default: the value is removed) or off for one owner's chat.

        History turned on for a chat the old scope rule keeps out on one of ``connections`` (the chat's;
        none: unknown) is stored as an explicit "on", which overrides the rule. Idempotent: a chat already
        in that state is not rewritten. A corrupt file is replaced, after a backup, only when
        ``repair_corrupt`` (an explicit CLI write with --owner).
        """
        _check_feature(feature)
        if via not in UPDATED_VIA:
            raise ValueError(f"updated_via must be one of {', '.join(UPDATED_VIA)}, got {via!r}")
        owner_key = _modes._validate_owner(owner)
        chat_key = _modes._validate_chat(chat)
        path = self._path_fn()
        with _modes._writer_lock(path):
            raw, backup, scope_repair = self._read_for_write(path, now=now, repair_corrupt=repair_corrupt)
            owners = raw.setdefault("owners", {})
            owner_value = owners.setdefault(owner_key, {"chats": {}})
            chats = owner_value.setdefault("chats", {})
            current = chats.get(chat_key)
            stored = (current or {}).get(feature)
            if not on:
                wanted = OFF
            elif stored == ON or (feature == "history" and scope_keeps_out(_parse_payload(raw), connections, chat_key)):
                wanted = ON
            else:
                wanted = None
            if stored == wanted and backup is None:
                return ChatWriteResult(entry=_parse_entry(current) if current is not None else None, changed=False)
            if current is None and len(chats) >= MAX_CHATS_PER_OWNER:
                raise ValueError(f"owner already has {MAX_CHATS_PER_OWNER} chats with a stored chat setting")
            item = dict(current or {})
            if wanted is None:
                item.pop(feature, None)
            else:
                item[feature] = wanted
            entry = None
            if any(name in item for name in FEATURES):
                item.update(updated_at=_modes._format_timestamp(now), updated_via=via)
                chats[chat_key] = item
                entry = _parse_entry(item)
            else:
                _drop_chat(owners, owner_key, chat_key)  # nothing stored: the chat follows the defaults again
            raw["version"] = SCHEMA_VERSION
            _modes._write_private_json_atomic(path, raw)
            self._remember_write(path, raw)
        return ChatWriteResult(entry=entry, changed=True, backup_path=backup, scope_repair=scope_repair)

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
                        repair_corrupt: bool) -> tuple[dict[str, Any], Path | None, str | None]:
        """(the file's JSON, the backup of a repaired file, what the repair did with the old scope rule)."""
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return {"version": SCHEMA_VERSION, "owners": {}}, None, None
        try:
            raw = _decode(data)
            _parse_payload(raw)
            return raw, None, None
        except ChatSettingsFileCorrupt as exc:
            if not repair_corrupt:
                # Telegram never overwrites a damaged file: the pauses in it may be the only copy.
                raise ChatSettingsFileCorrupt(f"{path} is corrupt ({exc}); {REPAIR_HINT}") from exc
            backup = _modes._backup_corrupt_file(path, now)
            logger.warning("%s: chat settings file %s was corrupt (%s); backed up to %s and started empty",
                           _history.PLUGIN_NAME, path, exc, backup)
            raw, scope_repair = _repaired_scope(data, now=now)
            if scope_repair == "lost":
                logger.warning("%s: the old history scope rule was lost with the backup; chats it kept out now "
                               "save history", _history.PLUGIN_NAME)
            return raw, backup, scope_repair


def _repaired_scope(data: bytes, *, now: datetime | None) -> tuple[dict[str, Any], str | None]:
    """A repaired file's start and what it did with the old scope rule (None: there was none).

    It keeps the rule and its migration record from the damaged file when both still parse, else stores the
    rule again from the variables when they are set, else reports the rule lost when the file named one.
    """
    raw: dict[str, Any] = {"version": SCHEMA_VERSION, "owners": {}}
    try:
        damaged = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        damaged = None
    if isinstance(damaged, dict) and _scope_recorded(damaged):
        try:
            rule = damaged.get(LEGACY_SCOPE_KEY)
            if rule is not None:
                OldScope.from_json(rule)
        except ChatSettingsFileCorrupt:
            pass
        else:
            raw["migrations"] = damaged["migrations"]
            if rule is not None:
                raw[LEGACY_SCOPE_KEY] = rule
            return raw, "kept" if rule is not None else None
    if _scope_env_names():
        rule = OldScope.from_env()
        if not rule.admits_all():
            raw[LEGACY_SCOPE_KEY] = rule.to_json()
        raw["migrations"] = {SCOPE_MIGRATION: {"at": _modes._format_timestamp(now), "rule": not rule.admits_all()}}
        return raw, "variables" if not rule.admits_all() else None
    return raw, "lost" if LEGACY_SCOPE_KEY.encode() in data else None


def _drop_chat(owners: dict[str, Any], owner_key: str, chat_key: str) -> None:
    owner_value = owners.get(owner_key) or {}
    chats = owner_value.get("chats") or {}
    chats.pop(chat_key, None)
    if not chats and set(owner_value) <= {"chats"}:
        owners.pop(owner_key, None)


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


# ---------------------------------------------------------------------------
# The old scope rule (TELESEC_HISTORY_CONNECTIONS / _CHATS / _CHAT_TYPES up to 0.13)
# ---------------------------------------------------------------------------

SCOPE_ENV = (_history.HISTORY_CONNECTIONS_ENV, _history.HISTORY_CHATS_ENV, _history.HISTORY_CHAT_TYPES_ENV)
_SCOPE_WARNED: set[str] = set()
_ENV_SCOPE: tuple[tuple[str | None, ...], OldScope | None] = ((None, None, None), None)


def _scope_env_names() -> list[str]:
    """The old scope variables that hold a value; an empty one was no filter in 0.13."""
    return [name for name in SCOPE_ENV if os.environ.get(name, "").replace(",", " ").strip()]


def _env_scope() -> OldScope | None:
    """The variables as 0.13 read them, while they are set; parsed again only when they change."""
    global _ENV_SCOPE
    raw = tuple(os.environ.get(name) for name in SCOPE_ENV)
    cached_raw, cached = _ENV_SCOPE
    if raw == cached_raw:
        return cached
    rule = OldScope.from_env()
    rule = None if rule.admits_all() else rule
    _ENV_SCOPE = (raw, rule)
    return rule


def active_scopes(snapshot: ChatSnapshot) -> tuple[OldScope, ...]:
    """The old scope rules in effect: the stored one, and the variables while they are set (fail closed: a
    rollback to 0.13 may have changed them after the rule was stored)."""
    return tuple(rule for rule in (snapshot.scope, _env_scope()) if rule is not None)


def scope_keeps_out(snapshot: ChatSnapshot, connections: Iterable[Any], chat: Any, *,
                    everywhere: bool = False) -> bool:
    """Does an old scope rule keep this private chat out of history on one of ``connections`` (on every one
    with ``everywhere``; none: unknown)?"""
    rules = active_scopes(snapshot)
    if not rules:
        return False
    found = all if everywhere else any
    return found(any(not rule.allows(connection, chat) for rule in rules)
                 for connection in (tuple(connections) or (None,)))


def _turned_on(snapshot: ChatSnapshot, owner: Any, chat: Any) -> bool:
    """The owner stored an explicit "on" for this chat's history; an unknown owner never has (fail closed)."""
    if owner is None:
        return False
    entry = snapshot.owners.get(str(owner), {}).get(str(chat))
    return entry is not None and "history" in entry.explicit_on


def _stored_on(snapshot: ChatSnapshot, owner: Any, chat: Any, feature: str) -> bool:
    """The chat's stored value; an unknown owner counts as off when any owner turned it off (fail closed)."""
    if owner is None:
        return str(chat) not in snapshot.off[feature]
    entry = snapshot.owners.get(str(owner), {}).get(str(chat))
    return entry is None or entry.on(feature)


def _features(feature: str) -> tuple[str, ...]:
    _check_feature(feature)
    return ("history",) if feature == "history" else FEATURES


def may_be_paused(business_connection_id: Any, chat_id: Any, feature: str) -> bool:
    """False when ``feature`` surely runs for this chat whoever its owner is: the fast path, no owner lookup."""
    snapshot = default_store().snapshot()
    return (snapshot.blind or scope_keeps_out(snapshot, (business_connection_id,), chat_id)
            or any(str(chat_id) in snapshot.off[name] for name in _features(feature)))


def paused(business_connection_id: Any, chat_id: Any, feature: str, owner: Any = None) -> bool:
    """Is ``feature`` paused for this chat? A dossier also pauses while the chat's history is off.

    History is off when the owner chose Don't save, or when the old scope rule keeps the chat of this
    connection out and the owner did not turn it on. No live lookup: the owner comes from ``owner``,
    the cache or a binding, and an unknown owner counts as paused when either could apply (fail closed).
    """
    return pause_reason(business_connection_id, chat_id, feature, owner) is not None


def pause_reason(business_connection_id: Any, chat_id: Any, feature: str, owner: Any = None) -> str | None:
    """Why ``paused`` is True: "unreadable" (everything paused), "off" (Don't save or No dossier) or "scope"
    (the old scope rule); None when ``feature`` runs for the chat."""
    features = _features(feature)
    snapshot = default_store().snapshot()
    if snapshot.blind:
        return "unreadable"
    kept_out = scope_keeps_out(snapshot, (business_connection_id,), chat_id)
    if not kept_out and not any(str(chat_id) in snapshot.off[name] for name in features):
        return None
    owner = owner if owner is not None else known_owner(business_connection_id)
    if not all(_stored_on(snapshot, owner, chat_id, name) for name in features):
        return "off"
    return "scope" if kept_out and not _turned_on(snapshot, owner, chat_id) else None


def cli_owner(business_connection_id: Any, chat_id: Any) -> str | None:
    """The chat's owner for a CLI command without --owner, found as ``chat set`` finds it: the connection's
    binding (or the one known owner), else the one owner holding a setting for the chat. None when unknown,
    so ``paused`` fails closed."""
    try:
        return _resolve_owner(default_store(), None, str(chat_id), str(business_connection_id), "check")
    except (OSError, ValueError):
        return None


def chat_state(owner: Any, chat: Any, *, chat_type: Any = None, connections: Iterable[Any] = (),
               snapshot: ChatSnapshot | None = None) -> tuple[str, str]:
    """(history, dossier) for one owner's chat, as the panel shows them. Blocking.

    history: "on" | "off" (this chat) | "scope" (the old scope rule keeps the chat out on every one of
    ``connections``, the ones it is captured through; none: unknown) | "settings" (off for all chats) |
    "unreadable" (chat-settings.json can't be read, everything paused).
    dossier: "on" | "off" (this chat) | "settings" (dossiers off for all chats) | "needs_history" (this
    chat's history is off) | "no_history" (history is not on for the chat) | "private_only".
    """
    snapshot = snapshot or default_store().snapshot()
    if not _settings.enabled("history"):
        history = "settings"
    elif snapshot.blind:
        history = "unreadable"
    elif not _stored_on(snapshot, owner, chat, "history"):
        history = "off"
    elif scope_keeps_out(snapshot, connections, chat, everywhere=True) and not _turned_on(snapshot, owner, chat):
        history = "scope"
    else:
        history = "on"
    if history not in ("on", "off", "scope"):
        return history, "no_history"
    if not _settings.enabled("dossiers"):
        return history, "settings"
    if history != "on":
        return history, "needs_history"
    if chat_type is not None and chat_type != "private":
        return history, "private_only"
    return history, "on" if _stored_on(snapshot, owner, chat, "dossier") else "off"


@dataclass(frozen=True)
class ChatScope:
    """What the CLI knows of each chat for ``chat_state``: its type and connections, from the history
    catalog, and each connection's owner, from the modes bindings."""

    chat_types: Mapping[str, str] = field(default_factory=dict)
    chat_connections: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    owners: Mapping[str, str] = field(default_factory=dict)

    def connections(self, owner: str | None, chat: str) -> tuple[str, ...]:
        """The chat's connections of this owner, else every connection bound to the owner."""
        seen = tuple(c for c in self.chat_connections.get(chat, ())
                     if owner is None or self.owners.get(c) in (None, owner))
        return seen or tuple(c for c, bound in self.owners.items() if owner is not None and bound == owner)

    def state(self, owner: str | None, chat: str, *, connection: str | None = None,
              snapshot: ChatSnapshot | None = None) -> tuple[str, str]:
        connections = (connection,) if connection else self.connections(owner, chat)
        return chat_state(owner, chat, chat_type=self.chat_types.get(chat), connections=connections,
                          snapshot=snapshot)


def cli_scope() -> ChatScope:
    """One read of the history catalog and the modes bindings for a CLI call."""
    chat_types: dict[str, str] = {}
    chat_connections: dict[str, list[str]] = {}
    for entry in _modes._history_entries():
        chat, connection = str(entry.get("chat_id")), str(entry.get("business_connection_id") or "")
        chat_type = _history._normalize_chat_type(entry.get("chat_type"))
        if chat_type:
            chat_types.setdefault(chat, chat_type)
        if connection:
            chat_connections.setdefault(chat, []).append(connection)
    try:
        owners = {str(c): str(o) for c, o in _modes.default_store().read_strict().connections.items()}
    except (OSError, ValueError):
        owners = {}  # no bindings: the rule is checked as for an unknown connection
    return ChatScope(chat_types, {chat: tuple(items) for chat, items in chat_connections.items()}, owners)


# ---------------------------------------------------------------------------
# One-time migration: the 0.13 history scope becomes the stored old scope rule
# ---------------------------------------------------------------------------

def _store_scope(store: ChatSettingsStore, rule: OldScope, *, now: datetime | None) -> bool:
    """Store the rule (when it keeps any chat out) and the migration record in one locked write.

    False when another process already did. No owner lookup and no history read, so nothing else can fail.
    """
    path = store.path
    with _modes._writer_lock(path):
        raw, _backup, _repair = store._read_for_write(path, now=now, repair_corrupt=False)
        if _scope_recorded(raw):
            return False
        if not rule.admits_all():
            raw[LEGACY_SCOPE_KEY] = rule.to_json()
        if not isinstance(raw.get("migrations"), dict):
            raw["migrations"] = {}
        raw["migrations"][SCOPE_MIGRATION] = {"at": _modes._format_timestamp(now), "rule": not rule.admits_all()}
        raw["version"] = SCHEMA_VERSION
        _modes._write_private_json_atomic(path, raw)
        store._remember_write(path, raw)
    return True


def migrate_history_scope(*, now: datetime | None = None) -> list[str]:
    """Store the old TELESEC_HISTORY_* filter once, as 0.13 parsed it, as the old scope rule.

    Runs while one of the variables is set and chat-settings.json has no record of this migration.
    When the write fails the variables keep applying in this process (``active_scopes``) and the next start
    retries. Returns the variables it logged about, for ``warn_ignored_scope_env``.
    """
    names = _scope_env_names()
    if not names:
        return []
    store = default_store()
    rule = OldScope.from_env()
    listed = ", ".join(names)
    try:
        if not _store_scope(store, rule, now=now):
            return []  # done at an earlier start, or by another process just now
    except (OSError, ValueError) as exc:
        logger.warning("%s: could not store the old history scope (%s); %s still apply in this process, and the "
                       "next start retries", _history.PLUGIN_NAME, type(exc).__name__, listed)
        return names
    # WARNING, not INFO: register() runs before the gateway configures logging. Counts only, never ids.
    if rule.admits_all():
        logger.warning("%s: the old history scope in %s kept no private chat out, so no rule was stored. %s",
                       _history.PLUGIN_NAME, listed, DOWNGRADE_NOTE)
    else:
        logger.warning("%s: the old history scope (%s) is now a stored rule in %s. Chats it keeps out don't save "
                       "history until you turn them on: %s. '%s' drops the rule. While %s stay set they apply "
                       "too. %s", _history.PLUGIN_NAME, rule.summary(), SETTINGS_FILENAME, TURN_ON_HINT,
                       SCOPE_CLEAR_COMMAND, listed, DOWNGRADE_NOTE)
    return names


def warn_ignored_scope_env(skip: Iterable[str] = ()) -> list[str]:
    """One WARNING per process for each old scope variable still set once the rule is stored; ``skip`` names
    the ones the migration just logged."""
    if not default_store().snapshot().scope_recorded:
        return []  # not stored yet: the migration logs about them
    warned = []
    for name in _scope_env_names():
        if name in _SCOPE_WARNED:
            continue
        _SCOPE_WARNED.add(name)
        if name in skip:
            continue
        logger.warning("%s: %s is still set, so it keeps chats out of history together with the old history scope "
                       "rule stored in %s. %s See 'hermes telesec chat scope'.", _history.PLUGIN_NAME, name,
                       SETTINGS_FILENAME, DOWNGRADE_NOTE)
        warned.append(name)
    return warned


def clear_scope(store: ChatSettingsStore | None = None, *, now: datetime | None = None) -> bool:
    """Drop the stored old scope rule. True when one was stored.

    Also records the migration, so a later start never stores the variables again. The explicit "on" values
    that only overrode the rule go too, unless a variable is still set: they override it as well.
    """
    store = store or default_store()
    path = store.path
    keep_on = bool(_scope_env_names())
    with _modes._writer_lock(path):
        raw, _backup, _repair = store._read_for_write(path, now=now, repair_corrupt=False)
        applied = raw.get(LEGACY_SCOPE_KEY) is not None
        raw.pop(LEGACY_SCOPE_KEY, None)
        if not isinstance(raw.get("migrations"), dict):
            raw["migrations"] = {}
        stamp = _modes._format_timestamp(now)
        record = raw["migrations"].get(SCOPE_MIGRATION)
        raw["migrations"][SCOPE_MIGRATION] = {**(record if isinstance(record, dict) else {"at": stamp}),
                                              "cleared_at": stamp}
        owners = raw.setdefault("owners", {})
        for owner_key, owner_value in list(owners.items()):
            for chat_key, item in list((owner_value.get("chats") or {}).items()):
                if not keep_on and isinstance(item, dict) and item.get("history") == ON:
                    item.pop("history")
                    if not any(name in item for name in FEATURES):
                        _drop_chat(owners, owner_key, chat_key)
        raw["version"] = SCHEMA_VERSION
        _modes._write_private_json_atomic(path, raw)
        store._remember_write(path, raw)
    return applied


# ---------------------------------------------------------------------------
# CLI: hermes telesec chat list | get <chat> | set <chat> [--history on|off] [--dossier on|off] | scope [clear]
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

    scope_parser = chat_subs.add_parser("scope", help="Show or drop the old history scope rule")
    scope_parser.set_defaults(_chat_action="scope")
    scope_subs = scope_parser.add_subparsers(dest="telesec_chat_scope_command")
    clear_parser = scope_subs.add_parser("clear", help="Drop the old history scope rule")
    clear_parser.add_argument("--yes", action="store_true", help="Drop it; without --yes only explains")
    clear_parser.set_defaults(_chat_action="scope_clear")


def _flag(on: bool) -> str:
    return "on" if on else "off"


def _entry_json(owner: str | None, chat: str, entry: ChatEntry | None, *, scope: ChatScope,
                connection: str | None = None, label: str | None = None,
                snapshot: ChatSnapshot | None = None) -> dict[str, Any]:
    entry_or_default = entry or ChatEntry()
    history, dossier = scope.state(owner, chat, connection=connection, snapshot=snapshot)
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


def _scope_json(snapshot: ChatSnapshot) -> dict[str, Any] | None:
    """The old scope rules in effect, counts only: the stored one and the variables; None when neither applies."""
    env = _env_scope()
    if snapshot.scope is None and env is None:
        return None

    def rule_json(rule: OldScope | None) -> dict[str, Any] | None:
        def count(value: _history.Scope | None) -> Any:
            return "any" if value is None or value.wildcard else len(value.values)

        return None if rule is None else {"connections": count(rule.connections), "chats": count(rule.chats),
                                          "chat_types": sorted(rule.chat_types)}

    return {"stored": rule_json(snapshot.scope), "variables": rule_json(env)}


def _scope_lines(snapshot: ChatSnapshot) -> list[str]:
    """The old scope rules in effect, counts only; none when neither applies."""
    lines = []
    if snapshot.scope is not None:
        lines.append(f"Old history scope ({snapshot.scope.summary()}): a stored rule in {SETTINGS_FILENAME}.")
    env = _env_scope()
    if env is not None:
        also = (": the next start stores it as the rule" if not snapshot.scope_recorded
                else ", with the stored rule" if snapshot.scope is not None else "")
        lines.append(f"Old history scope from {', '.join(_scope_env_names())} ({env.summary()}): applies while "
                     f"they stay set{also}.")
    if lines:
        lines.append("Chats it keeps out don't save history until turned on; see 'hermes telesec chat scope'.")
    return lines


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
                     "old_scope": _scope_json(snapshot),
                     "chats": [_entry_json(o, c, e, scope=scope, snapshot=snapshot) for o, c, e in rows]})
        return 0
    scope_lines = _scope_lines(snapshot)
    for line in scope_lines:
        print(line)
    if not rows:
        print("No chat has history or the dossier off; every chat follows Settings"
              + (" and the old history scope." if scope_lines else "."))
    for owner_key, chat, entry in rows:
        print(f"owner={owner_key} chat={chat} {_entry_text(entry)} via={entry.updated_via} "
              f"updated_at={entry.updated_at}")
    return 0


def _scope_cli(store: ChatSettingsStore) -> int:
    snapshot = store.read_strict()
    lines = _scope_lines(snapshot)
    if not lines:
        print("No old history scope rule; every chat follows its own setting and Settings.")
        return 0
    turned_on = sum(1 for chats in snapshot.owners.values() for entry in chats.values()
                    if "history" in entry.explicit_on)
    for line in lines:
        print(line)
    print(f"Its dossiers pause too. Turn a chat on: {TURN_ON_HINT}. Chats turned on: {turned_on}.")
    if snapshot.scope is not None:
        print(f"Drop the stored rule: '{SCOPE_CLEAR_COMMAND}'.")
    if _scope_env_names():
        print(f"note: {DOWNGRADE_NOTE}")
    return 0


def _scope_clear_cli(store: ChatSettingsStore, confirmed: bool) -> int:
    snapshot = store.read_strict()
    names = _scope_env_names()
    if not active_scopes(snapshot):
        print("No old history scope rule to drop.")
        return 0
    if snapshot.scope is None:
        done = "No stored old history scope rule; a later start won't store one from the variables."
        ask = "No old history scope rule is stored yet. This makes sure a later start never stores one."
    else:
        done = ("Dropped the stored old history scope rule. Chats it kept out start saving history with their next "
                "message, except chats set to Don't save; their dossiers resume too.")
        ask = (f"This drops the stored old history scope rule ({snapshot.scope.summary()}). Chats it keeps out "
               f"start saving history with their next message, except chats set to Don't save.")
    if not confirmed:
        print(f"{ask} Run '{SCOPE_CLEAR_COMMAND}' to do it.")
    else:
        clear_scope(store)
        print(done)
    if names:
        print(f"note: while {', '.join(names)} stay set, they keep chats out too. {DOWNGRADE_NOTE} Then restart "
              f"the gateway.")
    return 0 if confirmed else 1


_SCOPE_REPAIR_NOTES = {
    "kept": "The old history scope rule was kept from the damaged file.",
    "variables": "The old history scope rule was stored again from the TELESEC_HISTORY_* variables.",
    "lost": ("warning: the old history scope rule was lost with the backup, so the chats it kept out now save "
             "history. Set them to Don't save, or set the TELESEC_HISTORY_* variables again and restart the "
             "gateway to store it again."),
}


def handle_chat_cli(args: argparse.Namespace, *, store: ChatSettingsStore | None = None) -> int:
    action = getattr(args, "_chat_action", None)
    if action is None:
        print(CHAT_USAGE)
        return 1
    store = store or default_store()
    try:
        if action == "list":
            return _list(args, store)
        if action == "scope":
            return _scope_cli(store)
        if action == "scope_clear":
            return _scope_clear_cli(store, bool(getattr(args, "yes", False)))
        owner_arg = getattr(args, "owner", None)
        chat, label, connection = _modes._resolve_chat_selector(args.chat, getattr(args, "connection", None))
        owner = _resolve_owner(store, owner_arg, chat, connection, action)
        if action == "get":
            snapshot = store.read_strict()
            entry = snapshot.owners.get(owner, {}).get(chat) if owner is not None else None
            scope = cli_scope()
            if getattr(args, "format", None) == "json":
                _print_json({**_entry_json(owner, chat, entry, scope=scope, connection=connection, label=label,
                                           snapshot=snapshot), "old_scope": _scope_json(snapshot)})
                return 0
            history, dossier = scope.state(owner, chat, connection=connection, snapshot=snapshot)
            stored = "" if entry is None else f" (via {entry.updated_via} at {entry.updated_at})"
            print(f"chat={chat} {_entry_text(entry)}{stored}")
            print(f"in effect: history={history} dossier={dossier}")
            if history == "scope":
                print(f"The old history scope rule keeps this chat out. Turn it on: 'hermes telesec chat set {chat} "
                      f"--history on'. Drop the rule: '{SCOPE_CLEAR_COMMAND}'.")
            return 0
        if action == "set":
            changes = [(feature, getattr(args, feature)) for feature in FEATURES
                       if getattr(args, feature, None) is not None]
            if not changes:
                raise ValueError("pass --history on|off, --dossier on|off, or both")
            connections = (connection,) if connection else cli_scope().connections(owner, chat)
            entry = None
            for feature, value in changes:
                result = store.set_flag(owner, chat, feature, value == "on", via="cli",
                                        repair_corrupt=owner_arg is not None, connections=connections)
                if result.backup_path is not None:
                    print(f"warning: corrupt {SETTINGS_FILENAME} backed up to {result.backup_path}")
                    if result.scope_repair:
                        print(_SCOPE_REPAIR_NOTES[result.scope_repair])
                entry = result.entry
            print(f"chat={chat} {_entry_text(entry)}")
            if entry is not None and "history" in entry.explicit_on:
                print("History is on for this chat although the old history scope rule keeps it out.")
            print("The gateway applies it to the next update; no restart is needed.")
            return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(CHAT_USAGE)
    return 1
