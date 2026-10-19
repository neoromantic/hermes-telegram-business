"""Per-chat history and dossier controls: chat-settings.json, capture, the owner panel, dossiers, stats and the CLI."""

from __future__ import annotations

import json
import logging
import stat
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import test_dossier_panel as tdp
import test_history
import test_owner_panel as top
import test_plugin
import test_transcription_modes as ttm
from dossier_helpers import BUSINESS_ID, OWNER_ID, FakeLlm, cli, cycle, quote_first, read_dossier, seed

isolate_plugin_environment = test_plugin.isolate_plugin_environment
isolate_modes_environment = ttm.isolate_modes_environment
plugin = test_plugin.plugin
modes = ttm.modes
history_enabled = ttm.history_enabled
panel = tdp.panel

OWNER = ttm.OWNER
CHAT = ttm.CHAT
BCID = ttm.BCID
OTHER_OWNER = 3000
OTHER_BCID = "business-999"
HISTORY_ROW = [("• Save history", "tb:f:991:h.on"), ("Don't save", "tb:f:991:h.off")]
DOSSIER_ROW = [("• Keep dossier", "tb:f:991:d.on"), ("No dossier", "tb:f:991:d.off")]
REFRESH_ROW = [("Refresh", "tb:p:991")]
UNREADABLE = "Chat settings can't be read; history and dossiers are paused."
SAVE_ERROR = "Error: could not save the chat setting"
GARBAGE = b"{not json SECRET-CONTENT"


@pytest.fixture
def cs(plugin, modes):
    plugin._chat_settings.reset_in_memory_caches()
    yield plugin._chat_settings
    plugin._dossier_support.stop_workers(timeout=2)
    plugin._chat_settings.reset_in_memory_caches()


@pytest.fixture
def both_on(cs, plugin):
    """History and dossiers on in Settings, so both chat rows show."""
    test_history.set_switch(plugin, "history", True)
    test_history.set_switch(plugin, "dossiers", True)
    plugin._history_support.reset_in_memory_caches()
    return cs


def stored(cs) -> dict:
    return json.loads(cs.chat_settings_path().read_text(encoding="utf-8"))


def text_update(message_id: int = 77, *, chat_id: int = CHAT, business_id: str = BCID, edited: bool = False):
    return test_history.make_business_text_update(message_id=message_id, chat_id=chat_id, business_id=business_id,
                                                  edited=edited, update_id=message_id + (1000 if edited else 0))


async def capture(plugin, update, bot=None) -> None:
    await plugin._history_support.observe_ptb_update(update, bot=bot or test_history.FakeBot())


async def tap(plugin, data: str, bot, *, lang: str | None = None) -> None:
    assert await top.dispatch(plugin, top.callback_update(data, language_code=lang), bot) is True


# --- Store -----------------------------------------------------------------------------------------


def test_store_keeps_only_off_values_and_a_chat_back_on_is_removed(cs):
    store = cs.default_store()
    stamp = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

    result = store.set_flag(OWNER, CHAT, "history", False, via="chat", now=stamp)
    assert result.changed and result.entry.history is False and result.entry.dossier is True
    assert stored(cs) == {"version": 1, "owners": {"1000": {"chats": {"991": {
        "history": "off", "updated_at": "2026-10-19T12:00:00Z", "updated_via": "chat"}}}}}
    assert stat.S_IMODE(cs.chat_settings_path().stat().st_mode) == 0o600

    before = cs.chat_settings_path().read_bytes()
    assert store.set_flag(OWNER, CHAT, "history", False, via="chat").changed is False  # a double tap
    assert cs.chat_settings_path().read_bytes() == before

    store.set_flag(OWNER, CHAT, "dossier", False, via="cli", now=stamp)
    store.set_flag(OWNER, CHAT, "history", True, via="cli", now=stamp)
    assert stored(cs)["owners"]["1000"]["chats"]["991"] == {
        "dossier": "off", "updated_at": "2026-10-19T12:00:00Z", "updated_via": "cli"}
    assert store.set_flag(OWNER, CHAT, "dossier", True, via="cli").entry is None
    assert stored(cs) == {"version": 1, "owners": {}}
    assert store.counts() == {"history": 0, "dossier": 0}


