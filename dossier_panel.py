"""Telegram glue for dossiers: the owner panel section and the owner's ``/dossier`` in a Business chat.

Rendering lives in ``dossier_view`` (pure) and the ``/dossier`` summary in ``dossier_post``; this module
loads snapshots, starts refreshes and posts, and talks to the Bot API. Blocking work (files, history
folds, cycles, model calls) runs in threads. Nothing but the summary the owner's exact ``/dossier`` asks
for ever goes into a Business chat; refusals go to the owner's private chat with the bot. Logs carry
action names, counts, hashed chat refs and error type names only, never message or document text, notes
or names.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime
from typing import Any

from . import chat_settings as _chat_settings
from . import dossier_post as _post
from . import dossier_support as _ds
from . import dossier_view as _view
from . import history_support as _history
from . import transcription_modes as _modes

logger = logging.getLogger(__name__)

SECTION_KEY = "dossier"
SECTION_LABEL = "Dossier"
REFRESH_THREAD_NAME = "telesec-dossier-refresh"
COMMAND_IDENTITY = "dossier-command"

_REFRESH_LOCK = threading.Lock()
# Business connections the gateway saw in updates this process (set by register_owner_panel_section).
_seen_connections: Callable[[], Iterable[Any]] = tuple
# (dossier root, chat key) -> (refresh thread, its stop event)
_REFRESHES: dict[tuple[str, str], tuple[threading.Thread, threading.Event]] = {}
# (dossier root, chat key) -> the running /dossier post and its stop event (once set, its build thread
# makes no further model call); a second command meanwhile is "busy". The loop thread adds and removes
# posts; stop_refresh may read them from a worker thread.
_POST_LOCK = threading.Lock()
_POST_TASKS: dict[tuple[str, str], tuple[asyncio.Task[None], threading.Event]] = {}


# ---------------------------------------------------------------------------
# Snapshots (blocking: callers run them with asyncio.to_thread)
# ---------------------------------------------------------------------------


def llm_ready(llm: Any = None) -> bool:
    """Is a host LLM facade with ``complete`` bound (or is ``llm`` one)?"""
    return _ds._llm_ready(_ds._LLM if llm is None else llm)


def _stub(business_connection_id: Any, chat_id: Any) -> dict[str, Any]:
    return {"business_connection_id": str(business_connection_id), "chat_id": chat_id}


def contact_entry(business_connection_id: Any, chat_id: Any) -> tuple[dict[str, Any], bool]:
    """(private catalog entry, True), else ({connection, chat} stub, False)."""
    if not business_connection_id or chat_id is None:
        raise ValueError("no Business chat")
    try:
        entry = _history._resolve_history_entry(business_connection_id=str(business_connection_id),
                                                chat_id=str(chat_id), contact=None)
    except (OSError, ValueError):
        entry = None
    if isinstance(entry, dict) and entry.get("chat_type") == "private":
        return entry, True
    return _stub(business_connection_id, chat_id), False


def _display_name(entry: dict[str, Any], private: bool) -> str:
    return _history._catalog_entry_display_name(entry) if private else f"chat:{entry.get('chat_id')}"


def _failure(state: dict[str, Any], key: str) -> str | None:
    item = (state.get("failures") or {}).get(key)
    return str(item["error"]) if isinstance(item, dict) and item.get("error") else None


def _cycle_busy() -> bool:
    try:
        return _ds.cycle_running()
    except OSError:
        return False


def _refresh_key(key: str) -> tuple[str, str]:
    return str(_ds.dossier_root()), key


def refreshing(business_connection_id: Any, chat_id: Any) -> bool:
    with _REFRESH_LOCK:
        item = _REFRESHES.get(_refresh_key(_ds.chat_key(business_connection_id, chat_id)))
    return item is not None and item[0].is_alive()


def manages_global(owner_user_id: Any, business_connection_id: Any) -> bool:
    """May ``owner_user_id`` open the persona and switch dossiers? Both are global, and any Premium account
    can connect the bot, so only an owner of every connection the plugin knows may: private chats in history,
    stored contact dossiers, chats the persona read, recorded bindings, and this process's lookups and
    Business updates. Owners come from this process's lookups, then the transcription modes bindings; an
    unknown owner, or a stored contact dossier whose connection is not among those, fails closed. Blocking."""
    owner = str(owner_user_id or "")
    if not owner or not business_connection_id:
        return False
    try:
        store = _modes.default_store()
        connections = {str(e["business_connection_id"]) for e in _ds.private_entries()
                       if e.get("business_connection_id")} | {str(business_connection_id)}
        connections |= set(store.snapshot().connections) | set(_history._OWNER_CACHE)
        connections |= {str(c) for c in _seen_connections() if c}
        try:
            persona = _ds.load_persona() or {}
        except (OSError, ValueError):  # corrupt: its screen shows only the reset command; the other sources still gate
            persona = {}
        connections |= {str(c["business_connection_id"]) for c in persona.get("chats") or []
                        if isinstance(c, dict) and c.get("business_connection_id")}
        chats = _ds.dossier_root() / "chats"
        stored = {p.name for p in chats.iterdir() if p.is_dir()} if chats.is_dir() else set()
        if stored - {_history._path_key(c) for c in connections}:
            return False  # a contact dossier of a connection whose owner is unknown here
        return all((_history.cached_business_owner(c) or store.connection_owner(c)) == owner for c in connections)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.info("%s: dossier panel owner scope check failed error=%s", _history.PLUGIN_NAME,
                    type(exc).__name__)
        return False


def _unqualified(entry: dict[str, Any], now: datetime) -> bool:
    """No document can be written yet: the first pass lacks messages from one side."""
    try:
        p = _ds.pending(entry, now=now)
    except Exception:  # noqa: BLE001 - shown as "no dossier yet"
        return False
    return p.first and not p.qualified


def load_contact_snapshot(business_connection_id: Any, chat_id: Any, *, now: datetime | None = None,
                          manage_global: bool = False, owner_user_id: Any = None) -> _view.DossierSnapshot:
    """Everything the contact screen needs for one Business chat. Blocking.

    ``owner_user_id`` is the chat's verified owner, whose per-chat switches apply (else the known one).
    """
    now, cfg = now or _history._utcnow(), _ds.load_config()
    entry, private = contact_entry(business_connection_id, chat_id)
    history_enabled = _history.capture_enabled()
    chat_off = _chat_settings.paused(business_connection_id, chat_id, "dossier", owner=owner_user_id)
    try:
        doc, corrupt = _ds.load_contact(business_connection_id, chat_id), False
    except (OSError, ValueError):
        doc, corrupt = None, True
    unqualified = False
    if not corrupt and not (doc or {}).get("document") and private and cfg.enabled and history_enabled \
            and not chat_off:
        unqualified = _unqualified(entry, now)
    return _view.DossierSnapshot(
        target="contact", name=_display_name(entry, private), chat_id=chat_id, doc=doc, corrupt=corrupt,
        enabled=cfg.enabled, history_enabled=history_enabled, chat_off=chat_off, llm_ready=llm_ready(),
        refreshing=refreshing(business_connection_id, chat_id),
        failure=_failure(_ds.load_state(), _ds.chat_key(business_connection_id, chat_id)), unqualified=unqualified,
        manage_global=manage_global, in_history=private)


def load_persona_snapshot() -> _view.DossierSnapshot:
    """Everything the persona screen needs. Blocking."""
    try:
        doc, corrupt = _ds.load_persona(), False
    except (OSError, ValueError):
        doc, corrupt = None, True
    return _view.DossierSnapshot(
        target="persona", doc=doc, corrupt=corrupt, enabled=_ds.load_config().enabled,
        history_enabled=_history.capture_enabled(), llm_ready=llm_ready(),
        failure=_failure(_ds.load_state(), "persona"), manage_global=True)


def load_snapshot(target: str, business_connection_id: Any, chat_id: Any, *, now: datetime | None = None,
                  manage_global: bool = False, owner_user_id: Any = None) -> _view.DossierSnapshot:
    if target == "persona":
        return load_persona_snapshot()
    return load_contact_snapshot(business_connection_id, chat_id, now=now, manage_global=manage_global,
                                 owner_user_id=owner_user_id)


# ---------------------------------------------------------------------------
# Refresh (``dossier run --contact`` for one chat, in the background)
# ---------------------------------------------------------------------------


def _up_to_date(entry: dict[str, Any], key: str) -> bool:
    """The document is built, nothing is new, edited or deleted, the note is unchanged and nothing failed."""
    try:
        if _failure(_ds.load_state(), key):
            return False
        return _ds.pending(entry).nothing()
    except Exception:  # noqa: BLE001 - e.g. an unreadable document: the refresh records the failure
        return False


def start_refresh(business_connection_id: Any, chat_id: Any, owner_user_id: Any = None) -> str:
    """Start a forced contact cycle in a thread: started | running | busy | unavailable | up_to_date.

    "up_to_date" starts no thread and calls no model. Blocking.
    """
    cfg, llm = _ds.load_config(), _ds._LLM
    entry, private = contact_entry(business_connection_id, chat_id)
    if not cfg.enabled or not _history.capture_enabled() or not llm_ready(llm) or not private:
        return "unavailable"
    if _chat_settings.paused(business_connection_id, chat_id, "dossier", owner=owner_user_id):
        return "unavailable"
    chat = _ds.chat_key(business_connection_id, chat_id)
    key = _refresh_key(chat)
    current = _up_to_date(entry, chat)  # reads history: outside the lock
    with _REFRESH_LOCK:
        running = _REFRESHES.get(key)
        if running is not None and running[0].is_alive():
            return "running"
        if _cycle_busy():
            return "busy"
        if current:
            return "up_to_date"
        snapshot = contextvars.copy_context()  # keeps the profile scope (HERMES_HOME ContextVar)
        stop = threading.Event()
        thread = threading.Thread(target=snapshot.run, args=(_refresh_worker, llm, entry, key, stop),
                                  name=REFRESH_THREAD_NAME, daemon=True)
        _REFRESHES[key] = (thread, stop)
        thread.start()
    logger.info("%s: dossier refresh started chat_ref=%s", _history.PLUGIN_NAME, _ds._log_ref(entry))
    return "started"


def _refresh_worker(llm: Any, entry: dict[str, Any], key: tuple[str, str], stop: threading.Event) -> None:
    try:
        r = _ds.run_cycle(llm=llm, trigger="telegram", target=("contact", entry), stop=stop)
        logger.info("%s: dossier refresh chat_ref=%s outcome=%s calls=%d refreshed=%d failed=%d duration_ms=%d",
                    _history.PLUGIN_NAME, _ds._log_ref(entry), r.outcome, r.calls, r.refreshed, r.failed,
                    r.duration_ms)
    except Exception as exc:  # noqa: BLE001 - a background refresh must never raise
        logger.warning("%s: dossier refresh failed chat_ref=%s error=%s", _history.PLUGIN_NAME,
                       _ds._log_ref(entry), type(exc).__name__)
    finally:
        with _REFRESH_LOCK:
            item = _REFRESHES.get(key)
            if item is not None and item[0] is threading.current_thread():
                del _REFRESHES[key]


def _cancel(task: asyncio.Task[Any]) -> None:
    """Cancel ``task`` from any thread: ``Task.cancel`` is not thread-safe, so another thread asks its loop."""
    loop = task.get_loop()
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        task.cancel()
        return
    try:
        loop.call_soon_threadsafe(task.cancel)
    except RuntimeError:  # the loop is closed, so the task no longer runs
        pass


def stop_refresh(business_connection_id: Any, chat_id: Any) -> None:
    """Stop this chat's panel refresh and its ``/dossier`` post ("No dossier", "Don't save" or an erase):
    no further model call, write or post. Safe from any thread."""
    key = _refresh_key(_ds.chat_key(business_connection_id, chat_id))
    with _REFRESH_LOCK:
        item = _REFRESHES.get(key)
    if item is not None:
        item[1].set()
    with _POST_LOCK:
        post = _POST_TASKS.get(key)
    if post is not None:
        post[1].set()
        _cancel(post[0])


def join_refreshes(timeout: float | None = None) -> None:
    """Wait for running refresh threads (tests, shutdown)."""
    with _REFRESH_LOCK:
        threads = [thread for thread, _stop in _REFRESHES.values()]
    for thread in threads:
        thread.join(timeout)


def stop_background(timeout: float = 0.0) -> None:
    """on_unload: stop the gateway worker, every panel refresh (it ends a rate-limit wait and makes no
    further model call or write) and every ``/dossier`` post. Blocks only with a timeout."""
    _ds.stop_workers(timeout)
    with _REFRESH_LOCK:
        items = list(_REFRESHES.values())
    for _thread, stop in items:
        stop.set()
    with _POST_LOCK:
        posts = list(_POST_TASKS.values())
    for task, stop in posts:
        stop.set()
        _cancel(task)
    if timeout:
        for thread, _stop in items:
            thread.join(timeout)


# ---------------------------------------------------------------------------
# Owner panel
# ---------------------------------------------------------------------------


def switch(enabled: bool) -> None:
    """The global dossier switch from Telegram (Settings). Blocking."""
    _ds.set_enabled(enabled, via="telegram")
    if enabled:
        wake()


def wake() -> None:
    """The worker runs whenever the facade is bound; a wake starts the next cycle sooner. Blocking."""
    _ds.ensure_worker_started()
    _ds.wake_worker()


async def handle_panel_action(*, owner_user_id: Any, business_connection_id: Any, chat_id: Any,
                              language_code: str | None, args: str | None) -> _view.View:
    """The dossier section of the owner panel: callback ``args`` ('' or None = the dossier) -> a view.

    The panel has already checked that ``owner_user_id`` owns this Business chat.
    """
    lang = _view.lang_for(language_code)
    try:
        return await _panel_action(owner_user_id, business_connection_id, chat_id, lang, args)
    except Exception as exc:  # noqa: BLE001 - the panel always gets a view
        logger.warning("%s: dossier panel action failed error=%s", _history.PLUGIN_NAME, type(exc).__name__)
        return _view.render_state("failed", lang, error=type(exc).__name__)


async def _panel_action(owner_user_id: Any, conn: Any, chat_id: Any, lang: str, args: str | None) -> _view.View:
    if not conn or chat_id is None:  # the panel has no verified connection for this chat yet
        logger.info("%s: dossier panel without a known Business connection", _history.PLUGIN_NAME)
        return _view.render_state("no_connection", lang)
    # Malformed args and the fact buttons of earlier versions show the dossier: nothing is written.
    action = (_view.parse_args(args) if args else None) or _view.Action("overview")
    manage_global = await asyncio.to_thread(manages_global, owner_user_id, conn)
    if not manage_global and action.target == "persona":
        logger.info("%s: dossier panel %s refused target=persona reason=owner_scope", _history.PLUGIN_NAME,
                    action.name)
        return _view.render_state("operator_only", lang)

    async def snapshot(kind: str) -> _view.DossierSnapshot:
        return await asyncio.to_thread(load_snapshot, kind, conn, chat_id, manage_global=manage_global,
                                       owner_user_id=owner_user_id)

    if action.name == "refresh":
        outcome = await asyncio.to_thread(start_refresh, conn, chat_id, owner_user_id)
        if outcome in ("running", "up_to_date"):
            notice = _view.notice_text("refresh_started" if outcome == "running" else "up_to_date", lang)
            return replace(_view.render_overview(await snapshot("contact"), lang), notice=notice)
        return _view.render_state({"started": "refresh_started"}.get(outcome, outcome), lang)
    return _view.render_overview(await snapshot(action.target), lang)  # the panel adds its own Back row


def panel_view(owner_panel: Any, view: _view.View, chat_id: Any) -> Any:
    """``view`` as the owner panel's ``PanelView``: args become section callbacks (a button whose data would
    not fit Telegram's 64 bytes is dropped) and the notice heads the text, since the panel's toast is the
    section label."""
    view = _view.with_notice(view)
    rows = []
    for row in view.buttons:
        kept = []
        for label, args in row:
            try:
                kept.append((label, owner_panel.section_callback(chat_id, SECTION_KEY, args)))
            except ValueError:
                continue
        if kept:
            rows.append(kept)
    return owner_panel.PanelView(text=view.text, buttons=rows, html=view.html)


def register_owner_panel_section(owner_panel: Any,
                                 seen_connections: Callable[[], Iterable[Any]] | None = None) -> None:
    """Add the 'Dossier' section to the owner panel ("Manage Bot" in a Business chat).

    ``seen_connections`` returns the Business connections seen in updates this process; the persona
    and the switch stay closed until their owners are known too (see manages_global).
    """
    global _seen_connections
    if seen_connections is not None:
        _seen_connections = seen_connections

    async def render(ctx: Any) -> Any:
        view = await handle_panel_action(
            owner_user_id=ctx.owner_user_id, business_connection_id=ctx.business_connection_id,
            chat_id=ctx.chat_id, language_code=ctx.language_code, args=ctx.arg)
        return panel_view(owner_panel, view, ctx.chat_id)

    owner_panel.register_strings({"en": {f"section.{SECTION_KEY}": SECTION_LABEL},
                                  "ru": {f"section.{SECTION_KEY}": "Досье"}})
    owner_panel.register_section(SECTION_KEY, SECTION_LABEL, render)


# ---------------------------------------------------------------------------
# /dossier in a Business chat
# ---------------------------------------------------------------------------


def _chat_id_value(value: Any) -> Any:
    text = str(value)
    return int(text) if text.lstrip("-").isdigit() else value


async def _delete_command(bot: Any, business_connection_id: str, message_id: Any) -> None:
    delete = getattr(bot, "delete_business_messages", None)
    if not callable(delete) or message_id is None:
        return
    try:
        await _history._maybe_await(delete(business_connection_id=business_connection_id, message_ids=[message_id]))
    except Exception as exc:  # noqa: BLE001 - the post still goes out
        logger.warning("%s: could not delete the /dossier command (%s); allow the bot to delete messages "
                       "in Telegram Business > Chatbots", _history.PLUGIN_NAME, type(exc).__name__)


async def _tell_owner(bot: Any, owner_user_id: Any, text: str) -> None:
    """Why ``/dossier`` posted nothing, in the owner's private chat with the bot (never the Business chat)."""
    try:
        await bot.send_message(chat_id=_chat_id_value(owner_user_id), text=text, parse_mode=None)
    except Exception as exc:  # noqa: BLE001 - e.g. the owner never started the bot
        logger.warning("%s: could not send the /dossier reason to the owner error=%s", _history.PLUGIN_NAME,
                       type(exc).__name__)


