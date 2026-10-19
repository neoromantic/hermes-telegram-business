"""Plain helpers for the dossier tests.

Fixtures live in ``conftest.py``; everything here is an ordinary function or class,
so test modules import it with ``from dossier_helpers import ...`` without touching
fixture resolution. History is always written through the real capture path
(``observe_ptb_update``) except in ``raw``, which appends canonical records directly
for shapes the capture path cannot produce.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import itertools
import json
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from test_history import (
    FakeBot,
    append_raw_history_records,
    load_records,
    make_business_text_update,
    make_deleted_update,
    make_history_record,
    snapshot_history_tree,
)

__all__ = [
    "BOT_ID",
    "BUSINESS_ID",
    "CONTACT_ID",
    "NOW",
    "OWNER_ID",
    "WORKER_THREAD_NAME",
    "FakeApp",
    "FakeBot",
    "FakeCtx",
    "FakeLlm",
    "ago",
    "append_raw_history_records",
    "catalog_entry",
    "change",
    "cli",
    "connect_gateway",
    "cycle",
    "delete",
    "dossier_dir",
    "dossier_module",
    "edit",
    "live_worker_threads",
    "maintain",
    "make_business_text_update",
    "make_deleted_update",
    "make_history_record",
    "output",
    "persona_dir",
    "quote_first",
    "raw",
    "read_changes",
    "read_dossier",
    "read_json",
    "read_persona",
    "read_state",
    "seed",
    "snapshot_history_tree",
    "snapshot_tree",
    "wait_worker_cycle",
]

# Fixed at import so every fixture and cycle in a test agrees on "now" [L4].
NOW = datetime.now(UTC).replace(microsecond=0)
OWNER_ID = 1000  # FakeBot's default owner: messages from this user are outbound
CONTACT_ID = 2000  # sender of inbound messages
BOT_ID = 4242  # sender_business_bot id for bot-posted outbound messages
BUSINESS_ID = "business-123"
WORKER_THREAD_NAME = "telegram-business-dossiers"

# A time is a datetime, minutes before ``base`` (int/float), or a timedelta before ``base``.
When = datetime | int | float | timedelta
# Seed row: (message_id, who, text, when); who is "in" | "out" | "bot" | "unknown".
Row = tuple[int, str, str, When]

# Unique per process so no two helper writes collapse into one event_id.
_UPDATE_IDS = itertools.count(100_000)
_SOURCES = {
    "message.created": "business_message",
    "message.edited": "edited_business_message",
    "message.deleted": "deleted_business_messages",
}
_RAW_DIRECTIONS = {
    "in": ("inbound", CONTACT_ID),
    "out": ("outbound", OWNER_ID),
    "bot": ("outbound", OWNER_ID),
    "unknown": ("unknown", None),
}
_WHO_FROM_DIRECTION = {"inbound": "in", "outbound": "out"}


def ago(when: When, *, base: datetime = NOW) -> datetime:
    """Absolute UTC time for ``when``: a datetime as is, minutes or a timedelta counted back from ``base``."""
    if isinstance(when, datetime):
        return when
    if isinstance(when, timedelta):
        return base - when
    return base - timedelta(minutes=when)


def dossier_module(plugin: ModuleType) -> ModuleType:
    """The ``dossier_support`` module that belongs to this plugin load."""
    module = getattr(plugin, "_dossier_support", None)
    if module is None:
        module = importlib.import_module(f"{plugin.__name__}.dossier_support")
    return module


def _cancel_deletion_timers(history: ModuleType) -> None:
    # History arms real-time threading.Timers for pending deletions. Cancelling them keeps
    # classification deterministic: it then happens only on a later write or maintain().
    with history._CACHE_LOCK:
        entries = list(history._DELETION_TIMERS.values())
        history._DELETION_TIMERS.clear()
    for entry in entries:
        entry.timer.cancel()


def _sender_kwargs(
    who: str,
    *,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> dict[str, Any]:
    if who == "in":
        return {
            "from_user_id": CONTACT_ID,
            "from_user_username": username,
            "from_user_first_name": first_name,
            "from_user_last_name": last_name,
        }
    if who in ("out", "bot"):
        kwargs: dict[str, Any] = {
            "from_user_id": OWNER_ID,
            "from_user_username": "owner_account",
            "from_user_first_name": "Owner",
            "from_user_last_name": None,
        }
        if who == "bot":
            kwargs["sender_business_bot_id"] = BOT_ID
        return kwargs
    if who == "unknown":
        return {"from_user_id": None}
    raise ValueError(f"unknown sender {who!r}; use in, out, bot or unknown")


async def seed(
    plugin: ModuleType,
    chat_id: int,
    username: str | None,
    rows: Iterable[Row],
    *,
    base: datetime = NOW,
    business_id: str = BUSINESS_ID,
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    chat_type: str | None = "private",
    check: bool = True,
) -> list[bool]:
    """Write new text messages through ``observe_ptb_update``.

    Each row is ``(message_id, who, text, when)``. ``when`` is both the Telegram send
    time (``message_at``) and the observation time. ``in`` comes from CONTACT_ID,
    ``out`` from OWNER_ID (outbound through FakeBot), ``bot`` is outbound via
    ``sender_business_bot`` and ``unknown`` has no sender, so its direction is unknown.
    """
    history = plugin._history_support
    bot = FakeBot(owner_id=OWNER_ID)
    results: list[bool] = []
    for message_id, who, text, when in rows:
        at = ago(when, base=base)
        update = make_business_text_update(
            text=text,
            business_id=business_id,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_title=title,
            chat_username=username,
            chat_first_name=first_name,
            chat_last_name=last_name,
            message_id=message_id,
            update_id=next(_UPDATE_IDS),
            date=at,
            **_sender_kwargs(who, username=username, first_name=first_name, last_name=last_name),
        )
        wrote = await history.observe_ptb_update(update, bot=bot, now=at)
        _cancel_deletion_timers(history)
        if check:
            assert wrote, f"history did not record message {message_id} in chat {chat_id}"
        results.append(wrote)
    return results


def _latest_message_record(plugin: ModuleType, business_id: str, chat_id: int, message_id: int) -> dict[str, Any] | None:
    for record in reversed(load_records(plugin, business_id=business_id, chat_id=chat_id)):
        if record.get("event_type") in ("message.created", "message.edited") and str(record.get("message_id")) == str(
            message_id
        ):
            return record
    return None


async def edit(
    plugin: ModuleType,
    chat_id: int,
    message_id: int,
    text: str,
    when: When,
    *,
    who: str | None = None,
    base: datetime = NOW,
    business_id: str = BUSINESS_ID,
    check: bool = True,
) -> bool:
    """Edit a message through the real ``edited_business_message`` path, observed at ``when``.

    Like Telegram, the edit keeps the original send date, and it reuses the chat profile
    of the message's latest record. ``who`` defaults to the stored direction; pass
    ``who="unknown"`` to record an edit whose direction cannot be resolved.
    """
    history = plugin._history_support
    previous = _latest_message_record(plugin, business_id, chat_id, message_id) or {}
    at = ago(when, base=base)
    profile = previous.get("chat_profile") or {}
    sender = who or _WHO_FROM_DIRECTION.get(str(previous.get("direction")), "unknown")
    update = make_business_text_update(
        text=text,
        business_id=business_id,
        chat_id=chat_id,
        chat_type=profile.get("type", "private"),
        chat_title=profile.get("title"),
        chat_username=profile.get("username"),
        chat_first_name=profile.get("first_name"),
        chat_last_name=profile.get("last_name"),
        message_id=message_id,
        update_id=next(_UPDATE_IDS),
        date=history._parse_datetime(previous.get("message_at")) or at,
        edited=True,
        **_sender_kwargs(
            sender,
            username=profile.get("username"),
            first_name=profile.get("first_name"),
            last_name=profile.get("last_name"),
        ),
    )
    wrote = await history.observe_ptb_update(update, bot=FakeBot(owner_id=OWNER_ID), now=at)
    _cancel_deletion_timers(history)
    if check:
        assert wrote, f"history did not record the edit of message {message_id} in chat {chat_id}"
    return wrote


async def delete(
    plugin: ModuleType,
    chat_id: int,
    *message_ids: int,
    when: When,
    base: datetime = NOW,
    business_id: str = BUSINESS_ID,
    chat_type: str | None = "private",
    check: bool = True,
) -> bool:
    """Delete messages through the real ``deleted_business_messages`` path, observed at ``when``.

    The deletion stays ``pending`` until a later write to the chat or ``maintain()`` runs
    after the correction window (10 s under the ``dossiers`` fixture) has passed.
    """
    history = plugin._history_support
    update = make_deleted_update(
        business_id=business_id,
        chat_id=chat_id,
        chat_type=chat_type,
        message_ids=tuple(message_ids),
        update_id=next(_UPDATE_IDS),
    )
    wrote = await history.observe_ptb_update(update, bot=FakeBot(owner_id=OWNER_ID), now=ago(when, base=base))
    _cancel_deletion_timers(history)
    if check:
        assert wrote, f"history did not record the deletion of {message_ids} in chat {chat_id}"
    return wrote


def maintain(plugin: ModuleType, when: When = 0, *, base: datetime = NOW) -> Any:
    """Run history maintenance (deletion classification, retention and size pruning) at ``when``."""
    history = plugin._history_support
    result = history.maintain_history(now=ago(when, base=base))
    _cancel_deletion_timers(history)
    return result


def raw(
    plugin: ModuleType,
    chat_id: int,
    rows: Iterable[tuple[Any, ...]],
    *,
    username: str | None = None,
    base: datetime = NOW,
    business_id: str = BUSINESS_ID,
    chat_type: str | None = "private",
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    message_lag: timedelta | None = timedelta(0),
    rebuild: bool = True,
) -> list[dict[str, Any]]:
    """Append canonical records directly, then rebuild the contact catalog.

    Use it for shapes the capture path cannot produce: unknown direction on a known
    sender, missing ``message_at``, partitions in old months, and group chats that
    the history allowlist would reject.

    Each row is ``(message_id, who, text, when)`` or ``(message_id, who, text, when, event)``
    with ``event`` in ``created`` (default), ``edited`` or ``deleted``. ``when`` is the
    observation time. ``message_at = observed_at - message_lag``; ``message_lag=None``
    omits ``message_at``. Created and edited records carry a chat profile snapshot.
    Returns the written records.
    """
    history = plugin._history_support
    profile = history._normalize_profile_snapshot(
        {
            "id": chat_id,
            "type": chat_type,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "title": title,
        }
    )
    records: list[dict[str, Any]] = []
    for row in rows:
        message_id, who, text, when = row[:4]
        event_type = f"message.{row[4] if len(row) > 4 else 'created'}"
        if event_type not in _SOURCES:
            raise ValueError(f"unknown raw event {row[4]!r}; use created, edited or deleted")
        direction, sender_id = _RAW_DIRECTIONS[who]
        observed_at = ago(when, base=base)
        record = make_history_record(
            plugin,
            event_type=event_type,
            source=_SOURCES[event_type],
            observed_at=observed_at,
            telegram_update_id=next(_UPDATE_IDS),
            business_connection_id=business_id,
            chat_id=chat_id,
            message_id=message_id,
            message_at=None if message_lag is None else observed_at - message_lag,
            sender_id=sender_id,
            direction=direction,
            text=None if event_type == "message.deleted" else text,
        )
        # chat_profile is not part of the event-id signature, so adding it afterwards is safe.
        if event_type != "message.deleted" and profile is not None:
            record["chat_profile"] = profile
        records.append(record)
    append_raw_history_records(plugin, *records)
    if rebuild:
        history.rebuild_contact_catalog()
    return records


def catalog_entry(plugin: ModuleType, chat_id: int, *, business_id: str = BUSINESS_ID) -> dict[str, Any]:
    """The catalog entry for a chat, resolved like ``--connection/--chat`` on the CLI."""
    return plugin._history_support._resolve_history_entry(
        business_connection_id=business_id,
        chat_id=str(chat_id),
        contact=None,
    )


class FakeLlm:
    """Sync stand-in for the host ``ctx.llm`` facade.

    Scripted responses are used once each, in order; after that the model answers
    ``{"changes": []}``. A response is a dict (returned as ``parsed``), a str (returned as
    raw ``text`` with ``parsed=None``), a callable ``(payload, kwargs) -> dict | str``, or an
    exception instance or class, which is raised. ``on_call(payload)`` runs before the
    response is chosen, for owner edits or ``forget`` during a call. ``model`` overrides
    the answering model name, like a host fallback.
    """

    def __init__(self, *responses: Any, model: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = list(responses)
        self.on_call: Callable[[dict[str, Any]], None] | None = None
        self.model = model

    def complete_structured(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        payload = json.loads(kwargs["input"][0]["text"])
        if self.on_call is not None:
            self.on_call(payload)
        response = self.responses.pop(0) if self.responses else {"changes": []}
        if isinstance(response, type) and issubclass(response, BaseException):
            raise response()
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(payload, kwargs)
        model = self.model or kwargs.get("model")
        if isinstance(response, str):
            return SimpleNamespace(parsed=None, text=response, provider=kwargs.get("provider"), model=model)
        return SimpleNamespace(parsed=response, text="", provider=kwargs.get("provider"), model=model)

    async def acomplete_structured(self, **kwargs: Any) -> Any:
        raise AssertionError("dossiers must use the sync facade")

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(call["input"][0]["text"]) for call in self.calls]


def _source_item(source: Any) -> dict[str, str]:
    if isinstance(source, dict):
        return dict(source)
    if isinstance(source, str):
        return {"ref": source, "quote": ""}
    ref, quote = source
    return {"ref": ref, "quote": quote}


def change(
    op: str = "add",
    fid: str = "",
    section: str = "preferences",
    kind: str = "observed",
    conf: str = "high",
    text: str = "…",
    sources: Iterable[Any] = (("m10", "quote"),),
    reason: str = "",
) -> dict[str, Any]:
    """One model output item. A source is ``(ref, quote)``, a bare ref (empty quote) or a dict."""
    return {
        "op": op,
        "fact_id": fid,
        "section": section,
        "kind": kind,
        "confidence": conf,
        "text": text,
        "sources": [_source_item(source) for source in sources],
        "reason": reason,
    }


def output(*items: dict[str, Any]) -> dict[str, Any]:
    """A model output object: ``{"changes": [items...]}``."""
    return {"changes": list(items)}


def quote_first(payload: dict[str, Any], kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Responder: one observed fact quoting the first ``contact`` message of the batch.

    Persona payloads have no ``from`` field, so there it quotes the first message. The
    quote is the first 80 characters (whitespace collapsed) and the fact text is
    ``The contact said "<quote>".`` (``The owner wrote ...`` for the persona). The section
    is ``preferences`` when offered, else the payload's first section. With no matching
    message it returns no changes.
    """
    for message in payload.get("messages", []):
        if message.get("from", "contact") == "contact":
            break
    else:
        return output()
    quote = " ".join(str(message["text"]).split())[:80].strip()
    sections = list(payload.get("sections") or ["preferences"])
    section = "preferences" if "preferences" in sections else sections[0]
    lead = "The contact said" if "from" in message else "The owner wrote"
    return output(change(section=section, text=f'{lead} "{quote}".', sources=((message["ref"], quote),)))


