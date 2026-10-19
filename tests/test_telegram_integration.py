"""Real PTB dispatch and API serialization with an in-memory Telegram transport."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
import test_history
import test_plugin
from telegram import Update
from telegram.ext import Application, ExtBot, TypeHandler
from telegram.request import BaseRequest

isolate_plugin_environment = test_plugin.isolate_plugin_environment
plugin = test_plugin.plugin


class TelegramRequest(BaseRequest):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.responses = {
            "getMe": (200, {"ok": True, "result": {"id": 123, "is_bot": True, "first_name": "Test"}}),
            "getBusinessConnection": (200, {"ok": True, "result": {
                "id": "business-123", "user": {"id": 1000, "is_bot": False, "first_name": "Owner"},
                "user_chat_id": 1000, "date": 1, "is_enabled": True,
            }}),
            "editMessageCaption": (200, {"ok": True, "result": True}),
            "sendMessage": (200, {"ok": True, "result": {
                "message_id": 700, "date": 1, "chat": {"id": 991, "type": "private"}, "text": "sent",
            }}),
        }

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, request_data.json_parameters))
        status, response = self.responses[endpoint]
        return status, json.dumps(response).encode()


@pytest_asyncio.fixture
async def telegram_app():
    request = TelegramRequest()
    bot = ExtBot(token="123:test-token", request=request)
    app = Application.builder().bot(bot).updater(None).build()
    async with app:
        yield app, request


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [
    {"voice": {"file_id": "v", "file_unique_id": "v", "duration": 3}},
    {"video_note": {"file_id": "v", "file_unique_id": "v", "duration": 3, "length": 100}},
    {"audio": {"file_id": "v", "file_unique_id": "v", "duration": 3, "mime_type": "audio/mpeg"}},
    {"document": {"file_id": "v", "file_unique_id": "v", "file_name": "note.m4a"}},
])
@pytest.mark.parametrize("authorized,bypass,claimed", [
    (True, False, True), (False, False, False), (False, True, True),
])
@pytest.mark.parametrize("agent_enabled", [False, True])
async def test_native_business_media_dispatch(
    plugin, telegram_app, monkeypatch, media, authorized, bypass, claimed, agent_enabled,
):
    app, _request = telegram_app
    monkeypatch.setenv("TELESEC_VOICE_BYPASS_AUTH", str(int(bypass)))
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", str(int(agent_enabled)))
    observe = AsyncMock(return_value=False)
    process = AsyncMock()
    monkeypatch.setattr(plugin._history_support, "observe_ptb_update", observe)
    monkeypatch.setattr(plugin, "_process_business_voice_event", process)
    adapter = SimpleNamespace(platform="telegram", _bot=app.bot, _is_user_authorized_from_message=Mock(return_value=authorized))
    ctx = SimpleNamespace(llm=None, register_telegram_handler=Mock(), register_hook=Mock(),
                          get_config={}.get, register_cli_command=Mock(), register_skill=Mock())
    plugin.register(ctx)
    factory, = ctx.register_telegram_handler.call_args.args
    factory(app, adapter)
    core = AsyncMock()
    app.add_handler(TypeHandler(Update, core))
    update = Update.de_json({"update_id": 42, "business_message": {
        "message_id": 77, "date": 1, "business_connection_id": "business-123",
        "chat": {"id": 991, "type": "private"},
        "from": {"id": 2000, "is_bot": False, "first_name": "Customer"}, **media,
    }}, app.bot)

    await app.process_update(update)
    if plugin._pending_tasks:
        await asyncio.gather(*plugin._pending_tasks)
    await app.process_update(update)

    assert observe.await_count == 2
    observe.assert_awaited_with(update, bot=app.bot)
    assert process.await_count == int(claimed)
    assert core.await_count == 2 * int(agent_enabled and not claimed)
    if claimed:
        assert process.call_args.kwargs["event"].raw_message is update.effective_message


@pytest.mark.asyncio
@pytest.mark.parametrize("history_fails", [False, True])
@pytest.mark.parametrize("agent_enabled", [False, True])
@pytest.mark.parametrize("kind", ["business_message", "edited_business_message", "deleted_business_messages"])
@pytest.mark.parametrize("sender_id", [1000, 2000])
async def test_native_business_updates_stop_after_history(
    plugin, telegram_app, monkeypatch, history_fails, agent_enabled, kind, sender_id,
):
    app, _request = telegram_app
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", str(int(agent_enabled)))
    calls = []

    async def observe(update, *, bot):
        calls.append("history")
        if history_fails:
            raise OSError("disk full")

    async def core(update, context):
        calls.append("core")

    monkeypatch.setattr(plugin._history_support, "observe_ptb_update", observe)
    adapter = SimpleNamespace(platform="telegram", _bot=app.bot)
    plugin._register_telegram_handlers(app, adapter)
    app.add_handler(TypeHandler(Update, core))
    payload = {
        "message_id": 77, "date": 1, "business_connection_id": "business-123",
        "chat": {"id": 991, "type": "private"}, "text": "Hello",
        "from": {"id": sender_id, "is_bot": False, "first_name": "Speaker"},
    }
    if kind == "deleted_business_messages":
        payload = {"business_connection_id": "business-123", "chat": payload["chat"], "message_ids": [77]}
    update = Update.de_json({"update_id": 42, kind: payload}, app.bot)

    await app.process_update(update)

    expected = ["history"]
    if agent_enabled:
        expected.append("core")
    assert calls == expected


@pytest.mark.asyncio
async def test_native_history_persists_real_ptb_updates_and_leaves_bot_chats_alone(plugin, telegram_app, monkeypatch):
    app, _request = telegram_app
    test_history.set_switch(plugin, "history", True)
    adapter = SimpleNamespace(platform="telegram", _bot=app.bot)
    plugin._register_telegram_handlers(app, adapter)
    core = AsyncMock()
    app.add_handler(TypeHandler(Update, core))
    message = {
        "message_id": 77, "date": int(datetime.now(UTC).timestamp()),
        "business_connection_id": "business-123", "chat": {"id": 991, "type": "private"},
        "from": {"id": 2000, "is_bot": False, "first_name": "Casey"}, "text": "First text",
    }
    updates = [
        {"update_id": 1, "business_message": message},
        {"update_id": 2, "edited_business_message": {**message, "text": "Edited text"}},
        {"update_id": 3, "deleted_business_messages": {
            "business_connection_id": "business-123", "chat": message["chat"], "message_ids": [77],
        }},
        {"update_id": 4, "message": {
            "message_id": 88, "date": 1, "chat": message["chat"],
            "voice": {"file_id": "v", "file_unique_id": "v", "duration": 3},
        }},
    ]
    try:
        for payload in updates:
            await app.process_update(Update.de_json(payload, app.bot))
        records = [json.loads(line)
                   for path in plugin._history_support.history_root().rglob("*.jsonl")
                   for line in path.read_text().splitlines()]
        assert [record["event_type"] for record in records] == [
            "message.created", "message.edited", "message.deleted",
        ]
        assert [record["text"] for record in records[:2]] == ["First text", "Edited text"]
        assert all(record["direction"] == "inbound" for record in records)
        core.assert_awaited_once()
        assert core.call_args.args[0].message.message_id == 88
    finally:
        plugin._history_support.reset_in_memory_caches()


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_id,caption_status,endpoint", [
    (1000, 200, "editMessageCaption"), (2000, 200, "sendMessage"), (1000, 400, "sendMessage"),
])
async def test_real_ptb_business_transcript_serialization(plugin, telegram_app, sender_id, caption_status, endpoint):
    app, request = telegram_app
    if caption_status == 400:
        request.responses["editMessageCaption"] = (400, {
            "ok": False, "error_code": 400, "description": "Bad Request: message can't be edited",
        })
    update = Update.de_json({"update_id": 42, "business_message": {
        "message_id": 77, "date": int(datetime.now(UTC).timestamp()),
        "business_connection_id": "business-123", "chat": {"id": 991, "type": "private"},
        "from": {"id": sender_id, "is_bot": False, "first_name": "Speaker"},
        "voice": {"file_id": "v", "file_unique_id": "v", "duration": 3},
    }}, app.bot)
    transcript = "Hello 🙂 from Telegram."

    await plugin._publish_transcript(
        bot=app.bot, adapter=SimpleNamespace(), message=update.effective_message, transcript=transcript,
        cleanup_fn=None, llm=None,
    )

    method, params = request.calls[-1]
    assert method == endpoint
    assert params["business_connection_id"] == "business-123"
    assert params["chat_id"] == "991"
    if method == "editMessageCaption":
        assert params["message_id"] == "77"
        assert params["caption"] == f"🎙️ {transcript}"
        entities = json.loads(params["caption_entities"])
    else:
        assert json.loads(params["reply_parameters"])["message_id"] == 77
        assert params["text"] == f"🎙️ {transcript}"
        entities = json.loads(params["entities"])
    assert entities[0]["type"] == "expandable_blockquote"
    assert entities[0]["length"] == len(f"🎙️ {transcript}".encode("utf-16-le")) // 2


def _register_native(plugin, app, *, authorized=True):
    adapter = SimpleNamespace(
        platform="telegram", _bot=app.bot, _is_user_authorized_from_message=Mock(return_value=authorized),
    )
    plugin._register_telegram_handlers(app, adapter)
    core = AsyncMock()
    app.add_handler(TypeHandler(Update, core))
    return core


def _business_update(app, *, sender_id, update_id=42, **content):
    return Update.de_json({"update_id": update_id, "business_message": {
        "message_id": 77, "date": int(datetime.now(UTC).timestamp()),
        "business_connection_id": "business-123", "chat": {"id": 991, "type": "private", "first_name": "Casey"},
        "from": {"id": sender_id, "is_bot": False, "first_name": "Speaker"}, **content,
    }}, app.bot)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    {"text": "Hello"},
    {"voice": {"file_id": "v", "file_unique_id": "v", "duration": 3}},
    {"document": {"file_id": "d", "file_unique_id": "d", "file_name": "notes.pdf"}},
    {"video": {"file_id": "v", "file_unique_id": "v", "width": 100, "height": 100, "duration": 3}},
])
async def test_disabled_transcription_still_consumes_business_updates(plugin, telegram_app, monkeypatch, content):
    app, request = telegram_app
    test_history.set_switch(plugin, "transcription", False)
    core = _register_native(plugin, app)

    await app.process_update(_business_update(app, sender_id=2000, **content))

    core.assert_not_awaited()
    assert [method for method, _params in request.calls] == ["getMe"]


@pytest.mark.asyncio
async def test_real_ptb_off_chat_claims_voice_without_download_or_reply(plugin, telegram_app):
    app, request = telegram_app
    plugin._transcription_modes.default_store().set_mode("1000", "991", "off", via="cli")
    core = _register_native(plugin, app)

    await app.process_update(_business_update(
        app, sender_id=2000, voice={"file_id": "v", "file_unique_id": "v", "duration": 3},
    ))
    if plugin._pending_tasks:
        await asyncio.gather(*plugin._pending_tasks)

    assert core.await_count == 0
    endpoints = [method for method, _params in request.calls]
    assert "getFile" not in endpoints
    assert "sendMessage" not in endpoints
    assert "getBusinessConnection" in endpoints


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_id", [1000, 2000])
async def test_real_ptb_transcribe_text_is_ordinary_business_text(plugin, telegram_app, sender_id):
    app, request = telegram_app
    core = _register_native(plugin, app, authorized=False)

    await app.process_update(_business_update(app, sender_id=sender_id, text="/transcribe off"))

    core.assert_not_awaited()
    assert [method for method, _params in request.calls] == ["getMe"]
    assert not plugin._transcription_modes.modes_path().exists()


@pytest.mark.asyncio
async def test_real_ptb_off_then_both_gates_media_download(plugin, telegram_app, monkeypatch):
    app, request = telegram_app
    monkeypatch.setenv("TELESEC_CLEANUP_DISABLE", "1")
    core = _register_native(plugin, app)
    download = AsyncMock(side_effect=RuntimeError("stop at download"))
    monkeypatch.setattr(plugin, "_download_voice", download)

    store = plugin._transcription_modes.default_store()

    def update(number):
        content = {"voice": {"file_id": "v", "file_unique_id": "v", "duration": 3}}
        message = _business_update(app, sender_id=1000, update_id=number, **content).to_dict()["business_message"]
        message["message_id"] = number
        return Update.de_json({"update_id": number, "business_message": message}, app.bot)

    async def process(number):
        await app.process_update(update(number))
        if plugin._pending_tasks:
            await asyncio.gather(*plugin._pending_tasks)

    store.set_mode("1000", "991", "off", via="chat", connection="business-123")  # as an owner-panel tap stores it
    await process(1)
    download.assert_not_awaited()
    assert "sendMessage" not in [method for method, _params in request.calls]

    store.set_mode("1000", "991", "both", via="chat", connection="business-123")
    await process(2)
    download.assert_awaited_once()
    assert core.await_count == 0
