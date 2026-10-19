"""Per-chat transcription modes (issue #7): policy, store, command, history exclusion, CLI."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_history
import test_plugin
from telegram.ext import ApplicationHandlerStop

isolate_plugin_environment = test_plugin.isolate_plugin_environment
plugin = test_plugin.plugin
FakeBot = test_plugin.FakeBot
FakeAdapter = test_plugin.FakeAdapter
BadRequest = test_plugin.BadRequest
TimedOut = test_plugin.TimedOut
NetworkError = test_plugin.NetworkError
make_message = test_plugin.make_message
make_event = test_plugin.make_event
make_audio_file_message = test_plugin.make_audio_file_message

OWNER = 1000
PEER = 2000
CHAT = 991
BCID = "business-123"
USAGE = "Usage: /transcribe [both|own|peer|off|reset]"
LONG_TRANSCRIPT = "x" * 1100


@pytest.fixture(autouse=True)
def isolate_modes_environment(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE",
        "HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS",
        "HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS",
        "HERMES_TELEGRAM_BUSINESS_HISTORY_CHAT_TYPES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TG_BUSINESS_VOICE_CLEANUP_DISABLE", "1")


@pytest.fixture
def modes(plugin):
    plugin._transcription_modes.reset_in_memory_caches()
    plugin._history_support.reset_in_memory_caches()
    yield plugin._transcription_modes
    plugin._transcription_modes.reset_in_memory_caches()
    plugin._history_support.reset_in_memory_caches()


@pytest.fixture
def history_enabled(monkeypatch: pytest.MonkeyPatch, plugin):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW", "10")
    plugin._history_support.reset_in_memory_caches()
    yield plugin
    plugin._history_support.reset_in_memory_caches()


class CommandBot(FakeBot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.text_edit_calls: list[dict] = []
        self.text_edit_error: Exception | None = None
        self.text_edit_result: object = True

    async def edit_message_text(self, **kwargs):
        self.text_edit_calls.append(kwargs)
        if self.text_edit_error is not None:
            raise self.text_edit_error
        return self.text_edit_result


def make_text_update(
    text: str,
    *,
    from_user_id: int | None = OWNER,
    edited: bool = False,
    sender_business_bot: int | None = None,
    update_id: int = 42,
    message_id: int = 77,
    chat_id: int = CHAT,
    chat_username: str | None = "customer991",
):
    update = test_history.make_business_text_update(
        text=text,
        from_user_id=from_user_id,
        edited=edited,
        sender_business_bot_id=sender_business_bot,
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        chat_username=chat_username,
        date=datetime.now(UTC),
    )
    update.effective_message = update.edited_business_message if edited else update.business_message
    return update


def seed(plugin, owner, chat, mode, *, label=None):
    return plugin._transcription_modes.default_store().set_mode(str(owner), str(chat), mode, via="cli", label=label)


def modes_file(plugin) -> Path:
    return plugin._transcription_modes.modes_path()


def gateway_for(event, bot):
    return SimpleNamespace(adapters={event.source.platform: FakeAdapter(bot)})


def registered_business_handler(plugin, adapter):
    handlers = []
    app = SimpleNamespace(add_handler=lambda handler, group: handlers.append((handler, group)))
    plugin._register_telegram_handlers(app, adapter)
    (handler, _group), = handlers
    return handler.callback


def command_adapter(bot, *, authorized: bool = True):
    return SimpleNamespace(
        platform="telegram",
        _bot=bot,
        _notification_kwargs=lambda _message: {"disable_notification": True},
        _is_user_authorized_from_message=Mock(return_value=authorized),
    )


def run_cli(plugin, *argv: str) -> tuple[int, str]:
    parser = argparse.ArgumentParser(prog="hermes telegram-business")
    plugin._setup_cli(parser)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = plugin._handle_cli(parser.parse_args(list(argv)))
    return exit_code, output.getvalue().strip()


# --- Policy table and every mode x direction ------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "direction", "transcribe", "reason"),
    [
        *[("both", direction, True, "mode_both") for direction in ("own", "peer", "unknown", "not_needed")],
        *[("off", direction, False, "mode_off") for direction in ("own", "peer", "unknown", "not_needed")],
        ("own", "own", True, "own_message"),
        ("own", "peer", False, "peer_message_skipped"),
        ("own", "unknown", True, "direction_unknown"),
        ("peer", "own", False, "own_message_skipped"),
        ("peer", "peer", True, "peer_message"),
        ("peer", "unknown", True, "direction_unknown"),
    ],
)
def test_decide_policy_table(modes, mode, direction, transcribe, reason):
    decision = modes.decide(mode, direction)

    assert decision.transcribe is transcribe
    assert decision.reason == reason
    assert (decision.mode, decision.direction) == (mode, direction)


def _media_message(plugin, monkeypatch, media: str, *, from_user_id: int, sender_business_bot: bool):
    tracker = {"extract": 0, "duration": 0}
    if media == "audio":
        message = make_audio_file_message()
        message.from_user = SimpleNamespace(id=from_user_id)

        def duration(_path):
            tracker["duration"] += 1
            return 5.0

        def extract(_source, destination, _seconds):
            tracker["extract"] += 1
            destination.write_bytes(b"probe")
            return True

        monkeypatch.setattr(plugin, "_local_audio_duration", duration)
        monkeypatch.setattr(plugin, "_extract_audio_probe", extract)
    else:
        message = make_message(media_kind=media, from_user_id=from_user_id)
    if sender_business_bot:
        message.sender_business_bot = SimpleNamespace(id=555)
    return message, getattr(message, media), tracker


DIRECTIONS = {
    "own_from_owner": (OWNER, False, "own"),
    "own_sender_business_bot": (PEER, True, "own"),
    "peer": (PEER, False, "peer"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("media", ["voice", "video_note", "audio"])
@pytest.mark.parametrize("direction_case", list(DIRECTIONS))
@pytest.mark.parametrize("mode", ["both", "own", "peer", "off"])
async def test_every_mode_and_direction_for_every_media(plugin, modes, monkeypatch, caplog, mode, direction_case, media):
    caplog.set_level(logging.DEBUG)
    from_user_id, via_bot, direction = DIRECTIONS[direction_case]
    seed(plugin, OWNER, CHAT, mode)
    message, payload, tracker = _media_message(
        plugin, monkeypatch, media, from_user_id=from_user_id, sender_business_bot=via_bot
    )
    event = make_event(message)
    bot = FakeBot(business_owner_id=OWNER)
    stt_calls = []

    def transcribe(path):
        stt_calls.append(path)
        return {"success": True, "transcript": "hello there friend"}

    await plugin._process_business_voice_event(event=event, gateway=gateway_for(event, bot), transcribe_fn=transcribe)

    expected = modes.decide(mode, direction).transcribe
    # A stored `both` takes the no-lookup fast path; every other mode logs its table reason.
    reason = "default_mode" if mode == "both" else modes.decide(mode, direction).reason
    assert f"decision={'transcribe' if expected else 'skip'} reason={reason}" in caplog.text
    assert "hello there friend" not in caplog.text
    if expected:
        assert stt_calls
        assert payload.get_file_calls == 1
        assert bot.calls or bot.edit_calls
    else:
        assert stt_calls == []
        assert payload.get_file_calls == 0
        assert payload.file.download_calls == 0
        assert bot.calls == []
        assert bot.edit_calls == []
        assert tracker == {"extract": 0, "duration": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("store_state", ["missing", "empty_file", "only_both", "other_chats"])
async def test_default_fast_path_makes_no_owner_lookup(plugin, modes, store_state):
    if store_state == "empty_file":
        modes_file(plugin).parent.mkdir(parents=True, exist_ok=True)
        modes_file(plugin).write_text("", encoding="utf-8")
    elif store_state == "only_both":
        seed(plugin, OWNER, CHAT, "both")
    elif store_state == "other_chats":
        seed(plugin, OWNER, 992, "off")
        seed(plugin, OWNER, -100123, "peer")
    message = make_message(from_user_id=PEER)
    event = make_event(message)
    bot = FakeBot(business_owner_id=OWNER)

    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": LONG_TRANSCRIPT},
    )

    assert bot.business_connection_calls == []
    assert bot.calls[0]["text"] == f"🎙️ {LONG_TRANSCRIPT}"


@pytest.mark.asyncio
async def test_entry_for_same_chat_under_another_owner_uses_default_for_this_owner(plugin, modes):
    seed(plugin, 3000, CHAT, "off")
    message = make_message(from_user_id=PEER)
    event = make_event(message)
    bot = FakeBot(business_owner_id=OWNER)

    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": LONG_TRANSCRIPT},
    )

    assert bot.business_connection_calls == [BCID]
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_edited_media_under_off_is_still_edit_ignored(plugin, modes):
    seed(plugin, OWNER, CHAT, "off")
    message = make_message(from_user_id=PEER)
    message.edit_date = datetime.now(UTC)
    bot = FakeBot()
    event = make_event(message)

    result = plugin._on_pre_gateway_dispatch(event, gateway_for(event, bot))

    assert result == {"action": "skip", "reason": "telegram_business_voice_media_edit_ignored"}
    assert not plugin._pending_tasks
    assert message.voice.get_file_calls == 0
    assert bot.business_connection_calls == []


# --- Unknown direction and the shared owner/direction helper ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["own", "peer"])
async def test_unknown_direction_under_own_or_peer_transcribes_and_logs_reason(plugin, modes, caplog, mode):
    caplog.set_level(logging.INFO)
    seed(plugin, OWNER, CHAT, mode)
    plugin._history_support._OWNER_CACHE[BCID] = str(OWNER)
    message = make_message(from_user_id=PEER, caption="Private caption words")
    message.from_user = None
    event = make_event(message)
    bot = FakeBot(business_owner_id=OWNER)

    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": "Secret spoken words"},
    )

    assert bot.calls or bot.edit_calls
    assert "direction_unknown:missing_sender" in caplog.text
    assert "decision=transcribe" in caplog.text
    assert "Secret spoken words" not in caplog.text
    assert "Private caption words" not in caplog.text
    assert bot.business_connection_calls == []


async def _voice_event(plugin, bot, *, message_id=77, bcid=BCID):
    message = make_message(from_user_id=PEER)
    message.message_id = message_id
    message.business_connection_id = bcid
    event = make_event(message)
    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": LONG_TRANSCRIPT},
    )
    return message


@pytest.mark.asyncio
async def test_owner_lookup_failure_keeps_bound_owner_off_and_retries_next_message(plugin, modes, caplog):
    caplog.set_level(logging.INFO)
    seed(plugin, OWNER, CHAT, "off")
    bot = FakeBot(business_owner_id=OWNER)

    # A successful lookup binds the connection to its owner in the modes file.
    assert (await _voice_event(plugin, bot, message_id=76)).voice.get_file_calls == 0
    assert modes.default_store().connection_owner(BCID) == "1000"
    assert json.loads(modes_file(plugin).read_text(encoding="utf-8"))["connections"] == {BCID: "1000"}

    # After a restart the owner cache is empty and the live lookup fails.
    plugin._history_support.reset_in_memory_caches()
    modes.reset_in_memory_caches()
    bot.business_connection_calls.clear()
    bot.business_connection_error = RuntimeError("token=secret-detail")
    for message_id in (77, 78):
        assert (await _voice_event(plugin, bot, message_id=message_id)).voice.get_file_calls == 0

    assert bot.business_connection_calls == [BCID, BCID]
    assert bot.calls == []
    assert bot.edit_calls == []
    assert "decision=skip reason=mode_off:owner_unknown:lookup_failed:RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text


@pytest.mark.asyncio
async def test_owner_command_binds_its_connection_for_lookup_failures(plugin, modes):
    command_bot = CommandBot(business_owner_id=OWNER)
    assert await _run_command(plugin, make_text_update("/transcribe off"), command_bot) is True
    assert modes.default_store().connection_owner(BCID) == "1000"

    plugin._history_support.reset_in_memory_caches()
    bot = FakeBot(business_owner_id=OWNER)
    bot.business_connection_error = RuntimeError("lookup down")
    message = await _voice_event(plugin, bot)

    assert message.voice.get_file_calls == 0
    assert bot.calls == []


@pytest.mark.asyncio
async def test_owner_lookup_failure_never_applies_another_owners_entry(plugin, modes, caplog):
    caplog.set_level(logging.INFO)
    # Owner A (1000) turned transcription off for contact CHAT over connection
    # "business-a". Owner B (3000) shares the contact's chat id, never set a
    # mode, and its connection BCID has never been resolved.
    seed(plugin, OWNER, CHAT, "off")
    modes.default_store().bind_connection("business-a", OWNER)
    bot = FakeBot(business_owner_id=3000)
    bot.business_connection_error = RuntimeError("token=secret-detail")

    await _voice_event(plugin, bot)

    assert len(bot.calls) == 1
    assert "decision=transcribe reason=owner_unknown:lookup_failed:RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text


@pytest.mark.asyncio
async def test_owner_lookup_failure_for_a_bound_owner_without_entry_uses_both(plugin, modes, caplog):
    caplog.set_level(logging.INFO)
    seed(plugin, OWNER, CHAT, "off")
    store = modes.default_store()
    store.bind_connection("business-a", OWNER)
    store.bind_connection(BCID, 3000)
    bot = FakeBot(business_owner_id=3000)
    bot.business_connection_error = RuntimeError("lookup down")

    await _voice_event(plugin, bot)

    assert len(bot.calls) == 1
    assert "decision=transcribe reason=mode_both:owner_unknown:lookup_failed:RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_owner_lookup_failure_without_binding_fails_open(plugin, modes, caplog):
    caplog.set_level(logging.INFO)
    seed(plugin, OWNER, CHAT, "off")
    bot = FakeBot(business_owner_id=OWNER)
    bot.business_connection_error = RuntimeError("lookup down")

    await _voice_event(plugin, bot)

    assert len(bot.calls) == 1
    assert "decision=transcribe reason=owner_unknown:lookup_failed:RuntimeError" in caplog.text
    assert "connections" not in json.loads(modes_file(plugin).read_text(encoding="utf-8"))


def test_connection_bindings_round_trip_and_invalid_ones_are_skipped(plugin, modes, caplog):
    store = modes.default_store()
    store.set_mode(OWNER, CHAT, "off", via="chat", connection=BCID)
    path = modes_file(plugin)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["connections"] == {BCID: "1000"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    payload["connections"].update({"bad id": "1000", "business-x": "alice"})
    path.write_text(json.dumps(payload), encoding="utf-8")
    modes.reset_in_memory_caches()
    assert store.connection_owner(BCID) == "1000"
    assert store.connection_owner("bad id") is None
    assert store.connection_owner("business-x") is None
    assert store.get(OWNER, CHAT).mode == "off"

    payload["connections"] = ["not", "a", "map"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    modes.reset_in_memory_caches()
    assert store.connection_owner(BCID) is None
    assert store.get(OWNER, CHAT).mode == "off"

    with pytest.raises(modes.ModeStoreError):
        store.bind_connection("bad id", OWNER)
    store.reset(OWNER, CHAT, connection="business-y")
    assert store.connection_owner("business-y") == "1000"


def _direction_message(*, bcid=BCID, from_user_id=OWNER, sender_business_bot=False):
    return SimpleNamespace(
        business_connection_id=bcid,
        from_user=None if from_user_id is None else SimpleNamespace(id=from_user_id),
        sender_business_bot=SimpleNamespace(id=555) if sender_business_bot else None,
        api_kwargs={},
    )


class OwnerlessBot:
    def __init__(self):
        self.calls = 0

    async def get_business_connection(self, _bcid):
        self.calls += 1
        return SimpleNamespace(user=None)


@pytest.mark.asyncio
async def test_shared_direction_helper_covers_every_reason(plugin, modes):
    history = plugin._history_support
    resolve = history.resolve_business_direction

    bot = FakeBot(business_owner_id=OWNER)
    via_bot = await resolve(_direction_message(from_user_id=PEER, sender_business_bot=True), bot=bot)
    assert (via_bot.direction, via_bot.owner_user_id, via_bot.reason) == ("own", None, "sender_business_bot")
    assert bot.business_connection_calls == []

    match = await resolve(_direction_message(from_user_id=OWNER), bot=bot)
    assert (match.direction, match.owner_user_id, match.reason) == ("own", "1000", "owner_match")
    mismatch = await resolve(_direction_message(from_user_id=PEER), bot=bot)
    assert (mismatch.direction, mismatch.owner_user_id, mismatch.reason) == ("peer", "1000", "owner_mismatch")
    assert bot.business_connection_calls == [BCID]

    via_bot_cached = await resolve(_direction_message(from_user_id=PEER, sender_business_bot=True), bot=bot)
    assert via_bot_cached.owner_user_id == "1000"

    assert (await resolve(_direction_message(bcid=None), bot=bot)).reason == "missing_connection"
    assert (await resolve(_direction_message(from_user_id=None), bot=bot)).reason == "missing_sender"
    no_bot = await resolve(_direction_message(), bot=None)
    assert (no_bot.direction, no_bot.reason) == ("unknown", "no_bot")

    unavailable = await resolve(_direction_message(bcid="other"), bot=object())
    assert (unavailable.direction, unavailable.reason) == ("unknown", "lookup_unavailable")

    failing = FakeBot()
    failing.business_connection_error = RuntimeError("secret detail")
    failed = await resolve(_direction_message(bcid="failing"), bot=failing)
    assert (failed.direction, failed.reason) == ("unknown", "lookup_failed:RuntimeError")
    await resolve(_direction_message(bcid="failing"), bot=failing)
    assert failing.business_connection_calls == ["failing", "failing"]

    ownerless = OwnerlessBot()
    missing = await resolve(_direction_message(bcid="ownerless"), bot=ownerless)
    assert (missing.direction, missing.reason) == ("unknown", "owner_missing")
    await resolve(_direction_message(bcid="ownerless"), bot=ownerless)
    assert ownerless.calls == 2


@pytest.mark.asyncio
async def test_direction_helper_falls_back_to_the_stored_binding(plugin, modes):
    resolve = plugin._history_support.resolve_business_direction
    failing = FakeBot()
    failing.business_connection_error = RuntimeError("lookup down")
    stored = {BCID: "1000"}.get

    own = await resolve(_direction_message(from_user_id=OWNER), bot=failing, stored_owner=stored)
    assert (own.direction, own.owner_user_id, own.reason) == ("own", "1000", "stored_owner_match")
    peer = await resolve(_direction_message(from_user_id=PEER), bot=failing, stored_owner=stored)
    assert (peer.direction, peer.owner_user_id, peer.reason) == ("peer", "1000", "stored_owner_mismatch")
    unbound = await resolve(_direction_message(bcid="other"), bot=failing, stored_owner=stored)
    assert (unbound.direction, unbound.reason) == ("unknown", "lookup_failed:RuntimeError")
    via_bot = await resolve(
        _direction_message(from_user_id=PEER, sender_business_bot=True), bot=failing, stored_owner=stored
    )
    assert (via_bot.direction, via_bot.owner_user_id, via_bot.reason) == ("own", None, "sender_business_bot")

    # A live lookup wins over a stale binding.
    live = await resolve(_direction_message(from_user_id=OWNER), bot=FakeBot(business_owner_id=OWNER), stored_owner={BCID: "3000"}.get)
    assert (live.direction, live.owner_user_id, live.reason) == ("own", "1000", "owner_match")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "sender", "transcribed"),
    [("own", PEER, False), ("own", OWNER, True), ("peer", OWNER, False), ("peer", PEER, True)],
)
async def test_stored_binding_gives_direction_when_the_lookup_fails(plugin, modes, caplog, mode, sender, transcribed):
    caplog.set_level(logging.INFO)
    seed(plugin, OWNER, CHAT, mode)
    modes.default_store().bind_connection(BCID, OWNER)
    bot = FakeBot(business_owner_id=OWNER)
    bot.business_connection_error = RuntimeError("lookup down")
    message = make_message(from_user_id=sender)
    event = make_event(message)

    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": LONG_TRANSCRIPT},
    )

    assert message.voice.get_file_calls == int(transcribed)
    direction = "own" if sender == OWNER else "peer"
    assert f"direction={direction} decision={'transcribe' if transcribed else 'skip'}" in caplog.text
    assert "direction_unknown" not in caplog.text


@pytest.mark.asyncio
async def test_direction_adapters_keep_their_contracts(plugin, modes):
    history = plugin._history_support
    bot = FakeBot(business_owner_id=OWNER)

    assert await history._resolve_direction(_direction_message(from_user_id=OWNER), bot=bot) == "outbound"
    assert await history._resolve_direction(_direction_message(from_user_id=PEER), bot=bot) == "inbound"
    assert await history._resolve_direction(_direction_message(from_user_id=None), bot=bot) == "unknown"
    assert await plugin._is_outgoing_business_message(bot=bot, message=_direction_message(from_user_id=OWNER))
    assert not await plugin._is_outgoing_business_message(bot=bot, message=_direction_message(from_user_id=PEER))
    assert not await plugin._is_outgoing_business_message(bot=bot, message=_direction_message(from_user_id=None))
    assert bot.business_connection_calls == [BCID]


# --- Policy before download -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_path_claims_skipped_note_without_download(plugin, modes):
    seed(plugin, OWNER, CHAT, "off")
    message = make_message(from_user_id=PEER)
    event = make_event(message)
    bot = FakeBot(business_owner_id=OWNER)
    gateway = gateway_for(event, bot)

    result = plugin._on_pre_gateway_dispatch(event, gateway)
    if plugin._pending_tasks:
        await asyncio.gather(*plugin._pending_tasks)

    assert result == {"action": "skip", "reason": "telegram_business_voice_media_transcribed"}
    assert message.voice.get_file_calls == 0
    assert bot.calls == []
    assert bot.edit_calls == []
    assert plugin._on_pre_gateway_dispatch(event, gateway) == {
        "action": "skip",
        "reason": "telegram_business_voice_media_duplicate",
    }


@pytest.mark.asyncio
async def test_policy_error_fails_open(plugin, modes, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    seed(plugin, OWNER, CHAT, "off")

    def broken(_chat_id):
        raise RuntimeError("broken store")

    monkeypatch.setattr(modes.default_store(), "needs_owner_lookup", broken)
    message = make_message(from_user_id=PEER)
    event = make_event(message)
    bot = FakeBot(business_owner_id=OWNER)

    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": LONG_TRANSCRIPT},
    )

    assert len(bot.calls) == 1
    assert "reason=policy_error:RuntimeError" in caplog.text


# --- In-chat /transcribe command ---------------------------------------------------------------


async def _run_command(plugin, update, bot):
    return await plugin._handle_transcribe_command_update(update, bot=bot, adapter=FakeAdapter(bot))


@pytest.mark.asyncio
async def test_owner_command_sets_mode_and_edits_command_into_status(plugin, modes):
    bot = CommandBot(business_owner_id=OWNER)

    assert await _run_command(plugin, make_text_update("/transcribe off"), bot) is True

    entry = modes.default_store().get(OWNER, CHAT)
    assert (entry.mode, entry.updated_via, entry.label) == ("off", "chat", "Casey Customer (@customer991)")
    assert bot.text_edit_calls == [
        {
            "text": "/transcribe off → Transcription: off",
            "chat_id": CHAT,
            "message_id": 77,
            "business_connection_id": BCID,
        }
    ]
    assert bot.calls == []
    stored = json.loads(modes_file(plugin).read_text(encoding="utf-8"))
    assert "/transcribe" not in json.dumps(stored)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seeded", "text", "status", "stored"),
    [
        (None, "/transcribe", "/transcribe → Transcription: both (default)", None),
        ("own", "/transcribe", "/transcribe → Transcription: own", "own"),
        ("off", "/transcribe reset", "/transcribe reset → Transcription: both (default)", None),
        (None, "/transcribe own", "/transcribe own → Transcription: own", "own"),
        (None, "/transcribe peer", "/transcribe peer → Transcription: peer", "peer"),
        ("off", "/transcribe both", "/transcribe both → Transcription: both", "both"),
        (None, "/transcribe OFF", "/transcribe off → Transcription: off", "off"),
        (None, "/transcribe@SomeBot peer", "/transcribe peer → Transcription: peer", "peer"),
        (None, "/transcribe banana", f"/transcribe banana → {USAGE}", None),
        ("own", "/transcribe off now", f"/transcribe off now → {USAGE}", "own"),
    ],
)
async def test_command_grammar(plugin, modes, seeded, text, status, stored):
    if seeded is not None:
        seed(plugin, OWNER, CHAT, seeded)
    file_existed = modes_file(plugin).exists()
    bot = CommandBot(business_owner_id=OWNER)

    assert await _run_command(plugin, make_text_update(text), bot) is True

    assert [call["text"] for call in bot.text_edit_calls] == [status]
    entry = modes.default_store().get(OWNER, CHAT)
    assert (entry.mode if entry else None) == stored
    if not file_existed and stored is None:
        assert not modes_file(plugin).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/transcribeoff", "/transcribers", "transcribe off", "hello /transcribe off"])
async def test_non_command_text_is_not_claimed(plugin, modes, text):
    bot = CommandBot(business_owner_id=OWNER)

    assert await _run_command(plugin, make_text_update(text), bot) is False
    assert bot.business_connection_calls == []
    assert bot.text_edit_calls == []


def test_parse_command_and_status_texts(modes):
    assert modes.parse_command("/transcribe") == modes.TranscribeCommand("show", None, "/transcribe")
    assert modes.parse_command("/TRANSCRIBE   Peer ") == modes.TranscribeCommand("set", "peer", "/transcribe peer")
    assert modes.parse_command("/transcribe reset").action == "reset"
    assert modes.parse_command("/transcribeoff") is None
    assert modes.parse_command(None) is None
    long_usage = modes.parse_command("/transcribe " + "y" * 80)
    assert long_usage.display == "/transcribe " + "y" * 32
    assert modes.status_text(modes.parse_command("/transcribe off"), entry=None, error=True) == (
        "/transcribe off → Error: could not save transcription mode"
    )
    assert modes.build_chat_label(SimpleNamespace(title="Team\x07 Chat")) == "Team Chat"
    assert modes.build_chat_label(SimpleNamespace(first_name="Ann", username="ann")) == "Ann (@ann)"
    assert modes.build_chat_label(SimpleNamespace(username="ann")) == "@ann"
    assert modes.build_chat_label(SimpleNamespace(first_name="A" * 300)) == "A" * 128
    assert modes.build_chat_label(SimpleNamespace()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("echo", ["edited_owner", "sender_business_bot"])
async def test_status_echoes_are_claimed_but_not_executed(plugin, modes, echo):
    bot = CommandBot(business_owner_id=OWNER)
    status = "/transcribe off → Transcription: off"
    if echo == "edited_owner":
        update = make_text_update(status, edited=True)
    else:
        update = make_text_update(status, from_user_id=OWNER, sender_business_bot=555)

    assert await _run_command(plugin, update, bot) is True

    assert not modes_file(plugin).exists()
    assert bot.text_edit_calls == []
    assert bot.calls == []


def test_every_status_text_matches_the_echo_predicate(modes):
    history = modes._history
    commands = [
        modes.parse_command(text)
        for text in ("/transcribe", "/transcribe off", "/transcribe reset", "/transcribe banana split", "/transcribe " + "y" * 80)
    ]
    entry = modes.ModeEntry(mode="own", updated_at="2026-10-19T12:00:00Z", updated_via="chat", label=None)
    for command in commands:
        for kwargs in ({"entry": None}, {"entry": entry}, {"entry": None, "error": True}):
            assert history.is_transcribe_status_text(modes.status_text(command, **kwargs))
    for text in ("/transcribe off", "/transcribe lets the owner choose", "/transcribe off → Transcription: loud"):
        assert not history.is_transcribe_status_text(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/transcribe lets the owner choose who gets transcribed", "/transcribe peer"])
async def test_bot_sent_non_status_transcribe_text_is_ordinary_outbound_history(history_enabled, modes, text):
    plugin = history_enabled
    bot = CommandBot(business_owner_id=OWNER)
    callback = registered_business_handler(plugin, command_adapter(bot))

    await callback(make_text_update(text, sender_business_bot=555), SimpleNamespace(bot=bot))

    records = test_history.load_records(plugin)
    assert [(record["direction"], record["text"]) for record in records] == [("outbound", text)]
    assert not modes_file(plugin).exists()
    assert bot.text_edit_calls == []
    assert bot.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["forward_origin", "via_bot"])
async def test_forwarded_or_inline_bot_owner_command_is_ordinary_text(history_enabled, modes, origin):
    plugin = history_enabled
    bot = CommandBot(business_owner_id=OWNER)
    callback = registered_business_handler(plugin, command_adapter(bot))
    update = make_text_update("/transcribe off")
    setattr(update.business_message, origin, SimpleNamespace(type="user", id=3000))

    assert await _run_command(plugin, update, bot) is False
    await callback(update, SimpleNamespace(bot=bot))

    assert not modes_file(plugin).exists()
    assert bot.text_edit_calls == []
    assert bot.calls == []
    records = test_history.load_records(plugin)
    assert [(record["direction"], record["text"]) for record in records] == [("outbound", "/transcribe off")]


@pytest.mark.asyncio
async def test_owner_command_does_not_need_dm_allowlist(plugin, modes):
    bot = CommandBot(business_owner_id=OWNER)
    adapter = command_adapter(bot, authorized=False)
    callback = registered_business_handler(plugin, adapter)

    with pytest.raises(ApplicationHandlerStop):
        await callback(make_text_update("/transcribe peer"), SimpleNamespace(bot=bot))

    assert modes.default_store().get(OWNER, CHAT).mode == "peer"
    adapter._is_user_authorized_from_message.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_command_delivery_writes_and_confirms_once(plugin, modes, monkeypatch):
    store = modes.default_store()
    writes = []
    original = store.set_mode

    def counting(*args, **kwargs):
        writes.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "set_mode", counting)
    bot = CommandBot(business_owner_id=OWNER)
    update = make_text_update("/transcribe off")

    assert await _run_command(plugin, update, bot) is True
    assert await _run_command(plugin, update, bot) is True

    assert len(writes) == 1
    assert len(bot.text_edit_calls) == 1


@pytest.mark.asyncio
async def test_store_failure_reports_error_status_and_still_claims(plugin, modes, monkeypatch, caplog):
    def failing(*_args, **_kwargs):
        raise OSError("disk full at /secret/path")

    monkeypatch.setattr(modes.default_store(), "set_mode", failing)
    bot = CommandBot(business_owner_id=OWNER)

    assert await _run_command(plugin, make_text_update("/transcribe off"), bot) is True

    assert [call["text"] for call in bot.text_edit_calls] == [
        "/transcribe off → Error: could not save transcription mode"
    ]
    assert "OSError" in caplog.text
    assert "/secret/path" not in caplog.text


# --- A contact's /transcribe is ordinary text -------------------------------------------------


@pytest.mark.asyncio
async def test_contact_command_is_not_claimed(plugin, modes):
    bot = CommandBot(business_owner_id=OWNER)

    assert await _run_command(plugin, make_text_update("/transcribe off", from_user_id=PEER), bot) is False

    assert not modes_file(plugin).exists()
    assert bot.text_edit_calls == []
    assert bot.calls == []


@pytest.mark.asyncio
async def test_contact_command_reaches_history_as_inbound_text(history_enabled, modes):
    plugin = history_enabled
    bot = CommandBot(business_owner_id=OWNER)
    callback = registered_business_handler(plugin, command_adapter(bot))

    await callback(make_text_update("/transcribe off", from_user_id=PEER), SimpleNamespace(bot=bot))

    records = test_history.load_records(plugin)
    assert [(record["direction"], record["text"]) for record in records] == [("inbound", "/transcribe off")]
    assert not modes_file(plugin).exists()


@pytest.mark.asyncio
async def test_unverifiable_owner_command_is_ordinary_unknown_text(history_enabled, modes):
    plugin = history_enabled
    bot = CommandBot(business_owner_id=OWNER)
    bot.business_connection_error = RuntimeError("lookup down")
    callback = registered_business_handler(plugin, command_adapter(bot))

    await callback(make_text_update("/transcribe off"), SimpleNamespace(bot=bot))

    records = test_history.load_records(plugin)
    assert [(record["direction"], record["text"]) for record in records] == [("unknown", "/transcribe off")]
    assert not modes_file(plugin).exists()
    assert bot.text_edit_calls == []


@pytest.mark.asyncio
async def test_owner_command_uses_the_stored_binding_when_the_lookup_fails(plugin, modes):
    store = modes.default_store()
    store.bind_connection(BCID, OWNER)
    bot = CommandBot(business_owner_id=OWNER)
    bot.business_connection_error = RuntimeError("lookup down")

    assert await _run_command(plugin, make_text_update("/transcribe off"), bot) is True

    assert store.get(OWNER, CHAT).mode == "off"
    assert [call["text"] for call in bot.text_edit_calls] == ["/transcribe off → Transcription: off"]

    # The binding never makes a contact the owner, nor lets the Business bot execute a command.
    contact = make_text_update("/transcribe both", from_user_id=PEER, message_id=78)
    assert await _run_command(plugin, contact, bot) is False
    for message_id, text in ((79, "/transcribe both"), (80, "/transcribe both → Transcription: both")):
        echo = make_text_update(text, from_user_id=OWNER, sender_business_bot=555, message_id=message_id)
        await _run_command(plugin, echo, bot)
    assert store.get(OWNER, CHAT).mode == "off"
    assert len(bot.text_edit_calls) == 1


# --- Edit confirmation and reply fallback -----------------------------------------------------


EXPECTED_REPLY = {
    "chat_id": CHAT,
    "text": "/transcribe off → Transcription: off",
    "business_connection_id": BCID,
    "disable_notification": True,
    "reply_to_message_id": 77,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cant_edit", "false_result", "timed_out", "network", "no_edit_method"])
async def test_failed_edit_falls_back_to_one_business_reply(plugin, modes, failure):
    bot = FakeBot(business_owner_id=OWNER) if failure == "no_edit_method" else CommandBot(business_owner_id=OWNER)
    if failure == "cant_edit":
        bot.text_edit_error = BadRequest("Bad Request: message can't be edited")
    elif failure == "false_result":
        bot.text_edit_result = False
    elif failure == "timed_out":
        bot.text_edit_error = TimedOut("timed out")
    elif failure == "network":
        bot.text_edit_error = NetworkError("connection reset")

    assert await _run_command(plugin, make_text_update("/transcribe off"), bot) is True

    assert bot.calls == [EXPECTED_REPLY]
    assert modes.default_store().get(OWNER, CHAT).mode == "off"


@pytest.mark.asyncio
async def test_message_not_modified_counts_as_applied(plugin, modes):
    bot = CommandBot(business_owner_id=OWNER)
    bot.text_edit_error = BadRequest("Bad Request: message is not modified")

    assert await _run_command(plugin, make_text_update("/transcribe off"), bot) is True

    assert len(bot.text_edit_calls) == 1
    assert bot.calls == []


@pytest.mark.asyncio
async def test_edit_and_reply_failures_are_contained(plugin, modes, caplog):
    bot = CommandBot(business_owner_id=OWNER)
    bot.text_edit_error = BadRequest("message can't be edited")
    bot.send_error = RuntimeError("send failed with private detail")

    assert await _run_command(plugin, make_text_update("/transcribe off"), bot) is True

    assert modes.default_store().get(OWNER, CHAT).mode == "off"
    assert "confirmation failed (RuntimeError)" in caplog.text
    assert "private detail" not in caplog.text


@pytest.mark.asyncio
async def test_disabled_transcriber_leaves_owner_command_as_ordinary_history(history_enabled, modes, monkeypatch):
    plugin = history_enabled
    monkeypatch.setenv("TG_BUSINESS_VOICE_TRANSCRIBER_DISABLE", "1")
    bot = CommandBot(business_owner_id=OWNER)
    callback = registered_business_handler(plugin, command_adapter(bot))
    update = make_text_update("/transcribe off")

    assert await _run_command(plugin, update, bot) is False
    await callback(update, SimpleNamespace(bot=bot))

    assert not modes_file(plugin).exists()
    assert bot.text_edit_calls == []
    assert bot.calls == []
    records = test_history.load_records(plugin)
    assert [(record["direction"], record["text"]) for record in records] == [("outbound", "/transcribe off")]


# --- History exclusion ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_command_and_its_echo_never_reach_history(history_enabled, modes):
    plugin = history_enabled
    bot = CommandBot(business_owner_id=OWNER)
    callback = registered_business_handler(plugin, command_adapter(bot))
    context = SimpleNamespace(bot=bot)

    with pytest.raises(ApplicationHandlerStop):
        await callback(make_text_update("/transcribe off", update_id=1), context)
    with pytest.raises(ApplicationHandlerStop):
        await callback(make_text_update(bot.text_edit_calls[0]["text"], edited=True, update_id=2), context)

    assert test_history.load_records(plugin) == []
    assert not (plugin._history_support.history_root() / "contacts.json").exists()

    await callback(make_text_update("Hello", message_id=78, update_id=3), context)
    records = test_history.load_records(plugin)
    assert [(record["direction"], record["text"]) for record in records] == [("outbound", "Hello")]


@pytest.mark.asyncio
async def test_history_filter_is_a_second_safeguard(history_enabled, modes):
    plugin = history_enabled
    history = plugin._history_support
    bot = FakeBot(business_owner_id=OWNER)

    assert not await history.observe_ptb_update(make_text_update("/transcribe off", update_id=1), bot=bot)
    assert not await history.observe_ptb_update(
        make_text_update("/transcribe off → Transcription: off", edited=True, update_id=2), bot=bot
    )
    assert not await history.observe_ptb_update(
        make_text_update(
            "/transcribe peer → Transcription: peer", sender_business_bot=555, message_id=79, update_id=3
        ),
        bot=bot,
    )
    assert test_history.load_records(plugin) == []

    assert await history.observe_ptb_update(
        make_text_update("/transcribe off", from_user_id=PEER, message_id=80, update_id=4), bot=bot
    )
    assert await history.observe_ptb_update(make_text_update("Hello", message_id=81, update_id=5), bot=bot)
    records = test_history.load_records(plugin)
    assert [(record["message_id"], record["direction"], record["text"]) for record in records] == [
        (80, "inbound", "/transcribe off"),
        (81, "outbound", "Hello"),
    ]
    assert records[1]["sender_id"] == OWNER
    assert records[1]["event_type"] == "message.created"


# --- Reload without restart -------------------------------------------------------------------


def test_gateway_store_sees_another_process_write_without_reset(plugin, modes):
    gateway_store = modes.ModeStore()
    cli_store = modes.ModeStore()
    cli_store.set_mode(OWNER, CHAT, "off", via="cli")
    assert gateway_store.get(OWNER, CHAT).mode == "off"

    cli_store.set_mode(OWNER, CHAT, "own", via="cli")

    assert gateway_store.get(OWNER, CHAT).mode == "own"


def test_external_replace_with_identical_mtime_and_size_is_detected(plugin, modes):
    store = modes.ModeStore()
    store.set_mode(OWNER, CHAT, "off", via="cli", now=datetime(2026, 10, 19, 12, 0, tzinfo=UTC))
    path = modes_file(plugin)
    original = path.stat()
    assert store.get(OWNER, CHAT).mode == "off"

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["owners"]["1000"]["chats"]["991"]["mode"] = "own"
    tmp = path.with_name("external.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    assert path.stat().st_size == original.st_size

    assert store.get(OWNER, CHAT).mode == "own"


def _rewrite_in_place(path: Path, mode: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["owners"]["1000"]["chats"]["991"]["mode"] = mode
    with path.open("r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.truncate()


def test_in_place_edit_with_same_inode_and_size_is_reloaded_on_mtime_change(plugin, modes):
    store = modes.ModeStore()
    store.set_mode(OWNER, CHAT, "off", via="cli")
    path = modes_file(plugin)
    original = path.stat()
    assert store.get(OWNER, CHAT).mode == "off"

    _rewrite_in_place(path, "own")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000))
    assert path.stat().st_ino == original.st_ino
    assert path.stat().st_size == original.st_size

    assert store.get(OWNER, CHAT).mode == "own"


def test_in_place_edit_with_same_inode_and_mtime_is_reloaded_on_size_change(plugin, modes):
    store = modes.ModeStore()
    store.set_mode(OWNER, CHAT, "off", via="cli")
    path = modes_file(plugin)
    original = path.stat()
    assert store.get(OWNER, CHAT).mode == "off"

    _rewrite_in_place(path, "both")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_ino == original.st_ino
    assert path.stat().st_mtime_ns == original.st_mtime_ns
    assert path.stat().st_size != original.st_size

    assert store.get(OWNER, CHAT).mode == "both"


@pytest.mark.asyncio
async def test_cli_set_applies_to_next_note_in_loaded_gateway(plugin, modes):
    bot = FakeBot(business_owner_id=OWNER)
    first = make_message(from_user_id=PEER)
    event = make_event(first)
    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": "first note"},
    )
    assert first.voice.get_file_calls == 1

    parser = argparse.ArgumentParser(prog="hermes telegram-business")
    plugin._setup_cli(parser)
    with contextlib.redirect_stdout(io.StringIO()):
        assert modes.handle_mode_cli(
            parser.parse_args(["mode", "set", "991", "off", "--owner", "1000"]), store=modes.ModeStore()
        ) == 0

    second = make_message(from_user_id=PEER)
    second.message_id = 78
    event = make_event(second)
    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": "second note"},
    )
    assert second.voice.get_file_calls == 0


# --- Atomic, private writes -------------------------------------------------------------------


def test_write_is_private_and_schema_is_minimal(plugin, modes):
    modes.default_store().set_mode(OWNER, CHAT, "off", via="chat", label="Alice (@alice)")
    path = modes_file(plugin)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"version", "owners"}
    assert payload["version"] == 1
    entry = payload["owners"]["1000"]["chats"]["991"]
    assert set(entry) == {"mode", "updated_at", "updated_via", "label"}
    assert entry["label"] == "Alice (@alice)"
    assert list(path.parent.glob(".transcription-modes.*.tmp")) == []


def test_temp_file_is_private_under_permissive_umask(plugin, modes, monkeypatch):
    observed = []
    original_replace = os.replace

    def recording_replace(src, dst):
        if Path(src).name.startswith(".transcription-modes."):
            observed.append(stat.S_IMODE(os.stat(src).st_mode))
        return original_replace(src, dst)

    previous = os.umask(0o022)
    try:
        monkeypatch.setattr(modes.os, "replace", recording_replace)
        modes.default_store().set_mode(OWNER, CHAT, "off", via="cli")
    finally:
        os.umask(previous)

    assert observed == [0o600]


def test_failed_replace_keeps_old_file_and_removes_temp(plugin, modes, monkeypatch):
    store = modes.default_store()
    store.set_mode(OWNER, CHAT, "off", via="cli")
    path = modes_file(plugin)
    before = path.read_bytes()

    def failing_replace(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr(modes.os, "replace", failing_replace)
    with pytest.raises(OSError):
        store.set_mode(OWNER, CHAT, "own", via="cli")

    assert path.read_bytes() == before
    assert list(path.parent.glob(".transcription-modes.*.tmp")) == []


def test_concurrent_writers_do_not_lose_updates(plugin, modes):
    errors = []

    def write(chat):
        try:
            modes.ModeStore().set_mode(OWNER, chat, "off", via="cli")
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(5000 + index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    payload = json.loads(modes_file(plugin).read_text(encoding="utf-8"))
    assert sorted(payload["owners"]["1000"]["chats"]) == [str(5000 + index) for index in range(8)]


def test_writer_waits_for_cross_process_file_lock(plugin, modes):
    fcntl = pytest.importorskip("fcntl")
    path = modes_file(plugin)
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    errors = []

    def write():
        try:
            modes.ModeStore().set_mode(OWNER, CHAT, "off", via="cli")
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
            errors.append(exc)

    # flock belongs to the open file description, so a separate descriptor in
    # this process conflicts exactly like the gateway or CLI in another one.
    with lock_path.open("r+b") as other_process:
        fcntl.flock(other_process.fileno(), fcntl.LOCK_EX)
        thread = threading.Thread(target=write)
        thread.start()
        thread.join(timeout=0.3)
        assert thread.is_alive()
        assert not path.exists()
        fcntl.flock(other_process.fileno(), fcntl.LOCK_UN)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert modes.ModeStore().get(OWNER, CHAT).mode == "off"


def test_cross_process_writers_do_not_lose_updates(plugin, modes):
    # Independent interpreters import this checkout against the same isolated home.
    script = """import importlib.util, sys, types
