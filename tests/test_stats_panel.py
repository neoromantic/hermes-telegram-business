from __future__ import annotations

import importlib
import logging
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import test_history as th
import test_owner_panel as top
import test_transcription_modes as ttm
from test_history import _build_history_parser, snapshot_history_state
from test_stats import (
    NOW,
    README_DOC,
    STATS_DOC,
    TIMEZONE_ENV,
    _alice_fixture,
    _break_history,
    _doc_section,
    _entry,
    _fenced_blocks,
    _msg,
    _overview_fixture,
    _seed,
    _stats,
    _utc,
)

plugin = th.plugin
enabled_history = th.enabled_history
modes = ttm.modes

HISTORY_ENABLE_ENV = "HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE"
ICON = "\U0001f4ca"
BASIS = "by Telegram send time; text messages only"
# (sub-argument, CLI --since, header label)
PERIODS = [
    ("7d", "7d", "last 7 days"),
    ("30d", "30d", "last 30 days"),
    ("90d", "90d", "last 90 days"),
    ("all", None, "all history"),
]
CHATS = [(991, "business-123"), (992, "business-123"), (994, "business-456"), (995, "business-123")]
SUB_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
READ_ERROR = (
    f"{ICON} Stats\nStats unavailable: history could not be read. For details, run on the gateway host:\n"
    "hermes telegram-business stats --chat 991 --connection business-123"
)


@pytest.fixture(autouse=True)
def _fixed_clock(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(TIMEZONE_ENV, raising=False)
    monkeypatch.delenv(HISTORY_ENABLE_ENV, raising=False)
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: NOW)


@pytest.fixture
def sp(plugin):
    return importlib.import_module(f"{plugin.__name__}.stats_panel")


@pytest.fixture
def cs(plugin):
    return importlib.import_module(f"{plugin.__name__}.correspondence_stats")


def _view(sp, period: str | None = None, *, chat: Any = 991, conn: str | None = "business-123", **kwargs: Any):
    return sp.build_stats_view(chat_id=chat, business_connection_id=conn, period=period, **kwargs)


def _panel_records(caplog: pytest.LogCaptureFixture, sp) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == sp.__name__]


def _buttons(current: str) -> tuple[tuple[str, str], ...]:
    labels = {"7d": "7d", "30d": "30d", "90d": "90d", "all": "All"}
    return tuple((f"• {label}" if sub == current else label, sub) for sub, label in labels.items())


# --- Windows and body ------------------------------------------------------------


@pytest.mark.parametrize("tz", [None, "Europe/Moscow"])
def test_period_windows_equal_cli_windows(plugin, sp, cs, tz: str | None):
    environ = {} if tz is None else {TIMEZONE_ENV: tz}
    parser = _build_history_parser(plugin)
    for sub, since, _ in PERIODS:
        args = parser.parse_args(["stats", *(["--since", since] if since else [])])
        window = sp.period_window(sub, now=NOW, environ=environ)
        assert window == cs.build_window(args, now=NOW, environ=environ)
        assert window.until is None and window.now == NOW and window.session_gap == timedelta(hours=6)
        assert window.tz_label == (tz or "UTC")
    assert sp.period_window("7d", now=NOW, environ={}).since == NOW - timedelta(days=7)
    assert sp.period_window("all", now=NOW, environ={}).since is None
    # Unknown or missing periods mean the default.
    for period in (None, "", "bogus"):
        assert sp.period_window(period, now=NOW, environ={}) == sp.period_window("30d", now=NOW, environ={})


