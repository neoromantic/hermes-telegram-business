"""stats_render: the owner panel report and the public card, over synthetic stats.

Goldens pin the exact rich message HTML and plain fallbacks. ``_check`` checks
every view against the rich message rules: allowed tags and attributes only,
balanced, every untrusted value escaped, right-to-left names that cannot flip a
line, and the size budgets.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
import unicodedata
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import test_history as th

plugin = th.plugin

NOW = datetime(2026, 10, 19, 11, 32, tzinfo=UTC)  # 13:32 in Berlin
TZ = "Europe/Berlin"


@pytest.fixture(autouse=True)
def _no_hermes_timezone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)


@pytest.fixture
def cs(plugin):
    return importlib.import_module(f"{plugin.__name__}.correspondence_stats")


@pytest.fixture
def render(plugin):
    return importlib.import_module(f"{plugin.__name__}.stats_render")


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def _window(cs, period: str | None, *, tz: str = TZ):
    """The panel's window: the period's whole local days up to now, or all history."""
    since = None
    if period is not None:
        days = int(period.removesuffix("d"))
        since = (NOW.astimezone(ZoneInfo(tz)).date() - timedelta(days=days - 1)).isoformat()
    args = argparse.Namespace(since=since, until=None, tz=tz, session_gap=None)
    return cs.build_window(args, now=NOW)


def _days(first: date, counts: list[int]) -> tuple[date, date]:
    return first, first + timedelta(days=len(counts) - 1)


def _base(cs, **changes: Any):
    stats = cs.ContactStats(
        business_connection_id="business-123",
        chat_id=991,
        chat_type="private",
        name="Денис Петров",
        username="denis",
        display_name="Денис Петров (@denis)",
        messages=cs.SideCounts(),
        active_days=cs.ActiveDays(),
        latest=cs.Latest(),
        sessions=cs.Sessions(),
        deletions=cs.Deletions(),
        missing=cs.MissingHistory(retained_from=_utc(2025, 10, 21, 8, 0)),
    )
    return replace(stats, **changes)


HOURS = (0, 0, 0, 0, 0, 6, 12, 20, 40, 50, 40, 32, 32, 40, 50, 50, 40, 50, 60, 70, 72, 90, 60, 18)
WEEKDAYS = (180, 260, 120, 190, 150, 90, 60)
MONTH_DAYS = [12, 0, 40, 55, 38, 61, 70, 45, 30, 52, 66, 48, 35, 58, 72, 25, 0, 0, 0, 0]
MONTH_DAYS += [33, 47, 59, 64, 41, 29, 55, 68, 80, 21]


def _month(cs, window):
    """The 30 days in Berlin: everything the panel can show."""
    first, last = _days(date(2026, 9, 20), MONTH_DAYS)
    return _base(
        cs,
        messages=cs.SideCounts(received=745, sent=539),
        active_days=cs.ActiveDays(total=25, received=24, sent=22),
        sessions=cs.Sessions(total=41, started_by_contact=25, started_by_owner=16, ended_on_received=20),
        deletions=cs.Deletions(total=3, unexplained=cs.SideCounts(received=1), likely_correction=2),
        share=cs.Share(received=58, sent=42),
        turns=cs.Turns(received=355, sent=385, per_turn_received=2.1, per_turn_sent=1.4),
        replies=cs.Replies(
            received=cs.ReplyTimes(count=96, median_seconds=720, p90_seconds=6000),
            sent=cs.ReplyTimes(count=88, median_seconds=2280, p90_seconds=11100),
        ),
        waiting=cs.Waiting(side="received", since=_utc(2026, 10, 19, 9, 22), seconds=7800),
        text_length=cs.TextLength(received=42, sent=18),
        edited=cs.SideCounts(received=7, sent=5),
        hours=HOURS,
        weekdays=WEEKDAYS,
        activity=cs.Activity(bucket="day", start=first, counts=tuple(MONTH_DAYS), first_day=first, last_day=last),
        streaks=cs.Streaks(
            current_days=10, longest_days=14, longest_start=date(2026, 9, 22), longest_end=date(2026, 10, 5)
        ),
        silence=cs.Silence(seconds=352800, start=_utc(2026, 10, 5, 20, 0), end=_utc(2026, 10, 9, 22, 0)),
        previous=cs.PreviousPeriod(
            since=window.since - timedelta(days=30),
            until=window.since - timedelta(microseconds=1),
            messages=cs.SideCounts(received=600, sent=488),
            active_days=cs.ActiveDays(total=20),
            replies=cs.Replies(
                received=cs.ReplyTimes(count=80, median_seconds=1260, p90_seconds=5000),
                sent=cs.ReplyTimes(count=70, median_seconds=2400, p90_seconds=9000),
            ),
        ),
    )


def _week(cs, window):
    """The last 7 days: the owner's turn waits, no deletions or edits, an empty previous week."""
    counts = [65, 50, 38, 61, 70, 55, 31]
    first, last = _days(date(2026, 10, 13), counts)
    return _base(
        cs,
        messages=cs.SideCounts(received=212, sent=158),
        active_days=cs.ActiveDays(total=7, received=7, sent=7),
        sessions=cs.Sessions(total=19, started_by_contact=8, started_by_owner=11, ended_on_received=9),
        share=cs.Share(received=57, sent=43),
        turns=cs.Turns(received=92, sent=99, per_turn_received=2.3, per_turn_sent=1.6),
        replies=cs.Replies(
            received=cs.ReplyTimes(count=40, median_seconds=540, p90_seconds=4200),
            sent=cs.ReplyTimes(count=35, median_seconds=1500, p90_seconds=9000),
        ),
        waiting=cs.Waiting(side="sent", since=_utc(2026, 10, 18, 21, 40), seconds=49920),
        text_length=cs.TextLength(received=38, sent=21),
        hours=HOURS,
        weekdays=WEEKDAYS,
        activity=cs.Activity(bucket="day", start=first, counts=tuple(counts), first_day=first, last_day=last),
        streaks=cs.Streaks(current_days=7, longest_days=7, longest_start=first, longest_end=last),
        silence=cs.Silence(seconds=34800, start=_utc(2026, 10, 14, 21, 0), end=_utc(2026, 10, 15, 6, 40)),
        previous=cs.PreviousPeriod(
            since=window.since - timedelta(days=7),
            until=window.since - timedelta(microseconds=1),
            messages=cs.SideCounts(),
            active_days=cs.ActiveDays(),
            replies=cs.Replies(),
        ),
    )


