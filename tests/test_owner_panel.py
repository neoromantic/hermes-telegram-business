"""Owner panel ("Manage Bot" -> `/start bizChat<id>` in the owner's private chat)."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
import test_history
import test_plugin
import test_telegram_integration
import test_transcription_modes as ttm
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, TypeHandler

isolate_plugin_environment = test_plugin.isolate_plugin_environment
isolate_modes_environment = ttm.isolate_modes_environment
history_enabled = ttm.history_enabled
plugin = test_plugin.plugin
modes = ttm.modes
telegram_app = test_telegram_integration.telegram_app

OWNER = ttm.OWNER
PEER = ttm.PEER
CHAT = ttm.CHAT
BCID = ttm.BCID
DISABLE_ENV = "TELESEC_DISABLE"


class PanelBot(ttm.CommandBot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.answers: list[dict] = []

    async def answer_callback_query(self, **kwargs):
        self.answers.append(kwargs)
        return True


def start_update(
    text: str = f"/start bizChat{CHAT}",
    *,
    from_user_id: int = OWNER,
    chat_type: str = "private",
    language_code: str | None = None,
):
    message = SimpleNamespace(
        message_id=5,
        text=text,
        chat=SimpleNamespace(id=from_user_id, type=chat_type),
        from_user=SimpleNamespace(id=from_user_id, language_code=language_code),
    )
    return SimpleNamespace(update_id=1, message=message, callback_query=None, effective_message=message)


def callback_update(data: str, *, from_user_id: int = OWNER, message_id: int = 500, language_code: str | None = None):
    query = SimpleNamespace(
        id="query-1",
        data=data,
        from_user=SimpleNamespace(id=from_user_id, language_code=language_code),
        message=SimpleNamespace(chat=SimpleNamespace(id=from_user_id, type="private"), message_id=message_id),
    )
    return SimpleNamespace(update_id=2, message=None, callback_query=query, effective_message=query.message)


def bind_owner(plugin, owner: int = OWNER, connection: str = BCID, *, seen_chat: int | None = CHAT) -> None:
    """Record the connection's owner and, by default, a Business update seen for CHAT on it."""
    plugin._transcription_modes.default_store().bind_connection(connection, str(owner))
    if seen_chat is not None:
        plugin._note_business_chat(connection, seen_chat)


class MultiOwnerBot(PanelBot):
    """get_business_connection answers from {connection id: owner id}; unknown ids fail."""

    def __init__(self, owners: dict[str, int], **kwargs):
        super().__init__(**kwargs)
        self.owners = owners

    async def get_business_connection(self, business_connection_id: str):
        self.business_connection_calls.append(business_connection_id)
        if self.business_connection_error is not None:
            raise self.business_connection_error
        if business_connection_id not in self.owners:
            raise ttm.BadRequest("Business connection not found")
        return SimpleNamespace(user=SimpleNamespace(id=self.owners[business_connection_id]), is_enabled=True)


def panel_handler(plugin):
    handlers = []
    app = SimpleNamespace(add_handler=lambda handler, group: handlers.append((handler, group)))
    plugin._register_telegram_handlers(app, ttm.command_adapter(PanelBot()))
    (match,) = [(handler, group) for handler, group in handlers if type(handler).__name__ == "OwnerPanelHandler"]
    assert match[1] == plugin._RAW_HISTORY_HANDLER_GROUP
    return match[0]


async def dispatch(plugin, update, bot) -> bool:
    """Run the registered handler like PTB does; True when it stopped dispatch."""
    handler = panel_handler(plugin)
    if not handler.check_update(update):
        return False
    try:
        await handler.callback(update, SimpleNamespace(bot=bot))
    except ApplicationHandlerStop:
        return True
    return False


def keyboard(markup) -> list[list[tuple[str, str]]]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [[(button.text, button.callback_data) for button in row] for row in markup.inline_keyboard]


def last_edit(bot) -> tuple[str, list[list[tuple[str, str]]]]:
    call = bot.text_edit_calls[-1]
    assert (call["chat_id"], call["message_id"]) == (OWNER, 500)
    return call["text"], keyboard(call["reply_markup"])


MODE_ROW = [("• both", "tb:m:991:both"), ("own", "tb:m:991:own"), ("peer", "tb:m:991:peer"), ("off", "tb:m:991:off")]


# --- Pure logic ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start bizChat991", 991),
        ("/start@HermesBizBot bizChat991", 991),
        ("/start bizChat-100123", -100123),
        ("/start bizChat991 ", 991),
        ("/start bizchat991", None),
        ("/start BizChat991", None),
        ("/start bizChat", None),
        ("/start bizChat99x", None),
        ("/start bizChat991 extra", None),
        ("/start", None),
        ("/startbizChat991", None),
        ("start bizChat991", None),
        (" /start bizChat991", None),
        ("/start bizChat1234567890123456789012", None),
        (None, None),
        (991, None),
    ],
)
def test_parse_start_payload(plugin, text, expected):
    assert plugin._owner_panel.parse_start_payload(text) == expected


def test_callback_data_round_trips_and_is_parsed_strictly(plugin):
    panel = plugin._owner_panel
    assert panel.callback_data("m", 991, "off") == "tb:m:991:off"
    assert panel.callback_data("r", -100123) == "tb:r:-100123"
    assert panel.section_callback(991, "stats", "page2") == "tb:s:991:stats/page2"
    assert panel.parse_callback("tb:m:991:off") == panel.PanelCallback("m", 991, "off")
    assert panel.parse_callback("tb:s:991:stats/page2") == panel.PanelCallback(
        "s", 991, "stats/page2", section="stats", sub="page2"
    )
    assert panel.parse_callback("tb:b:-5") == panel.PanelCallback("b", -5)
    for bad in (
        "tb:m:991",
        "tb:m:991:loud",
        "tb:r:991:off",
        "tb:p:991:x",
        "tb:s:991",
        "tb:s:991:Stats",
        "tb:s:991:stats/",
        "tb:x:991",
        "tb:m:abc:off",
        "tb:m:991:off:extra",
        "tb:m:991:off\n",
        "xx:m:991:off",
        "tb:",
        None,
        b"tb:m:991:off",
    ):
        assert panel.parse_callback(bad) is None, bad


def test_callback_data_never_exceeds_64_bytes(plugin):
    panel = plugin._owner_panel
    longest_chat = -(10**19)
    for mode in panel.MODES:
        assert len(panel.callback_data("m", longest_chat, mode).encode()) <= 64
    assert len(panel.section_callback(longest_chat, "a" * 16).encode()) <= 64
    with pytest.raises(ValueError):
        panel.section_callback(longest_chat, "a" * 16, "b" * 30)
    with pytest.raises(ValueError):
        panel.callback_data("m", 991, "loud")
    assert panel.parse_callback("tb:s:1:" + "a" * 16 + "/" + "b" * 40) is None