def test_unknown_fields_survive_a_write(cs):
    cs.chat_settings_path().parent.mkdir(parents=True, exist_ok=True)
    cs.chat_settings_path().write_text(json.dumps({"version": 1, "future": [1], "owners": {"1000": {
        "extra": "kept", "chats": {"991": {"history": "off", "pinned": True}}}}}), encoding="utf-8")

    cs.default_store().set_flag(OWNER, CHAT, "dossier", False, via="cli")

    data = stored(cs)
    assert data["future"] == [1] and data["owners"]["1000"]["extra"] == "kept"
    assert {key: data["owners"]["1000"]["chats"]["991"][key] for key in ("history", "dossier", "pinned")} == {
        "history": "off", "dossier": "off", "pinned": True}


def test_owners_are_separate_and_an_unknown_owner_fails_closed(cs):
    store = cs.default_store()
    store.set_flag(OWNER, CHAT, "history", False, via="chat")

    assert store.is_off(OWNER, CHAT, "history") is True
    assert store.is_off(OTHER_OWNER, CHAT, "history") is False  # the same contact with another owner
    assert store.is_off(None, CHAT, "history") is True
    assert store.is_off(None, 992, "history") is False
    assert store.is_off(OWNER, CHAT, "dossier") is False
    assert store.excluded_anywhere("history", CHAT) and not store.excluded_anywhere("dossier", CHAT)
    assert cs.paused(BCID, CHAT, "dossier", owner=OWNER) is True  # a dossier needs the chat's history
    assert cs.paused(BCID, CHAT, "dossier", owner=OTHER_OWNER) is False


def test_store_never_writes_into_the_transcription_modes_file(cs, plugin):
    ttm.modes_file(plugin).parent.mkdir(parents=True, exist_ok=True)
    plugin._transcription_modes.default_store().set_mode(OWNER, CHAT, "peer", via="cli")
    before = ttm.modes_file(plugin).read_bytes()

    cs.default_store().set_flag(OWNER, CHAT, "history", False, via="cli")

    assert ttm.modes_file(plugin).read_bytes() == before  # an older Telesec reads that file unchanged
    assert cs.chat_settings_path().name == "chat-settings.json"


@pytest.mark.parametrize("payload", [
    [], {"owners": {}}, {"version": 2, "owners": {}}, {"version": 1, "owners": []},
    {"version": 1, "owners": {"abc": {"chats": {}}}}, {"version": 1, "owners": {"1000": {"chats": {"x": {}}}}},
    {"version": 1, "owners": {"1000": {"chats": {"991": {"history": "no"}}}}},
    {"version": 1, "owners": {"1000": {"chats": {"991": "off"}}}},
])
def test_strict_parse_rejects_bad_shapes(cs, payload):
    cs.chat_settings_path().parent.mkdir(parents=True, exist_ok=True)
    cs.chat_settings_path().write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(cs.ChatSettingsFileCorrupt, match="--owner ID' backs it up"):
        cs.default_store().read_strict()


def test_a_corrupt_file_keeps_the_last_valid_settings(cs, caplog):
    store = cs.default_store()
    store.set_flag(OWNER, CHAT, "history", False, via="chat")
    cs.chat_settings_path().write_bytes(GARBAGE)

    with caplog.at_level(logging.WARNING):
        assert store.is_off(OWNER, CHAT, "history") is True
        assert store.is_off(OWNER, 992, "history") is False
        assert store.file_degraded() is True
    assert len([r for r in caplog.records if "is corrupt" in r.getMessage()]) == 1  # once per damaged file
    assert "SECRET-CONTENT" not in caplog.text
    with pytest.raises(cs.ChatSettingsFileCorrupt):
        store.set_flag(OWNER, CHAT, "history", True, via="chat")
    assert cs.chat_settings_path().read_bytes() == GARBAGE


def test_a_corrupt_file_on_a_cold_start_pauses_every_chat(cs):
    cs.chat_settings_path().parent.mkdir(parents=True, exist_ok=True)
    cs.chat_settings_path().write_bytes(GARBAGE)
    store = cs.default_store()

    assert store.snapshot().blind is True
    assert store.excluded_anywhere("history", 12345) and store.is_off(OWNER, 12345, "dossier")

    cs.chat_settings_path().unlink()
    assert store.snapshot().blind is False and not store.excluded_anywhere("history", 12345)