def _all(cs, window):
    """All history, a year: month buckets, a pause across New Year, no previous period."""
    counts = (310, 980, 1120, 870, 1010, 1240, 1190, 930, 760, 820, 1300, 1450, 345)
    return _base(
        cs,
        messages=cs.SideCounts(received=6948, sent=5377),
        active_days=cs.ActiveDays(total=312, received=300, sent=290),
        sessions=cs.Sessions(total=412, started_by_contact=239, started_by_owner=173, ended_on_received=200),
        deletions=cs.Deletions(total=37, unexplained=cs.SideCounts(received=3, sent=1), likely_correction=33),
        share=cs.Share(received=56, sent=44),
        turns=cs.Turns(received=3310, sent=3840, per_turn_received=2.1, per_turn_sent=1.4),
        replies=cs.Replies(
            received=cs.ReplyTimes(count=2900, median_seconds=540, p90_seconds=7200),
            sent=cs.ReplyTimes(count=3100, median_seconds=1440, p90_seconds=14400),
        ),
        text_length=cs.TextLength(received=42, sent=18),
        edited=cs.SideCounts(received=120, sent=85),
        hours=HOURS,
        weekdays=WEEKDAYS,
        activity=cs.Activity(
            bucket="month",
            start=date(2025, 10, 1),
            counts=counts,
            first_day=date(2025, 10, 21),
            last_day=date(2026, 10, 19),
        ),
        streaks=cs.Streaks(
            current_days=3, longest_days=23, longest_start=date(2026, 3, 2), longest_end=date(2026, 3, 24)
        ),
        silence=cs.Silence(seconds=1051200, start=_utc(2025, 12, 28, 10, 0), end=_utc(2026, 1, 9, 22, 0)),
    )


def _partial(cs, window):
    """90 days, but history starts on 1 Sep: only the contact wrote, so most owner values are unknown."""
    counts = (0, 0, 0, 0, 0, 0, 2, 0, 0, 1, 0, 0, 2, 0)
    return _base(
        cs,
        name=None,
        display_name="@denis",
        messages=cs.SideCounts(received=5),
        active_days=cs.ActiveDays(total=3, received=3),
        sessions=cs.Sessions(total=3, started_by_contact=3, ended_on_received=3),
        missing=cs.MissingHistory(
            retained_from=_utc(2026, 9, 1, 6, 0), range_partial=True, deletions_not_in_history=2, edit_only_messages=1
        ),
        share=cs.Share(received=100, sent=0),
        turns=cs.Turns(received=3, per_turn_received=1.7),
        replies=cs.Replies(),
        waiting=cs.Waiting(side="received", since=_utc(2026, 10, 8, 7, 5), seconds=966420),
        text_length=cs.TextLength(received=12),
        hours=(0,) * 9 + (2, 1, 0, 0, 0, 0, 0, 0, 0, 2) + (0,) * 5,
        weekdays=(0, 2, 0, 3, 0, 0, 0),
        activity=cs.Activity(
            bucket="week",
            start=date(2026, 7, 20),
            counts=counts,
            first_day=date(2026, 7, 22),
            last_day=date(2026, 10, 19),
        ),
        streaks=cs.Streaks(
            current_days=0, longest_days=1, longest_start=date(2026, 9, 1), longest_end=date(2026, 9, 1)
        ),
        silence=cs.Silence(seconds=1987200, start=_utc(2026, 9, 15, 7, 0), end=_utc(2026, 10, 8, 7, 0)),
    )


def _empty(cs, window):
    activity = None
    if window.since is not None:
        first, last = window.since.astimezone(window.tz).date(), NOW.astimezone(window.tz).date()
        activity = cs.Activity(
            bucket="day", start=first, counts=(0,) * ((last - first).days + 1), first_day=first, last_day=last
        )
    return _base(cs, activity=activity)


# --- Helpers ---------------------------------------------------------------------
# Russian names for a Russian owner, English ones otherwise; Berlin time.

