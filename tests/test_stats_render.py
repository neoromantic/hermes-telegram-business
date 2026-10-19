"""stats_render: the owner panel report and the public card, over synthetic stats.

Goldens pin the exact Telegram HTML. Property tests check the limits for every
view: <pre> lines of at most 31 columns, balanced tags with every untrusted
value escaped, right-to-left names that cannot flip a line, and Telegram's 4096
UTF-16 units.
"""

from __future__ import annotations

import argparse
import html
import importlib
import itertools
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
_PRE_RE = re.compile(r"<pre>(.*?)</pre>", flags=re.DOTALL)


@pytest.fixture(autouse=True)
def _no_hermes_timezone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "hermes_time", None)


@pytest.fixture
def cs(plugin):
    return importlib.import_module(f"{plugin.__name__}.correspondence_stats")


@pytest.fixture
def render(plugin, monkeypatch: pytest.MonkeyPatch):
    module = importlib.import_module(f"{plugin.__name__}.stats_render")

    # The renderer clips a too-wide <pre> line and drops trailing blocks past 4096
    # units only as a last resort. Here both must never fire, so the limit checks
    # test the layout itself, not the guards.
    def cut(line: str) -> str:
        assert module.display_width(line) <= module.PRE_WIDTH, line
        return line

    def finish(blocks, tail=()):
        text = "\n".join([*blocks, *tail])
        assert module.utf16_units(text) <= module.MAX_TEXT_UNITS
        return text

    monkeypatch.setattr(module, "_cut", cut)
    monkeypatch.setattr(module, "_finish", finish)
    return module


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


# --- Goldens ---------------------------------------------------------------------
# Russian names for a Russian owner, English ones otherwise; Berlin time, clock at 13:32.

PANEL_MONTH_RU = """
<b>📊 Денис Петров · 30 дн.</b>
<i>20.09–19.10 · Europe/Berlin</i>

<b>💬 1 284</b> · активных дней 25/30
<pre>Денис █████████░░░░░░░ 58%
Вы    ███████░░░░░░░░░ 42%

         Денис     Вы
сообщ.     745    539
ответ      12м    38м
p90      1ч40м   3ч5м
подряд     2.1    1.4
длина       42     18
правки       7      5</pre>
⏳ без ответа: Денис, с 11:22 (2ч10м)
<b>🕘 пик</b> вт 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
пн вт ср чт пт сб вс
▂·▄▆▄▇▇▅▃▆▇▅▄▆█▃····▄▅▆▇▅▃▆▇█▃
по дням 20.09 → 19.10

серия    10 дн. · макс 14
пауза    4д2ч · 05.10–10.10
беседы   41 · старт Денис 61%
удалено  3 · неясн. 1</pre>
<b>📈 к пред. 30 дн.:</b> сообщ. +18% · ответ Денис 21м→12м · ответ Вы 40м→38м
<i>история с 21.10.2025 · только текст · 13:32</i>
""".strip("\n")

PANEL_MONTH_EN = """
<b>📊 Денис Петров · 30 d</b>
<i>20 Sep–19 Oct · Europe/Berlin</i>

<b>💬 1,284</b> · active days 25/30
<pre>Denis █████████░░░░░░░ 58%
You   ███████░░░░░░░░░ 42%

           Denis    You
msgs         745    539
reply        12m    38m
p90        1h40m   3h5m
per turn     2.1    1.4
length        42     18
edits          7      5</pre>
⏳ no reply: Denis, since 11:22 (2h10m)
<b>🕘 peak</b> Tue 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
Mo Tu We Th Fr Sa Su
▂·▄▆▄▇▇▅▃▆▇▅▄▆█▃····▄▅▆▇▅▃▆▇█▃
by day 20 Sep → 19 Oct

streak   10 d · max 14
pause    4d2h · 5 Oct–10 Oct
sessions 41 · Denis starts 61%
deleted  3 · unexplained 1</pre>
<b>📈 vs prev. 30 d:</b> msgs +18% · Denis reply 21m→12m · You reply 40m→38m
<i>history since 21 Oct 2025 · text only · 13:32</i>
""".strip("\n")