def cycle(ds: ModuleType, llm: Any, **kwargs: Any) -> Any:
    """``ds.run_cycle`` with ``now=NOW`` and ``trigger="cli"`` unless given."""
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("trigger", "cli")
    return ds.run_cycle(llm=llm, **kwargs)


def cli(plugin: ModuleType, *argv: Any) -> tuple[int, str, str]:
    """Run ``hermes telegram-business <argv>`` in-process; returns ``(code, stdout, stderr)`` unstripped.

    The parser comes from ``history_support.setup_cli``, so it includes every subcommand
    that hooks into it (``history``, ``dossier``). Argparse usage errors return their exit
    code instead of raising.
    """
    history = plugin._history_support
    parser = argparse.ArgumentParser(prog="hermes telegram-business")
    history.setup_cli(parser)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = history.handle_cli(parser.parse_args([str(arg) for arg in argv]))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.getvalue(), err.getvalue()


def dossier_dir(ds: ModuleType, chat_id: int, *, business_id: str = BUSINESS_ID) -> Path:
    """``<dossier_root>/chats/<chat_key>`` for a contact."""
    return ds.dossier_root() / "chats" / ds.chat_key(business_id, chat_id)


def persona_dir(ds: ModuleType) -> Path:
    return ds.dossier_root() / "persona"