@pytest.mark.parametrize("tz", [None, "Europe/Moscow"])
def test_body_equals_cli_report_for_every_period(enabled_history, sp, monkeypatch: pytest.MonkeyPatch, tz: str | None):
    _overview_fixture(enabled_history)
    if tz is not None:
        monkeypatch.setenv(TIMEZONE_ENV, tz)
    hinted = 0
    for sub, since, _ in PERIODS:
        exit_code, output = _stats(
            enabled_history, "--chat", "991", "--connection", "business-123", *(["--since", since] if since else [])
        )
        assert exit_code == 0, output
        cli = output.splitlines()
        assert cli[1].startswith("Alice Example (@alice) connection=business-123 chat=991")
        hinted += any(line.lstrip().startswith("deletion details:") for line in cli)
        expected = [line.lstrip() for line in cli[2:] if not line.lstrip().startswith("deletion details:")]
        before = snapshot_history_state(enabled_history)

        lines = _view(sp, sub).text.splitlines()

        assert lines[2:-1] == expected
        # Read-only: the panel wrote nothing.
        assert snapshot_history_state(enabled_history) == before
        assert not any("connection=" in line or "deletion details" in line for line in lines)
    # The CLI-only hint was really there to drop (the 7 days have no unexplained deletion).
    assert hinted == len(PERIODS) - 1


def test_header_since_line_and_footer(enabled_history, sp, monkeypatch: pytest.MonkeyPatch):
    _alice_fixture(enabled_history)

    week = _view(sp, "7d").text.splitlines()
    assert week[0] == f"{ICON} Alice Example (@alice) · last 7 days"
    assert week[1] == f"since 2026-10-12 11:32 UTC, {BASIS}"
    assert week[-1] == "as of 2026-10-19 11:32"
    for sub, _, header in PERIODS:
        lines = _view(sp, sub).text.splitlines()
        assert lines[0] == f"{ICON} Alice Example (@alice) · {header}"
        assert lines[1].startswith("since ") != (sub == "all")
    assert _view(sp, "all").text.splitlines()[1] == f"UTC, {BASIS}"
    assert _view(sp, "90d").text.splitlines()[1] == f"since 2026-07-21 11:32 UTC, {BASIS}"

    # The time zone comes from the environment, as for the CLI.
    monkeypatch.setenv(TIMEZONE_ENV, "Europe/Moscow")
    moscow = _view(sp, "7d").text.splitlines()
    assert moscow[1] == f"since 2026-10-12 14:32 Europe/Moscow, {BASIS}"
    assert moscow[-1] == "as of 2026-10-19 14:32"
    assert _view(sp, "all").text.splitlines()[1] == f"Europe/Moscow, {BASIS}"
    # An explicit environ wins over os.environ.
    assert _view(sp, "7d", environ={}).text.splitlines()[1] == f"since 2026-10-12 11:32 UTC, {BASIS}"


# --- Buttons ---------------------------------------------------------------------


def test_period_buttons(enabled_history, sp):
    _alice_fixture(enabled_history)
    assert [sub for sub, *_ in sp.PERIODS] == ["7d", "30d", "90d", "all"]
    assert sp.SECTION_KEY == "stats" and sp.SECTION_LABEL == f"{ICON} Stats" and sp.DEFAULT_PERIOD == "30d"

    for sub, _, header in PERIODS:
        view = _view(sp, sub)
        assert view.buttons == _buttons(sub)
        assert sp.period_buttons(sub) == _buttons(sub)
        assert view.text.splitlines()[0].endswith(f"· {header}")
    assert _buttons("30d") == (("7d", "7d"), ("• 30d", "30d"), ("90d", "90d"), ("All", "all"))
    for period in (None, "", "bogus", "30D", "all/x"):
        view = _view(sp, period)
        assert view.buttons == _buttons("30d")
        assert view.text.splitlines()[0].endswith("· last 30 days")

    for _, sub in _buttons("all"):
        assert SUB_RE.fullmatch(sub)
        assert len(f"tb:s:-{'9' * 20}:stats/{sub}".encode()) <= 64


# --- States ----------------------------------------------------------------------


def test_history_off(plugin, sp):
    _alice_fixture(plugin)
    view = _view(sp)
    assert view.text == (
        f"{ICON} Stats\nStats need history: set HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE=1 and restart the gateway."
    )
    assert view.buttons == ()