async def _refuse(bot: Any, owner_user_id: Any, lang: str, entry: dict[str, Any], name: str, reason: str,
                  error: str | None = None) -> None:
    logger.info("%s: dossier not posted chat_ref=%s reason=%s error=%s", _history.PLUGIN_NAME, _ds._log_ref(entry),
                reason, error or "-")
    await _tell_owner(bot, owner_user_id, _view.post_refusal(reason, lang, name=name, error=error))


def _post_target(business_connection_id: str, chat_id: Any) -> tuple[dict[str, Any], str]:
    entry, private = contact_entry(business_connection_id, chat_id)
    return entry, _display_name(entry, private)


async def _target_or_stub(business_connection_id: str, chat_id: Any) -> tuple[dict[str, Any], str, str | None]:
    """(entry, name, None), or a stub and ``chat:<id>`` with the error type name when the catalog read fails."""
    try:
        entry, name = await asyncio.to_thread(_post_target, business_connection_id, chat_id)
    except Exception as exc:  # noqa: BLE001 - the owner still hears why nothing was posted
        return _stub(business_connection_id, chat_id), f"chat:{chat_id}", type(exc).__name__
    return entry, name, None


def _precheck(entry: dict[str, Any]) -> tuple[str | None, int, datetime]:
    gen0, now = _ds.generation(), _history._utcnow()
    return _post.post_precheck(entry, now=now), gen0, now


