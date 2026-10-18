"""Shared Business owner lookup and direction; never derive owners from peers."""
from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)
OWNER_CACHE: dict[str, str] = {}


def get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        return (obj.get("api_kwargs") or {}).get(name, default)
    value = getattr(obj, name, default)
    return (getattr(obj, "api_kwargs", None) or {}).get(name, default) if value is default else value


def connection_id(message: Any) -> Any:
    return get(message, "business_connection_id") or get(message, "_hermes_business_connection_id")


def sender_id(message: Any) -> Any:
    return get(get(message, "from_user") or get(message, "from"), "id")


def cached_owner(message: Any) -> str | None:
    connection = connection_id(message)
    if not connection:
        return None
    cached = OWNER_CACHE.get(str(connection))
    if cached is not None:
        return cached
    # Only reuse a mapping verified for this exact connection, never a peer ID.
    from . import transcription_modes
    owners = [owner for owner, connections in transcription_modes.snapshot()["owners"].items()
              if str(connection) in connections]
    if len(owners) == 1:
        OWNER_CACHE[str(connection)] = owners[0]
        return owners[0]
    return None


async def resolve_owner(message: Any, *, bot: Any = None) -> str | None:
    connection = connection_id(message)
    if not connection:
        return None
    owner = cached_owner(message)
    if owner is not None:
        return owner
    lookup = getattr(bot, "get_business_connection", None)
    if not callable(lookup):
        return None
    try:
        result = lookup(connection)
        if inspect.isawaitable(result):
            result = await result
        owner = get(get(result, "user"), "id")
        if owner is None:
            return None
        owner = str(owner)
        OWNER_CACHE[str(connection)] = owner
        # Persist discovery for the secondary CLI even with history disabled.
        from . import transcription_modes
        try:
            await transcription_modes.remember_owner_async(owner, str(connection))
        except (OSError, ValueError):
            logger.warning("Business owner discovery persistence failed; existing mode file preserved")
        return owner
    except Exception:
        logger.warning("Business owner lookup failed; owner unknown")
        return None


def direction(message: Any, owner: str | None) -> str:
    if get(message, "sender_business_bot") is not None:
        return "outbound"
    sender = sender_id(message)
    if owner is None or sender is None:
        return "unknown"
    return "outbound" if str(sender) == owner else "inbound"


async def resolve_direction(message: Any, *, bot: Any = None) -> str:
    if get(message, "sender_business_bot") is not None:
        return "outbound"
    return direction(message, await resolve_owner(message, bot=bot))


def command_text(message: Any) -> bool:
    text = get(message, "text")
    return isinstance(text, str) and bool(text.split()) and text.split()[0] == "/transcribe"


def owner_command(message: Any, owner: str | None) -> bool:
    # sender_business_bot establishes direction, NEVER command authorization.
    return owner is not None and str(sender_id(message)) == owner and command_text(message)


def confirmation(message: Any) -> bool:
    text = get(message, "text")
    return (get(message, "sender_business_bot") is not None and command_text(message)
            and isinstance(text, str) and " → " in text
            and any(marker in text for marker in (" → Transcription:", " → Usage:", " → Unable to")))


async def exclude_from_history(message: Any, *, bot: Any = None) -> bool:
    if not command_text(message):
        return False
    return confirmation(message) or owner_command(message, await resolve_owner(message, bot=bot))
