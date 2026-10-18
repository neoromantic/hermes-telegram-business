"""Manual transcription policy, private atomic JSON storage and CLI.

POSIX advisory locking serializes gateway/CLI read-modify-write transactions.
Corrupt files are never overwritten. Runtime retains its last valid snapshot.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any

try:
    import fcntl
except ModuleNotFoundError:  # Keep imports usable; never write without a process lock.
    fcntl = None

from . import business_identity as identity

logger = logging.getLogger(__name__)
MODES = ("both", "own", "peer", "off")
_LOCK = threading.RLock()
_CACHE: dict[Path, tuple[tuple[int, int, int] | None, dict[str, Any]]] = {}


def path() -> Path:
    # Import lazily to use the same active Hermes profile as history.
    from .history_support import get_hermes_home
    return Path(get_hermes_home()) / "data" / "telegram-business" / "transcription-modes.json"


def _empty() -> dict[str, Any]:
    return {"version": 1, "owners": {}, "chats": {}}


def _validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("invalid transcription mode schema")
    if not isinstance(data.get("owners"), dict) or not isinstance(data.get("chats"), dict):
        raise ValueError("invalid transcription mode schema")
    for owner, connections in data["owners"].items():
        if not owner.isdecimal() or not isinstance(connections, list) or not all(isinstance(c, str) for c in connections):
            raise ValueError("invalid transcription owner mapping")
    for owner, chats in data["chats"].items():
        if not owner.isdecimal() or not isinstance(chats, dict):
            raise ValueError("invalid transcription chat mapping")
        for chat, entry in chats.items():
            if not re.fullmatch(r"-?\d+", chat) or not isinstance(entry, dict):
                raise ValueError("invalid transcription mode entry")
            if (entry.get("mode") not in MODES or entry.get("updated_via") not in {"chat", "cli"}
                    or not isinstance(entry.get("updated_at"), str) or not isinstance(entry.get("label"), str)):
                raise ValueError("invalid transcription mode metadata")
    return data


def _read(target: Path) -> dict[str, Any]:
    try:
        with target.open(encoding="utf-8") as stream:
            return _validate(json.load(stream))
    except FileNotFoundError:
        return _empty()
    except (ValueError, UnicodeError, TypeError):
        raise ValueError("invalid transcription mode file; repair or restore it before writing") from None


def _signature(target: Path) -> tuple[int, int, int] | None:
    try:
        stat = target.stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


def snapshot(*, strict: bool = False) -> dict[str, Any]:
    target = path()
    with _LOCK:
        if strict:
            return deepcopy(_read(target))
        previous = _CACHE.get(target)
        try:
            signature = _signature(target)
            if previous is not None and previous[0] == signature:
                return deepcopy(previous[1])
            data = _read(target)
            # Cache the signature observed BEFORE reading: a racing replacement
            # will trigger another read on the next decision.
            _CACHE[target] = signature, data
            return deepcopy(data)
        except (OSError, ValueError):
            logger.warning("Transcription mode file unreadable; using last valid policy or default both")
            return deepcopy(previous[1]) if previous else _empty()


@contextmanager
def _writer_lock(target: Path):
    if fcntl is None:
        raise OSError("transcription mode writes require POSIX file locking")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _LOCK:
        fd = os.open(target.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _mutate(fn) -> None:
    target = path()
    with _writer_lock(target):
        data = _read(target)  # Always reread under cross-process lock.
        if fn(data) is False:
            return
        _validate(data)
        fd, name = tempfile.mkstemp(prefix=".transcription-modes-", suffix=".tmp", dir=target.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            written_signature = _signature(Path(name))
            os.replace(name, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            _CACHE[target] = written_signature, deepcopy(data)
        finally:
            Path(name).unlink(missing_ok=True)


def remember_owner(owner: str, connection: str) -> None:
    def update(data):
        connections = data["owners"].setdefault(str(owner), [])
        if connection in connections:
            return False
        connections.append(connection)
    _mutate(update)


async def remember_owner_async(owner: str, connection: str) -> None:
    await asyncio.to_thread(remember_owner, owner, connection)


def set_mode(owner: str, chat: str, mode: str | None, *, via: str, label: str = "") -> None:
    if mode is not None and mode not in MODES:
        raise ValueError("mode must be both, own, peer or off")
    def update(data):
        chats = data["chats"].setdefault(str(owner), {})
        if mode is None:
            chats.pop(str(chat), None)
        else:
            chats[str(chat)] = {"mode": mode, "updated_at": datetime.now(timezone.utc).isoformat(),
                                 "updated_via": via, "label": label}
    _mutate(update)


def mode_for(owner: str, chat: Any) -> str:
    return snapshot()["chats"].get(str(owner), {}).get(str(chat), {}).get("mode", "both")


def decision(mode: str, direction: str) -> tuple[bool, str]:
    if mode == "off":
        return False, "mode_off"
    if mode == "both":
        return True, "mode_both"
    if direction == "unknown":
        return True, "unknown_direction_fail_open"
    allow = (mode == "own" and direction == "outbound") or (mode == "peer" and direction == "inbound")
    return allow, "direction_allowed" if allow else "direction_excluded"


async def should_transcribe(message: Any, *, bot: Any) -> bool:
    owner = await identity.resolve_owner(message, bot=bot)
    if owner is None:
        allowed, reason, mode = True, "unknown_owner_fail_open", "unknown"
    else:
        mode = await asyncio.to_thread(mode_for, owner, identity.get(identity.get(message, "chat"), "id"))
        direction = identity.direction(message, owner) if mode in {"own", "peer"} else "unknown"
        allowed, reason = decision(mode, direction)
    logger.info("Transcription policy mode=%s decision=%s reason=%s", mode, "transcribe" if allowed else "skip", reason)
    return allowed


def display_label(message: Any) -> str:
    chat = identity.get(message, "chat")
    name = identity.get(chat, "title") or " ".join(str(identity.get(chat, key) or "") for key in ("first_name", "last_name")).strip()
    username = identity.get(chat, "username")
    return " ".join(part for part in (name, f"@{username}" if username else "") if part)


def command_status_sync(message: Any, *, owner: str) -> str:
    text = identity.get(message, "text", "")
    args = text.split()[1:]
    chat = identity.get(identity.get(message, "chat"), "id")
    prefix = "/transcribe" + (f" {args[0]}" if len(args) == 1 and args[0] in (*MODES, "reset") else "")
    if args and (len(args) != 1 or args[0] not in (*MODES, "reset")):
        return "/transcribe → Usage: /transcribe [both|own|peer|off|reset]"
    try:
        if args:
            set_mode(owner, str(chat), None if args[0] == "reset" else args[0], via="chat", label=display_label(message))
        data = snapshot(strict=True)
        mode = data["chats"].get(owner, {}).get(str(chat), {}).get("mode", "both")
        return f"{prefix} → Transcription: {mode}"
    except (OSError, ValueError):
        logger.warning("Transcription command storage failed; existing policy preserved")
        return "/transcribe → Unable to read or save transcription mode. Check the mode file."


async def command_status(message: Any, *, owner: str) -> str:
    return await asyncio.to_thread(command_status_sync, message, owner=owner)


def setup_cli(subs) -> None:
    mode = subs.add_parser("mode", help="Read or change per-chat transcription modes")
    actions = mode.add_subparsers(dest="_mode_action")
    for action in ("list", "get", "set", "reset"):
        parser = actions.add_parser(action)
        parser.add_argument("--owner", help="Connected account owner user ID")
        parser.add_argument("--connection", help="Scope history contact resolution to a connection")
        if action != "list":
            parser.add_argument("chat", help="Numeric chat ID, @username or exact contact name")
        if action == "set":
            parser.add_argument("mode", choices=MODES)


def handle_cli(args: argparse.Namespace) -> int:
    from . import history_support as history
    try:
        action = getattr(args, "_mode_action", None)
        if action is None:
            raise ValueError("Usage: hermes telegram-business mode <list|get|set|reset>")
        data = snapshot(strict=True)
        known_connections = {owner: list(connections) for owner, connections in data["owners"].items()}
        from . import business_identity as identity
        for connection, cached_owner in identity.OWNER_CACHE.items():
            known_connections.setdefault(cached_owner, []).append(connection)
        owners = set(known_connections) | set(data["chats"])
        requested = getattr(args, "owner", None)
        if requested is None:
            if len(owners) != 1:
                raise ValueError("pass --owner explicitly" if owners else "no known owner; pass --owner explicitly")
            owner = next(iter(owners))
        else:
            if not str(requested).isdecimal():
                raise ValueError("--owner must be a numeric owner user ID")
            owner = str(int(requested))
        if action == "list":
            for chat, entry in sorted(data["chats"].get(owner, {}).items()):
                print(f"owner={owner} chat={chat} mode={entry['mode']} label={entry['label']} updated_at={entry['updated_at']} updated_via={entry['updated_via']}")
            return 0
        selector = args.chat
        label = ""
        if re.fullmatch(r"-?\d+", selector):
            chat = str(int(selector))
        else:
            if not history.history_config_from_env().active:
                raise ValueError("name lookup requires history enabled; use a numeric chat ID")
            connection = getattr(args, "connection", None)
            connections = [connection for connection in known_connections.get(owner, [])
                           if sum(connection in values for values in known_connections.values()) == 1]
            if connection is not None:
                mapped = {key for key, values in known_connections.items() if connection in values}
                if mapped and mapped != {owner}:
                    raise ValueError("connection conflicts with verified ownership for the selected owner")
                if connection not in connections:
                    if requested is None:
                        raise ValueError("historical selection requires both --owner and --connection")
                    catalog = history._catalog_entries_for_cli()
                    if not any(str(entry.get("business_connection_id")) == connection for entry in catalog):
                        raise ValueError("connection is not present in the existing history catalog")
                    # Explicit local USER selection, never verified ownership.
                    connections = [connection]
            if not connections:
                raise ValueError("no verified owner-to-connection mapping; ask the owner to send /transcribe once "
                                 "or pass explicit --owner and --connection")
            # Use the existing exact contact resolver, including its ambiguity
            # diagnostics; scope its catalog input by the selected owner first.
            entry = history._resolve_contact_entry(business_connection_id=connection, contact=selector,
                                                   owner_connections=connections)
            chat = str(entry["chat_id"])
            label = history._catalog_entry_display_name(entry)
        if action in {"set", "reset"}:
            previous = data["chats"].get(owner, {}).get(chat, {})
            set_mode(owner, chat, args.mode if action == "set" else None, via="cli", label=label or previous.get("label", ""))
        print(f"owner={owner} chat={chat} mode={mode_for(owner, chat)}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Mode error: {exc}")
        return 1
