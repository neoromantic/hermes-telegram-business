"""Executable boundary/authorization examples, using synthetic canonical history and fake Bot API."""
from __future__ import annotations

import argparse
import importlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import TypeHandler

import test_stats as ts
import test_telegram_integration as tg

plugin = ts.plugin
cs = ts.cs
telegram_app = tg.telegram_app


def window(cs, *, now=ts.NOW, until=None, since=None, overdue=None, tz=None):
    return cs.build_window(argparse.Namespace(
        since=since, until=until, tz=tz, session_gap="6h", overdue_after=overdue,
    ), now=now, environ={})


def test_turn_runs_gap_unknown_ties_numeric_ids_and_last_to_first(plugin, cs):
    at = ts._utc(2026, 10, 18, 0)
    records = [
        ts._msg(plugin, 2, at, "inbound"),
        ts._msg(plugin, 10, at, "outbound"),
        ts._msg(plugin, 11, at + timedelta(seconds=5), "outbound"),
        ts._msg(plugin, 12, at + timedelta(seconds=35), "inbound"),
        ts._msg(plugin, 13, at + timedelta(seconds=45), "inbound"),
        ts._msg(plugin, 14, at + timedelta(hours=6, seconds=45), "outbound"),
        ts._msg(plugin, 15, at + timedelta(hours=6, seconds=46), "unknown"),
        ts._msg(plugin, 16, at + timedelta(hours=6, seconds=47), "inbound"),
        ts._msg(plugin, 17, at + timedelta(hours=12, seconds=48), "outbound"),
    ]
    # Numeric IDs break timestamp ties, regardless of input/canonical order.
    records[0], records[1] = records[1], records[0]
    report = cs.compute_stats(ts._entry(), records, window(cs))
    assert report.turns.received == 3 and report.turns.sent == 3
    assert report.turns.owner_seconds == (0, 21600)
    assert report.turns.contact_seconds == (30,)
    assert report.turns.unanswered_by_session == (1, 0)
    assert report.turns.session_ended_incoming == 1
    assert not report.turns.pending_latest_incoming
    assert report.turns.overdue_latest_incoming is None


def test_unknown_barrier_empty_samples_and_missing_history(plugin, cs):
    at = ts._utc(2026, 10, 18, 0)
    records = [ts._msg(plugin, i, at + timedelta(seconds=i), side)
               for i, side in enumerate(("inbound", "unknown", "outbound"), 1)]
    report = cs.compute_stats(ts._entry(), records, window(cs))
    assert report.turns.owner_seconds == report.turns.contact_seconds == ()
    assert report.turns.unknown_interrupted_incoming == 1
    doc = cs._contact_document(report, window(cs).tz)
    assert doc["response_time"]["owner"] == {"median_elapsed_seconds": None, "samples": 0}
    empty = cs.compute_stats(ts._entry(), [], window(cs))
    assert empty.turns.unanswered_by_session == ()
    assert empty.turns.received == empty.turns.sent == 0
    edited = cs.compute_stats(ts._entry(), [ts._msg(plugin, 1, at, "inbound", edited=True)], window(cs))
    assert edited.turns.received == 0


@pytest.mark.parametrize("delay,side,ended,answered,interrupted", [
    (60, "outbound", 0, 1, 0), (21601, "outbound", 1, 0, 0),
    (60, "unknown", 0, 0, 1), (60, "inbound", 0, 0, 0),
])
def test_historical_range_later_sources_never_overdue(plugin, cs, delay, side, ended, answered, interrupted):
    at = ts._utc(2026, 10, 18, 0)
    records = [ts._msg(plugin, 1, at, "inbound"),
               ts._msg(plugin, 2, at + timedelta(seconds=delay), side)]
    report = cs.compute_stats(ts._entry(), records, window(cs, until=at.isoformat(), overdue="1h"))
    assert not report.turns.pending_latest_incoming
    assert report.turns.overdue_latest_incoming is False
    assert report.turns.owner_seconds == ()
    assert report.turns.session_ended_incoming == ended
    assert report.turns.answered_after_range == answered
    assert report.turns.unknown_interrupted_incoming == interrupted


