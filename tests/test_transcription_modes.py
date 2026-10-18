from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import test_plugin
import test_history

plugin = test_plugin.plugin
isolate_plugin_environment = test_plugin.isolate_plugin_environment


def parser(plugin):
    value = argparse.ArgumentParser()
    plugin._history_support.setup_cli(value)
    return value


def cli(plugin, *args):
    return plugin._history_support.handle_cli(parser(plugin).parse_args(["mode", *args]))


@pytest.mark.parametrize("mode,direction,expected", [
    (mode, direction, (mode == "both" or (mode != "off" and (direction == "unknown" or
        mode == {"outbound": "own", "inbound": "peer"}.get(direction)))))
    for mode in ("both", "own", "peer", "off") for direction in ("outbound", "inbound", "unknown")
])
def test_policy_matrix(plugin, mode, direction, expected):
    allowed, reason = plugin._transcription_modes.decision(mode, direction)
    assert allowed == expected
    if mode in {"own", "peer"} and direction == "unknown":
        assert reason == "unknown_direction_fail_open"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,sender,bot_marker,expected", [
    ("both", 1000, False, True), ("both", 2000, False, True),
    ("own", 1000, False, True), ("own", 2000, False, False),
    ("peer", 1000, False, False), ("peer", 2000, False, True),
    ("own", 2000, True, True), ("peer", 2000, True, False),
    ("off", 1000, False, False), ("off", 2000, False, False),
])
async def test_policy_before_all_media_work(plugin, monkeypatch, mode, sender, bot_marker, expected):
    modes = plugin._transcription_modes
    modes.set_mode("1000", "991", mode, via="cli")
    message = test_plugin.make_message(from_user_id=sender)
    if bot_marker:
        message.sender_business_bot = SimpleNamespace(id=999)
    bot = test_plugin.FakeBot()
    event = test_plugin.make_event(message)
    gateway = SimpleNamespace(adapters={event.source.platform: test_plugin.FakeAdapter(bot)})
    download = AsyncMock(side_effect=RuntimeError("stop after policy"))
    monkeypatch.setattr(plugin, "_download_voice", download)
    stt, cleanup = Mock(), AsyncMock()
    await plugin._process_business_voice_event(event=event, gateway=gateway, transcribe_fn=stt, cleanup_fn=cleanup)
    assert download.await_count == int(expected)
    stt.assert_not_called()
    cleanup.assert_not_awaited()
    assert not bot.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voice", "video_note", "audio", "document"])
async def test_off_claimed_silently_in_normalized_path(plugin, monkeypatch, kind):
    plugin._transcription_modes.set_mode("1000", "991", "off", via="chat")
    message = (test_plugin.make_audio_file_message(media_kind=kind) if kind in {"audio", "document"}
               else test_plugin.make_message(media_kind=kind))
    event = test_plugin.make_event(message)
    bot = test_plugin.FakeBot()
    gateway = SimpleNamespace(adapters={event.source.platform: test_plugin.FakeAdapter(bot)})
    download = AsyncMock()
    monkeypatch.setattr(plugin, "_download_voice", download)
    result = plugin._on_pre_gateway_dispatch(event, gateway)
    await asyncio.gather(*plugin._pending_tasks)
    assert result["action"] == "skip"
    download.assert_not_awaited()
    assert bot.calls == [] and bot.edit_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["own", "peer"])
async def test_unknown_direction_and_unknown_owner_fail_open(plugin, mode, caplog):
    modes = plugin._transcription_modes
    modes.set_mode("1000", "991", mode, via="cli")
    message = test_plugin.make_message()
    message.from_user = None
    bot = test_plugin.FakeBot()
    with caplog.at_level(logging.INFO):
        assert await modes.should_transcribe(message, bot=bot)
    assert "unknown_direction_fail_open" in caplog.text
    plugin._business_identity.OWNER_CACHE.clear()
    message.business_connection_id = "unverified-new-connection"
    bot.business_connection_error = RuntimeError("SECRET CONTENT")
    with caplog.at_level(logging.INFO):
        assert await modes.should_transcribe(message, bot=bot)
    assert "unknown_owner_fail_open" in caplog.text
    assert "SECRET CONTENT" not in caplog.text
    assert "None" not in modes.snapshot()["chats"]