def test_longest_section_key_and_sub_fit_while_under_64_bytes(plugin):
    panel = plugin._owner_panel
    key, sub = "k" * 16, "x" * 32
    data = panel.section_callback(12345678, key, sub)
    assert len(data.encode()) == 63
    assert panel.parse_callback(data) == panel.PanelCallback("s", 12345678, f"{key}/{sub}", section=key, sub=sub)
    short = panel.section_callback(991, "a" * 16, "b" * 32)
    assert panel.parse_callback(short).sub == "b" * 32
    assert len(panel.section_callback(123456789, key, sub).encode()) == 64
    with pytest.raises(ValueError):
        panel.section_callback(1234567890, key, sub)  # 65 bytes
    with pytest.raises(ValueError):
        panel.section_callback(991, key, "x" * 33)  # over SUB_ARG_MAX


def test_register_section_validates_keys(plugin):
    panel = plugin._owner_panel
    for key in ("", "Stats", "1stats", "x" * 17, "a/b"):
        with pytest.raises(ValueError):
            panel.register_section(key, "Label", lambda ctx: panel.PanelView("x"))
    with pytest.raises(ValueError):
        panel.register_section("ok", " ", lambda ctx: panel.PanelView("x"))
    with pytest.raises(ValueError):
        panel.register_section("ok", "L" * 65, lambda ctx: panel.PanelView("x"))
    assert panel.register_section("ok", " " + "L" * 64 + " ", lambda ctx: panel.PanelView("x")).label == "L" * 64
    panel.unregister_section("ok")
    panel.register_section("late", "Late", lambda ctx: panel.PanelView("x"), order=200)
    panel.register_section("early", "Early", lambda ctx: panel.PanelView("x"), order=10)
    assert [section.key for section in panel.sections()] == ["early", "late"]


def test_update_matching_is_narrow(plugin):
    assert plugin._is_owner_panel_update(start_update())
    assert plugin._is_owner_panel_update(callback_update("tb:p:991"))
    assert not plugin._is_owner_panel_update(start_update(chat_type="group"))
    assert not plugin._is_owner_panel_update(start_update("/start"))
    assert not plugin._is_owner_panel_update(start_update("hello"))
    assert not plugin._is_owner_panel_update(callback_update("approve:123"))
    assert not plugin._is_owner_panel_update(SimpleNamespace(business_message=object()))


# --- /start bizChat ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_start_sends_the_panel_and_stops_dispatch(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True

    (call,) = bot.calls
    assert call["chat_id"] == OWNER
    assert "business_connection_id" not in call
    assert call["text"] == "Owner panel: chat 991\nTranscription: both (default)"
    assert keyboard(call["reply_markup"]) == [MODE_ROW, [("Refresh", "tb:p:991")]]
    assert bot.business_connection_calls == [BCID]


@pytest.mark.asyncio
async def test_panel_shows_the_stored_mode_label_and_reset(plugin, modes):
    bind_owner(plugin)
    ttm.seed(plugin, OWNER, CHAT, "peer", label="Casey (@casey)")
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True

    (call,) = bot.calls
    assert call["text"] == "Owner panel: Casey (@casey) · chat 991\nTranscription: peer"
    rows = keyboard(call["reply_markup"])
    assert rows[0] == [("both", "tb:m:991:both"), ("own", "tb:m:991:own"), ("• peer", "tb:m:991:peer"),
                       ("off", "tb:m:991:off")]
    assert rows[1] == [("Reset to default", "tb:r:991")]


@pytest.mark.asyncio
async def test_non_owner_start_is_not_claimed(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(from_user_id=PEER), bot) is False
    assert await plugin._handle_owner_panel_update(start_update(from_user_id=PEER), bot=bot) is False

    assert bot.calls == []


@pytest.mark.asyncio
async def test_unknown_connection_gets_one_hint_and_is_claimed(plugin, modes):
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True
    assert await dispatch(plugin, start_update(), bot) is True  # claimed again, but no second hint

    assert bot.calls == [{"chat_id": OWNER, "text": plugin._owner_panel.tr("unknown_connection")}]
    assert bot.business_connection_calls == []
    assert not ttm.modes_file(plugin).exists()


@pytest.mark.asyncio
async def test_unknown_connection_hint_works_without_transcription(plugin, modes, monkeypatch):
    monkeypatch.setenv(DISABLE_ENV, "1")
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True

    assert bot.calls == [{"chat_id": OWNER, "text": plugin._owner_panel.tr("unknown_connection")}]
    assert "/transcribe" not in bot.calls[0]["text"]


@pytest.mark.asyncio
async def test_any_business_message_in_the_chat_verifies_the_owner(plugin, modes, monkeypatch):
    # No binding, no history, no `business_connection` update: only a message in the chat.
    monkeypatch.setenv(DISABLE_ENV, "1")
    bot = PanelBot(business_owner_id=OWNER)
    business = ttm.registered_business_handler(plugin, ttm.command_adapter(bot))

    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls[-1]["text"] == plugin._owner_panel.tr("unknown_connection")

    with pytest.raises(ApplicationHandlerStop):  # Business text is consumed, never an agent turn
        await business(ttm.make_text_update("hello", from_user_id=PEER), SimpleNamespace(bot=bot))
    # The cached "unknown" does not outlive the new connection: the next tap opens the panel.
    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls[-1]["text"].startswith("Owner panel: chat 991\n")
    assert bot.business_connection_calls == [BCID]
    # Anyone else is still not the owner, and their `/start` stays ordinary.
    assert await dispatch(plugin, start_update(from_user_id=PEER), bot) is False
    assert not ttm.modes_file(plugin).exists()


@pytest.mark.asyncio
async def test_hint_clears_once_the_sender_is_bound(plugin, modes):
    bot = PanelBot(business_owner_id=OWNER)
    assert await dispatch(plugin, start_update(), bot) is True
    bind_owner(plugin)  # e.g. an earlier panel change in the chat

    assert await dispatch(plugin, start_update(), bot) is True

    assert bot.calls[-1]["text"].startswith("Owner panel: chat 991\n")


@pytest.mark.asyncio
async def test_second_owner_with_a_known_connection_falls_through(plugin, modes):
    bind_owner(plugin, owner=OWNER, connection="A")
    bot = MultiOwnerBot({"A": OWNER})

    assert await dispatch(plugin, start_update(from_user_id=3000), bot) is False

    assert bot.calls == []


@pytest.mark.asyncio
async def test_catalog_only_connection_with_failed_lookup_leaves_a_stranger_unclaimed(plugin, modes, monkeypatch):
    catalog = [{"business_connection_id": "business-x", "chat_id": 5, "current_profile": {}}]
    monkeypatch.setattr(plugin, "_panel_catalog", lambda: catalog)
    bot = PanelBot(business_owner_id=OWNER)
    bot.business_connection_error = ttm.TimedOut("timed out")

    assert await dispatch(plugin, start_update("/start bizChat5", from_user_id=PEER), bot) is False
    assert bot.calls == []
    # The owner falls through too while the lookup fails and nothing is bound; they can retry.
    assert await dispatch(plugin, start_update("/start bizChat5"), bot) is False


