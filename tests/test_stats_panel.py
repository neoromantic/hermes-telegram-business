from __future__ import annotations

import html
import importlib
import logging
import re
import sys
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import test_history as th
import test_owner_panel as top
import test_transcription_modes as ttm
from test_history import _build_history_parser, snapshot_history_state
from test_stats import (
    NOW,
    README_DOC,
    STATS_DOC,
    _alice_fixture,
    _break_history,
    _contact_json,
    _doc_section,
    _entry,
    _fenced_blocks,
    _hermes_tz,
    _msg,
    _overview_fixture,
    _seed,
    _stats,
    _utc,
)

plugin = th.plugin
enabled_history = th.enabled_history
modes = ttm.modes

ICON = "\U0001f4ca"
# (sub-argument, the CLI's --since on 2026-10-19): whole local days, today included.
PERIODS = [("7d", "2026-10-13"), ("30d", "2026-09-20"), ("90d", "2026-07-22"), ("all", None)]
CHATS = [(991, "business-123"), (992, "business-123"), (994, "business-456"), (995, "business-123")]
SUB_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
TITLE = f"<b>{ICON} Stats</b>"
TITLE_RU = f"<b>{ICON} Статистика</b>"
HISTORY_OFF = f"{TITLE}\nHistory is off. Turn it on in Settings (/telesec)."
HISTORY_OFF_RU = f"{TITLE_RU}\nИстория выключена. Включите её в настройках (/telesec)."
NO_CONNECTION = (
    f"{TITLE}\nThe Business connection for this chat is not known yet. Send any message in the chat and try again."
)
NO_CONNECTION_RU = (
    f"{TITLE_RU}\nBusiness-подключение этого чата пока неизвестно. "
    "Отправьте любое сообщение в этом чате и попробуйте снова."
)
NO_HISTORY = "No history for this chat yet. History records new text messages only."
NO_HISTORY_RU = "Истории этого чата пока нет. История записывает только новые текстовые сообщения."
READ_ERROR = (
    f"{TITLE}\nHistory could not be read. For details, run on the gateway host:\n"
    "<code>hermes telesec stats --chat 991 --connection business-123</code>"
)
READ_ERROR_RU = (
    f"{TITLE_RU}\nИсторию не удалось прочитать. Подробности — на хосте шлюза:\n"
    "<code>hermes telesec stats --chat 991 --connection business-123</code>"
)
_TAG_RE = re.compile(r"</?(?:b|i|pre|code)>|<a href=\"[^\"]*\">|</a>")

# Real views of the Alice fixture with Hermes time zone Europe/Moscow, clock at 2026-10-19 11:32 UTC.
# The week matches test_stats.ALICE_WEEK_MOSCOW number for number.
ALICE_WEEK_RU = """
<b>📊 Alice Example · 7 дн.</b>
<i>13.10–19.10 · Europe/Moscow</i>

<b>💬 3</b> · активных дней 2/7
<pre>Alice ███████████░░░░░ 67%
Вы    █████░░░░░░░░░░░ 33%</pre>
<pre>         Alice     Вы
сообщ.       2      1
ответ    1ч34м      –
p90      1ч34м      –
подряд     2.0    1.0
длина       10     10</pre>
⏳ без ответа: Alice, с 17.10 21:04 (1д17ч)
<b>🕘 пик</b> сб 0–1
<pre>█··················█·█··
0     6     12    18  23
▄▄ ·· ·· ·· ·· ██ ··
пн вт ср чт пт сб вс
····█·▄
по дням 13.10 → 19.10</pre>
<pre>серия    1 дн. · макс 1
пауза    1д2ч · 17.10–19.10
беседы   2 · старт Alice 50%
удалено  1</pre>
<b>📈 к пред. 7 дн.:</b> сообщ. +0% · ответ Alice 10м→1ч34м
<i>история с 03.10.2026 · только текст · 14:32</i>
""".strip("\n")

ALICE_WEEK_EN = """
<b>📊 Alice Example · 7 d</b>
<i>13 Oct–19 Oct · Europe/Moscow</i>

<b>💬 3</b> · active days 2/7
<pre>Alice ███████████░░░░░ 67%
You   █████░░░░░░░░░░░ 33%</pre>
<pre>           Alice    You
msgs           2      1
reply      1h34m      –
p90        1h34m      –
per turn     2.0    1.0
length        10     10</pre>
⏳ no reply: Alice, since 17 Oct 21:04 (1d17h)
<b>🕘 peak</b> Sat 0–1
<pre>█··················█·█··
0     6     12    18  23
▄▄ ·· ·· ·· ·· ██ ··
Mo Tu We Th Fr Sa Su
····█·▄
by day 13 Oct → 19 Oct</pre>
<pre>streak   1 d · max 1
pause    1d2h · 17 Oct–19 Oct
sessions 2 · Alice starts 50%
deleted  1</pre>
<b>📈 vs prev. 7 d:</b> msgs +0% · Alice reply 10m→1h34m
<i>history since 3 Oct 2026 · text only · 14:32</i>
""".strip("\n")

