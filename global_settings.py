"""Global Telesec switches: transcription, history and dossiers, for all chats, and history retention.

Transcription, history and retention live in ``$HERMES_HOME/data/telesec/settings.json``. The dossier
switch stays in ``dossiers/settings.json`` (0.11.0), so a rollback keeps it. Readers stat the file on
every call and re-read it only when it changed, so a change from Telegram or the CLI applies to the
next update without a restart.

Up to 0.11.0, ``TELESEC_DISABLE``, ``TELESEC_HISTORY_ENABLE`` and the Desktop ``history_enable``
setting were the switches, and up to 0.12 ``TELESEC_HISTORY_RETENTION_DAYS`` set retention.
``migrate_legacy()`` stores their value once, only when it differs from the default, and they are
never read again. Stdlib-only; the file never holds message text.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import dossier_support as _ds
from . import history_support as _history
from . import transcription_modes as _modes

logger = logging.getLogger(__name__)

FEATURES = ("transcription", "history", "dossiers")
# Dossiers keep their own 0.11.0 file; these two live in settings.json.
STORED_FEATURES = ("transcription", "history")
DEFAULTS = {"transcription": True, "history": False, "dossiers": False}
UPDATED_VIA = ("telegram", "cli")
MIGRATION_VIA = "migration"
SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"
SETTINGS_COMMAND = "hermes telesec settings"
LEGACY_TRANSCRIPTION_ENV = "TELESEC_DISABLE"
LEGACY_HISTORY_ENV = "TELESEC_HISTORY_ENABLE"
LEGACY_HISTORY_SETTING = "history_enable"
LEGACY_RETENTION_ENV = "TELESEC_HISTORY_RETENTION_DAYS"
LEGACY_ENV = {LEGACY_TRANSCRIPTION_ENV: "transcription", LEGACY_HISTORY_ENV: "history",
              LEGACY_RETENTION_ENV: "retention"}
# How long saved history is kept, in days; 0 keeps it forever. Its own top-level key, not part of the
# history item: storing it must not count as a stored history switch (see migrate_legacy).
RETENTION = "retention"
RETENTION_CHOICES = (0, 365, 90, 30)
DEFAULT_RETENTION_DAYS = _history.DEFAULT_RETENTION_DAYS
MAX_RETENTION_DAYS = 36500
RETENTION_VALUES = "forever|365|90|30|N"
SAVE_HINT = f"'{SETTINGS_COMMAND} set' rewrites it after a backup"


class SettingsFileCorrupt(ValueError):
    """settings.json exists but cannot be parsed; only ``settings set`` replaces it, after a backup."""


def settings_path() -> Path:
    return _history.get_hermes_home() / "data" / "telesec" / SETTINGS_FILENAME


def _parse(data: bytes) -> dict[str, Any]:
    """The stored payload; an empty file holds nothing, like a missing one."""
    if not data.strip():
        return {}
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SettingsFileCorrupt(type(exc).__name__) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise SettingsFileCorrupt("unsupported schema")
    for feature in STORED_FEATURES:
        item = raw.get(feature)
        if item is not None and not (isinstance(item, dict) and isinstance(item.get("enabled"), bool)):
            raise SettingsFileCorrupt(f"invalid {feature} entry")
    item = raw.get(RETENTION)
    if item is not None and not (isinstance(item, dict) and valid_retention(item.get("days"))):
        raise SettingsFileCorrupt(f"invalid {RETENTION} entry")
    return raw


def valid_retention(days: Any) -> bool:
    return type(days) is int and 0 <= days <= MAX_RETENTION_DAYS


def parse_retention(raw: str) -> int:
    """``forever`` or a number of days (0 is forever); raises ValueError otherwise."""
    text = raw.strip().lower()
    if text == "forever":
        return 0
    if text.isascii() and text.isdigit() and valid_retention(int(text)):
        return int(text)
    raise ValueError(f"retention must be forever or 0..{MAX_RETENTION_DAYS} days, got {raw!r}")


def keeps_less(days: int, than: int) -> bool:
    """Is ``days`` a shorter retention than ``than``? 0 is forever, the longest."""
    return days != 0 and (than == 0 or days < than)


class _Store:
    """settings.json reader with an inode/mtime/size-keyed cache, like ModeStore."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._corrupt: set[str] = set()
        self._unreadable: set[str] = set()

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._corrupt.clear()
            self._unreadable.clear()

    def _last_valid(self, key: str) -> dict[str, Any]:
        with self._lock:
            cached = self._cache.get(key)
        return cached[1] if cached is not None else {}

    def _mark_unreadable(self, path: Path, exc: OSError) -> dict[str, Any]:
        key = str(path)
        with self._lock:
            first = key not in self._unreadable
            self._unreadable.add(key)
        if first:
            logger.warning("Telesec settings file %s unreadable (%s); %s", path, type(exc).__name__,
                           _fallback_note(self._last_valid(key)))
        return self._last_valid(key)

    def payload(self) -> dict[str, Any]:
        """The stored switches; a damaged file keeps the last valid ones (none on a cold start)."""
        path = settings_path()
        key = str(path)
        try:
            signature = _modes._signature(os.stat(path))
        except FileNotFoundError:
            signature = None
        except OSError as exc:
            return self._mark_unreadable(path, exc)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] == signature:
                self._unreadable.discard(key)
                return cached[1]
        try:
            payload = _parse(path.read_bytes()) if signature is not None else {}
        except FileNotFoundError:
            payload = {}
        except SettingsFileCorrupt as exc:
            # Cached under the damaged signature, so the warning repeats only when the file changes.
            payload = self._last_valid(key)
            logger.warning("Telesec settings file %s is corrupt (%s); %s; %s", path, exc, _fallback_note(payload),
                           SAVE_HINT)
            with self._lock:
                self._corrupt.add(key)
                self._unreadable.discard(key)
                self._cache[key] = (signature, payload)
            return payload
        except OSError as exc:
            return self._mark_unreadable(path, exc)
        with self._lock:
            self._corrupt.discard(key)
            self._unreadable.discard(key)
            self._cache[key] = (signature, payload)
        return payload

    def degraded(self) -> bool:
        self.payload()
        key = str(settings_path())
        with self._lock:
            return key in self._corrupt or key in self._unreadable