def test_state_order(plugin, sp, monkeypatch: pytest.MonkeyPatch):
    history_off = (
        f"{ICON} Stats\nStats need history: set HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE=1 and restart the gateway."
    )
    no_connection = f"{ICON} Stats\nStats unavailable: the Business connection for this chat is not known yet."
    bad_tz = {TIMEZONE_ENV: "Mars/Olympus"}
    # History off wins over a missing connection and a bad time zone.
    assert _view(sp, conn=None).text == history_off
    assert _view(sp, conn=None, environ=bad_tz).text == history_off
    assert _view(sp, environ=bad_tz).text == history_off
    # With history on, a missing connection wins over a bad time zone.
    monkeypatch.setenv(HISTORY_ENABLE_ENV, "1")
    view = _view(sp, conn=None, environ=bad_tz)
    assert view.text == no_connection and view.buttons == ()


@pytest.mark.parametrize("conn", [None, ""])
def test_connection_missing(enabled_history, sp, conn: str | None):
    _alice_fixture(enabled_history)
    view = _view(sp, conn=conn)
    assert view.text == f"{ICON} Stats\nStats unavailable: the Business connection for this chat is not known yet."
    assert view.buttons == ()


def test_bad_timezone(enabled_history, sp, monkeypatch: pytest.MonkeyPatch):
    _alice_fixture(enabled_history)
    expected = (
        f"{ICON} Stats\nStats unavailable: HERMES_TELEGRAM_BUSINESS_TIMEZONE is not a known time zone; "
        "use an IANA name such as Europe/Moscow."
    )
    assert _view(sp, environ={TIMEZONE_ENV: "Mars/Olympus"}).text == expected
    monkeypatch.setenv(TIMEZONE_ENV, "Not/AZone")
    view = _view(sp)
    assert view.text == expected and view.buttons == ()


def test_no_history_for_chat(enabled_history, sp):
    no_history = "No history for this chat yet. History records new text messages only."
    # No history at all yet, and the panel creates none.
    assert snapshot_history_state(enabled_history) == {}
    assert _view(sp, chat=12345).text == f"{ICON} Stats: chat 12345\n{no_history}"
    assert snapshot_history_state(enabled_history) == {}
    _alice_fixture(enabled_history)
    before = snapshot_history_state(enabled_history)
    view = _view(sp, chat=12345, chat_label="Zoë")
    assert view.text == f"{ICON} Stats: Zoë\n{no_history}"
    assert view.buttons == ()
    assert snapshot_history_state(enabled_history) == before
    # The label is untrusted: escaped and clipped like contact names.
    view = _view(sp, chat=12345, chat_label="Eve\nX\u202e" + "y" * 200)
    first = view.text.splitlines()[0]
    assert first.startswith(f"{ICON} Stats: Eve\\nX\\u202e") and first.endswith("…")
    assert len(first) == len(f"{ICON} Stats: ") + 100
    assert "\u202e" not in view.text


def test_same_chat_under_another_connection_is_not_used(enabled_history, sp):
    _overview_fixture(enabled_history)
    # Carol (chat 994) exists only under business-456.
    view = _view(sp, chat=994, conn="business-123")
    assert view.text.startswith(f"{ICON} Stats: chat 994\nNo history for this chat yet.")
    assert "Carol" not in view.text
    assert _view(sp, chat=994, conn="business-456").text.startswith(f"{ICON} Carol · last 30 days\n")


def test_no_activity_in_period_renders_zero_report(enabled_history, sp):
    _overview_fixture(enabled_history)
    # Dave (chat 995) wrote only on 2026-09-10.
    before = snapshot_history_state(enabled_history)
    view = _view(sp, "30d", chat=995)
    assert view.text.splitlines() == [
        f"{ICON} Dave (@dave) · last 30 days",
        f"since 2026-09-19 11:32 UTC, {BASIS}",
        "messages 0: received 0, sent 0, unknown direction 0",
        "active days 0 (received 0, sent 0)",
        "latest none",
        "sessions 0 (gap 6h)",
        "deletions 0",
        "missing history: none (retained from 2026-09-10)",
        "as of 2026-10-19 11:32",
    ]
    assert view.buttons == _buttons("30d")
    assert "messages 1: received 1" in _view(sp, "all", chat=995).text
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
    [record] = _panel_records(caplog, sp)
    assert record.levelno == logging.WARNING
    assert re.fullmatch(r"telegram-business stats panel: history could not be read \((\w+)\)", record.getMessage())
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
        f"telegram-business stats panel: history could not be read ({error.__name__})"
    ]
    for secret in (message, "Alice", "SECRET", "/home/owner"):
        assert secret not in caplog.text


