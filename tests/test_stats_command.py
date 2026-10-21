"""In-chat `/stats`: the owner's command becomes the public stats card in the Business chat.

The contact sees the card too, so it never shows deletions, edits, who is waiting,
missing history, p90, or message text. The command, its card or notice, and their
echoes and deletion are control traffic: never history, never counted.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import sys
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
import test_history as th
import test_transcription_modes as ttm
from telegram.ext import ApplicationHandlerStop
from test_stats import (
    HISTORY_DOC,
    NOW,
    README_DOC,
    STATS_DOC,
    _alice_fixture,
    _break_history,
    _doc_section,
    _fenced_blocks,
    _overview_fixture,
)
from test_stats_panel import _visible

isolate_plugin_environment = ttm.isolate_plugin_environment
isolate_modes_environment = ttm.isolate_modes_environment
plugin = ttm.plugin
modes = ttm.modes
history_enabled = ttm.history_enabled

OWNER = ttm.OWNER
PEER = ttm.PEER
CHAT = ttm.CHAT
BCID = ttm.BCID
COMMAND_ID = 900

# The Alice fixture of test_stats, all history, clock at 2026-10-19 11:32 UTC, no Hermes time zone.
CARD_ALL_EN = """
<h3>📊 Sam × Alice</h3>
<p>all time · 3 Oct – 19 Oct · 💬 <b>10</b> · <b>4</b> days in touch</p>
<p>67% 🟦🟦🟦🟦🟦🟦🟦🟩🟩🟩 33%</p>
<table striped compact>
<tr><th></th><th align="right">🟦 Alice</th><th align="right">🟩 Sam</th></tr>
<tr><td>messages</td><td align="right"><b>6</b> · 67%</td><td align="right"><b>3</b> · 33%</td></tr>
<tr><td>⚡ reply</td><td align="right"><b>52 min</b></td><td align="right"><b>30 min</b></td></tr>
<tr><td>per turn</td><td align="right">1.5</td><td align="right">1.0</td></tr>
<tr><td>length</td><td align="right">10</td><td align="right">10</td></tr>
</table>
<h4>🗓 When</h4>
<table compact>
<tr><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th><th>Su</th></tr>
<tr><td></td><td></td><td></td><td></td><td></td><td>🟥</td><td>·</td></tr>
<tr><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td><td>·</td></tr>
<tr><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟧</td><td>🟨</td></tr>
<tr><td>·</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
<table compact>
<tr><th>0</th><th>3</th><th>6</th><th>9</th><th>12</th><th>15</th><th>18</th><th>21</th></tr>
<tr><td>·</td><td>·</td><td>🟧</td><td>🟥</td><td>·</td><td>🟨</td><td>🟧</td><td>🟧</td></tr>
</table>
<p>🎯 Most often — <mark>Sat, 9–10</mark></p>
<table compact>
<tr><td>🏆 record</td><td align="right"><b>2</b> days in a row</td></tr>
<tr><td>🗣 sessions</td><td align="right"><b>5</b> · first 🟦 <b>60%</b></td></tr>
</table>
<footer>counted by <a href="https://telesec.sergeybaus.com">Telesec</a></footer>
""".strip("\n")

CARD_ALL_EN_TEXT = """
📊 Sam × Alice
all time · 3 Oct – 19 Oct · 💬 10 · 4 days in touch
🟦 Alice: 6 (67%) · ⚡ 52 min
🟩 Sam: 3 (33%) · ⚡ 30 min
🎯 Most often — Sat, 9–10
counted by Telesec · telesec.sergeybaus.com
""".strip("\n")

CARD_ALL_RU = """
<h3>📊 Сергей × Alice</h3>
<p>всё время · 3 окт – 19 окт · 💬 <b>10</b> · <b>4</b> дня общения</p>
<p>67% 🟦🟦🟦🟦🟦🟦🟦🟩🟩🟩 33%</p>
<table striped compact>
<tr><th></th><th align="right">🟦 Alice</th><th align="right">🟩 Сергей</th></tr>
<tr><td>сообщений</td><td align="right"><b>6</b> · 67%</td><td align="right"><b>3</b> · 33%</td></tr>
<tr><td>⚡ ответ</td><td align="right"><b>52 мин</b></td><td align="right"><b>30 мин</b></td></tr>
<tr><td>подряд</td><td align="right">1,5</td><td align="right">1,0</td></tr>
<tr><td>длина</td><td align="right">10</td><td align="right">10</td></tr>
</table>
<h4>🗓 Когда</h4>
<table compact>
<tr><th>пн</th><th>вт</th><th>ср</th><th>чт</th><th>пт</th><th>сб</th><th>вс</th></tr>
<tr><td></td><td></td><td></td><td></td><td></td><td>🟥</td><td>·</td></tr>
<tr><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟥</td><td>·</td></tr>
<tr><td>·</td><td>·</td><td>·</td><td>·</td><td>·</td><td>🟧</td><td>🟨</td></tr>
<tr><td>·</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
</table>
<table compact>
<tr><th>0</th><th>3</th><th>6</th><th>9</th><th>12</th><th>15</th><th>18</th><th>21</th></tr>
<tr><td>·</td><td>·</td><td>🟧</td><td>🟥</td><td>·</td><td>🟨</td><td>🟧</td><td>🟧</td></tr>
</table>
<p>🎯 Чаще всего — <mark>сб, 9–10</mark></p>
<table compact>
<tr><td>🏆 рекорд</td><td align="right"><b>2</b> дня подряд</td></tr>
<tr><td>🗣 бесед</td><td align="right"><b>5</b> · первым 🟦 <b>60%</b></td></tr>
</table>
<footer>посчитал <a href="https://telesec.sergeybaus.com">Telesec</a></footer>
""".strip("\n")

NOTICES = {
    "history_off": {
        "en": "📊 Stats are unavailable: history is off.",
        "ru": "📊 Статистика недоступна: история выключена.",
    },
    "no_history": {
        "en": "📊 No history for this chat yet.",
        "ru": "📊 Истории этого чата пока нет.",
    },
    "read_error": {
        "en": "📊 Stats are unavailable: history could not be read.",
        "ru": "📊 Статистика недоступна: историю не удалось прочитать.",
    },
}


@pytest.fixture(autouse=True)
def _fixed_clock(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)  # no Hermes time zone
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: NOW)


@pytest.fixture
def alice(history_enabled, modes):
    _alice_fixture(history_enabled)
    return history_enabled


class StatsBot(ttm.CommandBot):
    """A Business bot whose sends return a message id, like Telegram's, with rich messages (``do_api_request``)."""

    def __init__(self) -> None:
        super().__init__(business_owner_id=OWNER)
        self.next_message_id = COMMAND_ID + 1
        self.api_calls: list[tuple[str, dict[str, Any]]] = []
        self.rich_edit_error: Exception | None = None
        self.rich_edit_result: object = True
        self.rich_send_error: Exception | None = None

    def _new_id(self) -> int:
        message_id, self.next_message_id = self.next_message_id, self.next_message_id + 1
        return message_id

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        return SimpleNamespace(message_id=self._new_id())

    async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any] | None = None, **_: Any):
        self.api_calls.append((endpoint, dict(api_kwargs or {})))
        if endpoint == "editmessagetext":
            if self.rich_edit_error is not None:
                raise self.rich_edit_error
            return self.rich_edit_result
        assert endpoint == "sendRichMessage"
        if self.rich_send_error is not None:
            raise self.rich_send_error
        return {"message_id": self._new_id()}


