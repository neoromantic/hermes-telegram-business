"""The ``/dossier`` summary (``dossier_post``): what it may read, when it refuses, and the final recheck.

The contact reads the summary, so it is built from the chat's live messages only. Canaries placed
everywhere else (the owner note, the document and its previous version, the persona, a deleted, a
replaced and a pre-edit message, another chat) must never reach the model input or the posted message.
The transcript of a deleted voice note is still shown in the chat, so it may go in, marked as a voice.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import importlib
import io
import logging
import os
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
    BOT_ID,
    BUSINESS_ID,
    CONTACT_ID,
    NOW,
    OWNER_ID,
    FakeBot,
    FakeLlm,
    ago,
    catalog_entry,
    cycle,
    delete,
    doc,
    edit,
    load_records,
    maintain,
    make_business_text_update,
    read_dossier,
    read_persona,
    seed,
    snapshot_tree,
)

plug = tdp.plug
ds = tdp.ds
panel = tdp.panel

ROOT = Path(__file__).resolve().parents[1]
CHAT = tdp.CHAT
OTHER_CHAT = 992
SECOND = timedelta(seconds=1)
MARK = chr(0x1F399) + chr(0xFE0F)  # the transcript marker as the plugin posts it

CANARY_NOTE = "CANARY-NOTE-7Q"
CANARY_DOC = "CANARY-DOC-4K"
CANARY_PREVIOUS = "CANARY-PREV-2M"
CANARY_PERSONA = "CANARY-PERSONA-9T"
CANARY_DELETED = "CANARY-DELETED-5R"
CANARY_REPLACED = "ZQX7"  # short, so the resent text still counts as a correction
CANARY_PRE_EDIT = "CANARY-PREEDIT-3W"
CANARY_OTHER = "CANARY-OTHER-8P"
CANARY_OLD = "CANARY-OLD-6D"
CANARIES = (CANARY_NOTE, CANARY_DOC, CANARY_PREVIOUS, CANARY_PERSONA, CANARY_DELETED, CANARY_REPLACED,
            CANARY_PRE_EDIT, CANARY_OTHER)

ASK = "Can we meet on Thursday at the north gate?"
AGREE = "Yes, Thursday at 10 works for me."
CORRECTED = "Let us meet at the north gate tomorrow morning around nine"
BUDGET = "The budget is 300 rubles."
VOICE = (f"{MARK} first part of the voice note", "second part of the voice note", "third part of the voice note")
LIVE = (("contact", ASK, False), ("owner", AGREE, False), ("owner", CORRECTED, False), ("contact", BUDGET, False),
        *(("unknown", text, True) for text in VOICE))
CHANGED = "Messages changed while the summary was being written. Send /dossier again."


@pytest.fixture(autouse=True)
def _isolated_env(dossier_env: Callable[..., None]) -> Callable[..., None]:
    return dossier_env


@pytest.fixture
def post(dossier_plugin: ModuleType) -> ModuleType:
    return importlib.import_module(f"{dossier_plugin.__name__}.dossier_post")


def echo(payload: dict[str, Any], _kwargs: dict[str, Any]) -> str:
    """A summary that repeats every input text, so anything the model saw would reach the post."""
    return doc("## Messages\n" + "\n".join(f"- {m['text']}" for m in payload["messages"]))


def contact_doc(name: str, line: str) -> str:
    return doc(f"**{name}, a client.**\n\n## Notes\n- {line} [m10]")


async def transcript(plugin: ModuleType, chat_id: int, voice_id: int, first_id: int, at: datetime) -> None:
    """The plugin's transcript of a contact voice note: chunk 0 replies to the voice, the next chunks follow."""
    history = plugin._history_support
    for index, text in enumerate(VOICE):
        when = at + 4 * index * SECOND
        update = make_business_text_update(
            text=text, business_id=BUSINESS_ID, chat_id=chat_id, chat_username="anna_k", chat_first_name="Anna",
            chat_last_name=None, message_id=first_id + index, update_id=70_000 + first_id + index, date=when,
            reply_to_message_id=voice_id if index == 0 else None, from_user_id=OWNER_ID,
            sender_business_bot_id=BOT_ID)
        assert await history.observe_ptb_update(update, bot=FakeBot(owner_id=OWNER_ID), now=when)