NAMES = {"ru": ("Сергей", "Денис"), "en": ("Sam", "Denis")}
BUILDERS = {"month": _month, "week": _week, "all": _all, "partial": _partial, "empty": _empty}
SITE = "https://telesec.sergeybaus.com"
ALLOWED_TAGS = {"h3", "h4", "p", "br", "b", "i", "mark", "blockquote", "footer", "a", "table", "tr", "th", "td"}
TABLE_ATTRS = {("striped", None), ("compact", None), ("bordered", None)}
# Named entities rich messages accept, and the numeric one html.escape writes for "'".
_BAD_ENTITY_RE = re.compile(r"&(?!(?:lt|gt|amp|quot|apos|nbsp|hellip|mdash|ndash|#x27);)")


def _stats_for(cs, case: str, period: str):
    window = _window(cs, None if period == "all" else period)
    return BUILDERS[case](cs, window), window


def _panel(cs, render, case: str, period: str, lang: str | None, *, note: str | None = None, **changes: Any):
    stats, window = _stats_for(cs, case, period)
    contact = changes.pop("contact_name", NAMES[render._owner_panel.language(lang)][1])
    return render.panel(
        replace(stats, **changes), window, period=period, language_code=lang, contact_name=contact, note=note
    )


def _card(
    cs, render, case: str, period: str, lang: str | None, *, owner: str | None = None, contact: str | None = None,
    **changes: Any,
):
    stats, window = _stats_for(cs, case, period)
    names = NAMES[render._owner_panel.language(lang)]
    return render.card(
        replace(stats, **changes),
        window,
        period=period,
        owner_name=owner or names[0],
        contact_name=contact or names[1],
        language_code=lang,
    )


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"tag {tag}")
        allowed = {
            "table": set(attrs) <= TABLE_ATTRS,
            "td": attrs in ([], [("align", "right")]),
            "th": attrs in ([], [("align", "right")]),
            "a": attrs == [("href", SITE)],
        }.get(tag, attrs == [])
        if not allowed:
            self.errors.append(f"attributes {tag} {attrs}")
        if tag != "br":
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced {tag}")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.errors.append(f"self-closing {tag}")


def _first_strong(line: str) -> str | None:
    return next((cls for cls in map(unicodedata.bidirectional, line) if cls in ("L", "R", "AL")), None)


def _check_direction(line: str) -> None:
    """A line must start left to right, and every right-to-left run must sit between two LRMs."""
    assert _first_strong(line) in (None, "L"), line
    outside = re.sub("\u200e[^\u200e]*\u200e", "", line)
    assert not any(unicodedata.bidirectional(char) in ("R", "AL", "AN") for char in outside), line


def _check(view, render=None) -> str:
    """Assert a valid rich message within the budgets and a fallback within 4096; returns the visible HTML text."""
    tags = _Tags()
    tags.feed(view.html)
    tags.close()
    assert tags.errors == [] and tags.stack == [], tags.errors
    # Outside the tags, no raw "<" or ">", and "&" only in an allowed entity.
    assert not re.search(r"[<>]", re.sub(r"<[^>]+>", "", view.html))
    assert not _BAD_ENTITY_RE.search(view.html)
    limit = 8000 if render is None else render.MAX_HTML_UNITS
    assert len(view.html.encode("utf-16-le")) // 2 <= limit
    assert len(view.text.encode("utf-16-le")) // 2 <= 4096
    import html as _html

    visible = _html.unescape(re.sub(r"<[^>]+>", "", view.html))
    for line in [*visible.split("\n"), *view.text.split("\n")]:
        _check_direction(line)
    return visible


# --- Goldens ---------------------------------------------------------------------

