"""Native owner-DM stats control. No Business replies, model calls, or cached auth."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import re
import shlex
from typing import Any, Callable

from . import correspondence_stats as _stats

logger = logging.getLogger(__name__)
_COMMAND = re.compile(r"^/business_stats(?:@[A-Za-z0-9_]+)?(?:\s|$)", re.IGNORECASE)
MAX_COMMAND_CHARS = 2048
MAX_CHUNK_CHARS = 3500
MAX_CHUNKS = 8


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def command_message(update: Any) -> Any:
    """Recognize reserved commands even when their origin must not execute them."""
    for key in (
        "message", "edited_message", "business_message", "edited_business_message", "channel_post", "edited_channel_post",
    ):
        message = _get(update, key)
        text = _get(message, "text")
        if isinstance(text, str) and _COMMAND.match(text):
            return message
    return None


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise ValueError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise ValueError(message or "Use /business_stats --connection ID [--contact @name] [stats options]")

    def print_help(self, file: Any = None) -> None:
        raise ValueError("Use /business_stats --connection ID [--contact @name] [stats options]")


def parse_command(text: str) -> argparse.Namespace:
    if len(text) > MAX_COMMAND_CHARS:
        raise ValueError("stats command is too long")
    parser = _Parser(add_help=False, allow_abbrev=False)
    subs = parser.add_subparsers()
    _stats.add_cli_parser(subs)
    args = parser.parse_args(["stats", *shlex.split(text)[1:]])
    if not args.connection:
        raise ValueError("--connection is required in the owner's private bot DM")
    # Bound the owner-DM rendering, even if the CLI's table limit is larger.
    args.limit = min(50, max(1, args.limit))
    return args


def chunks(text: str) -> list[str]:
    # Telegram's limit counts UTF-16 code units. ASCII escaping also keeps JSON
    # and untrusted profile labels deterministic on the wire.
    text = text.encode("ascii", "backslashreplace").decode("ascii")
    pieces = [text[index:index + MAX_CHUNK_CHARS] for index in range(0, len(text), MAX_CHUNK_CHARS)] or [""]
    if len(pieces) > MAX_CHUNKS:
        return ["Stats report exceeds the private-DM limit. Narrow --since / --contact / --limit, or use the CLI."]
    return pieces


async def handle_update(update: Any, *, bot: Any, mark_seen: Callable[[tuple[str, ...]], bool]) -> bool:
    message = command_message(update)
    if message is None:
        return False
    # Every recognized command is reserved and consumed, including spoofed,
    # forwarded, edited, group, and Business updates. None can fall into core.
    if _get(update, "message") is not message:
        return True
    sender = _get(message, "from_user") or _get(message, "from")
    chat = _get(message, "chat")
    sender_id, chat_id = _get(sender, "id"), _get(chat, "id")
    if (
        _get(chat, "type") != "private" or not isinstance(sender_id, int) or isinstance(sender_id, bool)
        or sender_id <= 0 or chat_id != sender_id or _get(sender, "is_bot") is not False
        or _get(message, "business_connection_id") or _get(message, "_hermes_business_connection_id")
        or _get(message, "is_automatic_forward", False)
        or any(_get(message, key) is not None for key in (
            "sender_business_bot", "sender_chat", "via_bot", "forward_origin", "forward_date", "forward_from",
            "forward_from_chat", "edit_date",
        ))
    ):
        return True
    message_id = _get(message, "message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        return True
    text = _get(message, "text")
    mention = text.split()[0].partition("@")[2]
    if mention:
        try:
            if mention.casefold() != str(bot.username).casefold():
                return True
        except Exception:  # noqa: BLE001 - an uninitialized bot cannot verify its mention
            return True
    if not mark_seen(("business-stats-command", str(chat_id), str(message_id))):
        return True
    try:
        args = parse_command(text)
    except ValueError:
        logger.info("business stats command requires explicit --connection and valid stats options")
        return True
    try:
        # Deliberately bypass history's owner cache and the CLI-writable mode
        # map. Bind the response to this exact enabled connection on every read.
        connection = bot.get_business_connection(args.connection)
        if inspect.isawaitable(connection):
            connection = await connection
        owner = _get(connection, "user")
        if (
            _get(connection, "id") != args.connection or _get(connection, "is_enabled") is not True
            or _get(owner, "id") != sender_id or _get(owner, "is_bot") is not False
            or _get(connection, "user_chat_id") != chat_id
        ):
            return True
    except Exception as exc:  # noqa: BLE001 - unknown ownership always fails closed
        logger.warning("business stats owner verification failed (%s)", type(exc).__name__)
        return True
    try:
        # The shared parser/report path applies the verified connection before
        # contact lookup, ambiguity errors, or overview rendering.
        lines = await asyncio.to_thread(_stats._run, args)
    except (OSError, ValueError, OverflowError) as exc:
        lines = [f"error: {_stats._safe(str(exc))}"]
    try:
        for piece in chunks("\n".join(lines)):
            await bot.send_message(chat_id=chat_id, text=piece, parse_mode=None, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001 - do not retry ambiguous sends
        logger.warning(
            "business stats private delivery failed (%s); owner must start/unblock the bot and retry the command",
            type(exc).__name__,
        )
    return True
