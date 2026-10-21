"""Pure Telegram views of dossiers: the owner panel screens, the ``/dossier`` post and its refusals.

A dossier is one Markdown document. ``markdown_html`` turns it into the small rich HTML subset Telesec
sends (``h4 p b i br ul li blockquote``; ``h3`` is the screen title) and escapes every text run.
No I/O: no files, history, env or clock (``now`` comes in). The I/O layer builds a ``DossierSnapshot``
and sends ``View.html`` as a rich message (``rich_message(view)``), ``View.text`` as the plain fallback
and ``View.buttons`` as inline keyboard rows of (label, callback args). Rich HTML stays within a safety
budget below the Bot API rich message limits; plain text stays within 4096 UTF-16 units. Callback args
are short ASCII and ``parse_args`` rejects anything else, because they come back from Telegram.
"""

from __future__ import annotations

import html as _html
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from . import dossier_doc as _doc
from . import dossier_writer as _writer

RICH_MAX_CHARS = 32_768  # Bot API rich message text limit
RICH_MAX_BLOCKS = 500  # incl. nested blocks, list items, quotations and details
BUDGET_CHARS = 30_000  # whole HTML string in UTF-16 units, so the parsed text is always under the limit
BUDGET_BLOCKS = 450
TEXT_MAX_CHARS = 4096
ARGS_MAX_BYTES = 30  # the panel adds its own prefix; Telegram callback_data is <= 64 bytes
NAME_CHARS = 64  # UTF-16 units of a contact name in a title or a message to the owner
NOTE_DISPLAY_UNITS = 500  # a note typed in the CLI shows whole; a longer one (converted or appended) is clipped
CLIPPED_DOC_UNITS = 3000  # the start of a converted v1 document over the limit, with room for the 'show' line
NOTE_DISPLAY_LINES = 10  # the plain fallback puts "> " before each quoted line, so lines cost room too
ERROR_CHARS = 64
POST_RU_SHARE = 0.3  # the post is in Russian when at least this share of its letters is Cyrillic
CLI = "hermes telesec dossier"
TARGETS = {"contact": "c", "persona": "p"}
POST_REASONS = ("disabled", "no_model", "history_off", "chat_off", "window", "unqualified", "empty", "busy",
                "changed", "failed", "send_refused", "send_failed", "send_unsure")
NOTICE_KEYS = ("busy", "refresh_started", "unavailable", "up_to_date", "failed", "operator_only", "no_connection")

Rows = tuple[tuple[tuple[str, str], ...], ...]

# ---------------------------------------------------------------------------
# Strings (constants only: no "<", ">", "&" or double quotes, so they are HTML-safe as is)
# ---------------------------------------------------------------------------