PANEL_WEEK_RU = """
<b>📊 Денис Петров · 7 дн.</b>
<i>13.10–19.10 · Europe/Berlin</i>

<b>💬 370</b> · активных дней 7/7
<pre>Денис █████████░░░░░░░ 57%
Вы    ███████░░░░░░░░░ 43%

         Денис     Вы
сообщ.     212    158
ответ       9м    25м
p90      1ч10м  2ч30м
подряд     2.3    1.6
длина       38     21</pre>
⏳ без ответа: Вы, с 18.10 23:40 (13ч52м)
<b>🕘 пик</b> вт 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
пн вт ср чт пт сб вс
█▆▅▇█▇▄
по дням 13.10 → 19.10

серия    7 дн. · макс 7
пауза    9ч40м · 14.10–15.10
беседы   19 · старт Денис 42%</pre>
<b>📈 к пред. 7 дн.:</b> сообщ. 0→370
<i>история с 21.10.2025 · только текст · 13:32</i>
""".strip("\n")

PANEL_WEEK_EN = """
<b>📊 Денис Петров · 7 d</b>
<i>13 Oct–19 Oct · Europe/Berlin</i>

<b>💬 370</b> · active days 7/7
<pre>Denis █████████░░░░░░░ 57%
You   ███████░░░░░░░░░ 43%

           Denis    You
msgs         212    158
reply         9m    25m
p90        1h10m  2h30m
per turn     2.3    1.6
length        38     21</pre>
⏳ no reply: You, since 18 Oct 23:40 (13h52m)
<b>🕘 peak</b> Tue 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
Mo Tu We Th Fr Sa Su
█▆▅▇█▇▄
by day 13 Oct → 19 Oct

streak   7 d · max 7
pause    9h40m · 14 Oct–15 Oct
sessions 19 · Denis starts 42%</pre>
<b>📈 vs prev. 7 d:</b> msgs 0→370
<i>history since 21 Oct 2025 · text only · 13:32</i>
""".strip("\n")

PANEL_ALL_RU = """
<b>📊 Денис Петров · всё время</b>
<i>21.10.2025–19.10.2026 · Europe/Berlin</i>

<b>💬 12 325</b> · активных дней 312/364
<pre>Денис █████████░░░░░░░ 56%
Вы    ███████░░░░░░░░░ 44%

         Денис     Вы
сообщ.   6 948  5 377
ответ       9м    24м
p90         2ч     4ч
подряд     2.1    1.4
длина       42     18
правки     120     85</pre>
<b>🕘 пик</b> вт 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
пн вт ср чт пт сб вс
▂▆▇▅▆▇▇▆▅▅██▂
по месяцам
21.10.2025 → 19.10.2026

серия    3 дн. · макс 23
пауза    12д
         28.12.2025–09.01.2026
беседы   412 · старт Денис 58%
удалено  37 · неясн. 4</pre>
<i>история с 21.10.2025 · только текст · 13:32</i>
""".strip("\n")

PANEL_ALL_EN = """
<b>📊 Денис Петров · all time</b>
<i>21 Oct 2025–19 Oct 2026 · Europe/Berlin</i>

<b>💬 12,325</b> · active days 312/364
<pre>Denis █████████░░░░░░░ 56%
You   ███████░░░░░░░░░ 44%

           Denis    You
msgs       6,948  5,377
reply         9m    24m
p90           2h     4h
per turn     2.1    1.4
length        42     18
edits        120     85</pre>
<b>🕘 peak</b> Tue 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
Mo Tu We Th Fr Sa Su
▂▆▇▅▆▇▇▆▅▅██▂
by month
21 Oct 2025 → 19 Oct 2026

streak   3 d · max 23
pause    12d
         28 Dec 2025–9 Jan 2026
sessions 412 · Denis starts 58%
deleted  37 · unexplained 4</pre>
<i>history since 21 Oct 2025 · text only · 13:32</i>
""".strip("\n")

PANEL_PARTIAL_RU = """
<b>📊 @denis · 90 дн.</b>
<i>22.07–19.10 · Europe/Berlin</i>

<b>💬 5</b> · активных дней 3/90
<pre>Денис ████████████████ 100%
Вы    ░░░░░░░░░░░░░░░░ 0%

         Денис     Вы
сообщ.       5      0
ответ        –      –
p90          –      –
подряд     1.7      –
длина       12      –</pre>
⏳ без ответа: Денис, с 08.10 09:05 (11д)
<b>🕘 пик</b> чт 9–10
<pre>·········█▄·······█·····
0     6     12    18  23
·· ▆▆ ·· ██ ·· ·· ··
пн вт ср чт пт сб вс
······█··▄··█·
по неделям 22.07 → 19.10

серия    0 дн. · макс 1
пауза    23д · 15.09–08.10
беседы   3 · старт Денис 100%</pre>
<i>история с 01.09.2026 (период неполный) · удалений вне истории 2 · правок без оригинала 1 · только текст · 13:32</i>
""".strip("\n")

