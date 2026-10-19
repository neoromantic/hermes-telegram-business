"""Owner-panel stats view: correspondence stats for one chat.

The view is shown only in the owner's private chat with the bot, never in the
Business chat with the contact. It is deterministic and read-only, and stores
nothing. It computes the same stats as ``stats --chat`` for the same window
and shows them as a Bot API rich message (``stats_render.panel``) with a
plain-text fallback, so the panel and the CLI cannot drift. Like the CLI, it
never shows message text. Its error states are short plain-text views.

``build_stats_view`` returns a panel-independent ``StatsPanelView``;
``render_section`` turns it into the owner panel's section view.

``build_stats_card`` is the in-chat ``/stats`` command's public card
(``stats_render.card``, also a rich message with a plain-text fallback) over the
same window rules. Unlike the panel, the card is posted into the Business chat,
so the contact sees it too; its error states are short notices without details.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

# Attribute access only, like correspondence_stats: monkeypatches of history_support apply.
from . import chat_settings as _chat_settings
from . import correspondence_stats as _stats
from . import history_support as _history
from . import owner_panel as _owner_panel
from . import stats_render as _render

logger = logging.getLogger(__name__)

_ICON = "\U0001f4ca"
SECTION_KEY = "stats"
SECTION_LABEL = f"{_ICON} Stats"
# (sub-argument, local days in the period; None for all history)
PERIODS: tuple[tuple[str, int | None], ...] = (("7d", 7), ("30d", 30), ("90d", 90), ("all", None))
DEFAULT_PERIOD = "30d"
CURRENT_MARK = "• "
MAX_LABEL_CHARS = _render.TITLE_NAME_CHARS
_PERIODS = dict(PERIODS)

# Panel strings: plain text. The rich report escapes the paused note itself.
STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "stats.title": SECTION_LABEL,
        "stats.button.7d": "7d",
        "stats.button.30d": "30d",
        "stats.button.90d": "90d",
        "stats.button.all": "All",
        "stats.chat": "chat {chat}",
        "stats.history_disabled": "History is off. Turn it on in Settings (/telesec).",
        "stats.connection_unknown": "The Business connection for this chat is not known yet. "
        "Send any message in the chat and try again.",
        "stats.no_history": "No history for this chat yet. History records new text messages only.",
        "stats.history_paused": "History is paused for this chat.",
        "stats.read_error": "History could not be read. For details, run on the gateway host:",
        # In-chat `/stats` notices: plain text, seen by the contact too, so no details.
        "stats.card.owner": "Owner",
        "stats.card.history_disabled": f"{_ICON} Stats are unavailable: history is off.",
        "stats.card.no_history": f"{_ICON} No history for this chat yet.",
        "stats.card.read_error": f"{_ICON} Stats are unavailable: history could not be read.",
    },
    "ru": {
        "stats.title": f"{_ICON} Статистика",
        "stats.button.7d": "7 дн.",
        "stats.button.30d": "30 дн.",
        "stats.button.90d": "90 дн.",
        "stats.button.all": "Всё",
        "stats.chat": "чат {chat}",
        "stats.history_disabled": "История выключена. Включите её в настройках (/telesec).",
        "stats.connection_unknown": "Business-подключение этого чата пока неизвестно. "
        "Отправьте любое сообщение в этом чате и попробуйте снова.",
        "stats.no_history": "Истории этого чата пока нет. История записывает только новые текстовые сообщения.",
        "stats.history_paused": "История этого чата сейчас не сохраняется.",
        "stats.read_error": "Историю не удалось прочитать. Подробности — на хосте шлюза:",
        "stats.card.owner": "Владелец",
        "stats.card.history_disabled": f"{_ICON} Статистика недоступна: история выключена.",
        "stats.card.no_history": f"{_ICON} Истории этого чата пока нет.",
        "stats.card.read_error": f"{_ICON} Статистика недоступна: историю не удалось прочитать.",
    },
}
# The section button; registered with the section, so other "stats" sections keep their own label.
SECTION_STRINGS = {"en": {"section.stats": SECTION_LABEL}, "ru": {"section.stats": STRINGS["ru"]["stats.title"]}}
_owner_panel.register_strings(STRINGS)


@dataclass(frozen=True, slots=True)
class StatsPanelView:
    text: str  # plain text: the report's fallback, or a short state
    # One row of period buttons as (label, sub-argument); empty when there is nothing to choose.
    buttons: tuple[tuple[str, str], ...] = ()
    html: str | None = None  # the report as rich message HTML; None for a state


def _period(period: str | None) -> str:
    # An unknown or missing sub-argument means the default period.
    return period if period in _PERIODS else DEFAULT_PERIOD


def period_window(period: str | None, *, now: datetime) -> _stats.StatsWindow:
    """The last N local days with today (or all history), in Hermes's time zone, running to now.

    Whole days, so "7d" shows 7 days, not 7 x 24 hours over 8 dates: the CLI's
    window for ``--since <date>`` with the first of those days.
    """
    window = _stats.build_window(argparse.Namespace(since=None, until=None, tz=None, session_gap=None), now=now)
    days = _PERIODS[_period(period)]
    if days is None:
        return window
    first = window.now.astimezone(window.tz).date() - timedelta(days=days - 1)
    return replace(window, since=_stats._local_day_start(first, window.tz))


def period_buttons(period: str | None, language_code: str | None = None) -> tuple[tuple[str, str], ...]:
    # Tapping the current period re-renders it, so it doubles as refresh.
    current = _period(period)
    return tuple(
        (("" if sub != current else CURRENT_MARK) + _owner_panel.tr(f"stats.button.{sub}", language_code), sub)
        for sub, _ in PERIODS
    )


def _label(text: str) -> str:
    return _render.safe_name(text, MAX_LABEL_CHARS)


def _state(message: str, language_code: str | None, title: str | None = None, note: str | None = None) -> str:
    # Plain text: ``message`` and ``note`` are catalog strings; ``title`` went through safe_name.
    heading = _owner_panel.tr("stats.title", language_code) if title is None else f"{_ICON} {title}"
    text = f"{heading}\n{message}"
    return text if note is None else f"{text}\n{note}"


def render_report(
    stats: _stats.ContactStats,
    window: _stats.StatsWindow,
    *,
    period: str | None,
    language_code: str | None = None,
    contact_name: str | None = None,
    note: str | None = None,
) -> StatsPanelView:
    """The report for one chat, with the period buttons; ``note`` is a plain catalog line kept last."""
    sub = _period(period)
    view = _render.panel(stats, window, period=sub, language_code=language_code, contact_name=contact_name, note=note)
    return StatsPanelView(text=view.text, buttons=period_buttons(sub, language_code), html=view.html)


def _first_name(entry: Mapping[str, Any]) -> str | None:
    profile = entry.get("current_profile")
    return _history._clean_optional_text(profile.get("first_name")) if isinstance(profile, dict) else None


def _read_error_text(
    chat_id: Any, business_connection_id: str, language_code: str | None, note: str | None = None
) -> str:
    command = " ".join(
        (
            "hermes telesec stats",
            _stats._cli_option("--chat", chat_id),
            _stats._cli_option("--connection", business_connection_id),
        )
    )
    message = f"{_owner_panel.tr('stats.read_error', language_code)}\n{_stats._safe(command)}"
    return _state(message, language_code, note=note)


def build_stats_view(
    *,
    chat_id: Any,
    business_connection_id: str | None,
    chat_label: str | None = None,
    period: str | None = None,
    now: datetime | None = None,
    language_code: str | None = None,
    history_paused: bool = False,
) -> StatsPanelView:
    """Read history for one chat under one Business connection and render it.

    Blocking: run it off the event loop. Reads are scoped to ``business_connection_id``.
    ``history_paused`` means the chat is set to Don't save: the saved data still
    shows, and every state but history off ends with a line that says so.
    """
    lang = language_code
    sub = _period(period)
    if not _history.history_config_from_env().enabled:
        return StatsPanelView(_state(_owner_panel.tr("stats.history_disabled", lang), lang))
    note = _owner_panel.tr("stats.history_paused", lang) if history_paused else None
    if not business_connection_id:
        return StatsPanelView(_state(_owner_panel.tr("stats.connection_unknown", lang), lang, note=note))
    window = period_window(sub, now=_history._utcnow() if now is None else now)
    try:
        entries = _history._filter_catalog_entries(
            business_connection_id=str(business_connection_id),
            chat_id=str(chat_id),
        )
        if not entries:
            label = _label(chat_label) if chat_label else _owner_panel.tr("stats.chat", lang, chat=_label(str(chat_id)))
            return StatsPanelView(_state(_owner_panel.tr("stats.no_history", lang), lang, title=label, note=note))
        if len(entries) > 1:
            # The catalog keys chats by (connection, chat), so two matches mean it is damaged.
            raise ValueError("ambiguous history chat")
        stats = _stats.collect_contact_stats(entries[0], window)
        return render_report(
            stats, window, period=sub, language_code=lang, contact_name=_first_name(entries[0]), note=note
        )
    except (OSError, ValueError, OverflowError) as exc:
        # Exception text can carry contact names and paths, so only its type is logged.
        logger.warning("telesec stats panel: history could not be read (%s)", type(exc).__name__)
        return StatsPanelView(
            _read_error_text(chat_id, str(business_connection_id), lang, note), period_buttons(sub, lang)
        )


@dataclass(frozen=True, slots=True)
class StatsCard:
    # The card's plain-text fallback, or a short plain-text notice (history off,
    # no history, read error) that the command deletes again after a few seconds.
    text: str
    # True: the card, with ``html`` set. False: a notice.
    ok: bool
    html: str | None = None  # the card as rich message HTML


CARD_DEFAULT_PERIOD = "all"


def build_stats_card(
    *,
    chat_id: Any,
    business_connection_id: str,
    period: str | None = None,
    owner_name: str | None = None,
    contact_name: str | None = None,
    now: datetime | None = None,
    language_code: str | None = None,
) -> StatsCard:
    """The public card for the in-chat ``/stats`` command, or a short notice.

    Blocking: run it off the event loop. Same window rules and read scope as the
    panel (``build_stats_view``), but ``/stats`` without a period means all
    history. ``owner_name`` and ``contact_name`` are first names; without them
    the card uses the contact's name from history and a generic owner label.
    A chat set to Don't save still gets a card over what was saved, without the
    panel's paused line: the contact sees the card.
    """
    lang = language_code
    sub = period if period in _PERIODS else CARD_DEFAULT_PERIOD
    if not _history.history_config_from_env().enabled:
        return StatsCard(_owner_panel.tr("stats.card.history_disabled", lang), ok=False)
    window = period_window(sub, now=_history._utcnow() if now is None else now)
    try:
        entries = _history._filter_catalog_entries(
            business_connection_id=str(business_connection_id),
            chat_id=str(chat_id),
        )
        if not entries:
            return StatsCard(_owner_panel.tr("stats.card.no_history", lang), ok=False)
        if len(entries) > 1:
            # The catalog keys chats by (connection, chat), so two matches mean it is damaged.
            raise ValueError("ambiguous history chat")
        stats = _stats.collect_contact_stats(entries[0], window)
    except (OSError, ValueError, OverflowError) as exc:
        # Exception text can carry contact names and paths, so only its type is logged.
        logger.warning("telesec stats command: history could not be read (%s)", type(exc).__name__)
        return StatsCard(_owner_panel.tr("stats.card.read_error", lang), ok=False)
    view = _render.card(
        stats,
        window,
        period=sub,
        owner_name=owner_name or _owner_panel.tr("stats.card.owner", lang),
        contact_name=contact_name or _first_name(entries[0]) or stats.name or stats.display_name,
        language_code=lang,
    )
    return StatsCard(view.text, ok=True, html=view.html)


def render_section(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    """The owner panel's stats section; the panel verified ``ctx.business_connection_id``."""
    view = build_stats_view(
        chat_id=ctx.chat_id,
        business_connection_id=ctx.business_connection_id,
        chat_label=ctx.chat_label,
        period=ctx.arg,
        language_code=ctx.language_code,
        # "Don't save" in this chat's panel (fails closed like capture).
        history_paused=_chat_settings.default_store().is_off(ctx.owner_user_id, ctx.chat_id, "history"),
    )
    row = [(label, _owner_panel.section_callback(ctx.chat_id, SECTION_KEY, sub)) for label, sub in view.buttons]
    # The router sends ``html`` as a rich message, and ``text`` when Telegram rejects it.
    return _owner_panel.PanelView(text=view.text, buttons=[row] if row else [], html=view.html)


def register_section() -> None:
    _owner_panel.register_strings(SECTION_STRINGS)
    _owner_panel.register_section(SECTION_KEY, SECTION_LABEL, render_section, order=20)