PANEL_MONTH_RU = """
<h3>📊 Денис Петров · 30 дней</h3>
<p>20 сен – 19 окт · 💬 <b>1 284</b> · <b>25</b> из 30 дней</p>
<p>58% 🟦🟦🟦🟦🟦🟦🟩🟩🟩🟩 42%</p>
<table striped compact>
<tr><th></th><th align="right">🟦 Денис</th><th align="right">🟩 Вы</th></tr>
<tr><td>сообщений</td><td align="right"><b>745</b> · 58%</td><td align="right"><b>539</b> · 42%</td></tr>
<tr><td>⚡ ответ</td><td align="right"><b>12 мин</b></td><td align="right"><b>38 мин</b></td></tr>
<tr><td>p90</td><td align="right">1 ч 40 мин</td><td align="right">3 ч 5 мин</td></tr>
<tr><td>подряд</td><td align="right">2,1</td><td align="right">1,4</td></tr>
<tr><td>длина</td><td align="right">42</td><td align="right">18</td></tr>
<tr><td>правки</td><td align="right">7</td><td align="right">5</td></tr>
</table>
<blockquote>⏳ Денис ждёт ответа <b>2 ч 10 мин</b></blockquote>
<h4>🗓 Когда</h4>
<table compact>
<tr><th>пн</th><th>вт</th><th>ср</th><th>чт</th><th>пт</th><th>сб</th><th>вс</th></tr>
<tr><td></td><td></td><td></td><td></td><td></td><td></td><td>🟨</td></tr>
<tr><td>·</td><td>🟧</td><td>🟥</td><td>🟧</td><td>🟥</td><td>🟥</td><td>🟧</td></tr>
<tr><td>🟧</td><td>🟧</td><td>🟥</td><td>🟧</td><td>🟧</td><td>🟥</td><td>🟥</td></tr>
<tr><td>🟨</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟧</td><td>🟧</td></tr>
<tr><td>🟥</td><td>🟥</td><td>🟧</td><td>🟧</td><td>🟥</td><td>🟥</td><td>🟥</td></tr>
<tr><td>🟨</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
<table compact>
<tr><th>0</th><th>3</th><th>6</th><th>9</th><th>12</th><th>15</th><th>18</th><th>21</th></tr>
<tr><td>·</td><td>🟨</td><td>🟧</td><td>🟧</td><td>🟧</td><td>🟥</td><td>🟥</td><td>🟥</td></tr>
</table>
<p>🎯 Пик — <mark>вт, 21–22</mark></p>
<table compact>
<tr><td>🔥 серия</td><td align="right"><b>10</b> дней</td><td>🏆 рекорд</td><td align="right"><b>14</b> дней</td></tr>
<tr><td>💤 пауза</td><td align="right"><b>4</b> дня</td><td>🗣 бесед</td><td align="right"><b>41</b></td></tr>
<tr><td>👋 первым</td><td align="right">🟦 <b>61%</b></td><td>🗑 удалено</td><td align="right"><b>3</b> · ❓1</td></tr>
</table>
<p>📈 К прошлым 30 дням: сообщений <b>+18%</b> · ответ 🟦 21 мин → 12 мин · ответ 🟩 40 мин → 38 мин</p>
<footer>история с 21 окт 2025 · только текст</footer>
""".strip("\n")
PANEL_MONTH_RU_TEXT = """
📊 Денис Петров · 30 дней
20 сен – 19 окт · 💬 1 284 · 25 из 30 дней
🟦 Денис: 745 (58%) · ⚡ 12 мин
🟩 Вы: 539 (42%) · ⚡ 38 мин
⏳ Денис ждёт ответа 2 ч 10 мин
🎯 Пик — вт, 21–22
история с 21 окт 2025 · только текст
""".strip("\n")
CARD_ALL_EN = """
<h3>📊 Sam × Denis</h3>
<p>all time · 21 Oct 2025 – 19 Oct 2026 · 💬 <b>12,325</b> · <b>312</b> days in touch</p>
<p>56% 🟦🟦🟦🟦🟦🟦🟩🟩🟩🟩 44%</p>
<table striped compact>
<tr><th></th><th align="right">🟦 Denis</th><th align="right">🟩 Sam</th></tr>
<tr><td>messages</td><td align="right"><b>6,948</b> · 56%</td><td align="right"><b>5,377</b> · 44%</td></tr>
<tr><td>⚡ reply</td><td align="right"><b>9 min</b></td><td align="right"><b>24 min</b></td></tr>
<tr><td>per turn</td><td align="right">2.1</td><td align="right">1.4</td></tr>
<tr><td>length</td><td align="right">42</td><td align="right">18</td></tr>
</table>
<h4>🗓 When</h4>
<table compact>
<tr><th></th><th>J</th><th>F</th><th>M</th><th>A</th><th>M</th><th>J</th><th>J</th><th>A</th><th>S</th><th>O</th><th>N</th><th>D</th></tr>
<tr><td>2025</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td>🟨</td><td>🟥</td><td>🟥</td></tr>
<tr><td>2026</td><td>🟧</td><td>🟥</td><td>🟥</td><td>🟥</td><td>🟧</td><td>🟧</td><td>🟧</td><td>🟥</td><td>🟥</td><td>🟨</td><td></td><td></td></tr>
</table>
<table compact>
<tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr>
<tr><td>🟥</td><td>🟥</td><td>🟧</td><td>🟥</td><td>🟧</td><td>🟧</td><td>🟨</td></tr>
</table>
<table compact>
<tr><th>0</th><th>3</th><th>6</th><th>9</th><th>12</th><th>15</th><th>18</th><th>21</th></tr>
<tr><td>·</td><td>🟨</td><td>🟧</td><td>🟧</td><td>🟧</td><td>🟥</td><td>🟥</td><td>🟥</td></tr>
</table>
<p>🎯 Most often — <mark>Tue, 21–22</mark></p>
<table compact>
<tr><td>🏆 record</td><td align="right"><b>23</b> days in a row</td></tr>
<tr><td>🗣 sessions</td><td align="right"><b>412</b> · first 🟦 <b>58%</b></td></tr>
</table>
<footer>counted by <a href="https://telesec.sergeybaus.com">Telesec</a></footer>
""".strip("\n")
CARD_ALL_EN_TEXT = """
📊 Sam × Denis
all time · 21 Oct 2025 – 19 Oct 2026 · 💬 12,325 · 312 days in touch
🟦 Denis: 6,948 (56%) · ⚡ 9 min
🟩 Sam: 5,377 (44%) · ⚡ 24 min
🎯 Most often — Tue, 21–22
counted by Telesec · telesec.sergeybaus.com
""".strip("\n")
PANEL_EMPTY_EN = """
<h3>📊 Денис Петров · 7 days</h3>
<p>no messages in this period</p>
<footer>history since 21 Oct 2025 · text only</footer>
""".strip("\n")
PANEL_EMPTY_EN_TEXT = """
📊 Денис Петров · 7 days
no messages in this period
history since 21 Oct 2025 · text only
""".strip("\n")
CARD_EMPTY_RU = """
<h3>📊 Сергей × Денис</h3>
<p>7 дней · нет сообщений за период</p>
<footer>посчитал <a href="https://telesec.sergeybaus.com">Telesec</a></footer>
""".strip("\n")


