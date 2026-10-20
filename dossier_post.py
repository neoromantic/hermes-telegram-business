"""The ``/dossier`` summary: a clean-room build from the live chat, made when the owner asks for it.

The contact reads the post, so it is built only from what this one chat shows now: its live text
messages with their latest text, plus the voice transcripts the chat still shows. It never reads the
private document, its previous version, the owner note, the persona, the worker state or any other
chat, so none of them can reach the post. Deleted and replaced messages and the text before an edit
Telesec recorded never go in. ``post_recheck`` runs again right before sending and refuses the post when an input message
changed, a dossier was forgotten or a switch went off meanwhile.

Stdlib only. Blocking: the panel runs these functions in threads. Logs carry codes, counts and
hashed chat refs only, never message or summary text.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import dossier_support as _ds
from . import dossier_writer as _writer
from . import history_support as _history

logger = logging.getLogger(__name__)

POST_TIMEOUT_SECONDS = 90.0  # per model call on an explicit route; the task route uses its own timeout
POST_WALL_SECONDS = 300  # precheck, build and recheck of one /dossier; a send that has started is never cut
POST_MAX_CHARS = 120_000  # sum(len(text) + 80) of the input messages, trimmed from the oldest
WINDOW_HOURS = 24  # Telegram lets a bot post only this long after the contact's last message
INBOUND_MAX = 10_000  # chats whose last contact message this process remembers

# (business connection id, chat id) -> when the contact's newest message this process saw was sent. Telegram's
# window counts any incoming message, but history keeps text only, so the Business handler notes voice, media,
# stickers and files here too (note_inbound). Times only, never text, and in memory only.
_INBOUND_LOCK = threading.Lock()
_LAST_INBOUND: dict[tuple[str, str], datetime] = {}
# The newest time the INBOUND_MAX cap dropped. While it is inside the window, a chat with no newer time noted
# may have lost its own, so its window is unknown.
_DROPPED_AT: datetime | None = None
# Since when _LAST_INBOUND has been noting, by time.monotonic(). Once that is more than WINDOW_HOURS ago, it has
# seen every message that can still keep a window open. A monotonic clock, so a wall clock set forward after the
# start (a machine that booted with the wrong time) never makes the process look older. Tests move it.
_STARTED_AT = time.monotonic()


@dataclass(frozen=True)
class PostResult:
    status: str  # ok | empty | failed
    text: str  # the summary Markdown when ok, else ""
    inputs: dict[str, str] = field(default_factory=dict)  # message id -> fingerprint of every input message
    error: str | None = None  # validator code or exception type name when failed
    provider: str | None = None  # what the host reports for the last call (it may have fallen back)
    model: str | None = None
    cost_usd: float | None = None


def _select(view: _ds.ChatView, now: datetime, cfg: _ds.DossierConfig) -> list[_ds.Msg]:
    """The input set: live messages with text inside the backfill window, newest first kept, time order."""
    cutoff = now - timedelta(days=cfg.backfill_days)
    live = sorted((m for m in view.messages.values() if not m.deleted and m.text.strip() and m.order_at >= cutoff),
                  key=_ds.sort_key)
    live = live[-cfg.backfill_messages:]
    chars = sum(len(m.text) + 80 for m in live)
    start = 0
    while chars > POST_MAX_CHARS:
        chars -= len(live[start].text) + 80
        start += 1
    return live[start:]


def _read(entry: dict[str, Any], now: datetime, cfg: _ds.DossierConfig) -> tuple[_ds.ChatView, list[_ds.Msg]]:
    view = _ds.read_chat_view(entry, since=now - timedelta(days=cfg.backfill_days) - _ds.FOLD_MARGIN)
    return view, _select(view, now, cfg)


def _qualified(inputs: list[_ds.Msg]) -> bool:
    # Like a first pass: a summary needs both sides of the chat.
    labels = {m.label for m in inputs}
    return "contact" in labels and "owner" in labels


def note_inbound(business_connection_id: Any, chat_id: Any, at: datetime) -> None:
    """The contact sent a new message of any kind at ``at``; an edit or a deletion is not one."""
    global _DROPPED_AT
    key = (str(business_connection_id), str(chat_id))
    with _INBOUND_LOCK:
        last = _LAST_INBOUND.pop(key, None)
        _LAST_INBOUND[key] = at if last is None else max(last, at)
        while len(_LAST_INBOUND) > INBOUND_MAX:
            dropped = _LAST_INBOUND.pop(next(iter(_LAST_INBOUND)))
            _DROPPED_AT = dropped if _DROPPED_AT is None else max(_DROPPED_AT, dropped)


def _window(view: _ds.ChatView, now: datetime) -> bool | None:
    """True when the contact wrote in the last WINDOW_HOURS, False when surely not, None when unknown.

    History has the contact's text messages, and this process notes their messages of any kind. A transcript
    never counts: it can be of the owner's own voice note.
    """
    since = now - timedelta(hours=WINDOW_HOURS)
    if any(m.label == "contact" and m.order_at >= since for m in view.messages.values()):
        return True
    with _INBOUND_LOCK:
        last = _LAST_INBOUND.get((str(view.entry.get("business_connection_id")), str(view.entry.get("chat_id"))))
        dropped = _DROPPED_AT
    if last is not None and last >= since:
        return True
    if dropped is not None and dropped >= since:
        return None  # the cap dropped a recent time, maybe this chat's
    return False if time.monotonic() - _STARTED_AT > WINDOW_HOURS * 3600 else None


def _check(entry: dict[str, Any], now: datetime, *, gateway: bool) -> tuple[str | None, list[_ds.Msg]]:
    """(the first refusal reason or None, the input set).

    ``gateway`` adds the checks only the gateway process can make: the bound host facade, and a window known
    to be closed (only the gateway sees the contact's voice and media messages).
    """
    cfg = _ds.load_config()
    if not cfg.enabled:
        return "disabled", []
    if gateway and not _ds._llm_ready(_ds._LLM):
        return "no_model", []
    if not _history.capture_enabled():
        return "history_off", []  # without capture no deletion is recorded, so a deleted text could go in
    if _ds.chat_paused(entry.get("business_connection_id"), entry.get("chat_id")):
        return "chat_off", []
    view, inputs = _read(entry, now, cfg)
    if gateway and _window(view, now) is False:
        return "window", inputs
    if not _qualified(inputs):
        return "empty", inputs
    return None, inputs


def _payload(inputs: list[_ds.Msg], now: datetime) -> str:
    today, zone = _ds.today_text(now)
    return _writer.post_input(today=today, messages=[_ds.as_wmsg(m, zone) for m in inputs])


def post_precheck(entry: dict[str, Any], *, now: datetime) -> str | None:
    """Why ``/dossier`` must post nothing, found before any model call; None when the build may start.

    "window" only when the contact surely has not written in the last WINDOW_HOURS; when that is unknown,
    the post is tried and Telegram decides.
    """
    return _check(entry, now, gateway=True)[0]


def post_window_open(entry: dict[str, Any], *, now: datetime) -> bool:
    """Is the contact known to have written in the last WINDOW_HOURS?

    When not, Telegram's refusal of the post is told to the owner as the closed window.
    """
    view = _ds.read_chat_view(entry, since=now - timedelta(hours=WINDOW_HOURS) - _ds.FOLD_MARGIN)
    return _window(view, now) is True


def post_inputs(entry: dict[str, Any], *, now: datetime) -> list[_ds.Msg]:
    """The messages a post built now would read, in time order."""
    return _read(entry, now, _ds.load_config())[1]


def post_payload(entry: dict[str, Any], *, now: datetime) -> tuple[str, str] | str:
    """(system prompt, user message) a post built now would send, or the reason it would be refused.

    For ``preview --post``: no model call and no writes. The CLI process has no host facade and never sees
    the contact's voice or media messages, so the ``no_model`` and ``window`` checks are left out.
    """
    reason, inputs = _check(entry, now, gateway=False)
    if reason is not None:
        return reason
    return _writer.POST_SYSTEM, _payload(inputs, now)


def build_post(entry: dict[str, Any], *, llm: Any, now: datetime,
               allowed: Callable[[], bool] | None = None) -> PostResult:
    """Write the summary of this chat from its live messages: one model call, plus one shorten call at most.

    It takes the chat and the model only: the private document, the note, the persona and the worker
    state are never read. Model and validation errors give status "failed" with their type name or code.
    When ``allowed()`` turns False (the post was stopped), no further model call is made.
    """
    inputs = _read(entry, now, _ds.load_config())[1]
    fps = {m.key: m.fp for m in inputs}
    if not _qualified(inputs):
        return PostResult("empty", "", fps)
    route = _ds.route()
    try:
        # A timeout of None lets Hermes read auxiliary.telesec_dossier.timeout on the task route.
        result = _writer.write(llm, kind="post", payload=_payload(inputs, now), mode="first", current=None,
                               route=route, timeout=None if route.task else POST_TIMEOUT_SECONDS, allowed=allowed)
    except _writer.OutputError as exc:
        return PostResult("failed", "", fps, exc.code)
    except Exception as exc:  # noqa: BLE001 - told to the owner by type, never by text
        return PostResult("failed", "", fps, type(exc).__name__)
    logger.info("%s: dossier call purpose=%s chat_ref=%s ms=%d in=%d out=%d", _history.PLUGIN_NAME,
                _writer.PURPOSE_POST, _ds._log_ref(entry), result.ms, result.input_tokens, result.output_tokens)
    if result.output.kind == "empty":
        return PostResult("empty", "", fps, None, result.provider, result.model, result.cost_usd)
    return PostResult("ok", result.output.text, fps, None, result.provider, result.model, result.cost_usd)


def post_recheck(entry: dict[str, Any], inputs: dict[str, str], *, gen0: int, now: datetime) -> str | None:
    """The final check right before sending: None, or why the built summary must not be posted.

    "changed" when an input message is gone, deleted or edited, or a dossier was forgotten since
    ``gen0``; otherwise the switch that went off.
    """
    cfg = _ds.load_config()
    view = _ds.read_chat_view(entry, since=now - timedelta(days=cfg.backfill_days) - _ds.FOLD_MARGIN)
    for key, fp in inputs.items():
        m = view.messages.get(key)
        if m is None or m.deleted or m.fp != fp:
            return "changed"
    if _ds.generation() != gen0:
        return "changed"
    if not cfg.enabled:
        return "disabled"
    if not _history.capture_enabled():
        return "history_off"
    if _ds.chat_paused(entry.get("business_connection_id"), entry.get("chat_id")):
        return "chat_off"
    return None