def _fallback_note(payload: dict[str, Any]) -> str:
    if any(feature in payload for feature in (*STORED_FEATURES, RETENTION)):
        return "keeping the last valid settings"
    return "transcription stays on and history off"


_STORE = _Store()


def reset_in_memory_caches() -> None:
    _STORE.clear()


def file_degraded() -> bool:
    """True while settings.json is corrupt or unreadable and the last valid settings stand in for it."""
    return _STORE.degraded()


def _check_feature(feature: str) -> None:
    if feature not in FEATURES:
        raise ValueError(f"feature must be one of {', '.join(FEATURES)}, got {feature!r}")


def enabled(feature: str) -> bool:
    """Is ``feature`` on for all chats? Dossiers also need history; see ``dossiers_active``."""
    _check_feature(feature)
    if feature == "dossiers":
        return bool(_ds.load_settings()["enabled"])
    item = _STORE.payload().get(feature)
    return item["enabled"] if isinstance(item, dict) else DEFAULTS[feature]


def dossiers_active() -> bool:
    """Dossiers are on and history is on: dossiers are built from history."""
    return enabled("dossiers") and enabled("history")


def retention_days() -> int:
    """Days saved history is kept (0: forever). Maintenance prunes whole closed months older than that."""
    item = _STORE.payload().get(RETENTION)
    return item["days"] if isinstance(item, dict) else DEFAULT_RETENTION_DAYS


def _source(via: Any) -> str:
    if via is None:
        return "default"
    return "migration" if via == MIGRATION_VIA else "stored"