@pytest.mark.asyncio
async def test_reconnect_and_shared_owner_cache_without_history(plugin):
    modes = plugin._transcription_modes
    modes.set_mode("1000", "991", "off", via="cli")
    bot = test_plugin.FakeBot()
    first = test_plugin.make_message(business_id="old")
    second = test_plugin.make_message(business_id="new")
    assert not await modes.should_transcribe(first, bot=bot)
    assert await plugin._history_support._resolve_direction(first, bot=bot) == "inbound"
    assert not await modes.should_transcribe(second, bot=bot)
    assert bot.business_connection_calls == ["old", "new"]
    assert modes.snapshot()["owners"]["1000"] == ["old", "new"]


def test_private_atomic_write_and_reload_delete_recreate(plugin, monkeypatch):
    modes = plugin._transcription_modes
    assert modes.mode_for("1000", "991") == "both"
    seen = []
    real_replace = os.replace
    def replace(source, target):
        seen.append((Path(source).stat().st_mode & 0o777, Path(target).name))
        assert json.loads(Path(source).read_text())["version"] == 1
        real_replace(source, target)
    monkeypatch.setattr(modes.os, "replace", replace)
    modes.set_mode("1000", "991", "off", via="cli", label="Casey @casey")
    assert seen == [(0o600, "transcription-modes.json")]
    assert modes.path().stat().st_mode & 0o777 == 0o600
    assert modes.mode_for("1000", "991") == "off"
    # Simulate another process's replacement; no in-process cache invalidation.
    document = modes.snapshot()
    document["chats"]["1000"]["991"]["mode"] = "peer"
    other = modes.path().with_suffix(".other")
    other.write_text(json.dumps(document))
    real_replace(other, modes.path())
    assert modes.mode_for("1000", "991") == "peer"
    modes.path().unlink()
    assert modes.mode_for("1000", "991") == "both"
    other.write_text(json.dumps(document))
    real_replace(other, modes.path())
    assert modes.mode_for("1000", "991") == "peer"
    assert not list(modes.path().parent.glob("*.tmp"))


@pytest.mark.parametrize("bad", ["{SECRET", "[]", '{"version":99}', '{"version":1,"owners":{},"chats":{"1000":{"991":{"mode":"auto"}}}}'])
def test_corrupt_file_preserved_on_write_and_logged_without_content(plugin, bad, caplog):
    modes = plugin._transcription_modes
    modes.set_mode("1000", "991", "off", via="cli")
    assert modes.mode_for("1000", "991") == "off"
    modes.path().write_text(bad)
    assert modes.mode_for("1000", "991") == "off"
    assert "unreadable" in caplog.text and "SECRET" not in caplog.text
    with pytest.raises(ValueError):
        modes.set_mode("1000", "992", "peer", via="chat")
    assert modes.path().read_text() == bad
    modes._CACHE.clear()
    assert modes.mode_for("1000", "991") == "both"


def test_failed_replace_preserves_existing_and_removes_temp(plugin, monkeypatch):
    modes = plugin._transcription_modes
    modes.set_mode("1000", "991", "off", via="cli")
    original = modes.path().read_bytes()
    monkeypatch.setattr(modes.os, "replace", Mock(side_effect=OSError("disk error")))
    with pytest.raises(OSError):
        modes.set_mode("1000", "992", "peer", via="chat")
    assert modes.path().read_bytes() == original
    assert not list(modes.path().parent.glob("*.tmp"))


def test_concurrent_chat_cli_writes(plugin):
    modes = plugin._transcription_modes
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: modes.set_mode("1000", str(i), "off", via="cli" if i % 2 else "chat"), range(40)))
    assert len(modes.snapshot(strict=True)["chats"]["1000"]) == 40