def _recheck(entry: dict[str, Any], inputs: dict[str, str], gen0: int) -> tuple[str | None, datetime]:
    now = _history._utcnow()
    reason = _post.post_recheck(entry, inputs, gen0=gen0, now=now)
    return reason, now.astimezone(_ds.today_text(now)[1])  # the footer date is the owner's local date


async def _build_and_send(bot: Any, entry: dict[str, Any], conn: str, chat_id: Any, started: float,
                          stop: threading.Event) -> tuple[str | None, str | None]:
    """Precheck, clean-room build, final recheck, send: (refusal reason, error), or (None, None) once posted.

    The wall cap (TimeoutError) covers the steps before the send only: a send that has started may already
    be delivered, so it is never cut and never told as "nothing was posted".
    """
    async with asyncio.timeout(_post.POST_WALL_SECONDS):
        reason, gen0, now = await asyncio.to_thread(_precheck, entry)
        if reason is not None:
            return reason, None
        result = await asyncio.to_thread(_post.build_post, entry, llm=_ds._LLM, now=now,
                                         allowed=lambda: not stop.is_set())
        if result.status != "ok":
            return ("empty", None) if result.status == "empty" else ("failed", result.error)
        reason, local = await asyncio.to_thread(_recheck, entry, result.inputs, gen0)
        if reason is not None:
            return reason, None
    # Rich only: the echo of a plain post would not be recognised as the bot's own and would reach history.
    try:
        sent = await bot.do_api_request("sendRichMessage", api_kwargs={
            "business_connection_id": conn, "chat_id": chat_id,
            "rich_message": {"html": _view.post_html(result.text, local), "skip_entity_detection": True}})
    except Exception as exc:  # noqa: BLE001 - told to the owner privately
        return "send_failed", type(exc).__name__
    _history.remember_control_message(conn, chat_id, _history._get(sent, "message_id"))
    logger.info("%s: dossier posted chat_ref=%s chars=%d ms=%d model=%s/%s", _history.PLUGIN_NAME,
                _ds._log_ref(entry), len(result.text), int((time.monotonic() - started) * 1000),
                _ds.without_credentials(result.provider or "-"), _ds.without_credentials(result.model or "-"))
    return None, None


