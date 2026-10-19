"""Global Telesec switches: transcription, history and dossiers, for all chats.

Transcription and history live in ``$HERMES_HOME/data/telesec/settings.json``. The dossier switch
stays in ``dossiers/settings.json`` (0.11.0), so a rollback keeps it. Readers stat the file on every
call and re-read it only when it changed, so a change from Telegram or the CLI applies to the next
update without a restart.

Up to 0.11.0, ``TELESEC_DISABLE``, ``TELESEC_HISTORY_ENABLE`` and the Desktop ``history_enable``
setting were the switches. ``migrate_legacy()`` stores their value once, only when it differs from the
default, and they are never read again. Stdlib-only; the file never holds message text.
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
LEGACY_ENV = {LEGACY_TRANSCRIPTION_ENV: "transcription", LEGACY_HISTORY_ENV: "history"}
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
    return raw


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
    if any(feature in payload for feature in STORED_FEATURES):
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


def _source(via: Any) -> str:
    if via is None:
        return "default"
    return "migration" if via == MIGRATION_VIA else "stored"


def status() -> dict[str, dict[str, Any]]:
    """Each switch: enabled, source (stored, migration, default or unreadable), updated_at, updated_via."""
    payload = _STORE.payload()
    out: dict[str, dict[str, Any]] = {}
    for feature in STORED_FEATURES:
        item = payload.get(feature)
        if isinstance(item, dict):
            via = item.get("updated_via") if item.get("updated_via") in (*UPDATED_VIA, MIGRATION_VIA) else None
            at = item.get("updated_at") if isinstance(item.get("updated_at"), str) else None
            out[feature] = {"enabled": item["enabled"], "source": "migration" if via == MIGRATION_VIA else "stored",
                            "updated_at": at, "updated_via": via}
        else:
            out[feature] = {"enabled": DEFAULTS[feature], "source": "default", "updated_at": None,
                            "updated_via": None}
    dossier = _ds.load_settings()
    out["dossiers"] = {"enabled": dossier["enabled"],
                       "source": "unreadable" if dossier.get("error") else _source(dossier["updated_via"]),
                       "updated_at": dossier["updated_at"], "updated_via": dossier["updated_via"]}
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


def _setting_on(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"})


def _legacy_values(get_config: Callable[..., Any]) -> dict[str, tuple[bool, str]]:
    """feature -> (value, source name) for each old switch that differs from the default.

    The variable wins over the Desktop setting, as it did up to 0.11.0.
    """
    found: dict[str, tuple[bool, str]] = {}
    if _history._truthy_env(LEGACY_TRANSCRIPTION_ENV):
        found["transcription"] = (False, LEGACY_TRANSCRIPTION_ENV)
    if LEGACY_HISTORY_ENV in os.environ:
        if _history._truthy_env(LEGACY_HISTORY_ENV):
            found["history"] = (True, LEGACY_HISTORY_ENV)
    elif _setting_on(get_config(LEGACY_HISTORY_SETTING, None)):
        found["history"] = (True, f"the {LEGACY_HISTORY_SETTING} setting")
    return found


def _state(on: bool) -> str:
    return "on" if on else "off"


def _ignored_line(name: str, feature: str, on: bool, source: str) -> str:
    return (f"{name} is no longer read; {feature} is {_state(on)} ({source}). "
            f"Use /telesec or '{SETTINGS_COMMAND} set {feature} on|off'.")


def migrate_legacy(*, get_config: Callable[..., Any] | None = None, now: datetime | None = None) -> list[str]:
    """Store each old switch once, only when it differs from the default and nothing is stored yet.

    ``register()`` runs in CLI processes too, where a service's environment may be missing, so a
    default value is never stored: it would later mask the service's setting. A corrupt file is
    left alone. Returns the migrated features.
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
    moved: list[tuple[str, bool, str]] = []
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
                payload[feature] = {"enabled": value, "updated_at": stamp, "updated_via": MIGRATION_VIA}
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
        logger.warning("%s: %s", _history.PLUGIN_NAME, _ignored_line(name, feature, item["enabled"], source))
        warned.append(name)
    return warned


def ignored_env_names() -> list[str]:
    """Old switch variables still set in this process (they are not read)."""
    return [name for name in LEGACY_ENV if name in os.environ]


# ---------------------------------------------------------------------------
# CLI: hermes telesec settings [--format json] | settings set <feature> on|off
# ---------------------------------------------------------------------------


def add_settings_parser(subs: Any) -> None:
    parser = subs.add_parser("settings", help="Show or change the switches for all chats")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.set_defaults(_settings_action="show")
    actions = parser.add_subparsers(dest="telesec_settings_command")
    set_parser = actions.add_parser("set", help="Turn transcription, history or dossiers on or off for all chats")
    set_parser.add_argument("feature", choices=list(FEATURES))
    set_parser.add_argument("value", choices=["on", "off"])
    set_parser.set_defaults(_settings_action="set")


def _mode_count() -> int | None:
    try:
        return sum(len(chats) for chats in _modes.default_store().read_strict().values())
    except (OSError, ValueError):
        return None


def _status_line(feature: str, item: dict[str, Any]) -> str:
    if item["source"] in ("default", "unreadable"):
        return f"{feature}: {_state(item['enabled'])} ({item['source']})"
    return f"{feature}: {_state(item['enabled'])} (stored via {item['updated_via']} at {item['updated_at']})"


def _show(as_json: bool) -> int:
    current = status()
    paused = current["dossiers"]["enabled"] and not current["history"]["enabled"]
    degraded = file_degraded()
    modes = _mode_count()
    ignored = ignored_env_names()
    if as_json:
        document = {**current, "chat_overrides": {"transcription": modes}, "ignored_env": ignored,
                    "settings_file_unreadable": degraded}
        document["dossiers"] = {**current["dossiers"], "paused": paused}
        print(json.dumps(document, ensure_ascii=True, indent=2))
        return 0
    lines = ["Telesec settings (all chats):"]
    lines += [_status_line(feature, current[feature]) for feature in FEATURES]
    if paused:
        lines.append("note: dossiers are paused while history is off.")
    if degraded:
        lines.append(f"warning: {settings_path()} is unreadable; {SAVE_HINT}.")
    if modes is not None:
        lines.append(f"Per-chat transcription modes: {modes}")
    for name in ignored:
        lines.append(f"note: {name} is set in this process but no longer read.")
    lines.append(f"Change: '{SETTINGS_COMMAND} set <transcription|history|dossiers> on|off', "
                 "or /telesec in your private chat with the bot.")
    print("\n".join(lines))
    return 0


def _set(feature: str, on: bool) -> int:
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
            return _set(args.feature, args.value == "on")
        return _show(getattr(args, "format", None) == "json")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
