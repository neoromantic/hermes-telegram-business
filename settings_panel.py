"""Owner-panel Settings: the switches for transcription, history and dossiers in all chats.

Settings opens from the Settings button on any chat's panel, or with `/telesec` in the owner's
private chat with the bot (chat ``owner_panel.HOME_CHAT``, with no Back row). Only an owner of every
Business connection the plugin knows may change the switches and history retention
(``dossier_panel.manages_global``); other verified owners see them read-only. Buttons carry target
values, not toggles, so a stale or repeated tap changes nothing. The models in use are shown, never
changed, here. Nothing here is ever posted into a Business chat.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from . import dossier_panel as _dossier_panel
from . import dossier_support as _ds
from . import dossier_view as _dossier_view
from . import global_settings as _settings
from . import history_support as _history
from . import owner_panel as _owner_panel

logger = logging.getLogger(__name__)

SECTION_KEY = _owner_panel.SETTINGS_SECTION
SECTION_LABEL = "⚙️ Settings"
ORDER = 900  # after every chat section
# "<feature>.<target value>"; "d.ask" opens the dossier confirm, since dossiers send chat text to a model,
# and only the confirm's "d.yes" turns them on.
SWITCH_ARGS = ("t.on", "t.off", "h.on", "h.off", "d.ask", "d.yes", "d.on", "d.off")
# "r.<days>" keeps history that many days (0: forever). A shorter value than the one in effect opens a
# confirm, and only its "r.<days>.yes" applies it. A value outside the choices (a migrated one) is
# accepted only while it is in effect, so a forged button cannot store an arbitrary number.
RETENTION_ARGS = (*(f"r.{days}" for days in _settings.RETENTION_CHOICES),
                  *(f"r.{days}.yes" for days in _settings.RETENTION_CHOICES if days))
ARGS = (*SWITCH_ARGS, *RETENTION_ARGS)
_FEATURES = {"t": "transcription", "h": "history", "d": "dossiers"}
_RETENTION_ARG_RE = re.compile(r"^r\.(\d{1,5})(\.yes)?\Z")
CURRENT_MARK = "• "
MODEL_NAME_MAX = 120

STRINGS = {
    "en": {
        f"section.{SECTION_KEY}": SECTION_LABEL,
        # Never "Owner panel:": the agent treats a reply to that as a question about one chat.
        "settings.title": "Telesec settings · all chats",
        "settings.one_chat": "One chat: Manage Bot in that chat.",
        "settings.transcription.on": "Transcribe",
        "settings.transcription.off": "Don't transcribe",
        "settings.history.on": "Save history",
        "settings.history.off": "Don't save",
        "settings.dossiers.on": "Keep dossiers",
        "settings.dossiers.off": "No dossiers",
        "settings.done.t.on": "Transcription is on.",
        "settings.done.t.off": "Transcription is off in all chats.",
        "settings.done.h.on": "History is on.",
        "settings.done.h.off": "History is off in all chats.",
        "settings.done.d.on": "Dossiers are on.",
        "settings.done.d.off": "Dossiers are off in all chats.",
        "settings.needs_history": "Dossiers need history. Turn history on first.",
        "settings.paused": "Dossiers are paused while history is off.",
        "settings.save_error": "Couldn't save: the settings file is unreadable (see hermes telesec settings).",
        "settings.status.transcription": "Transcription: {state}",
        "settings.status.history": "History: {state}",
        "settings.status.dossiers": "Dossiers: {state}",
        "settings.state.on": "on",
        "settings.state.off": "off",
        "settings.read_only": "Only the owner of every connected account can change these.",
        "settings.retention.forever": "forever",
        "settings.retention.forever_button": "Forever",
        "settings.retention.year": "1 year",
        "settings.retention.day": "{days} day",
        "settings.retention.few": "{days} days",
        "settings.retention.days": "{days} days",
        "settings.status.retention": "Keep history: {value}",
        "settings.retention.ask": "Keep {value}?",
        "settings.retention.explain": ("Saved messages are deleted a whole month at a time: a month is deleted at "
                                       "the next cleanup once all of it is older than that."),
        "settings.retention.yes": "Keep {value}",
        "settings.retention.cancel": "Cancel",
        "settings.retention.done": "History is kept {value}.",
        "settings.model.dossiers": "Dossier model: {name}",
        "settings.model.cleanup": "Voice cleanup: {name}",
    },
    "ru": {
        f"section.{SECTION_KEY}": "⚙️ Настройки",
        "settings.title": "Настройки Telesec · все чаты",
        "settings.one_chat": "Для одного чата — кнопка управления ботом в нём.",
        "settings.transcription.on": "Расшифровывать",
        "settings.transcription.off": "Не расшифровывать",
        "settings.history.on": "Сохранять историю",
        "settings.history.off": "Не сохранять",
        "settings.dossiers.on": "Вести досье",
        "settings.dossiers.off": "Не вести",
        "settings.done.t.on": "Расшифровка включена.",
        "settings.done.t.off": "Расшифровка выключена во всех чатах.",
        "settings.done.h.on": "История включена.",
        "settings.done.h.off": "История выключена во всех чатах.",
        "settings.done.d.on": "Досье включены.",
        "settings.done.d.off": "Досье выключены во всех чатах.",
        "settings.needs_history": "Для досье нужна история. Сначала включите её.",
        "settings.paused": "Досье на паузе, пока история выключена.",
        "settings.save_error": "Не удалось сохранить: файл настроек не читается (см. hermes telesec settings).",
        "settings.status.transcription": "Расшифровка: {state}",
        "settings.status.history": "История: {state}",
        "settings.status.dossiers": "Досье: {state}",
        "settings.state.on": "вкл",
        "settings.state.off": "выкл",
        "settings.read_only": "Менять их может только владелец всех подключённых аккаунтов.",
        "settings.retention.forever": "всегда",
        "settings.retention.forever_button": "Всегда",
        "settings.retention.year": "1 год",
        "settings.retention.day": "{days} день",
        "settings.retention.few": "{days} дня",
        "settings.retention.days": "{days} дней",
        "settings.status.retention": "Хранить историю: {value}",
        "settings.retention.ask": "Хранить {value}?",
        "settings.retention.explain": ("Сохранённые сообщения удаляются целыми месяцами: месяц удаляется при "
                                       "ближайшей очистке, когда весь он старше этого срока."),
        "settings.retention.yes": "Хранить {value}",
        "settings.retention.cancel": "Отмена",
        "settings.retention.done": "История хранится {value}.",
        "settings.model.dossiers": "Модель досье: {name}",
        "settings.model.cleanup": "Очистка расшифровок: {name}",
    },
}


# The voice cleanup model as (provider, model), or None while cleanup is off; bound by register_section,
# since the plugin module owns that configuration.
_voice_cleanup: Callable[[], tuple[str, str] | None] | None = None


def _tr(key: str, ctx: _owner_panel.PanelContext, **values: Any) -> str:
    return _owner_panel.tr(key, ctx.language_code, **values)


def _days_key(days: int, lang: str) -> str:
    """The plural form: English has day/days, Russian день/дня/дней (1, 21 / 2-4, 22 / 5-20, 25)."""
    if lang != "ru":
        return "day" if days == 1 else "days"
    if days % 10 == 1 and days % 100 != 11:
        return "day"
    if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
        return "few"
    return "days"


def _retention_value(ctx: _owner_panel.PanelContext, days: int) -> str:
    if days == 0:
        return _tr("settings.retention.forever", ctx)
    if days == 365:
        return _tr("settings.retention.year", ctx)
    key = _days_key(days, _owner_panel.language(ctx.language_code))
    return _tr(f"settings.retention.{key}", ctx, days=days)


def _without_credentials(value: str) -> str:
    """``value`` without user info (up to the last "@", so an "@" in a password is covered), query or fragment.

    With a scheme any "@" ends user info; without one only "name:secret@", so "model@version" stays.
    """
    scheme, sep, rest = value.strip().partition("://")
    if not sep:
        scheme, rest = "", scheme
    userinfo, at, host = rest.rpartition("@")
    if at and (sep or ":" in userinfo):
        rest = host
    return re.split(r"[?#]", scheme + sep + rest, maxsplit=1)[0].strip()


def _model_name(provider: str, model: str) -> str:
    """``provider / model`` as configured, without URL credentials, queries or fragments."""
    parts = [_without_credentials(str(part)) for part in (provider, model)]
    return _ds.clip(" / ".join(part for part in parts if part) or "?", MODEL_NAME_MAX)


def _model_lines(ctx: _owner_panel.PanelContext) -> list[str]:
    cfg = _ds.load_config()
    lines = [_tr("settings.model.dossiers", ctx, name=_model_name(cfg.provider, cfg.model))]
    if _voice_cleanup is not None:
        cleanup = _voice_cleanup()
        name = _model_name(*cleanup) if cleanup else _tr("settings.state.off", ctx)
        lines.append(_tr("settings.model.cleanup", ctx, name=name))
    return lines


def _callback(ctx: _owner_panel.PanelContext, sub: str | None = None) -> str:
    return _owner_panel.section_callback(ctx.chat_id, SECTION_KEY, sub)


def _apply(ctx: _owner_panel.PanelContext, arg: str) -> str:
    """Write one switch; returns the notice line. Blocking."""
    feature, on = _FEATURES[arg[0]], arg.endswith(".on")
    if feature == "dossiers" and on and not _settings.enabled("history"):
        return _tr("settings.needs_history", ctx)
    try:
        if feature == "dossiers":
            _dossier_panel.switch(on)
        else:
            _settings.set_enabled(feature, on, via="telegram")
            if feature == "history" and on and _settings.enabled("dossiers"):
                _dossier_panel.wake()  # paused dossiers resume without waiting for the next poll
    except (_settings.SettingsFileCorrupt, OSError) as exc:
        # Telegram never overwrites a corrupt file; `hermes telesec settings set` repairs it.
        logger.warning("%s: could not save settings (%s)", _history.PLUGIN_NAME, type(exc).__name__)
        return _tr("settings.save_error", ctx)
    return _tr(f"settings.done.{arg}", ctx)


def _retention_target(arg: str) -> tuple[int, bool] | None:
    """(days, confirmed) of a retention arg, else None."""
    match = _RETENTION_ARG_RE.match(arg)
    return None if match is None else (int(match.group(1)), match.group(2) is not None)


def _apply_retention(ctx: _owner_panel.PanelContext, days: int) -> str:
    """Store the retention; returns the notice line. Blocking."""
    try:
        _settings.set_retention(days, via="telegram")
    except (_settings.SettingsFileCorrupt, OSError) as exc:
        logger.warning("%s: could not save settings (%s)", _history.PLUGIN_NAME, type(exc).__name__)
        return _tr("settings.save_error", ctx)
    return _tr("settings.retention.done", ctx, value=_retention_value(ctx, days))


def _retention_row(ctx: _owner_panel.PanelContext, current: int) -> list[tuple[str, str]]:
    """Forever first, then shorter and shorter; a value in effect outside the choices gets its own button."""
    choices = sorted({*_settings.RETENTION_CHOICES, current}, key=lambda days: -days if days else float("-inf"))
    row = []
    for days in choices:
        # Short labels, like the stats periods: four or five buttons share one row on a phone.
        label = _tr("settings.retention.forever_button", ctx) if days == 0 else _retention_value(ctx, days)
        row.append(((CURRENT_MARK if days == current else "") + label, _callback(ctx, f"r.{days}")))
    return row


def _choice_row(ctx: _owner_panel.PanelContext, feature: str, on: bool, on_arg: str) -> list[tuple[str, str]]:
    letter = feature[0]
    return [
        ((CURRENT_MARK if on else "") + _tr(f"settings.{feature}.on", ctx), _callback(ctx, on_arg)),
        ((CURRENT_MARK if not on else "") + _tr(f"settings.{feature}.off", ctx), _callback(ctx, f"{letter}.off")),
    ]


def _main_view(ctx: _owner_panel.PanelContext, manager: bool, notice: str | None) -> _owner_panel.PanelView:
    current = {feature: _settings.enabled(feature) for feature in _settings.FEATURES}
    retention = _settings.retention_days()
    lines = [_tr("settings.title", ctx)]
    if notice:
        lines.append(notice)
    if manager and ctx.chat_id == str(_owner_panel.HOME_CHAT):
        lines.append(_tr("settings.one_chat", ctx))
    if current["dossiers"] and not current["history"]:
        lines.append(_tr("settings.paused", ctx))
    keep = _tr("settings.status.retention", ctx, value=_retention_value(ctx, retention))
    if not manager:
        for feature in _settings.FEATURES:
            state = _tr(f"settings.state.{'on' if current[feature] else 'off'}", ctx)
            lines.append(_tr(f"settings.status.{feature}", ctx, state=state))
            if feature == "history":
                lines.append(keep)
        lines += _model_lines(ctx)
        lines.append(_tr("settings.read_only", ctx))
        return _owner_panel.PanelView(text="\n".join(lines))
    lines.append(keep)  # the value in effect stays readable when a narrow screen cuts the buttons
    lines += _model_lines(ctx)
    buttons = [
        _choice_row(ctx, "transcription", current["transcription"], "t.on"),
        _choice_row(ctx, "history", current["history"], "h.on"),
        # Shown while history is off too: maintenance keeps pruning what was saved.
        _retention_row(ctx, retention),
        # Always shown, so dossiers can always be turned off; turning them on asks first.
        _choice_row(ctx, "dossiers", current["dossiers"], "d.on" if current["dossiers"] else "d.ask"),
    ]
    return _owner_panel.PanelView(text="\n".join(lines), buttons=buttons)


def _confirm_view(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    cfg = _ds.load_config()
    text, yes, cancel = _dossier_view.enable_confirm(_dossier_view.lang_for(ctx.language_code), cfg.provider,
                                                     cfg.model)
    return _owner_panel.PanelView(text=text, buttons=[[(yes, _callback(ctx, "d.yes")), (cancel, _callback(ctx))]])


def _retention_confirm_view(ctx: _owner_panel.PanelContext, days: int) -> _owner_panel.PanelView:
    value = _retention_value(ctx, days)
    text = f"{_tr('settings.retention.ask', ctx, value=value)}\n\n{_tr('settings.retention.explain', ctx)}"
    return _owner_panel.PanelView(text=text, buttons=[[
        (_tr("settings.retention.yes", ctx, value=value), _callback(ctx, f"r.{days}.yes")),
        (_tr("settings.retention.cancel", ctx), _callback(ctx)),
    ]])


def _known_arg(arg: str | None) -> str | None:
    if arg in ARGS or arg == f"r.{_settings.retention_days()}":
        return arg
    return None


def render(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    """The Settings screen; ``ctx.arg`` is a tapped button from ARGS. Blocking: the router runs it in a thread."""
    connection = ctx.business_connection_id or next(iter(ctx.owner_connections), None)
    manager = _dossier_panel.manages_global(ctx.owner_user_id, connection)
    arg = _known_arg(ctx.arg)
    notice = None
    if arg is not None and not manager:
        logger.info("%s: settings change refused reason=owner_scope", _history.PLUGIN_NAME)
        notice = _owner_panel.tr("not_allowed", ctx.language_code)
    elif arg == "d.ask" or (arg == "d.on" and not _settings.enabled("dossiers")):
        # A stale "• Keep dossiers" from before dossiers were turned off asks again, never enables.
        if _settings.enabled("history"):
            return _confirm_view(ctx)
        notice = _tr("settings.needs_history", ctx)
    elif arg is not None and (target := _retention_target(arg)) is not None:
        days, confirmed = target
        # Judged against the value in effect now, so a stale button for a shorter value asks again.
        if not confirmed and _settings.keeps_less(days, _settings.retention_days()):
            return _retention_confirm_view(ctx, days)
        notice = _apply_retention(ctx, days)
    elif arg is not None:
        notice = _apply(ctx, "d.on" if arg == "d.yes" else arg)
    return _main_view(ctx, manager, notice)


def register_section(voice_cleanup: Callable[[], tuple[str, str] | None] | None = None) -> None:
    """Register Settings; ``voice_cleanup`` returns the voice cleanup (provider, model), or None while it is off."""
    global _voice_cleanup
    if voice_cleanup is not None:
        _voice_cleanup = voice_cleanup
    _owner_panel.register_strings(STRINGS)
    _owner_panel.register_section(SECTION_KEY, SECTION_LABEL, render, order=ORDER)