from pathlib import Path
constants = types.ModuleType("hermes_constants")
constants.get_hermes_home = lambda: Path(sys.argv[2])
sys.modules["hermes_constants"] = constants
spec = importlib.util.spec_from_file_location("subject", Path(sys.argv[1]) / "__init__.py")
module = importlib.util.module_from_spec(spec)
sys.modules["subject"] = module
spec.loader.exec_module(module)
for index in range(20):
    module._transcription_modes.ModeStore().set_mode("1000", str(int(sys.argv[3]) + index), "off", via="cli")
"""
    home = modes_file(plugin).parents[2]
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(test_plugin.ROOT), str(home), str(offset)])
        for offset in (5000, 6000)
    ]
    assert [process.wait(timeout=60) for process in processes] == [0, 0]

    payload = json.loads(modes_file(plugin).read_text(encoding="utf-8"))
    assert sorted(payload["owners"]["1000"]["chats"], key=int) == [
        str(offset + index) for offset in (5000, 6000) for index in range(20)
    ]


def test_reset_keeps_owner_and_both_needs_no_lookup_and_label_is_kept(plugin, modes):
    store = modes.default_store()
    store.set_mode(OWNER, CHAT, "off", via="chat", label="Alice")
    assert store.needs_owner_lookup(CHAT)

    store.set_mode(OWNER, CHAT, "both", via="cli")
    assert not store.needs_owner_lookup(CHAT)
    assert store.get(OWNER, CHAT).label == "Alice"
    assert store.get(OWNER, CHAT).mode == "both"

    assert store.reset(OWNER, CHAT).changed is True
    assert store.reset(OWNER, CHAT).changed is False
    payload = json.loads(modes_file(plugin).read_text(encoding="utf-8"))
    assert payload["owners"] == {"1000": {"chats": {}}}
    assert store.known_owners() == ["1000"]

    with pytest.raises(modes.ModeStoreError):
        store.set_mode(OWNER, CHAT, "loud", via="cli")
    with pytest.raises(modes.ModeStoreError):
        store.set_mode("alice", CHAT, "off", via="cli")
    with pytest.raises(modes.ModeStoreError):
        store.set_mode(OWNER, "@alice", "off", via="api")


def test_reset_never_creates_an_owner_and_drops_an_emptied_extra_owner(plugin, modes):
    store = modes.default_store()
    store.set_mode(OWNER, CHAT, "off", via="chat")

    assert store.reset(1111, CHAT).changed is False
    assert store.known_owners() == ["1000"]

    store.set_mode(1111, CHAT, "own", via="cli")
    assert store.known_owners() == ["1000", "1111"]
    assert store.reset(1111, CHAT).changed is True
    assert store.known_owners() == ["1000"]
    assert store.get(OWNER, CHAT).mode == "off"


# --- Corrupt or missing file ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file_reads_create_nothing_and_note_is_transcribed(plugin, modes):
    store = modes.default_store()
    path = modes_file(plugin)

    assert store.snapshot() == {}
    assert store.get(OWNER, CHAT) is None
    assert not path.parent.exists()

    message = make_message(from_user_id=PEER)
    event = make_event(message)
    bot = FakeBot(business_owner_id=OWNER)
    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": LONG_TRANSCRIPT},
    )
    assert len(bot.calls) == 1
    assert not path.exists()
    assert not path.with_name(path.name + ".lock").exists()


CORRUPT_PAYLOADS = {
    "not_json": "{not json SECRET-CONTENT",
    "root_list": '["SECRET-CONTENT"]',
    "version_2": '{"version": 2, "owners": {"SECRET-CONTENT": {}}}',
    "owners_not_dict": '{"version": 1, "owners": ["SECRET-CONTENT"]}',
    "not_utf8": b"\xff\xfe SECRET-CONTENT",
}


def _write_raw(path: Path, payload: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(CORRUPT_PAYLOADS))
async def test_corrupt_file_fails_open_with_one_content_free_warning(plugin, modes, caplog, case):
    _write_raw(modes_file(plugin), CORRUPT_PAYLOADS[case])
    store = modes.default_store()

    assert store.snapshot() == {}
    assert store.snapshot() == {}
    warnings = [record for record in caplog.records if "unreadable" in record.getMessage()]
    assert len(warnings) == 1
    assert "SECRET-CONTENT" not in caplog.text

    message = make_message(from_user_id=PEER)
    event = make_event(message)
    bot = FakeBot(business_owner_id=OWNER)
    await plugin._process_business_voice_event(
        event=event,
        gateway=gateway_for(event, bot),
        transcribe_fn=lambda _path: {"success": True, "transcript": LONG_TRANSCRIPT},
    )
    assert len(bot.calls) == 1


def test_transient_read_error_is_not_cached(plugin, modes, monkeypatch, caplog):
    store = modes.ModeStore()
    store.set_mode(OWNER, CHAT, "off", via="cli")
    path = modes_file(plugin)
    store.clear_cache()
    original_read_bytes = Path.read_bytes
    failures = {"left": 2}

    def flaky_read_bytes(self):
        if self == path and failures["left"]:
            failures["left"] -= 1
            raise OSError(24, "Too many open files")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)

    assert store.get(OWNER, CHAT) is None
    assert store.get(OWNER, CHAT) is None
    assert store.get(OWNER, CHAT).mode == "off"
    warnings = [record for record in caplog.records if "unreadable" in record.getMessage()]
    assert len(warnings) == 1


def test_transient_read_error_keeps_last_valid_modes(plugin, modes, monkeypatch, caplog):
    store = modes.ModeStore()
    store.set_mode(OWNER, CHAT, "off", via="cli")
    assert store.get(OWNER, CHAT).mode == "off"
    # Another process changes the file, then reading it fails for a while.
    modes.ModeStore().set_mode(OWNER, CHAT, "peer", via="cli")
    path = modes_file(plugin)
    original_read_bytes = Path.read_bytes
    original_stat = os.stat
    failures = {"read": 2, "stat": 1}

    def flaky_read_bytes(self):
        if self == path and failures["read"]:
            failures["read"] -= 1
            raise OSError(24, "Too many open files")
        return original_read_bytes(self)

    def flaky_stat(target, *args, **kwargs):
        if Path(target) == path and failures["stat"]:
            failures["stat"] -= 1
            raise OSError(5, "Input/output error")
        return original_stat(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)
    monkeypatch.setattr(modes.os, "stat", flaky_stat)

    assert store.get(OWNER, CHAT).mode == "off"
    assert store.get(OWNER, CHAT).mode == "off"
    assert store.get(OWNER, CHAT).mode == "off"
    assert store.get(OWNER, CHAT).mode == "peer"
    warnings = [record.getMessage() for record in caplog.records if "unreadable" in record.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].endswith("keeping the last valid modes")


@pytest.mark.asyncio
async def test_corrupt_file_keeps_last_valid_modes_and_only_the_cli_repairs_it(plugin, modes, caplog):
    seed(plugin, OWNER, CHAT, "off")
    store = modes.default_store()
    assert store.get(OWNER, CHAT).mode == "off"
    path = modes_file(plugin)
    _write_raw(path, "{not json SECRET-CONTENT")

    assert store.get(OWNER, CHAT).mode == "off"
    assert store.get(OWNER, CHAT).mode == "off"
    warnings = [record.getMessage() for record in caplog.records if "unreadable" in record.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].endswith("keeping the last valid modes")
    assert "SECRET-CONTENT" not in caplog.text

    # The gateway still skips the note and never rewrites the file to bind the connection.
    bot = FakeBot(business_owner_id=OWNER)
    assert (await _voice_event(plugin, bot)).voice.get_file_calls == 0
    assert bot.calls == []

    # An in-chat set or reset answers with an error status instead of overwriting
    # the file; a bare /transcribe shows the mode the gateway is applying.
    command_bot = CommandBot(business_owner_id=OWNER)
    for message_id, text in ((90, "/transcribe both"), (91, "/transcribe reset"), (92, "/transcribe")):
        assert await _run_command(plugin, make_text_update(text, message_id=message_id), command_bot) is True
    assert [call["text"] for call in command_bot.text_edit_calls] == [
        "/transcribe both → Error: could not save transcription mode",
        "/transcribe reset → Error: could not save transcription mode",
        "/transcribe → Transcription: off",
    ]
    assert path.read_bytes() == b"{not json SECRET-CONTENT"
    assert list(path.parent.glob("transcription-modes.json.corrupt-*")) == []
    assert store.get(OWNER, CHAT).mode == "off"
    with pytest.raises(modes.ModesFileCorrupt):
        store.bind_connection(BCID, OWNER)

    # Only an explicit CLI write backs the damaged file up and starts fresh.
    assert run_cli(plugin, "mode", "set", "991", "peer", "--owner", "1000")[0] == 0
    assert len(list(path.parent.glob("transcription-modes.json.corrupt-*"))) == 1
    assert store.get(OWNER, CHAT).mode == "peer"


@pytest.mark.asyncio
async def test_corrupt_file_does_not_retry_the_connection_binding_per_message(plugin, modes, caplog, monkeypatch):
    seed(plugin, OWNER, CHAT, "off")
    store = modes.default_store()
    assert store.get(OWNER, CHAT).mode == "off"
    assert store.connection_owner(BCID) is None
    path = modes_file(plugin)
    _write_raw(path, "{not json")
    binds = []
    original_bind = store.bind_connection
    monkeypatch.setattr(store, "bind_connection", lambda *args: binds.append(args) or original_bind(*args))

    bot = FakeBot(business_owner_id=OWNER)
    for message_id in (80, 81, 82):
        assert (await _voice_event(plugin, bot, message_id=message_id)).voice.get_file_calls == 0
    assert binds == []
    assert store.file_degraded() is True
    assert "could not record Business connection owner" not in caplog.text
    assert len([record for record in caplog.records if "unreadable" in record.getMessage()]) == 1
    assert path.read_bytes() == b"{not json"

    # If the file breaks between the check and the write, the bind fails quietly.
    caplog.clear()
    with monkeypatch.context() as patch:
        patch.setattr(store, "file_degraded", lambda: False)
        await plugin._bind_business_connection(store, BCID, "1000")
    assert len(binds) == 1
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []

    # After the CLI repairs the file, the next note records the connection again.
    assert run_cli(plugin, "mode", "set", "991", "off", "--owner", "1000")[0] == 0
    assert store.file_degraded() is False
    assert (await _voice_event(plugin, bot, message_id=83)).voice.get_file_calls == 0
    assert store.connection_owner(BCID) == "1000"


@pytest.mark.asyncio
async def test_unreadable_file_does_not_retry_the_connection_binding_per_message(plugin, modes, caplog, monkeypatch):
    seed(plugin, OWNER, CHAT, "off")
    store = modes.default_store()
    assert store.get(OWNER, CHAT).mode == "off"
    path = modes_file(plugin)
    # Another process rewrites the file (a new inode), which this process then cannot read.
    modes.ModeStore().set_mode(OWNER, 992, "peer", via="cli")
    original_read_bytes = Path.read_bytes

    def denied_read_bytes(self):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return original_read_bytes(self)

    binds = []
    original_bind = store.bind_connection
    monkeypatch.setattr(store, "bind_connection", lambda *args: binds.append(args) or original_bind(*args))
    bot = FakeBot(business_owner_id=OWNER)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", denied_read_bytes)
        for message_id in (80, 81, 82):
            assert (await _voice_event(plugin, bot, message_id=message_id)).voice.get_file_calls == 0
        assert store.file_degraded() is True
    assert binds == []
    assert "could not record Business connection owner" not in caplog.text
    assert len([record for record in caplog.records if "unreadable" in record.getMessage()]) == 1

    # Once the file reads again, the next note records the connection.
    assert store.file_degraded() is False
    assert (await _voice_event(plugin, bot, message_id=83)).voice.get_file_calls == 0
    assert store.connection_owner(BCID) == str(OWNER)
    assert store.get(OWNER, 992).mode == "peer"


def test_stat_failure_marks_the_file_degraded_until_it_reads_again(plugin, modes, monkeypatch):
    seed(plugin, OWNER, CHAT, "off")
    store = modes.default_store()
    assert store.file_degraded() is False
    path = modes_file(plugin)
    original_stat = os.stat

    def failing_stat(target, *args, **kwargs):
        if Path(target) == path:
            raise OSError(5, "Input/output error")
        return original_stat(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(modes.os, "stat", failing_stat)
        assert store.file_degraded() is True
        assert store.get(OWNER, CHAT).mode == "off"
    # The unchanged file is served from the cache again, without a re-read.
    assert store.file_degraded() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["", " \n\t\n"])
async def test_empty_modes_file_reads_as_no_modes(plugin, modes, caplog, payload):
    path = modes_file(plugin)
    _write_raw(path, payload)
    store = modes.default_store()

    assert store.snapshot() == {}
    assert store.file_degraded() is False
    assert run_cli(plugin, "mode", "list") == (0, "No transcription modes set; every chat uses both (default).")
    assert run_cli(plugin, "mode", "get", "991") == (0, "chat=991 mode=both (default)")
    assert run_cli(plugin, "mode", "set", "991", "off")[1].startswith("error: no Business owner known yet;")
    assert "unreadable" not in caplog.text

    # The owner's in-chat command writes over it without a corrupt-file backup.
    bot = CommandBot(business_owner_id=OWNER)
    assert await _run_command(plugin, make_text_update("/transcribe off"), bot) is True
    assert [call["text"] for call in bot.text_edit_calls] == ["/transcribe off → Transcription: off"]
    assert store.get(OWNER, CHAT).mode == "off"
    assert list(path.parent.glob("transcription-modes.json.corrupt-*")) == []


@pytest.mark.asyncio
async def test_bare_transcribe_records_no_owner_for_the_cli(plugin, modes):
    bot = CommandBot(business_owner_id=OWNER)
    no_owner = (
        "error: no Business owner known yet; send /transcribe <mode> or /transcribe reset "
        "in a Business chat once, or pass --owner USER_ID"
    )

    assert await _run_command(plugin, make_text_update("/transcribe", message_id=90), bot) is True
    assert run_cli(plugin, "mode", "set", "992", "off") == (1, no_owner)

    assert await _run_command(plugin, make_text_update("/transcribe reset", message_id=91), bot) is True
    assert run_cli(plugin, "mode", "set", "992", "off") == (0, "chat=992 mode=off")


def test_cli_write_without_owner_never_backs_up_a_file_that_broke_after_the_read(plugin, modes, monkeypatch):
    seed(plugin, OWNER, 555, "off")
    store = modes.default_store()
    valid = store.read_strict()
    path = modes_file(plugin)
    _write_raw(path, "{not json")
    # The CLI read the valid file; it is corrupted before the write takes the lock.
    monkeypatch.setattr(store, "read_strict", lambda: valid)

    for argv in (("set", "991", "off"), ("reset", "555")):
        exit_code, output = run_cli(plugin, "mode", *argv)
        assert exit_code == 1, argv
        assert output.startswith("error: ") and "is corrupt (JSONDecodeError)" in output, argv
    assert path.read_bytes() == b"{not json"
    assert list(path.parent.glob("transcription-modes.json.corrupt-*")) == []

    assert run_cli(plugin, "mode", "set", "991", "off", "--owner", "1000")[0] == 0
    assert len(list(path.parent.glob("transcription-modes.json.corrupt-*"))) == 1


def test_invalid_entries_are_skipped_and_valid_entries_apply(plugin, modes, caplog):
    valid = {"mode": "off", "updated_at": "2026-10-19T12:00:00Z", "updated_via": "cli", "label": None}
    payload = {
        "version": 1,
        "owners": {
            "1000": {"chats": {"991": valid, "992": {**valid, "mode": "loud"}, "abc": valid}},
            "not-an-owner": {"chats": {"991": valid}},
        },
    }
    _write_raw(modes_file(plugin), json.dumps(payload))
    store = modes.default_store()

    assert store.get(OWNER, CHAT).mode == "off"
    assert store.get(OWNER, 992) is None
    assert store.known_owners() == ["1000"]
    assert "skipped 3 invalid entries" in caplog.text


def test_write_on_corrupt_file_backs_it_up(plugin, modes):
    path = modes_file(plugin)
    _write_raw(path, "{not json")

    exit_code, output = run_cli(plugin, "mode", "set", "991", "off", "--owner", "1000")

    assert exit_code == 0
    backups = list(path.parent.glob("transcription-modes.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"{not json"
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert f"warning: corrupt transcription-modes.json backed up to {backups[0]}" in output
    assert output.endswith("chat=991 mode=off")
    assert json.loads(path.read_text(encoding="utf-8"))["owners"]["1000"]["chats"]["991"]["mode"] == "off"


# --- CLI --------------------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_cli_reports_an_unreadable_modes_file_instead_of_the_default(plugin, modes):
    seed(plugin, OWNER, 555, "off")
    path = modes_file(plugin)
    path.chmod(0)
    try:
        for argv in (("list",), ("get", "555"), ("set", "556", "off"), ("reset", "555")):
            exit_code, output = run_cli(plugin, "mode", *argv)
            assert exit_code == 1, argv
            assert output.startswith("error: ") and "Permission denied" in output, argv
            assert "default" not in output and "no Business owner" not in output, argv
    finally:
        path.chmod(0o600)
    assert run_cli(plugin, "mode", "get", "555")[1].startswith("chat=555 mode=off (via cli at ")


def test_cli_reports_a_corrupt_modes_file_and_only_an_explicit_owner_write_repairs_it(plugin, modes):
    _write_raw(modes_file(plugin), "{not json SECRET-CONTENT")

    for argv in (("list",), ("get", "991"), ("set", "991", "off"), ("reset", "991")):
        exit_code, output = run_cli(plugin, "mode", *argv)
        assert exit_code == 1, argv
        assert output.startswith("error: ") and "is corrupt (JSONDecodeError)" in output, argv
        assert "SECRET-CONTENT" not in output, argv

    assert run_cli(plugin, "mode", "set", "991", "off", "--owner", "1000")[0] == 0
    assert run_cli(plugin, "mode", "get", "991")[1].startswith("chat=991 mode=off")


def test_cli_set_get_list_reset_round_trip(plugin, modes):
    assert run_cli(plugin, "mode", "set", "991", "off", "--owner", "1000") == (0, "chat=991 mode=off")
    path = modes_file(plugin)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert modes.default_store().get(OWNER, CHAT).updated_via == "cli"

    exit_code, output = run_cli(plugin, "mode", "get", "991")
    assert exit_code == 0
    assert output.startswith("chat=991 mode=off (via cli at ")

    exit_code, output = run_cli(plugin, "mode", "list")
    assert exit_code == 0
    assert output.startswith("owner=1000 chat=991 mode=off via=cli updated_at=")

    assert run_cli(plugin, "mode", "reset", "991") == (0, "chat=991 mode=both (default)")
    assert run_cli(plugin, "mode", "get", "991") == (0, "chat=991 mode=both (default)")
    assert run_cli(plugin, "mode", "set", "-100123", "peer") == (0, "chat=-100123 mode=peer")
    assert run_cli(plugin, "mode", "list", "--owner", "1000")[1].startswith("owner=1000 chat=-100123 mode=peer")


def test_cli_list_shows_labels_and_empty_state(plugin, modes):
    assert run_cli(plugin, "mode", "list") == (0, "No transcription modes set; every chat uses both (default).")
    modes.default_store().set_mode(OWNER, CHAT, "off", via="chat", label='Alice "A"')

    _exit_code, output = run_cli(plugin, "mode", "list")

    assert output.endswith('label="Alice \\"A\\""')


def test_cli_owner_inference(plugin, modes):
    exit_code, output = run_cli(plugin, "mode", "set", "991", "off")
    assert exit_code == 1
    assert output == (
        "error: no Business owner known yet; send /transcribe <mode> or /transcribe reset "
        "in a Business chat once, or pass --owner USER_ID"
    )
    assert run_cli(plugin, "mode", "get", "991") == (0, "chat=991 mode=both (default)")

    modes.default_store().set_mode(OWNER, 1, "off", via="chat")
    assert run_cli(plugin, "mode", "set", "991", "own") == (0, "chat=991 mode=own")
    assert modes.default_store().get(OWNER, CHAT).mode == "own"

    modes.default_store().set_mode(2000, 1, "off", via="chat")
    exit_code, output = run_cli(plugin, "mode", "set", "991", "peer")
    assert exit_code == 1
    assert "multiple Business owners known (1000, 2000); pass --owner" in output
    assert run_cli(plugin, "mode", "get", "991")[0] == 1
    assert len(run_cli(plugin, "mode", "list")[1].splitlines()) == 3
    exit_code, output = run_cli(plugin, "mode", "list", "--owner", "2000")
    assert exit_code == 0
    assert output.splitlines() and all(line.startswith("owner=2000 ") for line in output.splitlines())
    assert run_cli(plugin, "mode", "get", "991", "--owner", "1000")[1].startswith("chat=991 mode=own")

    exit_code, output = run_cli(plugin, "mode", "set", "991", "peer", "--owner", "alice")
    assert exit_code == 1
    assert output.startswith("error: owner must be a numeric")


def test_cli_warns_about_an_unknown_explicit_owner_and_reset_repairs_a_typo(plugin, modes):
    modes.default_store().set_mode(OWNER, 1, "off", via="chat")

    exit_code, output = run_cli(plugin, "mode", "set", "991", "off", "--owner", "1111")
    assert exit_code == 0
    assert output.splitlines() == ["warning: owner 1111 is not a known Business owner (known: 1000)", "chat=991 mode=off"]
    assert run_cli(plugin, "mode", "get", "991")[0] == 1

    assert run_cli(plugin, "mode", "reset", "991", "--owner", "1111") == (0, "chat=991 mode=both (default)")
    assert modes.default_store().known_owners() == ["1000"]
    assert run_cli(plugin, "mode", "reset", "5", "--owner", "2222") == (0, "chat=5 mode=both (default)")
    assert modes.default_store().known_owners() == ["1000"]
    assert run_cli(plugin, "mode", "get", "991") == (0, "chat=991 mode=both (default)")


@pytest.mark.asyncio
async def test_cli_one_owner_across_several_connections_needs_no_owner(history_enabled, modes):
    plugin = history_enabled
    history = plugin._history_support
    bot = FakeBot(business_owner_id=OWNER)
    base = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    for update_id, (bcid, chat_id, username) in enumerate(((BCID, 991, "alice"), ("business-b", 995, "bob")), 1):
        await history.observe_ptb_update(
            test_history.make_business_text_update(
                text="hi", business_id=bcid, chat_id=chat_id, chat_username=username, update_id=update_id, date=base
            ),
            bot=bot,
            now=base,
        )
    store = modes.default_store()
    store.bind_connection(BCID, OWNER)
    store.bind_connection("business-b", OWNER)

    assert run_cli(plugin, "mode", "set", "@alice", "own") == (0, "chat=991 mode=own")
    assert run_cli(plugin, "mode", "set", "@bob", "off") == (0, "chat=995 mode=off")
    assert run_cli(plugin, "mode", "get", "@bob")[1].startswith("chat=995 mode=off (via cli at ")
    assert run_cli(plugin, "mode", "reset", "@alice") == (0, "chat=991 mode=both (default)")
    assert store.get(OWNER, 995).mode == "off"

    # A second owner, known only through its connection binding: a chat on a
    # recorded connection still resolves, by name or by the chat_id history
    # lists for it; a chat_id history does not list needs --owner.
    store.bind_connection("business-c", 3000)
    assert run_cli(plugin, "mode", "get", "@bob")[1].startswith("chat=995 mode=off (via cli at ")
    assert run_cli(plugin, "mode", "get", "995")[1].startswith("chat=995 mode=off (via cli at ")
    assert run_cli(plugin, "mode", "set", "995", "peer") == (0, "chat=995 mode=peer")
    assert store.get(OWNER, 995).mode == "peer"
    for argv in (("set", "999", "own"), ("reset", "999"), ("get", "999")):
        assert run_cli(plugin, "mode", *argv) == (1, "error: multiple Business owners known (1000, 3000); pass --owner")
    assert run_cli(plugin, "mode", "set", "@bob", "own", "--owner", "3000") == (0, "chat=995 mode=own")
    assert store.get(OWNER, 995).mode == "peer"


@pytest.mark.asyncio
async def test_cli_requires_owner_for_a_connection_with_no_recorded_owner(history_enabled, modes):
    plugin = history_enabled
    history = plugin._history_support
    bot = FakeBot(business_owner_id=OWNER)
    base = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    await history.observe_ptb_update(
        test_history.make_business_text_update(text="hi", chat_id=991, chat_username="alice", update_id=1, date=base),
        bot=bot,
        now=base,
    )
    store = modes.default_store()
    store.set_mode(OWNER, 1, "off", via="cli")
    # One connection in history: the one known owner is inferred.
    assert run_cli(plugin, "mode", "set", "@alice", "own") == (0, "chat=991 mode=own")

    # A second Business account whose chats all use the default is never recorded.
    await history.observe_ptb_update(
        test_history.make_business_text_update(
            text="hi", business_id="business-b", chat_id=995, chat_username="bob", update_id=2, date=base
        ),
        bot=bot,
        now=base,
    )
    unbound_b = "error: no owner recorded for Business connection business-b; pass --owner"
    for argv in (("set", "@bob", "off"), ("reset", "@bob"), ("get", "@bob")):
        assert run_cli(plugin, "mode", *argv) == (1, unbound_b)
    numeric = "error: 2 Business connections found in history, not all recorded for owner 1000; pass --owner"
    assert run_cli(plugin, "mode", "set", "999", "off") == (1, numeric)
    # A chat_id history lists takes that chat's connection.
    assert run_cli(plugin, "mode", "set", "995", "off") == (1, unbound_b)
    assert run_cli(plugin, "mode", "set", "995", "off", "--connection", "business-b") == (1, unbound_b)
    assert run_cli(plugin, "mode", "set", "@alice", "peer") == (
        1,
        f"error: no owner recorded for Business connection {BCID}; pass --owner",
    )
    assert store.get(OWNER, 995) is None

    # A recorded connection resolves on its own, by name or by its chat_id;
    # a chat_id history does not list still needs --owner.
    store.bind_connection(BCID, OWNER)
    assert run_cli(plugin, "mode", "set", "@alice", "peer") == (0, "chat=991 mode=peer")
    assert run_cli(plugin, "mode", "set", "991", "off") == (0, "chat=991 mode=off")
    assert run_cli(plugin, "mode", "set", "999", "off") == (1, numeric)
    assert run_cli(plugin, "mode", "set", "@bob", "off", "--owner", "3000") == (
        0,
        "warning: owner 3000 is not a known Business owner (known: 1000)\nchat=995 mode=off",
    )
    assert store.get(3000, 995).mode == "off"
    assert store.get(OWNER, 995) is None

    # Once business-b is recorded for owner 3000, its chats resolve to that owner.
    store.bind_connection("business-b", 3000)
    assert run_cli(plugin, "mode", "set", "@bob", "peer") == (0, "chat=995 mode=peer")
    assert run_cli(plugin, "mode", "get", "995", "--connection", "business-b")[1].startswith("chat=995 mode=peer ")
    assert store.get(3000, 995).mode == "peer"
    assert run_cli(plugin, "mode", "set", "991", "own") == (0, "chat=991 mode=own")
    assert store.get(OWNER, 991).mode == "own"
    assert run_cli(plugin, "mode", "set", "999", "off") == (
        1,
        "error: multiple Business owners known (1000, 3000); pass --owner",
    )


def test_cli_owner_from_a_connection_binding_without_chats(plugin, modes):
    empty = "No transcription modes set; every chat uses both (default)."
    assert run_cli(plugin, "mode", "list") == (0, empty)
    assert run_cli(plugin, "mode", "get", "991") == (0, "chat=991 mode=both (default)")

    modes.default_store().bind_connection(BCID, OWNER)

    assert run_cli(plugin, "mode", "list") == (0, empty)
    assert run_cli(plugin, "mode", "get", "991") == (0, "chat=991 mode=both (default)")
    assert run_cli(plugin, "mode", "set", "991", "off") == (0, "chat=991 mode=off")
    assert modes.default_store().get(OWNER, CHAT).mode == "off"
    assert run_cli(plugin, "mode", "list")[1].startswith("owner=1000 chat=991 mode=off via=cli ")


@pytest.mark.asyncio
async def test_cli_chat_selector_uses_history_contacts(history_enabled, modes, monkeypatch):
    plugin = history_enabled
    history = plugin._history_support
    bot = FakeBot(business_owner_id=OWNER)
    base = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    for chat_id, username, update_id in ((991, "customer991", 1), (992, "customer992", 2)):
        await history.observe_ptb_update(
            test_history.make_business_text_update(
                text="hi", chat_id=chat_id, chat_username=username, update_id=update_id, date=base
            ),
            bot=bot,
            now=base,
        )

    assert run_cli(plugin, "mode", "set", "@customer991", "off", "--owner", "1000") == (0, "chat=991 mode=off")
    assert modes.default_store().get(OWNER, CHAT).label == "@customer991"

    exit_code, output = run_cli(plugin, "mode", "set", "Casey Customer", "off", "--owner", "1000")
    assert exit_code == 1
    assert "choose one of" in output
    assert "(pass the numeric chat_id or --connection)" in output

    exit_code, output = run_cli(plugin, "mode", "set", "nobody", "off", "--owner", "1000")
    assert exit_code == 1
    assert "no matching contact" in output

    monkeypatch.delenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE")
    exit_code, output = run_cli(plugin, "mode", "set", "@customer991", "off", "--owner", "1000")
    assert exit_code == 1
    assert "pass the chat_id or enable history to resolve names" in output


def test_cli_usage_and_routing(plugin, modes):
    with pytest.raises(SystemExit) as excinfo, contextlib.redirect_stderr(io.StringIO()):
        run_cli(plugin, "mode", "set", "991", "loud")
    assert excinfo.value.code == 2

    assert run_cli(plugin, "mode") == (1, modes.MODE_USAGE)
    assert run_cli(plugin) == (1, "Usage: hermes telegram-business <history|mode> ...")
    exit_code, output = run_cli(plugin, "history", "stats")
    assert exit_code == 0
    assert output.startswith("enabled=False")
    assert run_cli(plugin, "history")[0] == 1