async def live_chat(plugin: ModuleType) -> dict[str, Any]:
    """Two live messages, one from each side, within the last 24 hours; returns the catalog entry."""
    await seed(plugin, CHAT, "anna_k", [(10, "in", ASK, 120), (11, "out", AGREE, 118)], first_name="Anna")
    return catalog_entry(plugin, CHAT)


async def scene(plugin: ModuleType, ds: ModuleType) -> dict[str, Any]:
    """The live chat plus a canary in every place the summary must not read; returns the catalog entry."""
    entry = await live_chat(plugin)
    assert cycle(ds, FakeLlm(contact_doc("Anna", CANARY_PREVIOUS)), target=("contact", entry)).refreshed == 1
    await seed(plugin, CHAT, "anna_k", [(12, "in", f"My old code word is {CANARY_DELETED}.", 100)], first_name="Anna")
    await delete(plugin, CHAT, 12, when=99)
    t = ago(90)  # 13 is deleted and resent fixed as 14
    replaced = f"Let us meet at the north gate {CANARY_REPLACED} tomorrow morning around nine"
    await seed(plugin, CHAT, "anna_k", [(13, "out", replaced, t)], first_name="Anna")
    await delete(plugin, CHAT, 13, when=t + SECOND)
    await seed(plugin, CHAT, "anna_k", [(14, "out", CORRECTED, t + 5 * SECOND)], first_name="Anna")
    maintain(plugin, t + 20 * SECOND)
    await seed(plugin, CHAT, "anna_k", [(15, "in", f"The budget is {CANARY_PRE_EDIT} rubles.", 80)], first_name="Anna")
    await edit(plugin, CHAT, 15, BUDGET, 79)
    t = ago(70)  # a voice note (20) the bot transcribed; the contact deleted the voice later
    await transcript(plugin, CHAT, 20, 21, t)
    await delete(plugin, CHAT, 20, when=t + 60 * SECOND)
    maintain(plugin, t + 90 * SECOND)
    classified = {str(r["message_id"]): r["classification"]
                  for r in load_records(plugin, chat_id=CHAT) if r["event_type"] == "deletion.classified"}
    assert classified["13"] == "likely_correction"
    assert cycle(ds, FakeLlm(contact_doc("Anna", CANARY_DOC)), target=("contact", entry)).refreshed == 1
    assert cycle(ds, FakeLlm(doc(f"## Style\n- {CANARY_PERSONA}")), target=("persona", None)).persona == "refreshed"
    ds.set_note(("contact", entry), f"{CANARY_NOTE}: never mention the discount.")
    await seed(plugin, OTHER_CHAT, "boris_v", [(1, "in", f"{CANARY_OTHER} question", 50),
                                              (2, "out", f"{CANARY_OTHER} answer", 45)], first_name="Boris")
    other = catalog_entry(plugin, OTHER_CHAT)
    assert cycle(ds, FakeLlm(contact_doc("Boris", CANARY_OTHER)), target=("contact", other)).refreshed == 1
    # Every canary is really on disk, so the checks below mean something.
    stored = read_dossier(ds, CHAT)
    assert CANARY_DOC in stored["document"] and CANARY_PREVIOUS in stored["previous"] and CANARY_NOTE in stored["note"]
    assert CANARY_PERSONA in read_persona(ds)["document"]
    return entry