DAVE_MONTH = """
<b>📊 Dave · 30 d</b>
<i>20 Sep–19 Oct · Europe/Moscow</i>

no messages in this period
""".strip("\n")

ALICE_MONTH = """
<b>📊 Alice Example · 30 d</b>
<i>20 Sep–19 Oct · Europe/Moscow</i>

<b>💬 10</b> · active days 5/30
<pre>Alice ███████████░░░░░ 67%
You   █████░░░░░░░░░░░ 33%</pre>
<pre>           Alice    You
msgs           6      3
reply        52m    30m
p90        1h34m    30m
per turn     1.5    1.0
length        10     10
edits          0      1</pre>
⏳ no reply: Alice, since 17 Oct 21:04 (1d17h)
<b>🕘 peak</b> Sat 12–13
<pre>▆··········▆█······▃·▃·▃
0     6     12    18  23
▁▁ ·· ·· ·· ·· ██ ▁▁
Mo Tu We Th Fr Sa Su
·············█▃·····█······▆·▃
by day 20 Sep → 19 Oct</pre>
<pre>streak   1 d · max 2
pause    7d7h · 10 Oct–17 Oct
sessions 5 · Alice starts 60%
deleted  4 · unexplained 1</pre>
<i>history since 3 Oct 2026 (period partial) · deletions not in history 1 · edits without original 1 · text only · 14:32</i>
""".strip("\n")


@pytest.fixture(autouse=True)
def _fixed_clock(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "hermes_time", None)  # no Hermes time zone
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: NOW)


@pytest.fixture
def sp(plugin):
    return importlib.import_module(f"{plugin.__name__}.stats_panel")


@pytest.fixture
def cs(plugin):
    return importlib.import_module(f"{plugin.__name__}.correspondence_stats")


@pytest.fixture
def render(plugin):
    return importlib.import_module(f"{plugin.__name__}.stats_render")


def _view(sp, period: str | None = None, *, chat: Any = 991, conn: str | None = "business-123", **kwargs: Any):
    return sp.build_stats_view(chat_id=chat, business_connection_id=conn, period=period, **kwargs)


def _panel_records(caplog: pytest.LogCaptureFixture, sp) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == sp.__name__]


def _buttons(current: str, language: str = "en") -> tuple[tuple[str, str], ...]:
    labels = {
        "en": {"7d": "7d", "30d": "30d", "90d": "90d", "all": "All"},
        "ru": {"7d": "7 дн.", "30d": "30 дн.", "90d": "90 дн.", "all": "Всё"},
    }[language]
    return tuple((f"• {label}" if sub == current else label, sub) for sub, label in labels.items())


def _visible(text: str) -> str:
    """The text as Telegram shows it: tags dropped, entities decoded."""
    return html.unescape(_TAG_RE.sub("", text))


# --- Windows and body ------------------------------------------------------------


@pytest.mark.parametrize("tz", [None, "Europe/Moscow"])
def test_period_windows_equal_cli_windows(plugin, sp, cs, monkeypatch: pytest.MonkeyPatch, tz: str | None):
    if tz is not None:
        _hermes_tz(monkeypatch, tz)
    parser = _build_history_parser(plugin)
    for sub, since in PERIODS:
        args = parser.parse_args(["stats", *(["--since", since] if since else [])])
        window = sp.period_window(sub, now=NOW)
        assert window == cs.build_window(args, now=NOW)
        assert window.until is None and window.now == NOW and window.session_gap == timedelta(hours=6)
        assert window.tz_label == (tz or "UTC")
    assert sp.period_window("7d", now=NOW).since == datetime(2026, 10, 13, tzinfo=ZoneInfo(tz) if tz else UTC)
    assert sp.period_window("all", now=NOW).since is None
    # Unknown or missing periods mean the default.
    for period in (None, "", "bogus"):
        assert sp.period_window(period, now=NOW) == sp.period_window("30d", now=NOW)