PANEL_PARTIAL_EN = """
<b>📊 @denis · 90 d</b>
<i>22 Jul–19 Oct · Europe/Berlin</i>

<b>💬 5</b> · active days 3/90
<pre>Denis ████████████████ 100%
You   ░░░░░░░░░░░░░░░░ 0%

           Denis    You
msgs           5      0
reply          –      –
p90            –      –
per turn     1.7      –
length        12      –</pre>
⏳ no reply: Denis, since 8 Oct 09:05 (11d)
<b>🕘 peak</b> Thu 9–10
<pre>·········█▄·······█·····
0     6     12    18  23
·· ▆▆ ·· ██ ·· ·· ··
Mo Tu We Th Fr Sa Su
······█··▄··█·
by week 22 Jul → 19 Oct

streak   0 d · max 1
pause    23d · 15 Sep–8 Oct
sessions 3 · Denis starts 100%</pre>
<i>history since 1 Sep 2026 (period partial) · deletions not in history 2 · edits without original 1 · text only · 13:32</i>
""".strip("\n")

PANEL_EMPTY_WEEK_RU = """
<b>📊 Денис Петров · 7 дн.</b>
<i>13.10–19.10 · Europe/Berlin</i>

нет сообщений за период
""".strip("\n")

PANEL_EMPTY_WEEK_EN = """
<b>📊 Денис Петров · 7 d</b>
<i>13 Oct–19 Oct · Europe/Berlin</i>

no messages in this period
""".strip("\n")

PANEL_EMPTY_ALL_RU = """
<b>📊 Денис Петров · всё время</b>
<i>Europe/Berlin</i>

нет сообщений за период
""".strip("\n")

PANEL_EMPTY_ALL_EN = """
<b>📊 Денис Петров · all time</b>
<i>Europe/Berlin</i>

no messages in this period
""".strip("\n")

CARD_ALL_RU = """
<b>📊 Сергей × Денис · всё время</b>
<i>21.10.2025–19.10.2026</i>

<b>💬 12 325</b> · дней общения 312
<pre>Денис  █████████░░░░░░░ 56%
Сергей ███████░░░░░░░░░ 44%

         Денис Сергей
сообщ.   6 948  5 377
ответ       9м    24м
подряд     2.1    1.4
длина       42     18</pre>
<b>🕘 пик</b> вт 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
пн вт ср чт пт сб вс
▂▆▇▅▆▇▇▆▅▅██▂
по месяцам
21.10.2025 → 19.10.2026

рекорд   23 дн. подряд
беседы   412 · старт Денис 58%</pre>
<i>посчитал <a href="https://telesec.sergeybaus.com">Telesec</a></i>
""".strip("\n")

CARD_ALL_EN = """
<b>📊 Sam × Denis · all time</b>
<i>21 Oct 2025–19 Oct 2026</i>

<b>💬 12,325</b> · days in touch 312
<pre>Denis █████████░░░░░░░ 56%
Sam   ███████░░░░░░░░░ 44%

           Denis    Sam
messages   6,948  5,377
reply         9m    24m
per turn     2.1    1.4
length        42     18</pre>
<b>🕘 peak</b> Tue 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
Mo Tu We Th Fr Sa Su
▂▆▇▅▆▇▇▆▅▅██▂
by month
21 Oct 2025 → 19 Oct 2026

record   23 days in a row
sessions 412 · Denis starts 58%</pre>
<i>counted by <a href="https://telesec.sergeybaus.com">Telesec</a></i>
""".strip("\n")

CARD_MONTH_RU = """
<b>📊 Сергей × Денис · 30 дн.</b>
<i>20.09–19.10</i>

<b>💬 1 284</b> · дней общения 25
<pre>Денис  █████████░░░░░░░ 58%
Сергей ███████░░░░░░░░░ 42%

         Денис Сергей
сообщ.     745    539
ответ      12м    38м
подряд     2.1    1.4
длина       42     18</pre>
<b>🕘 пик</b> вт 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
пн вт ср чт пт сб вс
▂·▄▆▄▇▇▅▃▆▇▅▄▆█▃····▄▅▆▇▅▃▆▇█▃
по дням 20.09 → 19.10

рекорд   14 дн. подряд
беседы   41 · старт Денис 61%</pre>
<i>посчитал <a href="https://telesec.sergeybaus.com">Telesec</a></i>
""".strip("\n")

