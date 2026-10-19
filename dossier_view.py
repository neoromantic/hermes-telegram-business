"""Pure Telegram views of contact dossiers and the owner persona (owner panel and ``/dossier``).

No I/O: no files, history, env or clock (``now`` comes in the snapshot). The I/O layer builds a
``DossierSnapshot`` and sends ``View.html`` as a rich message (``rich_message(view)``), ``View.text``
as the plain fallback and ``View.buttons`` as inline keyboard rows of (label, callback args).
Every dynamic string is clipped and stripped of control/bidi characters (``dossier_support.clip``),
then HTML-escaped. Rich HTML stays within a safety budget below the Bot API rich message limits;
plain text stays within 4096 UTF-16 units. Callback args are short ASCII and ``parse_args`` rejects
anything else, because they come back from Telegram.
"""

from __future__ import annotations

import html as _html
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from . import dossier_support as _ds

RICH_MAX_CHARS = 32_768  # Bot API rich message text limit
RICH_MAX_BLOCKS = 500  # incl. nested blocks, list items, quotations and details
BUDGET_CHARS = 30_000  # whole HTML string in UTF-16 units, so the parsed text is always under the limit
BUDGET_BLOCKS = 450
TEXT_MAX_CHARS = 4096
ARGS_MAX_BYTES = 30  # the panel adds its own prefix; Telegram callback_data is <= 64 bytes
PAGE_SIZE = 24
PAGE_COLUMNS = 4
MAX_PAGE = 99
NAME_CHARS = 128
FACT_CHARS = _ds.MAX_OWNER_TEXT_CHARS
QUOTE_CHARS = _ds.MAX_QUOTE_CHARS + 40
NOW_TEXT_CHARS = 300
PICKER_TEXT_CHARS = 100
CARD_MAX_SOURCES = 10
CLI = "hermes telesec dossier"
CONTACT_ORDER = ("commitments", "open_questions", "decisions", "contradictions",
                 "preferences", "relationship", "shared_facts")  # action sections first
ACTION_SECTIONS = frozenset(CONTACT_ORDER[:4])
TARGETS = {"contact": "c", "persona": "p"}
POST_BLOCKERS = ("persona", "disabled", "corrupt", "missing", "unchecked", "empty")
POST_REASONS = (*POST_BLOCKERS, "send_failed")
NOTICE_KEYS = ("changed", "busy", "refresh_started", "unavailable",
               "pinned", "unpinned", "removed", "failed", "operator_only", "no_connection")

Rows = tuple[tuple[tuple[str, str], ...], ...]

# ---------------------------------------------------------------------------
# Strings (constants only: no "<", ">", "&" or quotes, so they are HTML-safe as is)
# ---------------------------------------------------------------------------