@pytest.mark.parametrize("tz", [None, "Europe/Moscow"])
def test_periods_are_whole_local_days(plugin, sp, cs, monkeypatch: pytest.MonkeyPatch, tz: str | None):
    # 7 x 24 hours back from now would touch 8 dates and count only part of the first one.
    if tz is not None:
        _hermes_tz(monkeypatch, tz)
    zone = ZoneInfo(tz) if tz else UTC
    today = NOW.astimezone(zone).date()
    for sub, days in sp.PERIODS[:3]:
        window = sp.period_window(sub, now=NOW)
        first = today - timedelta(days=days - 1)
        assert window.since == datetime.combine(first, time(), tzinfo=zone)
        # One message at the first instant of every day, and one a minute before the period.
        starts = [datetime.combine(first + timedelta(days=day), time(), tzinfo=zone) for day in range(days)]
        records = [_msg(plugin, mid, at.astimezone(UTC), "inbound") for mid, at in enumerate(starts, 1)]
        records.append(_msg(plugin, days + 1, (window.since - timedelta(minutes=1)).astimezone(UTC), "inbound"))
        stats = cs.compute_stats(_entry(first_seen_at="2026-01-01T00:00:00Z"), records, window)

        assert stats.messages.total == stats.active_days.total == days
        assert (stats.activity.first_day, stats.activity.last_day) == (first, today)
        if stats.activity.bucket == "day":
            assert stats.activity.counts == (1,) * days
        assert stats.streaks.current_days == stats.streaks.longest_days == days
        assert f"active days {days}/{days}" in sp.render_report(stats, window, period=sub).text
        # The previous period is the same number of whole local days right before it.
        previous = stats.previous
        assert previous.since == datetime.combine(first - timedelta(days=days), time(), tzinfo=zone)
        assert previous.until == window.since - timedelta(microseconds=1)
        assert previous.messages.total == 1


@pytest.mark.parametrize("tz", [None, "Europe/Moscow"])
def test_view_renders_the_cli_stats_for_every_period(
    enabled_history, sp, cs, render, monkeypatch: pytest.MonkeyPatch, tz: str | None
):
    _overview_fixture(enabled_history)
    if tz is not None:
        _hermes_tz(monkeypatch, tz)
    [entry] = enabled_history._history_support._filter_catalog_entries(
        business_connection_id="business-123", chat_id="991"
    )
    for sub, since in PERIODS:
        document = _contact_json(
            enabled_history, "--chat", "991", "--connection", "business-123", *(["--since", since] if since else [])
        )
        window = sp.period_window(sub, now=NOW)
        before = snapshot_history_state(enabled_history)

        view = _view(sp, sub)

        # The same stats as the CLI, rendered by stats_render with the contact's first name.
        stats = cs.collect_contact_stats(entry, window)
        assert view.text == render.panel(stats, window, period=sub, contact_name="Alice")
        assert f"<b>{chr(0x1F4AC)} {document['messages']['total']}</b>" in view.text
        assert f"active days {document['active_days']['total']}/" in view.text
        assert f"sessions {document['sessions']['total']} · Alice starts" in _visible(view.text)
        assert f"Alice {document['share']['received']}%" not in view.text  # bars, not a sentence
        # Read-only: the panel wrote nothing.
        assert snapshot_history_state(enabled_history) == before
        assert "connection=" not in view.text and "deletion details" not in view.text


@pytest.mark.parametrize(
    ("language", "expected"), [("ru", ALICE_WEEK_RU), ("en", ALICE_WEEK_EN), (None, ALICE_WEEK_EN)]
)
def test_real_week_view(enabled_history, sp, monkeypatch: pytest.MonkeyPatch, language, expected: str):
    _alice_fixture(enabled_history)
    _hermes_tz(monkeypatch, "Europe/Moscow")
    view = _view(sp, "7d", language_code=language)
    assert view.text == expected
    assert view.buttons == _buttons("7d", "ru" if language == "ru" else "en")


def test_time_zone_comes_from_hermes(enabled_history, sp, monkeypatch: pytest.MonkeyPatch):
    _alice_fixture(enabled_history)
    # The removed TELESEC_TIMEZONE is ignored.
    monkeypatch.setenv("TELESEC_TIMEZONE", "Europe/Moscow")
    utc = _view(sp, "7d").text.splitlines()
    assert utc[1] == "<i>13 Oct–19 Oct · UTC</i>" and utc[-1].endswith(" · 11:32</i>")
    _hermes_tz(monkeypatch, "Europe/Moscow")
    moscow = _view(sp, "7d").text.splitlines()
    assert moscow[1] == "<i>13 Oct–19 Oct · Europe/Moscow</i>" and moscow[-1].endswith(" · 14:32</i>")
    assert _view(sp, "all").text.splitlines()[1] == "<i>3 Oct–19 Oct · Europe/Moscow</i>"
    # A bad time zone is not a panel state: a bad Hermes value means UTC, and the old variable is not read.
    monkeypatch.setenv("TELESEC_TIMEZONE", "Mars/Olympus")
    for broken in ("Mars/Olympus", RuntimeError("config.yaml is broken")):
        _hermes_tz(monkeypatch, broken)
        for language, dates in ((None, "13 Oct–19 Oct"), ("ru", "13.10–19.10")):
            view = _view(sp, "7d", language_code=language)
            assert view.text.splitlines()[1] == f"<i>{dates} · UTC</i>"
            assert view.buttons == _buttons("7d", language or "en")
    assert not any(key.startswith("stats.bad_timezone") for strings in sp.STRINGS.values() for key in strings)


