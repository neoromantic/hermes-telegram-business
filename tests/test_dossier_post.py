"""What ``/dossier`` posts (``dossier_post``): the stored document only, brought up to date first.

The contact reads the post, so canaries placed everywhere else (the owner note, the previous version, the
persona, another chat's document, a converted v1 document) and the refs must never reach it. When the
document is behind the chat, one forced refresh runs first; when it fails, or the chat changes during it,
nothing is posted.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import test_dossier_panel as tdp
import test_history
from dossier_helpers import (
    BUSINESS_ID,
    CONTACT_ID,
    NOW,
    OWNER_ID,
    FakeLlm,
    ago,
    catalog_entry,
    cycle,
    delete,
    doc,
    dossier_dir,
    edit,
    read_dossier,
    read_persona,
    seed,
    snapshot_tree,
)
from test_dossier_migrate import fact, put, v1_contact

plug = tdp.plug
ds = tdp.ds
panel = tdp.panel

ROOT = Path(__file__).resolve().parents[1]
CHAT = tdp.CHAT
OTHER_CHAT = 992
SECOND = timedelta(seconds=1)

CANARY_NOTE = "CANARY-NOTE-7Q"
CANARY_PREVIOUS = "CANARY-PREV-2M"
CANARY_PERSONA = "CANARY-PERSONA-9T"
CANARY_OTHER = "CANARY-OTHER-8P"
CANARY_V1 = "CANARY-V1-3W"
CANARIES = (CANARY_NOTE, CANARY_PREVIOUS, CANARY_PERSONA, CANARY_OTHER, CANARY_V1)
# Owner-only metadata the document file holds next to the text: the model route, the cost, the ids.
METADATA = ("gemini", "ai-gateway", "$", "[m", "m10", "m11", BUSINESS_ID, str(CHAT), str(CONTACT_ID))

ASK = "Can we meet on Thursday at the north gate?"
AGREE = "Yes, Thursday at 10 works for me."
OLD_LINE = "Meets on Thursdays."
NEW_LINE = "Meets at the north gate at 10."
CHANGED = "Messages changed while the dossier was being refreshed. Send /dossier again."


@pytest.fixture(autouse=True)
def _isolated_env(dossier_env: Callable[..., None]) -> Callable[..., None]:
    return dossier_env


@pytest.fixture
def post(panel: Any) -> ModuleType:
    return panel._post


def contact_doc(name: str, line: str) -> str:
    return doc(f"**{name}, a client.**\n\n## Notes\n- {line} [m10, m11]")


async def live_chat(plugin: ModuleType) -> dict[str, Any]:
    """Two messages, one from each side, within the last 24 hours; returns the catalog entry."""
    await seed(plugin, CHAT, "anna_k", [(10, "in", ASK, 120), (11, "out", AGREE, 118)], first_name="Anna")
    return catalog_entry(plugin, CHAT)


def refresh_now(ds: ModuleType, entry: dict[str, Any], line: str) -> None:
    """One forced refresh, so the document has seen the chat and the note."""
    rep = cycle(ds, FakeLlm(contact_doc("Anna", line)), target=("contact", entry), now=datetime.now(UTC))
    assert rep.contact in ("refreshed", "checked"), rep


async def current(plugin: ModuleType, ds: ModuleType, line: str = OLD_LINE) -> dict[str, Any]:
    """The live chat with a document that is up to date; returns the catalog entry."""
    entry = await live_chat(plugin)
    refresh_now(ds, entry, line)
    return entry


async def scene(plugin: ModuleType, ds: ModuleType) -> dict[str, Any]:
    """An up-to-date document plus a canary in every place the post must not read; returns the catalog entry."""
    entry = await current(plugin, ds, CANARY_PREVIOUS)
    ds.set_note(("contact", entry), f"{CANARY_NOTE}: never mention the discount.")
    refresh_now(ds, entry, NEW_LINE)  # the note is seen, and the canary line moves to the previous version
    assert cycle(ds, FakeLlm(doc(f"## Style\n- {CANARY_PERSONA}")), target=("persona", None)).persona == "refreshed"
    await seed(plugin, OTHER_CHAT, "boris_v", [(1, "in", f"{CANARY_OTHER} question", 50),
                                              (2, "out", f"{CANARY_OTHER} answer", 45)], first_name="Boris")
    other = catalog_entry(plugin, OTHER_CHAT)
    assert cycle(ds, FakeLlm(contact_doc("Boris", CANARY_OTHER)), target=("contact", other)).refreshed == 1
    # Every canary is really on disk, so the checks below mean something.
    stored = read_dossier(ds, CHAT)
    assert NEW_LINE in stored["document"] and "[m10, m11]" in stored["document"]
    assert CANARY_PREVIOUS in stored["previous"] and CANARY_NOTE in stored["note"]
    assert CANARY_PERSONA in read_persona(ds)["document"]
    assert stored["last_call"] is not None
    return entry


def assert_clean(text: str) -> None:
    for secret in (*CANARIES, *METADATA):
        assert secret not in text, secret


async def _command(panel: Any, llm: Any = None) -> tdp.PanelBot:
    """The owner's ``/dossier``, with ``llm`` bound when given; returns the bot that recorded it."""
    if llm is not None:
        panel._ds.bind_llm(llm)
    bot = tdp.PanelBot()
    assert await tdp.run_command(panel, tdp.command_update(), bot)
    return bot


