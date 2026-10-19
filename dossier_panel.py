"""Telegram glue for dossiers: the owner panel section and the owner's ``/dossier`` in a Business chat.

Rendering lives in ``dossier_view`` (pure); this module loads snapshots, applies owner actions and
talks to the Bot API. Blocking work (files, history folds, cycles) runs in threads. Nothing but the
explicit owner ``/dossier`` post ever goes into a Business chat. Logs carry action names, counts,
hashed chat refs and error type names only, never message text, facts, quotes or names.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime
from typing import Any

from . import dossier_cli as _cli
from . import dossier_support as _ds
from . import dossier_view as _view
from . import history_support as _history
from . import transcription_modes as _modes

logger = logging.getLogger(__name__)

SECTION_KEY = "dossier"
SECTION_LABEL = "Dossier"
REFRESH_THREAD_NAME = "telesec-dossier-refresh"
COMMAND_IDENTITY = "dossier-command"
_EDIT_NOTICES = {"pin": "pinned", "unpin": "unpinned", "remove_do": "removed"}

_GLOBAL_ACTIONS = frozenset({"enable_ask", "enable_do", "disable"})

_REFRESH_LOCK = threading.Lock()
# Business connections the gateway saw in updates this process (set by register_owner_panel_section).
_seen_connections: Callable[[], Iterable[Any]] = tuple
# (dossier root, chat key) -> (refresh thread, its stop event)
_REFRESHES: dict[tuple[str, str], tuple[threading.Thread, threading.Event]] = {}


# ---------------------------------------------------------------------------
# Snapshots (blocking: callers run them with asyncio.to_thread)
# ---------------------------------------------------------------------------


def llm_ready(llm: Any = None) -> bool:
    llm = _ds._LLM if llm is None else llm
    return llm is not None and callable(getattr(llm, "complete_structured", None))


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
    stored contact dossiers, chats the persona cites, recorded bindings, and this process's lookups and
    Business updates. Owners
    come from this process's lookups, then the transcription modes bindings; an unknown owner, or a stored
    contact dossier whose connection is not among those, fails closed. Blocking."""
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
            persona = _ds.load_dossier(_ds.persona_path()) or {}
        except ValueError:  # corrupt: its screen shows only the reset command; the other sources still gate
            persona = {}
        connections |= {str(i["business_connection_id"]) for i in (persona.get("chat_aliases") or {}).values()
                        if isinstance(i, dict) and i.get("business_connection_id")}
        chats = _ds.dossier_root() / "chats"
        stored = {p.name for p in chats.iterdir() if p.is_dir()} if chats.is_dir() else set()
        if stored - {_history._path_key(c) for c in connections}:
            return False  # a contact dossier of a connection whose owner is unknown here
        return all((_history.cached_business_owner(c) or store.connection_owner(c)) == owner for c in connections)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.info("%s: dossier panel owner scope check failed error=%s", _history.PLUGIN_NAME,
                    type(exc).__name__)
        return False


def _common(cfg: _ds.DossierConfig, now: datetime, manage_global: bool) -> dict[str, Any]:
    return {"now": now, "enabled": cfg.enabled, "llm_ready": llm_ready(), "cycle_busy": _cycle_busy(),
            "provider": cfg.provider, "model": cfg.model, "backfill_days": cfg.backfill_days,
            "manage_global": manage_global, "history_enabled": _history.history_config_from_env().enabled}


def _sources(dossier: dict[str, Any], now: datetime, cfg: _ds.DossierConfig) -> dict[str, Any]:
    """stale + sources_now per fact; a failed history read still renders the stored dossier."""
    try:
        views = _ds.views_for_dossier(dossier, now=now, cfg=cfg)
        return {"stale": _ds.compute_stale(dossier, views),
                "sources_now": {str(f["id"]): _ds.sources_now(dossier, f, views) for f in dossier["facts"]}}
    except Exception as exc:  # noqa: BLE001 - the stored dossier must still render
        return {"stale_error": type(exc).__name__}