# --- Buttons ---------------------------------------------------------------------


def test_period_buttons(enabled_history, sp):
    _alice_fixture(enabled_history)
    assert [sub for sub, _ in sp.PERIODS] == ["7d", "30d", "90d", "all"]
    assert sp.SECTION_KEY == "stats" and sp.SECTION_LABEL == f"{ICON} Stats" and sp.DEFAULT_PERIOD == "30d"

    titles = {"7d": "7 d", "30d": "30 d", "90d": "90 d", "all": "all time"}
    for sub, _ in PERIODS:
        view = _view(sp, sub)
        assert view.buttons == _buttons(sub) == sp.period_buttons(sub)
        assert view.text.splitlines()[0] == f"<b>{ICON} Alice Example · {titles[sub]}</b>"
        assert _view(sp, sub, language_code="ru-RU").buttons == _buttons(sub, "ru") == sp.period_buttons(sub, "ru")
    assert _buttons("30d") == (("7d", "7d"), ("• 30d", "30d"), ("90d", "90d"), ("All", "all"))
    assert _buttons("30d", "ru") == (("7 дн.", "7d"), ("• 30 дн.", "30d"), ("90 дн.", "90d"), ("Всё", "all"))
    for period in (None, "", "bogus", "30D", "all/x"):
        view = _view(sp, period)
        assert view.buttons == _buttons("30d")
        assert view.text.splitlines()[0].endswith("· 30 d</b>")

    # Every period's callback data fits Telegram's 64 bytes, even for the longest chat id.
    panel = sp._owner_panel
    for _, sub in _buttons("all"):
        assert SUB_RE.fullmatch(sub)
        data = panel.section_callback(-(10**19), sp.SECTION_KEY, sub)
        assert len(data.encode()) <= 64 and panel.parse_callback(data).sub == sub


# --- States ----------------------------------------------------------------------


@pytest.mark.parametrize(("language", "expected"), [(None, HISTORY_OFF), ("ru", HISTORY_OFF_RU)])
def test_history_off(plugin, sp, language, expected: str):
    _alice_fixture(plugin)
    view = _view(sp, language_code=language)
    assert view.text == expected
    assert view.buttons == ()


def test_state_order(plugin, sp):
    # History off wins over a missing connection.
    assert _view(sp, conn=None).text == HISTORY_OFF
    th.set_switch(plugin, "history", True)
    view = _view(sp, conn=None)
    assert view.text == NO_CONNECTION and view.buttons == ()


@pytest.mark.parametrize("conn", [None, ""])
def test_connection_missing(enabled_history, sp, conn: str | None):
    _alice_fixture(enabled_history)
    view = _view(sp, conn=conn)
    assert view.text == NO_CONNECTION
    assert view.buttons == ()
    assert _view(sp, conn=conn, language_code="ru").text == NO_CONNECTION_RU


def test_no_history_for_chat(enabled_history, sp):
    # No history at all yet, and the panel creates none.
    assert snapshot_history_state(enabled_history) == {}
    assert _view(sp, chat=12345).text == f"<b>{ICON} chat 12345</b>\n{NO_HISTORY}"
    assert _view(sp, chat=12345, language_code="ru").text == f"<b>{ICON} чат 12345</b>\n{NO_HISTORY_RU}"
    assert snapshot_history_state(enabled_history) == {}
    _alice_fixture(enabled_history)
    before = snapshot_history_state(enabled_history)
    view = _view(sp, chat=12345, chat_label="Zoë")
    assert view.text == f"<b>{ICON} Zoë</b>\n{NO_HISTORY}"
    assert view.buttons == ()
    assert snapshot_history_state(enabled_history) == before
    # The label is untrusted: escaped like contact names, HTML-escaped, and clipped.
    view = _view(sp, chat=12345, chat_label="<Eve>&\nX\u202e" + "y" * 200)
    first = view.text.splitlines()[0]
    assert first.startswith(f"<b>{ICON} &lt;Eve&gt;&amp;\\nX\\u202e") and first.endswith("…</b>")
    assert len(html.unescape(first)) == len(f"<b>{ICON} </b>") + 64
    assert "\u202e" not in view.text


def test_same_chat_under_another_connection_is_not_used(enabled_history, sp):
    _overview_fixture(enabled_history)
    # Carol (chat 994) exists only under business-456.
    view = _view(sp, chat=994, conn="business-123")
    assert view.text == f"<b>{ICON} chat 994</b>\n{NO_HISTORY}"
    assert "Carol" not in view.text
    assert _view(sp, chat=994, conn="business-456").text.startswith(f"<b>{ICON} Carol · 30 d</b>\n")


