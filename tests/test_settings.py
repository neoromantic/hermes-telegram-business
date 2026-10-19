"""Global switches and retention: settings.json, the one-time migration, `hermes telesec settings`, and the Settings
screen."""

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
set_retention = test_history.set_retention

RETENTION_ROW = [("• Forever", "tb:s:0:settings/r.0"), ("1 year", "tb:s:0:settings/r.365"),
                 ("90 days", "tb:s:0:settings/r.90"), ("30 days", "tb:s:0:settings/r.30")]
MANAGER_ROWS = [
    [("• Transcribe", "tb:s:0:settings/t.on"), ("Don't transcribe", "tb:s:0:settings/t.off")],
    [("Save history", "tb:s:0:settings/h.on"), ("• Don't save", "tb:s:0:settings/h.off")],
    RETENTION_ROW,
    [("Keep dossiers", "tb:s:0:settings/d.ask"), ("• No dossiers", "tb:s:0:settings/d.off")],
]
MODEL_LINES = "Dossier model: gemini / gemini-3.8-flash\nVoice cleanup: gemini / gemini-3.8-flash"
MANAGER_TAIL = f"Keep history: forever\n{MODEL_LINES}"
MANAGER_TEXT = f"Telesec settings · all chats\nOne chat: Manage Bot in that chat.\n{MANAGER_TAIL}"


@pytest.fixture
def gs(plugin, modes):
    plugin._settings_panel.register_section(voice_cleanup=plugin._voice_cleanup_model)  # as register() does
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
    assert gs.file_degraded() is False and set(stored(plugin)) == {"schema_version", "history", "retention"}
    assert [p.name for p in path.parent.iterdir() if p.name.startswith("settings.json.corrupt")]


# --- CLI ----------------------------------------------------------------------------------------


def test_cli_shows_and_sets_every_switch(gs, plugin, monkeypatch):
    code, out = ttm.run_cli(plugin, "settings")
    assert code == 0
    assert out.splitlines()[:5] == ["Telesec settings (all chats):", "transcription: on (default)",
                                    "history: off (default)", "retention: forever (default)", "dossiers: off (default)"]
    assert ("Change: 'hermes telesec settings set <transcription|history|dossiers> on|off', "
            "'hermes telesec settings set retention forever|365|90|30|N'") in out

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
    assert data["chat_overrides"] == {"transcription": 0, "history": 0, "dossier": 0, "history_on": 0, "old_scope": 0}
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

    assert view.text == MANAGER_TEXT
    assert rows(view) == MANAGER_ROWS
    ru = plugin._settings_panel.render(ctx(plugin, lang="ru"))
    assert ru.text == ("Настройки Telesec · все чаты\nДля одного чата — кнопка управления ботом в нём.\n"
                       "Хранить историю: всегда\n"
                       "Модель досье: gemini / gemini-3.8-flash\nОчистка расшифровок: gemini / gemini-3.8-flash")
    assert [[label for label, _data in row] for row in ru.buttons] == [
        ["• Расшифровывать", "Не расшифровывать"], ["Сохранять историю", "• Не сохранять"],
        ["• Всегда", "1 год", "90 дней", "30 дней"], ["Вести досье", "• Не вести"]]