def stats_update(
    text: str = "/stats",
    *,
    sender: int | None = OWNER,
    lang: str | None = "en",
    first_name: str | None = "Sam",
    message_id: int = COMMAND_ID,
    update_id: int = 500,
    edited: bool = False,
    sender_business_bot: int | None = None,
):
    update = th.make_business_text_update(
        text=text,
        from_user_id=sender,
        from_user_first_name=first_name,
        from_user_last_name=None,
        from_user_username=None,
        from_user_language_code=lang,
        chat_first_name="Alice",
        chat_last_name="Example",
        chat_username="alice",
        message_id=message_id,
        update_id=update_id,
        edited=edited,
        sender_business_bot_id=sender_business_bot,
        date=NOW,
    )
    update.effective_message = update.edited_business_message if edited else update.business_message
    update.effective_chat = update.effective_message.chat
    return update


def deleted_update(*message_ids: int, update_id: int = 600):
    update = th.make_deleted_update(message_ids=message_ids, update_id=update_id)
    update.effective_message = None
    update.effective_chat = update.deleted_business_messages.chat
    return update


async def dispatch(plugin, update, bot, *, authorized: bool = True, adapter: Any = None) -> bool:
    """Run the registered Business handler; True when it stopped the update (claimed or consumed)."""
    callback = ttm.registered_business_handler(plugin, adapter or ttm.command_adapter(bot, authorized=authorized))
    try:
        await callback(update, SimpleNamespace(bot=bot))
    except ApplicationHandlerStop:
        return True
    return False


async def drain(plugin) -> None:
    if plugin._pending_tasks:
        await asyncio.gather(*list(plugin._pending_tasks))


def module(plugin, name: str):
    return importlib.import_module(f"{plugin.__name__}.{name}")


def contact_stats(plugin, period: str = "all"):
    cs = module(plugin, "correspondence_stats")
    # A period is whole local days with today: the CLI's --since with its first date (UTC here).
    since = None if period == "all" else (NOW.date() - timedelta(days=int(period[:-1]) - 1)).isoformat()
    window = cs.build_window(argparse.Namespace(since=since, until=None, tz=None, session_gap=None), now=NOW)
    [entry] = plugin._history_support._filter_catalog_entries(business_connection_id=BCID, chat_id=str(CHAT))
    return cs.collect_contact_stats(entry, window), window