def test_pending_threshold_exact_timezone_correction_and_range_start(plugin, cs):
    at = ts._utc(2026, 10, 18, 0)
    records = [ts._msg(plugin, 1, at, "outbound"), ts._msg(plugin, 2, at + timedelta(minutes=1), "inbound")]
    tomb = ts._tomb(plugin, 1, at + timedelta(minutes=2), "outbound", message_at=at)
    records += [tomb, ts._cls(plugin, tomb, "likely_correction", at + timedelta(minutes=3), "replacement")]
    report = cs.compute_stats(ts._entry(), records, window(cs, now=at + timedelta(hours=1, minutes=1),
                                                         overdue="1h", tz="Europe/Berlin"))
    assert report.turns.sent == 0 and report.turns.received == 1
    assert report.turns.contact_seconds == ()
    assert report.turns.pending_latest_incoming and report.turns.overdue_latest_incoming
    later = cs.compute_stats(ts._entry(), records, window(cs, since=(at + timedelta(minutes=1)).isoformat()))
    assert later.turns.contact_seconds == ()


def payload(*, text="/business_stats --connection business-123", sender=1000, chat=None, kind="message", **fields):
    return {"update_id": 840, kind: {
        "message_id": 880, "date": 1, "text": text,
        "chat": {"id": sender if chat is None else chat, "type": "private"},
        "from": {"id": sender, "is_bot": False, "first_name": "Owner"}, **fields,
    }}


@pytest.mark.asyncio
async def test_native_stats_cold_cache_exact_scope_duplicate_and_safe_dm(plugin, telegram_app, monkeypatch):
    app, request = telegram_app
    ts._overview_fixture(plugin)
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: ts.NOW)
    plugin._history_support._OWNER_CACHE["business-123"] = "999999"  # never trusted
    plugin._transcription_modes.default_store().bind_connection("business-123", "999999")
    core = AsyncMock()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    app.add_handler(TypeHandler(Update, core))
    update = Update.de_json(payload(text="/business_stats --connection business-123 --contact @alice"), app.bot)
    await app.process_update(update)
    await app.process_update(update)
    assert core.await_count == 0
    lookups = [params for endpoint, params in request.calls if endpoint == "getBusinessConnection"]
    assert lookups == [{"business_connection_id": "business-123"}]
    sends = [params for endpoint, params in request.calls if endpoint == "sendMessage"]
    assert sends and all(params["chat_id"] == "1000" for params in sends)
    assert all("business_connection_id" not in params and "parse_mode" not in params for params in sends)
    assert all(len(params["text"]) <= 3500 for params in sends)
    assert "Alice Example" in sends[0]["text"] and "SECRET-" not in sends[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"sender": 2000}, {"chat": -100}, {"kind": "business_message", "business_connection_id": "business-123"},
    {"kind": "edited_message"}, {"via_bot": {"id": 9, "is_bot": True, "first_name": "Bot"}},
    {"forward_origin": {"type": "hidden_user", "date": 1, "sender_user_name": "Other"}},
    {"from": {"id": 1000, "is_bot": True, "first_name": "Bot"}},
    {"chat": {"id": 1000, "type": "group"}},
    {"text": "/business_stats --connection business-456 --contact @alice"},
    {"text": "/business_stats --owner 1000"},
])
async def test_native_reserved_unsafe_stats_never_core_or_private_send(plugin, telegram_app, change):
    app, request = telegram_app
    if isinstance(change.get("chat"), dict):
        data = payload()
        data["message"]["chat"] = change["chat"]
    else:
        data = payload(**change)
    core = AsyncMock()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    app.add_handler(TypeHandler(Update, core))
    await app.process_update(Update.de_json(data, app.bot))
    core.assert_not_awaited()
    assert not [params for endpoint, params in request.calls if endpoint == "sendMessage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disabled", "wrong_owner", "missing_user_chat", "lookup_error"])