def test_another_owner_sees_a_read_only_view_and_cannot_write(gs, plugin, modes):
    top.bind_owner(plugin)
    modes.default_store().bind_connection(OTHER, str(PEER))  # someone else connected the same bot

    view = plugin._settings_panel.render(ctx(plugin, PEER, OTHER))
    assert view.text == ("Telesec settings · all chats\nTranscription: on\nHistory: off\nKeep history: forever\n"
                         f"Dossiers: off\n{MODEL_LINES}\nOnly the owner of every connected account can change these.")
    assert view.buttons == []
    for arg in plugin._settings_panel.ARGS:  # a forged button
        forged = plugin._settings_panel.render(ctx(plugin, PEER, OTHER, arg=arg))
        assert forged.text.splitlines()[1] == "Not allowed" and forged.buttons == []
    assert not gs.settings_path().exists() and gs.enabled("dossiers") is False
    assert plugin._settings_panel.render(ctx(plugin, OWNER, BCID)).buttons == []  # OWNER does not own OTHER
    ru = plugin._settings_panel.render(ctx(plugin, PEER, OTHER, lang="ru"))
    assert "Расшифровка: вкл\nИстория: выкл\nХранить историю: всегда\nДосье: выкл" in ru.text


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
    assert rows(on)[3] == [("• Keep dossiers", "tb:s:0:settings/d.on"), ("No dossiers", "tb:s:0:settings/d.off")]
    assert plugin._dossier_support.load_settings()["updated_via"] == "telegram"

    paused = panel.render(ctx(plugin, arg="h.off"))
    assert paused.text == ("Telesec settings · all chats\nHistory is off in all chats.\n"
                           f"One chat: Manage Bot in that chat.\nDossiers are paused while history is off.\n{MANAGER_TAIL}")
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
    assert rows(panel.render(ctx(plugin)))[3][0] == ("• Keep dossiers", "tb:s:0:settings/d.on")
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
    gs.set_retention(gs.MAX_RETENTION_DAYS, via="cli")  # the longest day count gets its own button
    panel = plugin._settings_panel
    views = [panel.render(ctx(plugin, arg=arg, chat=chat)) for arg in (None, "d.ask", "r.365", "r.30")]
    views.append(plugin._owner_panel.with_back(views[0], chat))
    data = [d for view in views for row in view.buttons for _label, d in row]
    assert len(data) == 11 + 2 + 2 + 2 + 12
    for item in data:
        assert len(item.encode()) <= plugin._owner_panel.MAX_CALLBACK_BYTES
        assert plugin._owner_panel.parse_callback(item) is not None
    for longest in (f"tb:s:{chat}:settings/r.36500", f"tb:s:{chat}:settings/r.365.yes"):
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
    assert call["text"] == MANAGER_TEXT
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
    assert text == f"Telesec settings · all chats\nHistory is on.\nOne chat: Manage Bot in that chat.\n{MANAGER_TAIL}"
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
    assert text == f"Telesec settings · all chats\nTranscription is off in all chats.\n{MANAGER_TAIL}"  # no "One chat"
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


# --- Retention ------------------------------------------------------------------------------------


def retention_labels(view) -> list[str]:
    return [label for label, _data in rows(view)[2]]


def test_retention_row_for_a_manager_and_read_only(gs, plugin, modes):
    top.bind_owner(plugin)
    set_retention(plugin, 90)
    panel = plugin._settings_panel

    assert rows(panel.render(ctx(plugin)))[2] == [
        ("Forever", "tb:s:0:settings/r.0"), ("1 year", "tb:s:0:settings/r.365"),
        ("• 90 days", "tb:s:0:settings/r.90"), ("30 days", "tb:s:0:settings/r.30")]
    manager_ru = panel.render(ctx(plugin, lang="ru"))
    assert retention_labels(manager_ru) == ["Всегда", "1 год", "• 90 дней", "30 дней"]
    assert "Хранить историю: 90 дней" in manager_ru.text.splitlines()  # readable when the row is cut off

    modes.default_store().bind_connection(OTHER, str(PEER))
    read_only = panel.render(ctx(plugin, PEER, OTHER))
    assert "History: off\nKeep history: 90 days\nDossiers: off" in read_only.text and read_only.buttons == []
    for arg in ("r.30", "r.30.yes", "r.0", "r.90"):  # forged buttons
        forged = panel.render(ctx(plugin, PEER, OTHER, arg=arg))
        assert forged.text.splitlines()[1] == "Not allowed" and forged.buttons == []
    assert gs.retention_days() == 90 and stored(plugin)["retention"]["updated_via"] == "cli"
    set_retention(plugin, 365)
    assert "Хранить историю: 1 год" in panel.render(ctx(plugin, PEER, OTHER, lang="ru")).text


