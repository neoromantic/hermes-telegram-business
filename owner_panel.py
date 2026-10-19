"""Owner panel: per-chat controls in the owner's private chat with the bot.

Telegram shows a Business account owner a "Manage Bot" button in every chat
where the Business bot is connected. It opens the owner's private chat with
the bot, and the bot receives ``/start bizChat<user_chat_id>``. Nothing is
visible to the contact. The panel answers there with the chat's controls as
inline buttons.

This module is pure logic: payload and callback parsing, the section registry,
and rendering. Routing, owner verification, and Telegram I/O live in the
plugin's ``__init__``. Other features add a section with ``register_section``;
the router renders it in the owner's private chat, adds a Back button, and
passes a ``PanelContext`` whose ``business_connection_id`` is always one the
owner was verified for, so a section must scope its reads to it.

The panel speaks English, or Russian to a Telegram client set to Russian. Its
strings live in one catalog; sections add their own with ``register_strings``
and read them with ``tr(key, ctx.language_code)``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import history_support as _history

MODES = _history.TRANSCRIBE_MODES
DEFAULT_MODE = _history.TRANSCRIBE_DEFAULT_MODE

CALLBACK_PREFIX = "tb:"
MAX_CALLBACK_BYTES = 64  # Telegram's callback_data limit.
ACTIONS = frozenset({"m", "r", "s", "b", "p", "f"})
NO_ARG_ACTIONS = frozenset({"r", "b", "p"})
# `tb:f:<chat>:<arg>`: this chat's history (h) or dossier (d) on or off. Args carry the target value,
# so a stale or double tap is harmless.
FLAG_ARGS = frozenset({"h.on", "h.off", "d.on", "d.off"})
FLAG_FEATURES = {"h": "history", "d": "dossier"}

MAX_TEXT_UNITS = 4096  # Telegram's message text limit, counted in UTF-16 code units.
MAX_TOAST_CHARS = 200  # Telegram's answerCallbackQuery text limit.
MAX_SECTION_LABEL_CHARS = 64  # The label is also the section's button text.
SECTION_KEY_MAX = 16
SUB_ARG_MAX = 32
ELLIPSIS = "\u2026"

# `/start bizChat<id>`, optionally `/start@BotName`. "bizChat" is exact, as Telegram sends it.
_START_RE = re.compile(r"^/start(?:@[A-Za-z0-9_]{1,64})?[ \t]+bizChat(-?\d{1,20})[ \t]*\Z")
# An exact `/telesec`, optionally `/telesec@BotName`, opens Settings; any argument leaves it to the agent.
_SETTINGS_RE = re.compile(r"^/telesec(?:@[A-Za-z0-9_]{1,64})?[ \t]*\Z")
# Chat id of a screen opened by `/telesec` rather than from a chat's panel. The router accepts
# only the settings section on it, so no mode or section ever applies to a chat "0".
HOME_CHAT = 0
SETTINGS_SECTION = "settings"
_CHAT_ID_RE = r"-?\d{1,20}"
_SECTION_KEY_RE = rf"[a-z][a-z0-9_]{{0,{SECTION_KEY_MAX - 1}}}"
SECTION_KEY_RE = re.compile(rf"^{_SECTION_KEY_RE}\Z")
_SUB_ARG_RE = rf"[A-Za-z0-9_.-]{{1,{SUB_ARG_MAX}}}"
# The arg is unbounded here: MAX_CALLBACK_BYTES and the per-action checks
# (MODES, _SECTION_ARG_RE) set the real limits, so the patterns cannot drift apart.
_CALLBACK_RE = re.compile(rf"^tb:([a-z]):({_CHAT_ID_RE})(?::([A-Za-z0-9_./-]+))?\Z")
_SECTION_ARG_RE = re.compile(rf"^({_SECTION_KEY_RE})(?:/({_SUB_ARG_RE}))?\Z")


# --- Strings -------------------------------------------------------------------------------------

LANGUAGES = ("en", "ru")
# English defines every key; another language falls back to English key by key.
# A section's button label is its "section.<key>" string when there is one.
_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Owner panel",
        "transcription": "Transcription: {mode}",
        "transcription_default": "Transcription: {mode} (default)",
        "transcription_disabled": "Transcription: off in Settings",
        "mode.both": "both",
        "mode.own": "own",
        "mode.peer": "peer",
        "mode.off": "off",
        "reset": "Reset to default",
        "history.on": "Save history",
        "history.off": "Don't save",
        "dossier.on": "Keep dossier",
        "dossier.off": "No dossier",
        "history.settings": "History: off in Settings",
        "history.server": "History: off (server settings)",
        "history.unreadable": "Chat settings can't be read; history and dossiers are paused.",
        "dossier.settings": "Dossier: off in Settings",
        "dossier.needs_history": "Dossier: needs history",
        "dossier.private_only": "Dossier: private chats only",
        "history.done.on": "History: on for this chat",
        "history.done.off": "History: off for this chat",
        "dossier.done.on": "Dossier: on for this chat",
        "dossier.done.off": "Dossier: off for this chat",
        "chat_save_error": "Error: could not save the chat setting",
        "refresh": "Refresh",
        "refreshed": "Refreshed",
        "back": "« Back",
        "section_unavailable": "Section unavailable",
        "not_allowed": "Not allowed",
        "unknown_action": "Unknown action",
        "save_error": _history.TRANSCRIBE_SAVE_ERROR,
        "unknown_connection": (
            "I can't confirm you own this Business connection yet. "
            "Send any message in that chat, then tap Manage Bot again."
        ),
        "settings_unknown_connection": (
            "I don't see your Business connection yet. Send any message in a Business chat, then send /telesec again."
        ),
        "unknown_connection_modes_unreadable": (
            f"{_history.TRANSCRIBE_SAVE_ERROR}\n"
            "The transcription modes file is unreadable, so I can't confirm you own this Business connection. "
            "Send any message in that chat, then tap Manage Bot again."
        ),
    },
    "ru": {
        "title": "Панель владельца",
        "transcription": "Расшифровка: {mode}",
        "transcription_default": "Расшифровка: {mode} (по умолчанию)",
        "transcription_disabled": "Расшифровка: выключена в настройках",
        "mode.both": "все",
        "mode.own": "мои",
        "mode.peer": "собеседника",
        "mode.off": "выкл",
        "reset": "Сбросить",
        "history.on": "Сохранять историю",
        "history.off": "Не сохранять",
        "dossier.on": "Вести досье",
        "dossier.off": "Не вести",
        "history.settings": "История: выключена в настройках",
        "history.server": "История: выкл (настройки сервера)",
        "history.unreadable": "Настройки чатов не читаются; история и досье на паузе.",
        "dossier.settings": "Досье: выключено в настройках",
        "dossier.needs_history": "Досье: нужна история",
        "dossier.private_only": "Досье: только для личных чатов",
        "history.done.on": "История: включена для этого чата",
        "history.done.off": "История: выключена для этого чата",
        "dossier.done.on": "Досье: включено для этого чата",
        "dossier.done.off": "Досье: выключено для этого чата",
        "chat_save_error": "Ошибка: не удалось сохранить настройку чата",
        "refresh": "Обновить",
        "refreshed": "Обновлено",
        "back": "« Назад",
        "section_unavailable": "Раздел недоступен",
        "not_allowed": "Нет доступа",
        "unknown_action": "Неизвестное действие",
        "save_error": "Ошибка: не удалось сохранить режим расшифровки",
        "unknown_connection": (
            "Пока не могу подтвердить, что это ваше Business-подключение. "
            "Отправьте любое сообщение в том чате и снова нажмите кнопку управления ботом."
        ),
        "settings_unknown_connection": (
            "Пока не вижу вашего Business-подключения. Отправьте любое сообщение в Business-чате и снова пришлите /telesec."
        ),
        "unknown_connection_modes_unreadable": (
            "Ошибка: не удалось сохранить режим расшифровки\n"
            "Файл режимов расшифровки не читается, поэтому я не могу подтвердить, что это ваше "
            "Business-подключение. Отправьте любое сообщение в том чате и снова нажмите кнопку управления ботом."
        ),
    },
}


def language(language_code: Any) -> str:
    """"ru" for any Russian Telegram client language ("ru", "ru-RU", ...), else "en"."""
    return "ru" if isinstance(language_code, str) and language_code.strip().casefold().startswith("ru") else "en"


def tr(key: str, language_code: str | None = None, **values: Any) -> str:
    """The catalog string for ``key`` in the client's language, formatted with ``values``.

    An unknown key is a KeyError: every key has an English string.
    """
    template = _STRINGS[language(language_code)].get(key)
    if template is None:
        template = _STRINGS["en"][key]
    return template.format(**values) if values else template


def register_strings(strings: Mapping[str, Mapping[str, str]]) -> None:
    """Add or replace catalog strings, e.g. ``{"en": {"stats.title": "Stats"}, "ru": {...}}``.

    Every key needs an English string, in this call or an earlier one. Prefix
    keys with the section key so sections never collide.
    """
    for lang, values in strings.items():
        if lang not in LANGUAGES:
            raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items()):
            raise TypeError("catalog keys and strings must be str")
    english = {**_STRINGS["en"], **strings.get("en", {})}
    missing = sorted({key for values in strings.values() for key in values if key not in english})
    if missing:
        raise ValueError(f"no English string for {', '.join(missing)}")
    for lang, values in strings.items():
        _STRINGS[lang].update(values)


def mode_status(mode: str, *, default: bool, language_code: str | None = None) -> str:
    """ "Transcription: off", or "Transcription: both (default)" for a chat without a stored mode."""
    key = "transcription_default" if default else "transcription"
    return tr(key, language_code, mode=tr(f"mode.{mode}", language_code))


def parse_settings_command(text: Any) -> bool:
    """True for an exact `/telesec` (or `/telesec@BotName`), which opens Settings."""
    return isinstance(text, str) and _SETTINGS_RE.match(text) is not None


def parse_start_payload(text: Any) -> int | None:
    """The Business chat id from a "Manage Bot" ``/start bizChat<id>``; None for anything else."""
    if not isinstance(text, str):
        return None
    match = _START_RE.match(text)
    return int(match.group(1)) if match is not None else None


# --- Callback data -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PanelCallback:
    action: str
    chat_id: int
    arg: str | None = None
    section: str | None = None
    sub: str | None = None


def callback_data(action: str, chat_id: int | str, arg: str | None = None) -> str:
    """Build ``tb:<action>:<chat_id>[:<arg>]``; raises ValueError for anything ``parse_callback`` rejects.

    Data that would exceed Telegram's 64-byte limit is an error, never clipped:
    a clipped payload would silently mean something else.
    """
    data = f"{CALLBACK_PREFIX}{action}:{chat_id}" + (f":{arg}" if arg is not None else "")
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback data longer than {MAX_CALLBACK_BYTES} bytes")
    if parse_callback(data) is None:
        raise ValueError("invalid callback data")
    return data


def section_callback(chat_id: int | str, key: str, sub: str | None = None) -> str:
    """Callback data that opens section ``key``, optionally with a section-defined sub-argument.

    ``sub`` is at most ``SUB_ARG_MAX`` characters of ``[A-Za-z0-9_.-]``, but the
    whole payload must also fit Telegram's 64 bytes, so the real budget is
    ``64 - len("tb:s:") - len(str(chat_id)) - 1 - len(key) - 1``: a 16-character
    key leaves 32 for ``sub`` only with chat ids of up to 9 digits. Keep page
    tokens short, or catch the ValueError and offer fewer buttons.
    """
    return callback_data("s", chat_id, key if sub is None else f"{key}/{sub}")


def parse_callback(data: Any) -> PanelCallback | None:
    """Strict parse of panel callback data; None for anything malformed."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return None
    match = _CALLBACK_RE.match(data)
    if match is None:
        return None
    action, chat, arg = match.group(1), match.group(2), match.group(3)
    if action not in ACTIONS:
        return None
    if action in NO_ARG_ACTIONS:
        return PanelCallback(action, int(chat)) if arg is None else None
    if arg is None:
        return None
    if action == "m":
        return PanelCallback(action, int(chat), arg) if arg in MODES else None
    if action == "f":
        return PanelCallback(action, int(chat), arg) if arg in FLAG_ARGS else None
    section = _SECTION_ARG_RE.match(arg)
    if section is None:
        return None
    return PanelCallback(action, int(chat), arg, section=section.group(1), sub=section.group(2))