async def _post_task(bot: Any, business_connection_id: str, chat_id: Any, owner_user_id: Any, lang: str,
                     stop: threading.Event) -> None:
    """One ``/dossier``: the summary goes into the chat, or the reason to the owner. Cancelled by stop_refresh."""
    started = time.monotonic()
    entry, name, error = await _target_or_stub(business_connection_id, chat_id)
    if error is not None:
        await _refuse(bot, owner_user_id, lang, entry, name, "failed", error)
        return
    try:
        reason, error = await _build_and_send(bot, entry, business_connection_id, chat_id, started, stop)
    except TimeoutError:  # the wall cap, before the send
        reason, error = "failed", "timeout"
    except Exception as exc:  # noqa: BLE001 - e.g. unreadable history; never the text
        reason, error = "failed", type(exc).__name__
    finally:
        stopped = stop.is_set()  # stop_refresh or stop_background: the owner turned something off
        stop.set()  # a build thread that outlives the cap or a cancel makes no further model call
    if reason is not None and not stopped:
        await _refuse(bot, owner_user_id, lang, entry, name, reason, error)


def _post_done(key: tuple[str, str], task: asyncio.Task[None]) -> None:
    with _POST_LOCK:
        post = _POST_TASKS.get(key)
        if post is not None and post[0] is task:
            del _POST_TASKS[key]
    if not task.cancelled() and task.exception() is not None:
        logger.warning("%s: dossier post failed error=%s", _history.PLUGIN_NAME, type(task.exception()).__name__)