CARD_MONTH_EN = """
<b>📊 Sam × Denis · 30 d</b>
<i>20 Sep–19 Oct</i>

<b>💬 1,284</b> · days in touch 25
<pre>Denis █████████░░░░░░░ 58%
Sam   ███████░░░░░░░░░ 42%

           Denis    Sam
messages     745    539
reply        12m    38m
per turn     2.1    1.4
length        42     18</pre>
<b>🕘 peak</b> Tue 21–22
<pre>·····▁▂▂▄▅▄▃▃▄▅▅▄▅▆▇▇█▆▂
0     6     12    18  23
▆▆ ██ ▄▄ ▆▆ ▅▅ ▃▃ ▂▂
Mo Tu We Th Fr Sa Su
▂·▄▆▄▇▇▅▃▆▇▅▄▆█▃····▄▅▆▇▅▃▆▇█▃
by day 20 Sep → 19 Oct

record   14 days in a row
sessions 41 · Denis starts 61%</pre>
<i>counted by <a href="https://telesec.sergeybaus.com">Telesec</a></i>
""".strip("\n")

CARD_EMPTY_WEEK_RU = """
<b>📊 Сергей × Денис · 7 дн.</b>
<i>13.10–19.10</i>

нет сообщений за период
<i>посчитал <a href="https://telesec.sergeybaus.com">Telesec</a></i>
""".strip("\n")

CARD_EMPTY_WEEK_EN = """
<b>📊 Sam × Denis · 7 d</b>
<i>13 Oct–19 Oct</i>

no messages in this period
<i>counted by <a href="https://telesec.sergeybaus.com">Telesec</a></i>
""".strip("\n")

NAMES = {"ru": ("Сергей", "Денис"), "en": ("Sam", "Denis")}
BUILDERS = {"month": _month, "week": _week, "all": _all, "partial": _partial, "empty": _empty}
PANELS = [
    ("month", "30d", "ru", PANEL_MONTH_RU),
    ("month", "30d", "en", PANEL_MONTH_EN),
    ("week", "7d", "ru", PANEL_WEEK_RU),
    ("week", "7d", "en", PANEL_WEEK_EN),
    ("all", "all", "ru", PANEL_ALL_RU),
    ("all", "all", "en", PANEL_ALL_EN),
    ("partial", "90d", "ru", PANEL_PARTIAL_RU),
    ("partial", "90d", "en", PANEL_PARTIAL_EN),
    ("empty", "7d", "ru", PANEL_EMPTY_WEEK_RU),
    ("empty", "7d", "en", PANEL_EMPTY_WEEK_EN),
    ("empty", "all", "ru", PANEL_EMPTY_ALL_RU),
    ("empty", "all", "en", PANEL_EMPTY_ALL_EN),
]
CARDS = [
    ("all", "all", "ru", CARD_ALL_RU),
    ("all", "all", "en", CARD_ALL_EN),
    ("month", "30d", "ru", CARD_MONTH_RU),
    ("month", "30d", "en", CARD_MONTH_EN),
    ("empty", "7d", "ru", CARD_EMPTY_WEEK_RU),
    ("empty", "7d", "en", CARD_EMPTY_WEEK_EN),
]
PERIOD_FOR = {"month": "30d", "week": "7d", "all": "all", "partial": "90d", "empty": "7d"}
ALLOWED_TAGS = {"b", "i", "pre", "code", "a"}
_MARKUP_RE = re.compile(r"</?(?:b|i|pre|code)>|<a href=\"[^\"<>&]*\">|</a>|&(?:lt|gt|amp);")


def _stats_for(cs, case: str, period: str):
    window = _window(cs, None if period == "all" else period)
    return BUILDERS[case](cs, window), window


def _panel(cs, render, case: str, period: str, lang: str | None, **changes: Any) -> str:
    stats, window = _stats_for(cs, case, period)
    contact = changes.pop("contact_name", NAMES[render._owner_panel.language(lang)][1])
    return render.panel(replace(stats, **changes), window, period=period, language_code=lang, contact_name=contact)


def _card(
    cs, render, case: str, period: str, lang: str | None, *, owner: str | None = None, contact: str | None = None
):
    stats, window = _stats_for(cs, case, period)
    names = NAMES[render._owner_panel.language(lang)]
    return render.card(
        stats, window, period=period, owner_name=owner or names[0], contact_name=contact or names[1], language_code=lang
    )


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"tag {tag}")
        if (tag == "a") != bool(attrs) or (attrs and attrs != [("href", "https://telesec.sergeybaus.com")]):
            self.errors.append(f"attributes {tag} {attrs}")
        if tag == "pre" and "pre" in self.stack:
            self.errors.append("nested pre")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced {tag}")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.errors.append(f"self-closing {tag}")