async def test_native_stats_live_verification_fail_closed(plugin, telegram_app, failure):
    app, request = telegram_app
    result = request.responses["getBusinessConnection"][1]["result"]
    if failure == "disabled":
        result["is_enabled"] = False
    elif failure == "wrong_owner":
        result["user"]["id"] = 2000
    elif failure == "missing_user_chat":
        result.pop("user_chat_id")
    else:
        request.responses["getBusinessConnection"] = (403, {"ok": False, "description": "Forbidden"})
    core = AsyncMock()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    app.add_handler(TypeHandler(Update, core))
    await app.process_update(Update.de_json(payload(), app.bot))
    core.assert_not_awaited()
    assert not [params for endpoint, params in request.calls if endpoint == "sendMessage"]


@pytest.mark.asyncio
async def test_dict_same_boundary_lookup_exception_and_report_exception(plugin, monkeypatch):
    module = importlib.import_module(f"{plugin.__name__}.owner_stats")
    bot = SimpleNamespace(get_business_connection=AsyncMock(side_effect=OSError), send_message=AsyncMock())
    assert await module.handle_update(payload(), bot=bot, mark_seen=lambda identity: True)
    bot.send_message.assert_not_awaited()
    bot.get_business_connection = AsyncMock(return_value={
        "id": "business-123", "is_enabled": True, "user": {"id": 1000, "is_bot": False}, "user_chat_id": 1000,
    })
    monkeypatch.setattr(module._stats, "_run", lambda args: (_ for _ in ()).throw(ValueError("retained history unavailable")))
    assert await module.handle_update(payload(), bot=bot, mark_seen=lambda identity: True)
    assert bot.send_message.call_args.kwargs["text"] == "error: retained history unavailable"
    assert bot.send_message.call_args.kwargs["chat_id"] == 1000
    assert len(module.chunks("a" * 100000)) == 1
    assert "exceeds" in module.chunks("a" * 100000)[0]


def test_dst_fallback_elapsed_time_and_string_numeric_ties(plugin, cs):
    from datetime import datetime
    before = datetime.fromisoformat("2026-10-25T02:30:00+02:00")
    after = datetime.fromisoformat("2026-10-25T02:30:00+01:00")
    records = [ts._msg(plugin, "10", after, "outbound"), ts._msg(plugin, "2", before, "inbound")]
    report = cs.compute_stats(ts._entry(), records, window(cs, tz="Europe/Berlin", now=after))
    assert report.turns.owner_seconds == (3600,)
    assert cs._contact_document(report, window(cs).tz)["response_time"]["owner"] == {
        "median_elapsed_seconds": 3600, "samples": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("poison", [False, True])
async def test_native_overview_and_same_name_scoped_before_lookup(plugin, telegram_app, monkeypatch, poison):
    app, request = telegram_app
    ts._overview_fixture(plugin)
    # A foreign-owner contact sharing Alice's alias must not cause ambiguity or
    # leak the name in a scoped resolver error/overview.
    ts._seed(plugin, ts._msg(plugin, 300, ts.NOW, "inbound", conn="business-456", chat=1999,
                            profile={"id": 1999, "type": "private", "username": "alice", "first_name": "Foreign Secret"}))
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: ts.NOW)
    assert not plugin._history_support._OWNER_CACHE
    if poison:
        plugin._history_support._OWNER_CACHE["business-123"] = "99999"
        plugin._transcription_modes.default_store().bind_connection("business-123", "99999")
    core = AsyncMock()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    app.add_handler(TypeHandler(Update, core))
    for mid, command in enumerate(("/business_stats --connection business-123",
                                   "/business_stats --connection business-123 --contact @alice"), 900):
        data = payload(text=command)
        data["message"]["message_id"] = mid
        await app.process_update(Update.de_json(data, app.bot))
    core.assert_not_awaited()
    assert len([1 for endpoint, _params in request.calls if endpoint == "getBusinessConnection"]) == 2
    texts = [params["text"] for endpoint, params in request.calls if endpoint == "sendMessage"]
    assert len(texts) >= 2 and all("Foreign Secret" not in text and "Carol" not in text for text in texts)
    assert "Alice Example" in texts[-1] and "ambiguous" not in texts[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["channel_post", "edited_channel_post"])