def _html(bot: tdp.PanelBot) -> str:
    (sent,) = tdp.posts(bot)
    assert bot.sent == []
    return sent["rich_message"]["html"]


# ---------------------------------------------------------------------------
# G2: the post is the document body only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_post_is_the_stored_document_and_nothing_owner_only(plug, ds, panel, post):
    entry = await scene(plug, ds)
    llm = FakeLlm()

    html = _html(await _command(panel, llm))

    assert llm.calls == []  # nothing changed, so no model call
    assert html.startswith("<h3>Dossier</h3><p><b>Anna, a client.</b></p><h4>Notes</h4>")
    assert f"<li>{NEW_LINE}</li>" in html and "Prepared automatically from this chat on" in html
    assert_clean(html)
    state = post.post_state(entry, now=datetime.now(UTC))
    assert (state.reason, state.text) == (None, read_dossier(ds, CHAT)["document"])


@pytest.mark.asyncio
async def test_no_message_ref_reaches_the_post_in_any_form(plug, ds, panel):
    await live_chat(plug)
    refs = "[M10] [м11] (m10) **[m11]** [ m10 , m 11 ] [m10…m11] `[m10]`"  # forms a model may slip into
    llm = FakeLlm(doc(f"## Notes\n- {NEW_LINE} {refs}"))

    html = _html(await _command(panel, llm))

    assert f"<li>{NEW_LINE}</li>" in html
    for leftover in ("m10", "M10", "м11", "**", "`", "<b></b>"):
        assert leftover not in html, leftover


def test_the_post_reads_no_field_of_the_document_but_its_text():
    # The plan reads the note to see whether it changed, but only the document text leaves this module.
    tree = ast.parse((ROOT / "dossier_post.py").read_text(encoding="utf-8"))
    keys = {node.slice.value for node in ast.walk(tree)
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)}
    assert keys == {"document"}


@pytest.mark.asyncio
async def test_a_converted_v1_document_is_never_posted_before_a_refresh(plug, ds, panel, post):
    entry = await live_chat(plug)
    put(dossier_dir(ds, CHAT), v1_contact(fact("f1", f"Was rude once ({CANARY_V1})."), conn=BUSINESS_ID, chat=CHAT))
    assert cycle(ds, FakeLlm(), now=datetime.now(UTC) - timedelta(days=1)).calls == 0  # migrates only
    assert CANARY_V1 in read_dossier(ds, CHAT)["document"]
    assert post.post_state(entry, now=datetime.now(UTC)).reason == "due"
    llm = FakeLlm(contact_doc("Anna", NEW_LINE))

    html = _html(await _command(panel, llm))

    assert [call["purpose"] for call in llm.calls] == [post._writer.PURPOSE_CONTACT]
    assert NEW_LINE in html and CANARY_V1 not in html