def _columns(line: str) -> int:
    """Monospace columns, counted apart from the renderer: wide characters take 2;
    marks, format characters (LRM, ZWJ), and the emoji a ZWJ joins take 0."""
    width, joined = 0, False
    for char in line:
        if not joined and unicodedata.category(char) not in ("Mn", "Me", "Cf"):
            width += 2 if unicodedata.east_asian_width(char) in "WF" else 1
        joined = char == "\u200d"
    return width


def _first_strong(line: str) -> str | None:
    return next((cls for cls in map(unicodedata.bidirectional, line) if cls in ("L", "R", "AL")), None)


def _check_direction(line: str) -> None:
    """Telegram picks a line's direction from its first strong character: it must stay left to right,
    and every right-to-left run must sit between two LRMs."""
    assert _first_strong(line) in (None, "L"), line
    outside = re.sub("\u200e[^\u200e]*\u200e", "", line)
    assert not any(unicodedata.bidirectional(char) in ("R", "AL", "AN") for char in outside), line


def _check(text: str) -> str:
    """Assert Telegram-valid HTML within the limits; returns the visible text."""
    tags = _Tags()
    tags.feed(text)
    tags.close()
    assert tags.errors == [] and tags.stack == [], tags.errors
    # Outside markup and the three entities html.escape writes, no raw "<", ">" or "&" is left.
    assert not re.search(r"[<>&]", _MARKUP_RE.sub("", text))
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    # Telegram gives every <pre> a copy bar: a view has two at most, none empty,
    # and one empty line at most between the parts of a block, never at its ends.
    blocks = _PRE_RE.findall(text)
    assert len(blocks) <= 2, blocks
    for block in blocks:
        assert block.strip("\n") == block != "" and "\n\n\n" not in block, block
    for block in blocks:
        for line in html.unescape(block).split("\n"):
            assert _columns(line) <= 31, line
            assert line == line.rstrip(), line
    visible = html.unescape(re.sub(r"<[^>]+>", "", text))
    for line in visible.split("\n"):
        _check_direction(line)
    return visible


@pytest.mark.parametrize(("case", "period", "lang", "expected"), PANELS)
def test_panel_goldens(cs, render, case: str, period: str, lang: str, expected: str):
    text = _panel(cs, render, case, period, lang)
    assert text == expected
    _check(text)
    assert text.count("<pre>") == (0 if case == "empty" else 2)


@pytest.mark.parametrize(("case", "period", "lang", "expected"), CARDS)
def test_card_goldens(cs, render, case: str, period: str, lang: str, expected: str):
    text = _card(cs, render, case, period, lang)
    assert text == expected
    _check(text)
    assert text.count("<pre>") == (0 if case == "empty" else 2)


def test_language_follows_the_owner_client(cs, render):
    for code in ("ru", "ru-RU", "RU"):
        assert _panel(cs, render, "month", "30d", code) == PANEL_MONTH_RU
    for code in (None, "", "en", "de", "uk"):
        assert _panel(cs, render, "month", "30d", code) == PANEL_MONTH_EN


def test_panel_omits_rows_without_data(cs, render):
    month = _panel(cs, render, "month", "30d", "en")
    assert "edits" in month and "deleted" in month and "no reply" in month and "vs prev." in month
    # The week has no deletions or edits, so those rows are gone, not zero.
    week = _panel(cs, render, "week", "7d", "en")
    assert "edits" not in week and "deleted" not in week
    bare = _panel(
        cs,
        render,
        "month",
        "30d",
        "en",
        waiting=None,
        previous=None,
        silence=None,
        deletions=cs.Deletions(total=2, likely_correction=2),
        edited=cs.SideCounts(unknown=3),
        share=cs.Share(),
    )
    assert "no reply" not in bare and "vs prev." not in bare and "pause" not in bare
    assert "edits" not in bare  # only unknown-direction edits: no side to show them under
    assert "deleted  2</pre>" in bare and "unexplained" not in bare
    assert "█░" not in bare.split("<pre>")[1]  # no share bars without known sides
    _check(bare)  # so the first block starts with the table, not an empty line
    # A missing name falls back to the stats' own name.
    assert "<pre>Денис П… " in _panel(cs, render, "month", "30d", "ru", contact_name=None)


def test_card_never_shows_owner_only_details(cs, render):
    stats, window = _stats_for(cs, "month", "30d")
    card = render.card(stats, window, period="30d", owner_name="Sam", contact_name="Denis")
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
    # Deletions, edits, waiting, missing history, p90, the comparison, and the
    # stats' own name never reach the card, so removing them changes nothing.
    assert render.card(stripped, window, period="30d", owner_name="Sam", contact_name="Denis") == card
    for word in ("p90", "deleted", "unexplained", "edits", "no reply", "history", "You", "vs prev."):
        assert word not in card
    for lang in ("ru", "en"):
        partial = _card(cs, render, "partial", "90d", lang)
        assert "2026" not in partial.split("</i>")[1]  # no "history since"