def assert_clean(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text, canary


# ---------------------------------------------------------------------------
# G1-G3: the input is the live chat only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_post_reads_only_the_live_chat(plug, ds, panel, post):
    await scene(plug, ds)
    llm = FakeLlm(echo)
    ds.bind_llm(llm)
    bot = tdp.PanelBot()

    assert await tdp.run_command(panel, tdp.command_update(), bot)

    (call,) = llm.calls
    assert (call["purpose"], call["system"]) == ("telesec_post", post._writer.POST_SYSTEM)
    messages = call["user"]["messages"]
    assert [(m["from"], m["text"], m.get("voice", False)) for m in messages] == list(LIVE)
    assert all(set(m) <= {"at", "from", "text", "voice"} for m in messages)  # no refs, no ids
    assert_clean(call["raw"])
    (sent,) = tdp.posts(bot)
    html = sent["rich_message"]["html"]
    assert_clean(html)
    assert ASK in html and "third part of the voice note" in html  # the echo really reached the post
    assert bot.sent == []


@pytest.mark.asyncio
async def test_build_post_never_reads_documents_notes_the_persona_or_the_state(plug, ds, post, monkeypatch):
    entry = await scene(plug, ds)
    root = ds.dossier_root()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the post read stored dossier data")

    for module, name in ((ds._doc, "read_doc"), (ds, "load_contact"), (ds, "load_persona"), (ds, "load_state")):
        monkeypatch.setattr(module, name, forbidden)
    opened: list[str] = []
    real_open = io.open

    def spying_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, str | bytes | os.PathLike):
            path = Path(os.fsdecode(file))
            if path.is_relative_to(root) and path.name != "settings.json":  # the on/off switch is fine
                opened.append(path.relative_to(root).as_posix())
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", spying_open)
    monkeypatch.setattr(builtins, "open", spying_open)

    result = post.build_post(entry, llm=FakeLlm(echo), now=NOW)
    clean = list(opened)
    read_dossier(ds, CHAT)  # the spy sees an ordinary read on this Python

    monkeypatch.undo()
    assert result.status == "ok" and clean == []
    assert opened == [f"chats/{ds.chat_key(BUSINESS_ID, CHAT)}/document.json"]
    assert_clean(result.text)
    assert "third part of the voice note" in result.text


def test_dossier_post_imports_only_the_chat_side():
    tree = ast.parse((ROOT / "dossier_post.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level
                for alias in node.names}
    assert imported == {"dossier_support", "dossier_writer", "history_support"}  # never dossier_doc


@pytest.mark.asyncio
async def test_payload_and_inputs_match_what_the_build_sends(plug, ds, post):
    entry = await scene(plug, ds)
    before = snapshot_tree(ds.dossier_root())

    inputs = post.post_inputs(entry, now=NOW)
    payload = post.post_payload(entry, now=NOW)  # no model is bound: preview --post needs none
    llm = FakeLlm(echo)
    result = post.build_post(entry, llm=llm, now=NOW)

    assert [m.key for m in inputs] == ["10", "11", "14", "15", "21", "22", "23"]
    assert result.inputs == {m.key: m.fp for m in inputs}
    assert payload == (post._writer.POST_SYSTEM, llm.calls[0]["raw"])
    assert snapshot_tree(ds.dossier_root()) == before  # reading writes nothing


@pytest.mark.asyncio
async def test_the_input_keeps_the_newest_messages_within_the_limits(plug, ds, post, monkeypatch, dossier_env):
    await seed(plug, CHAT, "anna_k", [(i, "in" if i % 2 else "out", f"message number {i}", 100 - i)
                                      for i in range(1, 21)], first_name="Anna")
    await seed(plug, CHAT, "anna_k", [(30, "in", "a message from long ago", timedelta(days=40))], first_name="Anna")
    entry = catalog_entry(plug, CHAT)
    dossier_env(BACKFILL_DAYS=30, BACKFILL_MESSAGES=10)

    assert [m.key for m in post.post_inputs(entry, now=NOW)] == [str(i) for i in range(11, 21)]
    monkeypatch.setattr(post, "POST_MAX_CHARS", 3 * (len("message number 20") + 80))
    assert [m.key for m in post.post_inputs(entry, now=NOW)] == ["18", "19", "20"]


@pytest.mark.asyncio
async def test_a_message_older_than_the_backfill_window_stays_out(plug, ds, panel, dossier_env):
    await seed(plug, CHAT, "anna_k", [(9, "in", f"My old code word is {CANARY_OLD}.", timedelta(days=2))],
               first_name="Anna")
    entry = await live_chat(plug)
    dossier_env(BACKFILL_DAYS=1)
    # The history read reaches FOLD_MARGIN further back, so only the window itself keeps the canary out.
    assert "9" in ds.read_chat_view(entry, since=NOW - timedelta(days=1) - ds.FOLD_MARGIN).messages
    llm = FakeLlm(echo)
    ds.bind_llm(llm)
    bot = tdp.PanelBot()

    assert await tdp.run_command(panel, tdp.command_update(), bot)

    (call,) = llm.calls
    assert [m["text"] for m in call["user"]["messages"]] == [ASK, AGREE]
    (sent,) = tdp.posts(bot)
    assert ASK in sent["rich_message"]["html"] and CANARY_OLD not in sent["rich_message"]["html"]


# ---------------------------------------------------------------------------
# Refusals before the model call
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
        lambda: ds.bind_llm(FakeLlm()),
        lambda: test_history.set_switch(plug, "history", True),
        lambda: store.set_flag(OWNER_ID, CHAT, "dossier", True, via="cli"),
    ]

    reasons = [post.post_precheck(entry, now=NOW)]
    for step in steps:
        step()
        reasons.append(post.post_precheck(entry, now=NOW))

    assert reasons == ["disabled", "no_model", "history_off", "chat_off", None]