def _missing_reason(entry: dict[str, Any], cfg: _ds.DossierConfig, now: datetime) -> str | None:
    """'unqualified' or 'pending' from the same first-pass plan as ``build_preview`` (no payload)."""
    try:
        view = _ds.read_chat_view(entry, since=_ds.fold_since(None, None, now, cfg), missing_lock_ok=True)
        return "pending" if _ds.plan_contact(None, view, now=now, cfg=cfg).qualified else "unqualified"
    except Exception:  # noqa: BLE001 - shown as "next cycle"
        return None


def load_contact_snapshot(business_connection_id: Any, chat_id: Any, *, now: datetime | None = None,
                          manage_global: bool = False) -> _view.DossierSnapshot:
    """Everything the contact views need for one Business chat. Blocking."""
    now, cfg = now or _history._utcnow(), _ds.load_config()
    entry, private = contact_entry(business_connection_id, chat_id)
    key = _ds.chat_key(business_connection_id, chat_id)
    failure = _failure(_ds.load_state(), key)
    base = {**_common(cfg, now, manage_global), "kind": "contact", "failure_error": failure,
            "refreshing": refreshing(business_connection_id, chat_id), "in_history": private,
            "business_connection_id": str(business_connection_id), "chat_id": chat_id}
    try:
        dossier, corrupt = _ds.load_for_target(("contact", entry)), False
    except (OSError, ValueError):
        dossier, corrupt = None, True
    stored = ((dossier or {}).get("contact") or {}).get("display_name")
    label = _history._catalog_entry_display_name(entry) if private else str(stored or f"chat:{chat_id}")
    if corrupt:
        return _view.DossierSnapshot(label=label, dossier=None, status="corrupt", **base)
    if dossier is None:
        reason = None
        if cfg.enabled:
            reason = "failed" if failure else _missing_reason(entry, cfg, now)
        return _view.DossierSnapshot(label=label, dossier=None, status="missing", missing_reason=reason, **base)
    return _view.DossierSnapshot(label=label, dossier=dossier, **base, **_sources(dossier, now, cfg))


def _chat_names(dossier: dict[str, Any]) -> dict[str, str]:
    try:
        return _cli._persona_chat_names(dossier)
    except Exception:  # noqa: BLE001 - names are decoration
        return {}


def load_persona_snapshot(*, now: datetime | None = None) -> _view.DossierSnapshot:
    """Everything the persona views need. Blocking."""
    now, cfg = now or _history._utcnow(), _ds.load_config()
    failure = _failure(_ds.load_state(), "persona")
    base = {**_common(cfg, now, True), "kind": "persona", "label": "persona", "failure_error": failure}
    try:
        persona = _ds.load_dossier(_ds.persona_path())
    except (OSError, ValueError):
        return _view.DossierSnapshot(dossier=None, status="corrupt", **base)
    if persona is None:
        reason = ("failed" if failure else "pending") if cfg.enabled else None
        return _view.DossierSnapshot(dossier=None, status="missing", missing_reason=reason, **base)
    return _view.DossierSnapshot(dossier=persona, chat_names=_chat_names(persona), **base,
                                 **_sources(persona, now, cfg))


def load_snapshot(target: str, business_connection_id: Any, chat_id: Any, *, now: datetime | None = None,
                  manage_global: bool = False) -> _view.DossierSnapshot:
    if target == "persona":
        return load_persona_snapshot(now=now)
    return load_contact_snapshot(business_connection_id, chat_id, now=now, manage_global=manage_global)


# ---------------------------------------------------------------------------
# Refresh (``dossier run --contact`` for one chat, in the background)
# ---------------------------------------------------------------------------


def start_refresh(business_connection_id: Any, chat_id: Any) -> str:
    """Start a forced contact cycle in a thread: started | running | busy | unavailable. Blocking."""
    cfg, llm = _ds.load_config(), _ds._LLM
    entry, private = contact_entry(business_connection_id, chat_id)
    if not cfg.enabled or not llm_ready(llm) or not private:
        return "unavailable"
    key = _refresh_key(_ds.chat_key(business_connection_id, chat_id))
    with _REFRESH_LOCK:
        running = _REFRESHES.get(key)
        if running is not None and running[0].is_alive():
            return "running"
        if _cycle_busy():
            return "busy"
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