# --- Sections ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PanelContext:
    owner_user_id: str
    chat_id: str
    chat_label: str | None
    business_connection_id: str | None
    arg: str | None = None
    language_code: str | None = None  # the tapping owner's Telegram client language; see ``tr``
    owner_connections: tuple[str, ...] = ()  # every Business connection the owner was verified for


@dataclass
class PanelView:
    text: str
    # Rows of (label, callback data); build the data with callback_data/section_callback.
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    # Optional Bot API rich message HTML (sendRichMessage); the router sends it when set, ``text`` is the fallback.
    html: str | None = None


SectionRender = Callable[[PanelContext], "PanelView | Awaitable[PanelView]"]


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    render: SectionRender
    order: int = 100


_SECTIONS: dict[str, Section] = {}


def register_section(key: str, label: str, render: SectionRender, order: int = 100) -> Section:
    """Add (or replace) a panel section.

    ``render`` runs in a worker thread, so blocking reads are fine; it may also
    return an awaitable. It gets the chat's ``PanelContext`` (``arg`` is the
    sub-argument from ``section_callback``) and returns a ``PanelView``; the
    router appends the Back button. Use ``tr(key, ctx.language_code)`` for text. Keep text free of anything the owner did
    not ask to see, and never log it.

    ``label`` is the section's button text and its tap toast, at most 64
    characters. Limits the router enforces: text must be non-empty (an empty view shows
    "Section unavailable"), text over Telegram's 4096 UTF-16 units is clipped
    with an ellipsis, and a render gets about 8 seconds before the panel shows
    "Section unavailable" instead. The bot handles updates one at a time, so
    keep renders cheap or precomputed.
    """
    if not isinstance(key, str) or not SECTION_KEY_RE.match(key):
        raise ValueError(f"section key must match {_SECTION_KEY_RE}")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("section label must be non-empty")
    if len(label.strip()) > MAX_SECTION_LABEL_CHARS:
        raise ValueError(f"section label must be at most {MAX_SECTION_LABEL_CHARS} characters")
    if not callable(render):
        raise TypeError("section render must be callable")
    section = Section(key=key, label=label.strip(), render=render, order=int(order))
    _SECTIONS[key] = section
    return section