def test_chats_per_owner_are_capped(cs, monkeypatch):
    monkeypatch.setattr(cs, "MAX_CHATS_PER_OWNER", 2)
    store = cs.default_store()
    store.set_flag(OWNER, 1, "history", False, via="cli")
    store.set_flag(OWNER, 2, "dossier", False, via="cli")

    with pytest.raises(ValueError, match="2 chats"):
        store.set_flag(OWNER, 3, "history", False, via="cli")
    store.set_flag(OWNER, 1, "dossier", False, via="cli")  # an existing chat can still change
    store.set_flag(OTHER_OWNER, 3, "history", False, via="cli")


# --- Capture ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dont_save_skips_new_messages_edits_and_deletions(cs, history_enabled):
    plugin = history_enabled
    await capture(plugin, text_update(77))
    await capture(plugin, text_update(77, chat_id=992))
    assert len(test_history.load_records(plugin)) == 1
    cs.default_store().set_flag(OWNER, CHAT, "history", False, via="chat")

    await capture(plugin, text_update(78))
    await capture(plugin, text_update(77, edited=True))
    await capture(plugin, test_history.make_deleted_update(message_ids=(77,)))
    await capture(plugin, text_update(78, chat_id=992))

    assert [r["message_id"] for r in test_history.load_records(plugin)] == [77]  # saved history is kept
    assert len(test_history.load_records(plugin, chat_id=992)) == 2  # other chats still record

    cs.default_store().set_flag(OWNER, CHAT, "history", True, via="chat")
    await capture(plugin, text_update(79))
    assert [r["message_id"] for r in test_history.load_records(plugin)] == [77, 79]


@pytest.mark.asyncio
async def test_capture_needs_no_owner_lookup_while_nothing_is_paused(cs, history_enabled, monkeypatch):
    def fail(_connection):
        raise AssertionError("owner looked up for an unpaused chat")

    monkeypatch.setattr(cs, "known_owner", fail)
    await capture(history_enabled, text_update(77))
    assert len(test_history.load_records(history_enabled)) == 1


@pytest.mark.asyncio
async def test_capture_fails_closed_for_an_unknown_owner(cs, history_enabled):
    plugin = history_enabled
    cs.default_store().set_flag(OWNER, CHAT, "history", False, via="chat")
    bot = test_history.FakeBot(owner_id=OTHER_OWNER)
    bot.lookup_error = RuntimeError("lookup failed")

    await capture(plugin, text_update(77, business_id=OTHER_BCID), bot)
    await capture(plugin, text_update(77, chat_id=992, business_id=OTHER_BCID), bot)
    assert test_history.load_records(plugin, business_id=OTHER_BCID) == []
    assert len(test_history.load_records(plugin, business_id=OTHER_BCID, chat_id=992)) == 1  # nobody paused it

    # Once the other owner is known, their chat with the same contact records again.
    bot.lookup_error = None
    plugin._history_support.reset_in_memory_caches()
    await capture(plugin, text_update(78, business_id=OTHER_BCID), bot)
    assert len(test_history.load_records(plugin, business_id=OTHER_BCID)) == 1


@pytest.mark.asyncio
async def test_capture_pauses_on_an_unreadable_store_or_a_failed_check(cs, history_enabled, monkeypatch, caplog):
    plugin = history_enabled
    cs.chat_settings_path().parent.mkdir(parents=True, exist_ok=True)
    cs.chat_settings_path().write_bytes(GARBAGE)
    await capture(plugin, text_update(77))
    assert test_history.load_records(plugin) == []

    cs.chat_settings_path().unlink()

    def broken(*_args):
        raise OSError("disk")

    monkeypatch.setattr(cs.ChatSettingsStore, "excluded_anywhere", broken)
    with caplog.at_level(logging.WARNING):
        await capture(plugin, text_update(78))
    assert test_history.load_records(plugin) == []
    assert "chat settings check failed (OSError); history not recorded" in caplog.text
    assert "hello" not in caplog.text


# --- Owner panel -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panel_shows_both_rows_in_the_transcription_style(both_on, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update(), bot) is True

    (call,) = bot.calls
    assert call["text"] == "Owner panel: chat 991\nTranscription: both (default)"
    assert top.keyboard(call["reply_markup"]) == [top.MODE_ROW, HISTORY_ROW, DOSSIER_ROW, REFRESH_ROW]