@pytest.mark.asyncio
async def test_the_window_needs_a_contact_message_in_the_last_24_hours(plug, ds, post):
    await seed(plug, CHAT, "anna_k", [(10, "in", ASK, timedelta(hours=30)), (11, "out", AGREE, timedelta(hours=29)),
                                      (12, "out", "Are you there?", 60)], first_name="Anna")
    entry = catalog_entry(plug, CHAT)
    ds.bind_llm(FakeLlm())
    tdp.up_two_days(plug)
    assert post.post_precheck(entry, now=NOW) == "window"  # the owner's own message does not open it

    await transcript(plug, CHAT, 40, 41, ago(30))  # the owner's voice note: a transcript is nobody's side
    assert post.post_precheck(entry, now=NOW) == "window"
    assert post.post_window_open(entry, now=NOW) is False
    _system, user = post.post_payload(entry, now=NOW)  # the CLI never sees media, so it never says "window"
    assert ASK in user and VOICE[1] in user

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
    ds.bind_llm(FakeLlm())
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
    ds.bind_llm(FakeLlm())
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
@pytest.mark.parametrize("rows", [
    [(10, "in", ASK, 30)],  # only the contact wrote
    [(10, "in", ASK, 30), (11, "bot", f"{MARK} a transcript", 29)],  # a transcript is nobody's side
])
async def test_without_both_sides_the_post_is_empty_without_a_call(plug, ds, post, rows):
    await seed(plug, CHAT, "anna_k", rows, first_name="Anna")
    entry = catalog_entry(plug, CHAT)
    llm = FakeLlm()
    ds.bind_llm(llm)

    assert post.post_precheck(entry, now=NOW) == "empty"
    assert post.post_payload(entry, now=NOW) == "empty"
    result = post.build_post(entry, llm=llm, now=NOW)
    assert (result.status, result.text, result.error) == ("empty", "", None)
    assert llm.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disabled", "no_model", "history_off", "chat_off", "window", "empty"])
async def test_each_refusal_makes_no_model_call(plug, ds, panel, reason):
    await tdp._refusal_case(plug, ds, reason)
    llm = FakeLlm(echo)
    if reason != "no_model":
        ds.bind_llm(llm)
    bot = tdp.PanelBot()

    assert await tdp.run_command(panel, tdp.command_update(), bot)

    assert llm.calls == [] and tdp.posts(bot) == []
    assert bot.sent[0]["text"].startswith(f"{tdp.HEAD}\n")