def test_no_activity_in_period_shows_one_line(enabled_history, sp, monkeypatch: pytest.MonkeyPatch):
    _overview_fixture(enabled_history)
    _hermes_tz(monkeypatch, "Europe/Moscow")
    # Dave (chat 995) wrote only on 2026-09-10.
    before = snapshot_history_state(enabled_history)
    view = _view(sp, "30d", chat=995)
    assert view.text == DAVE_MONTH
    assert view.buttons == _buttons("30d")
    assert _view(sp, "30d", chat=995, language_code="ru").text.splitlines()[-1] == "нет сообщений за период"
    assert f"<b>{chr(0x1F4AC)} 1</b>" in _view(sp, "all", chat=995).text
    # Read-only: the panel wrote nothing.
    assert snapshot_history_state(enabled_history) == before


@pytest.mark.parametrize("case", ["stale", "dirty", "chat_lock", "all_locks", "torn_tail"])
def test_read_error_shows_cli_command(enabled_history, sp, caplog: pytest.LogCaptureFixture, case: str):
    _alice_fixture(enabled_history)
    cli_error = _break_history(enabled_history, case)
    before = snapshot_history_state(enabled_history)
    caplog.set_level(logging.WARNING)

    view = _view(sp, "7d")

    assert view.text == READ_ERROR
    assert view.buttons == _buttons("7d")
    assert _view(sp, "7d", language_code="ru").text == READ_ERROR_RU
    record = _panel_records(caplog, sp)[0]
    assert record.levelno == logging.WARNING
    assert re.fullmatch(r"telesec stats panel: history could not be read \((\w+)\)", record.getMessage())
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("Alice", "alice", "business-123", "991", str(enabled_history._history_support.history_root())):
        assert secret not in logged
    assert "Alice" not in caplog.text
    assert cli_error.removeprefix("error: ")[:20] not in logged
    # Read-only: nothing was repaired or rebuilt.
    assert snapshot_history_state(enabled_history) == before


@pytest.mark.parametrize("error", [OSError, ValueError, OverflowError])
def test_read_error_logs_only_exception_type(
    enabled_history, sp, cs, monkeypatch, caplog: pytest.LogCaptureFixture, error: type[Exception]
):
    _alice_fixture(enabled_history)
    caplog.set_level(logging.WARNING)
    message = "cannot read /home/owner/history/Alice Example SECRET-101"

    def explode(*_args: Any, **_kwargs: Any):
        raise error(message)

    monkeypatch.setattr(cs, "collect_contact_stats", explode)
    view = _view(sp)

    assert view.text == READ_ERROR
    assert view.buttons == _buttons("30d")
    assert [record.getMessage() for record in _panel_records(caplog, sp)] == [
        f"telesec stats panel: history could not be read ({error.__name__})"
    ]
    for secret in (message, "Alice", "SECRET", "/home/owner"):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    ("conn", "expected"),
    [
        ("-Ab C", "<code>hermes telesec stats --chat=-100123 --connection='-Ab C'</code>"),
        ("<b>&x", "<code>hermes telesec stats --chat=-100123 --connection '&lt;b&gt;&amp;x'</code>"),
    ],
)
def test_read_error_command_quotes_values(
    enabled_history, sp, cs, monkeypatch, caplog: pytest.LogCaptureFixture, conn: str, expected: str
):
    entries = [_entry(conn=conn), _entry(conn=conn)]
    # Two catalog entries for one (connection, chat) mean the catalog is damaged.
    monkeypatch.setattr(enabled_history._history_support, "_filter_catalog_entries", lambda **_kwargs: entries)
    caplog.set_level(logging.WARNING)

    view = _view(sp, chat=-100123, conn=conn)

    assert view.text.splitlines()[-1] == expected
    assert html.unescape(expected).endswith(cs._cli_option("--connection", conn) + "</code>")
    assert [record.getMessage() for record in _panel_records(caplog, sp)] == [
        "telesec stats panel: history could not be read (ValueError)"
    ]


# --- Names, size, and privacy -----------------------------------------------------


def test_contact_name_is_escaped_like_the_cli(enabled_history, sp, cs, monkeypatch: pytest.MonkeyPatch):
    profile = {"id": 993, "type": "private", "first_name": "Eve<b>&\x1b[31m\nEvil\u202eman\u2066", "username": "eve"}
    _seed(enabled_history, _msg(enabled_history, 1, _utc(2026, 10, 18, 9, 0), "inbound", chat=993, profile=profile))
    window = sp.period_window("30d", now=NOW)
    stats = cs.compute_stats(_entry(chat=993, profile=profile), [], window)

    text = _view(sp, chat=993).text
    header = text.splitlines()[0]

    # The CLI's escaping first, then HTML escaping, in the title and in every <pre> block.
    assert header == f"<b>{ICON} {html.escape(cs._safe(stats.name), quote=False)} · 30 d</b>"
    assert header == f"<b>{ICON} Eve&lt;b&gt;&amp;\\x1b[31m\\nEvil\\u202eman\\u2066 · 30 d</b>"
    assert "<pre>Eve&lt;b&gt;&amp;… ████████████████ 100%\n" in text
    assert "\x1b" not in text and "\u202e" not in text and "\u2066" not in text and "<b>&" not in text
    # The panel shows the same name as the CLI.
    exit_code, output = _stats(enabled_history, "--chat", "993")
    assert exit_code == 0 and output.splitlines()[1].startswith(f"{cs._safe(stats.display_name)} connection=")


