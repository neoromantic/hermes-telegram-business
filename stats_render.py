"""Telegram rich-message views of one chat's correspondence stats.

``panel`` is the owner panel's report, shown only in the owner's private chat
with the bot. ``card`` is the public card for the in-chat ``/stats`` command:
it is posted into the Business chat, so the contact sees it too, and it never
shows deletions, edits, who is waiting, missing history, the previous period,
or p90 reply times.

Both are pure and deterministic: they format a ``ContactStats`` that never held
message text. Each returns a ``RichView``: ``html`` is Bot API rich message HTML
(``sendRichMessage``, or ``editMessageText`` with ``rich_message``, entity
detection off), and ``text`` is a short plain-text fallback without a parse mode.

Layout: a title, a balance bar of ``BAR_CELLS`` squares (the contact is always
``CONTACT``, the owner ``OWNER``), a table per side, emoji heat maps
(``HEAT``) inside tables so their columns line up in any font, a few tiles,
and a footer. Every untrusted value (only the names) goes through
``correspondence_stats._safe``, is clipped, and is HTML-escaped. A name with
right-to-left letters sits between two LRM marks, so it cannot reorder the
line around it. A view stays within ``MAX_HTML_UNITS`` (and its fallback within
Telegram's 4096): past that, the heat maps go first, then the tiles; the title,
the panel's note, and the footer always stay.
"""

from __future__ import annotations

import html
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo

from . import correspondence_stats as _stats
from . import owner_panel as _owner_panel

PERIODS = ("7d", "30d", "90d", "all")
SITE_URL = "https://telesec.sergeybaus.com"
MAX_HTML_UNITS = 8000  # far below the rich message limit (about 32768 characters, 500 blocks)
MAX_TEXT_UNITS = 4096  # Telegram's message text limit, in UTF-16 code units.
TITLE_NAME_CHARS = 64
COLUMN_NAME_CHARS = 16
BAR_CELLS = 10
CONTACT = "🟦"
OWNER = "🟩"
HEAT = ("·", "🟨", "🟧", "🟥")  # zero, then thirds of the largest value in one table
YEARS_PER_TABLE = 8
NONE = "–"  # a value that is not known, e.g. a reply time without replies
ELLIPSIS = "…"
LRM = "\u200e"  # left-to-right mark: zero width, a strong left-to-right character
_ZWJ = "\u200d"
# Bidi classes that lay text out right to left in a left-to-right line.
_RTL_CLASSES = frozenset({"R", "AL", "AN"})
_EMPTY_CELL = "<td></td>"

# English defines every key; Russian falls back to English key by key. Values are
# plain text without "<", ">" or "&": placeholders take ready HTML (or plain text
# in the fallback), so a template never needs escaping.
_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "period.7d": "7 days",
        "period.30d": "30 days",
        "period.90d": "90 days",
        "period.all": "all time",
        "previous.7d": "vs the previous 7 days",
        "previous.30d": "vs the previous 30 days",
        "previous.90d": "vs the previous 90 days",
        "you": "You",
        "row.messages": "messages",
        "row.reply": "⚡ reply",
        "row.p90": "p90",
        "row.per_turn": "per turn",
        "row.length": "length",
        "row.edits": "edits",
        "waiting.received": "{name} is waiting for your reply",
        "waiting.sent": "You are waiting for a reply",
        "when": "🗓 When",
        "weekdays": "Mo Tu We Th Fr Sa Su",
        "weekday_names": "Mon Tue Wed Thu Fri Sat Sun",
        "months": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
        "months_of": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
        "month_initials": "J F M A M J J A S O N D",
        "peak": "🎯 Peak —",
        "peak_card": "🎯 Most often —",
        "tile.streak": "🔥 streak",
        "tile.record": "🏆 record",
        "tile.pause": "💤 pause",
        "tile.sessions": "🗣 sessions",
        "tile.first": "👋 first",
        "tile.deleted": "🗑 deleted",
        "first": "first",
        "previous.messages": "messages",
        "previous.reply": "reply",
        "history_since": "history since {date}",
        "text_only": "text only",
        "empty": "no messages in this period",
        "counted_by": "counted by {link}",
        "units": "s min h d",
    },
    "ru": {
        "period.7d": "7 дней",
        "period.30d": "30 дней",
        "period.90d": "90 дней",
        "period.all": "всё время",
        "previous.7d": "К прошлым 7 дням",
        "previous.30d": "К прошлым 30 дням",
        "previous.90d": "К прошлым 90 дням",
        "you": "Вы",
        "row.messages": "сообщений",
        "row.reply": "⚡ ответ",
        "row.per_turn": "подряд",
        "row.length": "длина",
        "row.edits": "правки",
        "waiting.received": "{name} ждёт ответа",
        "waiting.sent": "Вы ждёте ответа",
        "when": "🗓 Когда",
        "weekdays": "пн вт ср чт пт сб вс",
        "weekday_names": "пн вт ср чт пт сб вс",
        "months": "янв фев мар апр май июн июл авг сен окт ноя дек",
        "months_of": "янв фев мар апр мая июн июл авг сен окт ноя дек",
        "month_initials": "Я Ф М А М И И А С О Н Д",
        "peak": "🎯 Пик —",
        "peak_card": "🎯 Чаще всего —",
        "tile.streak": "🔥 серия",
        "tile.record": "🏆 рекорд",
        "tile.pause": "💤 пауза",
        "tile.sessions": "🗣 бесед",
        "tile.first": "👋 первым",
        "tile.deleted": "🗑 удалено",
        "first": "первым",
        "previous.messages": "сообщений",
        "previous.reply": "ответ",
        "history_since": "история с {date}",
        "text_only": "только текст",
        "empty": "нет сообщений за период",
        "counted_by": "посчитал {link}",
        "units": "с мин ч дн",
    },
}
_QUARTERS = ("I", "II", "III", "IV")
_HOUR_BUCKETS = 8  # three hours each