def join_refreshes(timeout: float | None = None) -> None:
    """Wait for running refresh threads (tests, shutdown)."""
    with _REFRESH_LOCK:
        threads = [thread for thread, _stop in _REFRESHES.values()]
    for thread in threads:
        thread.join(timeout)


def stop_background(timeout: float = 0.0) -> None:
    """on_unload: stop the gateway worker and every panel refresh (it ends a rate-limit wait and makes no
    further model call or write). Blocks only with a timeout."""
    _ds.stop_workers(timeout)
    with _REFRESH_LOCK:
        items = list(_REFRESHES.values())
    for _thread, stop in items:
        stop.set()
    if timeout:
        for thread, _stop in items:
            thread.join(timeout)


# ---------------------------------------------------------------------------
# Owner panel
# ---------------------------------------------------------------------------


def _switch(enabled: bool) -> None:
    _ds.set_enabled(enabled, via="telegram")
    if enabled:  # the worker runs whenever the facade is bound; a wake starts the first cycle sooner
        _ds.ensure_worker_started()
        _ds.wake_worker()


def _edit_target(target: str, business_connection_id: Any, chat_id: Any) -> tuple[str, Any]:
    return ("persona", None) if target == "persona" else ("contact", _stub(business_connection_id, chat_id))


async def handle_panel_action(*, owner_user_id: Any, business_connection_id: Any, chat_id: Any,
                              language_code: str | None, args: str | None) -> _view.View:
    """The dossier section of the owner panel: callback ``args`` ('' or None = overview) -> a view.

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
    action = (_view.parse_args(args) if args else None) or _view.Action("overview")  # malformed -> overview
    name, target = action.name, action.target
    manage_global = await asyncio.to_thread(manages_global, owner_user_id, conn)
    if not manage_global and (target == "persona" or name in _GLOBAL_ACTIONS):
        logger.info("%s: dossier panel %s refused target=%s reason=owner_scope", _history.PLUGIN_NAME, name, target)
        return _view.render_state("operator_only", lang)

    async def snapshot(kind: str = target) -> _view.DossierSnapshot:
        return await asyncio.to_thread(load_snapshot, kind, conn, chat_id, manage_global=manage_global)

    if name == "enable_ask":
        cfg = await asyncio.to_thread(_ds.load_config)
        return _view.render_enable_confirm(lang, cfg.provider, cfg.model)
    if name in ("enable_do", "disable"):
        await asyncio.to_thread(_switch, name == "enable_do")
        notice = _view.notice_text("enabled" if name == "enable_do" else "disabled", lang)
        return replace(_view.render_overview(await snapshot("contact"), lang), notice=notice)
    if name == "refresh":
        outcome = await asyncio.to_thread(start_refresh, conn, chat_id)
        if outcome == "running":
            notice = _view.notice_text("refresh_started", lang)
            return replace(_view.render_overview(await snapshot("contact"), lang), notice=notice)
        return _view.render_state({"started": "refresh_started"}.get(outcome, outcome), lang)
    if name in _EDIT_NOTICES:
        edit = "remove" if name == "remove_do" else name
        try:
            await asyncio.to_thread(_ds.apply_owner_edit, _edit_target(target, conn, chat_id), edit,
                                    fact_id=action.fact_id, expected_dossier_id=action.prefix)
        except ValueError as exc:  # DossierChanged, a fact gone since, or an unreadable file: no edit
            logger.info("%s: dossier panel %s refused target=%s error=%s", _history.PLUGIN_NAME, edit, target,
                        type(exc).__name__)
            return _view.render_state("changed", lang, target)
        logger.info("%s: dossier panel %s target=%s", _history.PLUGIN_NAME, edit, target)
        snap = await snapshot()
        view = (_view.render_fact_picker(snap, lang) if name == "remove_do"
                else _view.render_fact_card(snap, lang, str(action.fact_id)))
        return replace(view, notice=_view.notice_text(_EDIT_NOTICES[name], lang, fact_id=action.fact_id))
    snap = await snapshot()
    if name == "manage":
        return _view.render_fact_picker(snap, lang, action.page)
    if name == "fact":
        return _view.render_fact_card(snap, lang, str(action.fact_id), prefix=action.prefix)
    if name == "remove_ask":
        return _view.render_remove_confirm(snap, lang, str(action.fact_id), prefix=action.prefix)
    return _view.render_overview(snap, lang)  # overview (the panel adds its own Back row)


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


async def _tell_owner(bot: Any, owner_user_id: Any, view: _view.View) -> None:
    """The reason ``/dossier`` posted nothing, in the owner's private chat with the bot (never the Business chat)."""
    try:
        await bot.send_message(chat_id=_chat_id_value(owner_user_id), text=view.text, parse_mode=None)
    except Exception as exc:  # noqa: BLE001 - e.g. the owner never started the bot
        logger.warning("%s: could not send the /dossier reason to the owner error=%s", _history.PLUGIN_NAME,
                       type(exc).__name__)