@pytest.mark.asyncio
async def test_dont_save_hides_the_dossier_row_and_a_double_tap_writes_nothing(both_on, plugin, caplog):
    cs = both_on
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    with caplog.at_level(logging.INFO):
        await tap(plugin, "tb:f:991:h.off", bot)

    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "History: off for this chat"}
    assert top.last_edit(bot) == ("Owner panel: chat 991\nTranscription: both (default)\nDossier: needs history", [
        top.MODE_ROW, [("Save history", "tb:f:991:h.on"), ("• Don't save", "tb:f:991:h.off")], REFRESH_ROW])
    assert stored(cs)["owners"]["1000"]["chats"]["991"]["updated_via"] == "chat"
    assert "action=f:h.off" in caplog.text

    before = cs.chat_settings_path().read_bytes()
    await tap(plugin, "tb:f:991:h.off", bot)
    assert cs.chat_settings_path().read_bytes() == before
    assert bot.answers[-1]["text"] == "History: off for this chat"

    await tap(plugin, "tb:f:991:h.on", bot)
    assert top.last_edit(bot)[1] == [top.MODE_ROW, HISTORY_ROW, DOSSIER_ROW, REFRESH_ROW]
    assert stored(cs) == {"version": 1, "owners": {}}


@pytest.mark.asyncio
async def test_no_dossier_keeps_history_on(both_on, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    await tap(plugin, "tb:f:991:d.off", bot)

    assert bot.answers[-1]["text"] == "Dossier: off for this chat"
    assert top.last_edit(bot)[1] == [
        top.MODE_ROW, HISTORY_ROW, [("Keep dossier", "tb:f:991:d.on"), ("• No dossier", "tb:f:991:d.off")],
        REFRESH_ROW]
    assert both_on.default_store().is_off(OWNER, CHAT, "dossier") is True
    assert both_on.default_store().is_off(OWNER, CHAT, "history") is False


@pytest.mark.asyncio
async def test_reset_stays_transcription_only(both_on, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)
    await tap(plugin, "tb:m:991:peer", bot)
    await tap(plugin, "tb:f:991:h.off", bot)

    await tap(plugin, "tb:r:991", bot)

    assert plugin._transcription_modes.default_store().get(str(OWNER), str(CHAT)) is None
    assert both_on.default_store().is_off(OWNER, CHAT, "history") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("switches", "env", "line", "rows"), [
    ({"history": False}, {}, "History: off in Settings", []),
    ({"history": True, "dossiers": False}, {}, "Dossier: off in Settings", [HISTORY_ROW]),
    ({"history": True, "dossiers": True}, {"TELESEC_HISTORY_CHATS": "-5"}, "History: off (server settings)", []),
])
async def test_rows_hidden_by_settings_show_a_line_and_refuse_a_stale_tap(cs, plugin, monkeypatch, switches, env,
                                                                           line, rows):
    for feature, on in switches.items():
        test_history.set_switch(plugin, feature, on)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    plugin._history_support.reset_in_memory_caches()
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update(), bot) is True
    assert bot.calls[0]["text"] == f"Owner panel: chat 991\nTranscription: both (default)\n{line}"
    assert top.keyboard(bot.calls[0]["reply_markup"]) == [top.MODE_ROW, *rows, REFRESH_ROW]

    await tap(plugin, "tb:f:991:d.off", bot)
    assert bot.answers[-1]["text"] == line
    assert not cs.chat_settings_path().exists()


def test_a_group_chat_gets_history_but_no_dossier_row(both_on, plugin, monkeypatch):
    chat_state = both_on.chat_state
    assert chat_state(str(OWNER), "-100123", connection=BCID, chat_type="supergroup") == ("server", "no_history")
    monkeypatch.setenv("TELESEC_HISTORY_CHAT_TYPES", "private,group,supergroup")
    assert chat_state(str(OWNER), "-100123", connection=BCID, chat_type="supergroup") == ("on", "private_only")

    lines, rows = plugin._owner_panel.feature_rows(991, ("on", "private_only"))
    assert (lines, rows) == ([], [HISTORY_ROW])
    assert plugin._owner_panel.flag_unavailable("dossier", ("on", "private_only")) == "Dossier: private chats only"