@dataclass(frozen=True, slots=True)
class RichView:
    html: str  # Bot API rich message HTML
    text: str  # the plain-text fallback, no parse mode


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _extends(char: str) -> bool:
    # Marks, joiners, variation selectors, other format characters (such as LRM),
    # and skin tones belong to the character before them.
    return unicodedata.category(char) in ("Mn", "Me", "Mc", "Cf") or 0x1F3FB <= ord(char) <= 0x1F3FF


def _regional(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def graphemes(text: str) -> list[str]:
    """``text`` as characters a reader sees: a base with its marks, emoji ZWJ sequences, and flag pairs.

    An approximation of Unicode grapheme clusters, good enough to clip names.
    """
    clusters: list[str] = []
    joined = flag_open = False
    for char in text:
        if clusters and (joined or _extends(char) or (flag_open and _regional(char))):
            clusters[-1] += char
            flag_open = flag_open and not _regional(char)
        else:
            clusters.append(char)
            flag_open = _regional(char)
        joined = char == _ZWJ
    return clusters


def _clip(text: str, limit: int) -> str:
    """At most ``limit`` characters a reader sees, cut between them with "…"."""
    clusters = graphemes(text)
    return text if len(clusters) <= limit else "".join(clusters[: limit - 1]) + ELLIPSIS


def _isolate(name: str) -> str:
    """A name with right-to-left letters between LRMs, so the line around it stays left to right.

    Telegram picks a paragraph's direction from its first strong character, and an
    unisolated Arabic or Hebrew name would flip it and pull numbers past it.
    ``_safe`` escaped every bidi control the name had, so it cannot undo the marks.
    """
    if any(unicodedata.bidirectional(char) in _RTL_CLASSES for char in name):
        return f"{LRM}{name}{LRM}"
    return name


def safe_name(text: str, limit: int) -> str:
    """An untrusted name, escaped like the CLI does and clipped to ``limit`` characters; not yet HTML-escaped."""
    return _isolate(_clip(_stats._safe(text), limit))


def _round_percent(part: int, whole: int) -> int:
    # Half up in integers, like the stats themselves.
    return (200 * part + whole) // (2 * whole)


def _ru_plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


@dataclass(frozen=True, slots=True)
class _Format:
    lang: str
    tz: tzinfo
    year: int  # the current local year: dates in it are shown without the year

    def t(self, key: str, **values: object) -> str:
        template = _STRINGS[self.lang].get(key) or _STRINGS["en"][key]
        return template.format(**values) if values else template

    def num(self, value: int) -> str:
        text = f"{value:,}"
        return text.replace(",", " ") if self.lang == "ru" else text

    def decimal(self, value: float | None) -> str:
        if value is None:
            return NONE
        text = f"{value:,.1f}"
        return text.replace(",", " ").replace(".", ",") if self.lang == "ru" else text

    def dur(self, seconds: int | None) -> str:
        """Rounded down: 40 s, 12 min, 1 h 40 min, 2 h, 3 d 5 h, 12 d; a zero minor unit is dropped."""
        if seconds is None:
            return NONE
        s, m, h, d = self.t("units").split()
        seconds = max(0, seconds)
        if seconds < 60:
            return f"{seconds} {s}"
        if seconds < 3600:
            return f"{seconds // 60} {m}"
        if seconds < 86400:
            minutes = seconds % 3600 // 60
            return f"{seconds // 3600} {h}" + (f" {minutes} {m}" if minutes else "")
        days, hours = seconds // 86400, seconds % 86400 // 3600
        # Hours only while they still matter: 3 d 5 h, but 12 d.
        return f"{days} {d}" + (f" {hours} {h}" if hours and days < 10 else "")

    def days(self, count: int) -> str:
        """A whole number of days, the word in full: 1 day, 9 days; 1 день, 3 дня, 9 дней."""
        if self.lang == "ru":
            return f"<b>{self.num(count)}</b> {_ru_plural(count, 'день', 'дня', 'дней')}"
        return f"<b>{self.num(count)}</b> {'day' if count == 1 else 'days'}"

    def day(self, day: date, *, with_year: bool | None = None) -> str:
        if with_year is None:
            with_year = day.year != self.year
        month = self.t("months_of").split()[day.month - 1]
        return f"{day.day} {month}" + (f" {day.year}" if with_year else "")

    def span(self, first: date, last: date) -> str:
        # Both ends show the year when either is outside the current year.
        if first == last:
            return self.day(first)
        with_year = first.year != self.year or last.year != self.year
        return f"{self.day(first, with_year=with_year)} – {self.day(last, with_year=with_year)}"

    def local(self, at: datetime) -> datetime:
        return at.astimezone(self.tz)


def _format(window: _stats.StatsWindow, language_code: str | None) -> _Format:
    return _Format(
        lang=_owner_panel.language(language_code),
        tz=window.tz,
        year=window.now.astimezone(window.tz).year,
    )


def period_label(period: str, language_code: str | None = None) -> str:
    """The period in a title, such as "30 days" or "всё время"."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    lang = _owner_panel.language(language_code)
    return _STRINGS[lang].get(f"period.{period}") or _STRINGS["en"][f"period.{period}"]


# --- Building blocks -----------------------------------------------------------------------------


def heat(value: int, top: int) -> str:
    """One heat square: "·" for zero, else level ceil(3 * value / top) as 🟨, 🟧, or 🟥."""
    if value <= 0 or top <= 0:
        return HEAT[0]
    return HEAT[min(3, -(-3 * value // top))]


def balance_cells(received: int, sent: int, *, has_received: bool, has_sent: bool) -> int:
    """The contact's squares of ``BAR_CELLS`` for the shares in percent; a side with messages gets one at least."""
    cells = (2 * received * BAR_CELLS + 100) // 200
    if has_received:
        cells = max(1, cells)
    if has_sent:
        cells = min(BAR_CELLS - 1, cells)
    return max(0, min(BAR_CELLS, cells))


def _balance(stats: _stats.ContactStats) -> str | None:
    """"58% 🟦…🟩 42%", or None when a side's share is not known."""
    share = stats.share
    if share.received is None or share.sent is None:
        return None
    cells = balance_cells(
        share.received,
        share.sent,
        has_received=share.received > 0 or stats.messages.received > 0,
        has_sent=share.sent > 0 or stats.messages.sent > 0,
    )
    return f"{share.received}% {CONTACT * cells}{OWNER * (BAR_CELLS - cells)} {share.sent}%"


def _td(content: str, *, right: bool = False) -> str:
    return f'<td align="right">{content}</td>' if right else f"<td>{content}</td>"


def _th(content: str, *, right: bool = False) -> str:
    return f'<th align="right">{content}</th>' if right else f"<th>{content}</th>"


def _table(rows: Sequence[Sequence[str]], *, attrs: str = "compact", header: Sequence[str] | None = None) -> str:
    """A table of ready cells (``_td``/``_th``); ``header`` is one row of ``<th>`` cells."""
    lines = [f"<table {attrs}>"]
    if header is not None:
        lines.append("<tr>" + "".join(header) + "</tr>")
    lines.extend("<tr>" + "".join(row) + "</tr>" for row in rows)
    lines.append("</table>")
    return "\n".join(lines)


def _heat_row(counts: Sequence[int]) -> list[str]:
    top = max(counts, default=0)
    return [_td(heat(value, top)) for value in counts]


def _weekday_header(fmt: _Format) -> list[str]:
    return [_th(name) for name in fmt.t("weekdays").split()]


def _calendar(activity: _stats.Activity, fmt: _Format) -> list[str]:
    """Day buckets: one row per Monday-started week, empty cells outside the period."""
    top = max(activity.counts, default=0)
    rows = []
    monday = activity.first_day - timedelta(days=activity.first_day.weekday())
    while monday <= activity.last_day:
        row = []
        for offset in range(7):
            day = monday + timedelta(days=offset)
            index = (day - activity.start).days
            inside = activity.first_day <= day <= activity.last_day and 0 <= index < len(activity.counts)
            row.append(_td(heat(activity.counts[index], top)) if inside else _EMPTY_CELL)
        rows.append(row)
        monday += timedelta(days=7)
    return [_table(rows, header=_weekday_header(fmt))]


def _weeks(activity: _stats.Activity, fmt: _Format) -> list[str]:
    """Week buckets: one row per month (by each week's Monday), that month's weeks in order."""
    top = max(activity.counts, default=0)
    months = fmt.t("months").split()
    groups: list[tuple[tuple[int, int], list[str]]] = []
    for index, value in enumerate(activity.counts):
        monday = activity.start + timedelta(days=7 * index)
        key = (monday.year, monday.month)
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(_td(heat(value, top)))
    width = max((len(cells) for _, cells in groups), default=0)
    rows = [
        [_td(months[month - 1]), *cells, *[_EMPTY_CELL] * (width - len(cells))] for (_, month), cells in groups
    ]
    return [_table(rows)]


def _by_year(activity: _stats.Activity, per_year: int, header: Sequence[str]) -> list[str]:
    """Month or quarter buckets: one row per year, empty cells outside the period."""
    top = max(activity.counts, default=0)
    first = activity.start.year * per_year + (activity.start.month - 1) * per_year // 12
    rows = []
    for year in range(activity.start.year, activity.last_day.year + 1):
        row = [_td(str(year))]
        for slot in range(per_year):
            index = year * per_year + slot - first
            row.append(_td(heat(activity.counts[index], top)) if 0 <= index < len(activity.counts) else _EMPTY_CELL)
        rows.append(row)
    return [_table(rows, header=[_th(""), *(_th(label) for label in header)])]


def _years(activity: _stats.Activity) -> list[str]:
    """Year buckets: the years as the header, one row of cells, at most ``YEARS_PER_TABLE`` per table."""
    top = max(activity.counts, default=0)
    tables = []
    for offset in range(0, len(activity.counts), YEARS_PER_TABLE):
        chunk = activity.counts[offset : offset + YEARS_PER_TABLE]
        header = [_th(str(activity.start.year + offset + index)) for index in range(len(chunk))]
        tables.append(_table([[_td(heat(value, top)) for value in chunk]], header=header))
    return tables


def _hours(stats: _stats.ContactStats) -> list[int]:
    size = len(stats.hours) // _HOUR_BUCKETS
    return [sum(stats.hours[index * size : (index + 1) * size]) for index in range(_HOUR_BUCKETS)]


def _covered(stats: _stats.ContactStats, fmt: _Format) -> _stats.Activity | None:
    """The activity from the first day history covers, so days before it are not shown as days without messages.

    History covers the period from its first observation, or from an earlier message (an imported one).
    """
    activity, retained = stats.activity, stats.missing.retained_from
    if activity is None or retained is None:
        return activity
    bucket = activity.bucket
    origin = _stats._bucket_index(activity.start, bucket)
    covered = _stats._bucket_index(fmt.local(retained).date(), bucket) - origin
    earliest = next((index for index, value in enumerate(activity.counts) if value), covered)
    first = min(covered, earliest)
    if first <= 0 or first >= len(activity.counts):
        return activity
    start = _stats._bucket_start(origin + first, bucket)
    return replace(activity, start=start, counts=activity.counts[first:], first_day=max(activity.first_day, start))


def _when(stats: _stats.ContactStats, fmt: _Format) -> str:
    """The "When" heading and its heat maps: the period, weekdays (unless the period is a calendar), hours."""
    activity = _covered(stats, fmt)
    tables: list[str] = []
    if activity is not None:
        if activity.bucket == "day":
            tables = _calendar(activity, fmt)
        elif activity.bucket == "week":
            tables = _weeks(activity, fmt)
        elif activity.bucket == "month":
            tables = _by_year(activity, 12, fmt.t("month_initials").split())
        elif activity.bucket == "quarter":
            tables = _by_year(activity, 4, _QUARTERS)
        else:
            tables = _years(activity)
    if activity is None or activity.bucket != "day":
        tables.append(_table([_heat_row(stats.weekdays)], header=_weekday_header(fmt)))
    size = 24 // _HOUR_BUCKETS
    tables.append(_table([_heat_row(_hours(stats))], header=[_th(str(i * size)) for i in range(_HOUR_BUCKETS)]))
    return "\n".join([f"<h4>{fmt.t('when')}</h4>", *tables])


def _peak_text(stats: _stats.ContactStats, fmt: _Format) -> str | None:
    hour, weekday = stats.peak_hour, stats.peak_weekday
    if hour is None or weekday is None:
        return None
    return f"{fmt.t('weekday_names').split()[weekday]}, {hour}–{hour + 1}"


def _span(stats: _stats.ContactStats, fmt: _Format) -> str | None:
    activity = stats.activity
    return None if activity is None else fmt.span(activity.first_day, activity.last_day)


def _messages_cell(fmt: _Format, count: int, share: int | None) -> str:
    return f"<b>{fmt.num(count)}</b>" + ("" if share is None else f" · {share}%")


def _length(fmt: _Format, value: int | None) -> str:
    return NONE if value is None else fmt.num(value)


def _bold_dur(fmt: _Format, seconds: int | None) -> str:
    return NONE if seconds is None else f"<b>{fmt.dur(seconds)}</b>"


def _sides_table(names: tuple[str, str], rows: Sequence[tuple[str, str, str]]) -> str:
    """The per-side table: ``names`` and the row values are ready HTML."""
    header = [_th(""), _th(f"{CONTACT} {names[0]}", right=True), _th(f"{OWNER} {names[1]}", right=True)]
    body = [[_td(label), _td(first, right=True), _td(second, right=True)] for label, first, second in rows]
    return _table(body, attrs="striped compact", header=header)


def _first_tile(stats: _stats.ContactStats) -> str | None:
    """Who starts sessions more often, with their square and share; None without sessions."""
    sessions = stats.sessions
    if not sessions.total:
        return None
    contact_first = sessions.started_by_contact >= sessions.started_by_owner
    starts = sessions.started_by_contact if contact_first else sessions.started_by_owner
    return f"{CONTACT if contact_first else OWNER} <b>{_round_percent(starts, sessions.total)}%</b>"


def _tiles(pairs: Sequence[tuple[str, str]]) -> str:
    """Label and value pairs, two per row; an odd last pair leaves two empty cells."""
    rows = []
    for index in range(0, len(pairs), 2):
        row: list[str] = []
        for label, value in pairs[index : index + 2]:
            row.extend([_td(label), _td(value, right=True)])
        row.extend([_EMPTY_CELL] * (4 - len(row)))
        rows.append(row)
    return _table(rows)


def _fit(blocks: Sequence[tuple[int, str]], limit: int) -> str:
    """Blocks joined by newlines within ``limit`` UTF-16 units.

    Each block is balanced HTML (or whole lines of text) with a drop rank: past the
    limit, every block of the lowest rank above 0 goes, then the next. Rank 0 always stays.
    """
    kept = list(blocks)
    while utf16_units("\n".join(block for _, block in kept)) > limit:
        ranks = [rank for rank, _ in kept if rank > 0]
        if not ranks:
            break
        lowest = min(ranks)
        kept = [(rank, block) for rank, block in kept if rank != lowest]
    return "\n".join(block for _, block in kept)


def _view(html_blocks: Sequence[tuple[int, str]], text_blocks: Sequence[tuple[int, str]]) -> RichView:
    return RichView(_fit(html_blocks, MAX_HTML_UNITS), _fit(text_blocks, MAX_TEXT_UNITS))


# Drop ranks, lowest first: the heat maps, then the tiles, then the rest; 0 never goes.
_KEEP, _GRID, _TILES, _EXTRA, _WAITING, _TABLE, _BAR, _SUBTITLE = range(8)


# --- Owner panel ---------------------------------------------------------------------------------


def panel(
    stats: _stats.ContactStats,
    window: _stats.StatsWindow,
    *,
    period: str,
    language_code: str | None = None,
    contact_name: str | None = None,
    note: str | None = None,
) -> RichView:
    """The owner panel's report: everything the stats know, compact.

    ``contact_name`` is the short name for the columns (the contact's first name);
    the title shows the full name. Owner-only: it includes deletions, edits,
    who is waiting, p90 reply times, the previous period, and missing history.
    ``note`` is a fixed plain-text line (a catalog string) kept as the last
    paragraph before the footer, even when the report must be cut to fit.
    """
    fmt = _format(window, language_code)
    full_name = safe_name(stats.name or stats.display_name, TITLE_NAME_CHARS)
    contact = safe_name(contact_name or stats.name or stats.display_name, COLUMN_NAME_CHARS)
    you = fmt.t("you")
    title = f"📊 {full_name} · {period_label(period, fmt.lang)}"
    footer = " · ".join(_footer(stats, fmt))
    html_blocks: list[tuple[int, str]] = [(_KEEP, f"<h3>{_esc(title)}</h3>")]
    text_blocks: list[tuple[int, str]] = [(_KEEP, title)]
    note_html = [] if note is None else [(_KEEP, f"<p>{_esc(note)}</p>")]
    note_text = [] if note is None else [(_KEEP, note)]
    tail_html = [*note_html, (_KEEP, f"<footer>{_esc(footer)}</footer>")]
    tail_text = [*note_text, (_KEEP, footer)]

    if not stats.messages.total:
        html_blocks.append((_SUBTITLE, f"<p>{fmt.t('empty')}</p>"))
        text_blocks.append((_SUBTITLE, fmt.t("empty")))
        return _view([*html_blocks, *tail_html], [*text_blocks, *tail_text])

    span = _span(stats, fmt)
    total = fmt.num(stats.messages.total)
    active = fmt.num(stats.active_days.total)
    subtitle_html = [*([] if span is None else [span]), f"💬 <b>{total}</b>"]
    subtitle_text = [*([] if span is None else [span]), f"💬 {total}"]
    activity = stats.activity
    if activity is not None:
        days = (activity.last_day - activity.first_day).days + 1
        of_days = _of_days(fmt, days)
        subtitle_html.append(f"<b>{active}</b> {of_days}")
        subtitle_text.append(f"{active} {of_days}")
    html_blocks.append((_SUBTITLE, f"<p>{' · '.join(subtitle_html)}</p>"))
    text_blocks.append((_SUBTITLE, " · ".join(subtitle_text)))

    balance = _balance(stats)
    if balance is not None:
        html_blocks.append((_BAR, f"<p>{balance}</p>"))

    replies, turns, length, edited, share = stats.replies, stats.turns, stats.text_length, stats.edited, stats.share
    rows = [
        (
            fmt.t("row.messages"),
            _messages_cell(fmt, stats.messages.received, share.received),
            _messages_cell(fmt, stats.messages.sent, share.sent),
        ),
        (
            fmt.t("row.reply"),
            _bold_dur(fmt, replies.received.median_seconds),
            _bold_dur(fmt, replies.sent.median_seconds),
        ),
        (fmt.t("row.p90"), fmt.dur(replies.received.p90_seconds), fmt.dur(replies.sent.p90_seconds)),
        (fmt.t("row.per_turn"), fmt.decimal(turns.per_turn_received), fmt.decimal(turns.per_turn_sent)),
        (fmt.t("row.length"), _length(fmt, length.received), _length(fmt, length.sent)),
    ]
    if edited.received or edited.sent:
        rows.append((fmt.t("row.edits"), fmt.num(edited.received), fmt.num(edited.sent)))
    html_blocks.append((_TABLE, _sides_table((_esc(contact), you), rows)))
    text_blocks.append((_TABLE, _side_lines(stats, fmt, contact, you)))

    waiting = stats.waiting
    if waiting is not None:
        key = f"waiting.{'received' if waiting.side == 'received' else 'sent'}"
        duration = fmt.dur(waiting.seconds)
        html_blocks.append(
            (_WAITING, f"<blockquote>⏳ {fmt.t(key, name=_esc(contact))} <b>{duration}</b></blockquote>")
        )
        text_blocks.append((_WAITING, f"⏳ {fmt.t(key, name=contact)} {duration}"))

    html_blocks.append((_GRID, _when(stats, fmt)))
    peak = _peak_text(stats, fmt)
    if peak is not None:
        html_blocks.append((_EXTRA, f"<p>{fmt.t('peak')} <mark>{peak}</mark></p>"))
        text_blocks.append((_EXTRA, f"{fmt.t('peak')} {peak}"))

    html_blocks.append((_TILES, _tiles(_panel_tiles(stats, fmt))))

    previous = _previous(stats, fmt, period)
    if previous is not None:
        html_blocks.append((_EXTRA, f"<p>{previous}</p>"))

    return _view([*html_blocks, *tail_html], [*text_blocks, *tail_text])


def _of_days(fmt: _Format, days: int) -> str:
    """ "из 30 дней" / "of 30 days" after the active days."""
    if fmt.lang == "ru":
        return f"из {fmt.num(days)} {_ru_plural(days, 'дня', 'дней', 'дней')}"
    return f"of {fmt.num(days)} {'day' if days == 1 else 'days'}"


def _side_lines(stats: _stats.ContactStats, fmt: _Format, contact: str, owner: str) -> str:
    """The fallback's two side lines: count, share, and reply median."""
    lines = []
    for square, name, count, share, median in (
        (CONTACT, contact, stats.messages.received, stats.share.received, stats.replies.received.median_seconds),
        (OWNER, owner, stats.messages.sent, stats.share.sent, stats.replies.sent.median_seconds),
    ):
        percent = "" if share is None else f" ({share}%)"
        lines.append(f"{square} {name}: {fmt.num(count)}{percent} · ⚡ {fmt.dur(median)}")
    return "\n".join(lines)


def _panel_tiles(stats: _stats.ContactStats, fmt: _Format) -> list[tuple[str, str]]:
    streaks = stats.streaks
    pairs = [
        (fmt.t("tile.streak"), fmt.days(streaks.current_days)),
        (fmt.t("tile.record"), fmt.days(streaks.longest_days)),
    ]
    silence = stats.silence
    if silence is not None:
        seconds = silence.seconds
        pause = fmt.days(seconds // 86400) if seconds >= 86400 else f"<b>{fmt.dur(seconds)}</b>"
        pairs.append((fmt.t("tile.pause"), pause))
    pairs.append((fmt.t("tile.sessions"), f"<b>{fmt.num(stats.sessions.total)}</b>"))
    first = _first_tile(stats)
    if first is not None:
        pairs.append((fmt.t("tile.first"), first))
    deletions = stats.deletions
    if deletions.total:
        deleted = f"<b>{fmt.num(deletions.total)}</b>"
        if deletions.unexplained.total:
            deleted += f" · ❓{fmt.num(deletions.unexplained.total)}"
        pairs.append((fmt.t("tile.deleted"), deleted))
    return pairs


def _previous(stats: _stats.ContactStats, fmt: _Format, period: str) -> str | None:
    """The change against the previous period, as HTML; None without one."""
    previous = stats.previous
    if previous is None or period == "all":
        return None
    change = _stats.change_percent(stats.messages.total, previous.messages.total)
    changed = (
        f"{fmt.num(previous.messages.total)} → {fmt.num(stats.messages.total)}" if change is None else f"{change:+d}%"
    )
    parts = [f"{fmt.t('previous.messages')} <b>{changed}</b>"]
    for square, before, after in (
        (CONTACT, previous.replies.received.median_seconds, stats.replies.received.median_seconds),
        (OWNER, previous.replies.sent.median_seconds, stats.replies.sent.median_seconds),
    ):
        # A reply time is compared only when both periods have one.
        if before is not None and after is not None:
            parts.append(f"{fmt.t('previous.reply')} {square} {fmt.dur(before)} → {fmt.dur(after)}")
    return f"📈 {fmt.t(f'previous.{period}')}: {' · '.join(parts)}"


def _footer(stats: _stats.ContactStats, fmt: _Format) -> list[str]:
    parts: list[str] = []
    retained = stats.missing.retained_from
    if retained is not None:
        parts.append(fmt.t("history_since", date=fmt.day(fmt.local(retained).date(), with_year=True)))
    parts.append(fmt.t("text_only"))
    return parts


# --- Public card ---------------------------------------------------------------------------------


def card(
    stats: _stats.ContactStats,
    window: _stats.StatsWindow,
    *,
    period: str,
    owner_name: str,
    contact_name: str,
    language_code: str | None = None,
) -> RichView:
    """The public card: both people see it, so it holds only the friendly numbers.

    Never deletions, edits, who is waiting, missing history, p90, the previous
    period, or message text. Names are the owner's and the contact's first
    names, never "You". The footer links to the Telesec site.
    """
    fmt = _format(window, language_code)
    owner = safe_name(owner_name, TITLE_NAME_CHARS // 2)
    contact = safe_name(contact_name, TITLE_NAME_CHARS // 2)
    owner_column = safe_name(owner_name, COLUMN_NAME_CHARS)
    contact_column = safe_name(contact_name, COLUMN_NAME_CHARS)
    title = f"📊 {owner} × {contact}"
    label = period_label(period, fmt.lang)
    before, _, after = fmt.t("counted_by", link="\0").partition("\0")
    footer_html = f'<footer>{before}<a href="{SITE_URL}">Telesec</a>{after}</footer>'
    footer_text = f"{before}Telesec{after} · {SITE_URL.removeprefix('https://')}"
    html_blocks: list[tuple[int, str]] = [(_KEEP, f"<h3>{_esc(title)}</h3>")]
    text_blocks: list[tuple[int, str]] = [(_KEEP, title)]

    if not stats.messages.total:
        html_blocks.append((_SUBTITLE, f"<p>{label} · {fmt.t('empty')}</p>"))
        text_blocks.append((_SUBTITLE, f"{label} · {fmt.t('empty')}"))
    else:
        span = _span(stats, fmt)
        total = fmt.num(stats.messages.total)
        days = stats.active_days.total
        in_touch = _days_in_touch(fmt, days)
        dates = [label, *([] if span is None else [span])]
        subtitle = [*dates, f"💬 <b>{total}</b>", f"<b>{fmt.num(days)}</b> {in_touch}"]
        html_blocks.append((_SUBTITLE, f"<p>{' · '.join(subtitle)}</p>"))
        text_blocks.append((_SUBTITLE, " · ".join([*dates, f"💬 {total}", f"{fmt.num(days)} {in_touch}"])))

        balance = _balance(stats)
        if balance is not None:
            html_blocks.append((_BAR, f"<p>{balance}</p>"))
        replies, turns, length, share = stats.replies, stats.turns, stats.text_length, stats.share
        rows = [
            (
                fmt.t("row.messages"),
                _messages_cell(fmt, stats.messages.received, share.received),
                _messages_cell(fmt, stats.messages.sent, share.sent),
            ),
            (
                fmt.t("row.reply"),
                _bold_dur(fmt, replies.received.median_seconds),
                _bold_dur(fmt, replies.sent.median_seconds),
            ),
            (fmt.t("row.per_turn"), fmt.decimal(turns.per_turn_received), fmt.decimal(turns.per_turn_sent)),
            (fmt.t("row.length"), _length(fmt, length.received), _length(fmt, length.sent)),
        ]
        html_blocks.append((_TABLE, _sides_table((_esc(contact_column), _esc(owner_column)), rows)))
        text_blocks.append((_TABLE, _side_lines(stats, fmt, contact_column, owner_column)))

        html_blocks.append((_GRID, _when(stats, fmt)))
        peak = _peak_text(stats, fmt)
        if peak is not None:
            html_blocks.append((_EXTRA, f"<p>{fmt.t('peak_card')} <mark>{peak}</mark></p>"))
            text_blocks.append((_EXTRA, f"{fmt.t('peak_card')} {peak}"))

        longest = stats.streaks.longest_days
        sessions = f"<b>{fmt.num(stats.sessions.total)}</b>"
        first = _first_tile(stats)
        if first is not None:
            sessions += f" · {fmt.t('first')} {first}"
        tiles = [
            [_td(fmt.t("tile.record")), _td(f"<b>{fmt.num(longest)}</b> {_in_a_row(fmt, longest)}", right=True)],
            [_td(fmt.t("tile.sessions")), _td(sessions, right=True)],
        ]
        html_blocks.append((_TILES, _table(tiles)))

    html_blocks.append((_KEEP, footer_html))
    text_blocks.append((_KEEP, footer_text))
    return _view(html_blocks, text_blocks)


def _days_in_touch(fmt: _Format, days: int) -> str:
    if fmt.lang == "ru":
        return f"{_ru_plural(days, 'день', 'дня', 'дней')} общения"
    return "day in touch" if days == 1 else "days in touch"


def _in_a_row(fmt: _Format, days: int) -> str:
    if fmt.lang == "ru":
        return f"{_ru_plural(days, 'день', 'дня', 'дней')} подряд"
    return "day in a row" if days == 1 else "days in a row"