# ---------------------------------------------------------------------------
# Model answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_summary_comes_back_without_refs(plug, ds, post):
    entry = await live_chat(plug)
    llm = FakeLlm(doc("## Agreements\n- Meet on **Thursday** at 10 [m10, m11]."), model="google/gemini-3.8-flash")

    result = post.build_post(entry, llm=llm, now=NOW)

    assert (result.status, result.error) == ("ok", None)
    assert result.text == "## Agreements\n- Meet on **Thursday** at 10."
    assert (result.provider, result.model) == ("ai-gateway", "google/gemini-3.8-flash")
    assert result.cost_usd is not None and result.inputs.keys() == {"10", "11"}
    (call,) = llm.calls
    # No timeout on the task route: Hermes reads auxiliary.telesec_dossier.timeout, so the owner's value wins.
    assert (call["purpose"], call["task"], call["timeout"]) == ("telesec_post", "telesec_dossier", None)


@pytest.mark.asyncio
async def test_an_explicit_route_has_its_own_timeout(plug, ds, post):
    entry = await live_chat(plug)
    ds.set_task_registered(False)  # no task config to read a timeout from
    llm = FakeLlm(doc("## Agreements\n- Meet on Thursday at 10 [m10]."))

    assert post.build_post(entry, llm=llm, now=NOW).status == "ok"

    (call,) = llm.calls
    assert "task" not in call and call["timeout"] == post.POST_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_an_empty_answer_is_empty(plug, ds, post):
    entry = await live_chat(plug)

    result = post.build_post(entry, llm=FakeLlm(doc("EMPTY")), now=NOW)

    assert (result.status, result.text, result.error) == ("empty", "", None)
    assert result.inputs.keys() == {"10", "11"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("responses", "error", "calls"), [
    (("A summary without the end line",), "no_end_marker", 1),
    ((doc("NO_CHANGE"),), "bad_no_change", 1),
    ((doc(""),), "empty", 1),
    ((doc("Just one sentence without sections."),), "unstructured", 1),
    ((doc("## Notes\n" + "- a long point\n" * 400),) * 2, "too_long", 2),  # one shorten call, still too long
    ((RuntimeError(f"provider error about {CANARY_DOC}"),), "RuntimeError", 1),
])
async def test_a_bad_answer_fails_with_its_code(plug, ds, post, responses, error, calls):
    entry = await live_chat(plug)
    llm = FakeLlm(*responses)

    result = post.build_post(entry, llm=llm, now=NOW)

    assert (result.status, result.text, result.error) == ("failed", "", error)
    assert len(llm.calls) == calls


@pytest.mark.asyncio
async def test_a_stopped_build_makes_no_further_model_call(plug, ds, post):
    entry = await live_chat(plug)
    stopped = threading.Event()
    llm = FakeLlm(doc("## Notes\n" + "- a long point\n" * 400))  # too long: the writer would ask to shorten it
    llm.on_call = lambda _payload: stopped.set()  # the owner stops the post during the first call

    result = post.build_post(entry, llm=llm, now=NOW, allowed=lambda: not stopped.is_set())

    assert (result.status, result.text) == ("failed", "")
    assert [call["purpose"] for call in llm.calls] == ["telesec_post"]
    assert post.build_post(entry, llm=llm, now=NOW, allowed=lambda: False).status == "failed"
    assert len(llm.calls) == 1  # stopped before the first call


# ---------------------------------------------------------------------------
# The final recheck
# ---------------------------------------------------------------------------