def card_for(plugin, period: str = "all", lang: str | None = "en", owner: str = "Sam", contact: str = "Alice"):
    stats, window = contact_stats(plugin, period)
    return module(plugin, "stats_render").card(
        stats, window, period=period, owner_name=owner, contact_name=contact, language_code=lang
    )


def rich_edits(bot) -> list[dict[str, Any]]:
    return [call for endpoint, call in bot.api_calls if endpoint == "editmessagetext"]


def rich_sends(bot) -> list[dict[str, Any]]:
    return [call for endpoint, call in bot.api_calls if endpoint == "sendRichMessage"]


def edit_call(bot) -> dict[str, Any]:
    """The one rich edit of the command."""
    [call] = rich_edits(bot)
    assert bot.text_edit_calls == []
    return call


def edit_html(bot) -> str:
    call = edit_call(bot)
    assert call["rich_message"]["skip_entity_detection"] is True
    return call["rich_message"]["html"]


def edit_call_text(bot) -> str:
    [call] = bot.text_edit_calls
    return call["text"]


def record_ids(plugin) -> list[Any]:
    return [record["message_id"] for record in th.load_records(plugin)]


# --- The owner's command ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_stats_is_edited_in_place_into_the_card(alice):
    bot = StatsBot()

    assert await dispatch(alice, stats_update(), bot)

    assert edit_call(bot) == {
        "chat_id": CHAT,
        "message_id": COMMAND_ID,
        "business_connection_id": BCID,
        "rich_message": {"html": CARD_ALL_EN, "skip_entity_detection": True},
    }
    assert (CARD_ALL_EN, CARD_ALL_EN_TEXT) == (card_for(alice).html, card_for(alice).text)
    assert bot.calls == [] and bot.delete_calls == [] and rich_sends(bot) == []
    assert COMMAND_ID not in record_ids(alice)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    [("ru", "ru"), ("ru-RU", "ru"), ("RU", "ru"), ("en", "en"), (None, "en"), ("de", "en"), ("uk", "en")],
)
async def test_card_language_follows_the_owner_client(alice, code, expected):
    bot = StatsBot()
    owner = "Сергей" if expected == "ru" else "Sam"

    assert await dispatch(alice, stats_update(lang=code, first_name=owner), bot)

    assert edit_html(bot) == (CARD_ALL_RU if expected == "ru" else CARD_ALL_EN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "period"),
    [
        ("/stats", "all"),
        ("/stats all", "all"),
        ("/stats 7d", "7d"),
        ("/stats 30d", "30d"),
        ("/stats 90d", "90d"),
        ("/STATS 30D", "30d"),
        ("/Stats All", "all"),
        ("/stats@TelesecBot", "all"),
        ("/stats@TelesecBot 7d", "7d"),
        ("/stats   90d  ", "90d"),
        ("/stats\n7d\n", "7d"),
    ],
)
async def test_each_period_argument(alice, text, period):
    bot = StatsBot()

    assert await dispatch(alice, stats_update(text), bot)

    assert edit_html(bot) == card_for(alice, period).html
    labels = {"all": "all time", "7d": "7 days", "30d": "30 days", "90d": "90 days"}
    # 7d is 7 dates, not 8: the period starts at midnight of its first day.
    dates = {"all": "3 Oct – 19 Oct", "7d": "13 Oct – 19 Oct", "30d": "20 Sep – 19 Oct", "90d": "22 Jul – 19 Oct"}
    assert edit_html(bot).startswith(f"<h3>📊 Sam × Alice</h3>\n<p>{labels[period]} · {dates[period]} · ")
    assert COMMAND_ID not in record_ids(alice)


@pytest.mark.asyncio
async def test_card_reads_only_this_chat_under_this_connection(history_enabled, modes):
    # Chat 994 has history only under business-456 (Carol); business-123 has none for it.
    _overview_fixture(history_enabled)
    bot = StatsBot()
    update = stats_update(first_name="Sam")
    update.business_message.chat.id = 994
    update.business_message.chat.first_name = "Carol"

    assert await dispatch(history_enabled, update, bot)
    other = stats_update(first_name="Sam", message_id=COMMAND_ID + 1, update_id=501)
    other.business_message.chat.id = 994
    other.business_message.chat.first_name = "Carol"
    other.business_message.business_connection_id = "business-456"
    assert await dispatch(history_enabled, other, bot)

    [unknown] = bot.text_edit_calls
    assert (unknown["text"], unknown["business_connection_id"], unknown["chat_id"]) == (
        NOTICES["no_history"]["en"],
        BCID,
        994,
    )
    [carol] = rich_edits(bot)
    assert carol["business_connection_id"] == "business-456" and carol["chat_id"] == 994
    html = carol["rich_message"]["html"]
    assert html.startswith("<h3>📊 Sam × Carol</h3>\n<p>all time · 20 Sep – 19 Oct · 💬 <b>1</b> · ")
    # Alice's chat 991 under business-123 never reaches either chat.
    assert "Alice" not in html + unknown["text"]