@pytest.mark.asyncio
async def test_a_corrupt_file_shows_the_last_values_and_a_tap_shows_the_save_error(both_on, plugin):
    cs = both_on
    cs.default_store().set_flag(OWNER, CHAT, "history", False, via="cli")
    cs.chat_settings_path().write_bytes(GARBAGE)
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    await tap(plugin, "tb:f:991:h.on", bot)

    assert bot.answers[-1]["text"] == SAVE_ERROR
    text, rows = top.last_edit(bot)
    assert text == f"Owner panel: chat 991\n{SAVE_ERROR}\nTranscription: both (default)\nDossier: needs history"
    assert rows[1] == [("Save history", "tb:f:991:h.on"), ("• Don't save", "tb:f:991:h.off")]
    assert cs.chat_settings_path().read_bytes() == GARBAGE
    assert list(cs.chat_settings_path().parent.glob("chat-settings.json.corrupt-*")) == []


@pytest.mark.asyncio
async def test_an_unreadable_file_on_a_cold_start_hides_both_rows(both_on, plugin):
    cs = both_on
    cs.chat_settings_path().parent.mkdir(parents=True, exist_ok=True)
    cs.chat_settings_path().write_bytes(GARBAGE)
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update(), bot) is True
    assert bot.calls[0]["text"] == f"Owner panel: chat 991\nTranscription: both (default)\n{UNREADABLE}"
    assert top.keyboard(bot.calls[0]["reply_markup"]) == [top.MODE_ROW, REFRESH_ROW]

    await tap(plugin, "tb:f:991:h.off", bot)
    assert bot.answers[-1]["text"] == SAVE_ERROR
    assert cs.chat_settings_path().read_bytes() == GARBAGE


@pytest.mark.asyncio
async def test_panel_rows_speak_russian(both_on, plugin):
    top.bind_owner(plugin)
    plugin._stats_panel.register_section()
    bot = top.PanelBot(business_owner_id=OWNER)

    await tap(plugin, "tb:f:991:h.off", bot, lang="ru")

    assert bot.answers[-1]["text"] == "История: выключена для этого чата"
    text, rows = top.last_edit(bot)
    assert text.endswith("\nДосье: нужна история")
    assert rows[1:] == [[("Сохранять историю", "tb:f:991:h.on"), ("• Не сохранять", "tb:f:991:h.off")],
                        [("📊 Статистика", "tb:s:991:stats"), ("Обновить", "tb:p:991")]]


def test_flag_callbacks_parse_strictly_and_fit_64_bytes(plugin):
    panel_module = plugin._owner_panel
    longest = panel_module.callback_data("f", -1009999999999999, "h.off")
    assert longest == "tb:f:-1009999999999999:h.off" and len(longest.encode()) <= 64
    assert panel_module.parse_callback(longest) == panel_module.PanelCallback("f", -1009999999999999, "h.off")
    for data in ("tb:f:991", "tb:f:991:h", "tb:f:991:x.on", "tb:f:991:h.maybe", "tb:f:991:h.on:extra"):
        assert panel_module.parse_callback(data) is None


# --- Dossiers --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_skips_a_paused_chat_and_keeps_its_dossier(dossier_plugin, dossiers):
    plug, ds = dossier_plugin, dossiers
    await seed(plug, 5504, "paused_contact", [(1, "in", "PAUSED-TAG question", 90), (2, "out", "PAUSED-TAG reply", 85)])
    await seed(plug, 5505, "open_contact", [(1, "in", "OPEN-TAG question", 80), (2, "out", "OPEN-TAG reply", 75)])
    assert cycle(ds, FakeLlm(quote_first, quote_first, quote_first)).refreshed == 2
    plug._chat_settings.default_store().set_flag(OWNER_ID, 5504, "dossier", False, via="cli")
    await seed(plug, 5504, "paused_contact", [(3, "in", "PAUSED-TAG later", 30), (4, "out", "PAUSED-TAG ok", 25)])
    await seed(plug, 5505, "open_contact", [(3, "in", "OPEN-TAG later", 30), (4, "out", "OPEN-TAG ok", 25)])
    paused_before = read_dossier(ds, 5504)
    assert paused_before is not None
    fake = FakeLlm(quote_first, quote_first, quote_first)

    report = cycle(ds, fake)

    payloads = json.dumps(fake.payloads())
    assert "OPEN-TAG later" in payloads and "PAUSED-TAG" not in payloads  # contact and persona calls alike
    assert report.orphans == 0 and read_dossier(ds, 5504) == paused_before  # frozen, never an orphan