@pytest.mark.asyncio
async def test_reading_what_may_be_posted_writes_nothing(plug, ds, post):
    entry = await scene(plug, ds)
    await seed(plug, CHAT, "anna_k", [(12, "in", "One more thing.", 0)], first_name="Anna")
    before = snapshot_tree(ds.dossier_root())

    assert post.post_state(entry, now=datetime.now(UTC)).reason == "due"
    assert post.post_precheck(entry, now=datetime.now(UTC)) is None

    assert snapshot_tree(ds.dossier_root()) == before


# ---------------------------------------------------------------------------
# One refresh first when the document is behind the chat
# ---------------------------------------------------------------------------


async def _behind(plug: ModuleType, ds: ModuleType, entry: dict[str, Any], change: str) -> None:
    if change == "new":
        await seed(plug, CHAT, "anna_k", [(12, "in", "Can it be at 10:30?", 5)], first_name="Anna")
    elif change == "edit":
        await edit(plug, CHAT, 11, "Yes, Thursday at 10 at the north gate works for me.", 5)
    elif change == "delete":
        await delete(plug, CHAT, 10, when=5)
    elif change == "note":
        ds.set_note(("contact", entry), "Mention the new office.")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["first", "new", "edit", "delete", "note"])
async def test_a_document_behind_the_chat_is_refreshed_once_and_then_posted(plug, ds, panel, post, change):
    if change == "first":  # no document yet
        entry = await live_chat(plug)
    else:
        entry = await current(plug, ds)
        await _behind(plug, ds, entry, change)
    state = post.post_state(entry, now=datetime.now(UTC))
    assert (state.reason, state.text) == ("due", "")
    llm = FakeLlm(contact_doc("Anna", NEW_LINE))

    html = _html(await _command(panel, llm))

    assert [call["purpose"] for call in llm.calls] == [post._writer.PURPOSE_CONTACT]
    assert NEW_LINE in html and OLD_LINE not in html
    assert post.post_state(entry, now=datetime.now(UTC)).reason is None  # the refresh was stored


@pytest.mark.asyncio
async def test_a_refresh_that_changes_nothing_posts_the_stored_document(plug, ds, panel, post):
    entry = await current(plug, ds)
    await _behind(plug, ds, entry, "new")
    llm = FakeLlm(doc("NO_CHANGE"))

    html = _html(await _command(panel, llm))

    assert len(llm.calls) == 1 and OLD_LINE in html


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [(10, "in", ASK, 30)],  # only the contact wrote
    [(10, "in", ASK, 30), (11, "bot", f"{chr(0x1F399)}{chr(0xFE0F)} a transcript", 29)],  # a transcript is nobody's
])
async def test_without_both_sides_there_is_no_dossier_and_no_call(plug, ds, panel, post, rows):
    await seed(plug, CHAT, "anna_k", rows, first_name="Anna")
    entry = catalog_entry(plug, CHAT)
    llm = FakeLlm()

    bot = await _command(panel, llm)

    assert post.post_state(entry, now=datetime.now(UTC)).reason == "unqualified"
    assert llm.calls == [] and tdp.posts(bot) == []
    assert [sent["text"] for sent in bot.sent] == [
        f"{tdp.HEAD}\nNo dossier yet: it needs messages from both you and the contact."]