def unregister_section(key: str) -> None:
    _SECTIONS.pop(key, None)


def sections() -> list[Section]:
    return sorted(_SECTIONS.values(), key=lambda section: (section.order, section.key))


def get_section(key: str | None) -> Section | None:
    return _SECTIONS.get(key) if key is not None else None


def section_label(section: Section, language_code: str | None = None) -> str:
    """The section's "section.<key>" catalog string, else its registered label."""
    key = f"section.{section.key}"
    return tr(key, language_code) if key in _STRINGS["en"] else section.label


# --- Rendering -----------------------------------------------------------------------------------


def chat_label_text(chat_id: Any, label: str | None) -> str:
    """The chat's name plus ``chat <id>``, untranslated, so a reply to the panel names the chat."""
    return f"{label} · chat {chat_id}" if label else f"chat {chat_id}"


# Row states with a status line of their own; any other hidden state shows no line.
_HISTORY_LINES = frozenset({"settings", "server", "unreadable"})
_DOSSIER_LINES = frozenset({"settings", "needs_history"})
CHAT_DEFAULT_FEATURES = ("on", "on")


def feature_rows(
    chat_id: Any, features: tuple[str, str], language_code: str | None = None
) -> tuple[list[str], list[list[tuple[str, str]]]]:
    """This chat's history and dossier controls in the transcription style: (status lines, button rows).

    ``features`` is (history, dossier) from ``chat_settings.chat_state``: "on" or "off" shows a choice
    row with "•" on the value in effect; any other state hides the row, and some show a line instead.
    """
    lines: list[str] = []
    rows: list[list[tuple[str, str]]] = []
    for (key, feature), state, with_line in zip(
        FLAG_FEATURES.items(), features, (_HISTORY_LINES, _DOSSIER_LINES), strict=True
    ):
        if state in ("on", "off"):
            rows.append(
                [
                    (("• " if value == state else "") + tr(f"{feature}.{value}", language_code),
                     callback_data("f", chat_id, f"{key}.{value}"))
                    for value in ("on", "off")
                ]
            )
        elif state in with_line:
            lines.append(tr(f"{feature}.{state}", language_code))
    return lines, rows