def test_cross_process_writers_do_not_lose_updates(plugin):
    import subprocess
    import sys
    modes = plugin._transcription_modes
    modes.remember_owner("1000", "connection")
    # Independent interpreters import only this checkout with an isolated home.
    script = '''import importlib.util, sys, types
from pathlib import Path
constants=types.ModuleType("hermes_constants")
constants.get_hermes_home=lambda: Path(sys.argv[2])
sys.modules["hermes_constants"]=constants
spec=importlib.util.spec_from_file_location("subject", Path(sys.argv[1])/"__init__.py")
module=importlib.util.module_from_spec(spec)
sys.modules["subject"]=module
spec.loader.exec_module(module)
for i in range(20):
 module._transcription_modes.set_mode("1000", str(int(sys.argv[3])+i), "off", via="cli")
'''
    home = modes.path().parents[2]
    processes = [subprocess.Popen([sys.executable, "-c", script, str(test_plugin.ROOT), str(home), str(offset)]) for offset in (0, 100)]
    assert [p.wait(timeout=20) for p in processes] == [0, 0]
    assert len(modes.snapshot(strict=True)["chats"]["1000"]) == 40


@pytest.mark.parametrize("args,expected", [
    (["set", "991", "off", "--owner", "1000"], "mode=off"),
    (["get", "991", "--owner", "1000"], "mode=both"),
    (["reset", "991", "--owner", "1000"], "mode=both"),
])
def test_cli_numeric_without_history(plugin, capsys, args, expected):
    assert cli(plugin, *args) == 0
    assert expected in capsys.readouterr().out


def test_cli_list_get_set_reset_and_multiple_owners(plugin, capsys):
    modes = plugin._transcription_modes
    modes.remember_owner("1000", "one")
    assert cli(plugin, "set", "991", "own") == 0
    assert cli(plugin, "get", "991") == 0
    assert cli(plugin, "list") == 0
    out = capsys.readouterr().out
    assert "mode=own" in out and "updated_via=cli" in out
    assert cli(plugin, "reset", "991") == 0
    assert modes.mode_for("1000", "991") == "both"
    modes.remember_owner("2000", "two")
    assert cli(plugin, "get", "991") == 1
    assert "--owner" in capsys.readouterr().out
    assert cli(plugin, "get", "991", "--owner", "1000") == 0
    assert cli(plugin, "get", "@casey", "--owner", "1000") == 1
    assert "history enabled" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_existing_contact_resolver_scoped_and_ambiguous(plugin, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    history = plugin._history_support
    for connection, chat, owner in [("one", 991, 1000), ("two", 992, 2000)]:
        await history.observe_ptb_update(test_history.make_business_text_update(
            business_id=connection, chat_id=chat, chat_username="casey"), bot=test_history.FakeBot(owner_id=owner))
    assert cli(plugin, "set", "@casey", "peer", "--owner", "1000") == 0
    assert plugin._transcription_modes.mode_for("1000", "991") == "peer"
    assert plugin._transcription_modes.mode_for("2000", "992") == "both"
    await history.observe_ptb_update(test_history.make_business_text_update(
        business_id="one", chat_id=993, chat_username="casey"), bot=test_history.FakeBot())
    assert cli(plugin, "set", "@casey", "off", "--owner", "1000") == 1
    assert "ambiguous" in capsys.readouterr().out
    assert cli(plugin, "set", "993", "off", "--owner", "1000") == 0
    assert cli(plugin, "get", "@casey", "--owner", "1000", "--connection", "two") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text,mode,contains", [
    ("/transcribe", "both", "Transcription: both"),
    ("/transcribe off", "off", "Transcription: off"),
    ("/transcribe own", "own", "Transcription: own"),
    ("/transcribe peer", "peer", "Transcription: peer"),
    ("/transcribe both", "both", "Transcription: both"),
    ("/transcribe reset", "both", "Transcription: both"),
    ("/transcribe auto", "both", "Usage:"),
    ("/transcribe off extra", "both", "Usage:"),
])
@pytest.mark.parametrize("fallback", [False, True])
async def test_normalized_owner_commands_confirmation_and_echo(plugin, text, mode, contains, fallback):
    identity = plugin._business_identity
    bot = SimpleNamespace(edit_message_text=AsyncMock(side_effect=RuntimeError("edit failure") if fallback else None),
                          send_message=AsyncMock(), get_business_connection=AsyncMock(return_value=SimpleNamespace(user=SimpleNamespace(id=1000))))
    message = test_history.make_business_text_update(text=text, from_user_id=1000).business_message
    await identity.resolve_owner(message, bot=bot)
    event = test_plugin.make_event(message)
    gateway = SimpleNamespace(adapters={event.source.platform: SimpleNamespace(_bot=bot)})
    assert plugin._on_pre_gateway_dispatch(event, gateway)["action"] == "skip"
    await asyncio.gather(*plugin._pending_tasks)
    assert plugin._transcription_modes.mode_for("1000", "991") == mode
    assert contains in bot.edit_message_text.call_args.kwargs["text"]
    assert bot.edit_message_text.call_args.kwargs["business_connection_id"] == "business-123"
    assert bot.send_message.await_count == int(fallback)
    if fallback:
        assert bot.send_message.call_args.kwargs["business_connection_id"] == "business-123"
        assert bot.send_message.call_args.kwargs["reply_to_message_id"] == 77
    # No second execution on either edited echo or duplicate original update.
    message.text = bot.edit_message_text.call_args.kwargs["text"]
    message.edit_date = message.date
    assert plugin._on_pre_gateway_dispatch(event, gateway)["action"] == "skip"
    assert bot.edit_message_text.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sender,marker,excluded", [(1000, False, True), (2000, False, False), (2000, True, False)])