@pytest.mark.asyncio
async def test_failed_lookups_are_cached_and_strangers_are_cheap(plugin, modes, monkeypatch):
    catalog = [{"business_connection_id": f"old-{n}", "chat_id": n, "current_profile": {}} for n in range(3)]
    monkeypatch.setattr(plugin, "_panel_catalog", lambda: catalog)
    bot = PanelBot(business_owner_id=OWNER)
    bot.business_connection_error = ttm.BadRequest("Business connection not found")

    assert await dispatch(plugin, start_update("/start bizChat1", from_user_id=PEER), bot) is False
    assert await dispatch(plugin, start_update("/start bizChat1", from_user_id=PEER), bot) is False
    assert await dispatch(plugin, start_update("/start bizChat1", from_user_id=PEER + 1), bot) is False

    assert sorted(bot.business_connection_calls) == ["old-0", "old-1", "old-2"]
    assert bot.calls == []


@pytest.mark.asyncio
async def test_live_lookups_are_capped(plugin, modes, monkeypatch):
    catalog = [{"business_connection_id": f"old-{n}", "chat_id": n, "current_profile": {}} for n in range(8)]
    monkeypatch.setattr(plugin, "_panel_catalog", lambda: catalog)
    bot = PanelBot(business_owner_id=OWNER)
    bot.business_connection_error = ttm.BadRequest("Business connection not found")

    assert await dispatch(plugin, start_update("/start bizChat1", from_user_id=PEER), bot) is False

    assert len(bot.business_connection_calls) == plugin._PANEL_MAX_LIVE_LOOKUPS
    assert bot.business_connection_calls[0] == "old-1"  # the chat's own connection first


@pytest.mark.asyncio
async def test_definitive_lookup_failure_ignores_the_binding(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)
    bot.business_connection_error = ttm.BadRequest("Business connection not found")

    assert await dispatch(plugin, start_update(), bot) is False
    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True

    assert bot.calls == []
    assert bot.answers == [{"callback_query_id": "query-1", "text": "Not allowed", "show_alert": True}]
    assert modes.default_store().get(OWNER, CHAT) is None


@pytest.mark.asyncio
async def test_disabled_connection_lookup_is_not_ownership(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    async def disabled(business_connection_id):
        bot.business_connection_calls.append(business_connection_id)
        return SimpleNamespace(user=SimpleNamespace(id=OWNER), is_enabled=False)

    bot.get_business_connection = disabled

    assert await dispatch(plugin, start_update(), bot) is False
    assert plugin._history_support._OWNER_CACHE == {}


def connection_update(*, owner: int = OWNER, connection: str = BCID, enabled: bool = True):
    return SimpleNamespace(
        update_id=9,
        message=None,
        callback_query=None,
        business_connection=SimpleNamespace(
            id=connection, user=SimpleNamespace(id=owner), user_chat_id=owner, is_enabled=enabled
        ),
    )


def connection_handler(plugin):
    handlers = []
    app = SimpleNamespace(add_handler=lambda handler, group: handlers.append((handler, group)))
    plugin._register_telegram_handlers(app, ttm.command_adapter(PanelBot()))
    (match,) = [(h, g) for h, g in handlers if type(h).__name__ == "BusinessConnectionHandler"]
    assert match[1] == plugin._RAW_HISTORY_HANDLER_GROUP
    return match[0]


@pytest.mark.asyncio
async def test_business_connection_update_makes_a_new_owner_known_and_disable_revokes(plugin, modes):
    bind_owner(plugin, owner=OWNER, connection="A")
    handler = connection_handler(plugin)
    new_owner = 3000
    bot = MultiOwnerBot({"A": OWNER})  # B is never looked up live
    update = connection_update(owner=new_owner, connection="B")
    assert handler.check_update(update)
    assert not handler.check_update(start_update())

    await handler.callback(update, SimpleNamespace(bot=bot))  # never raises ApplicationHandlerStop

    # Known in memory only: nothing persisted, so the CLI never sees a second owner.
    assert modes.default_store().connection_owner("B") is None
    assert plugin._PANEL_KNOWN_CONNECTIONS == {"B": str(new_owner)}
    plugin._history_support._OWNER_CACHE.pop("B")  # the in-memory map alone verifies
    assert await dispatch(plugin, start_update(from_user_id=new_owner), bot) is True
    assert bot.calls[-1]["text"].startswith("Owner panel: chat 991\n")
    assert "B" not in bot.business_connection_calls  # only A, where this chat was seen, is checked live

    await handler.callback(connection_update(owner=new_owner, connection="B", enabled=False), SimpleNamespace(bot=bot))
    assert "B" not in plugin._history_support._OWNER_CACHE
    assert plugin._PANEL_KNOWN_CONNECTIONS == {}
    assert await dispatch(plugin, callback_update("tb:p:991", from_user_id=new_owner), bot) is True
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "Not allowed", "show_alert": True}


@pytest.mark.asyncio
async def test_connection_update_creates_no_modes_file(plugin, modes):
    handler = connection_handler(plugin)

    await handler.callback(connection_update(owner=OWNER, connection="B"), SimpleNamespace(bot=PanelBot()))

    assert not ttm.modes_file(plugin).exists()
    assert await dispatch(plugin, start_update(), PanelBot(business_owner_id=OWNER)) is True


@pytest.mark.asyncio
async def test_a_strangers_connection_update_does_not_break_cli_owner_inference(plugin, modes):
    ttm.seed(plugin, OWNER, 993, "off")
    assert ttm.run_cli(plugin, "mode", "set", "993", "own")[0] == 0
    stranger = connection_update(owner=5555, connection="stranger-bc")

    await connection_handler(plugin).callback(stranger, SimpleNamespace(bot=PanelBot()))

    assert modes.default_store().snapshot().owners() == [str(OWNER)]
    assert ttm.run_cli(plugin, "mode", "set", "993", "own")[0] == 0
    assert ttm.run_cli(plugin, "mode", "get", "991")[0] == 0


@pytest.mark.asyncio
async def test_lookup_failure_for_one_of_two_connections_falls_through(plugin, modes, monkeypatch):
    # Fail closed: one connection is someone else's and the other cannot be checked.
    catalog = [
        {"business_connection_id": "A", "chat_id": 1, "current_profile": {}},
        {"business_connection_id": "B", "chat_id": 2, "current_profile": {}},
    ]
    monkeypatch.setattr(plugin, "_panel_catalog", lambda: catalog)
    bot = MultiOwnerBot({"B": 3000})  # A raises BadRequest

    assert await dispatch(plugin, start_update(), bot) is False
    assert bot.calls == []


