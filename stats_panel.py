"""Owner-panel stats view: correspondence stats for one chat.

The view is shown only in the owner's private chat with the bot, never in the
Business chat with the contact. It is deterministic and read-only, and stores
nothing. Its body is the ``stats --chat`` CLI report for the same window, so
the panel and the CLI cannot drift. Like the CLI, it never shows message text.

``build_stats_view`` returns a panel-independent ``StatsPanelView``;
``render_section`` turns it into the owner panel's section view.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Attribute access only, like correspondence_stats: monkeypatches of history_support apply.
from . import correspondence_stats as _stats
from . import history_support as _history
from . import owner_panel as _owner_panel

logger = logging.getLogger(__name__)

_ICON = "\U0001f4ca"
SECTION_KEY = "stats"
SECTION_LABEL = f"{_ICON} Stats"
# (sub-argument, button label, header label, CLI --since value)
PERIODS: tuple[tuple[str, str, str, str | None], ...] = (
    ("7d", "7d", "last 7 days", "7d"),
    ("30d", "30d", "last 30 days", "30d"),
    ("90d", "90d", "last 90 days", "90d"),
    ("all", "All", "all history", None),
)
DEFAULT_PERIOD = "30d"
CURRENT_MARK = "• "
MAX_NAME_CHARS = 100
MAX_TEXT_UTF16 = 4096  # Telegram's message text limit, in UTF-16 code units.
_PERIODS = {period[0]: period for period in PERIODS}

HISTORY_DISABLED = f"{SECTION_LABEL}\nStats need history: set {_history.HISTORY_ENABLE_ENV}=1 and restart the gateway."
CONNECTION_UNKNOWN = f"{SECTION_LABEL}\nStats unavailable: the Business connection for this chat is not known yet."
BAD_TIMEZONE = (
    f"{SECTION_LABEL}\nStats unavailable: {_stats.TIMEZONE_ENV} is not a known time zone; "
    "use an IANA name such as Europe/Moscow."
)
NO_HISTORY = "No history for this chat yet. History records new text messages only."


@dataclass(frozen=True, slots=True)
class StatsPanelView:
    text: str
    # One row of period buttons as (label, sub-argument); empty when there is nothing to choose.
    buttons: tuple[tuple[str, str], ...] = ()


def _period(period: str | None) -> tuple[str, str, str, str | None]:
    # An unknown or missing sub-argument means the default period.
    return _PERIODS.get(period or "", _PERIODS[DEFAULT_PERIOD])


def period_window(period: str | None, *, now: datetime, environ: Mapping[str, str]) -> _stats.StatsWindow:
    """The CLI's window for ``--since <period>`` (or no --since for all history)."""
    args = argparse.Namespace(since=_period(period)[3], until=None, tz=None, session_gap=None)
    return _stats.build_window(args, now=now, environ=environ)


def period_buttons(period: str | None) -> tuple[tuple[str, str], ...]:
    # Tapping the current period re-renders it, so it doubles as refresh.
    current = _period(period)[0]
    return tuple((f"{CURRENT_MARK}{label}" if sub == current else label, sub) for sub, label, _, _ in PERIODS)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit(text: str) -> str:
    # Names are clipped and the body is short, so this only guards against surprises.
    if len(text.encode("utf-16-le")) // 2 <= MAX_TEXT_UTF16:
        return text
    head = text.encode("utf-16-le")[: (MAX_TEXT_UTF16 - 1) * 2]
    return head.decode("utf-16-le", errors="ignore") + "…"  # drops a split surrogate pair


def _label(text: str) -> str:
    return _clip(_stats._safe(text), MAX_NAME_CHARS)


def render_report(stats: _stats.ContactStats, window: _stats.StatsWindow, *, period: str | None) -> StatsPanelView:
    """The report for one chat: a header, the CLI report body, and when it was computed."""
    sub, _, header, _ = _period(period)
    tz = window.tz
    basis = f"{window.tz_label}, by Telegram send time; text messages only"
    if window.since is not None:
        since = _stats._bound_text(window.since, tz, end_of_day=False, missing="start of history")
        basis = f"since {since} {basis}"
    # The CLI lines minus the range and identity lines (the header replaces them)
    # and the CLI-only deletion details hint.
    body = [line.lstrip() for line in _stats.render_contact_text(stats, window)[2:]]
    lines = [
        f"{_ICON} {_label(stats.display_name)} · {header}",
        basis,
        *(line for line in body if not line.startswith("deletion details:")),
        f"as of {_stats._local_text(window.now, tz)}",
    ]
    return StatsPanelView(text=_fit("\n".join(lines)), buttons=period_buttons(sub))


def _read_error_text(chat_id: Any, business_connection_id: str) -> str:
    command = " ".join(
        (
            "hermes telegram-business stats",
            _stats._cli_option("--chat", chat_id),
            _stats._cli_option("--connection", business_connection_id),
        )
    )
    return (
        f"{SECTION_LABEL}\nStats unavailable: history could not be read. "
        f"For details, run on the gateway host:\n{_stats._safe(command)}"
    )


def build_stats_view(
    *,
    chat_id: Any,
    business_connection_id: str | None,
    chat_label: str | None = None,
    period: str | None = None,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> StatsPanelView:
    """Read history for one chat under one Business connection and render it.

    Blocking: run it off the event loop. Reads are scoped to ``business_connection_id``.
    """
    environ = os.environ if environ is None else environ
    sub = _period(period)[0]
    if not _history.history_config_from_env().enabled:
        return StatsPanelView(HISTORY_DISABLED)
    if not business_connection_id:
        return StatsPanelView(CONNECTION_UNKNOWN)
    try:
        _stats.resolve_timezone(None, environ)
    except ValueError:
        return StatsPanelView(BAD_TIMEZONE)
    window = period_window(sub, now=_history._utcnow() if now is None else now, environ=environ)
    try:
        entries = _history._filter_catalog_entries(
            business_connection_id=str(business_connection_id),
            chat_id=str(chat_id),
        )
        if not entries:
            label = _label(chat_label) if chat_label else f"chat {_stats._safe(str(chat_id))}"
            return StatsPanelView(f"{SECTION_LABEL}: {label}\n{NO_HISTORY}")
        if len(entries) > 1:
            # The catalog keys chats by (connection, chat), so two matches mean it is damaged.
            raise ValueError("ambiguous history chat")
        return render_report(_stats.collect_contact_stats(entries[0], window), window, period=sub)
    except (OSError, ValueError, OverflowError) as exc:
        # Exception text can carry contact names and paths, so only its type is logged.
        logger.warning("telegram-business stats panel: history could not be read (%s)", type(exc).__name__)
        return StatsPanelView(_read_error_text(chat_id, str(business_connection_id)), period_buttons(sub))


def render_section(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    """The owner panel's stats section; the panel verified ``ctx.business_connection_id``."""
    view = build_stats_view(
        chat_id=ctx.chat_id,
        business_connection_id=ctx.business_connection_id,
        chat_label=ctx.chat_label,
        period=ctx.arg,
    )
    row = [(label, _owner_panel.section_callback(ctx.chat_id, SECTION_KEY, sub)) for label, sub in view.buttons]
    return _owner_panel.PanelView(text=view.text, buttons=[row] if row else [])


def register_section() -> None:
    _owner_panel.register_section(SECTION_KEY, SECTION_LABEL, render_section, order=20)