@pytest.mark.parametrize(
    ("first_name", "title", "column"),
    [
        ("A" * 64, "A" * 64, "A" * 7 + "…"),
        ("A" * 65, "A" * 63 + "…", "A" * 7 + "…"),
        ("Анна", "Анна", "Анна"),
        ("\x01" * 60, "\\x01" * 15 + "\\x0…", "\\x01\\x0…"),
        # An emoji takes two columns, so three fit before the ellipsis.
        ("\U0001f600" * 150, "\U0001f600" * 63 + "…", "\U0001f600" * 3 + "…"),
    ],
)
def test_contact_name_is_clipped(plugin, sp, cs, first_name: str, title: str, column: str):
    window = sp.period_window("7d", now=NOW)
    profile = {"id": 991, "type": "private", "first_name": first_name}
    records = [_msg(plugin, 1, NOW - timedelta(hours=1), "inbound", profile=profile)]
    stats = cs.compute_stats(_entry(profile=profile), records, window)
    lines = _visible(sp.render_report(stats, window, period="7d", contact_name=first_name).text).splitlines()
    # 64 characters in the title, 8 columns in the <pre> blocks.
    assert lines[0] == f"{ICON} {title} · 7 d"
    assert lines[4].startswith(f"{column} ████")


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_text_fits_telegram_limit(sp, cs):
    window = sp.period_window("all", now=NOW)
    huge = {"id": 991, "type": "private", "first_name": "&\U0001f600\u202e" * 5000, "username": "\U0001f600" * 5000}
    stats = cs.compute_stats(_entry(profile=huge), [], window)
    for language in (None, "ru"):
        worst = sp.render_report(stats, window, period="all", language_code=language).text
        assert _utf16_units(worst) <= 4096
        worst.encode("utf-8")  # no split surrogate pair


def test_message_text_never_appears(enabled_history, sp, monkeypatch: pytest.MonkeyPatch, caplog):
    _overview_fixture(enabled_history)
    caplog.set_level(logging.DEBUG)
    views = []
    for tz in (None, "Europe/Moscow"):
        _hermes_tz(monkeypatch, tz)
        for sub, _ in PERIODS:
            for chat, conn in CHATS:
                for language in (None, "ru"):
                    views.append(_view(sp, sub, chat=chat, conn=conn, language_code=language))
    _break_history(enabled_history, "torn_tail")
    views.append(_view(sp))
    assert views[-1].text == READ_ERROR
    for view in views:
        assert "SECRET" not in view.text
        assert all("SECRET" not in label for label, _ in view.buttons)
    assert "SECRET" not in caplog.text


# --- Owner panel -----------------------------------------------------------------


def test_register_adds_the_stats_section(plugin):
    ctx = SimpleNamespace(
        llm=None,
        register_telegram_handler=lambda handler: None,
        register_hook=lambda name, callback: None,
        get_config={}.get,
        register_cli_command=lambda **kwargs: None,
        register_skill=lambda name, path, description="": None,
    )
    panel = plugin._owner_panel
    assert panel.get_section("stats") is None

    plugin.register(ctx)

    section = panel.get_section("stats")
    assert (section.label, section.order) == (f"{ICON} Stats", 20)
    assert section.render is plugin._stats_panel.render_section
    assert panel.section_label(section, "ru") == f"{ICON} Статистика"
    assert panel.section_label(section, "en") == f"{ICON} Stats"


def _edit_call(bot) -> dict[str, Any]:
    call = bot.text_edit_calls[-1]
    assert (call["chat_id"], call["message_id"]) == (top.OWNER, 500)
    return call