async def test_owner_only_commands_and_all_history_capture(plugin, monkeypatch, sender, marker, excluded):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    history, identity = plugin._history_support, plugin._business_identity
    update = test_history.make_business_text_update(text="/transcribe off", from_user_id=sender,
        sender_business_bot_id=999 if marker else None)
    bot = test_history.FakeBot()
    wrote = await history.observe_ptb_update(update, bot=bot)
    assert wrote == (not excluded)
    event = test_plugin.make_event(update.business_message)
    result = plugin._on_pre_gateway_dispatch(event, SimpleNamespace())
    assert (result is not None) == excluded
    if plugin._pending_tasks:
        await asyncio.gather(*plugin._pending_tasks)
    assert plugin._transcription_modes.mode_for("1000", "991") == ("off" if excluded else "both")


@pytest.mark.asyncio
async def test_confirmation_excluded_after_cache_reset(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    update = test_history.make_business_text_update(text="/transcribe off → Transcription: off", edited=True,
                                                  sender_business_bot_id=999)
    assert not await plugin._history_support.observe_ptb_update(update, bot=test_history.FakeBot())
    assert not list(plugin._transcription_modes.path().parents[1].rglob("*.jsonl"))


@pytest.mark.asyncio
async def test_storage_error_command_claimed_and_status_has_no_exception_content(plugin):
    modes = plugin._transcription_modes
    modes.path().parent.mkdir(parents=True)
    modes.path().write_text("SECRET CORRUPT")
    message = test_history.make_business_text_update(text="/transcribe off", from_user_id=1000).business_message
    status = await modes.command_status(message, owner="1000")
    assert "Unable to" in status and "SECRET" not in status
    assert modes.path().read_text() == "SECRET CORRUPT"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("both", True), ("off", False)])
async def test_both_off_with_stable_owner_do_not_resolve_direction(plugin, monkeypatch, mode, expected):
    modes, identity = plugin._transcription_modes, plugin._business_identity
    modes.set_mode("1000", "991", mode, via="cli")
    message = test_plugin.make_message()
    message.from_user = None
    identity.OWNER_CACHE["business-123"] = "1000"
    monkeypatch.setattr(identity, "direction", Mock(side_effect=AssertionError("direction not needed")))
    assert await modes.should_transcribe(message, bot=None) == expected


@pytest.mark.asyncio
async def test_gateway_policy_reloads_external_atomic_write(plugin):
    modes = plugin._transcription_modes
    message = test_plugin.make_message()
    bot = test_plugin.FakeBot()
    assert await modes.should_transcribe(message, bot=bot)
    data = modes.snapshot()
    data["chats"] = {"1000": {"991": {"mode": "off", "updated_at": "2026-10-18T00:00:00+00:00",
                                    "updated_via": "cli", "label": "Casey"}}}
    replacement = modes.path().with_suffix(".replacement")
    replacement.write_text(json.dumps(data))
    replacement.replace(modes.path())
    assert not await modes.should_transcribe(message, bot=bot)
    assert bot.business_connection_calls == ["business-123"]