@pytest.mark.asyncio
async def test_a_document_with_nothing_worth_keeping_is_not_posted(plug, ds, panel, post):
    entry = await live_chat(plug)
    assert cycle(ds, FakeLlm(doc("EMPTY")), target=("contact", entry), now=datetime.now(UTC)).contact == "checked"
    llm = FakeLlm()

    bot = await _command(panel, llm)

    assert post.post_state(entry, now=datetime.now(UTC)).reason == "empty"
    assert llm.calls == [] and tdp.posts(bot) == []
    assert [sent["text"] for sent in bot.sent] == [f"{tdp.HEAD}\nThe dossier has nothing worth keeping yet."]


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "error"), [
    ("A document without the end line", "no_end_marker"),
    (RuntimeError(f"provider error about {CANARY_NOTE}"), "RuntimeError"),
])
async def test_a_failed_refresh_posts_nothing_not_even_the_stored_document(plug, ds, panel, post, response, error):
    entry = await current(plug, ds)
    await _behind(plug, ds, entry, "new")
    llm = FakeLlm(response)

    bot = await _command(panel, llm)

    assert len(llm.calls) == 1 and tdp.posts(bot) == []
    assert [sent["text"] for sent in bot.sent] == [f"{tdp.HEAD}\nThe dossier could not be prepared ({error})."]
    assert OLD_LINE in read_dossier(ds, CHAT)["document"]


@pytest.mark.asyncio
async def test_no_model_matters_only_when_a_refresh_is_due(plug, ds, panel):
    await current(plug, ds)

    assert len(tdp.posts(await _command(panel))) == 1  # no model bound: the stored document needs none

    await seed(plug, CHAT, "anna_k", [(12, "in", "Can it be at 10:30?", 5)], first_name="Anna")
    bot = tdp.PanelBot()
    assert await tdp.run_command(panel, tdp.command_update(message_id=501), bot)
    assert tdp.posts(bot) == []
    assert [sent["text"] for sent in bot.sent] == [
        f"{tdp.HEAD}\nThe dossier needs a refresh first, and the model is not connected."]


# ---------------------------------------------------------------------------
# A change during the refresh
# ---------------------------------------------------------------------------


async def _change(plug: ModuleType, ds: ModuleType, entry: dict[str, Any], change: str) -> None:
    history = plug._history_support
    if change == "delete":
        await delete(plug, CHAT, 10, when=0)
    elif change == "edit":
        await edit(plug, CHAT, 11, "Actually, Friday at 10 works better.", 0)
    elif change == "note":
        ds.set_note(("contact", entry), "Leave out the meeting place.")
    elif change == "erase":
        history.forget_chat(BUSINESS_ID, CHAT)
    elif change == "forget":
        ds.forget(("contact", entry))
    elif change == "forget_other":  # any forget: the document is not worth the risk
        ds.forget(("contact", {"business_connection_id": BUSINESS_ID, "chat_id": OTHER_CHAT}))
    elif change == "new":
        await seed(plug, CHAT, "anna_k", [(12, "in", "See you there.", 0)], first_name="Anna")
    elif change == "disabled":
        ds.set_enabled(False, via="cli")
    elif change == "chat_off":
        plug._chat_settings.default_store().set_flag(OWNER_ID, CHAT, "dossier", False, via="cli")
    elif change == "history_off":
        test_history.set_switch(plug, "history", False)


def _during_the_call(plug: ModuleType, ds: ModuleType, entry: dict[str, Any], change: str,
                     loop: asyncio.AbstractEventLoop) -> Callable[[dict[str, Any]], None]:
    def hook(_payload: dict[str, Any]) -> None:  # runs in the refresh's worker thread
        asyncio.run_coroutine_threadsafe(_change(plug, ds, entry, change), loop).result(5)

    return hook


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "expected"), [
    ("delete", CHANGED), ("edit", CHANGED), ("note", CHANGED), ("erase", CHANGED), ("forget", CHANGED),
    ("forget_other", CHANGED), ("disabled", "Dossiers are off. Turn them on in Settings (/telesec)."),
    ("chat_off", "Dossier is off for this chat."),
    ("history_off", "History is off, so deleted messages can't be tracked. Turn history on in Settings (/telesec)."),
])
async def test_a_change_during_the_refresh_posts_nothing(plug, ds, panel, change, expected):
    entry = await current(plug, ds)
    await _behind(plug, ds, entry, "new")
    llm = FakeLlm(contact_doc("Anna", NEW_LINE))
    llm.on_call = _during_the_call(plug, ds, entry, change, asyncio.get_running_loop())

    bot = await _command(panel, llm)

    assert len(llm.calls) == 1 and tdp.posts(bot) == []
    assert [sent["text"] for sent in bot.sent] == [f"{tdp.HEAD}\n{expected}"]