STRINGS: dict[str, dict[str, Any]] = {
    "en": {
        "months": ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                   "October", "November", "December"),
        "forms": {"messages": ("message", "messages"), "chats": ("chat", "chats")},
        "title_persona": "My persona",
        "head_contact": "Updated {time} · from {messages}",
        "head_persona": "Updated {time} · from {n} of your messages in {chats}",
        "note_label": "Your note (not posted)",
        "persona_note_label": "Your note",
        "st_failed": "The last refresh failed ({error}). This is the earlier version.",
        "st_failed_none": "The dossier could not be written yet ({error}). Telesec will try again.",
        "st_failed_none_persona": "The persona could not be written yet ({error}). Telesec will try again.",
        "st_missing": "No dossier yet. It is written a few minutes after the chat goes quiet, or now with Refresh.",
        "st_not_in_history": "No dossier yet: this chat has no saved messages. It is written once new messages "
                             "arrive.",
        "st_unqualified": "No dossier yet: it needs messages from both you and the contact.",
        "st_empty": "Nothing worth keeping yet.",
        "st_converted": "Converted from the old dossier. The first refresh rewrites it.",
        "st_converted_waiting": "Converted from the old dossier. It is rewritten once the chat has saved messages "
                                "from both you and the contact.",
        "st_clipped": f"Only the start fits here. The full text: {CLI} show {{target}}",
        "st_persona_missing": "No persona yet. It is written once you have sent at least 10 messages in chats "
                              "with dossiers.",
        "st_persona_due": "No persona yet. It is written at the next dossier cycle.",
        "st_refreshing": "Refreshing now…",
        "st_off": "Dossiers are off. Turn them on in Settings.",
        "st_history_off": "History is off, so dossiers are paused. Turn it on in Settings.",
        "note_chat_off": "Off for this chat.",
        "st_no_model": "The model is not connected in this process.",
        "st_corrupt": f"This dossier file can't be read. Reset it with: {CLI} forget {{target}}",
        "st_corrupt_persona": f"The persona file can't be read. Reset it with: {CLI} forget --persona",
        "off_cli": f"From the terminal: {CLI} enable",
        "b_refresh": "Refresh", "b_persona": "My persona", "b_back_dossier": "‹ Dossier", "b_dossier": "Open dossier",
        "b_enable_yes": "Enable", "b_cancel": "Cancel",
        "enable_q": "Turn on dossiers?",
        "enable_explain": "The text of ALL private chats will be sent to {model} to build a dossier for each "
                          "contact and your persona.",
        "main_model": "your main model",
        "enable_route": "Dossiers use the Telesec dossiers model task; change it with 'hermes model'. If that "
                        "model fails, Hermes may retry with your main model.",
        "enable_gate": "For short checks, the current dossier and the chat's new messages go to TypeSafe Jev "
                       "through the same AI Gateway, with zero data retention.",
        "enable_clairic": "New messages also go to Cloudflare Workers AI (Clairic, a preview model) to pick out "
                          "dates, amounts and contact details for the writer.",
        "enable_keep": "Dossiers are stored locally. You can turn them off at any time; stored dossiers are kept.",
        "notice": {"busy": "Another dossier cycle is running. Try again in a minute.",
                   "refresh_started": "Refresh started. Open the dossier again in a minute.",
                   "unavailable": "Refresh is unavailable: dossiers or history are off, or the model is not connected.",
                   "up_to_date": "Already up to date",
                   "failed": "That did not work ({error}).",
                   "operator_only": "Only the owner of every Business connection the bot knows about can open "
                                    "the persona or turn dossiers on and off.",
                   "no_connection": "The Business connection of this chat is not known yet. Send any message "
                                    "in the chat, then open Manage Bot again."},
        "post_title": "Dossier",
        "post_footer": "Prepared automatically from this chat on {date}; it may contain mistakes.",
        "post_head": "Nothing was posted to the chat with {name}.",
        "post_head_unsure": "The dossier may have been posted to the chat with {name}.",
        "post": {"disabled": "Dossiers are off. Turn them on in Settings (/telesec).",
                 "no_model": "The dossier needs a refresh first, and the model is not connected.",
                 "history_off": "History is off, so deleted messages can't be tracked. Turn history on in "
                                "Settings (/telesec).",
                 "chat_off": "Dossier is off for this chat.",
                 "window": "The contact hasn't written in the last 24 hours, and Telegram lets a bot post only "
                           "within 24 hours of their last message.",
                 "unqualified": "No dossier yet: it needs messages from both you and the contact.",
                 "empty": "The dossier has nothing worth keeping yet.",
                 "busy": "The dossier for this chat is already being posted.",
                 "changed": "Messages changed while the dossier was being refreshed. Send /dossier again.",
                 "failed": "The dossier could not be prepared ({error}).",
                 "send_refused": "Telegram did not accept the dossier ({error}). One possible cause: a bot can post "
                                 "in a Business chat only if the contact wrote there in the last 24 hours.",
                 "send_failed": "Telegram did not accept the dossier ({error}).",
                 "send_unsure": "Telegram did not confirm it ({error}). Check the chat before you send /dossier "
                                "again."},
    },
    "ru": {
        "months": ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
                   "октября", "ноября", "декабря"),
        "forms": {"messages": ("сообщение", "сообщения", "сообщений"), "chats": ("чат", "чата", "чатов")},
        "title_persona": "Моя персона",
        "head_contact": "Обновлено {time} · сообщений: {n}",
        "head_persona": "Обновлено {time} · ваших сообщений: {n}, чатов: {m}",
        "note_label": "Ваша заметка (не публикуется)",
        "persona_note_label": "Ваша заметка",
        "st_failed": "Последнее обновление не удалось ({error}). Показана прошлая версия.",
        "st_failed_none": "Досье пока не удалось составить ({error}). Telesec попробует ещё раз.",
        "st_failed_none_persona": "Персону пока не удалось составить ({error}). Telesec попробует ещё раз.",
        "st_missing": "Досье пока нет. Оно появится через несколько минут после того, как в чате станет тихо, "
                      "или сразу по кнопке «Обновить».",
        "st_not_in_history": "Досье пока нет: в этом чате нет сохранённых сообщений. Оно появится после новых "
                             "сообщений.",
        "st_unqualified": "Досье пока нет: нужны сообщения и от вас, и от собеседника.",
        "st_empty": "Пока нечего сохранить.",
        "st_converted": "Перенесено из старого досье. Первое обновление перепишет его.",
        "st_converted_waiting": "Перенесено из старого досье. Оно будет переписано, когда в чате сохранятся "
                                "сообщения и от вас, и от собеседника.",
        "st_clipped": f"Здесь помещается только начало. Полный текст: {CLI} show {{target}}",
        "st_persona_missing": "Персоны пока нет. Она появится, когда вы отправите хотя бы 10 сообщений в чатах "
                              "с досье.",
        "st_persona_due": "Персоны пока нет. Она появится после следующего цикла обработки.",
        "st_refreshing": "Обновляю…",
        "st_off": "Досье выключены. Включите их в Настройках.",
        "st_history_off": "История выключена, поэтому досье на паузе. Включите её в настройках.",
        "note_chat_off": "Для этого чата выключено.",
        "st_no_model": "Модель не подключена в этом процессе.",
        "st_corrupt": f"Файл досье не читается. Сбросьте его командой: {CLI} forget {{target}}",
        "st_corrupt_persona": f"Файл персоны не читается. Сбросьте его командой: {CLI} forget --persona",
        "off_cli": f"Из терминала: {CLI} enable",
        "b_refresh": "Обновить", "b_persona": "Моя персона", "b_back_dossier": "‹ Досье",
        "b_dossier": "Открыть досье", "b_enable_yes": "Включить", "b_cancel": "Отмена",
        "enable_q": "Включить досье?",
        "enable_explain": "Текст ВСЕХ личных чатов будет отправляться в {model}, чтобы собрать досье "
                          "по каждому собеседнику и вашу персону.",
        "main_model": "вашу основную модель",
        "enable_route": "Досье пишет модель задачи Telesec dossiers; её можно сменить через 'hermes model'. "
                        "Если эта модель не ответит, Hermes может повторить запрос на вашей основной модели.",
        "enable_gate": "Для коротких проверок текущее досье и новые сообщения чата уходят в TypeSafe Jev через "
                       "тот же AI Gateway, без хранения данных.",
        "enable_clairic": "Новые сообщения также уходят в Cloudflare Workers AI (Clairic, тестовая модель): она "
                          "выделяет для досье даты, суммы и контакты.",
        "enable_keep": "Досье хранятся локально. Выключить их можно в любой момент, сохранённые досье останутся.",
        "notice": {"busy": "Сейчас идёт другой цикл обработки. Попробуйте через минуту.",
                   "refresh_started": "Обновление запущено. Откройте досье через минуту.",
                   "unavailable": "Обновление недоступно: досье или история выключены, либо модель не подключена.",
                   "up_to_date": "Уже актуально",
                   "failed": "Не получилось ({error}).",
                   "operator_only": "Персона и включение досье доступны только владельцу всех бизнес-подключений, "
                                    "известных боту.",
                   "no_connection": "Business-подключение этого чата пока неизвестно. Отправьте любое "
                                    "сообщение в том чате и снова нажмите кнопку управления ботом."},
        "post_title": "Досье",
        "post_footer": "Составлено автоматически по этой переписке {date}; возможны неточности.",
        "post_head": "В чат «{name}» ничего не отправлено.",
        "post_head_unsure": "Досье, возможно, отправлено в чат «{name}».",
        "post": {"disabled": "Досье выключены. Включите их в Настройках (/telesec).",
                 "no_model": "Досье нужно сначала обновить, а модель не подключена.",
                 "history_off": "История выключена, поэтому удалённые сообщения не отслеживаются. Включите "
                                "историю в Настройках (/telesec).",
                 "chat_off": "Досье для этого чата выключено.",
                 "window": "Собеседник не писал последние 24 часа, а Telegram разрешает боту писать только "
                           "в течение 24 часов после его последнего сообщения.",
                 "unqualified": "Досье пока нет: нужны сообщения и от вас, и от собеседника.",
                 "empty": "В досье пока нечего сохранить.",
                 "busy": "Досье для этого чата уже отправляется.",
                 "changed": "Пока обновлялось досье, сообщения изменились. Отправьте /dossier ещё раз.",
                 "failed": "Не удалось подготовить досье ({error}).",
                 "send_refused": "Telegram не принял досье ({error}). Возможная причина: бот может писать в "
                                 "бизнес-чат, только если собеседник писал туда за последние 24 часа.",
                 "send_failed": "Telegram не принял досье ({error}).",
                 "send_unsure": "Telegram не подтвердил отправку ({error}). Проверьте чат, прежде чем отправлять "
                                "/dossier ещё раз."},
    },
}