@pytest.mark.asyncio
async def test_dont_save_also_pauses_the_dossier(dossier_plugin, dossiers):
    plug, ds = dossier_plugin, dossiers
    await seed(plug, 5504, "paused_contact", [(1, "in", "PAUSED-TAG question", 90), (2, "out", "PAUSED-TAG reply", 85)])
    plug._chat_settings.default_store().set_flag(OWNER_ID, 5504, "history", False, via="cli")
    fake = FakeLlm(quote_first, quote_first)

    cycle(ds, fake)

    assert "PAUSED-TAG" not in json.dumps(fake.payloads())


@pytest.mark.asyncio
async def test_run_and_preview_refuse_a_paused_contact(dossier_plugin, dossiers):
    plug, ds = dossier_plugin, dossiers
    await seed(plug, 5504, "paused_contact", [(1, "in", "PAUSED-TAG question", 90), (2, "out", "PAUSED-TAG reply", 85)])
    plug._chat_settings.default_store().set_flag(OWNER_ID, 5504, "dossier", False, via="cli")
    fake = FakeLlm()
    ds.bind_llm(fake)
    target = ("--connection", BUSINESS_ID, "--chat", "5504")

    code, out, _err = cli(plug, "dossier", "run", *target)
    assert (code, out.strip()) == (1, "error: Dossier is off for this chat.")
    code, out, err = cli(plug, "dossier", "preview", *target)
    assert code == 1 and "Dossier is off for this chat." in out + err
    assert fake.calls == []


@pytest.mark.asyncio
async def test_owner_command_in_a_paused_chat_tells_only_the_owner(dossier_plugin, dossiers, panel):
    await tdp.build(dossier_plugin, dossiers)
    dossier_plugin._chat_settings.default_store().set_flag(OWNER_ID, tdp.CHAT, "dossier", False, via="cli")
    bot = tdp.PanelBot()

    assert await tdp.run_command(panel, tdp.command_update(), bot) is True

    assert tdp.posts(bot) == []  # nothing goes into the Business chat
    (sent,) = bot.sent
    assert sent["chat_id"] == OWNER_ID and "Dossier is off for this chat." in sent["text"]


@pytest.mark.asyncio
async def test_panel_section_shows_the_frozen_dossier_without_refresh(dossier_plugin, dossiers, panel):
    await tdp.build(dossier_plugin, dossiers)
    dossiers.bind_llm(FakeLlm())
    assert "rf.c" in tdp.args_of(await tdp.act(panel))
    dossier_plugin._chat_settings.default_store().set_flag(OWNER_ID, tdp.CHAT, "dossier", False, via="cli")

    view = await tdp.act(panel)

    assert tdp.COMMITMENT in view.html and "Off for this chat." in view.text + view.html
    assert "rf.c" not in tdp.args_of(view)
    assert panel.start_refresh(BUSINESS_ID, tdp.CHAT, OWNER_ID) == "unavailable"
    refused = await tdp.act(panel, "rf.c")
    assert refused.notice == "Refresh is unavailable: dossiers or history are off, or the model is not connected."


def test_stop_refresh_stops_a_running_refresh(dossier_plugin, dossiers, panel):
    key = panel._refresh_key(dossiers.chat_key(BUSINESS_ID, tdp.CHAT))
    stop = threading.Event()
    panel._REFRESHES[key] = (SimpleNamespace(), stop)
    try:
        panel.stop_refresh(BUSINESS_ID, 992)
        assert not stop.is_set()
        panel.stop_refresh(BUSINESS_ID, tdp.CHAT)
        assert stop.is_set()
    finally:
        panel._REFRESHES.pop(key, None)


# --- Stats -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_section_says_history_is_paused(cs, history_enabled):
    plugin = history_enabled
    await capture(plugin, text_update(77))
    ctx = plugin._owner_panel.PanelContext(str(OWNER), str(CHAT), None, BCID)
    assert plugin._stats_panel.HISTORY_PAUSED not in plugin._stats_panel.render_section(ctx).text

    cs.default_store().set_flag(OWNER, CHAT, "history", False, via="cli")

    assert plugin._stats_panel.render_section(ctx).text.endswith("\nHistory is paused for this chat.")