@pytest.mark.asyncio
async def test_unknown_owner_never_authorizes_command(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    update = test_history.make_business_text_update(text="/transcribe off", from_user_id=1000)
    bot = test_history.FakeBot()
    bot.lookup_error = RuntimeError("failed")
    assert await plugin._history_support.observe_ptb_update(update, bot=bot)
    event = test_plugin.make_event(update.business_message)
    assert plugin._on_pre_gateway_dispatch(event, SimpleNamespace()) is None
    assert not plugin._transcription_modes.path().exists()


@pytest.mark.asyncio
async def test_false_edit_result_uses_reply_and_reply_error_is_contained(plugin):
    message = test_history.make_business_text_update(text="/transcribe off", from_user_id=1000).business_message
    plugin._business_identity.OWNER_CACHE["business-123"] = "1000"
    bot = SimpleNamespace(edit_message_text=AsyncMock(return_value=False),
                          send_message=AsyncMock(side_effect=RuntimeError("SECRET FAILURE")))
    event = test_plugin.make_event(message)
    gateway = SimpleNamespace(adapters={event.source.platform: SimpleNamespace(_bot=bot)})
    assert plugin._on_pre_gateway_dispatch(event, gateway)["action"] == "skip"
    await asyncio.gather(*plugin._pending_tasks)
    bot.send_message.assert_awaited_once()
    assert plugin._transcription_modes.mode_for("1000", "991") == "off"


def test_cli_corrupt_file_missing_action_invalid_owner_and_private_metadata(plugin, capsys):
    modes = plugin._transcription_modes
    assert cli(plugin) == 1
    assert cli(plugin, "get", "991", "--owner", "unknown") == 1
    assert cli(plugin, "get", "991") == 1
    assert cli(plugin, "set", "991", "off", "--owner", "1000") == 0
    entry = modes.snapshot()["chats"]["1000"]["991"]
    assert set(entry) == {"mode", "updated_at", "updated_via", "label"}
    modes.path().write_text("SECRET BAD FILE")
    assert cli(plugin, "set", "992", "own", "--owner", "1000") == 1
    assert cli(plugin, "get", "991", "--owner", "1000") == 1
    assert "SECRET" not in capsys.readouterr().out
    assert modes.path().read_text() == "SECRET BAD FILE"


def test_unreadable_mode_file_keeps_last_valid_policy(plugin, monkeypatch):
    modes = plugin._transcription_modes
    modes.set_mode("1000", "991", "off", via="cli")
    monkeypatch.setattr(modes, "_signature", Mock(side_effect=PermissionError("SECRET")))
    assert modes.mode_for("1000", "991") == "off"
    modes._CACHE.clear()
    assert modes.mode_for("1000", "991") == "both"


@pytest.fixture
def legacy_history(plugin, monkeypatch):
    """Write pre-0.9 JSONL directly: no owner lookup or mode mappings."""
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    history = plugin._history_support
    def write(connection="old", owner=1000, chat=991, direction="outbound"):
        record = {
            "schema_version": 1, "event_type": "message.created", "source": "business_message",
            "observed_at": "2026-07-19T10:00:00+00:00", "message_at": "2026-07-19T10:00:00+00:00",
            "telegram_update_id": chat, "business_connection_id": connection, "chat_id": chat,
            "message_id": 77, "sender_id": owner, "direction": direction,
            "reply_to_message_id": None, "text": "old history",
            "chat_profile": {"id": chat, "type": "private", "first_name": "Casey", "username": "casey"},
        }
        directory = history._history_chat_dir_path(connection, chat)
        with history._root_writer_lock():
            with history._chat_writer_lock(directory):
                (directory / "2026-07.jsonl").write_text(json.dumps(record) + "\n")
        history.rebuild_contact_catalog()
    write()
    assert not plugin._transcription_modes.path().exists()
    return write


def test_upgrade_cli_explicit_selection_never_verifies_owner(plugin, legacy_history, capsys, monkeypatch):
    modes = plugin._transcription_modes
    # Old-format outbound records can represent a peer sender via business-bot
    # semantics; the marker itself was never retained in the historical record.
    peer_message = test_plugin.make_message(from_user_id=2000)
    peer_message.sender_business_bot = SimpleNamespace(id=999)
    assert plugin._business_identity.direction(peer_message, "1000") == "outbound"
    legacy_history("peer-outbound", 2000, 992)
    monkeypatch.setattr(plugin._history_support, "_load_records", Mock(side_effect=AssertionError("no archive scan")))
    assert cli(plugin, "get", "@casey") == 1
    assert "--owner" in capsys.readouterr().out
    assert cli(plugin, "get", "@casey", "--owner", "1000") == 1
    assert "/transcribe once" in capsys.readouterr().out
    assert cli(plugin, "set", "@casey", "off", "--owner", "1000", "--connection", "peer-outbound") == 0
    assert modes.mode_for("1000", "992") == "off"
    assert modes.mode_for("2000", "992") == "both"
    assert modes.snapshot()["owners"] == {}
    assert modes.snapshot()["chats"]["1000"]["992"]["label"] == "@casey"
    assert plugin._business_identity.cached_owner(test_plugin.make_message(business_id="peer-outbound")) is None
    assert not plugin._business_identity.owner_command(
        test_history.make_business_text_update(business_id="peer-outbound", text="/transcribe off",
                                             from_user_id=1000).business_message, None)


def test_upgrade_cli_preserves_ambiguity_and_requires_existing_connection(plugin, legacy_history, capsys):
    legacy_history("old", 2000, 993)
    assert cli(plugin, "get", "@casey", "--owner", "1000", "--connection", "old") == 1
    assert "ambiguous" in capsys.readouterr().out
    assert cli(plugin, "get", "@casey", "--owner", "1000", "--connection", "missing") == 1
    assert "existing history catalog" in capsys.readouterr().out
    assert cli(plugin, "get", "@casey", "--connection", "old") == 1
    assert not plugin._transcription_modes.path().exists()


@pytest.mark.parametrize("verified", [["2000"], ["1000", "2000"]])
def test_upgrade_cli_rejects_foreign_or_ambiguous_verified_mapping(plugin, legacy_history, capsys, verified):
    for owner in verified:
        plugin._transcription_modes.remember_owner(owner, "old")
    assert cli(plugin, "get", "@casey", "--owner", "1000", "--connection", "old") == 1
    assert "conflicts with verified ownership" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_restart_saved_off_uses_exact_verified_mapping_without_network(plugin):
    modes, identity = plugin._transcription_modes, plugin._business_identity
    modes.remember_owner("1000", "business-123")
    modes.set_mode("1000", "991", "off", via="cli")
    identity.OWNER_CACHE.clear()
    modes._CACHE.clear()
    bot = test_plugin.FakeBot()
    bot.business_connection_error = RuntimeError("network unavailable")
    assert not await modes.should_transcribe(test_plugin.make_message(), bot=bot)
    assert bot.business_connection_calls == []
    assert await modes.should_transcribe(test_plugin.make_message(business_id="new"), bot=bot)
    assert bot.business_connection_calls == ["new"]


@pytest.mark.asyncio
async def test_normalized_cold_command_commits_delayed_write_before_next_voice(plugin, monkeypatch):
    import time
    modes, identity = plugin._transcription_modes, plugin._business_identity
    assert not modes.path().exists()
    identity.OWNER_CACHE.clear()
    modes._CACHE.clear()
    original = modes.set_mode
    def delayed(*args, **kwargs):
        time.sleep(0.05)
        original(*args, **kwargs)
    monkeypatch.setattr(modes, "set_mode", delayed)
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock(),
                          get_business_connection=AsyncMock(return_value=SimpleNamespace(user=SimpleNamespace(id=1000))))
    message = test_history.make_business_text_update(text="/transcribe off", from_user_id=1000).business_message
    event = test_plugin.make_event(message)
    gateway = SimpleNamespace(adapters={event.source.platform: SimpleNamespace(_bot=bot)})
    hooks = {}
    plugin.register(SimpleNamespace(
        llm=None, register_telegram_handler=lambda callback: None,
        register_hook=lambda name, callback: hooks.update({name: callback}),
        register_cli_command=lambda **kwargs: None, register_skill=lambda *args: None))
    callback = hooks["pre_gateway_dispatch"]
    assert asyncio.iscoroutinefunction(callback)
    assert await callback(event, gateway) == {"action": "skip", "reason": "telegram_business_transcription_command"}
    # No event-loop yield between command claim and checking persisted policy.
    assert modes.mode_for("1000", "991") == "off"
    assert not await modes.should_transcribe(test_plugin.make_message(), bot=bot)
    download = AsyncMock()
    monkeypatch.setattr(plugin, "_download_voice", download)
    voice = test_plugin.make_message()
    voice.message_id = 78
    assert (await callback(test_plugin.make_event(voice), gateway))["action"] == "skip"
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    assert not await plugin._history_support.observe_ptb_update(
        test_history.make_business_text_update(text="/transcribe off", from_user_id=1000), bot=bot)
    assert test_history.load_records(plugin) == []
    await asyncio.gather(*plugin._pending_tasks)
    bot.get_business_connection.assert_awaited_once_with("business-123")
    download.assert_not_awaited()