def lang_for(language_code: str | None) -> str:
    """'ru' for Telegram language codes starting with "ru", otherwise 'en'."""
    return "ru" if isinstance(language_code, str) and language_code.strip().lower().startswith("ru") else "en"


def _s(lang: str) -> dict[str, Any]:
    return STRINGS["ru" if lang == "ru" else "en"]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class View:
    html: str  # rich message HTML
    text: str  # plain fallback, <= 4096 UTF-16 units
    buttons: Rows = ()  # inline keyboard rows of (label, callback args)
    notice: str | None = None  # short toast text (answerCallbackQuery), <= 200 chars


@dataclass(frozen=True)
class DossierSnapshot:
    """Everything a screen needs, gathered by the I/O layer. ``doc`` is the ``document.json`` content."""

    target: str  # contact | persona
    name: str = ""  # catalog display name; "" for the persona
    chat_id: Any = None  # for the CLI lines on the screen ("forget", "show")
    connection: Any = None  # the chat's Business connection: a contact can have chats with two owners
    doc: dict[str, Any] | None = None  # None when there is no document
    corrupt: bool = False  # document.json exists but can't be read
    enabled: bool = True
    history_enabled: bool = True  # history capture is on; dossiers are built from it
    chat_off: bool = False  # the owner set "No dossier" or "Don't save" for this chat
    llm_ready: bool = True
    refreshing: bool = False  # a refresh of this chat runs now
    failure: str | None = None  # error type or validator code of the last failed attempt
    unqualified: bool = False  # no dossier can be written yet: messages from both sides are missing
    manage_global: bool = False  # may open the persona (it owns every connected account)
    persona_due: bool = False  # no persona yet, but enough sent messages: the next cycle writes it
    in_history: bool = True  # the chat is a private chat in the history catalog (a refresh needs it)


