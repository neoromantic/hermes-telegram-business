"""The ``/dossier`` post: the contact's stored dossier, brought up to date first when the chat changed.

The contact reads the post, so it is the document body only: never the owner note, the persona, the
previous version or any other field, and ``dossier_view`` removes the refs. Before the post goes out, the
document must have seen every new message, edit and deletion history recorded and the current note.
When it has not, or there is no document yet, ``refresh`` runs one forced refresh of this contact, as the
panel's Refresh does; when that fails, nothing is posted. A message that arrives during that refresh does
not hold the post back, but an edit, a deletion or a note change does. ``post_state`` runs again right
before sending.

Stdlib only. Blocking: the panel runs these functions in threads. Logs carry codes, counts and
hashed chat refs only, never message or document text.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import dossier_doc as _doc
from . import dossier_support as _ds
from . import dossier_writer as _writer
from . import history_support as _history

logger = logging.getLogger(__name__)

POST_WALL_SECONDS = 300  # checks and the refresh of one /dossier; a send that has started is never cut
BUSY_RETRY_SECONDS = 2.0  # how often a post waiting for another dossier cycle tries again
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
# Contact outcomes of a forced refresh after which the document has seen the chat as the refresh read it.
_REFRESHED = frozenset({"refreshed", "checked", "unchanged"})


@dataclass(frozen=True)
class PostState:
    """What ``/dossier`` may post now.

    ``reason`` None: post ``text``, the stored document. "due": one refresh must run first. Anything else
    is why nothing may be posted, with ``error`` for "failed".
    """

    reason: str | None
    text: str = ""
    error: str | None = None
    pending: _ds.Pending | None = None  # what the document has not seen yet, when it was read


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


def _window(entry: dict[str, Any], now: datetime) -> bool | None:
    """True when the contact wrote in the last WINDOW_HOURS, False when surely not, None when unknown.

    History has the contact's text messages, and this process notes their messages of any kind. A transcript
    never counts: it can be of the owner's own voice note.
    """
    since = now - timedelta(hours=WINDOW_HOURS)
    view = _ds.read_chat_view(entry, since=since - _ds.FOLD_MARGIN)
    if any(m.label == "contact" and m.order_at >= since for m in view.messages.values()):
        return True
    with _INBOUND_LOCK:
        last = _LAST_INBOUND.get((str(entry.get("business_connection_id")), str(entry.get("chat_id"))))
        dropped = _DROPPED_AT
    if last is not None and last >= since:
        return True
    if dropped is not None and dropped >= since:
        return None  # the cap dropped a recent time, maybe this chat's
    return False if time.monotonic() - _STARTED_AT > WINDOW_HOURS * 3600 else None


def _switches(entry: dict[str, Any]) -> str | None:
    if not _ds.load_config().enabled:
        return "disabled"
    if not _history.capture_enabled():
        return "history_off"  # without capture no deletion is recorded, so the document could not drop one
    if _ds.chat_paused(entry.get("business_connection_id"), entry.get("chat_id")):
        return "chat_off"
    return None


def post_precheck(entry: dict[str, Any], *, now: datetime) -> str | None:
    """Why ``/dossier`` must post nothing, found before the document is read; None when it may go on.

    "window" only when the contact surely has not written in the last WINDOW_HOURS; when that is unknown,
    the post is tried and Telegram decides.
    """
    reason = _switches(entry)
    if reason is None and _window(entry, now) is False:
        return "window"
    return reason


def post_window_open(entry: dict[str, Any], *, now: datetime) -> bool:
    """Is the contact known to have written in the last WINDOW_HOURS?

    When not, Telegram's refusal of the post is told to the owner as the closed window.
    """
    return _window(entry, now) is True


def post_state(entry: dict[str, Any], *, now: datetime, refreshed: bool = False) -> PostState:
    """The stored document when it may be posted as it is, "due" when one refresh must run first, or why not.

    Reads the switches, the document and history; no model call and no writes. ``refreshed`` is True after
    this post's refresh: new messages that came during it no longer hold the post back, but an edit, a
    deletion or a note change still does ("changed"), and so does a document forgotten meanwhile.
    """
    reason = _switches(entry)
    if reason is not None:
        return PostState(reason)
    try:
        doc, _view, plan = _ds._plan_for(entry, now, _ds.load_config())
    except _doc.DocError:
        return PostState("failed", error="corrupt")
    pending = _ds._pending_of(plan)
    if plan.first_pass:  # no document yet, or only the one converted from v1, which no refresh has checked
        if not plan.qualified:
            return PostState("unqualified", pending=pending)
        return PostState("changed" if refreshed else "due", pending=pending)
    if plan.edited or plan.deleted or plan.pruned or plan.note_changed:
        return PostState("changed" if refreshed else "due", pending=pending)
    if plan.new and not refreshed:
        return PostState("due", pending=pending)
    if not _writer.strip_refs(doc["document"]):
        return PostState("empty", pending=pending)
    return PostState(None, doc["document"], pending=pending)


def _refresh_reason(entry: dict[str, Any], rep: _ds.CycleReport) -> tuple[str | None, str | None]:
    if rep.outcome in ("disabled", "history_off", "chat_off"):
        return rep.outcome, None
    if rep.outcome == "aborted_trust":
        return "failed", "trust_denied"
    if rep.contact in _REFRESHED:
        return None, None
    if rep.contact == "unqualified":
        return "unqualified", None
    if rep.contact == "discarded":
        return "changed", None  # forgotten during the refresh
    if rep.contact == "paused":
        return "chat_off", None
    if rep.contact == "failed":
        return "failed", rep.failures[-1][1] if rep.failures else None
    if "stopped" in (rep.contact, rep.outcome):
        reason = _switches(entry)  # dossiers or history went off during the refresh
        return (reason, None) if reason is not None else ("failed", "stopped")
    return "failed", rep.contact or rep.outcome


def refresh(entry: dict[str, Any], *, llm: Any, stop: threading.Event) -> tuple[str | None, str | None]:
    """One forced refresh of this contact, like the panel's Refresh: (None, None) once it succeeded, else
    (reason, error).

    While another dossier cycle runs, it tries again every BUSY_RETRY_SECONDS until ``stop`` is set. Once
    ``stop`` is set, the refresh starts no further model call; a write already under way may still finish.
    """
    started = time.monotonic()
    while True:
        rep = _ds.run_cycle(llm=llm, trigger="post", target=("contact", entry), stop=stop)
        if rep.outcome != "busy":
            break
        if stop.wait(BUSY_RETRY_SECONDS):
            return "failed", "stopped"
    logger.info("%s: dossier post refresh chat_ref=%s outcome=%s calls=%d duration_ms=%d", _history.PLUGIN_NAME,
                _ds._log_ref(entry), rep.contact or rep.outcome, rep.calls, int((time.monotonic() - started) * 1000))
    return _refresh_reason(entry, rep)
