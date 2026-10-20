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
import sys
import threading
import time
import types
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from model_fakes import FakeOpener
from test_history import (
    ROOT,
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
    "END",
    "NOW",
    "OWNER_ID",
    "PERSONA_DEFAULT",
    "WORKER_THREAD_NAME",
    "FakeApp",
    "FakeBot",
    "FakeCtx",
    "FakeLlm",
    "FakeOpener",
    "ago",
    "append_raw_history_records",
    "catalog_entry",
    "cli",
    "connect_gateway",
    "core_module",
    "cycle",
    "default_reply",
    "delete",
    "doc",
    "dossier_dir",
    "dossier_module",
    "edit",
    "live_worker_threads",
    "load_records",
    "maintain",
    "make_business_text_update",
    "make_deleted_update",
    "make_history_record",
    "persona_dir",
    "quote_first",
    "raw",
    "read_dossier",
    "read_json",
    "read_persona",
    "read_settings",
    "read_state",
    "seed",
    "snapshot_history_tree",
    "snapshot_tree",
    "spy_worker_cycles",
    "wait_until",
    "wait_worker_cycle",
]

# Fixed at import so every fixture and cycle in a test agrees on "now" [L4].
NOW = datetime.now(UTC).replace(microsecond=0)
OWNER_ID = 1000  # FakeBot's default owner: messages from this user are outbound
CONTACT_ID = 2000  # sender of inbound messages
BOT_ID = 4242  # sender_business_bot id for bot-posted outbound messages
BUSINESS_ID = "business-123"
WORKER_THREAD_NAME = "telesec-dossiers"
END = "<<END>>"  # the line every writer reply ends with
PERSONA_DEFAULT = f"## Style\n- Short, polite messages without emoji.\n{END}"  # FakeLlm's persona answer

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


def core_module(monkeypatch: Any, tmp_path: Path, name: str) -> ModuleType:
    """Import one plugin module in a fresh package without running the plugin entry point.

    For the pure modules (``dossier_doc``, ``dossier_writer``, ``model_client``): their tests do not
    depend on the rest of the plugin, and HERMES_HOME is ``tmp_path``.
    """
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    package_name = f"telesec_core_{uuid.uuid4().hex}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)
    return importlib.import_module(f"{package_name}.{name}")


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
    capture never records.

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


def doc(text: str) -> str:
    """A writer reply: ``text`` followed by the end marker line."""
    return f"{text}\n{END}"


def quote_first(payload: dict[str, Any], kwargs: dict[str, Any] | None = None) -> str:
    """Responder: a small document quoting the first message of the batch from the contact.

    Persona messages have no ``from`` field, so there it quotes the first message. Deleted messages
    and messages without text are skipped. The quote is the first 80 characters, whitespace
    collapsed. With no message to quote it answers ``EMPTY``.
    """
    for message in payload.get("messages", []):
        if message.get("deleted") or not message.get("text"):
            continue
        if message.get("from", "contact") == "contact":
            break
    else:
        return doc("EMPTY")
    quote = " ".join(str(message["text"]).split())[:80].strip()
    if "from" not in message:
        return doc(f'## Style\n- The owner wrote "{quote}".')
    return doc(f'**The contact.**\n\n## Notes\n- The contact said "{quote}".')


def default_reply(payload: dict[str, Any], kwargs: dict[str, Any]) -> str:
    """What ``FakeLlm`` answers once its scripted responses run out, chosen by purpose."""
    purpose = kwargs.get("purpose")
    if purpose == "telesec_persona":
        return PERSONA_DEFAULT
    if purpose == "telesec_shorten":
        return doc(str(payload.get("document", ""))[:2000])
    return quote_first(payload, kwargs)