async def _start_post(bot: Any, payload: Any, *, business_connection_id: str, chat_id: Any,
                      owner_user_id: Any) -> None:
    """Delete the command and start its post in the background; a post already running for the chat -> busy."""
    lang = _view.lang_for(_history._get(_history._get(payload, "from_user"), "language_code"))
    command_id = _history._get(payload, "message_id")
    _history.remember_control_message(business_connection_id, chat_id, command_id)
    await _delete_command(bot, business_connection_id, command_id)
    key = _refresh_key(_ds.chat_key(business_connection_id, chat_id))
    with _POST_LOCK:
        running = _POST_TASKS.get(key)
        busy = running is not None and not running[0].done()
        if not busy:
            stop = threading.Event()
            task = asyncio.get_running_loop().create_task(
                _post_task(bot, business_connection_id, chat_id, owner_user_id, lang, stop))
            _POST_TASKS[key] = (task, stop)
    if busy:
        entry, name, _error = await _target_or_stub(business_connection_id, chat_id)
        await _refuse(bot, owner_user_id, lang, entry, name, "busy")
        return
    task.add_done_callback(lambda done: _post_done(key, done))


async def join_posts() -> None:
    """Wait for the ``/dossier`` posts running on this event loop (tests, shutdown)."""
    loop = asyncio.get_running_loop()
    with _POST_LOCK:
        tasks = [task for task, _stop in _POST_TASKS.values() if task.get_loop() is loop]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)  # gather can return at once for finished tasks; their done callbacks run now