@pytest.mark.asyncio
async def test_card_uses_first_names_and_falls_back_to_history(alice):
    bot = StatsBot()
    update = stats_update(first_name=None)
    update.business_message.chat.first_name = None

    assert await dispatch(alice, update, bot)

    # The contact's first name from history, and a generic owner label: never "You".
    assert edit_html(bot).startswith("<h3>📊 Owner × Alice</h3>")
    assert "You" not in edit_html(bot)


@pytest.mark.asyncio
async def test_card_never_shows_owner_only_details_or_text(alice):
    # The fixture has deletions, edits, an unanswered message, missing history, and p90 reply times:
    # the owner panel shows them all.
    panel = module(alice, "stats_render").panel(*contact_stats(alice), period="all", contact_name="Alice").html
    for word in ("deleted", "❓", "edits", "waiting", "p90", "history since"):
        assert word in panel
    cards = {}
    for lang, owner in (("en", "Sam"), ("ru", "Сергей")):
        bot = StatsBot()
        update = stats_update(lang=lang, first_name=owner, message_id=COMMAND_ID + len(cards))
        assert await dispatch(alice, update, bot)
        cards[lang] = _visible(edit_html(bot))
        cards[f"{lang} text"] = card_for(alice, lang=lang, owner=owner).text

    forbidden = {
        "en": ("deleted", "❓", "edits", "waiting", "p90", "history", "You", "previous", "📈"),
        "ru": ("удалено", "❓", "правки", "ждёт", "p90", "история", "Вы", "прошлым", "📈"),
    }
    forbidden.update({f"{lang} text": words for lang, words in list(forbidden.items())})
    for lang, card in cards.items():
        assert "SECRET" not in card  # the fixture's message text
        for word in forbidden[lang]:
            assert word not in card, (lang, word)


@pytest.mark.asyncio
async def test_duplicate_delivery_posts_once(alice):
    bot = StatsBot()
    update = stats_update()

    assert await dispatch(alice, update, bot)
    assert await dispatch(alice, update, bot)

    assert len(rich_edits(bot)) == 1
    assert bot.calls == [] and COMMAND_ID not in record_ids(alice)


@pytest.mark.asyncio
async def test_command_skips_the_dm_allowlist_and_never_reaches_core(alice, monkeypatch):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")
    bot = StatsBot()
    adapter = ttm.command_adapter(bot, authorized=False)

    assert await dispatch(alice, stats_update(), bot, adapter=adapter)
    # The edit's echo is the command's too.
    echo = stats_update(CARD_ALL_EN_TEXT, edited=True, sender_business_bot=555, update_id=501)
    assert await dispatch(alice, echo, bot, adapter=adapter)

    assert edit_html(bot) == CARD_ALL_EN
    adapter._is_user_authorized_from_message.assert_not_called()
    # Ordinary text still takes the agent path when it is enabled.
    assert not await dispatch(alice, stats_update("Hello", sender=PEER, message_id=950, update_id=502), bot)


@pytest.mark.asyncio
async def test_stored_owner_binding_verifies_when_the_lookup_fails(alice):
    alice._transcription_modes.default_store().bind_connection(BCID, OWNER)
    bot = StatsBot()
    bot.business_connection_error = RuntimeError("lookup down")

    assert await dispatch(alice, stats_update(), bot)

    assert edit_html(bot) == CARD_ALL_EN


@pytest.mark.asyncio
@pytest.mark.parametrize(("lang", "owner", "expected"), [("en", "Sam", CARD_ALL_EN), ("ru", "Сергей", CARD_ALL_RU)])
async def test_paused_chat_gets_the_same_card_and_stays_control_traffic(alice, lang, owner, expected):
    # "Don't save" in the chat's panel: the card still counts what was saved and says
    # nothing about the pause, since the contact sees it.
    store = alice._chat_settings.default_store()
    store.set_flag(OWNER, CHAT, "history", False, via="cli")
    before, _ = contact_stats(alice)
    bot = StatsBot()

    assert await dispatch(alice, stats_update(lang=lang, first_name=owner), bot)
    echo = stats_update(_visible(expected), edited=True, sender_business_bot=555, update_id=501)
    assert await dispatch(alice, echo, bot)

    assert edit_html(bot) == expected
    assert "History is paused" not in expected and "не сохраняется" not in expected
    # The pause still skips the chat's ordinary messages.
    await dispatch(alice, stats_update("Hello", sender=PEER, message_id=950, update_id=502), bot)
    assert {COMMAND_ID, 950}.isdisjoint(record_ids(alice))

    # Back on: ordinary messages are recorded again, and the command's deletion is still dropped.
    store.set_flag(OWNER, CHAT, "history", True, via="cli")
    assert await dispatch(alice, deleted_update(COMMAND_ID), bot)
    await dispatch(alice, stats_update("Hello again", sender=PEER, message_id=951, update_id=503), bot)
    ids = record_ids(alice)
    assert 951 in ids and {COMMAND_ID, 950}.isdisjoint(ids)
    after, _ = contact_stats(alice)
    assert after.messages.received == before.messages.received + 1 and after.messages.sent == before.messages.sent
    # No deletion counted for the command (pending ones may get classified meanwhile).
    assert after.deletions.total == before.deletions.total
    assert after.missing.deletions_not_in_history == before.missing.deletions_not_in_history