@pytest.mark.asyncio
async def test_failed_lookup_falls_back_to_the_recorded_owner(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)
    bot.business_connection_error = ttm.TimedOut("timed out")

    assert await dispatch(plugin, start_update(), bot) is True

    assert bot.calls[0]["text"].startswith("Owner panel: chat 991")


@pytest.mark.asyncio
async def test_live_lookup_overrides_a_stale_binding(plugin, modes):
    bind_owner(plugin, owner=OWNER)
    bot = PanelBot(business_owner_id=PEER)  # the connection now belongs to someone else

    assert await dispatch(plugin, start_update(), bot) is False
    assert bot.calls == []


@pytest.mark.asyncio
async def test_owner_lookups_are_cached_per_connection(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True
    assert await dispatch(plugin, callback_update("tb:p:991"), bot) is True

    assert bot.business_connection_calls == [BCID]


def probe_section(plugin) -> list:
    seen = []

    def render(ctx):
        seen.append(ctx)
        return plugin._owner_panel.PanelView("Section body")

    plugin._owner_panel.register_section("probe", "Probe", render)
    return seen


@pytest.mark.asyncio
async def test_bindings_alone_verify_owner_without_history(plugin, modes):
    bind_owner(plugin, connection=BCID)
    bind_owner(plugin, connection="business-456", seen_chat=None)
    seen = probe_section(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls[0]["text"].startswith("Owner panel: chat 991\n")
    assert await dispatch(plugin, callback_update("tb:s:991:probe"), bot) is True
    assert seen[0].business_connection_id == BCID  # the connection this chat was seen on
    assert seen[0].owner_user_id == str(OWNER)
    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True

    entry = modes.default_store().get(OWNER, CHAT)
    assert (entry.mode, entry.updated_via, entry.label) == ("off", "chat", None)


@pytest.mark.asyncio
async def test_real_history_catalog_names_the_chat_and_scopes_the_section(history_enabled, modes):
    plugin = history_enabled
    bot = PanelBot(business_owner_id=OWNER)
    business = ttm.registered_business_handler(plugin, ttm.command_adapter(bot))
    update = test_history.make_business_text_update(
        text="hello", business_id="business-456", chat_first_name=None, chat_last_name=None, chat_username="casey"
    )
    update.effective_message = update.business_message
    update.effective_chat = update.business_message.chat
    with pytest.raises(ApplicationHandlerStop):
        await business(update, SimpleNamespace(bot=bot))
    plugin._PANEL_SEEN_CHATS.clear()  # only the catalog ties the chat here
    assert [entry["business_connection_id"] for entry in plugin._panel_catalog()] == ["business-456"]
    seen = probe_section(plugin)

    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls[-1]["text"].startswith("Owner panel: @casey · chat 991\n")
    assert await dispatch(plugin, callback_update("tb:s:991:probe"), bot) is True
    assert seen[0].business_connection_id == "business-456"
    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True

    entry = modes.default_store().get(OWNER, CHAT)
    assert (entry.mode, entry.updated_via, entry.label) == ("off", "chat", "@casey")
    assert modes.default_store().connection_owner("business-456") == str(OWNER)


@pytest.mark.asyncio
async def test_panel_keeps_a_stored_label(plugin, modes, monkeypatch):
    bot = PanelBot(business_owner_id=OWNER)
    # A label stored earlier (0.9.0's in-chat command stored the chat's own profile).
    modes.default_store().set_mode(OWNER, CHAT, "off", via="chat", label="Casey Customer (@casey)", connection=BCID)
    catalog = [{"business_connection_id": BCID, "chat_id": CHAT,
                "current_profile": {"first_name": "Casey", "last_name": "Customer", "username": "casey"}}]
    monkeypatch.setattr(plugin, "_panel_catalog", lambda: catalog)

    assert await dispatch(plugin, callback_update("tb:m:991:peer"), bot) is True

    entry = modes.default_store().get(OWNER, CHAT)
    assert (entry.mode, entry.label) == ("peer", "Casey Customer (@casey)")
    assert last_edit(bot)[0] == "Owner panel: Casey Customer (@casey) · chat 991\nTranscription: peer"


@pytest.mark.asyncio
async def test_header_prefers_the_stored_label_then_the_catalog_then_the_chat_id(plugin, modes, monkeypatch):
    bind_owner(plugin)
    ttm.seed(plugin, OWNER, CHAT, "peer", label="Stored name")
    catalog = [{"business_connection_id": BCID, "chat_id": CHAT, "current_profile": {"username": "other"}}]
    monkeypatch.setattr(plugin, "_panel_catalog", lambda: catalog)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls[-1]["text"].startswith("Owner panel: Stored name · chat 991\n")

    modes.default_store().reset(str(OWNER), str(CHAT))
    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls[-1]["text"].startswith("Owner panel: @other · chat 991\n")

    catalog.clear()
    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls[-1]["text"].startswith("Owner panel: chat 991\n")


@pytest.mark.asyncio
async def test_a_connection_added_later_scopes_its_chats(plugin, modes, monkeypatch):
    bind_owner(plugin, connection="connA", seen_chat=None)
    catalog: list[dict] = []
    monkeypatch.setattr(plugin, "_panel_catalog", lambda: list(catalog))
    seen = probe_section(plugin)
    bot = MultiOwnerBot({"connA": OWNER, "connB": OWNER})

    assert await dispatch(plugin, start_update(f"/start bizChat{PEER}"), bot) is True
    catalog.append({"business_connection_id": "connB", "chat_id": CHAT, "current_profile": {"username": "casey"}})

    assert await dispatch(plugin, callback_update("tb:s:991:probe"), bot) is True
    assert seen[0].business_connection_id == "connB"
    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True
    assert modes.default_store().connection_owner("connB") == str(OWNER)
    assert plugin._PANEL_SEEN_CHATS == {}


@pytest.mark.asyncio
async def test_cross_owner_chat_is_never_scoped_to_another_owners_connection(plugin, modes, monkeypatch):
    other = 3000
    bind_owner(plugin, owner=OWNER, connection="A", seen_chat=None)
    bind_owner(plugin, owner=other, connection="B", seen_chat=None)
    catalog = [{"business_connection_id": "B", "chat_id": CHAT, "current_profile": {"username": "victim"}}]
    monkeypatch.setattr(plugin, "_panel_catalog", lambda: catalog)
    seen = probe_section(plugin)
    bot = MultiOwnerBot({"A": OWNER, "B": other})

    assert await dispatch(plugin, start_update(), bot) is True
    assert "victim" not in bot.calls[-1]["text"]
    assert await dispatch(plugin, callback_update("tb:s:991:probe"), bot) is True
    assert seen[0].business_connection_id is None  # never guessed from the owner's other connections
    assert "victim" not in (seen[0].chat_label or "")
    owner = await plugin._verify_panel_owner(bot, str(OWNER), catalog, str(CHAT))
    assert owner.connections == ("A",)
    # OWNER's own entry for this contact, on OWNER's connection; the other owner's chat is untouched.
    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True
    assert modes.default_store().get(OWNER, CHAT).mode == "off"
    assert modes.default_store().get(other, CHAT) is None
    assert modes.default_store().connection_owner("B") == str(other)
    assert "victim" not in (modes.default_store().get(OWNER, CHAT).label or "")

    assert await dispatch(plugin, start_update(from_user_id=other), bot) is True
    assert bot.calls[-1]["chat_id"] == other
    assert bot.calls[-1]["text"].startswith("Owner panel: @victim · chat 991\n")
    assert await dispatch(plugin, callback_update("tb:s:991:probe", from_user_id=other), bot) is True
    assert seen[-1].business_connection_id == "B"


# --- Buttons -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_owner_callback_is_claimed_with_an_alert_only(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:m:991:off", from_user_id=PEER), bot) is True

    assert bot.answers == [{"callback_query_id": "query-1", "text": "Not allowed", "show_alert": True}]
    assert bot.text_edit_calls == []
    assert bot.calls == []
    assert modes.default_store().get(PEER, CHAT) is None
    assert modes.default_store().get(OWNER, CHAT) is None


@pytest.mark.asyncio
async def test_callback_without_known_connection_is_not_allowed(plugin, modes):
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:p:991"), bot) is True

    assert bot.answers == [{"callback_query_id": "query-1", "text": "Not allowed", "show_alert": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["both", "own", "peer", "off"])
async def test_each_mode_button_sets_the_mode_via_panel_and_edits_the_panel(plugin, modes, mode):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update(f"tb:m:991:{mode}"), bot) is True

    entry = modes.default_store().get(OWNER, CHAT)
    assert (entry.mode, entry.updated_via, entry.label) == (mode, "chat", None)
    assert modes.default_store().connection_owner(BCID) == str(OWNER)
    assert bot.answers == [{"callback_query_id": "query-1", "text": f"Transcription: {mode}"}]
    text, rows = last_edit(bot)
    assert text == f"Owner panel: chat 991\nTranscription: {mode}"
    assert [label for label, _data in rows[0]] == [f"• {m}" if m == mode else m for m in ("both", "own", "peer", "off")]
    assert rows[1] == [("Reset to default", "tb:r:991")]
    assert bot.calls == []
    stored = json.loads(ttm.modes_file(plugin).read_text(encoding="utf-8"))
    assert stored["owners"][str(OWNER)]["chats"][str(CHAT)]["updated_via"] == "chat"  # 0.9.0-compatible


@pytest.mark.asyncio
async def test_reset_button_returns_to_default_and_keeps_the_label_rule(plugin, modes):
    bind_owner(plugin)
    ttm.seed(plugin, OWNER, CHAT, "off", label="Casey")
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:m:991:own"), bot) is True
    assert modes.default_store().get(OWNER, CHAT).label == "Casey"  # no new label: the old one stays
    assert await dispatch(plugin, callback_update("tb:r:991"), bot) is True

    assert modes.default_store().get(OWNER, CHAT) is None
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "Transcription: both (default)"}
    text, rows = last_edit(bot)
    assert text == "Owner panel: Casey · chat 991\nTranscription: both (default)"
    assert rows == [MODE_ROW, [("Refresh", "tb:p:991")]]


@pytest.mark.asyncio
async def test_refresh_shows_a_change_from_elsewhere(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)
    ttm.seed(plugin, OWNER, CHAT, "off")

    assert await dispatch(plugin, callback_update("tb:p:991"), bot) is True

    assert bot.answers == [{"callback_query_id": "query-1", "text": "Refreshed"}]
    assert last_edit(bot)[0] == "Owner panel: chat 991\nTranscription: off"


@pytest.mark.asyncio
async def test_not_modified_edit_is_quiet(plugin, modes, caplog):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)
    bot.text_edit_error = ttm.BadRequest("Message is not modified: specified new message content is the same")

    assert await dispatch(plugin, callback_update("tb:p:991"), bot) is True

    assert "owner panel edit failed" not in caplog.text
    assert bot.answers