def test_panel_golden(cs, render):
    view = _panel(cs, render, "month", "30d", "ru")
    assert view.html == PANEL_MONTH_RU
    assert view.text == PANEL_MONTH_RU_TEXT
    _check(view)


def test_card_golden(cs, render):
    view = _card(cs, render, "all", "all", "en")
    assert view.html == CARD_ALL_EN
    assert view.text == CARD_ALL_EN_TEXT
    _check(view)


def test_empty_period(cs, render):
    view = _panel(cs, render, "empty", "7d", "en")
    assert (view.html, view.text) == (PANEL_EMPTY_EN, PANEL_EMPTY_EN_TEXT)
    _check(view)
    card = _card(cs, render, "empty", "7d", "ru")
    assert card.html == CARD_EMPTY_RU
    _check(card)


def test_language_follows_the_owner_client(cs, render):
    for code in ("ru", "ru-RU", "RU"):
        assert _panel(cs, render, "month", "30d", code).html == PANEL_MONTH_RU
    for code in (None, "", "en", "de", "uk"):
        assert _panel(cs, render, "month", "30d", code).html.startswith("<h3>📊 Денис Петров · 30 days</h3>\n")


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_paused_note_is_the_last_paragraph(cs, render, lang: str):
    note = "History is paused for this chat." if lang == "en" else "История этого чата сейчас не сохраняется."
    for case, period in (("month", "30d"), ("empty", "7d")):
        plain = _panel(cs, render, case, period, lang)
        noted = _panel(cs, render, case, period, lang, note=note)
        head, footer = plain.html.rsplit("\n", 1)
        assert noted.html == f"{head}\n<p>{note}</p>\n{footer}"
        head, footer = plain.text.rsplit("\n", 1)
        assert noted.text == f"{head}\n{note}\n{footer}"
        _check(noted)


def test_waiting_names_the_side(cs, render):
    contact = _panel(cs, render, "month", "30d", "en")
    assert "<blockquote>⏳ Denis is waiting for your reply <b>2 h 10 min</b></blockquote>" in contact.html
    assert "\n⏳ Denis is waiting for your reply 2 h 10 min\n" in contact.text
    owner = _panel(cs, render, "week", "7d", "ru")
    assert "<blockquote>⏳ Вы ждёте ответа <b>13 ч 52 мин</b></blockquote>" in owner.html
    assert "\n⏳ Вы ждёте ответа 13 ч 52 мин\n" in owner.text
    # No clock time: Telegram may show the message in another time zone.
    assert not re.search(r"\d\d:\d\d", contact.html + owner.html + contact.text + owner.text)
    assert "blockquote" not in _panel(cs, render, "all", "all", "en").html


def test_panel_omits_rows_without_data(cs, render):
    week = _panel(cs, render, "week", "7d", "en").html
    assert "edits" not in week and "deleted" not in week  # none in the week, so no rows
    bare = _panel(
        cs, render, "month", "30d", "en",
        waiting=None, previous=None, silence=None, share=cs.Share(),
        deletions=cs.Deletions(total=2, likely_correction=2), edited=cs.SideCounts(unknown=3),
    )
    _check(bare)
    html = bare.html
    assert "blockquote" not in html and "📈" not in html and "pause" not in html and "edits" not in html
    assert '<td>🗑 deleted</td><td align="right"><b>2</b></td><td></td><td></td></tr>' in html and "❓" not in html
    assert "🟦🟦" not in html and "%" not in html.split("<h4>")[0].split("</h3>")[1].split("</p>", 1)[1]
    # A missing name falls back to the stats' own name, clipped for the column.
    assert '<th align="right">🟦 Денис Петров</th>' in _panel(cs, render, "month", "30d", "ru", contact_name=None).html


def test_card_never_shows_owner_only_details(cs, render):
    stats, window = _stats_for(cs, "month", "30d")
    view = render.card(stats, window, period="30d", owner_name="Sam", contact_name="Denis")
    stripped = replace(
        stats,
        deletions=cs.Deletions(),
        edited=cs.SideCounts(),
        waiting=None,
        missing=cs.MissingHistory(),
        previous=None,
        replies=replace(
            stats.replies,
            received=replace(stats.replies.received, p90_seconds=None),
            sent=replace(stats.replies.sent, p90_seconds=None),
        ),
        name="Hidden Name",
        display_name="Hidden Name (@hidden)",
    )
    # None of these reach the card, so removing them changes nothing.
    assert render.card(stripped, window, period="30d", owner_name="Sam", contact_name="Denis") == view
    for word in ("p90", "deleted", "edits", "waiting", "history", "You", "previous", "📈", "Hidden"):
        assert word not in view.html + view.text


# --- Heat maps -------------------------------------------------------------------