@dataclass(frozen=True)
class Action:
    name: str  # overview | refresh
    target: str = "contact"  # contact | persona


# ---------------------------------------------------------------------------
# Callback args: "o.c" (dossier), "o.p" (persona), "rf.c" (refresh); "" is the dossier too
# ---------------------------------------------------------------------------

_CODES = {"overview": "o", "refresh": "rf"}
ARGS_RE = re.compile(r"[a-z0-9.]{1,30}")


def build_args(name: str, target: str = "contact") -> str:
    """Callback args for an action; raises ValueError for anything parse_args would reject."""
    if name not in _CODES or target not in TARGETS or (name == "refresh" and target != "contact"):
        raise ValueError(f"unknown action {name!r} for target {target!r}")
    return f"{_CODES[name]}.{TARGETS[target]}"


_ACTIONS = {build_args(name, target): Action(name, target)
            for name, target in (("overview", "contact"), ("overview", "persona"), ("refresh", "contact"))}


def parse_args(data: Any) -> Action | None:
    """The action encoded by ``build_args`` ("" opens the dossier), or None for anything else.

    The fact codes of earlier versions give None too; the panel then shows the dossier.
    """
    if data == "":
        return Action("overview")
    if not isinstance(data, str) or not ARGS_RE.fullmatch(data):
        return None
    return _ACTIONS.get(data)


def with_notice(view: View) -> View:
    """``view`` with its notice as the first line, for a host that cannot show it as a toast."""
    if not view.notice or view.html == f"<p>{_html.escape(view.notice)}</p>":
        return view  # a render_state screen already is the notice
    html = f"<p><b>{_html.escape(view.notice)}</b></p>{view.html}"
    return View(html, _clip_text(f"{view.notice}\n\n{view.text}"), view.buttons, view.notice)


def rich_message(view: View) -> dict[str, Any]:
    """InputRichMessage for sendRichMessage / editMessageText(rich_message=...).

    Entity detection is off: dossiers quote contacts, so a "/command" or link in them must stay inert.
    """
    return {"html": view.html, "skip_entity_detection": True}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# C0 and C1 controls except the newline, and the bidi marks, embeddings, overrides and isolates.
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _units(text: str) -> int:
    return _doc.units(text)


def _clip_text(text: str, limit: int = TEXT_MAX_CHARS) -> str:
    """Clip to ``limit`` UTF-16 units (Telegram's count), ending in an ellipsis when clipped."""
    if _units(text) <= limit:
        return text
    used = 0
    for end, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit - 1:
            break
    return text[:end].rstrip() + "…"


def _clip(value: Any, limit: int) -> str:
    """One line: controls and bidi characters removed, whitespace collapsed, at most ``limit`` UTF-16 units."""
    text = _CONTROL_RE.sub(" ", "" if value is None else str(value))
    return _clip_text(" ".join(text.split()), limit)


def _e(value: Any, limit: int) -> str:
    return _html.escape(_clip(value, limit), quote=False)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _word(lang: str, n: int, forms: tuple[str, ...]) -> str:
    if lang != "ru":
        return forms[0] if n == 1 else forms[1]
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    return forms[1] if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else forms[2]


def _plural(lang: str, n: int, forms: tuple[str, ...]) -> str:
    return f"{n} {_word(lang, n, forms)}"


def _dt(value: Any) -> datetime | None:
    """An aware UTC datetime, or None for anything else (bad text, or out of range in UTC)."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())  # 3.11+: accepts a trailing "Z"
        except ValueError:
            return None
    else:
        return None
    try:
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except OverflowError:  # e.g. year 1 with a positive offset
        return None


def _date(at: datetime, lang: str) -> str:
    """'20 October 2026' / '20 октября 2026', from the fields of ``at`` as they are."""
    return f"{at.day} {_s(lang)['months'][at.month - 1]} {at.year}"


def _tg_time(at: datetime, fmt: str, body: str) -> str:
    unix = _html.escape(str(int(at.timestamp())), quote=True)
    return f'<tg-time unix="{unix}" format="{_html.escape(fmt, quote=True)}">{_html.escape(body)}</tg-time>'


def _rows(*rows: list[tuple[str, str]]) -> Rows:
    return tuple(tuple(row) for row in rows if row)


# ---------------------------------------------------------------------------
# Block counting and plain text (both parse our own HTML)
# ---------------------------------------------------------------------------

_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "details", "blockquote", "ul", "ol",
                         "hr", "footer", "pre"})
_VOID_TAGS = frozenset({"br", "hr"})
_RUN_CONTAINERS = frozenset({"", "li", "details", "blockquote"})  # "" = the message root


class _BlockCounter(HTMLParser):
    """Counts rich blocks: block tags plus each loose inline run that becomes an implicit paragraph.

    List items and quotations hold blocks, so even their only text is a paragraph block of its own.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks = 0
        self.frames: list[list[Any]] = [["", 0, False]]  # tag, runs, run open

    def _inline(self) -> None:
        frame = self.frames[-1]
        if frame[0] in _RUN_CONTAINERS and not frame[2]:
            frame[1], frame[2] = frame[1] + 1, True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.blocks += 1
            self.frames[-1][2] = False
            if tag not in _VOID_TAGS:
                self.frames.append([tag, 0, False])
        elif tag == "summary":
            self.frames.append([tag, 0, False])
        else:
            self._inline()

    def handle_endtag(self, tag: str) -> None:
        if len(self.frames) > 1 and self.frames[-1][0] == tag:
            self._close(self.frames.pop())

    def _close(self, frame: list[Any]) -> None:
        tag, runs, _open = frame
        if tag in _RUN_CONTAINERS:
            self.blocks += runs  # each loose inline run becomes its own paragraph

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._inline()

    def total(self) -> int:
        self.close()
        while len(self.frames) > 1:
            self._close(self.frames.pop())
        self._close(self.frames[0])
        return self.blocks