@pytest.mark.asyncio
async def test_malformed_owner_callback_is_claimed_and_answered(plugin, modes):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:m:991:loud"), bot) is True

    assert bot.answers == [{"callback_query_id": "query-1", "text": "Unknown action"}]
    assert bot.text_edit_calls == []
    assert modes.default_store().get(OWNER, CHAT) is None


@pytest.mark.asyncio
async def test_corrupt_modes_file_shows_the_save_error_and_is_never_overwritten(plugin, modes):
    bind_owner(plugin)
    modes.default_store().snapshot()
    path = ttm.modes_file(plugin)
    path.write_text("{not json", encoding="utf-8")
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True

    assert path.read_text(encoding="utf-8") == "{not json"
    error = "Error: could not save transcription mode"
    assert bot.answers == [{"callback_query_id": "query-1", "text": error}]
    assert last_edit(bot)[0] == f"Owner panel: chat 991\n{error}\nTranscription: both (default)"


@pytest.mark.asyncio
async def test_corrupt_modes_file_after_restart_explains_the_cause(plugin, modes):
    bind_owner(plugin)
    path = ttm.modes_file(plugin)
    path.write_text("{not json", encoding="utf-8")
    modes.reset_in_memory_caches()
    plugin._history_support.reset_in_memory_caches()
    plugin._PANEL_SEEN_CHATS.clear()
    plugin._PANEL_NEGATIVE.clear()
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls == [{"chat_id": OWNER, "text": plugin._owner_panel.tr("unknown_connection_modes_unreadable")}]
    assert bot.calls[0]["text"].startswith("Error: could not save transcription mode\n")
    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True
    assert bot.answers == [{"callback_query_id": "query-1", "text": "Not allowed", "show_alert": True}]
    assert path.read_text(encoding="utf-8") == "{not json"

    # Recovery: any Business message in the chat lets a live owner lookup verify again.
    plugin._note_business_chat(BCID, CHAT)
    assert await dispatch(plugin, start_update(), bot) is True
    assert bot.calls[-1]["text"].startswith("Owner panel: chat 991\n")
    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "Error: could not save transcription mode"}
    assert path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.asyncio
async def test_quiet_chat_after_restart_still_gets_mode_buttons(plugin, modes):
    # History off, no stored mode, no Business update seen since the restart.
    bind_owner(plugin, seen_chat=None)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update("/start bizChat424242"), bot) is True
    (call,) = bot.calls
    assert call["text"] == "Owner panel: chat 424242\nTranscription: both (default)"
    assert keyboard(call["reply_markup"]) == [
        [(label, data.replace(":991:", ":424242:")) for label, data in MODE_ROW],
        [("Refresh", "tb:p:424242")],
    ]

    assert await dispatch(plugin, callback_update("tb:m:424242:off"), bot) is True
    assert bot.answers == [{"callback_query_id": "query-1", "text": "Transcription: off"}]
    entry = modes.default_store().get(OWNER, 424242)
    assert (entry.mode, entry.updated_via) == ("off", "chat")  # the panel's write, 0.9.0-compatible
    assert plugin._PANEL_SEEN_CHATS == {}