def test_shorter_retention_asks_first_and_longer_applies_at_once(gs, plugin):
    top.bind_owner(plugin)
    panel = plugin._settings_panel

    confirm = panel.render(ctx(plugin, arg="r.30"))
    assert confirm.text == ("Keep 30 days?\n\nSaved messages are deleted a whole month at a time: a month is deleted "
                            "at the next cleanup once all of it is older than that.")
    assert rows(confirm) == [[("Keep 30 days", "tb:s:0:settings/r.30.yes"), ("Cancel", "tb:s:0:settings")]]
    assert not gs.settings_path().exists() and gs.retention_days() == 0  # asking writes nothing
    ru = panel.render(ctx(plugin, arg="r.365", lang="ru"))
    assert ru.text.startswith("Хранить 1 год?\n\nСохранённые сообщения удаляются целыми месяцами")
    assert rows(ru) == [[("Хранить 1 год", "tb:s:0:settings/r.365.yes"), ("Отмена", "tb:s:0:settings")]]

    done = panel.render(ctx(plugin, arg="r.30.yes"))
    assert done.text.splitlines()[1] == "History is kept 30 days."
    assert retention_labels(done) == ["Forever", "1 year", "90 days", "• 30 days"]
    assert "Keep history: 30 days" in done.text.splitlines()
    assert stored(plugin)["retention"]["days"] == 30 and stored(plugin)["retention"]["updated_via"] == "telegram"

    longer = panel.render(ctx(plugin, arg="r.90"))  # longer: no question
    assert longer.text.splitlines()[1] == "History is kept 90 days." and gs.retention_days() == 90
    assert panel.render(ctx(plugin, arg="r.30")).text.startswith("Keep 30 days?")  # shorter again: asks
    forever = panel.render(ctx(plugin, arg="r.0", lang="ru"))
    assert forever.text.splitlines()[1] == "История хранится всегда." and gs.retention_days() == 0


def test_retention_stays_while_history_is_off_and_turning_it_off_keeps_it(gs, plugin):
    top.bind_owner(plugin)
    set_switch(plugin, "history", True)
    panel = plugin._settings_panel
    panel.render(ctx(plugin, arg="r.365.yes"))

    off = panel.render(ctx(plugin, arg="h.off"))

    assert retention_labels(off)[1] == "• 1 year" and gs.retention_days() == 365
    assert set(stored(plugin)) == {"schema_version", "history", "retention"}
    assert stored(plugin)["history"] == {"enabled": False, "updated_at": stored(plugin)["history"]["updated_at"],
                                         "updated_via": "telegram"}


def test_stale_retention_taps_are_harmless(gs, plugin):
    top.bind_owner(plugin)
    panel = plugin._settings_panel
    set_retention(plugin, 30)
    before = stored(plugin)["retention"]

    same = panel.render(ctx(plugin, arg="r.30"))  # a double tap: already in effect, nothing to ask
    assert same.text.splitlines()[1] == "History is kept 30 days."
    assert stored(plugin)["retention"] == before

    set_retention(plugin, 90)  # changed elsewhere since the message was sent
    assert panel.render(ctx(plugin, arg="r.30")).text.startswith("Keep 30 days?")  # judged against 90, asks again
    assert gs.retention_days() == 90
    assert panel.render(ctx(plugin, arg="r.0")).text.splitlines()[1] == "History is kept forever."

    for forged in ("r.7", "r.45", "r.45.yes", "r.0.yes", "r.030", "r.-1", "r.30.no", "r.99999"):
        view = panel.render(ctx(plugin, arg=forged))
        assert view.text == MANAGER_TEXT and gs.retention_days() == 0, forged


def test_a_migrated_value_has_its_own_choice_until_changed(gs, plugin, monkeypatch, caplog):
    top.bind_owner(plugin)
    panel = plugin._settings_panel
    monkeypatch.setenv("TELESEC_HISTORY_RETENTION_DAYS", "45")
    assert gs.migrate_legacy(get_config={}.get) == ["retention"]

    view = panel.render(ctx(plugin))
    assert rows(view)[2] == [
        ("Forever", "tb:s:0:settings/r.0"), ("1 year", "tb:s:0:settings/r.365"),
        ("90 days", "tb:s:0:settings/r.90"), ("• 45 days", "tb:s:0:settings/r.45"), ("30 days", "tb:s:0:settings/r.30")]
    assert panel.render(ctx(plugin, arg="r.45")).text.splitlines()[1] == "History is kept 45 days."
    assert stored(plugin)["retention"]["updated_via"] == "migration"  # a tap on the value in effect writes nothing
    assert panel.render(ctx(plugin, arg="r.30")).text.startswith("Keep 30 days?")

    assert panel.render(ctx(plugin, arg="r.90")).text.splitlines()[1] == "History is kept 90 days."
    assert retention_labels(panel.render(ctx(plugin))) == ["Forever", "1 year", "• 90 days", "30 days"]
    panel.render(ctx(plugin, arg="r.45"))  # the old "• 45 days" button: no longer a choice
    assert gs.retention_days() == 90