def count_blocks(html: str) -> int:
    """Rich message blocks in ``html`` (block tags, list items, quotes, details and implicit paragraphs)."""
    counter = _BlockCounter()
    counter.feed(html)
    return counter.total()


def fits(html: str) -> bool:
    """True when ``html`` is within the safety budget (and so within the rich message limits)."""
    return _units(html) <= BUDGET_CHARS and count_blocks(html) <= BUDGET_BLOCKS


class _TextRenderer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.buf: list[str] = []
        self.quote = 0
        self.lists = 0
        self.details = 0

    def flush(self) -> None:
        line = " ".join("".join(self.buf).split())
        self.buf = []
        if line:
            self.lines.append("> " * self.quote + line)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS or tag in ("br", "summary", "cite"):
            self.flush()
        if tag == "li":
            self.buf.append("  " * max(self.lists - 1, 0) + "• ")
        elif tag in ("ul", "ol"):
            self.lists += 1
        elif tag == "blockquote":
            self.quote += 1
        elif tag == "cite":
            self.buf.append("— ")
        elif tag == "details":
            self.details += 1
        elif tag == "summary" and self.details == 1 and self.lines:
            self.lines.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS or tag in ("summary", "cite"):
            self.flush()
        if tag in ("ul", "ol"):
            self.lists = max(self.lists - 1, 0)
        elif tag == "blockquote":
            self.quote = max(self.quote - 1, 0)
        elif tag == "details":
            self.details = max(self.details - 1, 0)

    def handle_data(self, data: str) -> None:
        self.buf.append(data)


def html_to_text(html: str, limit: int = TEXT_MAX_CHARS) -> str:
    """Plain-text rendering of our rich HTML, clipped to ``limit`` with an ellipsis."""
    renderer = _TextRenderer()
    renderer.feed(html)
    renderer.close()
    renderer.flush()
    return _clip_text("\n".join(renderer.lines).strip(), limit)


def _view(html: str, buttons: Rows = (), notice: str | None = None) -> View:
    return View(html, html_to_text(html), buttons, notice)


# ---------------------------------------------------------------------------
# Markdown -> rich HTML
# ---------------------------------------------------------------------------

# Fence lines (with at most a language word, so "```x``` y" keeps its text) and rule lines.
_DROP_RE = re.compile(r"\s*(?:`{3,}|~{3,})\s*[\w+#.-]*\s*|\s*([-*_])(?:\s*\1){2,}\s*")
_HEADING_RE = re.compile(r"\s*#{1,6}(?:\s+(.*?))?(?:\s+#+)?\s*")
_TABLE_RE = re.compile(r"\s*\|(.*)\|\s*")
_TABLE_RULE_RE = re.compile(r":?-+:?")
_QUOTE_RE = re.compile(r"\s*(?:>\s?)+")
_BULLET_RE = re.compile(r"(\s*)[-*+•](?:\s+(.*))?")
_CODE_RE = re.compile(r"(`+)([^`]+)\1")
_LINK_RE = re.compile(r"!?\[([^\[\]]+)\]\([^()]*\)")
_STRIKE_RE = re.compile(r"~~(?=\S)([^~]+?)(?<=\S)~~")
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(?=[^\s*])([^*<>]+?)(?<=[^\s*])\*\*\*")
_BOLD_RE = re.compile(r"\*\*(?=[^\s*])([^<>]+?)(?<=[^\s*])\*\*|(?<!\w)__(?=[^\s_])([^<>]+?)(?<=[^\s_])__(?!\w)")
# Italic may hold a finished <b>…</b>, never part of one, so the tags always nest.
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])((?:[^<>*]|<b>[^<>]*</b>)+?)(?<=[^\s*])\*(?![\w*])"
                        r"|(?<![\w_])_(?=[^\s_])((?:[^<>_]|<b>[^<>]*</b>)+?)(?<=[^\s_])_(?![\w_])")


def _lines(md: str) -> list[str]:
    """Normalised lines: controls, bidi characters and refs removed; end markers, fences and rules blank."""
    text = str(md).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "  ")
    lines = []
    for raw in _CONTROL_RE.sub("", text).split("\n"):
        line = _writer.REF_RE.sub("", raw).rstrip()
        lines.append("" if line.strip() == _writer.END_MARKER or _DROP_RE.fullmatch(line) else line)
    return lines


