"""Telegram HTML views of one chat's correspondence stats.

``panel`` is the owner panel's report, shown only in the owner's private chat
with the bot. ``card`` is the public card for the in-chat ``/stats`` command:
it is posted into the Business chat, so the contact sees it too, and it never
shows deletions, edits, who is waiting, missing history, or p90 reply times.

Both are pure and deterministic: they format a ``ContactStats`` that never held
message text. The output is Telegram HTML (``PARSE_MODE``). Every untrusted
value (names, the time zone label) goes through ``correspondence_stats._safe``
and then ``html.escape``. ``<pre>`` lines are at most ``PRE_WIDTH`` columns
(``display_width``: wide characters such as CJK and emoji count 2) so they fit
a phone screen, names there are clipped to ``PRE_NAME_CHARS`` columns, and a
view stays within Telegram's 4096 UTF-16 units. A name with right-to-left
letters sits between two LRM marks, so it cannot reorder or flip the line
around it. Voice metadata can become one more table row later; nothing here
assumes text only except the footer.
"""

from __future__ import annotations

import html
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo

from . import correspondence_stats as _stats
from . import owner_panel as _owner_panel

PARSE_MODE = "HTML"
PERIODS = ("7d", "30d", "90d", "all")
SITE_URL = "https://telesec.sergeybaus.com"
MAX_TEXT_UNITS = 4096  # Telegram's message text limit, in UTF-16 code units.
PRE_WIDTH = 31  # columns of a <pre> line, counted with display_width
PRE_NAME_CHARS = 8  # columns of a name in a <pre> block
TITLE_NAME_CHARS = 64
BAR_CELLS = 16
BAR_FULL = "█"
BAR_EMPTY = "░"
SPARK = "▁▂▃▄▅▆▇█"
SPARK_ZERO = "·"
NONE = "–"  # a value that is not known, e.g. a reply time without replies
ELLIPSIS = "…"
LRM = "\u200e"  # left-to-right mark: zero width, a strong left-to-right character
_ZWJ = "\u200d"
# Bidi classes that lay text out right to left in a left-to-right line.
_RTL_CLASSES = frozenset({"R", "AL", "AN"})
HOURS_AXIS = "0     6     12    18  23"
_LABEL_WIDTH = 9  # label column of the summary block
_MIN_COLUMN = 7  # value column of the table
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# English defines every key; Russian falls back to English key by key. Values are
# plain text: the renderer escapes whole lines, so a template never holds HTML.
_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "period.7d": "7 d",
        "period.30d": "30 d",
        "period.90d": "90 d",
        "period.all": "all time",
        "you": "You",
        "active_days": "active days {days}/{span}",
        "days_in_touch": "days in touch {days}",
        "row.messages": "msgs",
        "row.messages_card": "messages",
        "row.reply": "reply",
        "row.p90": "p90",
        "row.per_turn": "per turn",
        "row.length": "length",
        "row.edits": "edits",
        "waiting": "⏳ no reply: {name}, since {since} ({duration})",
        "peak": "🕘 peak",
        "weekday_names": "Mon Tue Wed Thu Fri Sat Sun",
        "weekday_axis": "Mo Tu We Th Fr Sa Su",
        "bucket.day": "by day",
        "bucket.week": "by week",
        "bucket.month": "by month",
        "bucket.quarter": "by quarter",
        "bucket.year": "by year",
        "streak": "streak",
        "streak_days": "{days} d",
        "streak_max": "max {days}",
        "pause": "pause",
        "sessions": "sessions",
        "starts": "{name} starts {percent}%",
        "deleted": "deleted",
        "unexplained": "unexplained {count}",
        "record": "record",
        "record_days": "{days} days in a row",
        "record_day": "{days} day in a row",
        "previous": "📈 vs prev. {period}:",
        "previous_messages": "msgs {change}",
        "previous_reply": "{name} reply {before}→{after}",
        "history_since": "history since {date}",
        "period_partial": "{text} (period partial)",
        "text_only": "text only",
        "not_in_history": "deletions not in history {count}",
        "edit_only": "edits without original {count}",
        "empty": "no messages in this period",
        "counted_by": "counted by {link}",
        "units": "s m h d",
    },
    "ru": {
        "period.7d": "7 дн.",
        "period.30d": "30 дн.",
        "period.90d": "90 дн.",
        "period.all": "всё время",
        "you": "Вы",
        "active_days": "активных дней {days}/{span}",
        "days_in_touch": "дней общения {days}",
        "row.messages": "сообщ.",
        "row.messages_card": "сообщ.",
        "row.reply": "ответ",
        "row.p90": "p90",
        "row.per_turn": "подряд",
        "row.length": "длина",
        "row.edits": "правки",
        "waiting": "⏳ без ответа: {name}, с {since} ({duration})",
        "peak": "🕘 пик",
        "weekday_names": "пн вт ср чт пт сб вс",
        "weekday_axis": "пн вт ср чт пт сб вс",
        "bucket.day": "по дням",
        "bucket.week": "по неделям",
        "bucket.month": "по месяцам",
        "bucket.quarter": "по кварталам",
        "bucket.year": "по годам",
        "streak": "серия",
        "streak_days": "{days} дн.",
        "streak_max": "макс {days}",
        "pause": "пауза",
        "sessions": "беседы",
        "starts": "старт {name} {percent}%",
        "deleted": "удалено",
        "unexplained": "неясн. {count}",
        "record": "рекорд",
        "record_days": "{days} дн. подряд",
        "record_day": "{days} дн. подряд",
        "previous": "📈 к пред. {period}:",
        "previous_messages": "сообщ. {change}",
        "previous_reply": "ответ {name} {before}→{after}",
        "history_since": "история с {date}",
        "period_partial": "{text} (период неполный)",
        "text_only": "только текст",
        "not_in_history": "удалений вне истории {count}",
        "edit_only": "правок без оригинала {count}",
        "empty": "нет сообщений за период",
        "counted_by": "посчитал {link}",
        "units": "с м ч д",
    },
}


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + ELLIPSIS