def test_names_are_escaped_and_clipped(cs, render):
    hostile = "<b>Eve & Co</b>\x1b[31m\n\u202e"
    safe = "<b>Eve & Co</b>\\x1b[31m\\n\\u202e"
    panel = _panel(cs, render, "month", "30d", "en", name=hostile, display_name=hostile, contact_name=hostile)
    visible = _check(panel)
    assert panel.startswith("<b>📊 &lt;b&gt;Eve &amp; Co&lt;/b&gt;\\x1b[31m\\n\\u202e · 30 d</b>\n")
    assert f"📊 {safe} · 30 d" in visible
    assert "\x1b" not in panel and "\u202e" not in panel and "<b>Eve" not in panel
    # In <pre> blocks and the lines around them, names are clipped to 8 columns and padded.
    short = "<b>Eve …"
    assert f"{short} █████████░░░░░░░ 58%\nYou      ███████░░░░░░░░░ 42%" in visible
    assert f"\n          {short}      You\nmsgs           745      539\n" in visible
    assert f"no reply: {short}, since" in visible and f"{short} reply 21m→12m" in visible
    # A row too wide for 31 columns wraps under the values.
    assert f"\nsessions 41\n         {short} starts 61%\n" in visible

    card = _card(cs, render, "month", "30d", "en", owner="A&B <i>", contact=hostile)
    visible = _check(card)
    assert card.startswith("<b>📊 A&amp;B &lt;i&gt; × &lt;b&gt;Eve &amp; Co&lt;/b&gt;\\x1b[31m\\n\\u202e · 30 d</b>")
    assert f"{short} █████████░░░░░░░ 58%\nA&B <i>  ███████░░░░░░░░░ 42%" in visible
    # Long names are clipped in the title too.
    long = _card(cs, render, "month", "30d", "en", owner="O" * 50, contact="\U0001f600" * 50)
    title = _check(long).split("\n")[0]
    assert title == f"📊 {'O' * 31}… × {chr(0x1F600) * 31}… · 30 d"
    # An emoji takes two columns: three fit before the ellipsis, then one space pads to the owner's 8.
    assert f"{chr(0x1F600) * 3}…  ████" in long


WORST_NAMES = [
    "W" * 40,
    "\U0001f600" * 40,
    "&" * 40,
    "a\x1bb\u202ec" * 10,
    "Я",
    "たなかたろうさん",
    "Ден🚀🚀🚀🚀🚀",
    "👨\u200d👩\u200d👧" * 6,
    "e\u0301" * 12,
    "محمد علي",
    "דוד 42",
    "٣٣٣",
]
OWNER_NAMES = [*WORST_NAMES[:3], "たなかたろうさん", "محمد علي"]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_view_stays_within_the_limits(cs, render, lang: str):
    big = cs.SideCounts(received=9_999_999, sent=9_999_998, unknown=1)
    extremes = {
        "messages": big,
        "active_days": cs.ActiveDays(total=99_999),
        "sessions": cs.Sessions(total=999_999, started_by_contact=999_999),
        "deletions": cs.Deletions(total=999_999, unexplained=cs.SideCounts(received=999_999)),
        "missing": cs.MissingHistory(
            retained_from=_utc(1999, 12, 31, 12, 0),
            range_partial=True,
            deletions_not_in_history=999_999,
            edit_only_messages=999_999,
        ),
        "share": cs.Share(received=100, sent=0),
        "turns": cs.Turns(received=1, sent=1, per_turn_received=9_999_999.0, per_turn_sent=0.1),
        "replies": cs.Replies(
            received=cs.ReplyTimes(count=1, median_seconds=10**9, p90_seconds=10**9),
            sent=cs.ReplyTimes(count=1, median_seconds=9 * 86400 + 23 * 3600, p90_seconds=23 * 3600 + 59 * 60),
        ),
        "waiting": cs.Waiting(side="sent", since=_utc(2001, 1, 1), seconds=10**9),
        "text_length": cs.TextLength(received=9_999_999, sent=0),
        "edited": big,
        "streaks": cs.Streaks(current_days=99_999, longest_days=99_999),
        "silence": cs.Silence(seconds=10**9, start=_utc(2001, 12, 28, 22, 0), end=_utc(2031, 12, 30, 22, 0)),
    }
    for (case, period), contact, owner in itertools.product(
        [("month", "30d"), ("week", "7d"), ("all", "all"), ("partial", "90d"), ("empty", "7d")],
        WORST_NAMES,
        OWNER_NAMES,
    ):
        stats, window = _stats_for(cs, case, period)
        for changes in ({}, extremes):
            worst = replace(stats, name=contact * 3, display_name=contact * 3, **changes)
            _check(render.panel(worst, window, period=period, language_code=lang, contact_name=contact))
            _check(
                render.card(worst, window, period=period, owner_name=owner, contact_name=contact, language_code=lang)
            )