async def _change(plug: ModuleType, ds: ModuleType, entry: dict[str, Any], change: str) -> None:
    history = plug._history_support
    if change == "delete":
        await delete(plug, CHAT, 10, when=0)
    elif change == "edit":
        await edit(plug, CHAT, 11, "Actually, Friday at 10 works better.", 0)
    elif change == "erase":
        history.forget_chat(BUSINESS_ID, CHAT)
    elif change == "prune":  # retention or size pruning removed the month that holds the messages
        chat_dir = history._entry_chat_dir(entry)
        for path in list(history._iter_history_files(chat_dir)):
            path.unlink()
        history._invalidate_chat_cache(chat_dir)
    elif change == "forget":
        ds.forget(("contact", entry))
    elif change == "forget_other":  # any forget: the summary is not worth the risk
        ds.forget(("contact", {"business_connection_id": BUSINESS_ID, "chat_id": OTHER_CHAT}))
    elif change == "disabled":
        ds.set_enabled(False, via="cli")
    elif change == "chat_off":
        plug._chat_settings.default_store().set_flag(OWNER_ID, CHAT, "dossier", False, via="cli")
    elif change == "history_off":
        test_history.set_switch(plug, "history", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(("change", "reason"), [
    ("delete", "changed"), ("edit", "changed"), ("erase", "changed"), ("prune", "changed"), ("forget", "changed"),
    ("forget_other", "changed"), ("disabled", "disabled"), ("chat_off", "chat_off"), ("history_off", "history_off"),
])
async def test_recheck_refuses_a_chat_that_changed_during_the_build(plug, ds, post, change, reason):
    entry = await live_chat(plug)
    gen0 = ds.generation()
    result = post.build_post(entry, llm=FakeLlm(), now=NOW)
    assert result.status == "ok"
    assert post.post_recheck(entry, result.inputs, gen0=gen0, now=NOW) is None

    await _change(plug, ds, entry, change)

    assert post.post_recheck(entry, result.inputs, gen0=gen0, now=datetime.now(UTC)) == reason


@pytest.mark.asyncio
async def test_a_message_outside_the_input_does_not_stop_the_post(plug, ds, post):
    entry = await live_chat(plug)
    gen0 = ds.generation()
    result = post.build_post(entry, llm=FakeLlm(), now=NOW)

    await seed(plug, CHAT, "anna_k", [(12, "in", "One more thing.", 0)], first_name="Anna")
    await delete(plug, CHAT, 12, when=0)

    assert post.post_recheck(entry, result.inputs, gen0=gen0, now=datetime.now(UTC)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "edit", "erase", "forget"])
async def test_a_change_during_the_model_call_posts_nothing(plug, ds, panel, change):
    entry = await live_chat(plug)
    loop = asyncio.get_running_loop()
    llm = FakeLlm(echo)

    def during_the_call(_payload: dict[str, Any]) -> None:  # runs in the build's worker thread
        asyncio.run_coroutine_threadsafe(_change(plug, ds, entry, change), loop).result(5)

    llm.on_call = during_the_call
    ds.bind_llm(llm)
    bot = tdp.PanelBot()

    assert await tdp.run_command(panel, tdp.command_update(), bot)

    assert len(llm.calls) == 1 and tdp.posts(bot) == []
    assert [sent["text"] for sent in bot.sent] == [f"{tdp.HEAD}\n{CHANGED}"]


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logs_carry_codes_and_counts_only(plug, ds, panel, post, caplog):
    with caplog.at_level(logging.DEBUG):
        entry = await scene(plug, ds)
        ds.bind_llm(FakeLlm(echo, RuntimeError(f"provider echoed {CANARY_NOTE} and {ASK}"), "no end line"))
        for message_id in (500, 501, 502):  # posted, failed with an exception, failed validation
            await tdp.run_command(panel, tdp.command_update(message_id=message_id), tdp.PanelBot())
        post.build_post(entry, llm=FakeLlm(echo), now=NOW)

    assert "dossier call purpose=telesec_post chat_ref=" in caplog.text
    assert "dossier posted chat_ref=" in caplog.text
    assert "reason=failed error=RuntimeError" in caplog.text and "reason=failed error=no_end_marker" in caplog.text
    assert not [r for r in caplog.records if r.exc_info]
    assert_clean(caplog.text)
    for text in (ASK, AGREE, CORRECTED, BUDGET, *VOICE, "anna_k", "Anna", f"chat_id={CHAT}", str(CONTACT_ID)):
        assert text not in caplog.text
