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
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import history_support as _history

MODES = _history.TRANSCRIBE_MODES
DEFAULT_MODE = _history.TRANSCRIBE_DEFAULT_MODE

CALLBACK_PREFIX = "tb:"
MAX_CALLBACK_BYTES = 64  # Telegram's callback_data limit.
ACTIONS = frozenset({"m", "r", "s", "b", "p"})
NO_ARG_ACTIONS = frozenset({"r", "b", "p"})

NOT_ALLOWED = "Not allowed"
UNKNOWN_CONNECTION = (
    "I can't confirm you own this Business connection yet. "
    "Send /transcribe in that chat once, then try Manage Bot again."
)
UNKNOWN_CONNECTION_NO_TRANSCRIBE = (
    "I can't confirm you own this Business connection yet. "
    "Transcription is off here, so /transcribe won't help; turn on history capture, then try Manage Bot again."
)
UNKNOWN_CONNECTION_MODES_UNREADABLE = (
    f"{_history.TRANSCRIBE_SAVE_ERROR}\n"
    "The transcription modes file is unreadable, so I can't confirm you own this Business connection. "
    "Send /transcribe in that chat once to re-verify, then try Manage Bot again."
)
TRANSCRIPTION_DISABLED = "Transcription is disabled on this gateway."
SECTION_UNAVAILABLE = "Section unavailable"
BACK_LABEL = "« Back"
REFRESH_LABEL = "Refresh"
RESET_LABEL = "Reset to default"
MAX_TEXT_UNITS = 4096  # Telegram's message text limit, counted in UTF-16 code units.
MAX_TOAST_CHARS = 200  # Telegram's answerCallbackQuery text limit.
MAX_SECTION_LABEL_CHARS = 64  # The label is also the section's button text.
SECTION_KEY_MAX = 16
SUB_ARG_MAX = 32
ELLIPSIS = "\u2026"

# `/start bizChat<id>`, optionally `/start@BotName`. "bizChat" is exact, as Telegram sends it.
_START_RE = re.compile(r"^/start(?:@[A-Za-z0-9_]{1,64})?[ \t]+bizChat(-?\d{1,20})[ \t]*\Z")
_CHAT_ID_RE = r"-?\d{1,20}"
_SECTION_KEY_RE = rf"[a-z][a-z0-9_]{{0,{SECTION_KEY_MAX - 1}}}"
SECTION_KEY_RE = re.compile(rf"^{_SECTION_KEY_RE}\Z")
_SUB_ARG_RE = rf"[A-Za-z0-9_.-]{{1,{SUB_ARG_MAX}}}"
# The arg is unbounded here: MAX_CALLBACK_BYTES and the per-action checks
# (MODES, _SECTION_ARG_RE) set the real limits, so the patterns cannot drift apart.
_CALLBACK_RE = re.compile(rf"^tb:([a-z]):({_CHAT_ID_RE})(?::([A-Za-z0-9_./-]+))?\Z")
_SECTION_ARG_RE = re.compile(rf"^({_SECTION_KEY_RE})(?:/({_SUB_ARG_RE}))?\Z")


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


@dataclass
class PanelView:
    text: str
    # Rows of (label, callback data); build the data with callback_data/section_callback.
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)


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
    router appends the Back button. Keep text free of anything the owner did
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


# --- Rendering -----------------------------------------------------------------------------------


def chat_label_text(chat_id: Any, label: str | None) -> str:
    return label if label else f"chat {chat_id}"


def render_main(
    ctx: PanelContext,
    entry: Any,
    section_list: list[Section],
    transcription_enabled: bool,
    *,
    notice: str | None = None,
) -> PanelView:
    """The panel's main view: transcription controls, one row per section, Refresh.

    ``entry`` is the chat's stored mode entry (None for the default). ``notice``
    is a one-line status such as a save error, shown under the header.
    """
    chat = ctx.chat_id
    lines = [f"Owner panel: {chat_label_text(chat, ctx.chat_label)}"]
    if notice:
        lines.append(notice)
    buttons: list[list[tuple[str, str]]] = []
    if transcription_enabled:
        current = entry.mode if entry is not None else DEFAULT_MODE
        lines.append(f"Transcription: {current}" if entry is not None else f"Transcription: {DEFAULT_MODE} (default)")
        buttons.append([(f"• {mode}" if mode == current else mode, callback_data("m", chat, mode)) for mode in MODES])
        if entry is not None:
            buttons.append([(RESET_LABEL, callback_data("r", chat))])
    else:
        lines.append(TRANSCRIPTION_DISABLED)
    for section in section_list:
        buttons.append([(section.label, section_callback(chat, section.key))])
    buttons.append([(REFRESH_LABEL, callback_data("p", chat))])
    return PanelView(text="\n".join(lines), buttons=buttons)


def with_back(view: PanelView, chat_id: Any) -> PanelView:
    """A section view plus the Back row the router always adds."""
    return PanelView(text=view.text, buttons=[*view.buttons, [(BACK_LABEL, callback_data("b", chat_id))]])


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


def unavailable_view(chat_id: Any, label: str | None) -> PanelView:
    return with_back(PanelView(text=f"{chat_label_text(chat_id, label)}\n{SECTION_UNAVAILABLE}."), chat_id)