def _extends(char: str) -> bool:
    # Marks, joiners, variation selectors, other format characters (such as LRM),
    # and skin tones belong to the character before them.
    return unicodedata.category(char) in ("Mn", "Me", "Mc", "Cf") or 0x1F3FB <= ord(char) <= 0x1F3FF


def _regional(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def graphemes(text: str) -> list[str]:
    """``text`` as characters a reader sees: a base with its marks, emoji ZWJ sequences, and flag pairs.

    An approximation of Unicode grapheme clusters, good enough to clip and pad names.
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


def _cluster_width(cluster: str) -> int:
    if all(_extends(char) for char in cluster):
        return 0
    wide = (
        "\ufe0f" in cluster  # emoji presentation
        or _regional(cluster[0])
        or any(unicodedata.east_asian_width(char) in "WF" for char in cluster)
    )
    return 2 if wide else 1


def display_width(text: str) -> int:
    """Monospace columns: wide (CJK, emoji) characters take 2, marks and format characters 0."""
    return sum(_cluster_width(cluster) for cluster in graphemes(text))


def _clip_columns(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` columns, cut between characters with "…" when it is longer."""
    if display_width(text) <= limit:
        return text
    kept: list[str] = []
    width = 0
    for cluster in graphemes(text):
        width += _cluster_width(cluster)
        if width > limit - 1:  # the ellipsis takes the last column
            break
        kept.append(cluster)
    return "".join(kept) + ELLIPSIS


def _ljust(text: str, width: int) -> str:
    return text + " " * (width - display_width(text))


def _rjust(text: str, width: int) -> str:
    return " " * (width - display_width(text)) + text


def _isolate(name: str) -> str:
    """A name with right-to-left letters between LRMs, so the line around it stays left to right.

    Telegram picks each line's direction from its first strong character, and an
    unisolated Arabic or Hebrew name would flip the line and pull numbers past it.
    ``_safe`` escaped every bidi control the name had, so it cannot undo the marks.
    """
    if any(unicodedata.bidirectional(char) in _RTL_CLASSES for char in name):
        return f"{LRM}{name}{LRM}"
    return name


def safe_name(text: str, limit: int) -> str:
    """An untrusted name, escaped like the CLI does and clipped to ``limit`` code points; not yet HTML-escaped."""
    return _isolate(_clip(_stats._safe(text), limit))


def pre_name(text: str) -> str:
    """An untrusted name for a ``<pre>`` block: like ``safe_name``, clipped to ``PRE_NAME_CHARS`` columns."""
    return _isolate(_clip_columns(_stats._safe(text), PRE_NAME_CHARS))


def _round_percent(part: int, whole: int) -> int:
    # Half up in integers, like the stats themselves.
    return (200 * part + whole) // (2 * whole)


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

    def dur(self, seconds: int | None) -> str:
        """Compact and rounded down: 40s, 12m, 1h40m, 2h, 3d5h, 12d; a zero minor unit is dropped."""
        if seconds is None:
            return NONE
        s, m, h, d = self.t("units").split()
        seconds = max(0, seconds)
        if seconds < 60:
            return f"{seconds}{s}"
        if seconds < 3600:
            return f"{seconds // 60}{m}"
        if seconds < 86400:
            minutes = seconds % 3600 // 60
            return f"{seconds // 3600}{h}" + (f"{minutes}{m}" if minutes else "")
        days, hours = seconds // 86400, seconds % 86400 // 3600
        # Hours only while they still matter: 3d5h, but 12d.
        return f"{days}{d}" + (f"{hours}{h}" if hours and days < 10 else "")

    def day(self, day: date, *, with_year: bool | None = None) -> str:
        if with_year is None:
            with_year = day.year != self.year
        if self.lang == "ru":
            return f"{day.day:02d}.{day.month:02d}" + (f".{day.year}" if with_year else "")
        return f"{day.day} {_MONTHS[day.month - 1]}" + (f" {day.year}" if with_year else "")

    def span(self, first: date, last: date, separator: str = "–") -> str:
        # Both ends show the year when either is outside the current year.
        if first == last:
            return self.day(first)
        with_year = first.year != self.year or last.year != self.year
        return f"{self.day(first, with_year=with_year)}{separator}{self.day(last, with_year=with_year)}"

    def local(self, at: datetime) -> datetime:
        return at.astimezone(self.tz)

    def clock(self, at: datetime, now: datetime) -> str:
        """HH:MM today, else the date and HH:MM."""
        local = self.local(at)
        time = local.strftime("%H:%M")
        return time if local.date() == self.local(now).date() else f"{self.day(local.date())} {time}"


def _format(window: _stats.StatsWindow, language_code: str | None) -> _Format:
    return _Format(
        lang=_owner_panel.language(language_code),
        tz=window.tz,
        year=window.now.astimezone(window.tz).year,
    )


def period_label(period: str, language_code: str | None = None) -> str:
    """The period in a title, such as "30 d" or "всё время"."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    lang = _owner_panel.language(language_code)
    return _STRINGS[lang].get(f"period.{period}") or _STRINGS["en"][f"period.{period}"]


# --- Building blocks -----------------------------------------------------------------------------


def spark(counts: Sequence[int]) -> str:
    """One glyph per value, scaled to the largest: 0 is "·", the largest is "█"."""
    top = max(counts, default=0)
    return "".join(SPARK_ZERO if value <= 0 else SPARK[-(-value * len(SPARK) // top) - 1] for value in counts)


def bar(share: int) -> str:
    filled = min(BAR_CELLS, max(0, (2 * share * BAR_CELLS + 100) // 200))
    return BAR_FULL * filled + BAR_EMPTY * (BAR_CELLS - filled)


def _cut(line: str) -> str:
    # Defensive only: every block is sized to fit.
    return _clip_columns(line, PRE_WIDTH)


def _pre(lines: Sequence[str]) -> str:
    return "<pre>" + _esc("\n".join(_cut(line) for line in lines)) + "</pre>"


def _bars(rows: Sequence[tuple[str, int]]) -> list[str]:
    width = max(display_width(name) for name, _ in rows)
    return [f"{_ljust(name, width)} {bar(share)} {share}%" for name, share in rows]


def _table(names: tuple[str, str], rows: Sequence[tuple[str, str, str]]) -> list[str]:
    label_width = max(display_width(label) for label, _, _ in rows) + 1
    widest = max(display_width(cell) for cell in (*names, *(value for _, *values in rows for value in values)))
    column = max(_MIN_COLUMN, widest + 1)
    if label_width + 2 * column > PRE_WIDTH:
        column = widest + 1
    lines = [" " * label_width + "".join(_rjust(name, column) for name in names)]
    lines.extend(
        _ljust(label, label_width) + _rjust(first, column) + _rjust(second, column) for label, first, second in rows
    )
    return lines


def _row(label: str, parts: Sequence[str]) -> list[str]:
    """A summary row: the label column, then the parts joined by " · ", wrapped when too wide."""
    head = _ljust(label, _LABEL_WIDTH) + parts[0]
    line = " · ".join([head, *parts[1:]])
    if display_width(line) <= PRE_WIDTH:
        return [line]
    # Each further part gets its own line under the values, or as far left as it needs.
    return [head, *(" " * min(_LABEL_WIDTH, max(0, PRE_WIDTH - display_width(part))) + part for part in parts[1:])]


def _rhythm(stats: _stats.ContactStats, fmt: _Format) -> list[str]:
    lines = [
        spark(stats.hours),
        HOURS_AXIS,
        " ".join(glyph * 2 for glyph in spark(stats.weekdays)),
        fmt.t("weekday_axis"),
    ]
    activity = stats.activity
    if activity is not None:
        bucket = fmt.t(f"bucket.{activity.bucket}")
        dates = fmt.span(activity.first_day, activity.last_day, " → ")
        label = f"{bucket} {dates}"
        lines.extend([spark(activity.counts), *([label] if display_width(label) <= PRE_WIDTH else [bucket, dates])])
    return lines


def _peak(stats: _stats.ContactStats, fmt: _Format) -> str | None:
    hour, weekday = stats.peak_hour, stats.peak_weekday
    if hour is None or weekday is None:
        return None
    day = fmt.t("weekday_names").split()[weekday]
    return f"<b>{_esc(fmt.t('peak'))}</b> {_esc(day)} {hour}–{hour + 1}"


def _per_turn(value: float | None) -> str:
    return NONE if value is None else f"{value:.1f}"


def _optional(fmt: _Format, value: int | None) -> str:
    return NONE if value is None else fmt.num(value)


def _sessions_row(stats: _stats.ContactStats, fmt: _Format, contact: str) -> list[str]:
    sessions = stats.sessions
    parts = [fmt.num(sessions.total)]
    if sessions.total:
        parts.append(fmt.t("starts", name=contact, percent=_round_percent(sessions.started_by_contact, sessions.total)))
    return _row(fmt.t("sessions"), parts)


def _shares(stats: _stats.ContactStats, contact: str, owner: str) -> str | None:
    share = stats.share
    if share.received is None or share.sent is None:
        return None
    return _pre(_bars([(contact, share.received), (owner, share.sent)]))


def _range_dates(stats: _stats.ContactStats, fmt: _Format) -> str | None:
    activity = stats.activity
    return None if activity is None else fmt.span(activity.first_day, activity.last_day)


def _finish(blocks: Sequence[str], tail: Sequence[str] = ()) -> str:
    # Each block is whole lines with balanced tags, so dropping trailing blocks keeps
    # the HTML valid. Names are clipped, so this only guards against surprises.
    # ``tail`` always stays at the end; the report's own blocks go first.
    kept = list(blocks)
    while len(kept) > 1 and utf16_units("\n".join([*kept, *tail])) > MAX_TEXT_UNITS:
        kept.pop()
    return "\n".join([*kept, *tail])


# --- Owner panel ---------------------------------------------------------------------------------


def panel(
    stats: _stats.ContactStats,
    window: _stats.StatsWindow,
    *,
    period: str,
    language_code: str | None = None,
    contact_name: str | None = None,
    note: str | None = None,
) -> str:
    """The owner panel's report: everything the stats know, compact.

    ``contact_name`` is the short name for the columns (the contact's first name);
    the title shows the full name. Owner-only: it includes deletions, edits,
    who is waiting, p90 reply times, and missing history. ``note`` is a fixed
    line of Telegram HTML (a catalog string) kept as the last line, even when
    the report must be cut to fit.
    """
    tail = () if note is None else (note,)
    fmt = _format(window, language_code)
    title_name = safe_name(stats.name or stats.display_name, TITLE_NAME_CHARS)
    contact = pre_name(contact_name or stats.name or stats.display_name)
    owner = fmt.t("you")
    dates = _range_dates(stats, fmt)
    tz_label = _stats._safe(window.tz_label)
    blocks = [
        f"<b>{_esc(f'📊 {title_name} · {period_label(period, fmt.lang)}')}</b>",
        f"<i>{_esc(tz_label if dates is None else f'{dates} · {tz_label}')}</i>",
        "",
    ]
    if not stats.messages.total:
        blocks.append(_esc(fmt.t("empty")))
        return _finish(blocks, tail)

    activity = stats.activity
    days = (
        fmt.num(stats.active_days.total)
        if activity is None
        else fmt.t(
            "active_days",
            days=fmt.num(stats.active_days.total),
            span=fmt.num((activity.last_day - activity.first_day).days + 1),
        )
    )
    blocks.append(f"<b>{_esc(f'💬 {fmt.num(stats.messages.total)}')}</b> · {_esc(days)}")
    shares = _shares(stats, contact, owner)
    if shares is not None:
        blocks.append(shares)

    replies, turns, length, edited = stats.replies, stats.turns, stats.text_length, stats.edited
    rows = [
        (fmt.t("row.messages"), fmt.num(stats.messages.received), fmt.num(stats.messages.sent)),
        (fmt.t("row.reply"), fmt.dur(replies.received.median_seconds), fmt.dur(replies.sent.median_seconds)),
        (fmt.t("row.p90"), fmt.dur(replies.received.p90_seconds), fmt.dur(replies.sent.p90_seconds)),
        (fmt.t("row.per_turn"), _per_turn(turns.per_turn_received), _per_turn(turns.per_turn_sent)),
        (fmt.t("row.length"), _optional(fmt, length.received), _optional(fmt, length.sent)),
    ]
    if edited.received or edited.sent:
        rows.append((fmt.t("row.edits"), fmt.num(edited.received), fmt.num(edited.sent)))
    blocks.append(_pre(_table((contact, owner), rows)))

    waiting = stats.waiting
    if waiting is not None:
        line = fmt.t(
            "waiting",
            name=contact if waiting.side == "received" else owner,
            since=fmt.clock(waiting.since, window.now),
            duration=fmt.dur(waiting.seconds),
        )
        blocks.append(_esc(line))

    peak = _peak(stats, fmt)
    if peak is not None:
        blocks.append(peak)
    blocks.append(_pre(_rhythm(stats, fmt)))

    streaks = stats.streaks
    summary = _row(
        fmt.t("streak"),
        [
            fmt.t("streak_days", days=fmt.num(streaks.current_days)),
            fmt.t("streak_max", days=fmt.num(streaks.longest_days)),
        ],
    )
    silence = stats.silence
    if silence is not None:
        pause = fmt.span(fmt.local(silence.start).date(), fmt.local(silence.end).date())
        summary.extend(_row(fmt.t("pause"), [fmt.dur(silence.seconds), pause]))
    summary.extend(_sessions_row(stats, fmt, contact))
    deletions = stats.deletions
    if deletions.total:
        parts = [fmt.num(deletions.total)]
        if deletions.unexplained.total:
            parts.append(fmt.t("unexplained", count=fmt.num(deletions.unexplained.total)))
        summary.extend(_row(fmt.t("deleted"), parts))
    blocks.append(_pre(summary))

    previous = stats.previous
    if previous is not None:
        change = _stats.change_percent(stats.messages.total, previous.messages.total)
        changed = (
            f"{fmt.num(previous.messages.total)}→{fmt.num(stats.messages.total)}" if change is None else f"{change:+d}%"
        )
        parts = [fmt.t("previous_messages", change=changed)]
        for name, before, after in (
            (contact, previous.replies.received.median_seconds, replies.received.median_seconds),
            (owner, previous.replies.sent.median_seconds, replies.sent.median_seconds),
        ):
            # A reply time is compared only when both periods have one.
            if before is not None and after is not None:
                parts.append(fmt.t("previous_reply", name=name, before=fmt.dur(before), after=fmt.dur(after)))
        heading = fmt.t("previous", period=period_label(period, fmt.lang))
        blocks.append(f"<b>{_esc(heading)}</b> {_esc(' · '.join(parts))}")

    blocks.append(f"<i>{_esc(' · '.join(_footer(stats, fmt, window)))}</i>")
    return _finish(blocks, tail)


def _footer(stats: _stats.ContactStats, fmt: _Format, window: _stats.StatsWindow) -> list[str]:
    missing = stats.missing
    parts: list[str] = []
    if missing.retained_from is not None:
        since = fmt.t("history_since", date=fmt.day(fmt.local(missing.retained_from).date(), with_year=True))
        parts.append(fmt.t("period_partial", text=since) if missing.range_partial else since)
    if missing.deletions_not_in_history:
        parts.append(fmt.t("not_in_history", count=fmt.num(missing.deletions_not_in_history)))
    if missing.edit_only_messages:
        parts.append(fmt.t("edit_only", count=fmt.num(missing.edit_only_messages)))
    parts.append(fmt.t("text_only"))
    parts.append(fmt.local(window.now).strftime("%H:%M"))
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
) -> str:
    """The public card: both people see it, so it holds only the friendly numbers.

    Never deletions, edits, who is waiting, missing history, p90, or message
    text. Names are the owner's and the contact's first names, never "You".
    Send it with link previews disabled: the footer links to the Telesec site.
    """
    fmt = _format(window, language_code)
    owner_title = safe_name(owner_name, TITLE_NAME_CHARS // 2)
    contact_title = safe_name(contact_name, TITLE_NAME_CHARS // 2)
    owner = pre_name(owner_name)
    contact = pre_name(contact_name)
    dates = _range_dates(stats, fmt)
    blocks = [f"<b>{_esc(f'📊 {owner_title} × {contact_title} · {period_label(period, fmt.lang)}')}</b>"]
    if dates is not None:
        blocks.append(f"<i>{_esc(dates)}</i>")
    blocks.append("")
    if not stats.messages.total:
        blocks.append(_esc(fmt.t("empty")))
    else:
        days = fmt.t("days_in_touch", days=fmt.num(stats.active_days.total))
        blocks.append(f"<b>{_esc(f'💬 {fmt.num(stats.messages.total)}')}</b> · {_esc(days)}")
        shares = _shares(stats, contact, owner)
        if shares is not None:
            blocks.append(shares)
        replies, turns, length = stats.replies, stats.turns, stats.text_length
        rows = [
            (fmt.t("row.messages_card"), fmt.num(stats.messages.received), fmt.num(stats.messages.sent)),
            (fmt.t("row.reply"), fmt.dur(replies.received.median_seconds), fmt.dur(replies.sent.median_seconds)),
            (fmt.t("row.per_turn"), _per_turn(turns.per_turn_received), _per_turn(turns.per_turn_sent)),
            (fmt.t("row.length"), _optional(fmt, length.received), _optional(fmt, length.sent)),
        ]
        blocks.append(_pre(_table((contact, owner), rows)))
        peak = _peak(stats, fmt)
        if peak is not None:
            blocks.append(peak)
        blocks.append(_pre(_rhythm(stats, fmt)))
        longest = stats.streaks.longest_days
        record = fmt.t("record_day" if longest == 1 else "record_days", days=fmt.num(longest))
        blocks.append(_pre([*_row(fmt.t("record"), [record]), *_sessions_row(stats, fmt, contact)]))
    before, _, after = fmt.t("counted_by", link="\0").partition("\0")
    blocks.append(f'<i>{_esc(before)}<a href="{SITE_URL}">Telesec</a>{_esc(after)}</i>')
    return _finish(blocks)