def flag_unavailable(feature: str, features: tuple[str, str], language_code: str | None = None) -> str | None:
    """The toast for a tap on a hidden history or dossier row (a stale button), else None."""
    history, dossier = features
    state = history if feature == "history" else dossier
    if state in ("on", "off"):
        return None
    if feature == "dossier" and state == "no_history":
        state, feature = history, "history"
    if state == "unreadable":
        return tr("chat_save_error", language_code)
    return tr(f"{feature}.{state}", language_code)


def render_main(
    ctx: PanelContext,
    entry: Any,
    section_list: list[Section],
    transcription_enabled: bool,
    *,
    notice: str | None = None,
    features: tuple[str, str] = CHAT_DEFAULT_FEATURES,
) -> PanelView:
    """The panel's main view: transcription, history and dossier controls, then the sections and Refresh.

    ``entry`` is the chat's stored mode entry (None for the default). ``notice``
    is a one-line status such as a save error, shown under the header.
    ``features`` is (history, dossier), see ``feature_rows``.
    """
    chat = ctx.chat_id
    lang = ctx.language_code
    lines = [f"{tr('title', lang)}: {chat_label_text(chat, ctx.chat_label)}"]
    if notice:
        lines.append(notice)
    buttons: list[list[tuple[str, str]]] = []
    if transcription_enabled:
        current = entry.mode if entry is not None else DEFAULT_MODE
        lines.append(mode_status(current, default=entry is None, language_code=lang))
        buttons.append(
            [
                (("• " if mode == current else "") + tr(f"mode.{mode}", lang), callback_data("m", chat, mode))
                for mode in MODES
            ]
        )
        if entry is not None:
            buttons.append([(tr("reset", lang), callback_data("r", chat))])
    else:
        lines.append(tr("transcription_disabled", lang))
    feature_lines, feature_buttons = feature_rows(chat, features, lang)
    lines += feature_lines
    buttons += feature_buttons
    # Sections and Refresh, two per row.
    tail = [(section_label(section, lang), section_callback(chat, section.key)) for section in section_list]
    tail.append((tr("refresh", lang), callback_data("p", chat)))
    buttons += [tail[index : index + 2] for index in range(0, len(tail), 2)]
    return PanelView(text="\n".join(lines), buttons=buttons)


def with_back(view: PanelView, chat_id: Any, language_code: str | None = None) -> PanelView:
    """A section view plus the Back row the router always adds."""
    back = [(tr("back", language_code), callback_data("b", chat_id))]
    return PanelView(text=view.text, buttons=[*view.buttons, back], html=view.html)


def clip_text(text: str) -> str:
    """Clip to Telegram's 4096 UTF-16 units, ending in an ellipsis when clipped."""
    if len(text.encode("utf-16-le")) // 2 <= MAX_TEXT_UNITS:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > MAX_TEXT_UNITS - 1:
            break
        kept.append(char)
        used += size
    return "".join(kept) + ELLIPSIS


def unavailable_view(chat_id: Any, label: str | None, language_code: str | None = None) -> PanelView:
    text = f"{chat_label_text(chat_id, label)}\n{tr('section_unavailable', language_code)}."
    return with_back(PanelView(text=text), chat_id, language_code)