def _inline(text: str) -> str:
    """One escaped line with Markdown emphasis as <b> and <i>; markers without a partner stay literal."""
    out = _html.escape(text.strip(), quote=False)
    out = _CODE_RE.sub(r"\2", out)
    out = _LINK_RE.sub(r"\1", out)  # no <a> in the subset: a contact's link stays inert text
    out = _STRIKE_RE.sub(r"\1", out)
    out = _BOLD_ITALIC_RE.sub(r"<b><i>\1</i></b>", out)
    out = _BOLD_RE.sub(lambda m: f"<b>{m[1] or m[2]}</b>", out)
    return _ITALIC_RE.sub(lambda m: f"<i>{m[1] or m[2]}</i>", out)


def _blocks(lines: list[str]) -> str:
    out: list[str] = []
    kind, items, base = "", [], 0  # the open block ("p", "ul" or "quote"), its lines or items, the list indent

    def close() -> None:
        nonlocal kind, items
        if items and kind == "ul":
            out.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
        elif items:
            tag = "blockquote" if kind == "quote" else "p"
            out.append(f"<{tag}>{'<br>'.join(items)}</{tag}>")
        kind, items = "", []

    def open_block(name: str) -> None:
        nonlocal kind
        if kind != name:
            close()
            kind = name

    for line in lines:
        if not line.strip():
            close()
            continue
        if heading := _HEADING_RE.fullmatch(line):
            close()
            if text := _inline(heading[1] or ""):
                out.append(f"<h4>{text}</h4>")
            continue
        if table := _TABLE_RE.fullmatch(line):
            close()
            cells = [cell.strip() for cell in table[1].split("|")]
            if not all(_TABLE_RULE_RE.fullmatch(cell) for cell in cells if cell):  # also drops an empty row
                out.append(f"<p>{' · '.join(_inline(cell) for cell in cells if cell)}</p>")
            continue
        if quote := _QUOTE_RE.match(line):
            open_block("quote")
            if text := _inline(line[quote.end():]):
                items.append(text)
            continue
        if bullet := _BULLET_RE.fullmatch(line):
            if kind != "ul":
                open_block("ul")
                base = len(bullet[1])
            if text := _inline(bullet[2] or ""):
                items.append(("– " if len(bullet[1]) >= base + 2 else "") + text)  # nested items are flattened
            continue
        if kind == "ul" and items and line[0].isspace():
            items[-1] += "<br>" + _inline(line)
            continue
        open_block("p")
        items.append(_inline(line))
    close()
    return "".join(out)


def _plain_html(lines: list[str]) -> str:
    """The guard's fallback: every non-blank line escaped, in one paragraph."""
    shown = [_html.escape(line.strip(), quote=False) for line in lines if line.strip()]
    return f"<p>{'<br>'.join(shown)}</p>" if shown else ""


def markdown_html(md: str) -> str:
    """Rich HTML for a Markdown dossier, in the subset ``h4 p b i br ul li blockquote``.

    Refs like ``[m123]`` and the ``<<END>>`` marker are removed. Headings become ``h4`` (``h3`` is the
    screen title), nested bullets are flattened, numbered lines and table rows become paragraph lines,
    and links keep only their text. If the result does not fit the rich budget, the lines are sent as
    plain escaped text instead.
    """
    lines = _lines(md)
    html = _blocks(lines)
    return html if fits(html) else _plain_html(lines)


def _framed(before: str, md: str, after: str) -> str:
    """``before`` + the converted ``md`` + ``after``, with the plain fallback when the whole does not fit."""
    html = before + markdown_html(md) + after
    return html if fits(html) else before + _plain_html(_lines(md)) + after


# ---------------------------------------------------------------------------
# Owner panel screens
# ---------------------------------------------------------------------------


def _doc_text(doc: dict[str, Any] | None, key: str) -> str:
    value = doc.get(key) if doc else None
    return value if isinstance(value, str) else ""


def _header(snap: DossierSnapshot, doc: dict[str, Any], at: datetime, lang: str) -> str:
    s = _s(lang)
    time = _tg_time(at, "r", f"{_date(at, lang)}, {at:%H:%M} UTC")
    n = _count(doc.get("messages"))
    if snap.target == "persona":
        m = len(doc["chats"]) if isinstance(doc.get("chats"), list) else 0
        return s["head_persona"].format(time=time, n=n, m=m, chats=_plural(lang, m, s["forms"]["chats"]))
    return s["head_contact"].format(time=time, n=n, messages=_plural(lang, n, s["forms"]["messages"]))