async def _post_dossier(bot: Any, payload: Any, *, business_connection_id: str, chat_id: Any,
                        owner_user_id: Any) -> None:
    lang = _view.lang_for(_history._get(_history._get(payload, "from_user"), "language_code"))
    ref = _ds._log_ref(_stub(business_connection_id, chat_id))
    await _delete_command(bot, business_connection_id, _history._get(payload, "message_id"))
    snap = await asyncio.to_thread(load_contact_snapshot, business_connection_id, chat_id)
    reason, error = _view.post_blocker(snap), None
    if reason is None:
        view = _view.render_overview(snap, lang, audience="contact")
        try:
            await bot.do_api_request("sendRichMessage", api_kwargs={
                "business_connection_id": business_connection_id, "chat_id": chat_id,
                "rich_message": _view.rich_message(view)})
        except Exception as exc:  # noqa: BLE001 - told to the owner privately
            reason, error = "send_failed", type(exc).__name__
        else:
            logger.info("%s: dossier posted chat_ref=%s facts=%d", _history.PLUGIN_NAME, ref,
                        _view.contact_fact_count(snap))
            return
    logger.info("%s: dossier not posted chat_ref=%s reason=%s error=%s", _history.PLUGIN_NAME, ref, reason,
                error or "-")
    await _tell_owner(bot, owner_user_id, _view.render_post_reason(snap, lang, reason, error=error))


async def handle_dossier_command_update(update: Any, *, bot: Any, adapter: Any,
                                        mark_seen: Any) -> bool:
    """Execute an owner ``/dossier``; True means the update is claimed (never history, never core).

    The bot's own posted dossier (its echo) is claimed too. A contact's ``/dossier``, a forwarded
    one, or one whose owner cannot be verified is ordinary text. ``mark_seen(identity)`` returns
    False for a repeated delivery.
    """
    edited = getattr(update, "edited_business_message", None)
    payload = edited or getattr(update, "business_message", None)
    if payload is None:
        return False
    if _history.is_dossier_post(payload):
        return True
    text = _history._get(payload, "text")
    if not _history.is_dossier_control_message(payload, text):
        return False
    conn = _history._get(payload, "business_connection_id") or _history._get(payload, "_hermes_business_connection_id")
    chat = _history._get(payload, "chat")
    chat_id = _history._get(chat, "id")
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
        await _post_dossier(bot, payload, business_connection_id=str(conn), chat_id=chat_id,
                            owner_user_id=resolved.owner_user_id)
    except Exception as exc:  # noqa: BLE001 - the owner's command must never reach core
        logger.warning("%s: /dossier command failed error=%s", _history.PLUGIN_NAME, type(exc).__name__)
    return True
