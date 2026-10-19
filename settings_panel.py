"""Owner-panel Settings: the switches for transcription, history and dossiers in all chats.

Settings opens from the Settings button on any chat's panel, or with `/telesec` in the owner's
private chat with the bot (chat ``owner_panel.HOME_CHAT``, with no Back row). Only an owner of every
Business connection the plugin knows may change the switches (``dossier_panel.manages_global``);
other verified owners see them read-only. Buttons carry target values, not toggles, so a stale or
repeated tap changes nothing. Nothing here is ever posted into a Business chat.
"""

from __future__ import annotations

import logging
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
# "<feature>.<target value>"; "d.ask" opens the dossier confirm, since dossiers send chat text to a model.
ARGS = ("t.on", "t.off", "h.on", "h.off", "d.ask", "d.on", "d.off")
_FEATURES = {"t": "transcription", "h": "history", "d": "dossiers"}
CURRENT_MARK = "• "

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
    },
}


def _tr(key: str, ctx: _owner_panel.PanelContext, **values: Any) -> str:
    return _owner_panel.tr(key, ctx.language_code, **values)


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


def _choice_row(ctx: _owner_panel.PanelContext, feature: str, on: bool, on_arg: str) -> list[tuple[str, str]]:
    letter = feature[0]
    return [
        ((CURRENT_MARK if on else "") + _tr(f"settings.{feature}.on", ctx), _callback(ctx, on_arg)),
        ((CURRENT_MARK if not on else "") + _tr(f"settings.{feature}.off", ctx), _callback(ctx, f"{letter}.off")),
    ]


def _main_view(ctx: _owner_panel.PanelContext, manager: bool, notice: str | None) -> _owner_panel.PanelView:
    current = {feature: _settings.enabled(feature) for feature in _settings.FEATURES}
    lines = [_tr("settings.title", ctx)]
    if notice:
        lines.append(notice)
    if manager and ctx.chat_id == str(_owner_panel.HOME_CHAT):
        lines.append(_tr("settings.one_chat", ctx))
    if current["dossiers"] and not current["history"]:
        lines.append(_tr("settings.paused", ctx))
    if not manager:
        for feature in _settings.FEATURES:
            state = _tr(f"settings.state.{'on' if current[feature] else 'off'}", ctx)
            lines.append(_tr(f"settings.status.{feature}", ctx, state=state))
        lines.append(_tr("settings.read_only", ctx))
        return _owner_panel.PanelView(text="\n".join(lines))
    buttons = [
        _choice_row(ctx, "transcription", current["transcription"], "t.on"),
        _choice_row(ctx, "history", current["history"], "h.on"),
        # Always shown, so dossiers can always be turned off; turning them on asks first.
        _choice_row(ctx, "dossiers", current["dossiers"], "d.on" if current["dossiers"] else "d.ask"),
    ]
    return _owner_panel.PanelView(text="\n".join(lines), buttons=buttons)


def _confirm_view(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    cfg = _ds.load_config()
    text, yes, cancel = _dossier_view.enable_confirm(_dossier_view.lang_for(ctx.language_code), cfg.provider,
                                                     cfg.model)
    return _owner_panel.PanelView(text=text, buttons=[[(yes, _callback(ctx, "d.on")), (cancel, _callback(ctx))]])


def render(ctx: _owner_panel.PanelContext) -> _owner_panel.PanelView:
    """The Settings screen; ``ctx.arg`` is a tapped switch from ARGS. Blocking: the router runs it in a thread."""
    connection = ctx.business_connection_id or next(iter(ctx.owner_connections), None)
    manager = _dossier_panel.manages_global(ctx.owner_user_id, connection)
    arg = ctx.arg if ctx.arg in ARGS else None
    notice = None
    if arg is not None and not manager:
        logger.info("%s: settings change refused reason=owner_scope", _history.PLUGIN_NAME)
        notice = _owner_panel.tr("not_allowed", ctx.language_code)
    elif arg == "d.ask":
        if _settings.enabled("history"):
            return _confirm_view(ctx)
        notice = _tr("settings.needs_history", ctx)
    elif arg is not None:
        notice = _apply(ctx, arg)
    return _main_view(ctx, manager, notice)


def register_section() -> None:
    _owner_panel.register_strings(STRINGS)
    _owner_panel.register_section(SECTION_KEY, SECTION_LABEL, render, order=ORDER)