@pytest.mark.asyncio
async def test_a_new_message_during_the_refresh_still_posts_the_refreshed_document(plug, ds, panel):
    entry = await current(plug, ds)
    await _behind(plug, ds, entry, "edit")
    llm = FakeLlm(contact_doc("Anna", NEW_LINE))
    llm.on_call = _during_the_call(plug, ds, entry, "new", asyncio.get_running_loop())

    html = _html(await _command(panel, llm))

    assert len(llm.calls) == 1 and NEW_LINE in html


@pytest.mark.asyncio
async def test_a_forget_after_the_refresh_posts_nothing(plug, ds, panel, post, monkeypatch):
    entry = await current(plug, ds)
    await _behind(plug, ds, entry, "new")
    real = post.refresh

    def refresh_then_forget(*args: Any, **kwargs: Any) -> Any:
        outcome = real(*args, **kwargs)
        ds.forget(("contact", {"business_connection_id": BUSINESS_ID, "chat_id": OTHER_CHAT}))
        return outcome

    monkeypatch.setattr(post, "refresh", refresh_then_forget)

    bot = await _command(panel, FakeLlm(contact_doc("Anna", NEW_LINE)))

    assert tdp.posts(bot) == [] and [sent["text"] for sent in bot.sent] == [f"{tdp.HEAD}\n{CHANGED}"]


# ---------------------------------------------------------------------------
# The refresh itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_refresh_waits_for_another_cycle_and_stops_when_told(plug, ds, post, monkeypatch):
    entry = await current(plug, ds)
    await _behind(plug, ds, entry, "new")
    monkeypatch.setattr(post, "BUSY_RETRY_SECONDS", 0.01)
    real, outcomes = post._ds.run_cycle, ["busy", "busy"]

    def run_cycle(**kwargs: Any) -> Any:
        if outcomes:
            return post._ds.CycleReport(outcome=outcomes.pop(0), trigger=kwargs["trigger"])
        return real(**kwargs)

    monkeypatch.setattr(post._ds, "run_cycle", run_cycle)
    llm = FakeLlm(contact_doc("Anna", NEW_LINE))

    assert post.refresh(entry, llm=llm, stop=threading.Event()) == (None, None)
    assert outcomes == [] and len(llm.calls) == 1

    outcomes.extend(["busy"] * 1000)
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    assert post.refresh(entry, llm=llm, stop=stop) == ("failed", "stopped")
    assert len(llm.calls) == 1


@pytest.mark.parametrize(("report", "expected"), [
    ({"outcome": "disabled"}, ("disabled", None)),
    ({"outcome": "history_off"}, ("history_off", None)),
    ({"outcome": "chat_off"}, ("chat_off", None)),
    ({"outcome": "aborted_trust"}, ("failed", "trust_denied")),
    ({"contact": "refreshed"}, (None, None)),
    ({"contact": "checked"}, (None, None)),
    ({"contact": "unchanged"}, (None, None)),
    ({"contact": "unqualified"}, ("unqualified", None)),
    ({"contact": "discarded"}, ("changed", None)),
    ({"contact": "paused"}, ("chat_off", None)),
    ({"contact": "failed", "failures": [("chat", "no_end_marker")]}, ("failed", "no_end_marker")),
    ({"contact": "stopped", "outcome": "stopped"}, ("failed", "stopped")),
    ({"contact": "backoff"}, ("failed", "backoff")),
])
def test_each_refresh_outcome_has_its_reason(plug, ds, post, report, expected):
    entry = {"business_connection_id": BUSINESS_ID, "chat_id": CHAT}

    assert post._refresh_reason(entry, ds.CycleReport(**report)) == expected