@pytest.mark.parametrize(("days", "en", "ru"), [
    (1, "• 1 day", "• 1 день"), (2, "• 2 days", "• 2 дня"), (5, "• 5 days", "• 5 дней"), (11, "• 11 days", "• 11 дней"),
    (21, "• 21 days", "• 21 день"), (22, "• 22 days", "• 22 дня"), (112, "• 112 days", "• 112 дней"),
    (730, "• 730 days", "• 730 дней"),
])
def test_a_value_outside_the_choices_reads_naturally(gs, plugin, days, en, ru):
    top.bind_owner(plugin)
    set_retention(plugin, days)

    assert en in retention_labels(plugin._settings_panel.render(ctx(plugin)))
    assert ru in retention_labels(plugin._settings_panel.render(ctx(plugin, lang="ru")))


def test_telegram_never_overwrites_a_corrupt_file_with_retention(gs, plugin):
    top.bind_owner(plugin)
    path = gs.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")

    view = plugin._settings_panel.render(ctx(plugin, arg="r.0"))

    assert view.text.splitlines()[1] == "Couldn't save: the settings file is unreadable (see hermes telesec settings)."
    assert path.read_text(encoding="utf-8") == "{"


@pytest.mark.asyncio
async def test_retention_buttons_on_the_telesec_message(gs, plugin):
    top.bind_owner(plugin)
    bot = top.PanelBot(business_owner_id=OWNER)

    assert await top.dispatch(plugin, top.callback_update("tb:s:0:settings/r.90"), bot) is True
    text, keyboard = top.last_edit(bot)
    assert text.startswith("Keep 90 days?") and keyboard[0][0] == ("Keep 90 days", "tb:s:0:settings/r.90.yes")

    assert await top.dispatch(plugin, top.callback_update("tb:s:0:settings/r.90.yes"), bot) is True
    text, keyboard = top.last_edit(bot)
    assert text.splitlines()[1] == "History is kept 90 days."
    assert keyboard[2][2] == ("• 90 days", "tb:s:0:settings/r.90")
    assert gs.retention_days() == 90


@pytest.mark.parametrize(("raw", "expected"), [
    ("30", 30), ("400", 400), ("36500", 36500), (" 7 ", 7),
    ("0", None), ("-5", None), ("abc", None), ("", None), ("12.5", None), ("36501", None),
])
def test_retention_migration_stores_only_a_shorter_than_forever_value(gs, plugin, monkeypatch, caplog, raw, expected):
    monkeypatch.setenv("TELESEC_HISTORY_RETENTION_DAYS", raw)
    with caplog.at_level(logging.WARNING):
        moved = gs.migrate_legacy(get_config={}.get, now=datetime(2026, 10, 20, 9, tzinfo=UTC))

    if expected is None:
        assert moved == [] and not gs.settings_path().exists() and gs.retention_days() == 0
        return
    assert moved == ["retention"]
    assert stored(plugin)["retention"] == {"days": expected, "updated_at": "2026-10-20T09:00:00Z",
                                           "updated_via": "migration"}
    assert gs.retention_days() == expected
    assert gs.status()["retention"]["source"] == "migration"
    assert (f"TELESEC_HISTORY_RETENTION_DAYS is no longer read; history is kept {expected} days (moved to Telesec "
            "settings). Use /telesec or 'hermes telesec settings set retention forever|365|90|30|N'.") in caplog.text