class FakeLlm:
    """Sync stand-in for the host ``ctx.llm`` facade; dossiers use ``complete`` only.

    Scripted responses are used once each, in order; after that ``default_reply`` answers. A
    response is a reply ``str``, a callable ``(payload, kwargs) -> str``, or an exception instance
    or class, which is raised. ``on_call(payload)`` runs before the response is chosen, for a note
    or ``forget`` during a call. ``model`` and ``provider`` override the answering model and
    provider, like a host fallback. Each call is recorded in ``calls`` as its keyword arguments
    plus ``system``, ``user`` (the parsed user JSON) and ``raw`` (the user text as sent).
    """

    def __init__(self, *responses: Any, model: str | None = None, provider: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = list(responses)
        self.on_call: Callable[[dict[str, Any]], None] | None = None
        self.model = model
        self.provider = provider

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        system, user = messages[0]["content"], messages[1]["content"]
        payload = json.loads(user)
        self.calls.append({**kwargs, "system": system, "user": payload, "raw": user})
        if self.on_call is not None:
            self.on_call(payload)
        response = self.responses.pop(0) if self.responses else default_reply
        if isinstance(response, type) and issubclass(response, BaseException):
            raise response()
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(payload, kwargs)
        return SimpleNamespace(
            text=response,
            provider=self.provider or kwargs.get("provider") or "ai-gateway",
            model=self.model or kwargs.get("model") or "google/gemini-3.8-flash",
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )

    def complete_structured(self, **kwargs: Any) -> Any:
        raise AssertionError("dossiers must use complete()")

    async def acomplete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dossiers must use the sync facade")

    async def acomplete_structured(self, **kwargs: Any) -> Any:
        raise AssertionError("dossiers must use the sync facade")

    def payloads(self) -> list[dict[str, Any]]:
        """The parsed user JSON of every call, in order."""
        return [call["user"] for call in self.calls]

    def by_purpose(self, purpose: str) -> list[dict[str, Any]]:
        """The recorded calls made with ``purpose``."""
        return [call for call in self.calls if call.get("purpose") == purpose]


def cycle(ds: ModuleType, llm: Any, **kwargs: Any) -> Any:
    """``ds.run_cycle`` with ``now=NOW`` and ``trigger="cli"`` unless given."""
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("trigger", "cli")
    return ds.run_cycle(llm=llm, **kwargs)


def cli(plugin: ModuleType, *argv: Any) -> tuple[int, str, str]:
    """Run ``hermes telesec <argv>`` in-process; returns ``(code, stdout, stderr)`` unstripped.

    The parser comes from ``history_support.setup_cli``, so it includes every subcommand
    that hooks into it (``history``, ``dossier``). Argparse usage errors return their exit
    code instead of raising.
    """
    history = plugin._history_support
    parser = argparse.ArgumentParser(prog="hermes telesec")
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
    """The contact's ``document.json``, or None when it does not exist."""
    return read_json(dossier_dir(ds, chat_id, business_id=business_id) / "document.json")


def read_persona(ds: ModuleType) -> dict[str, Any] | None:
    """The persona's ``document.json``, or None when it does not exist."""
    return read_json(persona_dir(ds) / "document.json")


def read_state(ds: ModuleType) -> dict[str, Any]:
    """``state.json``, or ``{}`` when it does not exist."""
    return read_json(ds.dossier_root() / "state.json") or {}


def read_settings(ds: ModuleType) -> dict[str, Any] | None:
    """The stored on/off switch (``settings.json``), or None when it does not exist."""
    return read_json(ds.dossier_root() / "settings.json")


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


def spy_worker_cycles(monkeypatch: Any, ds: ModuleType) -> list[str]:
    """Record the outcome of every ``run_cycle`` the worker makes, in order."""
    outcomes: list[str] = []
    real = ds.run_cycle

    def spy(**kwargs: Any) -> Any:
        report = real(**kwargs)
        outcomes.append(report.outcome)
        return report

    monkeypatch.setattr(ds, "run_cycle", spy)
    return outcomes


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` seconds pass; returns its last value."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return predicate()
        time.sleep(interval)
    return True


class FakeCtx:
    """Host plugin context double that records registrations.

    ``on_unload`` exists only with ``with_unload=True``, because the plugin feature-detects it.
    ``register_auxiliary_task`` exists unless ``with_tasks=False`` (a host older than Hermes 0.21.6).
    """

    def __init__(self, llm: Any = None, *, with_unload: bool = False, with_tasks: bool = True) -> None:
        self.llm = llm
        self.get_config = {}.get
        self.telegram_handlers: list[Any] = []
        self.hooks: list[tuple[str, Any]] = []
        self.cli_commands: list[dict[str, Any]] = []
        self.skills: list[tuple[str, Any, str]] = []
        self.unload_callbacks: list[Callable[..., Any]] = []
        self.aux_tasks: list[dict[str, Any]] = []
        if with_unload:
            self.on_unload = self.unload_callbacks.append
        if with_tasks:
            self.register_auxiliary_task = self._register_auxiliary_task

    def _register_auxiliary_task(
        self,
        key: str,
        *,
        display_name: str,
        description: str,
        defaults: dict[str, Any] | None = None,
        inherit_from: str | None = None,
    ) -> None:
        self.aux_tasks.append(
            {"key": key, "display_name": display_name, "description": description, "defaults": defaults}
        )

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