def _stored_status(item: Any, key: str, default: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {key: default, "source": "default", "updated_at": None, "updated_via": None}
    via = item.get("updated_via") if item.get("updated_via") in (*UPDATED_VIA, MIGRATION_VIA) else None
    at = item.get("updated_at") if isinstance(item.get("updated_at"), str) else None
    return {key: item[key], "source": "migration" if via == MIGRATION_VIA else "stored", "updated_at": at,
            "updated_via": via}


def status() -> dict[str, dict[str, Any]]:
    """Each switch: enabled (retention: days), source (stored, migration, default or unreadable), updated_at,
    updated_via."""
    payload = _STORE.payload()
    out = {feature: _stored_status(payload.get(feature), "enabled", DEFAULTS[feature]) for feature in STORED_FEATURES}
    dossier = _ds.load_settings()
    out["dossiers"] = {"enabled": dossier["enabled"],
                       "source": "unreadable" if dossier.get("error") else _source(dossier["updated_via"]),
                       "updated_at": dossier["updated_at"], "updated_via": dossier["updated_via"]}
    out[RETENTION] = _stored_status(payload.get(RETENTION), "days", DEFAULT_RETENTION_DAYS)
    return out


def _read_for_write(path: Path, *, now: datetime | None, repair_corrupt: bool) -> tuple[dict[str, Any], Path | None]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    try:
        return _parse(data), None
    except SettingsFileCorrupt as exc:
        if not repair_corrupt:
            # Telegram never overwrites a damaged file; only an explicit CLI write does.
            raise SettingsFileCorrupt(f"{path} is corrupt ({exc}); {SAVE_HINT}") from exc
        backup = _modes._backup_corrupt_file(path, now)
        logger.warning("Telesec settings file %s was corrupt (%s); backed up to %s and started empty",
                       path, exc, backup)
        return {}, backup


def set_enabled(feature: str, on: bool, *, via: str, now: datetime | None = None,
                repair_corrupt: bool = False) -> dict[str, Any]:
    """Turn ``feature`` on or off for all chats (atomic, private, locked).

    Idempotent like the dossier switch: a stored value that already matches keeps its time and
    source. Returns the stored item plus "changed" and "backup_path" (a repaired corrupt file).
    Gateway callers wake the dossier worker after a dossier or history change.
    """
    _check_feature(feature)
    if via not in UPDATED_VIA:
        raise ValueError(f"unknown switch source {via!r}; use one of: {', '.join(UPDATED_VIA)}")
    if feature == "dossiers":
        return {**_ds.set_enabled(on, via=via, now=now), "backup_path": None}
    path = settings_path()
    with _modes._writer_lock(path):
        payload, backup = _read_for_write(path, now=now, repair_corrupt=repair_corrupt)
        current = payload.get(feature)
        if isinstance(current, dict) and current["enabled"] is bool(on):
            return {**current, "changed": False, "backup_path": None}
        item = {"enabled": bool(on), "updated_at": _modes._format_timestamp(now), "updated_via": via}
        _modes._write_private_json_atomic(path, {**payload, "schema_version": SCHEMA_VERSION, feature: item})
    logger.info("%s: %s switched enabled=%s via=%s", _history.PLUGIN_NAME, feature, bool(on), via)
    return {**item, "changed": True, "backup_path": backup}


def set_retention(days: int, *, via: str, now: datetime | None = None, repair_corrupt: bool = False) -> dict[str, Any]:
    """Keep saved history ``days`` days (0: forever); same write rules and result as ``set_enabled``."""
    if not valid_retention(days):
        raise ValueError(f"retention must be 0..{MAX_RETENTION_DAYS} days, got {days!r}")
    if via not in UPDATED_VIA:
        raise ValueError(f"unknown switch source {via!r}; use one of: {', '.join(UPDATED_VIA)}")
    path = settings_path()
    with _modes._writer_lock(path):
        payload, backup = _read_for_write(path, now=now, repair_corrupt=repair_corrupt)
        current = payload.get(RETENTION)
        if isinstance(current, dict) and current["days"] == days:
            return {**current, "changed": False, "backup_path": None}
        item = {"days": days, "updated_at": _modes._format_timestamp(now), "updated_via": via}
        _modes._write_private_json_atomic(path, {**payload, "schema_version": SCHEMA_VERSION, RETENTION: item})
    logger.info("%s: retention set days=%d via=%s", _history.PLUGIN_NAME, days, via)
    return {**item, "changed": True, "backup_path": backup}


def _setting_on(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"})


def _legacy_retention() -> int | None:
    """Days from ``TELESEC_HISTORY_RETENTION_DAYS`` when it kept less than forever, as 0.12 read it."""
    raw = os.environ.get(LEGACY_RETENTION_ENV, "").strip()
    try:
        days = int(raw)
    except ValueError:
        return None
    # 0.12 read a negative value as 0 (forever); over MAX_RETENTION_DAYS is forever in all but name.
    return days if 0 < days <= MAX_RETENTION_DAYS else None


def _legacy_values(get_config: Callable[..., Any]) -> dict[str, tuple[Any, str]]:
    """feature -> (value, source name) for each old switch to store.

    That is a value that differs from the default, plus history off when the variable turns off a
    Desktop "on": the variable wins over the Desktop setting, as it did up to 0.11.0, and without a
    stored "off" a later process without the variable would store the stale Desktop "on".
    """
    found: dict[str, tuple[Any, str]] = {}
    days = _legacy_retention()
    if days is not None:
        found[RETENTION] = (days, LEGACY_RETENTION_ENV)
    if _history._truthy_env(LEGACY_TRANSCRIPTION_ENV):
        found["transcription"] = (False, LEGACY_TRANSCRIPTION_ENV)
    desktop_history = _setting_on(get_config(LEGACY_HISTORY_SETTING, None))
    if LEGACY_HISTORY_ENV in os.environ:
        if _history._truthy_env(LEGACY_HISTORY_ENV):
            found["history"] = (True, LEGACY_HISTORY_ENV)
        elif desktop_history:
            found["history"] = (False, LEGACY_HISTORY_ENV)
    elif desktop_history:
        found["history"] = (True, f"the {LEGACY_HISTORY_SETTING} setting")
    return found


def _state(on: bool) -> str:
    return "on" if on else "off"


def retention_text(days: int) -> str:
    return "forever" if days == 0 else f"{days} day" + ("" if days == 1 else "s")


def _ignored_line(name: str, feature: str, value: Any, source: str) -> str:
    if feature == RETENTION:
        return (f"{name} is no longer read; history is kept {retention_text(value)} ({source}). "
                f"Use /telesec or '{SETTINGS_COMMAND} set retention {RETENTION_VALUES}'.")
    return (f"{name} is no longer read; {feature} is {_state(value)} ({source}). "
            f"Use /telesec or '{SETTINGS_COMMAND} set {feature} on|off'.")


def _stored_item(feature: str, value: Any, stamp: str) -> dict[str, Any]:
    key = "days" if feature == RETENTION else "enabled"
    return {key: value, "updated_at": stamp, "updated_via": MIGRATION_VIA}


def migrate_legacy(*, get_config: Callable[..., Any] | None = None, now: datetime | None = None) -> list[str]:
    """Store each old switch once (see ``_legacy_values``) when nothing is stored for it yet.

    ``register()`` runs in CLI processes too, where a service's environment may be missing, so a
    default value is stored only when the variable overrides a Desktop "on"; otherwise it would
    later mask the service's setting. A corrupt file is left alone. Returns the migrated features.
    """
    legacy = _legacy_values(get_config or _history.get_config)
    if not legacy:
        return []
    path = settings_path()
    try:
        current = _parse(path.read_bytes())
    except FileNotFoundError:
        current = {}
    except (OSError, SettingsFileCorrupt):
        return []
    if all(feature in current for feature in legacy):
        return []
    moved: list[tuple[str, Any, str]] = []
    with _modes._writer_lock(path):
        try:
            payload = _parse(path.read_bytes())
        except FileNotFoundError:
            payload = {}
        except SettingsFileCorrupt:
            return []
        stamp = _modes._format_timestamp(now)
        for feature, (value, source) in legacy.items():
            if feature not in payload:
                payload[feature] = _stored_item(feature, value, stamp)
                moved.append((feature, value, source))
        if moved:
            _modes._write_private_json_atomic(path, {**payload, "schema_version": SCHEMA_VERSION})
    for feature, value, source in moved:
        # WARNING, not INFO: register() runs before the gateway configures logging.
        logger.warning("%s: %s", _history.PLUGIN_NAME, _ignored_line(source, feature, value, "moved to Telesec settings"))
    return [feature for feature, _value, _source_name in moved]


_WARNED: set[str] = set()


def warn_ignored_env(skip: tuple[str, ...] | list[str] = ()) -> list[str]:
    """One WARNING per process for each old variable still set; ``skip`` names features the migration just
    logged, which counts as their warning."""
    current = status()
    warned = []
    for name, feature in LEGACY_ENV.items():
        if name not in os.environ or name in _WARNED:
            continue
        _WARNED.add(name)
        if feature in skip:
            continue
        item = current[feature]
        source = "moved to Telesec settings" if item["source"] == "migration" else item["source"]
        value = item["days"] if feature == RETENTION else item["enabled"]
        logger.warning("%s: %s", _history.PLUGIN_NAME, _ignored_line(name, feature, value, source))
        warned.append(name)
    return warned


def ignored_env_names() -> list[str]:
    """Old switch variables still set in this process (they are not read)."""
    return [name for name in LEGACY_ENV if name in os.environ]


# ---------------------------------------------------------------------------
# CLI: hermes telesec settings [--format json] | settings set <feature> on|off | settings set retention <days>
# ---------------------------------------------------------------------------


def add_settings_parser(subs: Any) -> None:
    parser = subs.add_parser("settings", help="Show or change the switches for all chats")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.set_defaults(_settings_action="show")
    actions = parser.add_subparsers(dest="telesec_settings_command")
    set_parser = actions.add_parser("set", help="Turn transcription, history or dossiers on or off for all chats, "
                                                "or set how long history is kept")
    set_parser.add_argument("feature", choices=[*FEATURES, RETENTION])
    set_parser.add_argument("value", help=f"on|off; for retention: {RETENTION_VALUES} (days)")
    set_parser.set_defaults(_settings_action="set")


def _mode_count() -> int | None:
    try:
        return sum(len(chats) for chats in _modes.default_store().read_strict().values())
    except (OSError, ValueError):
        return None


def _chat_counts() -> dict[str, int | None]:
    """Chats with history or the dossier off (None while chat-settings.json can't be read)."""
    from . import chat_settings  # lazy: chat_settings imports this module

    try:
        return dict(chat_settings.default_store().counts())
    except (OSError, ValueError):
        return {"history": None, "dossier": None}


def _status_line(feature: str, item: dict[str, Any]) -> str:
    value = retention_text(item["days"]) if feature == RETENTION else _state(item["enabled"])
    if item["source"] in ("default", "unreadable"):
        return f"{feature}: {value} ({item['source']})"
    return f"{feature}: {value} (stored via {item['updated_via']} at {item['updated_at']})"


def _show(as_json: bool) -> int:
    current = status()
    paused = current["dossiers"]["enabled"] and not current["history"]["enabled"]
    degraded = file_degraded()
    modes = _mode_count()
    chats = _chat_counts()
    ignored = ignored_env_names()
    if as_json:
        document = {**current, "chat_overrides": {"transcription": modes, **chats}, "ignored_env": ignored,
                    "settings_file_unreadable": degraded}
        document["dossiers"] = {**current["dossiers"], "paused": paused}
        print(json.dumps(document, ensure_ascii=True, indent=2))
        return 0
    lines = ["Telesec settings (all chats):"]
    lines += [_status_line(feature, current[feature]) for feature in ("transcription", "history", RETENTION, "dossiers")]
    if paused:
        lines.append("note: dossiers are paused while history is off.")
    if degraded:
        lines.append(f"warning: {settings_path()} is unreadable; {SAVE_HINT}.")
    if modes is not None:
        lines.append(f"Per-chat transcription modes: {modes}")
    if chats["history"] is None:
        lines.append("warning: chat-settings.json is unreadable; see 'hermes telesec chat list'.")
    elif chats["history"] or chats["dossier"]:
        lines.append(f"Chats with history off: {chats['history']}; with the dossier off: {chats['dossier']}")
    for name in ignored:
        lines.append(f"note: {name} is set in this process but no longer read.")
    lines.append(f"Change: '{SETTINGS_COMMAND} set <transcription|history|dossiers> on|off', "
                 f"'{SETTINGS_COMMAND} set retention {RETENTION_VALUES}', or /telesec in your private chat with the bot.")
    print("\n".join(lines))
    return 0


def _set_retention(raw: str) -> int:
    days = parse_retention(raw)
    result = set_retention(days, via="cli", repair_corrupt=True)
    if result.get("backup_path") is not None:
        print(f"warning: corrupt {SETTINGS_FILENAME} backed up to {result['backup_path']}")
    print(f"retention: {retention_text(days)}" + ("" if result["changed"] else " (unchanged)"))
    if days:
        print(f"Saved history is deleted a whole month at a time: a month goes at the next hourly cleanup once all "
              f"of it is older than {retention_text(days)}.")
    print("The gateway applies it to the next cleanup; no restart is needed.")
    return 0


def _set(feature: str, value: str) -> int:
    if feature == RETENTION:
        return _set_retention(value)
    if value not in ("on", "off"):
        raise ValueError(f"{feature} takes on or off, got {value!r}")
    on = value == "on"
    result = set_enabled(feature, on, via="cli", repair_corrupt=True)
    if result.get("backup_path") is not None:
        print(f"warning: corrupt {SETTINGS_FILENAME} backed up to {result['backup_path']}")
    print(f"{feature}: {_state(on)}" + ("" if result["changed"] else " (unchanged)"))
    if feature == "dossiers" and on:
        cfg = _ds.load_config()
        print(f"The text of all private Business chats is sent to {cfg.provider} / {cfg.model}.")
    if on and feature == "dossiers" and not enabled("history"):
        print(f"note: dossiers are paused while history is off; turn it on with '{SETTINGS_COMMAND} set history on'.")
    print("The gateway applies it to the next update; no restart is needed.")
    return 0


def handle_settings_cli(args: argparse.Namespace) -> int:
    try:
        if getattr(args, "_settings_action", None) == "set":
            return _set(args.feature, args.value)
        return _show(getattr(args, "format", None) == "json")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