def test_retention_migration_runs_once_and_never_overrides_a_stored_choice(gs, plugin, monkeypatch, caplog):
    set_retention(plugin, 365)  # the owner chose a year in Settings
    monkeypatch.setenv("TELESEC_HISTORY_RETENTION_DAYS", "30")  # the service file still has the old line
    before = gs.settings_path().read_bytes()

    assert gs.migrate_legacy(get_config={}.get) == []
    assert gs.settings_path().read_bytes() == before and gs.retention_days() == 365

    with caplog.at_level(logging.WARNING):
        assert gs.warn_ignored_env() == ["TELESEC_HISTORY_RETENTION_DAYS"]
        assert gs.warn_ignored_env() == []  # once per process
    assert caplog.text.count("TELESEC_HISTORY_RETENTION_DAYS is no longer read; history is kept 365 days (stored)") == 1


def test_register_moves_retention_with_history_and_warns_once(gs, plugin, monkeypatch, caplog):
    monkeypatch.setenv("TELESEC_HISTORY_ENABLE", "1")
    monkeypatch.setenv("TELESEC_HISTORY_RETENTION_DAYS", "90")

    with caplog.at_level(logging.WARNING):
        plugin.register(register_ctx())
        plugin.register(register_ctx())  # a restart with the same environment

    assert set(stored(plugin)) == {"schema_version", "history", "retention"}
    assert (gs.enabled("history"), gs.retention_days()) == (True, 90)
    assert caplog.text.count("history is kept 90 days (moved to Telesec settings)") == 1
    assert plugin._history_support.history_config_from_env().retention_days == 90
    monkeypatch.delenv("TELESEC_HISTORY_RETENTION_DAYS")
    assert gs.retention_days() == 90


def test_a_0_12_settings_file_gets_the_retention_once(gs, plugin, monkeypatch):
    path = gs.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = {"schema_version": 1,
           "transcription": {"enabled": False, "updated_at": "2026-10-19T09:00:00Z", "updated_via": "telegram"},
           "history": {"enabled": True, "updated_at": "2026-10-19T09:00:00Z", "updated_via": "migration"}}
    path.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setenv("TELESEC_HISTORY_RETENTION_DAYS", "30")

    assert gs.migrate_legacy(get_config={}.get, now=datetime(2026, 10, 20, 9, tzinfo=UTC)) == ["retention"]

    data = stored(plugin)
    assert {key: data[key] for key in ("schema_version", "transcription", "history")} == old
    assert data["retention"] == {"days": 30, "updated_at": "2026-10-20T09:00:00Z", "updated_via": "migration"}
    after = path.read_bytes()
    assert gs.migrate_legacy(get_config={}.get) == []
    assert path.read_bytes() == after


@pytest.mark.parametrize("reset", ["repair", "delete"])
def test_a_reset_settings_file_never_takes_the_old_retention_back(gs, plugin, monkeypatch, reset):
    monkeypatch.setenv("TELESEC_HISTORY_RETENTION_DAYS", "30")  # left in the service file
    assert gs.migrate_legacy(get_config={}.get) == ["retention"]
    gs.set_retention(0, via="telegram")  # the owner keeps history forever
    path = gs.settings_path()
    if reset == "repair":
        path.write_text("{", encoding="utf-8")
    else:
        path.unlink()

    code, out = ttm.run_cli(plugin, "settings", "set", "history", "on")  # starts a new file
    assert code == 0 and ("backed up" in out) is (reset == "repair")
    assert stored(plugin)["retention"]["days"] == 0 and stored(plugin)["retention"]["updated_via"] == "cli"

    assert gs.migrate_legacy(get_config={}.get) == []  # the next gateway start
    assert gs.retention_days() == 0 and gs.enabled("history") is True


def test_a_stored_retention_does_not_block_the_history_migration(gs, plugin, monkeypatch):
    set_retention(plugin, 30)  # from a CLI run before the gateway's first 0.13 start
    monkeypatch.setenv("TELESEC_HISTORY_ENABLE", "1")

    assert gs.migrate_legacy(get_config={}.get) == ["history"]
    assert (gs.enabled("history"), gs.retention_days()) == (True, 30)