def test_display_width_counts_monospace_columns(render):
    assert [render.display_width(text) for text in ("Денис", "たなか", "🚀", "█░▁·–→…", "e\u0301")] == [5, 6, 2, 7, 1]
    assert render.display_width("❤\ufe0f") == 2  # emoji presentation
    assert render.display_width("👍🏻") == 2  # a skin tone joins its emoji
    assert render.display_width("👨\u200d👩\u200d👧") == 2  # one ZWJ family
    assert render.display_width("🇩🇪🇫🇷") == 4  # two flags
    assert render.display_width("\u200eمحمد\u200e") == 4  # the marks take no room
    # A <pre> name is cut to 8 columns, never inside a character a reader sees.
    assert render.pre_name("👨\u200d👩\u200d👧" * 5) == "👨\u200d👩\u200d👧" * 3 + "…"
    assert render.pre_name("e\u0301" * 9) == "e\u0301" * 7 + "…"
    assert render.pre_name("🇩🇪" * 5) == "🇩🇪" * 3 + "…"
    assert render.pre_name("たなかたろうさん") == "たなか…"


def test_wide_names_keep_the_columns_aligned(cs, render):
    for contact, clipped in (("たなかたろうさん", "たなか…"), ("Ден🚀🚀🚀🚀🚀", "Ден🚀🚀…"), ("Маша 🌸", "Маша 🌸")):
        for view in (
            _panel(cs, render, "month", "30d", "en", contact_name=contact),
            _card(cs, render, "month", "30d", "en", owner="Sam", contact=contact),
        ):
            _check(view)
            bars, table = (part.split("\n") for part in html.unescape(_PRE_RE.findall(view)[0]).split("\n\n"))
            assert bars[0].startswith(f"{clipped} ")
            # Both bars start in the same column, and every table line ends in the same one.
            assert len({_columns(re.split("[█░]", line)[0]) for line in bars}) == 1, bars
            assert len({_columns(line) for line in table}) == 1, table


def test_right_to_left_names_cannot_flip_a_line(cs, render):
    lrm, name = "\u200e", "محمد علي"
    # _check asserts that every line starts left to right and keeps each name between LRMs.
    visible = _check(_panel(cs, render, "month", "30d", "ru", contact_name=name))
    assert f"\n{lrm}{name}{lrm} █████████░░░░░░░ 58%\nВы       ███████░░░░░░░░░ 42%" in visible
    assert f"старт {lrm}{name}{lrm} 61%" in visible and f"ответ {lrm}{name}{lrm} 21м→12м" in visible
    visible = _check(_card(cs, render, "all", "all", "en", owner="דוד", contact=name))
    assert visible.startswith(f"📊 {lrm}דוד{lrm} × {lrm}{name}{lrm} · all time\n")
    header = next(line for line in visible.split("\n") if line.endswith(f"{lrm}דוד{lrm}"))
    assert header.lstrip(" ").startswith(f"{lrm}{name}{lrm}")  # the contact's column stays on the left
    # Names without right-to-left letters get no marks.
    assert lrm not in _panel(cs, render, "month", "30d", "ru") + _card(cs, render, "all", "all", "en")


def test_every_activity_bucket_and_year_fits(cs, render):
    window = _window(cs, None)
    stats = _all(cs, window)
    for bucket, first in (
        ("day", date(2026, 9, 19)),
        ("week", date(2026, 1, 1)),
        ("month", date(2023, 12, 31)),
        ("quarter", date(2019, 12, 31)),
        ("year", date(1996, 12, 31)),
    ):
        activity = cs.Activity(
            bucket=bucket, start=first, counts=(1,) * 31, first_day=first, last_day=date(2026, 10, 19)
        )
        for lang in ("ru", "en"):
            visible = _check(render.panel(replace(stats, activity=activity), window, period="all", language_code=lang))
            assert "▁" * 31 not in visible and "█" * 31 in visible


