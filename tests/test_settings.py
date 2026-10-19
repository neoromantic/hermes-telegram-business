"""Global switches: settings.json, the one-time migration, `hermes telesec settings`, and the Settings screen."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_history
import test_owner_panel as top
import test_plugin
import test_transcription_modes as ttm

isolate_plugin_environment = test_plugin.isolate_plugin_environment
isolate_modes_environment = ttm.isolate_modes_environment
plugin = test_plugin.plugin
modes = ttm.modes

OWNER = ttm.OWNER
PEER = ttm.PEER
CHAT = ttm.CHAT
BCID = ttm.BCID
OTHER = "business-456"
set_switch = test_history.set_switch

MANAGER_ROWS = [
    [("• Transcribe", "tb:s:0:settings/t.on"), ("Don't transcribe", "tb:s:0:settings/t.off")],
    [("Save history", "tb:s:0:settings/h.on"), ("• Don't save", "tb:s:0:settings/h.off")],
    [("Keep dossiers", "tb:s:0:settings/d.ask"), ("• No dossiers", "tb:s:0:settings/d.off")],
]


@pytest.fixture
def gs(plugin, modes):
    plugin._settings_panel.register_section()
    yield plugin._global_settings
    plugin._dossier_support.stop_workers(timeout=2)


def stored(plugin) -> dict:
    return json.loads(plugin._global_settings.settings_path().read_text(encoding="utf-8"))


def ctx(plugin, owner=OWNER, connection=BCID, *, arg=None, chat="0", lang=None):
    return plugin._owner_panel.PanelContext(
        owner_user_id=str(owner), chat_id=chat, chat_label=None, business_connection_id=None if chat == "0" else connection,
        arg=arg, language_code=lang, owner_connections=(connection,),
    )


def rows(view) -> list[list[tuple[str, str]]]:
    return [list(row) for row in view.buttons]


def register_ctx(config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(llm=None, get_config=(config or {}).get, register_telegram_handler=Mock(),
                           register_hook=Mock(), register_cli_command=Mock(), register_skill=Mock(),
                           register_command=Mock())


# --- Migration ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "config", "expected"),
    [
        ({"TELESEC_HISTORY_ENABLE": "1"}, {}, {"history": True}),
        ({"TELESEC_HISTORY_ENABLE": "0"}, {}, {}),
        ({}, {"history_enable": True}, {"history": True}),  # Desktop
        ({}, {"history_enable": "yes"}, {"history": True}),
        ({"TELESEC_HISTORY_ENABLE": "0"}, {"history_enable": True}, {"history": False}),  # the variable wins
        ({"TELESEC_DISABLE": "1"}, {}, {"transcription": False}),
        ({"TELESEC_DISABLE": "0"}, {}, {}),
        ({"TELESEC_DISABLE": "true", "TELESEC_HISTORY_ENABLE": "yes"}, {}, {"transcription": False, "history": True}),
    ],
)
def test_migration_stores_only_values_that_differ_from_the_default(gs, plugin, monkeypatch, caplog, env, config,
                                                                  expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with caplog.at_level(logging.WARNING):
        moved = gs.migrate_legacy(get_config=config.get, now=datetime(2026, 10, 19, 9, tzinfo=UTC))

    assert sorted(moved) == sorted(expected)
    path = gs.settings_path()
    if not expected:
        assert not path.exists()
        return
    data = stored(plugin)
    assert data["schema_version"] == 1
    assert {feature: data[feature]["enabled"] for feature in expected} == expected
    assert all(data[feature]["updated_via"] == "migration" for feature in expected)
    assert path.stat().st_mode & 0o777 == 0o600
    for feature, value in expected.items():
        assert gs.enabled(feature) is value
        assert f"{feature} is {'on' if value else 'off'} (moved to Telesec settings)" in caplog.text
    assert "/telesec or 'hermes telesec settings set" in caplog.text


def test_migration_runs_once_and_never_overrides_a_stored_choice(gs, plugin, monkeypatch, caplog):
    set_switch(plugin, "history", False)  # the owner turned history off in Settings
    monkeypatch.setenv("TELESEC_HISTORY_ENABLE", "1")  # the service file still has the old line
    before = gs.settings_path().read_bytes()

    assert gs.migrate_legacy(get_config={}.get) == []
    assert gs.settings_path().read_bytes() == before and gs.enabled("history") is False

    with caplog.at_level(logging.WARNING):
        assert gs.warn_ignored_env() == ["TELESEC_HISTORY_ENABLE"]
        assert gs.warn_ignored_env() == []  # once per process
    assert caplog.text.count("TELESEC_HISTORY_ENABLE is no longer read; history is off (stored)") == 1


def test_a_cli_process_without_the_service_environment_never_writes(gs, plugin):
    plugin.register(register_ctx())

    assert not gs.settings_path().exists()
    assert (gs.enabled("transcription"), gs.enabled("history"), gs.enabled("dossiers")) == (True, False, False)


def test_a_variable_that_turns_off_desktop_history_keeps_it_off_later(gs, plugin, monkeypatch):
    desktop = {"history_enable": True}
    monkeypatch.setenv("TELESEC_HISTORY_ENABLE", "0")  # the gateway's unit overrides the Desktop setting
    plugin.register(register_ctx(desktop))
    assert stored(plugin)["history"]["enabled"] is False

    monkeypatch.delenv("TELESEC_HISTORY_ENABLE")  # an SSH shell, or the operator removed the variable
    plugin.register(register_ctx(desktop))

    assert gs.enabled("history") is False
    assert stored(plugin)["history"]["updated_via"] == "migration"


def test_a_cli_process_stores_the_desktop_history_setting(gs, plugin):
    plugin.register(register_ctx({"history_enable": True}))  # Desktop config, no service environment

    assert gs.enabled("history") is True
    assert stored(plugin)["history"]["updated_via"] == "migration"


def test_production_upgrade_keeps_history_transcription_and_dossiers_on(gs, plugin, monkeypatch, caplog):
    plugin._dossier_support.set_enabled(True, via="cli")  # stored by 0.11.0
    monkeypatch.setenv("TELESEC_HISTORY_ENABLE", "1")  # in the systemd unit

    with caplog.at_level(logging.WARNING):
        plugin.register(register_ctx())
        plugin.register(register_ctx())  # a restart, or a CLI run with the same environment

    assert (gs.enabled("transcription"), gs.enabled("history"), gs.dossiers_active()) == (True, True, True)
    assert plugin._history_support.history_config_from_env().active
    assert not plugin._disabled()
    assert set(stored(plugin)) == {"schema_version", "history"}
    assert caplog.text.count("TELESEC_HISTORY_ENABLE is no longer read; history is on (moved to Telesec settings)") == 1
    monkeypatch.delenv("TELESEC_HISTORY_ENABLE")  # the operator cleans up the unit later
    assert gs.enabled("history") is True


def test_migration_leaves_a_corrupt_file_alone(gs, plugin, monkeypatch):
    path = gs.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    monkeypatch.setenv("TELESEC_HISTORY_ENABLE", "1")

    assert gs.migrate_legacy(get_config={}.get) == []
    assert path.read_text(encoding="utf-8") == "{"


def test_register_adds_the_settings_command_fallback_and_section(gs, plugin):
    context = register_ctx()
    plugin.register(context)

    ((args, kwargs),) = [(call.args, call.kwargs) for call in context.register_command.call_args_list]
    assert args[0] == "telesec" and kwargs == {"description": "Telesec settings"}
    assert args[1]("") == ("Telesec settings are for the Business account owner, in the private chat with this "
                           "bot. In a terminal: hermes telesec settings.")
    section = plugin._owner_panel.get_section("settings")
    assert (section.order, plugin._owner_panel.section_label(section, "ru")) == (900, "⚙️ Настройки")
    assert "hermes telesec settings --format json" in plugin.AGENT_REFERENCE
    assert len(plugin.AGENT_REFERENCE) <= 2000


# --- Store --------------------------------------------------------------------------------------


def test_set_is_idempotent_and_keeps_unknown_keys(gs, plugin):
    path = gs.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "future": {"x": 1}}), encoding="utf-8")
    at = datetime(2026, 10, 19, 9, tzinfo=UTC)

    first = gs.set_enabled("history", True, via="telegram", now=at)
    again = gs.set_enabled("history", True, via="cli", now=at + timedelta(hours=1))

    assert (first["changed"], again["changed"], again["updated_via"]) == (True, False, "telegram")
    assert stored(plugin)["future"] == {"x": 1}
    status = gs.status()
    assert status["history"] == {"enabled": True, "source": "stored", "updated_at": first["updated_at"],
                                 "updated_via": "telegram"}
    assert status["transcription"]["source"] == "default" and status["dossiers"]["source"] == "default"
    with pytest.raises(ValueError):
        gs.set_enabled("history", True, via="env")
    with pytest.raises(ValueError):
        gs.enabled("voice")


def test_corrupt_file_keeps_the_last_valid_switches_and_only_the_cli_repairs_it(gs, plugin, caplog):
    set_switch(plugin, "history", True)
    set_switch(plugin, "transcription", False)
    assert (gs.enabled("history"), gs.enabled("transcription")) == (True, False)
    path = gs.settings_path()
    path.write_text("not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert (gs.enabled("history"), gs.enabled("transcription")) == (True, False)
        assert gs.enabled("history") is True
    assert caplog.text.count("is corrupt") == 1 and "keeping the last valid settings" in caplog.text
    assert gs.file_degraded() is True
    with pytest.raises(gs.SettingsFileCorrupt):
        gs.set_enabled("history", False, via="telegram")
    assert path.read_text(encoding="utf-8") == "not json"

    gs.reset_in_memory_caches()  # a cold start with the damaged file: defaults
    assert (gs.enabled("history"), gs.enabled("transcription")) == (False, True)

    code, out = ttm.run_cli(plugin, "settings", "set", "history", "on")
    assert code == 0
    assert "warning: corrupt settings.json backed up to" in out and "history: on" in out
    assert gs.file_degraded() is False and set(stored(plugin)) == {"schema_version", "history"}
    assert [p.name for p in path.parent.iterdir() if p.name.startswith("settings.json.corrupt")]


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_shows_and_sets_every_switch(gs, plugin, monkeypatch):
    code, out = ttm.run_cli(plugin, "settings")
    assert code == 0
    assert out.splitlines()[:4] == ["Telesec settings (all chats):", "transcription: on (default)",
                                    "history: off (default)", "dossiers: off (default)"]
    assert "Change: 'hermes telesec settings set <transcription|history|dossiers> on|off'" in out

    code, out = ttm.run_cli(plugin, "settings", "set", "dossiers", "on")
    assert code == 0
    assert "dossiers: on" in out and "sent to gemini / gemini-3.8-flash" in out
    assert "note: dossiers are paused while history is off" in out and "no restart is needed" in out

    assert ttm.run_cli(plugin, "settings", "set", "history", "on")[0] == 0
    assert ttm.run_cli(plugin, "settings", "set", "transcription", "off")[0] == 0
    assert ttm.run_cli(plugin, "settings", "set", "transcription", "off")[1].startswith("transcription: off (unchanged)")
    monkeypatch.setenv("TELESEC_DISABLE", "0")
    code, out = ttm.run_cli(plugin, "settings", "--format", "json")
    data = json.loads(out)
    assert code == 0
    assert (data["transcription"]["enabled"], data["history"]["enabled"], data["dossiers"]["enabled"]) == (
        False, True, True)
    assert data["transcription"]["updated_via"] == "cli" and data["dossiers"]["paused"] is False
    assert data["ignored_env"] == ["TELESEC_DISABLE"] and data["settings_file_unreadable"] is False
    assert data["chat_overrides"] == {"transcription": 0, "history": 0, "dossier": 0}
    assert plugin._disabled() is True  # the stored switch, not TELESEC_DISABLE=0


def test_dossier_cli_still_switches_and_reports_the_pause(gs, plugin):
    code, out = ttm.run_cli(plugin, "dossier", "enable")
    assert code == 0 and "note: history is off" in out
    assert gs.enabled("dossiers") is True
    code, out = ttm.run_cli(plugin, "dossier", "status")
    assert "Dossiers are paused while history is off" in out
    code, out = ttm.run_cli(plugin, "dossier", "run")
    assert code == 1 and "paused while history is off" in out
    assert ttm.run_cli(plugin, "dossier", "disable")[0] == 0 and gs.enabled("dossiers") is False


# --- Settings screen ------------------------------------------------------------------------------


def test_manager_view_from_telesec(gs, plugin):
    top.bind_owner(plugin)
    view = plugin._settings_panel.render(ctx(plugin))

    assert view.text == "Telesec settings · all chats\nOne chat: Manage Bot in that chat."
    assert rows(view) == MANAGER_ROWS
    ru = plugin._settings_panel.render(ctx(plugin, lang="ru"))
    assert ru.text == "Настройки Telesec · все чаты\nДля одного чата — кнопка управления ботом в нём."
    assert [label for row in ru.buttons for label, _data in row] == [
        "• Расшифровывать", "Не расшифровывать", "Сохранять историю", "• Не сохранять", "Вести досье", "• Не вести"]


def test_another_owner_sees_a_read_only_view_and_cannot_write(gs, plugin, modes):
    top.bind_owner(plugin)
    modes.default_store().bind_connection(OTHER, str(PEER))  # someone else connected the same bot

    view = plugin._settings_panel.render(ctx(plugin, PEER, OTHER))
    assert view.text == ("Telesec settings · all chats\nTranscription: on\nHistory: off\nDossiers: off\n"
                         "Only the owner of every connected account can change these.")
    assert view.buttons == []
    for arg in plugin._settings_panel.ARGS:  # a forged button
        forged = plugin._settings_panel.render(ctx(plugin, PEER, OTHER, arg=arg))
        assert forged.text.splitlines()[1] == "Not allowed" and forged.buttons == []
    assert not gs.settings_path().exists() and gs.enabled("dossiers") is False
    assert plugin._settings_panel.render(ctx(plugin, OWNER, BCID)).buttons == []  # OWNER does not own OTHER
    ru = plugin._settings_panel.render(ctx(plugin, PEER, OTHER, lang="ru"))
    assert "Расшифровка: вкл\nИстория: выкл\nДосье: выкл" in ru.text


def test_dossiers_ask_first_and_need_history(gs, plugin):
    top.bind_owner(plugin)
    panel = plugin._settings_panel

    refused = panel.render(ctx(plugin, arg="d.ask"))
    assert refused.text.splitlines()[1] == "Dossiers need history. Turn history on first."
    assert panel.render(ctx(plugin, arg="d.on")).text.splitlines()[1] == "Dossiers need history. Turn history on first."
    assert gs.enabled("dossiers") is False

    assert panel.render(ctx(plugin, arg="h.on")).text.splitlines()[1] == "History is on."
    confirm = panel.render(ctx(plugin, arg="d.ask"))
    assert confirm.text.startswith("Turn on dossiers?\n\nThe text of ALL private chats will be sent to gemini / ")
    assert rows(confirm) == [[("Enable", "tb:s:0:settings/d.yes"), ("Cancel", "tb:s:0:settings")]]
    assert gs.enabled("dossiers") is False  # asking writes nothing

    on = panel.render(ctx(plugin, arg="d.yes"))
    assert on.text.splitlines()[1] == "Dossiers are on."
    assert rows(on)[2] == [("• Keep dossiers", "tb:s:0:settings/d.on"), ("No dossiers", "tb:s:0:settings/d.off")]
    assert plugin._dossier_support.load_settings()["updated_via"] == "telegram"

    paused = panel.render(ctx(plugin, arg="h.off"))
    assert paused.text == ("Telesec settings · all chats\nHistory is off in all chats.\n"
                           "One chat: Manage Bot in that chat.\nDossiers are paused while history is off.")
    assert gs.enabled("dossiers") is True and gs.dossiers_active() is False
    assert not plugin._history_support.history_config_from_env().active

    off = panel.render(ctx(plugin, arg="d.off", lang="ru"))
    assert off.text.splitlines()[1] == "Досье выключены во всех чатах."
    assert "на паузе" not in off.text


def test_a_stale_keep_dossiers_button_asks_again(gs, plugin):
    top.bind_owner(plugin)
    panel = plugin._settings_panel
    set_switch(plugin, "history", True)
    set_switch(plugin, "dossiers", True)
    assert rows(panel.render(ctx(plugin)))[2][0] == ("• Keep dossiers", "tb:s:0:settings/d.on")
    set_switch(plugin, "dossiers", False)  # turned off since that message was sent

    stale = panel.render(ctx(plugin, arg="d.on"))

    assert stale.text.startswith("Turn on dossiers?")
    assert rows(stale)[0][0] == ("Enable", "tb:s:0:settings/d.yes")
    assert gs.enabled("dossiers") is False
    assert panel.render(ctx(plugin, arg="d.on", chat=str(CHAT))).text.startswith("Turn on dossiers?")
    assert gs.enabled("dossiers") is False


def test_switches_from_telegram_apply_without_a_restart(gs, plugin):
    top.bind_owner(plugin)
    panel = plugin._settings_panel
    assert panel.render(ctx(plugin, arg="t.off")).text.splitlines()[1] == "Transcription is off in all chats."
    assert plugin._disabled() is True and not plugin._voice_module_enabled()
    assert panel.render(ctx(plugin, arg="t.on", lang="ru")).text.splitlines()[1] == "Расшифровка включена."
    assert plugin._disabled() is False
    assert stored(plugin)["transcription"]["updated_via"] == "telegram"


def test_telegram_never_overwrites_a_corrupt_file(gs, plugin):
    top.bind_owner(plugin)
    path = gs.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")

    view = plugin._settings_panel.render(ctx(plugin, arg="h.on"))

    assert view.text.splitlines()[1] == (
        "Couldn't save: the settings file is unreadable (see hermes telesec settings).")
    assert path.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize("chat", ["0", "991", "-1001234567890", "-99999999999999999999"])
def test_every_settings_callback_fits_telegram(gs, plugin, chat):
    top.bind_owner(plugin)
    set_switch(plugin, "history", True)
    panel = plugin._settings_panel
    views = [panel.render(ctx(plugin, arg=arg, chat=chat)) for arg in (None, "d.ask")]
    views.append(plugin._owner_panel.with_back(views[0], chat))
    data = [d for view in views for row in view.buttons for _label, d in row]
    assert len(data) == 6 + 2 + 7
    for item in data:
        assert len(item.encode()) <= plugin._owner_panel.MAX_CALLBACK_BYTES
        assert plugin._owner_panel.parse_callback(item) is not None
    longest = f"tb:s:{chat}:settings/d.ask"
    assert longest in data and len(longest.encode()) <= 64


# --- /telesec and the router ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/telesec", "/telesec@HermesBizBot", "/telesec  "])
async def test_telesec_opens_settings_for_the_owner(gs, plugin, text):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update(text), bot) is True

    (call,) = bot.calls
    assert call["chat_id"] == OWNER
    assert call["text"] == "Telesec settings · all chats\nOne chat: Manage Bot in that chat."
    assert top.keyboard(call["reply_markup"]) == MANAGER_ROWS  # no Back row: there is no chat to go back to
    assert plugin._transcription_modes.default_store().get(str(OWNER), "0") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/telesec on", "/telesec\nhistory", "/Telesec", "/telesecx", " /telesec", "/start"])
async def test_only_an_exact_telesec_is_claimed(gs, plugin, text):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update(text), bot) is False
    assert bot.calls == []


@pytest.mark.asyncio
async def test_telesec_from_others_goes_to_core(gs, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update("/telesec", from_user_id=PEER), bot) is False
    assert await top.dispatch(plugin, top.start_update("/telesec", chat_type="group"), bot) is False
    assert bot.calls == []


@pytest.mark.asyncio
async def test_telesec_without_a_known_connection_hints_once(gs, plugin):
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update("/telesec", language_code="ru"), bot) is True
    assert await top.dispatch(plugin, top.start_update("/telesec", language_code="ru"), bot) is True

    assert bot.calls == [{"chat_id": OWNER, "text": (
        "Пока не вижу вашего Business-подключения. Отправьте любое сообщение в Business-чате и снова пришлите "
        "/telesec.")}]


@pytest.mark.asyncio
async def test_settings_buttons_on_the_telesec_message(gs, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.callback_update("tb:s:0:settings/h.on"), bot) is True

    assert bot.answers == [{"callback_query_id": "query-1"}]  # a switch is no news: no toast
    text, keyboard = top.last_edit(bot)
    assert text == "Telesec settings · all chats\nHistory is on.\nOne chat: Manage Bot in that chat."
    assert keyboard[1] == [("• Save history", "tb:s:0:settings/h.on"), ("Don't save", "tb:s:0:settings/h.off")]
    assert gs.enabled("history") is True

    assert await top.dispatch(plugin, top.callback_update("tb:s:0:settings"), bot) is True  # Cancel
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "⚙️ Settings"}


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["tb:m:0:off", "tb:r:0", "tb:b:0", "tb:p:0", "tb:s:0:stats", "tb:s:0:dossier/ds"])
async def test_chat_zero_accepts_only_settings(gs, plugin, data):
    top.bind_owner(plugin)
    plugin._stats_panel.register_section()
    bot = top.PanelBot(business_owner_id=OWNER)
    before = ttm.modes_file(plugin).read_bytes()

    assert await top.dispatch(plugin, top.callback_update(data), bot) is True

    assert bot.answers == [{"callback_query_id": "query-1", "text": "Unknown action"}]
    assert bot.text_edit_calls == []
    assert ttm.modes_file(plugin).read_bytes() == before
    assert plugin._transcription_modes.default_store().get(str(OWNER), "0") is None


@pytest.mark.asyncio
async def test_settings_from_a_chat_panel_keeps_its_back_button(gs, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.start_update(), bot) is True
    assert top.keyboard(bot.calls[0]["reply_markup"])[-1] == [("⚙️ Settings", f"tb:s:{CHAT}:settings"),
                                                            ("Refresh", f"tb:p:{CHAT}")]

    assert await top.dispatch(plugin, top.callback_update(f"tb:s:{CHAT}:settings/t.off"), bot) is True
    text, keyboard = top.last_edit(bot)
    assert text == "Telesec settings · all chats\nTranscription is off in all chats."  # no "One chat" line here
    assert keyboard[0] == [("Transcribe", f"tb:s:{CHAT}:settings/t.on"),
                           ("• Don't transcribe", f"tb:s:{CHAT}:settings/t.off")]
    assert keyboard[-1] == [("« Back", f"tb:b:{CHAT}")]

    assert await top.dispatch(plugin, top.callback_update(f"tb:b:{CHAT}"), bot) is True
    assert top.last_edit(bot)[0] == "Owner panel: chat 991\nTranscription: off in Settings\nHistory: off in Settings"


@pytest.mark.asyncio
async def test_telesec_in_a_business_chat_never_reaches_core(gs, plugin):
    # With TELESEC_AGENT_ENABLE, core would answer its /telesec fallback in the contact's chat.
    message = ttm.make_message()
    message.voice = None
    for text, skipped in (("/telesec", True), ("/telesec on", True), ("/telesec@HermesBizBot", True),
                          ("/TELESEC", True), ("about /telesec", False), ("/telesecx", False)):
        message.text = text
        result = plugin._on_pre_gateway_dispatch(ttm.make_event(message), None)
        assert result == ({"action": "skip", "reason": "telesec_command_in_business_chat"} if skipped else None)


# --- History off --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_is_found_from_the_catalog_while_history_is_off(gs, plugin):
    set_switch(plugin, "history", True)
    update = test_history.make_business_text_update(business_id=BCID, chat_id=CHAT, from_user_id=PEER)
    assert await plugin._history_support.observe_ptb_update(update, bot=test_history.FakeBot(owner_id=OWNER))
    set_switch(plugin, "history", False)
    plugin._history_support.reset_in_memory_caches()  # a restart: only the catalog knows the connection

    assert [entry["chat_id"] for entry in plugin._panel_catalog()] == [CHAT]
    bot = top.PanelBot(business_owner_id=OWNER)
    assert await top.dispatch(plugin, top.start_update("/telesec"), bot) is True
    assert bot.calls[0]["text"].startswith("Telesec settings · all chats")


@pytest.mark.asyncio
async def test_maintenance_runs_while_history_is_off(gs, plugin, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin._history_support, "maintain_history",
                        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(cap_exceeded=False, warnings=[]))
    monkeypatch.setattr(plugin._history_support, "_should_run_throttled_maintenance", lambda _now: True)
    update = test_history.make_business_text_update()

    assert await plugin._history_support.observe_ptb_update(update, bot=test_history.FakeBot()) is False
    assert calls == []  # no history directory yet, so nothing is created
    plugin._history_support.history_root().mkdir(parents=True)

    assert await plugin._history_support.observe_ptb_update(update, bot=test_history.FakeBot()) is False
    assert len(calls) == 1  # retention still prunes what was captured before


def test_the_dossier_worker_pauses_while_history_is_off(gs, plugin):
    ds = plugin._dossier_support
    ds.set_enabled(True, via="cli")

    report = ds.run_cycle(llm=None)

    assert report.outcome == "history_off"
    assert ds._config_if_on() is None