def _grid_stats(cs, bucket: str, start: date, first: date, last: date, counts: tuple[int, ...]):
    activity = cs.Activity(bucket=bucket, start=start, counts=counts, first_day=first, last_day=last)
    hours = (0,) * 21 + (3, 0, 0)
    return _base(
        cs, messages=cs.SideCounts(received=3), activity=activity, hours=hours, weekdays=(1, 0, 0, 0, 0, 0, 2)
    )


HOURS_TABLE = """<table compact>
<tr><th>0</th><th>3</th><th>6</th><th>9</th><th>12</th><th>15</th><th>18</th><th>21</th></tr>
<tr><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td></tr>
</table>"""
WEEKDAY_TABLE = """<table compact>
<tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr>
<tr><td>🟧</td><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td></tr>
</table>"""
GRIDS = [
    (
        ("day", date(2026, 10, 14), date(2026, 10, 14), date(2026, 10, 19), (0, 1, 2, 3, 0, 6)),
        """<table compact>
<tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr>
<tr><td></td><td></td><td>·</td><td>🟨</td><td>🟨</td><td>🟧</td><td>·</td></tr>
<tr><td>🟥</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>""",
    ),
    (
        ("week", date(2026, 8, 24), date(2026, 8, 26), date(2026, 10, 19), (1, 2, 3, 4, 5, 6, 7, 8, 9)),
        """<table compact>
<tr><td>Aug</td><td>🟨</td><td>🟨</td><td></td><td></td></tr>
<tr><td>Sep</td><td>🟨</td><td>🟧</td><td>🟧</td><td>🟧</td></tr>
<tr><td>Oct</td><td>🟥</td><td>🟥</td><td>🟥</td><td></td></tr>
</table>""",
    ),
    (
        ("month", date(2025, 11, 1), date(2025, 11, 15), date(2026, 2, 10), (3, 0, 1, 2)),
        """<table compact>
<tr><th></th><th>J</th><th>F</th><th>M</th><th>A</th><th>M</th><th>J</th><th>J</th><th>A</th><th>S</th><th>O</th><th>N</th><th>D</th></tr>
<tr><td>2025</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td>🟥</td><td>·</td></tr>
<tr><td>2026</td><td>🟨</td><td>🟧</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>""",
    ),
    (
        ("quarter", date(2024, 10, 1), date(2024, 11, 1), date(2026, 10, 19), (9, 0, 1, 2, 3, 4, 5, 6, 7)),
        """<table compact>
<tr><th></th><th>I</th><th>II</th><th>III</th><th>IV</th></tr>
<tr><td>2024</td><td></td><td></td><td></td><td>🟥</td></tr>
<tr><td>2025</td><td>·</td><td>🟨</td><td>🟨</td><td>🟨</td></tr>
<tr><td>2026</td><td>🟧</td><td>🟧</td><td>🟧</td><td>🟥</td></tr>
</table>""",
    ),
    (
        ("year", date(2015, 1, 1), date(2015, 3, 1), date(2026, 10, 19), (1,) * 11 + (3,)),
        """<table compact>
<tr><th>2015</th><th>2016</th><th>2017</th><th>2018</th><th>2019</th><th>2020</th><th>2021</th><th>2022</th></tr>
<tr><td>🟨</td><td>🟨</td><td>🟨</td><td>🟨</td><td>🟨</td><td>🟨</td><td>🟨</td><td>🟨</td></tr>
</table>
<table compact>
<tr><th>2023</th><th>2024</th><th>2025</th><th>2026</th></tr>
<tr><td>🟨</td><td>🟨</td><td>🟨</td><td>🟥</td></tr>
</table>""",
    ),
]


@pytest.mark.parametrize(("activity", "expected"), GRIDS, ids=[grid[0][0] for grid in GRIDS])
def test_every_activity_bucket_has_its_grid(cs, render, activity, expected: str):
    stats = _grid_stats(cs, *activity)
    window = _window(cs, None)
    when = render._when(stats, render._format(window, "en"))
    # The calendar shows weekdays itself; every other grid gets the weekday row.
    rows = [expected, HOURS_TABLE] if activity[0] == "day" else [expected, WEEKDAY_TABLE, HOURS_TABLE]
    assert when == "\n".join(["<h4>🗓 When</h4>", *rows])
    view = render.panel(stats, window, period="all")
    assert when in view.html
    _check(view)


def test_grids_start_where_history_does(cs, render):
    """Days before history starts are unknown, not days without messages; an imported earlier message still shows."""
    fmt = render._format(_window(cs, None), "en")
    days = _grid_stats(cs, "day", date(2026, 9, 20), date(2026, 9, 20), date(2026, 10, 19), (0,) * 28 + (2, 1))

    def covered(stats, retained):
        return render._covered(replace(stats, missing=cs.MissingHistory(retained_from=retained)), fmt)

    late = covered(days, _utc(2026, 10, 18, 9, 0))
    assert (late.start, late.first_day, late.counts) == (date(2026, 10, 18), date(2026, 10, 18), (2, 1))
    assert render._calendar(late, fmt) == [
        """<table compact>
<tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr>
<tr><td></td><td></td><td></td><td></td><td></td><td></td><td>🟥</td></tr>
<tr><td>🟧</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>"""
    ]
    imported = replace(days, activity=replace(days.activity, counts=(0,) * 10 + (4,) + (0,) * 17 + (2, 1)))
    assert covered(imported, _utc(2026, 10, 18, 9, 0)).start == date(2026, 9, 30)
    # History from before the period, or none known: the whole period.
    assert covered(days, _utc(2026, 9, 1, 9, 0)) == days.activity
    assert covered(days, None) == days.activity
    months = _grid_stats(cs, "month", date(2025, 11, 1), date(2025, 11, 15), date(2026, 2, 10), (0, 0, 1, 2))
    late_months = covered(months, _utc(2026, 1, 5, 9, 0))
    assert (late_months.start, late_months.counts) == (date(2026, 1, 1), (1, 2))
    assert "<td>2025</td>" not in render._when(
        replace(months, missing=cs.MissingHistory(retained_from=_utc(2026, 1, 5, 9, 0))), fmt
    )