def test_a_corrupt_retention_entry_keeps_the_last_valid_settings(gs, plugin, caplog):
    set_retention(plugin, 90)
    assert gs.retention_days() == 90
    data = stored(plugin)
    data["retention"]["days"] = "90"
    gs.settings_path().write_text(json.dumps(data), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert gs.retention_days() == 90
    assert "invalid retention entry" in caplog.text
    gs.reset_in_memory_caches()
    assert gs.retention_days() == 0


CLEANUP_LINE = ("Cleanup runs when the gateway starts, when a hermes command loads Telesec, and at most hourly while "
                "messages arrive; no restart is needed.")


def test_cli_sets_and_shows_retention(gs, plugin):
    code, out = ttm.run_cli(plugin, "settings", "set", "retention", "30")
    assert code == 0
    assert out.splitlines() == [
        "retention: 30 days",
        ("Saved history is deleted a whole month at a time: a month goes at the next cleanup once all of it is "
         "older than 30 days."),
        CLEANUP_LINE]
    assert ttm.run_cli(plugin, "settings", "set", "retention", "30")[1].startswith("retention: 30 days (unchanged)")
    code, out = ttm.run_cli(plugin, "settings")
    assert code == 0 and "retention: 30 days (stored via cli at " in out

    assert ttm.run_cli(plugin, "settings", "set", "retention", "Forever")[1].splitlines() == [
        "retention: forever", CLEANUP_LINE]
    assert gs.retention_days() == 0
    for raw, days in (("365", 365), ("90", 90), ("1", 1), ("45", 45)):
        assert ttm.run_cli(plugin, "settings", "set", "retention", raw)[0] == 0 and gs.retention_days() == days
    code, out = ttm.run_cli(plugin, "settings", "--format", "json")
    assert json.loads(out)["retention"] == {"days": 45, "source": "stored",
                                            "updated_at": stored(plugin)["retention"]["updated_at"],
                                            "updated_via": "cli"}

    for raw in ("-1", "abc", "36501", "1y", "on", "٣٠"):
        code, out = ttm.run_cli(plugin, "settings", "set", "retention", raw)
        assert code == 1 and out.startswith("error: retention must be forever or 0..36500 days"), raw
    code, out = ttm.run_cli(plugin, "settings", "set", "history", "30")
    assert code == 1 and out.startswith("error: history takes on or off")
    assert gs.retention_days() == 45 and gs.enabled("history") is False


@pytest.mark.asyncio
async def test_maintenance_prunes_per_the_stored_retention_without_a_restart(gs, plugin, monkeypatch):
    history = plugin._history_support
    set_switch(plugin, "history", True)
    months = {}
    for index, when in enumerate((datetime(2026, 5, 10, tzinfo=UTC), datetime(2026, 7, 10, tzinfo=UTC),
                                  datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 10, 10, tzinfo=UTC)), start=1):
        update = test_history.make_business_text_update(text=f"note {index}", message_id=index, update_id=index,
                                                        date=when)
        assert await history.observe_ptb_update(update, bot=test_history.FakeBot(), now=when)
        months[when.month] = test_history.load_history_file(plugin, month=f"2026-{when.month:02d}.jsonl")
    now = datetime(2026, 10, 20, 9, tzinfo=UTC)

    assert history.maintain_history(now=now).pruned_files == 0  # forever, the default
    assert ttm.run_cli(plugin, "settings", "set", "retention", "90")[0] == 0  # another process, like the CLI
    assert history.maintain_history(now=now).pruned_files == 1
    assert [month for month, path in months.items() if path.exists()] == [7, 9, 10]

    # From Telegram: 30 days keeps September (all of it is not older than 30 days yet) and October.
    top.bind_owner(plugin)
    plugin._settings_panel.render(ctx(plugin, arg="r.30.yes"))
    assert history.maintain_history(now=now).pruned_files == 1
    assert [month for month, path in months.items() if path.exists()] == [9, 10]
    assert history.maintain_history(now=datetime(2026, 11, 1, tzinfo=UTC)).pruned_files == 1
    assert [month for month, path in months.items() if path.exists()] == [10]  # not all of October is 30 days old