@pytest.mark.asyncio
async def test_typed_chat_ids_are_capped_per_owner(plugin, modes, monkeypatch):
    monkeypatch.setattr(modes, "MAX_CHATS_PER_OWNER", 2)
    bind_owner(plugin, seen_chat=None)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:m:1:off"), bot) is True
    assert await dispatch(plugin, callback_update("tb:m:2:off"), bot) is True
    assert await dispatch(plugin, callback_update("tb:m:3:off"), bot) is True

    error = "Error: could not save transcription mode"
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": error}
    assert modes.default_store().get(OWNER, 3) is None
    assert last_edit(bot)[0] == f"Owner panel: chat 3\n{error}\nTranscription: both (default)"
    assert sorted(modes.default_store().snapshot()[str(OWNER)]) == ["1", "2"]


@pytest.mark.asyncio
async def test_business_updates_tie_their_chat(plugin, modes):
    bot = PanelBot(business_owner_id=OWNER)
    business = ttm.registered_business_handler(plugin, ttm.command_adapter(bot))

    with pytest.raises(ApplicationHandlerStop):
        await business(ttm.make_text_update("hello", from_user_id=PEER, chat_id=31337), SimpleNamespace(bot=bot))

    assert (BCID, "31337") in plugin._PANEL_SEEN_CHATS


def test_store_caps_chats_per_owner(plugin, modes, monkeypatch):
    monkeypatch.setattr(modes, "MAX_CHATS_PER_OWNER", 2)
    store = modes.default_store()
    store.set_mode(OWNER, 1, "off", via="chat")
    store.set_mode(OWNER, 2, "off", via="chat")
    with pytest.raises(modes.ModeStoreError):
        store.set_mode(OWNER, 3, "off", via="chat")
    store.set_mode(OWNER, 2, "own", via="chat")  # an existing chat can still change
    store.set_mode(PEER, 3, "off", via="chat")  # the cap is per owner
    assert store.get(OWNER, 3) is None


def test_panel_value_is_not_a_stored_updated_via(plugin, modes):
    assert modes.UPDATED_VIA == ("chat", "cli")


@pytest.mark.asyncio
async def test_disabled_transcription_hides_mode_controls(plugin, modes, monkeypatch):
    monkeypatch.setenv(DISABLE_ENV, "1")
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True
    (call,) = bot.calls
    assert call["text"] == "Owner panel: chat 991\nTranscription is disabled on this gateway."
    assert keyboard(call["reply_markup"]) == [[("Refresh", "tb:p:991")]]

    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True
    assert modes.default_store().get(OWNER, CHAT) is None
    assert bot.answers == [{"callback_query_id": "query-1", "text": "Transcription is disabled on this gateway."}]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["tb:r:991", "tb:m:991:own"])
async def test_mode_buttons_leave_a_stored_mode_while_transcription_is_disabled(plugin, modes, monkeypatch, data):
    bind_owner(plugin)
    ttm.seed(plugin, OWNER, CHAT, "off", label="Casey")
    monkeypatch.setenv(DISABLE_ENV, "1")
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update(data), bot) is True

    assert modes.default_store().get(OWNER, CHAT).mode == "off"
    assert bot.answers == [{"callback_query_id": "query-1", "text": "Transcription is disabled on this gateway."}]
    assert last_edit(bot) == ("Owner panel: Casey · chat 991\nTranscription is disabled on this gateway.",
                              [[("Refresh", "tb:p:991")]])


# --- Sections ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_section_opens_with_back_and_back_returns(plugin, modes):
    bind_owner(plugin)
    panel = plugin._owner_panel
    seen = []

    def render(ctx):
        seen.append(ctx)
        page = ctx.arg or "1"
        return panel.PanelView(
            text=f"Stats for {panel.chat_label_text(ctx.chat_id, ctx.chat_label)}, page {page}",
            buttons=[[("Next", panel.section_callback(ctx.chat_id, "stats", "2"))]],
        )

    panel.register_section("stats", "Stats", render, order=10)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True
    assert keyboard(bot.calls[0]["reply_markup"])[1] == [("Stats", "tb:s:991:stats")]

    assert await dispatch(plugin, callback_update("tb:s:991:stats"), bot) is True
    text, rows = last_edit(bot)
    assert text == "Stats for chat 991, page 1"
    assert rows == [[("Next", "tb:s:991:stats/2")], [("« Back", "tb:b:991")]]
    assert seen[0] == panel.PanelContext(
        owner_user_id=str(OWNER), chat_id=str(CHAT), chat_label=None, business_connection_id=BCID, arg=None
    )

    assert await dispatch(plugin, callback_update("tb:s:991:stats/2"), bot) is True
    assert last_edit(bot)[0] == "Stats for chat 991, page 2"
    assert seen[1].arg == "2"

    assert await dispatch(plugin, callback_update("tb:b:991"), bot) is True
    text, rows = last_edit(bot)
    assert text == "Owner panel: chat 991\nTranscription: both (default)"
    assert rows == [MODE_ROW, [("Stats", "tb:s:991:stats")], [("Refresh", "tb:p:991")]]


@pytest.mark.asyncio
async def test_async_section_render_is_awaited(plugin, modes):
    bind_owner(plugin)
    panel = plugin._owner_panel

    async def render(ctx):
        return panel.PanelView(text="async body")

    panel.register_section("dossier", "Dossier", render)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:s:991:dossier"), bot) is True
    assert last_edit(bot) == ("async body", [[("« Back", "tb:b:991")]])


@pytest.mark.asyncio
async def test_broken_or_missing_section_degrades_to_a_short_status(plugin, modes, caplog):
    bind_owner(plugin)
    panel = plugin._owner_panel

    def render(ctx):
        raise RuntimeError("secret detail from Casey")

    panel.register_section("broken", "Broken", render)
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:s:991:broken"), bot) is True
    assert last_edit(bot) == ("chat 991\nSection unavailable.", [[("« Back", "tb:b:991")]])
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text

    assert await dispatch(plugin, callback_update("tb:s:991:gone"), bot) is True
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "Section unavailable"}
    assert last_edit(bot)[0].startswith("Owner panel: chat 991")


@pytest.mark.asyncio
async def test_long_section_text_is_clipped_to_telegrams_limit(plugin, modes):
    bind_owner(plugin)
    panel = plugin._owner_panel
    panel.register_section("dossier", "Dossier", lambda ctx: panel.PanelView(text="x" * 5000))
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:s:991:dossier"), bot) is True

    text, rows = last_edit(bot)
    assert len(text) == 4096 and text.endswith("\u2026")
    assert rows == [[("« Back", "tb:b:991")]]
    assert bot.answers == [{"callback_query_id": "query-1", "text": "Dossier"}]