# --- Ordinary text ---------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/stats", "/stats 7d"])
async def test_contact_stats_is_ordinary_text_and_captured(alice, text):
    bot = StatsBot()

    await dispatch(alice, stats_update(text, sender=PEER), bot)

    assert bot.text_edit_calls == [] and bot.api_calls == [] and bot.calls == [] and bot.delete_calls == []
    [record] = [record for record in th.load_records(alice) if record["message_id"] == COMMAND_ID]
    assert (record["direction"], record["text"]) == ("inbound", text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "/stats 1y",
        "/stats 7d 30d",
        "/stats week",
        "/stats7d",
        "/statsx",
        "/stats@ 7d",
        "stats",
        "hello /stats",
        "/stat",
    ],
)
async def test_unknown_argument_or_other_text_is_ordinary_text(alice, text):
    bot = StatsBot()

    await dispatch(alice, stats_update(text), bot)

    assert bot.text_edit_calls == [] and bot.api_calls == [] and bot.calls == [] and bot.delete_calls == []
    [record] = [record for record in th.load_records(alice) if record["message_id"] == COMMAND_ID]
    assert (record["direction"], record["text"]) == ("outbound", text)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["forward_origin", "via_bot", "edited", "sender_business_bot"])
async def test_forwarded_inline_edited_or_bot_sent_stats_is_not_a_command(alice, kind):
    bot = StatsBot()
    if kind == "edited":
        update = stats_update(edited=True)
    elif kind == "sender_business_bot":
        update = stats_update(sender_business_bot=555)
    else:
        update = stats_update()
        setattr(update.business_message, kind, SimpleNamespace(type="user", id=3000))

    await dispatch(alice, update, bot)

    assert bot.text_edit_calls == [] and bot.api_calls == [] and bot.calls == [] and bot.delete_calls == []
    [record] = [record for record in th.load_records(alice) if record["message_id"] == COMMAND_ID]
    assert (record["direction"], record["text"]) == ("outbound", "/stats")
    assert record["event_type"] == ("message.edited" if kind == "edited" else "message.created")


@pytest.mark.asyncio
async def test_unverified_owner_stats_is_ordinary_unknown_text(alice):
    bot = StatsBot()
    bot.business_connection_error = RuntimeError("lookup down")

    await dispatch(alice, stats_update(), bot)

    assert bot.text_edit_calls == [] and bot.api_calls == []
    [record] = [record for record in th.load_records(alice) if record["message_id"] == COMMAND_ID]
    assert (record["direction"], record["text"]) == ("unknown", "/stats")


# --- Fallback when the edit fails ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cant_edit", "false_result", "timed_out", "network"])
async def test_failed_edit_sends_the_rich_card_and_deletes_the_command(alice, failure):
    bot = StatsBot()
    if failure == "cant_edit":
        bot.rich_edit_error = ttm.BadRequest("Bad Request: message can't be edited")
    elif failure == "false_result":
        bot.rich_edit_result = False
    elif failure == "timed_out":
        bot.rich_edit_error = ttm.TimedOut("timed out")
    else:
        bot.rich_edit_error = ttm.NetworkError("connection reset")

    assert await dispatch(alice, stats_update(), bot)

    assert rich_sends(bot) == [
        {
            "business_connection_id": BCID,
            "chat_id": CHAT,
            "rich_message": {"html": CARD_ALL_EN, "skip_entity_detection": True},
            "disable_notification": True,
        }
    ]
    assert bot.calls == []
    # Right away, not after the notice delay: the new card replaces the command.
    assert bot.delete_calls == [{"business_connection_id": BCID, "message_ids": [COMMAND_ID]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "no_rich_api"])