def test_heat_scale(render):
    assert [render.heat(value, 9) for value in (0, 1, 3, 4, 6, 7, 9)] == ["·", "🟨", "🟨", "🟧", "🟧", "🟥", "🟥"]
    assert render.heat(1, 1000) == "🟨" and render.heat(5, 0) == "·" and render.heat(-1, 3) == "·"


def test_balance_bar_rounding(cs, render):
    def cells(received: int, sent: int, **sides: bool) -> int:
        return render.balance_cells(received, sent, has_received=sides.get("r", True), has_sent=sides.get("s", True))

    assert [cells(58, 42), cells(50, 50), cells(54, 46), cells(55, 45), cells(45, 55)] == [6, 5, 5, 6, 5]
    # A side with any messages keeps one square.
    assert (cells(3, 97), cells(97, 3), cells(100, 0, s=False), cells(0, 100, r=False)) == (1, 9, 10, 0)
    stats = _base(cs, messages=cs.SideCounts(received=1, sent=199), share=cs.Share(received=1, sent=99))
    assert render._balance(stats) == "1% 🟦🟩🟩🟩🟩🟩🟩🟩🟩🟩 99%"
    assert render._balance(_base(cs, share=cs.Share(received=None, sent=None))) is None


@pytest.mark.parametrize(
    ("days", "of_ru", "touch_ru", "row_ru", "of_en", "touch_en"),
    [
        (1, "из 1 дня", "день общения", "день подряд", "of 1 day", "day in touch"),
        (2, "из 2 дней", "дня общения", "дня подряд", "of 2 days", "days in touch"),
        (5, "из 5 дней", "дней общения", "дней подряд", "of 5 days", "days in touch"),
        (11, "из 11 дней", "дней общения", "дней подряд", "of 11 days", "days in touch"),
        (21, "из 21 дня", "день общения", "день подряд", "of 21 days", "days in touch"),
        (23, "из 23 дней", "дня общения", "дня подряд", "of 23 days", "days in touch"),
        (112, "из 112 дней", "дней общения", "дней подряд", "of 112 days", "days in touch"),
    ],
)
def test_plurals(cs, render, days, of_ru, touch_ru, row_ru, of_en, touch_en):
    window = _window(cs, "30d")
    ru, en = render._format(window, "ru"), render._format(window, "en")
    assert (render._of_days(ru, days), render._days_in_touch(ru, days), render._in_a_row(ru, days)) == (
        of_ru,
        touch_ru,
        row_ru,
    )
    assert (render._of_days(en, days), render._days_in_touch(en, days)) == (of_en, touch_en)
    assert render._in_a_row(en, days) == ("day in a row" if days == 1 else "days in a row")


# --- Names, safety, and size -----------------------------------------------------


def test_hostile_names_are_escaped_and_clipped(cs, render):
    hostile = "<b>Eve & Co</b>'\"\x1b[31m\n\u202e"
    escaped = "&lt;b&gt;Eve &amp; Co&lt;/b&gt;&#x27;&quot;\\x1b[31m\\n\\u202e"
    panel = _panel(cs, render, "month", "30d", "en", name=hostile, display_name=hostile, contact_name=hostile)
    _check(panel)
    assert panel.html.startswith(f"<h3>📊 {escaped} · 30 days</h3>\n")
    # Columns, the waiting line, and the fallback get the name clipped to 16 characters.
    assert '<th align="right">🟦 &lt;b&gt;Eve &amp; Co&lt;/b&gt;…</th>' in panel.html
    assert "⏳ &lt;b&gt;Eve &amp; Co&lt;/b&gt;… is waiting" in panel.html
    assert "\n🟦 <b>Eve & Co</b>…: 745 (58%)" in panel.text
    for text in (panel.html, panel.text):
        assert "\x1b" not in text and "\u202e" not in text
    assert "<b>Eve" not in panel.html

    card = _card(cs, render, "month", "30d", "en", owner="A&B <i>", contact=hostile)
    _check(card)
    # 32 characters for each name in the card's title.
    assert card.html.startswith(f"<h3>📊 A&amp;B &lt;i&gt; × {escaped[:-2]}…</h3>\n")
    # Long names are clipped in the title too, never inside a character a reader sees.
    long = _card(cs, render, "month", "30d", "en", owner="O" * 50, contact="👨\u200d👩\u200d👧" * 50)
    assert long.html.startswith(f"<h3>📊 {'O' * 31}… × {'👨' + chr(0x200D) + '👩' + chr(0x200D) + '👧'}")
    assert long.text.split("\n")[0] == f"📊 {'O' * 31}… × {'👨‍👩‍👧' * 31}…"