STRINGS: dict[str, dict[str, Any]] = {
    "en": {
        "sections": {"commitments": "Commitments", "open_questions": "Open questions", "decisions": "Decisions",
                     "contradictions": "Contradictions", "preferences": "Preferences",
                     "relationship": "Relationship", "shared_facts": "Shared facts", "language": "Language",
                     "tone": "Tone", "vocabulary": "Vocabulary", "formatting": "Formatting",
                     "reply_length": "Reply length", "greetings": "Greetings", "boundaries": "Boundaries"},
        "months": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        "just_now": "just now", "min_ago": "{n} min ago", "h_ago": "{n} h ago", "d_ago": "{n} d ago",
        "mo_ago": "{n} mo ago", "y_ago": "{n} y ago",
        "persona_title": "My persona",
        "updated": {"contact": "Dossier updated", "persona": "Persona updated"},
        "never": {"contact": "Dossier not refreshed by the model yet",
                  "persona": "Persona not refreshed by the model yet"},
        "revision": "revision {n}",
        "coverage": "Coverage", "no_coverage": "no model pass yet", "messages": ("message", "messages"),
        "in_chats": "in {chats}", "chats_in": ("chat", "chats"), "older": "older history not sent",
        "model": "Model",
        "count_facts": "Facts", "count_stale": "stale", "count_unsupported": "unsupported",
        "count_contradictions": "contradictions",
        "note_off": "Processing is off: this dossier is not updated. Turn dossiers on in Settings.",
        "note_history_off": "History is off, so dossiers are paused. Turn it on in Settings.",
        "note_no_llm": "The model is not connected in this process, so refresh is unavailable.",
        "note_refreshing": "Refreshing now…",
        "note_busy": "A processing cycle is running; the dossier may change shortly.",
        "note_failed": "The last refresh failed: {error}.",
        "note_stale_error": "Could not check sources against history: {error}.",
        "legend": "✎ owner edit · 📌 pinned · ~ model guess · ⚠ source changed",
        "legend_contact": "✎ owner edit · 📌 pinned · ~ model guess",
        "cli_hint": f"Corrections, notes, history and export: {CLI}",
        "sources": "Sources", "unsupported_title": "Unsupported",
        "unsupported_hint": "Removed on the next refresh unless new messages support them.",
        "more": "… {n} more {word}", "fact_forms": ("fact", "facts"), "source_forms": ("source", "sources"),
        "more_cli": f", see {CLI} show",
        "side": {"contact": "contact", "owner": "owner", "transcript": "transcript", "unknown": "unknown"},
        "deleted": "deleted",
        "change": {"edited": "edited", "deleted": "deleted", "undeleted": "restored", "gone": "gone",
                   "replaced": "replaced"},
        "now": "Now in history", "now_edited": "Edited, now",
        "state_old": "Older than the tracking window, not re-read", "state_gone": "No longer in history",
        "state_replaced": "Replaced by another message",
        "source_gone": "source gone", "conf": {"low": "low", "medium": "medium", "high": "high"},
        "off_title": "Dossiers are off. Turn them on in Settings.",
        "off_explain": "When they are on, the bot keeps a private dossier for each private chat: commitments, "
                       "decisions, open questions and more, each linked to the messages it comes from.",
        "off_privacy": "Turning them on sends the text of all private chats to {model}.",
        "off_cli": f"From the terminal: {CLI} enable",
        "no_dossier": {"contact": "No dossier yet.", "persona": "No persona yet."},
        "reason_unqualified": "It needs at least one received and one sent text message in the last {days}.",
        "day_forms": ("day", "days"),
        "reason_pending": {"contact": "It will be built in the next processing cycle.",
                           "persona": "It is built from your sent messages across private chats "
                                      "in the next processing cycle."},
        "reason_failed": "The last attempt failed: {error}.",
        "corrupt": {"contact": "The stored dossier file can't be read.",
                    "persona": "The stored persona file can't be read."},
        "corrupt_fix": "Reset it from the terminal:",
        "b_manage": "Manage facts", "b_refresh": "Refresh", "b_persona": "My persona",
        "b_back_dossier": "‹ Dossier", "b_back_persona": "‹ My persona", "b_back_facts": "‹ Facts", "b_pin": "Pin", "b_unpin": "Unpin", "b_remove": "Remove",
        "b_remove_yes": "Remove", "b_cancel": "Cancel", "b_enable_yes": "Enable", "b_dossier": "Open dossier",
        "b_prev": "‹ {n}", "b_next": "{n} ›",
        "kind_observed": "Observed in messages", "kind_inferred": "Model guess, {conf} confidence",
        "kind_owner": "Corrected by the owner", "kind_note": "Owner note",
        "card_pinned": "Pinned: the model never changes it.",
        "card_unsupported": "Unsupported: removed on the next refresh unless new messages support it.",
        "card_source_gone": "Its sources are gone from history.",
        "card_stale": "Sources changed since the last refresh: {changes}.",
        "previous": "Before the correction", "revisions": "Revisions {a}–{b}", "no_sources": "No sources.",
        "picker_page": "Facts, page {p} of {n}", "picker_hint": "Pick a fact to pin, unpin or remove it.",
        "picker_empty": "No facts yet.",
        "remove_q": "Remove {fid}?",
        "remove_explain": "The model will not add this fact again. This can't be undone in Telegram.",
        "enable_q": "Turn on dossiers?",
        "enable_explain": "The text of ALL private chats will be sent to {model} to build a dossier for each "
                          "contact and your persona.",
        "enable_keep": "Dossiers are stored locally. You can turn them off at any time; stored dossiers are kept.",
        "notice": {"changed": "This dossier changed. Open it again.",
                   "busy": "Another dossier cycle is running. Try again in a minute.",
                   "refresh_started": "Refresh started. Open the dossier again in a minute.",
                   "unavailable": "Refresh is unavailable: dossiers or history are off, or the model is not connected.",
                   "pinned": "{fid} pinned.", "unpinned": "{fid} unpinned.", "removed": "{fid} removed.",
                   "failed": "That did not work ({error}).",
                   "operator_only": "Only the owner of every Business connection the bot knows about can open "
                                    "the persona or turn dossiers on and off.",
                   "no_connection": "The Business connection of this chat is not known yet. Send any message "
                                    "in the chat, then open Manage Bot again."},
        "post_head": "Nothing was posted to the chat with {name}.",
        "post": {"persona": "The persona is never posted to a chat.", "disabled": "Dossiers are off.",
                 "empty": "The dossier has no facts to show yet.",
                 "unchecked": "Its sources could not be checked against the chat history ({error}), so it might "
                              "quote a deleted or edited message. Try again later.",
                 "send_failed": "Telegram did not accept the dossier message ({error}). A bot can post in a "
                                "Business chat only if the contact wrote there in the last 24 hours."},
    },
    "ru": {
        "sections": {"commitments": "Обязательства", "open_questions": "Открытые вопросы", "decisions": "Решения",
                     "contradictions": "Противоречия", "preferences": "Предпочтения",
                     "relationship": "Отношения", "shared_facts": "Общие факты", "language": "Язык",
                     "tone": "Тон", "vocabulary": "Словарь", "formatting": "Оформление",
                     "reply_length": "Длина ответов", "greetings": "Приветствия", "boundaries": "Границы"},
        "months": ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"),
        "just_now": "только что", "min_ago": "{n} мин назад", "h_ago": "{n} ч назад", "d_ago": "{n} дн. назад",
        "mo_ago": "{n} мес. назад", "y_ago": "{n} г. назад",
        "persona_title": "Моя персона",
        "updated": {"contact": "Досье обновлено", "persona": "Персона обновлена"},
        "never": {"contact": "Модель ещё не обновляла досье", "persona": "Модель ещё не обновляла персону"},
        "revision": "ревизия {n}",
        "coverage": "Охват", "no_coverage": "модель ещё не читала переписку",
        "messages": ("сообщение", "сообщения", "сообщений"),
        "in_chats": "в {chats}", "chats_in": ("чате", "чатах", "чатах"),
        "older": "более ранняя история не отправлялась",
        "model": "Модель",
        "count_facts": "Фактов", "count_stale": "устарели", "count_unsupported": "без подтверждения",
        "count_contradictions": "противоречий",
        "note_off": "Обработка выключена: досье не обновляется. Включить можно в настройках.",
        "note_history_off": "История выключена, поэтому досье на паузе. Включите её в настройках.",
        "note_no_llm": "Модель не подключена в этом процессе, поэтому обновление недоступно.",
        "note_refreshing": "Идёт обновление…",
        "note_busy": "Сейчас идёт цикл обработки, досье может скоро измениться.",
        "note_failed": "Последнее обновление не удалось: {error}.",
        "note_stale_error": "Не удалось сверить источники с историей: {error}.",
        "legend": "✎ правка владельца · 📌 закреплено · ~ догадка модели · ⚠ источник изменился",
        "legend_contact": "✎ правка владельца · 📌 закреплено · ~ догадка модели",
        "cli_hint": f"Исправления, заметки, история и экспорт: {CLI}",
        "sources": "Источники", "unsupported_title": "Без подтверждения",
        "unsupported_hint": "Удалятся при следующем обновлении, если новые сообщения их не подтвердят.",
        "more": "… ещё {n} {word}", "fact_forms": ("факт", "факта", "фактов"),
        "source_forms": ("источник", "источника", "источников"), "more_cli": f" — полностью: {CLI} show",
        "side": {"contact": "собеседник", "owner": "владелец", "transcript": "расшифровка",
                 "unknown": "неизвестно"},
        "deleted": "удалено",
        "change": {"edited": "изменено", "deleted": "удалено", "undeleted": "восстановлено", "gone": "пропало",
                   "replaced": "заменено"},
        "now": "Сейчас в истории", "now_edited": "Изменено, сейчас",
        "state_old": "Старше окна отслеживания, не перечитывалось", "state_gone": "Больше нет в истории",
        "state_replaced": "Заменено другим сообщением",
        "source_gone": "нет источника", "conf": {"low": "низкая", "medium": "средняя", "high": "высокая"},
        "off_title": "Досье выключены. Включить можно в настройках.",
        "off_explain": "Когда они включены, бот ведёт закрытое досье по каждому личному чату: обязательства, "
                       "решения, открытые вопросы и другое, со ссылками на сообщения.",
        "off_privacy": "После включения текст всех личных чатов отправляется в {model}.",
        "off_cli": f"Из терминала: {CLI} enable",
        "no_dossier": {"contact": "Досье пока нет.", "persona": "Персоны пока нет."},
        "reason_unqualified": "Нужно хотя бы одно полученное и одно отправленное текстовое сообщение "
                              "за последние {days}.",
        "day_forms": ("день", "дня", "дней"),
        "reason_pending": {"contact": "Оно появится в следующем цикле обработки.",
                           "persona": "Она строится по вашим отправленным сообщениям из личных чатов "
                                      "в следующем цикле обработки."},
        "reason_failed": "Последняя попытка не удалась: {error}.",
        "corrupt": {"contact": "Файл досье не читается.", "persona": "Файл персоны не читается."},
        "corrupt_fix": "Сбросить его можно в терминале:",
        "b_manage": "Управлять фактами", "b_refresh": "Обновить", "b_persona": "Моя персона",
        "b_back_dossier": "‹ Досье", "b_back_persona": "‹ Моя персона", "b_back_facts": "‹ Факты", "b_pin": "Закрепить",
        "b_unpin": "Открепить", "b_remove": "Удалить", "b_remove_yes": "Да, удалить", "b_cancel": "Отмена",
        "b_enable_yes": "Включить", "b_dossier": "Открыть досье", "b_prev": "‹ {n}", "b_next": "{n} ›",
        "kind_observed": "Прямо сказано в сообщениях", "kind_inferred": "Догадка модели, уверенность {conf}",
        "kind_owner": "Исправлено владельцем", "kind_note": "Заметка владельца",
        "card_pinned": "Закреплено: модель его не меняет.",
        "card_unsupported": "Без подтверждения: удалится при следующем обновлении, если новые сообщения "
                            "его не подтвердят.",
        "card_source_gone": "Его источников больше нет в истории.",
        "card_stale": "Источники изменились после обновления: {changes}.",
        "previous": "До исправления", "revisions": "Ревизии {a}–{b}", "no_sources": "Источников нет.",
        "picker_page": "Факты, страница {p} из {n}",
        "picker_hint": "Выберите факт, чтобы закрепить, открепить или удалить его.",
        "picker_empty": "Фактов пока нет.",
        "remove_q": "Удалить {fid}?",
        "remove_explain": "Модель больше не добавит этот факт. Отменить удаление в Telegram нельзя.",
        "enable_q": "Включить досье?",
        "enable_explain": "Текст ВСЕХ личных чатов будет отправляться в {model}, чтобы собрать досье "
                          "по каждому собеседнику и вашу персону.",
        "enable_keep": "Досье хранятся локально. Выключить их можно в любой момент, сохранённые досье останутся.",
        "notice": {"changed": "Досье изменилось. Откройте его заново.",
                   "busy": "Сейчас идёт другой цикл обработки. Попробуйте через минуту.",
                   "refresh_started": "Обновление запущено. Откройте досье через минуту.",
                   "unavailable": "Обновление недоступно: досье или история выключены, либо модель не подключена.",
                   "pinned": "{fid} закреплён.", "unpinned": "{fid} откреплён.", "removed": "{fid} удалён.",
                   "failed": "Не получилось ({error}).",
                   "operator_only": "Персона и включение досье доступны только владельцу всех бизнес-подключений, "
                                    "известных боту.",
                   "no_connection": "Business-подключение этого чата пока неизвестно. Отправьте любое "
                                    "сообщение в том чате и снова нажмите кнопку управления ботом."},
        "post_head": "В чат «{name}» ничего не отправлено.",
        "post": {"persona": "Персона никогда не отправляется в чат.", "disabled": "Досье выключены.",
                 "empty": "В досье пока нет фактов, которые можно показать.",
                 "unchecked": "Не удалось сверить источники с историей чата ({error}), и в сообщение могла "
                              "попасть цитата удалённого или изменённого сообщения. Попробуйте позже.",
                 "send_failed": "Telegram не принял сообщение с досье ({error}). Бот может писать в бизнес-чат, "
                                "только если собеседник писал туда за последние 24 часа."},
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
    """Everything a view needs, gathered by the I/O layer (load_for_target, load_state, compute_stale ...)."""

    kind: str  # contact | persona
    label: str  # contact display name (ignored for the persona)
    dossier: dict[str, Any] | None
    now: datetime
    status: str = "ok"  # ok | missing | corrupt
    missing_reason: str | None = None  # unqualified | pending | failed (dossier is None)
    failure_error: str | None = None  # error type of the last failed attempt (state failures)
    enabled: bool = True
    llm_ready: bool = True
    refreshing: bool = False  # a refresh of this chat runs now
    cycle_busy: bool = False  # some dossier cycle runs now
    provider: str = ""
    model: str = ""
    backfill_days: int = 90
    stale: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # compute_stale()
    sources_now: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # fact id -> sources_now()
    chat_names: dict[str, str] = field(default_factory=dict)  # persona chat alias -> display name
    stale_error: str | None = None  # error type when the history fold failed
    business_connection_id: str | None = None  # for the reset command of an unreadable file
    chat_id: Any = None
    manage_global: bool = True  # may open the persona and switch dossiers (it owns every connected account)
    history_enabled: bool = True  # history capture is on; dossiers are built from it
    in_history: bool = True  # the chat is a private chat in the history catalog (a refresh needs it)


@dataclass(frozen=True)
class Action:
    """A parsed callback: name in ACTION_NAMES; prefix/fact_id only on fact actions, page only on manage."""

    name: str
    target: str = "contact"  # contact | persona
    prefix: str | None = None  # first 6 hex chars of dossier_id
    fact_id: str | None = None  # "f12"
    page: int = 0


# ---------------------------------------------------------------------------
# Callback args: "<code>[.<c|p>[.<page> | .<prefix>.<fact number>]]", [a-z0-9.], <= 30 bytes
# ---------------------------------------------------------------------------

_CODES = {"overview": "o", "manage": "m", "fact": "f", "pin": "pn", "unpin": "up", "remove_ask": "ra",
          "remove_do": "rd", "refresh": "rf", "enable_ask": "ea", "enable_do": "ed", "disable": "ds"}
_NAMES = {code: name for name, code in _CODES.items()}
ACTION_NAMES = tuple(_CODES)
_FACT_ACTIONS = frozenset({"fact", "pin", "unpin", "remove_ask", "remove_do"})
_PLAIN_ACTIONS = frozenset({"enable_ask", "enable_do", "disable"})
_TARGET_NAMES = {letter: name for name, letter in TARGETS.items()}
ARGS_RE = re.compile(r"[a-z0-9.]{1,30}")
_PREFIX_RE = re.compile(r"[0-9a-f]{6}")
_FID_RE = re.compile(r"f([1-9][0-9]{0,5})")
_NUM_RE = re.compile(r"[1-9][0-9]{0,5}")
_PAGE_RE = re.compile(r"0|[1-9][0-9]?")


def dossier_prefix(dossier: dict[str, Any]) -> str:
    """First 6 chars of dossier_id ('' when it is not hex, so no fact action is offered)."""
    prefix = str(dossier.get("dossier_id") or "")[:6]
    return prefix if _PREFIX_RE.fullmatch(prefix) else ""


def build_args(name: str, target: str = "contact", *, prefix: str | None = None, fact_id: str | None = None,
               page: int = 0) -> str:
    """Callback args for an action; raises ValueError for anything parse_args would reject."""
    if name not in _CODES or target not in TARGETS:
        raise ValueError(f"unknown action {name!r} or target {target!r}")
    parts = [_CODES[name]]
    if name == "manage":
        if not isinstance(page, int) or not 0 <= page <= MAX_PAGE:
            raise ValueError(f"page must be 0..{MAX_PAGE}")
        parts += [TARGETS[target], str(page)]
    elif name in _FACT_ACTIONS:
        match = _FID_RE.fullmatch(str(fact_id or ""))
        if not match or not _PREFIX_RE.fullmatch(str(prefix or "")):
            raise ValueError("fact actions need a 6-char hex prefix and a fact id like f12")
        parts += [TARGETS[target], str(prefix), match[1]]
    elif name not in _PLAIN_ACTIONS:
        if name == "refresh" and target != "contact":
            raise ValueError("only a contact dossier can be refreshed")
        parts.append(TARGETS[target])
    args = ".".join(parts)
    if not ARGS_RE.fullmatch(args) or len(args.encode("ascii")) > ARGS_MAX_BYTES:
        raise ValueError("callback args too long")
    return args


def parse_args(data: Any) -> Action | None:
    """The action encoded by ``build_args``, or None for anything malformed."""
    if not isinstance(data, str) or not ARGS_RE.fullmatch(data):
        return None
    code, *rest = data.split(".")
    name = _NAMES.get(code)
    if name is None:
        return None
    if name in _PLAIN_ACTIONS:
        return None if rest else Action(name)
    if not rest or rest[0] not in _TARGET_NAMES:
        return None
    target = _TARGET_NAMES[rest[0]]
    if name == "manage":
        ok = len(rest) == 2 and _PAGE_RE.fullmatch(rest[1])
        return Action(name, target, page=int(rest[1])) if ok else None
    if name in _FACT_ACTIONS:
        ok = len(rest) == 3 and _PREFIX_RE.fullmatch(rest[1]) and _NUM_RE.fullmatch(rest[2])
        return Action(name, target, prefix=rest[1], fact_id=f"f{rest[2]}") if ok else None
    if len(rest) != 1 or (name == "refresh" and target != "contact"):
        return None
    return Action(name, target)


def with_notice(view: View) -> View:
    """``view`` with its notice as the first line, for a host that cannot show it as a toast."""
    if not view.notice or view.html == f"<p>{_html.escape(view.notice)}</p>":
        return view  # a render_state screen already is the notice
    html = f"<p><b>{_html.escape(view.notice)}</b></p>{view.html}"
    return View(html, _clip_text(f"{view.notice}\n\n{view.text}"), view.buttons, view.notice)


def rich_message(view: View) -> dict[str, Any]:
    """InputRichMessage for sendRichMessage / editMessageText(rich_message=...).

    Entity detection is off: fact texts quote contacts, so a "/command" or link in them must stay inert.
    """
    return {"html": view.html, "skip_entity_detection": True}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _e(value: Any, limit: int) -> str:
    return _html.escape(_ds.clip(value, limit), quote=True)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())  # 3.11+: accepts a trailing "Z"
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _day(value: Any, lang: str) -> str:
    at = _dt(value)
    return "?" if at is None else f"{at.day} {_s(lang)['months'][at.month - 1]} {at.year}"


def _ago(now: datetime, at: datetime, lang: str) -> str:
    s, secs = _s(lang), (_dt(now) or at) - at
    seconds = int(secs.total_seconds())
    for unit, size in (("y_ago", 365 * 86400), ("mo_ago", 30 * 86400), ("d_ago", 86400), ("h_ago", 3600),
                       ("min_ago", 60)):
        if seconds >= size * (2 if unit in ("y_ago", "mo_ago") else 1):
            return s[unit].format(n=seconds // size)
    return s["just_now"]


def _tg_time(at: datetime, fmt: str, body: str) -> str:
    unix = _html.escape(str(int(at.timestamp())), quote=True)
    return f'<tg-time unix="{unix}" format="{_html.escape(fmt, quote=True)}">{_html.escape(body)}</tg-time>'


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


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


def _rows(*rows: list[tuple[str, str]]) -> Rows:
    return tuple(tuple(row) for row in rows if row)


def _note(fact: dict[str, Any]) -> bool:
    return fact.get("kind") == "owner" and _ds._is_note(fact)


def _num(fact: dict[str, Any]) -> int:
    match = _FID_RE.fullmatch(str(fact.get("id") or ""))
    return int(match[1]) if match else 0


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


def _view(html: str, buttons: Rows = (), notice: str | None = None, *, text_html: str | None = None) -> View:
    return View(html, html_to_text(html if text_html is None else text_html), buttons, notice)


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cx:
    snap: DossierSnapshot
    lang: str
    owner: bool

    @property
    def s(self) -> dict[str, Any]:
        return _s(self.lang)

    @property
    def persona(self) -> bool:
        return self.snap.kind == "persona"

    @property
    def dossier(self) -> dict[str, Any]:
        return self.snap.dossier if isinstance(self.snap.dossier, dict) else {}


def _cx(snap: DossierSnapshot, lang: str, owner: bool = True) -> _Cx:
    if snap.kind not in TARGETS:
        raise ValueError(f"unknown dossier kind {snap.kind!r}")
    return _Cx(snap, "ru" if lang == "ru" else "en", owner)


def _title(cx: _Cx) -> str:
    return cx.s["persona_title"] if cx.persona else (_e(cx.snap.label, NAME_CHARS) or "?")


def _model_name(cx: _Cx, provider: Any = None, model: Any = None) -> str:
    provider, model = provider or cx.snap.provider, model or cx.snap.model
    return _e(f"{provider} / {model}" if provider and model else model or provider or "?", 120)


def _facts(cx: _Cx) -> list[dict[str, Any]]:
    """The facts on screen. The contact sees only facts that still have a source in history (not deleted,
    replaced, or gone), and no owner notes or unsupported facts."""
    facts = [f for f in cx.dossier.get("facts") or [] if isinstance(f, dict)]
    if cx.owner:
        return facts
    return [f for f in facts if not _note(f) and f.get("status") != "unsupported" and _pairs(cx, f)
            and not _only_deleted(cx, f)]


def _stale_ids(cx: _Cx) -> set[str]:
    """Facts with a changed source; none for the contact, since edits and deletions are the owner's archive."""
    return {str(k) for k, v in (cx.snap.stale or {}).items() if v} if cx.owner else set()


def _sort_key(cx: _Cx, stale: set[str]) -> Callable[[dict[str, Any]], tuple[int, int, int, str]]:
    def key(f: dict[str, Any]) -> tuple[int, int, int, str]:
        group = 0 if _ds.protected(f) else 1 if str(f.get("id")) in stale else 2
        return group, -_int(f.get("last_revision")), _num(f), str(f.get("id"))
    return key


def _groups(cx: _Cx) -> tuple[list[tuple[str, list[dict[str, Any]]]], list[dict[str, Any]]]:
    """Non-empty sections in display order with sorted active facts, then the sorted unsupported facts."""
    facts, key = _facts(cx), _sort_key(cx, _stale_ids(cx))
    active = [f for f in facts if f.get("status") != "unsupported"]
    order = list(_ds.PERSONA_SECTIONS if cx.persona else CONTACT_ORDER)
    order += sorted({str(f.get("section")) for f in active} - set(order))
    groups = [(name, sorted((f for f in active if str(f.get("section")) == name), key=key)) for name in order]
    return [g for g in groups if g[1]], sorted((f for f in facts if f.get("status") == "unsupported"), key=key)


def _display_order(cx: _Cx) -> list[dict[str, Any]]:
    groups, unsupported = _groups(cx)
    return [f for _, facts in groups for f in facts] + unsupported


def _section_title(cx: _Cx, name: str) -> str:
    return cx.s["sections"].get(name) or _e(name.replace("_", " "), 40)


def _markers(cx: _Cx, f: dict[str, Any], stale: set[str]) -> str:
    s, out = cx.s, []
    if f.get("kind") == "owner":
        out.append("✎")
    if f.get("pinned"):
        out.append("📌")
    if f.get("kind") == "inferred":
        out.append(f"<i>~{s['conf'].get(f.get('confidence'), '')}</i>")
    if str(f.get("id")) in stale:
        out.append("⚠")
    if f.get("source_gone"):
        out.append(f"<i>{s['source_gone']}</i>")
    return " ".join(out) + " " if out else ""


def _fact_head(cx: _Cx, f: dict[str, Any], stale: set[str], limit: int = FACT_CHARS) -> str:
    return f"{_markers(cx, f, stale)}<b>{_e(f.get('id') or '?', 12)}</b> {_e(f.get('text'), limit)}"


def _pairs(cx: _Cx, f: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(stored source, sources_now item) per stored source; {"state": "unknown"} when not checked."""
    now = {str(n.get("ref")): n for n in cx.snap.sources_now.get(str(f.get("id"))) or [] if isinstance(n, dict)}
    return [(s, now.get(str(s.get("ref"))) or {"state": "unknown"})
            for s in f.get("sources") or [] if isinstance(s, dict)]


def _changes(cx: _Cx, f: dict[str, Any]) -> dict[str, str]:
    items = cx.snap.stale.get(str(f.get("id"))) or []
    return {str(c.get("ref")): str(c.get("change")) for c in items if isinstance(c, dict)}


def _deleted(stored: dict[str, Any], now: dict[str, Any]) -> bool:
    return bool(now["deleted"] if "deleted" in now else stored.get("deleted"))


def _only_deleted(cx: _Cx, f: dict[str, Any]) -> bool:
    """No source left in history: each one deleted, replaced (deleted and resent), or gone."""
    pairs = _pairs(cx, f)
    return bool(pairs) and all(_deleted(st, nw) or nw.get("state") in ("replaced", "gone") for st, nw in pairs)


def _changed(cx: _Cx, f: dict[str, Any], stored: dict[str, Any], now: dict[str, Any]) -> bool:
    """Deleted, edited, gone, or not checked: its stored quote may no longer be in the chat."""
    return (_deleted(stored, now) or now.get("state") in ("edited", "gone", "replaced", "unknown")
            or str(stored.get("ref") or "?") in _changes(cx, f))


def _source_meta(cx: _Cx, f: dict[str, Any], stored: dict[str, Any], now: dict[str, Any], *, card: bool) -> str:
    s, ref = cx.s, str(stored.get("ref") or "?")
    parts = [f"<b>{_e(ref, 40)}</b>" if card else _e(ref, 40)]
    if cx.persona:
        name = cx.snap.chat_names.get(str(stored.get("chat") or ref.partition(":")[0]))
        if name:
            parts.append(_e(name, 64))
    date = now.get("date") or stored.get("date")
    at = _dt(date)
    if card and at is not None:
        parts.append(_tg_time(at, "dt", f"{_day(at, cx.lang)}, {at:%H:%M} UTC"))
    else:
        parts.append(_day(date, cx.lang))
    if not cx.persona:
        side = "transcript" if stored.get("transcript") else _ds.LABELS.get(stored.get("direction"), "unknown")
        parts.append(s["side"][side])
    if not cx.owner:
        return " · ".join(parts)  # deletions and edits are the owner's archive, not the dossier
    if _deleted(stored, now):
        parts.append(s["deleted"])
    change = _changes(cx, f).get(ref)
    if change:
        parts.append("⚠ " + (s["change"].get(change) or _e(change, 20)))
    return " · ".join(parts)


def _now_html(cx: _Cx, now: dict[str, Any], quote: Any) -> str:
    state, s = now.get("state"), cx.s
    if state in ("present", "edited"):
        text = now.get("text")
        if not isinstance(text, str) or not text.strip():
            return ""
        if state == "present" and isinstance(quote, str) and _ds.ws(text) == _ds.ws(quote):
            return ""  # the quote already shows the whole message
        label = s["now_edited"] if state == "edited" else s["now"]
        return f"<p><i>{label}:</i> {_e(text, NOW_TEXT_CHARS)}</p>"
    key = {"old": "state_old", "gone": "state_gone", "replaced": "state_replaced"}.get(str(state))
    return f"<p><i>{s[key]}</i></p>" if key else ""


def _source_html(cx: _Cx, f: dict[str, Any], stored: dict[str, Any], now: dict[str, Any], *,
                 now_text: bool, card: bool) -> str:
    meta, quote = _source_meta(cx, f, stored, now, card=card), stored.get("quote") or now.get("quote")
    if not cx.owner and _changed(cx, f, stored, now):
        quote = None  # the contact post never brings back text that left the chat
    if quote and card:
        out = f"<p>{meta}</p><blockquote>{_e(quote, QUOTE_CHARS)}</blockquote>"
    elif quote:
        out = f"<blockquote>{_e(quote, QUOTE_CHARS)}<cite>{meta}</cite></blockquote>"
    else:
        out = f"<p>{meta}</p>"
    return out + (_now_html(cx, now, quote) if now_text and cx.owner else "")


def _fact_li(cx: _Cx, f: dict[str, Any], stale: set[str], level: int) -> str:
    """level 0: sources with current history text (owner only); 1: sources without it; 2: source refs only."""
    head, pairs = _fact_head(cx, f, stale), _pairs(cx, f)
    if not pairs:
        return f"<li>{head}</li>"
    if level >= 2:
        return f"<li>{head} <i>({', '.join(_e(st.get('ref') or '?', 40) for st, _ in pairs)})</i></li>"
    body = "".join(_source_html(cx, f, st, nw, now_text=level == 0, card=False) for st, nw in pairs)
    return f"<li>{head}<details><summary>{cx.s['sources']} · {len(pairs)}</summary>{body}</details></li>"


def _freshness(cx: _Cx) -> str:
    d, s = cx.dossier, cx.s
    refresh = d.get("last_refresh") if isinstance(d.get("last_refresh"), dict) else {}
    at = _dt(refresh.get("at") or d.get("refreshed_at"))
    revision = s["revision"].format(n=_int(d.get("revision")))
    if at is None:
        return f"{s['never'][cx.snap.kind]} · {revision}"
    ago = _tg_time(at, "r", _ago(cx.snap.now, at, cx.lang))
    return f"{s['updated'][cx.snap.kind]} {ago} · {_day(at, cx.lang)} · {revision}"


def _coverage(cx: _Cx) -> str:
    d, s, lang = cx.dossier, cx.s, cx.lang
    cov = d.get("coverage") if isinstance(d.get("coverage"), dict) else None
    if not cov:
        return f"{s['coverage']}: {s['no_coverage']}"
    text = f"{s['coverage']}: {_plural(lang, _int(cov.get('messages_processed')), s['messages'])}"
    if cx.persona and cov.get("chats") is not None:
        text += " " + s["in_chats"].format(chats=_plural(lang, _int(cov.get("chats")), s["chats_in"]))
    if cov.get("first_message_at") or cov.get("last_message_at"):
        text += f", {_day(cov.get('first_message_at'), lang)} – {_day(cov.get('last_message_at'), lang)}"
    if cov.get("older_history_not_sent"):
        text += f"; {s['older']}"
    return text


def _counts(cx: _Cx, stale: set[str]) -> str:
    s, facts = cx.s, _facts(cx)
    active = [f for f in facts if f.get("status") != "unsupported"]
    parts = [f"{s['count_facts']}: {len(active)}"]
    if cx.owner:  # the contact view has neither
        parts += [f"{s['count_stale']}: {sum(1 for f in facts if str(f.get('id')) in stale)}",
                  f"{s['count_unsupported']}: {len(facts) - len(active)}"]
    if not cx.persona:
        parts.append(f"{s['count_contradictions']}: "
                     f"{sum(1 for f in active if f.get('section') == 'contradictions')}")
    return " · ".join(parts)


def _owner_notes(cx: _Cx, *, failure: bool = True) -> list[str]:
    snap, s, notes = cx.snap, cx.s, []
    if not snap.enabled:
        notes.append(s["note_off"])
        if not snap.manage_global and not cx.persona:  # no Enable button: say who can, and how
            notes += [s["notice"]["operator_only"], s["off_cli"]]
    elif not snap.history_enabled:
        notes.append(s["note_history_off"])
    elif not snap.llm_ready:
        notes.append(s["note_no_llm"])
    if snap.refreshing:
        notes.append(s["note_refreshing"])
    elif snap.cycle_busy:
        notes.append(s["note_busy"])
    if failure and snap.failure_error:
        notes.append(s["note_failed"].format(error=_e(snap.failure_error, 64)))
    if snap.stale_error:
        notes.append(s["note_stale_error"].format(error=_e(snap.stale_error, 64)))
    return notes


def _notes_html(notes: list[str]) -> str:
    return f"<p><i>{'<br>'.join(notes)}</i></p>" if notes else ""


def _forget_command(snap: DossierSnapshot) -> str:
    if snap.kind == "persona":
        return f"{CLI} forget --persona"
    conn, chat = _ds.clip(snap.business_connection_id, 128), _ds.clip(snap.chat_id, 64)
    if conn and chat:
        return f"{CLI} forget --connection {shlex.quote(conn)} --chat {shlex.quote(chat)}"
    return f"{CLI} list"


def _target(cx: _Cx) -> str:
    return "persona" if cx.persona else "contact"


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def _overview_html(cx: _Cx) -> tuple[str, str]:
    """(rich HTML within the budget, HTML for the plain fallback: all facts, refs only)."""
    s, stale = cx.s, _stale_ids(cx)
    groups, unsupported = _groups(cx)
    order = [f for _, facts in groups for f in facts] + unsupported
    n = len(order)
    model = f"<br>{s['model']}: {_model_name(cx, *_refresh_model(cx))}" if cx.owner else ""
    head = (f"<h3>{_title(cx)}</h3><p>{_freshness(cx)}</p><p>{_coverage(cx)}{model}</p>"
            f"<p>{_counts(cx, stale)}</p>" + (_notes_html(_owner_notes(cx)) if cx.owner else ""))
    if cx.owner:
        foot = f"<footer>{s['legend']}<br>{s['cli_hint']}</footer>"
    else:  # the contact sees the legend only when a mark is shown
        marked = any(f.get("kind") in ("owner", "inferred") or f.get("pinned") for f in order)
        foot = f"<footer>{s['legend_contact']}</footer>" if marked else ""
    lis: dict[tuple[int, int], str] = {}
    costs: dict[tuple[int, int], tuple[int, int]] = {}

    def li(j: int, level: int) -> str:
        if (j, level) not in lis:
            lis[j, level] = _fact_li(cx, order[j], stale, level)
        return lis[j, level]

    def cost(j: int, level: int) -> tuple[int, int]:
        if (j, level) not in costs:
            costs[j, level] = _units(li(j, level)), count_blocks(li(j, level))
        return costs[j, level]

    def more(hidden: int) -> str:
        line = s["more"].format(n=hidden, word=_word(cx.lang, hidden, s["fact_forms"]))
        return f"<p><i>{line}{s['more_cli'] if cx.owner else ''}</i></p>"

    def build(levels: list[int], keep: int) -> str:
        parts, j = [head], 0
        for name, facts in groups:
            shown = "".join(li(k, levels[k]) for k in range(j, min(j + len(facts), keep)))
            j += len(facts)
            if shown:
                opened = " open" if not cx.persona and name in ACTION_SECTIONS else ""
                parts.append(f"<details{opened}><summary>{_section_title(cx, name)} · {len(facts)}</summary>"
                             f"<ul>{shown}</ul></details>")
        shown = "".join(li(k, levels[k]) for k in range(j, min(n, keep)))
        if shown:
            parts.append(f"<details><summary>{s['unsupported_title']} · {len(unsupported)}</summary>"
                         f"<p><i>{s['unsupported_hint']}</i></p><ul>{shown}</ul></details>")
        if keep < n:
            parts.append(more(n - keep))
        parts.append(foot)
        return "".join(parts)

    plain = build([2] * n, n)
    levels = [0] * n
    html = build(levels, n)
    if fits(html):
        return html, plain
    levels = [1] * n  # 1. drop current history text
    html = build(levels, n)
    if fits(html):
        return html, plain
    used = [_units(html), count_blocks(html)]
    for j in sorted(range(n), key=lambda k: (_int(order[k].get("last_revision")), _num(order[k]))):
        if used[0] <= BUDGET_CHARS and used[1] <= BUDGET_BLOCKS:
            break  # 2. drop source details of the oldest facts
        (c1, b1), (c2, b2) = cost(j, 1), cost(j, 2)
        used, levels[j] = [used[0] - c1 + c2, used[1] - b1 + b2], 2
    html = build(levels, n)
    if fits(html):
        return html, plain
    levels, keep = [2] * n, n  # 3. truncate from the end of the display order
    html = build(levels, n)
    used = [_units(html) + _units(more(n)), count_blocks(html) + 1]
    while keep > 0 and (used[0] > BUDGET_CHARS or used[1] > BUDGET_BLOCKS):
        keep -= 1
        c, b = cost(keep, 2)
        used = [used[0] - c, used[1] - b]
    html = build(levels, keep)
    while keep > 0 and not fits(html):
        keep -= 1
        html = build(levels, keep)
    return html, plain


def _refresh_model(cx: _Cx) -> tuple[Any, Any]:
    refresh = cx.dossier.get("last_refresh")
    return (refresh.get("provider"), refresh.get("model")) if isinstance(refresh, dict) else (None, None)


def _overview_buttons(cx: _Cx, has_facts: bool) -> Rows:
    s, snap = cx.s, cx.snap
    if cx.persona:
        return _rows([(s["b_manage"], build_args("manage", "persona"))] if has_facts else [],
                     [(s["b_back_dossier"], build_args("overview"))])
    first = [(s["b_manage"], build_args("manage"))] if has_facts else []
    if (snap.enabled and snap.history_enabled and snap.llm_ready and snap.in_history and not snap.refreshing
            and snap.status != "corrupt" and not (snap.dossier is None and snap.missing_reason == "unqualified")):
        first.append((s["b_refresh"], build_args("refresh")))
    if not snap.manage_global:
        return _rows(first)
    return _rows(first, [(s["b_persona"], build_args("overview", "persona"))])


def _missing_reason_html(cx: _Cx) -> str:
    snap, s = cx.snap, cx.s
    if snap.missing_reason == "unqualified" and not cx.persona:
        days = _plural(cx.lang, max(_int(snap.backfill_days), 1), s["day_forms"])
        return s["reason_unqualified"].format(days=days)
    if snap.missing_reason == "failed" or (snap.missing_reason is None and snap.failure_error):
        return s["reason_failed"].format(error=_e(snap.failure_error or "?", 64))
    return s["reason_pending"][snap.kind]


def _missing_view(cx: _Cx) -> View:
    s, snap = cx.s, cx.snap
    parts = [f"<h3>{_title(cx)}</h3>"]
    if not snap.enabled:
        parts += [f"<p><b>{s['off_title']}</b></p>", f"<p>{s['off_explain']}</p>",
                  f"<p>{s['off_privacy'].format(model=f'<b>{_model_name(cx)}</b>')}</p>"]
        if not snap.manage_global:  # no Enable button here: say who can, and how
            parts += [f"<p>{s['notice']['operator_only']}</p>", f"<p>{s['off_cli']}</p>"]
    else:
        parts += [f"<p><b>{s['no_dossier'][snap.kind]}</b></p>", f"<p>{_missing_reason_html(cx)}</p>"]
        parts.append(_notes_html(_owner_notes(cx, failure=False)))
    return _view("".join(parts), _overview_buttons(cx, False))


def _corrupt_view(cx: _Cx) -> View:
    s = cx.s
    html = (f"<h3>{_title(cx)}</h3><p><b>{s['corrupt'][cx.snap.kind]}</b></p><p>{s['corrupt_fix']}</p>"
            f"<pre>{_html.escape(_forget_command(cx.snap), quote=True)}</pre>")
    return _view(html, _overview_buttons(cx, False))


def post_blocker(snap: DossierSnapshot) -> str | None:
    """Why ``/dossier`` must post nothing into the Business chat (one of POST_BLOCKERS), or None."""
    if snap.kind != "contact":
        return "persona"
    if not snap.enabled:
        return "disabled"
    if snap.status == "corrupt":
        return "corrupt"
    if not isinstance(snap.dossier, dict) or snap.status == "missing":
        return "missing"
    if snap.stale_error:
        return "unchecked"  # without the history check a quote could be text that left the chat
    if not contact_fact_count(snap):
        return "empty"
    return None


def contact_fact_count(snap: DossierSnapshot) -> int:
    """How many facts the ``/dossier`` post of a contact snapshot shows."""
    if snap.kind != "contact" or not isinstance(snap.dossier, dict):
        return 0
    return len(_facts(_cx(snap, "en", owner=False)))


def render_overview(snap: DossierSnapshot, lang: str, audience: str = "owner") -> View:
    """The main dossier screen. audience='contact' is the ``/dossier`` post: no owner notes (note facts),
    no buttons, no owner-only lines, never the persona; it raises ValueError when post_blocker(snap) is set.
    """
    if audience not in ("owner", "contact"):
        raise ValueError(f"unknown audience {audience!r}")
    if audience == "contact" and post_blocker(snap) is not None:
        raise ValueError(f"nothing to post: {post_blocker(snap)}")
    cx = _cx(snap, lang, owner=audience == "owner")
    if snap.status == "corrupt":
        return _corrupt_view(cx)
    if snap.status == "missing" or not isinstance(snap.dossier, dict):
        return _missing_view(cx)
    html, plain = _overview_html(cx)
    return _view(html, _overview_buttons(cx, bool(_facts(cx))) if cx.owner else (), text_html=plain)


def render_post_reason(snap: DossierSnapshot, lang: str, reason: str | None = None, *,
                       error: str | None = None) -> View:
    """Sent to the owner's private chat with the bot when ``/dossier`` posted nothing.

    ``reason`` is one of POST_REASONS; it defaults to post_blocker(snap) and ValueError is raised when
    nothing blocks the post (use "send_failed" with the error type when Telegram refused it).
    """
    reason = reason or post_blocker(snap)
    if reason not in POST_REASONS:
        raise ValueError(f"unknown post reason {reason!r}")
    cx = _cx(snap, lang)
    s = cx.s
    lines = [s["post_head"].format(name=_e(snap.label, NAME_CHARS) or "?")]
    if reason == "missing":
        lines += [s["no_dossier"]["contact"], _missing_reason_html(cx)]
    elif reason == "corrupt":
        lines += [s["corrupt"]["contact"], f"{s['corrupt_fix']} {_html.escape(_forget_command(snap), quote=True)}"]
    else:
        lines.append(s["post"][reason].format(error=_e(error or snap.stale_error, 64) or "?"))
    return _view(f"<p>{'<br>'.join(lines)}</p>")


# ---------------------------------------------------------------------------
# Fact picker, fact card, confirmations, states
# ---------------------------------------------------------------------------


def _guard(cx: _Cx, prefix: str | None) -> View | None:
    if cx.snap.status != "ok" or not isinstance(cx.snap.dossier, dict):
        return render_overview(cx.snap, cx.lang)
    if prefix is not None and prefix != dossier_prefix(cx.dossier):
        return render_state("changed", cx.lang, _target(cx))
    return None


def _find(cx: _Cx, fact_id: Any) -> dict[str, Any] | None:
    fid = _ds.normalize_fact_id(fact_id)
    return next((f for f in _facts(cx) if str(f.get("id")) == fid), None)


def render_fact_picker(snap: DossierSnapshot, lang: str, page: int = 0) -> View:
    """Fact id buttons (PAGE_COLUMNS per row, PAGE_SIZE per page) with prev/next and back."""
    cx = _cx(snap, lang)
    guard = _guard(cx, None)
    if guard is not None:
        return guard
    s, stale, target = cx.s, _stale_ids(cx), _target(cx)
    order, prefix = _display_order(cx), dossier_prefix(cx.dossier)
    back = [(s["b_back_persona"] if cx.persona else s["b_back_dossier"], build_args("overview", target))]
    if not order:
        return _view(f"<h3>{_title(cx)}</h3><p>{s['picker_empty']}</p>", _rows(back))
    pages = min((len(order) + PAGE_SIZE - 1) // PAGE_SIZE, MAX_PAGE + 1)
    page = min(max(_int(page), 0), pages - 1)
    chunk = order[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    items = "".join(f"<li>{_fact_head(cx, f, stale, PICKER_TEXT_CHARS)}</li>" for f in chunk)
    html = (f"<h3>{_title(cx)}</h3><p>{s['picker_page'].format(p=page + 1, n=pages)}</p><ul>{items}</ul>"
            f"<p><i>{s['picker_hint']}</i></p>")
    ids = [str(f.get("id")) for f in chunk if prefix and _FID_RE.fullmatch(str(f.get("id")))]
    grid = [[(fid, build_args("fact", target, prefix=prefix, fact_id=fid)) for fid in ids[i:i + PAGE_COLUMNS]]
            for i in range(0, len(ids), PAGE_COLUMNS)]
    nav = ([(s["b_prev"].format(n=page), build_args("manage", target, page=page - 1))] if page > 0 else [])
    if page + 1 < pages:
        nav.append((s["b_next"].format(n=page + 2), build_args("manage", target, page=page + 1)))
    return _view(html, _rows(*grid, nav, back))


def _kind_label(cx: _Cx, f: dict[str, Any]) -> str:
    s = cx.s
    if f.get("kind") == "owner":
        return s["kind_note"] if _note(f) else s["kind_owner"]
    if f.get("kind") == "inferred":
        return s["kind_inferred"].format(conf=s["conf"].get(f.get("confidence"), "?"))
    return s["kind_observed"]


def _picker_page(cx: _Cx, f: dict[str, Any]) -> int:
    order = _display_order(cx)
    index = next((i for i, g in enumerate(order) if g is f), 0)
    return min(index // PAGE_SIZE, MAX_PAGE)


def render_fact_card(snap: DossierSnapshot, lang: str, fact_id: str, *, prefix: str | None = None) -> View:
    """One fact in full with all sources; Pin/Unpin, Remove, Back. ``prefix`` from the callback is checked."""
    cx = _cx(snap, lang)
    guard = _guard(cx, prefix)
    if guard is not None:
        return guard
    f = _find(cx, fact_id)
    if f is None:
        return render_state("changed", lang, _target(cx))
    s, stale, target = cx.s, _stale_ids(cx), _target(cx)
    status = []
    if f.get("pinned"):
        status.append(s["card_pinned"])
    if f.get("status") == "unsupported":
        status.append(s["card_unsupported"])
    if f.get("source_gone"):
        status.append(s["card_source_gone"])
    changes = _changes(cx, f)
    if changes:
        listed = ", ".join(f"{_e(ref, 40)} {s['change'].get(c) or _e(c, 20)}" for ref, c in changes.items())
        status.append(s["card_stale"].format(changes=listed))
    status.append(s["revisions"].format(a=_int(f.get("first_revision")), b=_int(f.get("last_revision"))))
    parts = [f"<h3>{_title(cx)} · {_e(f.get('id'), 12)}</h3>",
             f"<p>{_markers(cx, f, stale)}{_section_title(cx, str(f.get('section')))} · {_kind_label(cx, f)}</p>",
             f"<p><b>{_e(f.get('text'), FACT_CHARS)}</b></p>", f"<p>{'<br>'.join(status)}</p>"]
    owner = f.get("owner") if isinstance(f.get("owner"), dict) else {}
    if owner.get("previous_text"):
        parts.append(f"<p><i>{s['previous']}:</i> {_e(owner['previous_text'], FACT_CHARS)}</p>")
    pairs = _pairs(cx, f)
    parts.append(f"<h4>{s['sources']} · {len(pairs)}</h4>" if pairs else f"<p>{s['no_sources']}</p>")
    parts += [_source_html(cx, f, st, nw, now_text=True, card=True) for st, nw in pairs[:CARD_MAX_SOURCES]]
    if len(pairs) > CARD_MAX_SOURCES:
        hidden = len(pairs) - CARD_MAX_SOURCES
        more = s["more"].format(n=hidden, word=_word(cx.lang, hidden, s["source_forms"]))
        parts.append(f"<p><i>{more}{s['more_cli']}</i></p>")
    buttons: list[list[tuple[str, str]]] = []
    fid, pfx = str(f.get("id")), dossier_prefix(cx.dossier)
    if pfx and _FID_RE.fullmatch(fid):
        pin = "unpin" if f.get("pinned") else "pin"
        buttons.append([(s[f"b_{pin}"], build_args(pin, target, prefix=pfx, fact_id=fid)),
                        (s["b_remove"], build_args("remove_ask", target, prefix=pfx, fact_id=fid))])
    buttons.append([(s["b_back_facts"], build_args("manage", target, page=_picker_page(cx, f)))])
    return _view("".join(parts), _rows(*buttons))


def render_remove_confirm(snap: DossierSnapshot, lang: str, fact_id: str, *, prefix: str | None = None) -> View:
    cx = _cx(snap, lang)
    guard = _guard(cx, prefix)
    if guard is not None:
        return guard
    f = _find(cx, fact_id)
    fid, pfx = str((f or {}).get("id")), dossier_prefix(cx.dossier)
    if f is None or not pfx or not _FID_RE.fullmatch(fid):
        return render_state("changed", lang, _target(cx))
    s, target = cx.s, _target(cx)
    html = (f"<h3>{s['remove_q'].format(fid=_e(fid, 12))}</h3><p>{_fact_head(cx, f, _stale_ids(cx))}</p>"
            f"<p>{s['remove_explain']}</p>")
    return _view(html, _rows([(s["b_remove_yes"], build_args("remove_do", target, prefix=pfx, fact_id=fid)),
                              (s["b_cancel"], build_args("fact", target, prefix=pfx, fact_id=fid))]))


def enable_confirm(lang: str, provider: str, model: str) -> tuple[str, str, str]:
    """Plain (text, confirm label, cancel label) for turning dossiers on; Settings adds its own callbacks."""
    s = _s(lang)
    name = _ds.clip(f"{provider} / {model}" if provider and model else model or provider or "?", 120)
    text = f"{s['enable_q']}\n\n{s['enable_explain'].format(model=name)}\n\n{s['enable_keep']}"
    return text, s["b_enable_yes"], s["b_cancel"]


def notice_text(key: str, lang: str, *, fact_id: str | None = None, error: str | None = None) -> str:
    """Short plain toast text for NOTICE_KEYS (<= 200 chars, no HTML)."""
    if key not in NOTICE_KEYS:
        raise ValueError(f"unknown notice {key!r}")
    text = _s(lang)["notice"][key].format(fid=_ds.clip(fact_id, 12) or "?", error=_ds.clip(error, 64) or "?")
    return _ds.clip(text, 200)


def render_state(key: str, lang: str, target: str = "contact", *, fact_id: str | None = None,
                 error: str | None = None) -> View:
    """A one-line screen for NOTICE_KEYS (changed, busy, refresh_started ...) with the same text as notice."""
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}")
    s, notice = _s(lang), notice_text(key, lang, fact_id=fact_id, error=error)
    retry = () if key == "no_connection" else _rows([(s["b_dossier"], build_args("overview", target))])
    return _view(f"<p>{_html.escape(notice)}</p>", retry, notice)