@pytest.mark.asyncio
async def test_a_refresh_stopped_by_a_switch_names_the_switch(plug, ds, post):
    entry = await live_chat(plug)
    stopped = ds.CycleReport(outcome="stopped", contact="stopped")
    test_history.set_switch(plug, "history", False)

    assert post._refresh_reason(entry, stopped) == ("history_off", None)


# ---------------------------------------------------------------------------
# Refusals before the document is read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_precheck_reasons_come_in_order(plug, ds, post):
    entry = await live_chat(plug)
    store = plug._chat_settings.default_store()
    ds.set_enabled(False, via="cli")
    test_history.set_switch(plug, "history", False)
    store.set_flag(OWNER_ID, CHAT, "dossier", False, via="cli")
    steps = [
        lambda: ds.set_enabled(True, via="cli"),
        lambda: test_history.set_switch(plug, "history", True),
        lambda: store.set_flag(OWNER_ID, CHAT, "dossier", True, via="cli"),
    ]

    reasons = [post.post_precheck(entry, now=NOW)]
    for step in steps:
        step()
        reasons.append(post.post_precheck(entry, now=NOW))

    assert reasons == ["disabled", "history_off", "chat_off", None]


@pytest.mark.asyncio
async def test_the_window_needs_a_contact_message_in_the_last_24_hours(plug, ds, post):
    await seed(plug, CHAT, "anna_k", [(10, "in", ASK, timedelta(hours=30)), (11, "out", AGREE, timedelta(hours=29)),
                                      (12, "out", "Are you there?", 60)], first_name="Anna")
    entry = catalog_entry(plug, CHAT)
    tdp.up_two_days(plug)
    assert post.post_precheck(entry, now=NOW) == "window"  # the owner's own message does not open it

    marker = chr(0x1F399) + chr(0xFE0F)
    await seed(plug, CHAT, "anna_k", [(40, "bot", f"{marker} the owner's voice note", 30)], first_name="Anna")
    assert post.post_precheck(entry, now=NOW) == "window"  # a transcript is nobody's side
    assert post.post_window_open(entry, now=NOW) is False
    assert post.post_state(entry, now=NOW).reason == "due"  # the CLI never sees media, so it never says "window"

    post.note_inbound(BUSINESS_ID, CHAT, ago(20))  # a sticker, photo or voice note from the contact
    assert post.post_precheck(entry, now=NOW) is None and post.post_window_open(entry, now=NOW) is True
    post.note_inbound(BUSINESS_ID, CHAT, ago(25 * 60))  # an older one delivered late changes nothing
    assert post.post_precheck(entry, now=ago(20) + timedelta(hours=24) - SECOND) is None
    assert post.post_precheck(entry, now=ago(20) + timedelta(hours=24) + SECOND) == "window"

    later = NOW + timedelta(hours=2)
    await seed(plug, CHAT, "anna_k", [(50, "in", "Sorry, wrong chat.", ago(10, base=later))], first_name="Anna")
    await delete(plug, CHAT, 50, when=ago(9, base=later))
    later += timedelta(hours=23)
    assert post.post_precheck(entry, now=later) is None  # a message the contact deleted was still written