async def handle_dossier_command_update(update: Any, *, bot: Any, adapter: Any,
                                        mark_seen: Any) -> bool:
    """Start an owner ``/dossier``; True means the update is claimed (never history, never core).

    The summary is written and posted in the background, so this returns before the model answers.
    The bot's own posted summary (its echo) is claimed too. A contact's ``/dossier``, a forwarded
    one, or one whose owner cannot be verified is ordinary text. ``mark_seen(identity)`` returns
    False for a repeated delivery.
    """
    edited = getattr(update, "edited_business_message", None)
    payload = edited or getattr(update, "business_message", None)
    if payload is None:
        return False
    conn = _history._get(payload, "business_connection_id") or _history._get(payload, "_hermes_business_connection_id")
    chat = _history._get(payload, "chat")
    chat_id = _history._get(chat, "id")
    if _history.is_dossier_post(payload):
        # Its send may have timed out after delivery, so only its echo tells the id its deletion has.
        _history.remember_control_message(conn, chat_id, _history._get(payload, "message_id"))
        return True
    text = _history._get(payload, "text")
    if not _history.is_dossier_control_message(payload, text):
        return False
    if not conn or chat_id is None or _history._get(chat, "type") not in (None, "private"):
        return False
    resolved = await _history.resolve_business_direction(
        payload, bot=bot, stored_owner=_modes.default_store().connection_owner)
    if resolved.direction != "own":
        logger.debug("%s: /dossier not from the owner reason=%s", _history.PLUGIN_NAME, resolved.reason)
        return False
    if edited is not None:  # an owner edit into `/dossier`: claimed, never executed
        try:  # history still records the edit of a message it already has
            await _history.observe_ptb_update(update, bot=bot, direction=_history._HISTORY_DIRECTIONS["own"])
        except Exception as exc:  # noqa: BLE001 - capture failures must not break dispatch
            logger.warning("%s: history observation of an edit failed error=%s", _history.PLUGIN_NAME,
                           type(exc).__name__)
        return True
    if resolved.owner_user_id is None:
        return True
    message_id = _history._get(payload, "message_id")
    if not mark_seen((COMMAND_IDENTITY, str(conn), str(chat_id), f"message:{message_id}")):
        return True
    try:
        await _start_post(bot, payload, business_connection_id=str(conn), chat_id=chat_id,
                          owner_user_id=resolved.owner_user_id)
    except Exception as exc:  # noqa: BLE001 - the owner's command must never reach core
        logger.warning("%s: /dossier command failed error=%s", _history.PLUGIN_NAME, type(exc).__name__)
    return True