async def test_failed_rich_send_sends_the_plain_card(alice, failure, caplog):
    bot = StatsBot()
    if failure == "rejected":
        bot.rich_edit_error = ttm.BadRequest("Bad Request: message can't be edited")
        bot.rich_send_error = ttm.BadRequest("Bad Request: rich messages are not supported")
    else:
        bot.do_api_request = None  # an older client without raw Bot API requests

    with caplog.at_level(logging.INFO):
        assert await dispatch(alice, stats_update(), bot)

    [call] = bot.calls
    call = dict(call)
    assert call.pop("link_preview_options").is_disabled is True
    assert call == {
        "chat_id": CHAT,
        "business_connection_id": BCID,
        "text": CARD_ALL_EN_TEXT,
        "disable_notification": True,
    }
    assert bot.delete_calls == [{"business_connection_id": BCID, "message_ids": [COMMAND_ID]}]
    assert "outcome=sent_text" in caplog.text


@pytest.mark.asyncio
async def test_message_not_modified_counts_as_edited(alice):
    bot = StatsBot()
    bot.rich_edit_error = ttm.BadRequest("Bad Request: message is not modified")

    assert await dispatch(alice, stats_update(), bot)

    assert len(rich_edits(bot)) == 1 and rich_sends(bot) == [] and bot.calls == [] and bot.delete_calls == []


@pytest.mark.asyncio
async def test_failed_fallback_keeps_the_command_and_logs_only_the_error_type(alice, caplog):
    bot = StatsBot()
    bot.rich_edit_error = ttm.BadRequest("Bad Request: message can't be edited")
    bot.rich_send_error = ttm.BadRequest("Bad Request: rich send failed with private detail")
    bot.send_error = RuntimeError("send failed with private detail")

    with caplog.at_level(logging.INFO):
        assert await dispatch(alice, stats_update(), bot)

    assert bot.delete_calls == []
    assert "stats card could not be sent (RuntimeError)" in caplog.text and "outcome=failed" in caplog.text
    assert "private detail" not in caplog.text
    assert COMMAND_ID not in record_ids(alice)


@pytest.mark.asyncio
async def test_fallback_card_its_echo_and_the_deletion_never_reach_history(alice):
    bot = StatsBot()
    bot.rich_edit_error = ttm.BadRequest("Bad Request: message can't be edited")
    before, _ = contact_stats(alice)

    assert await dispatch(alice, stats_update(), bot)
    card_id = COMMAND_ID + 1
    # Telegram echoes the bot's card and reports the command's deletion.
    assert await dispatch(alice, stats_update(CARD_ALL_EN_TEXT, message_id=card_id, sender_business_bot=555), bot)
    assert await dispatch(alice, deleted_update(COMMAND_ID), bot)
    # A deletion that also holds a real message keeps only the real one.
    assert await dispatch(alice, deleted_update(card_id, 112, update_id=601), bot)

    deletions = [record for record in th.load_records(alice) if record["event_type"] == "message.deleted"]
    assert COMMAND_ID not in record_ids(alice) and card_id not in record_ids(alice)
    assert [record["message_id"] for record in deletions][-1] == 112
    after, _ = contact_stats(alice)
    assert after.messages == before.messages
    assert after.deletions.total == before.deletions.total + 1
    assert after.missing.deletions_not_in_history == before.missing.deletions_not_in_history


@pytest.mark.asyncio
async def test_command_deleted_before_the_card_is_ready_posts_nothing(alice, caplog):
    bot = StatsBot()
    # The owner deleted `/stats` while the card was built: the edit finds no message.
    bot.rich_edit_error = ttm.BadRequest("Bad Request: message to edit not found")

    with caplog.at_level(logging.INFO):
        assert await dispatch(alice, stats_update(), bot)
        assert await dispatch(alice, deleted_update(COMMAND_ID), bot)

    # No card reaches the contact, and there is nothing left to delete.
    assert len(rich_edits(bot)) == 1 and rich_sends(bot) == [] and bot.calls == [] and bot.delete_calls == []
    assert "outcome=gone" in caplog.text
    assert COMMAND_ID not in record_ids(alice)


@pytest.mark.asyncio
async def test_rich_send_timeout_sends_no_second_card(alice, caplog):
    bot = StatsBot()
    bot.rich_edit_error = ttm.BadRequest("Bad Request: message can't be edited")
    bot.rich_send_error = ttm.TimedOut("timed out")
    before, _ = contact_stats(alice)
    card_id = COMMAND_ID + 1

    with caplog.at_level(logging.INFO):
        assert await dispatch(alice, stats_update(), bot)
    # Telegram may have delivered the rich card: no plain card follows, and the command goes.
    assert len(rich_sends(bot)) == 1 and bot.calls == []
    assert bot.delete_calls == [{"business_connection_id": BCID, "message_ids": [COMMAND_ID]}]
    assert "outcome=sent_uncertain" in caplog.text
    # Its echo is a bot-sent rich message, so it and its deletion stay out of history.
    echo = stats_update(None, message_id=card_id, sender_business_bot=555, update_id=501)  # type: ignore[arg-type]
    echo.business_message.api_kwargs = MappingProxyType({"rich_message": {"blocks": [{"type": "paragraph"}]}})
    await dispatch(alice, echo, bot)
    await dispatch(alice, deleted_update(card_id), bot)

    assert card_id not in record_ids(alice)
    after, _ = contact_stats(alice)
    assert after.missing.deletions_not_in_history == before.missing.deletions_not_in_history
    assert after.messages == before.messages