@pytest.mark.asyncio
async def test_hourly_maintenance_reads_the_retention_in_effect(gs, plugin, monkeypatch):
    seen = []
    history = plugin._history_support
    monkeypatch.setattr(history, "_should_run_throttled_maintenance", lambda _now: True)
    real = history.maintain_history

    def spy(**kwargs):
        seen.append(history.history_config_from_env().retention_days)
        return real(**kwargs)

    monkeypatch.setattr(history, "maintain_history", spy)
    history.history_root().mkdir(parents=True)
    update = test_history.make_business_text_update()
    await history.observe_ptb_update(update, bot=test_history.FakeBot())
    set_retention(plugin, 365)
    await history.observe_ptb_update(update, bot=test_history.FakeBot())

    assert seen == [0, 365]


# --- Models -------------------------------------------------------------------------------------


def test_model_lines_show_the_configured_models(gs, plugin, monkeypatch):
    top.bind_owner(plugin)
    monkeypatch.setenv("TELESEC_CLEANUP_PROVIDER", "openrouter")
    monkeypatch.setenv("TELESEC_CLEANUP_MODEL", "example/cleanup-small")
    monkeypatch.setenv("TELESEC_DOSSIER_MODEL", "example/dossier-large")

    text = plugin._settings_panel.render(ctx(plugin)).text
    # Dossiers fall back to the cleanup provider, as `hermes telesec dossier status` shows.
    assert text.splitlines()[-2:] == ["Dossier model: openrouter / example/dossier-large",
                                      "Voice cleanup: openrouter / example/cleanup-small"]
    code, out = ttm.run_cli(plugin, "dossier", "status")
    assert code == 0 and "Model: openrouter / example/dossier-large" in out

    monkeypatch.setenv("TELESEC_CLEANUP_DISABLE", "1")
    assert plugin._settings_panel.render(ctx(plugin)).text.splitlines()[-1] == "Voice cleanup: off"
    ru = plugin._settings_panel.render(ctx(plugin, lang="ru")).text
    assert ru.splitlines()[-2:] == ["Модель досье: openrouter / example/dossier-large", "Очистка расшифровок: выкл"]


def test_model_lines_never_show_credentials(gs, plugin, monkeypatch):
    top.bind_owner(plugin)
    monkeypatch.setenv("TELESEC_DOSSIER_PROVIDER", "https://user:sk-test-secret@llm.example.test:8443/v1?key=abc#x")
    monkeypatch.setenv("TELESEC_DOSSIER_MODEL", "model-a?api_key=xyz")
    monkeypatch.setenv("TELESEC_CLEANUP_PROVIDER", "http://token-only@cleanup.example.test/")
    monkeypatch.setenv("TELESEC_CLEANUP_MODEL", "vertex/model-b@20260101")

    text = plugin._settings_panel.render(ctx(plugin)).text

    assert text.splitlines()[-2:] == ["Dossier model: https://llm.example.test:8443/v1 / model-a",
                                      "Voice cleanup: http://cleanup.example.test/ / vertex/model-b@20260101"]
    for secret in ("sk-test-secret", "user:", "key=abc", "api_key", "xyz", "token-only"):
        assert secret not in text

    # An "@" or "?" in the password, and user info without a scheme.
    monkeypatch.setenv("TELESEC_DOSSIER_PROVIDER", "https://user:p@ss?w#rd@llm.example.test/v1")
    monkeypatch.setenv("TELESEC_DOSSIER_MODEL", "admin:hunter2@llm.example.test")
    text = plugin._settings_panel.render(ctx(plugin)).text
    assert text.splitlines()[-2] == "Dossier model: https://llm.example.test/v1 / llm.example.test"
    for secret in ("user", "p@ss", "ss?w", "rd@", "admin", "hunter2"):
        assert secret not in text
    monkeypatch.setenv("TELESEC_DOSSIER_MODEL", "m" * 500)
    line = plugin._settings_panel.render(ctx(plugin)).text.splitlines()[-2]
    assert len(line) == len("Dossier model: ") + 120 and line.endswith("…")