async def test_native_channel_reserved_command_consumed(plugin, telegram_app, kind):
    app, request = telegram_app
    core = AsyncMock()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    app.add_handler(TypeHandler(Update, core))
    await app.process_update(Update.de_json(payload(kind=kind), app.bot))
    core.assert_not_awaited()
    assert [endpoint for endpoint, _params in request.calls] == ["getMe"]


@pytest.mark.asyncio
async def test_native_unexpected_report_error_consumed_and_no_send(plugin, telegram_app, monkeypatch):
    app, request = telegram_app
    module = importlib.import_module(f"{plugin.__name__}.owner_stats")
    def fail(args):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(module._stats, "_run", fail)
    core = AsyncMock()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    app.add_handler(TypeHandler(Update, core))
    await app.process_update(Update.de_json(payload(), app.bot))
    core.assert_not_awaited()
    assert not [params for endpoint, params in request.calls if endpoint == "sendMessage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"text": "/business_stats@other_bot --connection business-123"},
    {"sender_chat": {"id": -1, "type": "channel", "title": "Fake"}},
    {"is_automatic_forward": True},
])
async def test_native_spoofed_fields_no_lookup(plugin, telegram_app, fields):
    app, request = telegram_app
    core = AsyncMock()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    app.add_handler(TypeHandler(Update, core))
    await app.process_update(Update.de_json(payload(**fields), app.bot))
    core.assert_not_awaited()
    assert [endpoint for endpoint, _params in request.calls] == ["getMe"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["forbidden", "timeout"])
async def test_native_private_delivery_failure_is_actionable_without_retry(plugin, telegram_app, monkeypatch,
                                                                          caplog, failure):
    app, request = telegram_app
    module = importlib.import_module(f"{plugin.__name__}.owner_stats")
    # More than one chunk proves that a failed first send stops the remaining
    # chunks, while the transport records exactly one attempted send.
    monkeypatch.setattr(module._stats, "_run", lambda args: ["x" * 7000])
    if failure == "forbidden":
        request.responses["sendMessage"] = (403, {"ok": False, "description": "Forbidden: bot was blocked"})
    else:
        from telegram.error import TimedOut
        original = request.do_request
        async def timed_out(url, method, request_data=None, **kwargs):
            if url.endswith("/sendMessage"):
                request.calls.append(("sendMessage", request_data.json_parameters))
                raise TimedOut("ambiguous transport")
            return await original(url, method, request_data, **kwargs)
        monkeypatch.setattr(request, "do_request", timed_out)
    core = AsyncMock()
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    app.add_handler(TypeHandler(Update, core))
    update = Update.de_json(payload(), app.bot)
    await app.process_update(update)
    await app.process_update(update)
    core.assert_not_awaited()
    assert len([1 for endpoint, _params in request.calls if endpoint == "sendMessage"]) == 1
    assert "owner must start/unblock the bot and retry" in caplog.text


def test_explicit_overdue_cli_text_json_and_median_sample_counts(plugin):
    at = ts._utc(2026, 10, 18, 0)
    ts._seed(plugin,
             ts._msg(plugin, 1, at, "inbound"),
             ts._msg(plugin, 2, at + timedelta(seconds=10), "outbound"),
             ts._msg(plugin, 3, at + timedelta(seconds=40), "inbound"),
             ts._msg(plugin, 4, at + timedelta(seconds=60), "outbound"),
             ts._msg(plugin, 5, at + timedelta(seconds=90), "inbound"))
    code, text = ts._stats(plugin, "--connection", "business-123", "--overdue-after", "24h")
    assert code == 0
    assert "overdue threshold 1d" in text
    assert "owner_n/contact_n are sample counts" in text
    doc = ts._contact_json(plugin, "--contact", "@alice", "--overdue-after", "24h")
    assert doc["response_time"]["owner"] == {"median_elapsed_seconds": 15, "samples": 2}
    assert doc["response_time"]["contact"] == {"median_elapsed_seconds": 30, "samples": 2}
    assert doc["turns"]["pending_latest_incoming"] is True
    assert doc["turns"]["overdue_latest_incoming"] is True
    for invalid in ("0h", "-1h", "tomorrow"):
        code, output = ts._stats(plugin, f"--overdue-after={invalid}")
        assert code == 1 and "unsupported --overdue-after" in output