def test_persisted_ambiguous_connection_never_authorizes_owner(plugin):
    plugin._transcription_modes.remember_owner("1000", "business-123")
    plugin._transcription_modes.remember_owner("2000", "business-123")
    assert plugin._business_identity.cached_owner(test_plugin.make_message()) is None


@pytest.mark.asyncio
async def test_registered_async_cold_contact_command_stays_ordinary(plugin, monkeypatch):
    hooks = {}
    plugin.register(SimpleNamespace(
        llm=None, register_telegram_handler=lambda callback: None,
        register_hook=lambda name, callback: hooks.update({name: callback}),
        register_cli_command=lambda **kwargs: None, register_skill=lambda *args: None))
    modes, identity = plugin._transcription_modes, plugin._business_identity
    assert not modes.path().exists()
    assert identity.OWNER_CACHE == {}
    bot = SimpleNamespace(get_business_connection=AsyncMock(
        return_value=SimpleNamespace(user=SimpleNamespace(id=1000))))
    update = test_history.make_business_text_update(text="/transcribe off", from_user_id=2000)
    event = test_plugin.make_event(update.business_message)
    gateway = SimpleNamespace(adapters={event.source.platform: SimpleNamespace(_bot=bot)})
    assert await hooks["pre_gateway_dispatch"](event, gateway) is None
    assert modes.mode_for("1000", "991") == "both"
    bot.get_business_connection.assert_awaited_once_with("business-123")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    assert await plugin._history_support.observe_ptb_update(update, bot=bot)
    assert test_history.load_records(plugin)[0]["text"] == "/transcribe off"


@pytest.mark.asyncio
async def test_registered_async_cold_confirmation_excludes_without_lookup_or_write(plugin, monkeypatch):
    hooks = {}
    plugin.register(SimpleNamespace(
        llm=None, register_telegram_handler=lambda callback: None,
        register_hook=lambda name, callback: hooks.update({name: callback}),
        register_cli_command=lambda **kwargs: None, register_skill=lambda *args: None))
    bot = SimpleNamespace(get_business_connection=AsyncMock(side_effect=AssertionError("echo needs no lookup")))
    update = test_history.make_business_text_update(
        text="/transcribe off → Transcription: off", from_user_id=2000)
    update.business_message.sender_business_bot = SimpleNamespace(id=999)
    event = test_plugin.make_event(update.business_message)
    gateway = SimpleNamespace(adapters={event.source.platform: SimpleNamespace(_bot=bot)})
    assert (await hooks["pre_gateway_dispatch"](event, gateway))["action"] == "skip"
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    assert not await plugin._history_support.observe_ptb_update(update, bot=bot)
    assert test_history.load_records(plugin) == []
    assert not plugin._transcription_modes.path().exists()
    bot.get_business_connection.assert_not_awaited()