@pytest.mark.asyncio
async def test_callback_toast_is_clipped_to_telegrams_limit(plugin):
    bot = PanelBot()
    query = SimpleNamespace(id="query-1")

    await plugin._answer_panel_callback(bot, query, "x" * 300)

    assert bot.answers == [{"callback_query_id": "query-1", "text": "x" * 200}]


def test_clip_text_counts_utf16_units(plugin):
    clip = plugin._owner_panel.clip_text
    assert clip("short") == "short"
    clipped = clip("\U0001F600" * 3000)  # 6000 UTF-16 units
    assert len(clipped.encode("utf-16-le")) // 2 <= 4096
    assert clipped.endswith("\u2026")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   \n "])
async def test_empty_section_shows_unavailable(plugin, modes, text):
    bind_owner(plugin)
    panel = plugin._owner_panel
    panel.register_section("stats", "Stats", lambda ctx: panel.PanelView(text=text))
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, callback_update("tb:s:991:stats"), bot) is True

    assert last_edit(bot) == ("chat 991\nSection unavailable.", [[("« Back", "tb:b:991")]])


@pytest.mark.asyncio
async def test_slow_section_times_out_after_the_callback_is_answered(plugin, modes, monkeypatch, caplog):
    bind_owner(plugin)
    monkeypatch.setattr(plugin, "_PANEL_SECTION_TIMEOUT", 0.05)
    panel = plugin._owner_panel
    bot = PanelBot(business_owner_id=OWNER)

    answered_before_render = []

    async def render(ctx):
        answered_before_render.append(list(bot.answers))
        await asyncio.sleep(5)

    panel.register_section("stats", "Stats", render)

    assert await dispatch(plugin, callback_update("tb:s:991:stats"), bot) is True

    assert answered_before_render == [[{"callback_query_id": "query-1", "text": "Stats"}]]
    assert last_edit(bot)[0] == "chat 991\nSection unavailable."
    assert "TimeoutError" in caplog.text
    assert "AssertionError" not in caplog.text


@pytest.mark.asyncio
async def test_failed_section_edit_falls_back_to_unavailable(plugin, modes):
    bind_owner(plugin)
    panel = plugin._owner_panel
    panel.register_section("stats", "Stats", lambda ctx: panel.PanelView(text="body"))
    bot = PanelBot(business_owner_id=OWNER)
    original = bot.edit_message_text

    async def edit(**kwargs):
        if kwargs["text"] == "body":
            bot.text_edit_calls.append(kwargs)
            raise ttm.BadRequest("Can't parse entities")
        return await original(**kwargs)

    bot.edit_message_text = edit

    assert await dispatch(plugin, callback_update("tb:s:991:stats"), bot) is True

    assert last_edit(bot)[0] == "chat 991\nSection unavailable."


# --- Robustness and logs -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_hold_actions_and_ids_but_never_names_or_text(plugin, modes, caplog):
    caplog.set_level(logging.DEBUG)
    bind_owner(plugin)
    ttm.seed(plugin, OWNER, CHAT, "peer", label="Casey Secretname")
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(), bot) is True
    assert await dispatch(plugin, callback_update("tb:m:991:off"), bot) is True
    assert await dispatch(plugin, callback_update("tb:m:991:off", from_user_id=PEER), bot) is True

    assert "owner panel action=open chat=991 mode=peer" in caplog.text
    assert "owner panel action=m chat=991 mode=off" in caplog.text
    assert "owner panel action=denied" in caplog.text
    assert "Secretname" not in caplog.text
    assert "Owner panel:" not in caplog.text
    assert "Transcription:" not in caplog.text


@pytest.mark.asyncio
async def test_failures_are_contained_and_callbacks_still_claimed(plugin, modes, monkeypatch, caplog):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)

    async def explode(*_args, **_kwargs):
        raise RuntimeError("boom with private text")

    monkeypatch.setattr(plugin, "_verify_panel_owner", explode)

    assert await dispatch(plugin, callback_update("tb:p:991"), bot) is True
    assert await dispatch(plugin, start_update(), bot) is False  # unverified: left to the ordinary path
    assert "RuntimeError" in caplog.text
    assert "private text" not in caplog.text


@pytest.mark.asyncio
async def test_send_failure_still_claims_a_verified_owner_start(plugin, modes, caplog):
    bind_owner(plugin)
    bot = PanelBot(business_owner_id=OWNER)
    bot.send_error = ttm.NetworkError("network down")

    assert await dispatch(plugin, start_update(), bot) is True
    assert "owner panel open failed (NetworkError)" in caplog.text


# --- Real PTB dispatch ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_ptb_start_and_button_never_reach_core(plugin, modes, telegram_app):
    app, request = telegram_app
    request.responses["answerCallbackQuery"] = (200, {"ok": True, "result": True})
    request.responses["editMessageText"] = (200, {"ok": True, "result": True})
    bind_owner(plugin)
    plugin._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    core_calls = []

    async def core(update, context):
        core_calls.append(update.update_id)

    app.add_handler(TypeHandler(Update, core))
    owner = {"id": OWNER, "is_bot": False, "first_name": "Owner"}
    start = Update.de_json(
        {
            "update_id": 50,
            "message": {
                "message_id": 5, "date": 1, "chat": {"id": OWNER, "type": "private"}, "from": owner,
                "text": "/start bizChat991", "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
            },
        },
        app.bot,
    )
    press = Update.de_json(
        {
            "update_id": 51,
            "callback_query": {
                "id": "cq-1", "from": owner, "chat_instance": "ci", "data": "tb:m:991:off",
                "message": {"message_id": 700, "date": 1, "chat": {"id": OWNER, "type": "private"}, "text": "panel"},
            },
        },
        app.bot,
    )
    plain = Update.de_json(
        {
            "update_id": 52,
            "message": {
                "message_id": 6, "date": 1, "chat": {"id": OWNER, "type": "private"}, "from": owner, "text": "/start",
            },
        },
        app.bot,
    )

    await app.process_update(start)
    await app.process_update(press)
    await app.process_update(plain)

    assert core_calls == [52]
    calls = [call for call in request.calls if call[0] != "getMe"]
    assert [endpoint for endpoint, _params in calls] == [
        "getBusinessConnection", "sendMessage", "answerCallbackQuery", "editMessageText"
    ]
    send = calls[1][1]
    assert send["chat_id"] == str(OWNER)
    assert json.loads(send["reply_markup"])["inline_keyboard"][0][3] == {"text": "off", "callback_data": "tb:m:991:off"}
    answer = calls[2][1]
    assert answer == {"callback_query_id": "cq-1", "text": "Transcription: off"}
    edit = calls[3][1]
    assert (edit["chat_id"], edit["message_id"]) == (str(OWNER), "700")
    assert edit["text"] == "Owner panel: chat 991\nTranscription: off"
    assert modes.default_store().get(OWNER, CHAT).updated_via == "chat"