def test_right_to_left_names_cannot_flip_a_line(cs, render):
    lrm, name = "\u200e", "محمد علي"
    # _check asserts that every line starts left to right and keeps each name between LRMs.
    panel = _panel(cs, render, "month", "30d", "ru", contact_name=name)
    _check(panel)
    assert f"🟦 {lrm}{name}{lrm}</th>" in panel.html
    card = _card(cs, render, "all", "all", "en", owner="דוד", contact=name)
    _check(card)
    assert card.text.startswith(f"📊 {lrm}דוד{lrm} × {lrm}{name}{lrm}\n")
    # Names without right-to-left letters get no marks.
    assert lrm not in _panel(cs, render, "month", "30d", "ru").html + _card(cs, render, "all", "all", "en").html


WORST_NAMES = ["W" * 100, "\U0001f600" * 100, "&" * 100, "a\x1bb\u202ec" * 30, "たなかたろうさん", "דוד 42"]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_view_stays_valid(cs, render, lang: str):
    for case, period in (("month", "30d"), ("week", "7d"), ("all", "all"), ("partial", "90d"), ("empty", "7d")):
        for name in WORST_NAMES:
            _check(_panel(cs, render, case, period, lang, name=name, display_name=name, contact_name=name, note="x"))
            _check(_card(cs, render, case, period, lang, owner=name, contact=name))


def test_budget_cut_keeps_title_note_and_footer(cs, render, monkeypatch: pytest.MonkeyPatch):
    note = "History is paused for this chat."
    full = _panel(cs, render, "month", "30d", "en", note=note)
    title = full.html.split("\n", 1)[0]
    tail = f"<p>{note}</p>\n<footer>history since 21 Oct 2025 · text only</footer>"
    assert full.html.endswith(tail)
    # A little under the full size: the heat maps go first, the tiles stay.
    monkeypatch.setattr(render, "MAX_HTML_UNITS", len(full.html.encode("utf-16-le")) // 2 - 1)
    cut = _panel(cs, render, "month", "30d", "en", note=note)
    _check(cut, render)
    assert "<h4>" not in cut.html and "🔥 streak" in cut.html and cut.html.endswith(tail)
    # Far too small: only what always stays.
    monkeypatch.setattr(render, "MAX_HTML_UNITS", 10)
    monkeypatch.setattr(render, "MAX_TEXT_UNITS", 10)
    tiny = _panel(cs, render, "month", "30d", "en", note=note)
    assert tiny.html == f"{title}\n{tail}"
    assert tiny.text == f"📊 Денис Петров · 30 days\n{note}\nhistory since 21 Oct 2025 · text only"
    card = _card(cs, render, "all", "all", "en")
    assert card.html == f'<h3>📊 Sam × Denis</h3>\n<footer>counted by <a href="{SITE}">Telesec</a></footer>'


# --- Formatting ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "ru", "en"),
    [
        (None, "–", "–"),
        (40, "40 с", "40 s"),
        (12 * 60 + 59, "12 мин", "12 min"),
        (2 * 3600, "2 ч", "2 h"),
        (3600 + 40 * 60, "1 ч 40 мин", "1 h 40 min"),
        (3 * 86400 + 5 * 3600 + 59 * 60, "3 дн 5 ч", "3 d 5 h"),
        (10 * 86400 + 23 * 3600, "10 дн", "10 d"),
    ],
)
def test_durations(cs, render, seconds: int | None, ru: str, en: str):
    window = _window(cs, "30d")
    for lang, expected in (("ru", ru), ("en", en)):
        assert render._format(window, lang).dur(seconds) == expected


def test_numbers_and_dates(cs, render):
    window = _window(cs, "30d")
    ru, en = render._format(window, "ru"), render._format(window, "en")
    assert [ru.num(n) for n in (999, 1000, 1234567)] == ["999", "1 000", "1 234 567"]
    assert [en.num(n) for n in (999, 1000, 1234567)] == ["999", "1,000", "1,234,567"]
    assert (ru.decimal(2.4), en.decimal(2.4), ru.decimal(None)) == ("2,4", "2.4", "–")
    assert (ru.day(date(2026, 5, 3)), en.day(date(2026, 5, 3))) == ("3 мая", "3 May")
    assert ru.span(date(2026, 9, 20), date(2026, 10, 19)) == "20 сен – 19 окт"
    assert en.span(date(2025, 10, 21), date(2026, 10, 19)) == "21 Oct 2025 – 19 Oct 2026"
    assert [render.period_label(period, "ru") for period in render.PERIODS] == [
        "7 дней",
        "30 дней",
        "90 дней",
        "всё время",
    ]
    assert [render.period_label(period) for period in render.PERIODS] == ["7 days", "30 days", "90 days", "all time"]
    with pytest.raises(ValueError):
        render.period_label("14d")


def test_every_russian_string_has_an_english_one(render):
    assert set(render._STRINGS["ru"]) <= set(render._STRINGS["en"])
    for strings in render._STRINGS.values():
        assert not any(char in value for value in strings.values() for char in "<>&")