# --- CLI -------------------------------------------------------------------------------------------


def test_chat_cli_sets_gets_and_lists(cs, plugin):
    top.bind_owner(plugin)
    test_history.set_switch(plugin, "history", True)

    assert ttm.run_cli(plugin, "chat", "list") == (
        0, "No chat has history or the dossier off; every chat follows Settings.")
    assert ttm.run_cli(plugin, "chat", "set", "991") == (1, "error: pass --history on|off, --dossier on|off, or both")
    code, out = ttm.run_cli(plugin, "chat", "set", "991", "--dossier", "off")
    assert code == 0
    assert out.splitlines() == ["chat=991 history=on dossier=off",
                                "The gateway applies it to the next update; no restart is needed."]
    code, out = ttm.run_cli(plugin, "chat", "get", "991")
    assert code == 0 and out.startswith("chat=991 history=on dossier=off (via cli at ")
    assert out.endswith("in effect: history=on dossier=settings")  # dossiers are off in Settings
    assert ttm.run_cli(plugin, "chat", "list")[1].startswith("owner=1000 chat=991 history=on dossier=off via=cli ")

    got = json.loads(ttm.run_cli(plugin, "chat", "get", "991", "--format", "json")[1])
    assert {key: got[key] for key in ("owner", "chat_id", "history", "dossier", "effective_history",
                                      "effective_dossier", "history_state", "dossier_state")} == {
        "owner": "1000", "chat_id": "991", "history": "on", "dossier": "off", "effective_history": "on",
        "effective_dossier": "off", "history_state": "on", "dossier_state": "settings"}
    listed = json.loads(ttm.run_cli(plugin, "chat", "list", "--format", "json")[1])
    assert (listed["history_enabled"], listed["dossiers_enabled"], len(listed["chats"])) == (True, False, 1)

    assert ttm.run_cli(plugin, "chat", "set", "991", "--dossier", "on")[0] == 0
    assert stored(cs) == {"version": 1, "owners": {}}
    assert ttm.run_cli(plugin, "chat") == (1, cs.CHAT_USAGE)


def test_chat_cli_repairs_a_corrupt_file_only_with_owner(cs, plugin):
    top.bind_owner(plugin)
    cs.chat_settings_path().parent.mkdir(parents=True, exist_ok=True)
    cs.chat_settings_path().write_bytes(GARBAGE)

    code, out = ttm.run_cli(plugin, "chat", "set", "991", "--history", "off")
    assert code == 1 and "is corrupt" in out and "SECRET-CONTENT" not in out
    assert ttm.run_cli(plugin, "chat", "list")[0] == 1

    code, out = ttm.run_cli(plugin, "chat", "set", "991", "--history", "off", "--owner", "1000")
    assert code == 0 and out.startswith("warning: corrupt chat-settings.json backed up to ")
    assert len(list(cs.chat_settings_path().parent.glob("chat-settings.json.corrupt-*"))) == 1
    assert cs.default_store().is_off(OWNER, CHAT, "history") is True


def test_mode_json_and_settings_show_the_chat_switches(cs, plugin):
    top.bind_owner(plugin)
    test_history.set_switch(plugin, "history", True)
    cs.default_store().set_flag(OWNER, CHAT, "history", False, via="cli")

    got = json.loads(ttm.run_cli(plugin, "mode", "get", "991", "--format", "json")[1])
    assert (got["effective_history"], got["effective_dossier"]) == ("off", "off")
    code, out = ttm.run_cli(plugin, "settings")
    assert code == 0 and "Chats with history off: 1; with the dossier off: 0" in out
    data = json.loads(ttm.run_cli(plugin, "settings", "--format", "json")[1])
    assert data["chat_overrides"] == {"transcription": 0, "history": 1, "dossier": 0}

    cs.chat_settings_path().write_bytes(GARBAGE)
    assert "warning: chat-settings.json is unreadable" in ttm.run_cli(plugin, "settings")[1]


def test_agent_reference_names_the_chat_command(plugin):
    assert "hermes telesec chat get <chat> --format json" in plugin.AGENT_REFERENCE
    assert len(plugin.AGENT_REFERENCE) <= 2000