@pytest.mark.asyncio
async def test_card_delivered_despite_a_send_timeout_never_reaches_history(alice, monkeypatch):
    bot = StatsBot()
    bot.rich_edit_error = ttm.BadRequest("Bad Request: message can't be edited")
    bot.rich_send_error = ttm.BadRequest("Bad Request: rich messages are not supported")
    bot.send_error = ttm.TimedOut("timed out")
    before, _ = contact_stats(alice)
    records_before = len(th.load_records(alice))
    card_id = COMMAND_ID + 1
    card_text = CARD_ALL_EN_TEXT

    assert await dispatch(alice, stats_update(), bot)
    # Telegram delivered the plain card anyway, so only its echo tells its id: the echo,
    # a later edit, and its deletion are the command's.
    assert await dispatch(alice, stats_update(card_text, message_id=card_id, sender_business_bot=555, update_id=501), bot)
    edit = stats_update(card_text, message_id=card_id, edited=True, sender_business_bot=555, update_id=502)
    assert await dispatch(alice, edit, bot)
    assert await dispatch(alice, deleted_update(card_id), bot)

    assert len(th.load_records(alice)) == records_before
    after, _ = contact_stats(alice)
    assert after == before
    # The expectation is short-lived and matches only the card's text from the bot.
    await dispatch(alice, stats_update("Transcript", message_id=card_id + 1, sender_business_bot=555, update_id=503), bot)
    monkeypatch.setattr(alice, "_STATS_ECHO_TTL_SECONDS", 0)
    await dispatch(alice, stats_update(card_text, message_id=card_id + 2, sender_business_bot=555, update_id=504), bot)
    assert {card_id + 1, card_id + 2} <= set(record_ids(alice))
    assert card_id not in record_ids(alice)


# --- Notices ---------------------------------------------------------------------------------------


def _set_up_notice(plugin, monkeypatch, state: str) -> None:
    if state == "history_off":
        return
    th.set_switch(plugin, "history", True)
    plugin._history_support.reset_in_memory_caches()
    if state == "read_error":
        _alice_fixture(plugin)
        _break_history(plugin, "dirty")


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize("state", ["history_off", "no_history", "read_error"])
async def test_notice_replaces_the_command_and_both_are_deleted(plugin, modes, monkeypatch, caplog, state, lang):
    monkeypatch.setattr(plugin, "_STATS_NOTICE_TTL_SECONDS", 0)
    _set_up_notice(plugin, monkeypatch, state)
    bot = StatsBot()

    with caplog.at_level(logging.DEBUG):
        assert await dispatch(plugin, stats_update(lang=lang), bot)
        # The owner and the contact first see the notice ...
        assert bot.delete_calls == []
        await drain(plugin)

    assert bot.text_edit_calls == [
        {"text": NOTICES[state][lang], "chat_id": CHAT, "message_id": COMMAND_ID, "business_connection_id": BCID}
    ]
    # ... then it goes, together with the command. No parse mode, no details, no reply.
    assert bot.delete_calls == [{"business_connection_id": BCID, "message_ids": [COMMAND_ID]}]
    assert bot.calls == []
    assert "outcome=notice" in caplog.text
    if state == "read_error":
        assert "history could not be read (ValueError)" in caplog.text
    for secret in ("Alice", "Sam", "SECRET", str(plugin._history_support.history_root())):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_failed_notice_edit_replies_and_deletes_both(plugin, modes, monkeypatch):
    monkeypatch.setattr(plugin, "_STATS_NOTICE_TTL_SECONDS", 0)
    bot = StatsBot()
    bot.text_edit_error = ttm.BadRequest("Bad Request: message can't be edited")

    assert await dispatch(plugin, stats_update(), bot)
    await drain(plugin)

    assert bot.calls == [
        {
            "chat_id": CHAT,
            "business_connection_id": BCID,
            "text": NOTICES["history_off"]["en"],
            "reply_to_message_id": COMMAND_ID,
            "disable_notification": True,
        }
    ]
    assert bot.delete_calls == [{"business_connection_id": BCID, "message_ids": [COMMAND_ID, COMMAND_ID + 1]}]


@pytest.mark.asyncio
async def test_notice_command_deleted_before_the_edit_posts_nothing(plugin, modes, monkeypatch, caplog):
    monkeypatch.setattr(plugin, "_STATS_NOTICE_TTL_SECONDS", 0)
    bot = StatsBot()
    bot.text_edit_error = ttm.BadRequest("Bad Request: message to edit not found")

    with caplog.at_level(logging.INFO):
        assert await dispatch(plugin, stats_update(), bot)
        await drain(plugin)

    assert bot.calls == [] and bot.delete_calls == []
    assert "outcome=gone" in caplog.text