def test_read_error_command_quotes_values(enabled_history, sp, cs, monkeypatch, caplog: pytest.LogCaptureFixture):
    entries = [_entry(conn="-Ab C"), _entry(conn="-Ab C")]
    # Two catalog entries for one (connection, chat) mean the catalog is damaged.
    monkeypatch.setattr(enabled_history._history_support, "_filter_catalog_entries", lambda **_kwargs: entries)
    caplog.set_level(logging.WARNING)

    view = _view(sp, chat=-100123, conn="-Ab C")

    assert view.text.splitlines()[-1] == "hermes telegram-business stats --chat=-100123 --connection='-Ab C'"
    assert view.text.splitlines()[-1].endswith(cs._cli_option("--connection", "-Ab C"))
    assert [record.getMessage() for record in _panel_records(caplog, sp)] == [
        "telegram-business stats panel: history could not be read (ValueError)"
    ]


# --- Names, size, and privacy -----------------------------------------------------


def test_contact_name_is_escaped_like_the_cli(enabled_history, sp, cs, monkeypatch: pytest.MonkeyPatch):
    profile = {"id": 993, "type": "private", "first_name": "Eve\x1b[31m\nEvil\u202eman\u2066", "username": "eve"}
    _seed(enabled_history, _msg(enabled_history, 1, _utc(2026, 10, 18, 9, 0), "inbound", chat=993, profile=profile))
    window = sp.period_window("30d", now=NOW, environ={})
    stats = cs.compute_stats(_entry(chat=993, profile=profile), [], window)

    header = _view(sp, chat=993).text.splitlines()[0]

    assert header == f"{ICON} {cs._safe(stats.display_name)} · last 30 days"
    assert header == f"{ICON} Eve\\x1b[31m\\nEvil\\u202eman\\u2066 (@eve) · last 30 days"
    # The panel shows the same name as the CLI.
    exit_code, output = _stats(enabled_history, "--chat", "993")
    assert exit_code == 0 and output.splitlines()[1].startswith(f"{cs._safe(stats.display_name)} connection=")


@pytest.mark.parametrize(
    ("first_name", "expected"),
    [
        ("A" * 100, "A" * 100),
        ("A" * 101, "A" * 99 + "…"),
        ("\x01" * 60, "\\x01" * 24 + "\\x0…"),
        ("\U0001f600" * 150, "\U0001f600" * 99 + "…"),
    ],
)
def test_contact_name_is_clipped(sp, cs, first_name: str, expected: str):
    window = sp.period_window("7d", now=NOW, environ={})
    stats = cs.compute_stats(_entry(profile={"id": 991, "type": "private", "first_name": first_name}), [], window)
    assert sp.render_report(stats, window, period="7d").text.splitlines()[0] == f"{ICON} {expected} · last 7 days"


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_text_fits_telegram_limit(sp, cs, monkeypatch: pytest.MonkeyPatch):
    window = sp.period_window("all", now=NOW, environ={})
    huge = {"id": 991, "type": "private", "first_name": "\U0001f600\u202e" * 5000, "username": "\U0001f600" * 5000}
    stats = cs.compute_stats(_entry(profile=huge), [], window)
    worst = sp.render_report(stats, window, period="all").text
    assert _utf16_units(worst) <= 4096

    # The guard holds even if the body ever grows past the limit.
    lines = ["range", "identity", *("\U0001f600" * 50 for _ in range(100))]
    monkeypatch.setattr(cs, "render_contact_text", lambda *_args: lines)
    clipped = sp.render_report(stats, window, period="all").text
    assert _utf16_units(clipped) == 4096 or _utf16_units(clipped) == 4095
    assert clipped.endswith("…")
    clipped.encode("utf-8")  # no split surrogate pair