@pytest.mark.asyncio
async def test_panel_opens_stats_and_switches_periods(enabled_history, sp, modes):
    plugin = enabled_history
    _alice_fixture(plugin)
    top.bind_owner(plugin)
    sp.register_section()
    bot = top.PanelBot(business_owner_id=top.OWNER)

    assert await top.dispatch(plugin, top.start_update(), bot) is True
    assert (f"{ICON} Stats", "tb:s:991:stats") in [row[0] for row in top.keyboard(bot.calls[0]["reply_markup"])]
    # The main panel stays plain text.
    assert "parse_mode" not in bot.calls[0] and "link_preview_options" not in bot.calls[0]

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats"), bot) is True
    text, rows = top.last_edit(bot)
    assert text == _view(sp).text
    assert text.startswith(f"<b>{ICON} Alice Example · 30 d</b>\n")
    assert rows == [
        [
            ("7d", "tb:s:991:stats/7d"),
            ("• 30d", "tb:s:991:stats/30d"),
            ("90d", "tb:s:991:stats/90d"),
            ("All", "tb:s:991:stats/all"),
        ],
        [("« Back", "tb:b:991")],
    ]
    call = _edit_call(bot)
    assert call["parse_mode"] == "HTML"
    assert call["link_preview_options"].is_disabled is True

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats/all"), bot) is True
    text, rows = top.last_edit(bot)
    assert text == _view(sp, "all").text
    assert rows[0][3] == ("• All", "tb:s:991:stats/all")
    assert _edit_call(bot)["parse_mode"] == "HTML"

    # Back to the main panel: plain text again.
    assert await top.dispatch(plugin, top.callback_update("tb:b:991"), bot) is True
    assert "parse_mode" not in _edit_call(bot) and "link_preview_options" not in _edit_call(bot)
    # Everything went to the owner's private chat; nothing reached the Business chat.
    assert {call["chat_id"] for call in [*bot.calls, *bot.text_edit_calls]} == {top.OWNER}


@pytest.mark.asyncio
async def test_panel_stats_speak_russian_to_a_russian_client(enabled_history, sp, modes):
    plugin = enabled_history
    _alice_fixture(plugin)
    top.bind_owner(plugin)
    sp.register_section()
    bot = top.PanelBot(business_owner_id=top.OWNER)

    assert await top.dispatch(plugin, top.start_update(language_code="ru"), bot) is True
    assert (f"{ICON} Статистика", "tb:s:991:stats") in [row[0] for row in top.keyboard(bot.calls[0]["reply_markup"])]

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats/7d", language_code="ru-RU"), bot) is True
    assert bot.answers[-1] == {"callback_query_id": "query-1"}  # a period tap inside the section: no toast
    text, rows = top.last_edit(bot)
    assert text == _view(sp, "7d", language_code="ru").text
    assert text.startswith(f"<b>{ICON} Alice Example · 7 дн.</b>\n")
    assert rows == [
        [
            ("• 7 дн.", "tb:s:991:stats/7d"),
            ("30 дн.", "tb:s:991:stats/30d"),
            ("90 дн.", "tb:s:991:stats/90d"),
            ("Всё", "tb:s:991:stats/all"),
        ],
        [("« Назад", "tb:b:991")],
    ]


@pytest.mark.asyncio
async def test_panel_state_without_buttons_keeps_only_back(plugin, sp, modes):
    top.bind_owner(plugin)
    sp.register_section()
    bot = top.PanelBot(business_owner_id=top.OWNER)

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats"), bot) is True
    assert top.last_edit(bot) == (HISTORY_OFF, [[("« Back", "tb:b:991")]])
    assert _edit_call(bot)["parse_mode"] == "HTML"

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats", language_code="ru"), bot) is True
    assert top.last_edit(bot) == (HISTORY_OFF_RU, [[("« Назад", "tb:b:991")]])


@pytest.mark.asyncio
async def test_rejected_html_falls_back_to_plain_unavailable(enabled_history, sp, modes):
    plugin = enabled_history
    _alice_fixture(plugin)
    top.bind_owner(plugin)
    sp.register_section()
    bot = top.PanelBot(business_owner_id=top.OWNER)
    original = bot.edit_message_text

    async def edit(**kwargs):
        if "parse_mode" in kwargs:
            bot.text_edit_calls.append(kwargs)
            raise ttm.BadRequest("Can't parse entities: unsupported start tag")
        return await original(**kwargs)

    bot.edit_message_text = edit

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats"), bot) is True

    first, fallback = bot.text_edit_calls
    assert first["parse_mode"] == "HTML"
    assert fallback["text"] == "Alice Example (@alice) · chat 991\nSection unavailable."
    assert "parse_mode" not in fallback and "link_preview_options" not in fallback


# --- History paused for this chat (Don't save) -----------------------------------

PAUSED = "History is paused for this chat."
PAUSED_RU = "История этого чата сейчас не сохраняется."


def _pause(plugin, chat: int = 991) -> None:
    plugin._chat_settings.default_store().set_flag(top.OWNER, chat, "history", False, via="cli")