# --- Localization --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [("ru", "ru"), ("ru-RU", "ru"), ("RU", "ru"), ("en", "en"), ("en-US", "en"), ("uk", "en"), ("", "en"), (None, "en")],
)
def test_language_is_russian_for_any_ru_client_else_english(plugin, code, expected):
    assert plugin._owner_panel.language(code) == expected


def test_catalog_falls_back_to_english_and_needs_an_english_string(plugin):
    panel = plugin._owner_panel
    panel.register_strings({"en": {"probe.only_en": "Only {what}"}})
    assert panel.tr("probe.only_en", "ru", what="English") == "Only English"
    panel.register_strings({"ru": {"probe.only_en": "Только {what}"}})
    assert panel.tr("probe.only_en", "ru-RU", what="русский") == "Только русский"
    with pytest.raises(ValueError, match="no English string"):
        panel.register_strings({"ru": {"probe.missing": "Нет"}})
    with pytest.raises(ValueError, match="language"):
        panel.register_strings({"de": {"title": "Panel"}})
    with pytest.raises(KeyError):
        panel.tr("probe.unknown")


@pytest.mark.asyncio
async def test_panel_speaks_russian_to_a_russian_client(plugin, modes):
    bind_owner(plugin)
    seen = probe_section(plugin)
    plugin._owner_panel.register_strings({"en": {"section.probe": "Probe"}, "ru": {"section.probe": "Проба"}})
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(language_code="ru"), bot) is True
    (call,) = bot.calls
    assert call["text"] == "Панель владельца: chat 991\nРасшифровка: все (по умолчанию)"
    assert keyboard(call["reply_markup"]) == [
        [("• все", "tb:m:991:both"), ("мои", "tb:m:991:own"), ("собеседника", "tb:m:991:peer"), ("выкл", "tb:m:991:off")],
        [("Проба", "tb:s:991:probe")],
        [("Обновить", "tb:p:991")],
    ]

    assert await dispatch(plugin, callback_update("tb:m:991:off", language_code="ru-RU"), bot) is True
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "Расшифровка: выкл"}
    text, buttons = last_edit(bot)
    assert text == "Панель владельца: chat 991\nРасшифровка: выкл"
    assert buttons[1] == [("Сбросить", "tb:r:991")]
    assert modes.default_store().get(OWNER, CHAT).mode == "off"  # stored by mode id, not by label

    assert await dispatch(plugin, callback_update("tb:s:991:probe", language_code="ru"), bot) is True
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "Проба"}
    assert last_edit(bot)[1][-1] == [("« Назад", "tb:b:991")]
    assert seen[-1].language_code == "ru"

    assert await dispatch(plugin, callback_update("tb:p:991", from_user_id=PEER, language_code="ru"), bot) is True
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "Нет доступа", "show_alert": True}

    # English for everyone else, including a client with no language.
    assert await dispatch(plugin, callback_update("tb:r:991", language_code="de"), bot) is True
    assert bot.answers[-1] == {"callback_query_id": "query-1", "text": "Transcription: both (default)"}
    assert last_edit(bot)[0] == "Owner panel: chat 991\nTranscription: both (default)"


@pytest.mark.asyncio
async def test_unknown_connection_hint_in_russian(plugin, modes):
    bot = PanelBot(business_owner_id=OWNER)

    assert await dispatch(plugin, start_update(language_code="ru"), bot) is True

    assert bot.calls == [{"chat_id": OWNER, "text": plugin._owner_panel.tr("unknown_connection", "ru")}]
    assert bot.calls[0]["text"].startswith("Пока не могу подтвердить")


# --- The owner's own chat with the bot -----------------------------------------------------------

BOT_ID = 8225039795


def business_text(**kwargs):
    update = test_history.make_business_text_update(**kwargs)
    update.effective_message = update.business_message
    update.effective_chat = update.business_message.chat
    return update


@pytest.mark.asyncio
async def test_owners_chat_with_the_bot_is_not_a_business_chat(history_enabled, modes):
    plugin = history_enabled
    bot = PanelBot(business_owner_id=OWNER)
    bot.id = BOT_ID
    business = ttm.registered_business_handler(plugin, ttm.command_adapter(bot))
    # Telegram mirrors the owner's DM with the bot as Business updates, both ways.
    mirrored = [
        business_text(text=f"/start bizChat{CHAT}", chat_id=BOT_ID, chat_username="example_secretary_bot", from_user_id=OWNER),
        business_text(
            text="Owner panel: chat 991", chat_id=BOT_ID, chat_username="example_secretary_bot",
            from_user_id=BOT_ID, from_user_is_bot=True, message_id=78,
        ),
    ]
    for update in mirrored:
        with pytest.raises(ApplicationHandlerStop):
            await business(update, SimpleNamespace(bot=bot))

    assert plugin._panel_catalog() == []
    assert plugin._PANEL_SEEN_CHATS == {}
    assert bot.business_connection_calls == []  # not even an owner lookup

    contact = business_text(text="hello", chat_id=CHAT)
    with pytest.raises(ApplicationHandlerStop):
        await business(contact, SimpleNamespace(bot=bot))
    assert [entry["chat_id"] for entry in plugin._panel_catalog()] == [CHAT]


@pytest.mark.asyncio
async def test_history_already_captured_from_the_bot_chat_is_never_read(history_enabled, modes, monkeypatch):
    plugin = history_enabled
    bot = PanelBot(business_owner_id=OWNER)  # no bot id and no token: an older plugin captured it
    business = ttm.registered_business_handler(plugin, ttm.command_adapter(bot))
    for chat_id in (BOT_ID, CHAT):
        update = business_text(text="hello", chat_id=chat_id, message_id=chat_id % 1000)
        with pytest.raises(ApplicationHandlerStop):
            await business(update, SimpleNamespace(bot=bot))
    history = plugin._history_support
    assert sorted(entry["chat_id"] for entry in history._catalog_entries_for_cli()) == [CHAT, BOT_ID]

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"{BOT_ID}:secret")

    # Every reader (history and stats CLI, dossiers, the panel) goes through the catalog.
    assert [entry["chat_id"] for entry in history._catalog_entries_for_cli()] == [CHAT]
    assert history._filter_catalog_entries(chat_id=str(BOT_ID)) == []
    assert history.is_own_bot_chat(BOT_ID) and not history.is_own_bot_chat(CHAT)


def test_own_bot_id_ignores_a_malformed_token_and_an_uninitialized_bot(plugin, monkeypatch):
    class Uninitialized:
        @property
        def id(self):
            raise RuntimeError("Bot is not properly initialized")

    history = plugin._history_support
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    assert history.own_bot_ids(Uninitialized()) == set()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert history.own_bot_ids(SimpleNamespace(id=456)) == {"123", "456"}