@pytest.mark.asyncio
async def test_notice_delivered_despite_a_send_timeout_is_deleted_and_never_recorded(plugin, modes, monkeypatch):
    monkeypatch.setattr(plugin, "_STATS_NOTICE_TTL_SECONDS", 0)
    _set_up_notice(plugin, monkeypatch, "no_history")
    bot = StatsBot()
    bot.text_edit_error = ttm.BadRequest("Bad Request: message can't be edited")
    bot.send_error = ttm.TimedOut("timed out")

    assert await dispatch(plugin, stats_update(), bot)
    # The reply notice landed although its send timed out; Telegram echoes it.
    echo = stats_update(NOTICES["no_history"]["en"], message_id=COMMAND_ID + 1, sender_business_bot=555, update_id=501)
    assert await dispatch(plugin, echo, bot)
    await drain(plugin)

    assert th.load_records(plugin) == []
    # The command goes as usual, and the notice goes once its echo tells its id.
    assert sorted(call["message_ids"] for call in bot.delete_calls) == [[COMMAND_ID], [COMMAND_ID + 1]]


@pytest.mark.asyncio
async def test_failed_delete_only_logs_a_hint(plugin, modes, monkeypatch, caplog):
    monkeypatch.setattr(plugin, "_STATS_NOTICE_TTL_SECONDS", 0)
    bot = StatsBot()
    bot.delete_error = ttm.BadRequest("Bad Request: business connection does not have the right")

    with caplog.at_level(logging.WARNING):
        assert await dispatch(plugin, stats_update(), bot)
        await drain(plugin)

    assert "allow the bot to delete messages" in caplog.text
    assert "does not have the right" not in caplog.text


@pytest.mark.asyncio
async def test_slow_or_broken_card_becomes_the_read_error_notice(alice, monkeypatch, caplog):
    def broken(**_kwargs):
        raise RuntimeError("private detail")

    monkeypatch.setattr(alice._stats_panel, "build_stats_card", broken)
    bot = StatsBot()

    with caplog.at_level(logging.WARNING):
        assert await dispatch(alice, stats_update(), bot)

    assert edit_call_text(bot) == NOTICES["read_error"]["en"]
    assert "could not build the card (RuntimeError)" in caplog.text and "private detail" not in caplog.text
    assert COMMAND_ID not in record_ids(alice)


# --- Never history, never counted ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_card_and_echoes_are_never_recorded_or_counted(alice, caplog):
    before, _ = contact_stats(alice)
    records_before = len(th.load_records(alice))
    bot = StatsBot()

    with caplog.at_level(logging.DEBUG):
        assert await dispatch(alice, stats_update(), bot)
        # Telegram echoes the edit as the owner's edited message, sent by the bot.
        echo = stats_update(CARD_ALL_EN_TEXT, edited=True, sender_business_bot=555, update_id=501)
        assert await dispatch(alice, echo, bot)
        # The owner deletes the card later.
        assert await dispatch(alice, deleted_update(COMMAND_ID), bot)
        # A second command sees exactly the same numbers.
        assert await dispatch(alice, stats_update(message_id=COMMAND_ID + 5, update_id=502), bot)

    assert len(th.load_records(alice)) == records_before
    assert [call["rich_message"]["html"] for call in rich_edits(bot)] == [CARD_ALL_EN, CARD_ALL_EN]
    after, _ = contact_stats(alice)
    assert after == before
    # Logs carry ids and the period, never names or text.
    assert "stats command chat=991 message=900 period=all outcome=edited" in caplog.text
    for secret in ("Alice", "Sam", "SECRET", "📊", "/stats"):
        assert secret not in caplog.text


# --- Docs ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docs_card_example_matches_the_real_card(alice):
    section = _doc_section(STATS_DOC.read_text(encoding="utf-8"), "In a Business chat (/stats)")
    bot = StatsBot()
    assert await dispatch(alice, stats_update(), bot)

    [(language, documented)] = _fenced_blocks(section)

    assert language == "html"
    assert documented == edit_html(bot)
    assert "the contact sees it too" in section
    # `/dossier` is the other owner command typed in a Business chat; both post what the contact sees.
    assert "one of the two commands" in section and "/dossier" in section
    readme = README_DOC.read_text(encoding="utf-8")
    assert "docs/STATS.md#in-a-business-chat-stats" in readme
    # Control traffic stays out of history only while its IDs are remembered; both docs say so.
    history = HISTORY_DOC.read_text(encoding="utf-8")
    assert "(STATS.md#in-a-business-chat-stats)" in history
    for doc in (history, section):
        assert "24 hours" in doc and "after a gateway restart" in doc