def read_json(path: Path) -> Any:
    """Parsed JSON at ``path``, or None when the file is missing."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_dossier(ds: ModuleType, chat_id: int, *, business_id: str = BUSINESS_ID) -> dict[str, Any] | None:
    return read_json(dossier_dir(ds, chat_id, business_id=business_id) / "dossier.json")


def read_persona(ds: ModuleType) -> dict[str, Any] | None:
    return read_json(persona_dir(ds) / "dossier.json")


def read_state(ds: ModuleType) -> dict[str, Any]:
    """``state.json``, or ``{}`` when it does not exist."""
    return read_json(ds.dossier_root() / "state.json") or {}


def read_changes(directory: Path) -> list[dict[str, Any]]:
    """Every line of ``<directory>/changes.jsonl``, oldest first; blank lines skipped."""
    path = directory / "changes.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def snapshot_tree(root: Path) -> dict[str, tuple[str, bytes, int]]:
    """``relative path -> (kind, bytes, mtime_ns)`` for everything under ``root``; ``{}`` if absent.

    Compare two snapshots to prove a command wrote nothing (contents and mtimes).
    """
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[str, bytes, int]] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[key] = ("dir", b"", path.stat().st_mtime_ns)
        else:
            snapshot[key] = ("file", path.read_bytes(), path.stat().st_mtime_ns)
    return snapshot


def live_worker_threads() -> list[threading.Thread]:
    """Live threads named like the dossier worker, from any plugin load."""
    return [thread for thread in threading.enumerate() if thread.name == WORKER_THREAD_NAME and thread.is_alive()]


def wait_worker_cycle(ds: ModuleType, timeout: float = 5.0) -> Any:
    """Wait until the only registered worker finishes a cycle; returns the worker."""
    workers = list(ds._WORKERS.values())
    assert len(workers) == 1, f"expected one dossier worker, found {len(workers)}"
    assert workers[0].cycle_done.wait(timeout), "dossier worker did not finish a cycle in time"
    return workers[0]


class FakeCtx:
    """Host plugin context double that records registrations.

    ``on_unload`` exists only with ``with_unload=True``, because the plugin feature-detects it.
    """

    def __init__(self, llm: Any = None, *, with_unload: bool = False) -> None:
        self.llm = llm
        self.telegram_handlers: list[Any] = []
        self.hooks: list[tuple[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []
        self.skills: list[tuple[str, Any, str]] = []
        self.unload_callbacks: list[Callable[..., Any]] = []
        if with_unload:
            self.on_unload = self.unload_callbacks.append

    def register_telegram_handler(self, factory: Any) -> None:
        self.telegram_handlers.append(factory)

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks.append((name, callback))

    def register_cli_command(self, **kwargs: Any) -> None:
        self.cli_commands.append(kwargs)

    def register_skill(self, name: str, path: Any, description: str = "") -> None:
        self.skills.append((name, path, description))


class FakeApp:
    """PTB Application double for the Telegram handler factory."""

    def __init__(self) -> None:
        self.handlers: list[tuple[Any, int]] = []

    def add_handler(self, handler: Any, group: int = 0) -> None:
        self.handlers.append((handler, group))


def connect_gateway(plugin: ModuleType, app: FakeApp | None = None) -> FakeApp:
    """Run the plugin's Telegram handler factory the way ``TelegramAdapter.connect()`` does."""
    app = app or FakeApp()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram"))
    return app