def test_message_text_never_appears(enabled_history, sp, monkeypatch: pytest.MonkeyPatch, caplog):
    _overview_fixture(enabled_history)
    caplog.set_level(logging.DEBUG)
    views = []
    for tz in (None, "Europe/Moscow"):
        environ = {} if tz is None else {TIMEZONE_ENV: tz}
        for sub, *_ in PERIODS:
            for chat, conn in CHATS:
                views.append(_view(sp, sub, chat=chat, conn=conn, environ=environ))
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
        register_cli_command=lambda **kwargs: None,
        register_skill=lambda name, path, description="": None,
    )
    assert plugin._owner_panel.get_section("stats") is None

    plugin.register(ctx)

    section = plugin._owner_panel.get_section("stats")
    assert (section.label, section.order) == (f"{ICON} Stats", 20)
    assert section.render is plugin._stats_panel.render_section


@pytest.mark.asyncio
async def test_panel_opens_stats_and_switches_periods(enabled_history, sp, modes):
    plugin = enabled_history
    _alice_fixture(plugin)
    top.bind_owner(plugin)
    sp.register_section()
    bot = top.PanelBot(business_owner_id=top.OWNER)

    assert await top.dispatch(plugin, top.start_update(), bot) is True
    assert (f"{ICON} Stats", "tb:s:991:stats") in [row[0] for row in top.keyboard(bot.calls[0]["reply_markup"])]

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats"), bot) is True
    text, rows = top.last_edit(bot)
    assert text == _view(sp).text
    assert text.startswith(f"{ICON} Alice Example (@alice) · last 30 days\n")
    assert rows == [
        [
            ("7d", "tb:s:991:stats/7d"),
            ("• 30d", "tb:s:991:stats/30d"),
            ("90d", "tb:s:991:stats/90d"),
            ("All", "tb:s:991:stats/all"),
        ],
        [("« Back", "tb:b:991")],
    ]

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats/all"), bot) is True
    text, rows = top.last_edit(bot)
    assert text == _view(sp, "all").text
    assert rows[0][3] == ("• All", "tb:s:991:stats/all")
    # Everything went to the owner's private chat; nothing reached the Business chat.
    assert {call["chat_id"] for call in [*bot.calls, *bot.text_edit_calls]} == {top.OWNER}


@pytest.mark.asyncio
async def test_panel_state_without_buttons_keeps_only_back(plugin, sp, modes):
    top.bind_owner(plugin)
    sp.register_section()
    bot = top.PanelBot(business_owner_id=top.OWNER)

    assert await top.dispatch(plugin, top.callback_update("tb:s:991:stats"), bot) is True
    assert top.last_edit(bot) == (sp.HISTORY_DISABLED, [[("« Back", "tb:b:991")]])


# --- Docs ------------------------------------------------------------------------


def test_docs_owner_panel_example_matches_view(enabled_history, sp, monkeypatch: pytest.MonkeyPatch):
    _overview_fixture(enabled_history)
    monkeypatch.setenv(TIMEZONE_ENV, "Europe/Moscow")
    section = _doc_section(STATS_DOC.read_text(encoding="utf-8"), "In Telegram (owner panel)")
    view = _view(sp)

    [(language, documented)] = _fenced_blocks(section)

    assert language == "text"
    assert documented == view.text + "\n\n" + " ".join(f"[{label}]" for label, _ in view.buttons)
    for _, since, _ in PERIODS[:3]:
        assert f"`--since {since}`" in section
    assert "docs/STATS.md#in-telegram-owner-panel" in README_DOC.read_text(encoding="utf-8")
    not_in_version = _doc_section(STATS_DOC.read_text(encoding="utf-8"), "Not in this version")
    assert "/stats" not in not_in_version
    for issue in (27, 28, 29):
        assert f"issues/{issue})" in not_in_version