@pytest.mark.asyncio
async def test_a_gateway_up_less_than_24_hours_tries_the_post(plug, ds, post):
    await seed(plug, CHAT, "anna_k", [(10, "in", ASK, timedelta(hours=30)), (11, "out", AGREE, timedelta(hours=29))],
               first_name="Anna")
    entry = catalog_entry(plug, CHAT)
    post._STARTED_AT = time.monotonic() - 23 * 3600  # a voice note just before the start would not have been seen

    assert post.post_precheck(entry, now=NOW) is None
    assert post.post_window_open(entry, now=NOW) is False  # not known open: Telegram's refusal means the window
    # Uptime is not wall-clock time: a clock set two days forward after the start (a machine that booted with
    # the wrong time) makes the gateway no older.
    assert post.post_precheck(entry, now=NOW + timedelta(days=2)) is None
    post._STARTED_AT = time.monotonic() - 25 * 3600
    assert post.post_precheck(entry, now=NOW) == "window"


@pytest.mark.asyncio
async def test_a_chat_the_inbound_cap_may_have_dropped_is_not_known_closed(plug, ds, post, monkeypatch):
    await seed(plug, CHAT, "anna_k", [(10, "in", ASK, timedelta(hours=30)), (11, "out", AGREE, timedelta(hours=29))],
               first_name="Anna")
    entry = catalog_entry(plug, CHAT)
    tdp.up_two_days(plug)
    monkeypatch.setattr(post, "INBOUND_MAX", 2)

    post.note_inbound(BUSINESS_ID, CHAT, ago(26 * 60))  # too old to matter when the cap drops it
    post.note_inbound(BUSINESS_ID, 9001, ago(50))
    post.note_inbound(BUSINESS_ID, 9002, ago(40))
    assert len(post._LAST_INBOUND) == 2 and post.post_precheck(entry, now=NOW) == "window"

    post.note_inbound(BUSINESS_ID, CHAT, ago(30))  # a sticker from the contact, dropped by the next two chats
    post.note_inbound(BUSINESS_ID, 9003, ago(20))
    post.note_inbound(BUSINESS_ID, 9004, ago(10))
    assert (BUSINESS_ID, str(CHAT)) not in post._LAST_INBOUND
    assert post.post_precheck(entry, now=NOW) is None  # unknown, so the post is tried
    assert post.post_window_open(entry, now=NOW) is False  # and Telegram's refusal is told as the window
    assert post.post_precheck(entry, now=ago(30) + timedelta(hours=24) + SECOND) == "window"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disabled", "history_off", "chat_off", "window"])
async def test_each_refusal_makes_no_model_call(plug, ds, panel, reason):
    await tdp._refusal_case(plug, ds, reason)
    llm = FakeLlm()

    bot = await _command(panel, llm)

    assert llm.calls == [] and tdp.posts(bot) == []
    assert bot.sent[0]["text"].startswith(f"{tdp.HEAD}\n")


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_carry_codes_and_counts_only(plug, ds, panel, caplog):
    with caplog.at_level(logging.DEBUG):
        entry = await scene(plug, ds)
        bot = tdp.PanelBot()
        assert await tdp.run_command(panel, tdp.command_update(message_id=500), bot)  # posted as it is
        await _behind(plug, ds, entry, "new")
        ds.bind_llm(FakeLlm(RuntimeError(f"provider echoed {CANARY_NOTE} and {ASK}"), "no end line",
                            contact_doc("Anna", NEW_LINE)))
        for message_id in (501, 502, 503):  # failed with an exception, failed validation, posted after a refresh
            await tdp.run_command(panel, tdp.command_update(message_id=message_id), bot)

    assert len(tdp.posts(bot)) == 2
    assert "dossier posted chat_ref=" in caplog.text and "refreshed=True" in caplog.text
    assert "dossier post refresh chat_ref=" in caplog.text
    assert "reason=failed error=RuntimeError" in caplog.text and "reason=failed error=no_end_marker" in caplog.text
    assert not [r for r in caplog.records if r.exc_info]
    for text in (*CANARIES, ASK, AGREE, NEW_LINE, "anna_k", "Anna", f"chat_id={CHAT}"):
        assert text not in caplog.text