@pytest.mark.parametrize(
    ("seconds", "ru", "en"),
    [
        (None, "–", "–"),
        (0, "0с", "0s"),
        (40, "40с", "40s"),
        (59, "59с", "59s"),
        (60, "1м", "1m"),
        (12 * 60 + 59, "12м", "12m"),
        (3600, "1ч", "1h"),
        (2 * 3600, "2ч", "2h"),
        (3600 + 40 * 60, "1ч40м", "1h40m"),
        (3 * 3600 + 5 * 60, "3ч5м", "3h5m"),
        (86400, "1д", "1d"),
        (3 * 86400, "3д", "3d"),
        (3 * 86400 + 5 * 3600 + 59 * 60, "3д5ч", "3d5h"),
        (9 * 86400 + 23 * 3600, "9д23ч", "9d23h"),
        (10 * 86400 + 23 * 3600, "10д", "10d"),
        (400 * 86400, "400д", "400d"),
    ],
)
def test_compact_durations(cs, render, seconds: int | None, ru: str, en: str):
    window = _window(cs, "30d")
    for lang, expected in (("ru", ru), ("en", en)):
        assert render._format(window, lang).dur(seconds) == expected


def test_numbers_dates_and_glyphs(cs, render):
    window = _window(cs, "30d")
    ru, en = render._format(window, "ru"), render._format(window, "en")
    assert [ru.num(n) for n in (0, 999, 1000, 12325, 1234567)] == ["0", "999", "1 000", "12 325", "1 234 567"]
    assert [en.num(n) for n in (0, 999, 1000, 12325, 1234567)] == ["0", "999", "1,000", "12,325", "1,234,567"]
    # The year shows only outside the current one, and then on both ends of a span.
    assert (ru.day(date(2026, 10, 3)), en.day(date(2026, 10, 3))) == ("03.10", "3 Oct")
    assert (ru.day(date(2025, 10, 21)), en.day(date(2025, 10, 21))) == ("21.10.2025", "21 Oct 2025")
    assert ru.span(date(2026, 9, 20), date(2026, 10, 19)) == "20.09–19.10"
    assert en.span(date(2025, 10, 21), date(2026, 10, 19)) == "21 Oct 2025–19 Oct 2026"
    assert ru.span(date(2026, 10, 3), date(2026, 10, 3)) == "03.10"
    assert render.spark([0, 1, 2, 3, 4, 5, 6, 7, 8]) == "·▁▂▃▄▅▆▇█"
    assert render.spark([0, 1, 100]) == "·▁█"
    assert render.spark([0, 0]) == "··" and render.spark([]) == ""
    assert [render.bar(share) for share in (0, 3, 4, 50, 58, 97, 100)] == [
        "░" * 16,
        "░" * 16,
        "█" + "░" * 15,
        "█" * 8 + "░" * 8,
        "█" * 9 + "░" * 7,
        "█" * 16,
        "█" * 16,
    ]
    assert render.HOURS_AXIS.index("6") == 6 and render.HOURS_AXIS.index("12") == 12
    assert render.HOURS_AXIS.index("18") == 18 and render.HOURS_AXIS.rindex("23") == 22
    assert len(render.HOURS_AXIS) == 24
    with pytest.raises(ValueError):
        render.period_label("14d")
    assert [render.period_label(period, "ru") for period in render.PERIODS] == [
        "7 дн.",
        "30 дн.",
        "90 дн.",
        "всё время",
    ]
    assert [render.period_label(period) for period in render.PERIODS] == ["7 d", "30 d", "90 d", "all time"]


def test_every_russian_string_has_an_english_one(render):
    assert set(render._STRINGS["ru"]) <= set(render._STRINGS["en"])
    for strings in render._STRINGS.values():
        assert not any(char in value for value in strings.values() for char in "<>&")


def test_last_resort_guards_keep_valid_html(plugin):
    render = importlib.import_module(f"{plugin.__name__}.stats_render")  # the real guards
    assert render._cut("x" * 40) == "x" * 30 + "…"
    assert render._cut("x" * 31) == "x" * 31
    blocks = ["<b>head</b>", "<pre>" + "y" * 3000 + "</pre>", "<i>" + "z" * 3000 + "</i>"]
    assert render._finish(blocks) == "\n".join(blocks[:2])
    assert render._finish(["<b>" + "w" * 5000 + "</b>"]) == "<b>" + "w" * 5000 + "</b>"
    # A tail (the panel's paused line) always stays last; the blocks before it go first.
    assert render._finish(blocks, ["paused"]) == "\n".join([*blocks[:2], "paused"])
    assert render._finish(blocks[:2], ["p" * 1100]) == "\n".join([blocks[0], "p" * 1100])