async def _open_stats(plugin, bot, data: str, language: str | None) -> tuple[str, list]:
    assert await top.dispatch(plugin, top.callback_update(data, language_code=language), bot) is True
    assert _edit_call(bot)["parse_mode"] == "HTML"
    return top.last_edit(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "report", "note", "back"),
    [(None, ALICE_WEEK_EN, PAUSED, "« Back"), ("ru", ALICE_WEEK_RU, PAUSED_RU, "« Назад")],
)
async def test_paused_chat_shows_the_saved_report_then_the_note(
    enabled_history, sp, modes, monkeypatch: pytest.MonkeyPatch, language, report: str, note: str, back: str
):
    plugin = enabled_history
    _alice_fixture(plugin)
    _hermes_tz(monkeypatch, "Europe/Moscow")
    top.bind_owner(plugin)
    sp.register_section()
    bot = top.PanelBot(business_owner_id=top.OWNER)

    text, _ = await _open_stats(plugin, bot, "tb:s:991:stats/7d", language)
    assert text == report and note not in text

    _pause(plugin)
    text, rows = await _open_stats(plugin, bot, "tb:s:991:stats/7d", language)
    # What was saved still shows; the note is its own last line, outside every tag.
    assert text == f"{report}\n{note}"
    assert rows == [
        [(label, f"tb:s:991:stats/{sub}") for label, sub in _buttons("7d", language or "en")],
        [(back, "tb:b:991")],
    ]
    assert _view(sp, "7d", language_code=language, history_paused=True).text == text

    # The other states end with it too, but not history off: there is nothing to pause.
    lang = language or "en"
    no_history = NO_HISTORY_RU if lang == "ru" else NO_HISTORY
    assert _view(sp, chat=12345, language_code=language, history_paused=True).text == (
        f"<b>{ICON} {'чат' if lang == 'ru' else 'chat'} 12345</b>\n{no_history}\n{note}"
    )
    assert _view(sp, conn=None, language_code=language, history_paused=True).text == (
        f"{NO_CONNECTION_RU if lang == 'ru' else NO_CONNECTION}\n{note}"
    )
    _break_history(plugin, "torn_tail")
    view = _view(sp, language_code=language, history_paused=True)
    assert view.text == f"{READ_ERROR_RU if lang == 'ru' else READ_ERROR}\n{note}"
    assert view.buttons == _buttons("30d", lang)


@pytest.mark.asyncio
@pytest.mark.parametrize(("language", "expected"), [(None, HISTORY_OFF), ("ru", HISTORY_OFF_RU)])
async def test_paused_note_is_not_shown_while_history_is_off(plugin, sp, modes, language, expected: str):
    _alice_fixture(plugin)
    top.bind_owner(plugin)
    sp.register_section()
    _pause(plugin)
    bot = top.PanelBot(business_owner_id=top.OWNER)

    text, _ = await _open_stats(plugin, bot, "tb:s:991:stats", language)

    assert text == expected
    assert _view(sp, language_code=language, history_paused=True).text == expected


def test_paused_note_stays_when_the_report_is_cut_to_fit(
    enabled_history, sp, cs, render, monkeypatch: pytest.MonkeyPatch
):
    _alice_fixture(enabled_history)
    window = sp.period_window("all", now=NOW)
    [entry] = enabled_history._history_support._filter_catalog_entries(
        business_connection_id="business-123", chat_id="991"
    )
    stats = cs.collect_contact_stats(entry, window)
    for language, note in ((None, PAUSED), ("ru", PAUSED_RU)):
        full = sp.render_report(stats, window, period="all", language_code=language).text
        noted = sp.render_report(stats, window, period="all", language_code=language, note=note).text
        assert noted == f"{full}\n{note}"
        # A tight limit drops the report's last blocks, never the note, and keeps every tag closed.
        with monkeypatch.context() as patch:
            patch.setattr(render, "MAX_TEXT_UNITS", _utf16_units(full))
            cut = sp.render_report(stats, window, period="all", language_code=language, note=note).text
        assert cut.endswith(f"\n{note}") and _utf16_units(cut) <= _utf16_units(full) < _utf16_units(noted)
        assert cut.startswith(full.split("\n", 1)[0])
        for tag in ("b", "i", "pre"):
            assert cut.count(f"<{tag}>") == cut.count(f"</{tag}>")


# --- Docs ------------------------------------------------------------------------


def test_docs_owner_panel_example_matches_view(enabled_history, sp, monkeypatch: pytest.MonkeyPatch):
    _overview_fixture(enabled_history)
    _hermes_tz(monkeypatch, "Europe/Moscow")
    section = _doc_section(STATS_DOC.read_text(encoding="utf-8"), "In Telegram (owner panel)")
    view = _view(sp)
    assert view.text == ALICE_MONTH

    [(language, documented)] = _fenced_blocks(section)

    assert language == "text"
    assert documented == _visible(view.text) + "\n\n" + " ".join(f"[{label}]" for label, _ in view.buttons)
    for _, since in PERIODS[:3]:
        assert f"`--since {since}`" in section
    assert "docs/STATS.md#in-telegram-owner-panel" in README_DOC.read_text(encoding="utf-8")
    not_in_version = _doc_section(STATS_DOC.read_text(encoding="utf-8"), "Not in this version")
    for issue in (27, 29):
        assert f"issues/{issue})" in not_in_version