def _note_html(note: str, label: str) -> str:
    """The owner note as a quote: line breaks kept (at most NOTE_DISPLAY_LINES), clipped with "…"."""
    lines = [" ".join(line.split()) for line in _CONTROL_RE.sub(" ", note).split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    if len(lines) > NOTE_DISPLAY_LINES:
        lines[NOTE_DISPLAY_LINES - 1:] = [" ".join(lines[NOTE_DISPLAY_LINES - 1:])]
    shown = _clip_text("\n".join(lines), NOTE_DISPLAY_UNITS).split("\n")
    return f"<blockquote><b>{label}</b><br>{'<br>'.join(_html.escape(line, quote=False) for line in shown)}</blockquote>"


def _off(snap: DossierSnapshot, s: dict[str, Any]) -> str:
    """Dossiers are off: only a manager has the Settings switch, so anyone else learns who can turn them on."""
    if snap.manage_global:
        return s["st_off"]
    return "<br>".join((s["st_off"], s["notice"]["operator_only"], s["off_cli"]))


def _failed_none(snap: DossierSnapshot, s: dict[str, Any]) -> str:
    key = "st_failed_none_persona" if snap.target == "persona" else "st_failed_none"
    return s[key].format(error=_e(snap.failure, ERROR_CHARS))


def _status(snap: DossierSnapshot, lang: str, *, empty: bool, converted: bool = False) -> str:
    """The one status line under a document, by priority; "" when there is nothing to say.

    ``converted`` marks a document the migration wrote from v1 facts that no refresh has rewritten yet.
    """
    s = _s(lang)
    if not snap.enabled:
        return _off(snap, s)
    if not snap.history_enabled:
        return s["st_history_off"]
    if snap.chat_off and snap.target == "contact":
        return s["note_chat_off"]
    if not snap.llm_ready:
        return s["st_no_model"]
    if snap.refreshing:
        return s["st_refreshing"]
    if snap.failure:  # an empty document has no earlier version to show
        return _failed_none(snap, s) if empty else s["st_failed"].format(error=_e(snap.failure, ERROR_CHARS))
    if converted:  # Refresh is hidden while a first build has nothing to read
        return s["st_converted_waiting" if snap.unqualified or not snap.in_history else "st_converted"]
    return s["st_empty"] if empty else ""


def _target_arg(snap: DossierSnapshot) -> str:
    """``--connection … --chat …`` for a CLI line on the screen: shell-quoted, clipped and HTML-escaped.

    A contact's chat id is the same under every owner, so the connection picks the chat this screen shows.
    """
    chat = f"--chat {shlex.quote(_clip(snap.chat_id, 64))}"
    if snap.connection:
        chat = f"--connection {shlex.quote(_clip(snap.connection, 64))} {chat}"
    return _html.escape(chat, quote=False)


def _body(snap: DossierSnapshot, lang: str) -> str:
    """The state text that stands in for a document that is not there."""
    s, persona = _s(lang), snap.target == "persona"
    if snap.corrupt:
        if persona:
            return s["st_corrupt_persona"]
        return s["st_corrupt"].format(target=_target_arg(snap))
    if not snap.enabled:
        return _off(snap, s)
    if not snap.history_enabled:
        return s["st_history_off"]
    if snap.chat_off and not persona:
        return s["note_chat_off"]
    if not snap.llm_ready:
        return s["st_no_model"]
    if snap.refreshing:
        return s["st_refreshing"]
    if snap.failure:
        return _failed_none(snap, s)
    if persona:
        return s["st_persona_due" if snap.persona_due else "st_persona_missing"]
    if not snap.in_history:  # Refresh needs the chat in history, so st_missing would point to a missing button
        return s["st_not_in_history"]
    return s["st_unqualified"] if snap.unqualified else s["st_missing"]


def _buttons(snap: DossierSnapshot, lang: str, *, shown: bool) -> Rows:
    """Rows under a screen; ``shown`` is True when the screen shows a built document rather than a state.

    A document converted from v1 is not built yet: like a missing one, it has no Refresh while unqualified.
    """
    s = _s(lang)
    if snap.target == "persona":
        return _rows([(s["b_back_dossier"], build_args("overview"))])
    row = []
    if (snap.enabled and snap.history_enabled and not snap.chat_off and snap.llm_ready and snap.in_history
            and not snap.refreshing and not snap.corrupt and (shown or not snap.unqualified)):
        row.append((s["b_refresh"], build_args("refresh")))
    if snap.manage_global:
        row.append((s["b_persona"], build_args("overview", "persona")))
    return _rows(row)


def render_overview(snap: DossierSnapshot, lang: str) -> View:
    """The dossier screen of a chat, or the persona screen: title, header, document, note and status.

    The header appears once messages were processed: ``checked_at`` for a contact, ``updated_at`` (the
    last build) for the persona. Without a document the state text is the body. Only the document and
    the note are shown: ``previous`` and every other field stay out of Telegram. Only a document converted
    from v1 can be longer than the limit: the screen shows its start and names ``show`` for the rest.
    """
    if snap.target not in TARGETS:
        raise ValueError(f"unknown dossier target {snap.target!r}")
    s, persona = _s(lang), snap.target == "persona"
    doc = snap.doc if isinstance(snap.doc, dict) and not snap.corrupt else None
    title = s["title_persona"] if persona else (_e(snap.name, NAME_CHARS) or "?")
    head = f"<h3>{title}</h3>"
    text = _doc_text(doc, "document")
    clipped = _units(text) > _doc.DOC_MAX_UNITS
    document = _clip_text(text, CLIPPED_DOC_UNITS if clipped else _doc.DOC_MAX_UNITS)
    converted = doc is not None and not persona and _doc.converted(doc)
    at = _dt(_doc_text(doc, "updated_at" if persona else "checked_at"))
    note = _note_html(_doc_text(doc, "note"), s["persona_note_label" if persona else "note_label"])
    shown = doc is not None and (bool(document) or at is not None)
    if shown:
        if at is not None:
            head += f"<p><i>{_header(snap, doc, at, lang)}</i></p>"
        status = _status(snap, lang, empty=not document, converted=converted)
        after = f"<p><i>{s['st_clipped'].format(target=_target_arg(snap))}</i></p>" if clipped else ""
        html = _framed(head, document, after + note + (f"<p><i>{status}</i></p>" if status else ""))
    else:
        html = f"{head}<p>{_body(snap, lang)}</p>{note}"
    return _view(html, _buttons(snap, lang, shown=shown and not converted))


def notice_text(key: str, lang: str, **fmt: Any) -> str:
    """Short plain toast text for NOTICE_KEYS (<= 200 chars, no HTML); ``error`` fills "failed"."""
    if key not in NOTICE_KEYS:
        raise ValueError(f"unknown notice {key!r}")
    return _clip(_s(lang)["notice"][key].format(error=_clip(fmt.get("error"), ERROR_CHARS) or "?"), 200)


def render_state(key: str, lang: str, target: str = "contact", **fmt: Any) -> View:
    """A one-line screen for NOTICE_KEYS (busy, refresh_started ...) with the same text as its notice."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    s, notice = _s(lang), notice_text(key, lang, **fmt)
    retry = () if key == "no_connection" else _rows([(s["b_dossier"], build_args("overview", target))])
    return _view(f"<p>{_html.escape(notice)}</p>", retry, notice)


def enable_lines(cfg: Any, lang: str) -> list[str]:
    """What turning dossiers on sends where, from a ``DossierConfig``; Settings and both CLI switches show it.

    The model, the task route sentence only while the task is in use, and the message check and fact
    extraction only while they run.
    """
    s, provider, model = _s(lang), cfg.provider, cfg.model
    name = _clip(f"{provider} / {model}" if provider and model else model or provider or "?", 120)
    main = provider == "auto" and not cfg.legacy_env  # the legacy 'auto' route still names its model
    lines = [s["enable_explain"].format(model=s["main_model"] if main else name)]
    if cfg.task_registered and not cfg.legacy_env:  # an explicit route has no task to change in 'hermes model'
        lines.append(s["enable_route"])
    if cfg.gate == "on":
        lines.append(s["enable_gate"])
    if cfg.clairic == "on":
        lines.append(s["enable_clairic"])
    return lines


def enable_confirm(cfg: Any, lang: str) -> tuple[str, str, str]:
    """Plain (text, confirm label, cancel label) for turning dossiers on; Settings adds its own callbacks."""
    s = _s(lang)
    return "\n\n".join([s["enable_q"], *enable_lines(cfg, lang), s["enable_keep"]]), s["b_enable_yes"], s["b_cancel"]


# ---------------------------------------------------------------------------
# /dossier: the post in the chat and the owner's message when nothing was posted (or maybe was)
# ---------------------------------------------------------------------------


def _post_lang(text: str) -> str:
    """The contact reads the post, so it follows the dossier: 'ru' when enough of its letters are Cyrillic."""
    letters = [char for char in _writer.REF_RE.sub("", text) if char.isalpha()]  # "[m12]" is not a word
    cyrillic = sum(1 for char in letters if "\u0400" <= char <= "\u04ff")
    return "ru" if letters and cyrillic >= POST_RU_SHARE * len(letters) else "en"


def post_html(document_md: str, now: datetime) -> str:
    """The ``/dossier`` post: a fixed title, the document without its refs, and a dated footer, in the
    document's language.

    Only the document text goes in: never the note, the previous version, the persona or any other field.
    ``now`` is the owner's local time; its date is printed as it is.
    """
    lang = _post_lang(document_md)
    s = _s(lang)
    footer = s["post_footer"].format(date=_date(now, lang))
    return _framed(f"<h3>{s['post_title']}</h3>", document_md, f"<p><i>{footer}</i></p>")


def post_refusal(reason: str, lang: str, *, name: str, error: str | None = None) -> str:
    """Plain text for the owner when ``/dossier`` posted nothing: the head line, then the reason line.

    ``send_unsure`` is a send that timed out or lost its connection: the post may be in the chat, so its
    head says so instead of "nothing was posted".
    """
    if reason not in POST_REASONS:
        raise ValueError(f"unknown post reason {reason!r}")
    s = _s(lang)
    head = s["post_head_unsure" if reason == "send_unsure" else "post_head"]
    head = head.format(name=_clip(name, NAME_CHARS) or "?")
    return f"{head}\n{s['post'][reason].format(error=_clip(error, ERROR_CHARS) or '?')}"
