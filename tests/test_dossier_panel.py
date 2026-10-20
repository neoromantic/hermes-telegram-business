"""Dossiers in Telegram: snapshot loading, the owner panel section and the owner's ``/dossier``.

Documents are built on disk by the real capture path and cycle with a scripted model; the Bot API is a
recording fake, plus one run through real PTB 22.8 dispatch and request serialization. What a
``/dossier`` summary may read is tested in ``test_dossier_post.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import shlex
import shutil
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from types import MappingProxyType, ModuleType, SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import test_history
from dossier_helpers import (
    BOT_ID,
    BUSINESS_ID,
    NOW,
    OWNER_ID,
    FakeCtx,
    FakeLlm,
    catalog_entry,
    cli,
    cycle,
    doc,
    dossier_dir,
    maintain,
    make_deleted_update,
    persona_dir,
    raw,
    read_dossier,
    read_persona,
    read_settings,
    seed,
    snapshot_tree,
    spy_worker_cycles,
    wait_until,
)
from telegram import Update
from telegram.ext import Application, ApplicationHandlerStop, ExtBot, TypeHandler
from test_dossier_migrate import fact, put, v1_contact
from test_history import load_records
from test_telegram_integration import TelegramRequest

CHAT = 991
CONTACT_ID = 2000
ROWS = [(10, "in", "I prefer calls after 6pm on weekdays.", 60),
        (11, "out", "Sure, I will send the contract by Friday.", 59)]
COMMITMENT = "The owner will send the contract by Friday."
PREFERENCE = "The contact prefers calls after 6pm."
DOCUMENT = f"**Anna, a client.**\n\n## Commitments\n- {COMMITMENT} [m11]\n\n## Preferences\n- {PREFERENCE} [m10]"
PERSONA_TEXT = "The owner writes short friendly replies."
PERSONA = f"## Style\n- {PERSONA_TEXT}"
NOTE = "Owner-only note: never mention the discount."
SUMMARY = "## Agreements\n- The owner sends the contract by **Friday**.\n- Calls work best after 6pm."
SUMMARY_LINE = "The owner sends the contract by <b>Friday</b>."
LONG_SUMMARY = doc("## Notes\n" + "- a long point\n" * 400)  # too long: the writer would ask to shorten it
SECRETS = (COMMITMENT, PREFERENCE, PERSONA_TEXT, NOTE, "I prefer calls", "send the contract", "Anna", "anna_k",
           "Calls work best")
UNAVAILABLE = "Refresh is unavailable: dossiers or history are off, or the model is not connected."
STARTED = "Refresh started. Open the dossier again in a minute."
OPERATOR_ONLY = ("Only the owner of every Business connection the bot knows about can open the persona or turn "
                 "dossiers on and off.")
HEAD = "Nothing was posted to the chat with @anna_k."


@pytest.fixture(autouse=True)
def _isolated_env(dossier_env: Callable[..., None]) -> Callable[..., None]:
    return dossier_env


def _panel_module(plugin: ModuleType) -> ModuleType:
    return importlib.import_module(f"{plugin.__name__}.dossier_panel")


@pytest.fixture
def plug(dossier_plugin: ModuleType, dossiers: ModuleType) -> ModuleType:
    """History and dossiers on (no LLM bound)."""
    return dossier_plugin


@pytest.fixture
def ds(dossiers: ModuleType) -> ModuleType:
    return dossiers


@pytest.fixture
def panel(dossier_plugin: ModuleType) -> Any:
    module = _panel_module(dossier_plugin)
    yield module
    module.join_refreshes(timeout=5)


class PanelBot:
    """Records Bot API calls; ``owner_id`` owns every Business connection."""

    def __init__(self, owner_id: int = OWNER_ID) -> None:
        self.owner_id = owner_id
        self.lookups: list[str] = []
        self.api: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.api_error: Exception | None = None
        self.send_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def get_business_connection(self, business_connection_id: str) -> Any:
        self.lookups.append(business_connection_id)
        return SimpleNamespace(user=SimpleNamespace(id=self.owner_id))

    async def delete_business_messages(self, **kwargs: Any) -> bool:
        self.deletes.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return True

    async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any] | None = None, **_: Any) -> Any:
        self.api.append((endpoint, dict(api_kwargs or {})))
        if self.api_error is not None:
            raise self.api_error
        return {"message_id": 900}

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        return SimpleNamespace(message_id=901)


class BadRequest(Exception):
    pass


async def build(plugin: ModuleType, ds: ModuleType, *, note: bool = True) -> dict[str, Any]:
    """A contact document (with COMMITMENT and PREFERENCE, plus the owner NOTE) and a persona on disk."""
    await seed(plugin, CHAT, "anna_k", ROWS, first_name="Anna")
    assert cycle(ds, FakeLlm(doc(DOCUMENT))).refreshed == 1
    assert cycle(ds, FakeLlm(doc(PERSONA)), target=("persona", None)).persona == "refreshed"
    if note:
        ds.set_note(("contact", catalog_entry(plugin, CHAT)), NOTE)
    return read_dossier(ds, CHAT)


async def act(panel: Any, args: str | None = None, *, lang: str | None = "en", chat: int = CHAT) -> Any:
    # The panel verified the owner of this chat's connection, so its live lookup is cached by then.
    panel._history._OWNER_CACHE.setdefault(BUSINESS_ID, str(OWNER_ID))
    return await panel.handle_panel_action(owner_user_id=OWNER_ID, business_connection_id=BUSINESS_ID,
                                           chat_id=chat, language_code=lang, args=args)


def args_of(view: Any) -> list[str]:
    return [args for row in view.buttons for _label, args in row]


def command_update(text: str = "/dossier", *, sender: int | None = OWNER_ID, edited: bool = False,
                   message_id: int = 500, lang: str = "en", bot_sender: int | None = None) -> Any:
    update = test_history.make_business_text_update(
        text=text, business_id=BUSINESS_ID, chat_id=CHAT, message_id=message_id, from_user_id=sender,
        from_user_language_code=lang, edited=edited, sender_business_bot_id=bot_sender, update_id=message_id)
    update.effective_message = update.edited_business_message if edited else update.business_message
    return update


def echo_update(message_id: int = 900) -> Any:
    """The bot-posted summary as Telegram echoes it: no text, ``rich_message`` in PTB's read-only api_kwargs."""
    update = command_update(None, message_id=message_id, bot_sender=BOT_ID)  # type: ignore[arg-type]
    update.business_message.api_kwargs = MappingProxyType({"rich_message": {"blocks": [{"type": "paragraph"}]}})
    return update


async def run_command(panel: Any, update: Any, bot: PanelBot, seen: set[Any] | None = None) -> bool:
    """The handler, then every post it started (they run in the background)."""
    seen = set() if seen is None else seen

    def mark_seen(key: Any) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    claimed = await panel.handle_dossier_command_update(update, bot=bot, adapter=None, mark_seen=mark_seen)
    await panel.join_posts()
    return claimed


def posts(bot: PanelBot) -> list[dict[str, Any]]:
    return [kwargs for endpoint, kwargs in bot.api if endpoint == "sendRichMessage"]


def holding_llm(*responses: Any) -> tuple[FakeLlm, threading.Event, threading.Event]:
    """A model that blocks inside its call until ``release`` is set; ``entered`` tells the call began."""
    llm, release, entered = FakeLlm(*responses), threading.Event(), threading.Event()

    def hold(_payload: dict[str, Any]) -> None:
        entered.set()
        release.wait(5)

    llm.on_call = hold
    return llm, release, entered


def watch_build(panel: Any, monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set once the ``/dossier`` build thread ends: it runs on after its post is cancelled."""
    done, real = threading.Event(), panel._post.build_post

    def build_post(*args: Any, **kwargs: Any) -> Any:
        try:
            return real(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(panel._post, "build_post", build_post)
    return done


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_snapshot_reads_the_document(plug, ds, panel):
    stored = await build(plug, ds)

    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)

    entry = catalog_entry(plug, CHAT)
    assert (snap.target, snap.doc, snap.corrupt) == ("contact", stored, False)
    assert snap.name == plug._history_support._catalog_entry_display_name(entry) == "@anna_k"
    assert (snap.enabled, snap.history_enabled, snap.chat_off, snap.llm_ready) == (True, True, False, False)
    assert (snap.refreshing, snap.failure, snap.unqualified) == (False, None, False)
    assert (snap.chat_id, snap.in_history, snap.manage_global) == (CHAT, True, False)
    assert COMMITMENT in snap.doc["document"] and snap.doc["note"] == NOTE


@pytest.mark.asyncio
async def test_contact_snapshot_without_catalog_entry_uses_a_stub(plug, ds, panel):
    snap = panel.load_contact_snapshot(BUSINESS_ID, 4242, now=NOW)

    assert (snap.name, snap.doc, snap.in_history, snap.unqualified) == ("chat:4242", None, False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(("rows", "unqualified"), [
    ([(10, "in", "Hello there, how are you?", 60)], True),
    (ROWS, False),
])
async def test_contact_without_a_document_says_why(plug, ds, panel, rows, unqualified):
    await seed(plug, CHAT, "anna_k", rows, first_name="Anna")
    ds.bind_llm(FakeLlm())

    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)
    view = await act(panel)

    assert (snap.doc, snap.unqualified) == (None, unqualified)
    if unqualified:
        assert "No dossier yet: it needs messages from both you and the contact." in view.text
        assert args_of(view) == ["o.p"]  # a refresh could not write anything yet
    else:
        assert "No dossier yet. It is written a few minutes after the chat goes quiet" in view.text
        assert args_of(view) == ["rf.c", "o.p"]
    assert ds.load_state()["failures"] == {}


V1_FACT = "Anna is a client at Northwind Demo."
CONVERTED = f"## Relationship\n- {V1_FACT}"
CONVERTED_STATUS = {
    "paused": "Off for this chat.",
    "inactive": ("Converted from the old dossier. It is rewritten once the chat has saved messages from both you "
                 "and the contact."),
    "pruned": ("Converted from the old dossier. It is rewritten once the chat has saved messages from both you "
               "and the contact."),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(CONVERTED_STATUS))
async def test_a_migrated_dossier_is_readable_where_no_refresh_can_run(plug, ds, panel, case):
    """Paused, inactive (nothing in the backfill window) and pruned chats show the converted v1 document.

    The first refresh once the chat qualifies still runs as a first pass and sends it as the current dossier.
    """
    if case == "paused":
        await seed(plug, CHAT, "anna_k", ROWS, first_name="Anna")
        plug._chat_settings.default_store().set_flag(OWNER_ID, CHAT, "dossier", False, via="cli")
    else:
        old = timedelta(days=200 if case == "inactive" else 70)  # older than the 90-day backfill window
        raw(plug, CHAT, [(1, "in", "Old order: two crates", old), (2, "out", "Old reply", old - timedelta(minutes=1))],
            username="anna_k", first_name="Anna")
    if case == "pruned":
        test_history.set_retention(plug, 30)
        assert maintain(plug, 0).pruned_files >= 1
    put(dossier_dir(ds, CHAT), v1_contact(fact("f1", V1_FACT), fact("f2", "Never call on Sundays.", kind="owner"),
                                          conn=BUSINESS_ID, chat=CHAT))
    fake = FakeLlm(doc(DOCUMENT))
    cycle(ds, fake)  # migrates, then writes nothing: no refresh can run here
    assert fake.calls == []
    ds.bind_llm(FakeLlm())

    view = await act(panel)

    assert V1_FACT in view.text and "Never call on Sundays." in view.text
    assert view.text.endswith(CONVERTED_STATUS[case])
    assert "No dossier yet" not in view.text and "Waiting for the first build" not in view.text
    assert args_of(view) == ["o.p"]  # no Refresh: it could not write anything here
    for command, *ids in (("show", "--chat", str(CHAT)), ("export", "--connection", BUSINESS_ID, "--chat", str(CHAT))):
        code, out, _err = cli(plug, "dossier", command, *ids)  # 'show --chat' is the line a clipped screen names
        assert code == 0 and f"- {V1_FACT}" in out.splitlines(), (command, out)
        assert "Waiting for the first build" not in out
        converted = "Converted from the old dossier; the first refresh rewrites it." in out.splitlines()
        assert converted is (command == "show"), (command, out)  # export prints the document only

    if case == "paused":
        plug._chat_settings.default_store().set_flag(OWNER_ID, CHAT, "dossier", True, via="cli")
    else:
        await seed(plug, CHAT, "anna_k", ROWS, first_name="Anna")
    report = cycle(ds, fake, target=("contact", catalog_entry(plug, CHAT)))

    assert report.refreshed == 1
    (call,) = fake.by_purpose("telesec_dossier")
    assert (call["user"]["mode"], call["user"]["current_dossier"]) == ("first", CONVERTED)
    stored = read_dossier(ds, CHAT)
    assert (stored["document"], stored["revision"], stored["previous"]) == (DOCUMENT, 1, CONVERTED)


@pytest.mark.asyncio
async def test_a_clipped_screen_names_show_for_this_connection(plug, ds, panel):
    """The contact also writes to another owner, under the same chat id: the named 'show' prints this chat's text."""
    other = "bc-other-demo"
    old = timedelta(days=70)
    raw(plug, CHAT, [(1, "in", "Old order: two crates", old), (2, "out", "Old reply", old - timedelta(minutes=1))],
        username="anna_k", first_name="Anna")
    test_history.set_retention(plug, 30)
    assert maintain(plug, 0).pruned_files >= 1  # this owner's chat is gone from the catalog
    await seed(plug, CHAT, "anna_k", ROWS, first_name="Anna", business_id=other)
    facts = [fact(f"f{i}", f"Anna asked about crate size {i} for the Northwind Demo order.") for i in range(80)]
    put(dossier_dir(ds, CHAT), v1_contact(*facts, conn=BUSINESS_ID, chat=CHAT))
    put(dossier_dir(ds, CHAT, business_id=other), v1_contact(fact("b1", "Other owner's fact."), conn=other, chat=CHAT))
    cycle(ds, FakeLlm(doc(DOCUMENT)))
    ds.bind_llm(FakeLlm())

    view = await act(panel)

    (line,) = [line for line in view.text.splitlines() if "hermes telesec dossier show" in line]
    args = shlex.split(line.partition("hermes telesec dossier ")[2])
    assert args == ["show", "--connection", BUSINESS_ID, "--chat", str(CHAT)]
    code, out, _err = cli(plug, "dossier", *args)
    assert code == 0 and "- Anna asked about crate size 79 for the Northwind Demo order." in out.splitlines()
    assert "Other owner" not in out and COMMITMENT not in out
    _code, out, _err = cli(plug, "dossier", "show", "--chat", str(CHAT))  # the other owner's chat
    assert "crate size" not in out


@pytest.mark.asyncio
async def test_contact_snapshot_failed_attempt(plug, ds, panel):
    await seed(plug, CHAT, "anna_k", ROWS, first_name="Anna")
    assert cycle(ds, FakeLlm(RuntimeError("model down with secret text"))).failed == 1
    ds.bind_llm(FakeLlm())

    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)

    assert (snap.doc, snap.failure) == (None, "RuntimeError")
    text = panel._view.render_overview(snap, "en").text
    assert "The dossier could not be written yet (RuntimeError)." in text and "secret" not in text


@pytest.mark.asyncio
async def test_contact_snapshot_corrupt(plug, ds, panel):
    await build(plug, ds)
    ds.bind_llm(FakeLlm())
    (dossier_dir(ds, CHAT) / "document.json").write_text("{not json", encoding="utf-8")

    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)
    view = await act(panel)

    assert (snap.corrupt, snap.doc) == (True, None)
    assert "can't be read" in view.text and f"hermes telesec dossier forget --connection {BUSINESS_ID} --chat {CHAT}" in view.text
    assert args_of(view) == ["o.p"]


@pytest.mark.asyncio
async def test_disabled_without_dossier_explains_and_points_to_settings(dossier_history, panel):
    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)
    view = await act(panel)

    assert (snap.enabled, snap.doc) == (False, None)
    assert "Dossiers are off. Turn them on in Settings." in view.text
    assert args_of(view) == ["o.p"]  # the switch is in Settings


@pytest.mark.asyncio
async def test_persona_snapshot_reads_the_persona_document(plug, ds, panel):
    await build(plug, ds)

    snap = panel.load_persona_snapshot()

    assert (snap.target, snap.doc, snap.corrupt, snap.manage_global) == ("persona", read_persona(ds), False, True)
    assert PERSONA_TEXT in snap.doc["document"]


@pytest.mark.asyncio
async def test_persona_snapshot_missing_and_corrupt(plug, ds, panel):
    ds.bind_llm(FakeLlm())
    missing = panel.load_persona_snapshot()
    assert (missing.doc, missing.corrupt) == (None, False)
    assert "No persona yet." in panel._view.render_overview(missing, "en").text

    persona_dir(ds).mkdir(parents=True)
    (persona_dir(ds) / "document.json").write_text("[]", encoding="utf-8")
    corrupt = panel.load_persona_snapshot()
    assert (corrupt.doc, corrupt.corrupt) == (None, True)
    assert "The persona file can't be read." in panel._view.render_overview(corrupt, "en").text


@pytest.mark.asyncio
async def test_llm_ready_checks_complete(plug, ds, panel):
    assert panel.llm_ready() is False
    assert panel.llm_ready(SimpleNamespace(complete_structured=lambda **_: None)) is False
    assert panel.llm_ready(FakeLlm()) is True
    ds.bind_llm(FakeLlm())
    assert panel.llm_ready() is True


# ---------------------------------------------------------------------------
# Owner panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_persona_screen_and_status_say_when_the_first_persona_is_due(plug, ds, panel, monkeypatch):
    ds.bind_llm(FakeLlm())
    await seed(plug, CHAT, "anna_k", [(i, "out", f"owner line {i}", 60 - i) for i in range(1, 10)],
               first_name="Anna")
    nine = panel.load_persona_snapshot()
    assert nine.persona_due is False  # nine sent messages: the 10-message text is still true
    assert "at least 10 messages" in panel._view.render_overview(nine, "en").text
    assert "owner persona no," in cli(plug, "dossier", "status")[1]

    await seed(plug, CHAT, "anna_k", [(10, "out", "owner line 10", 30)], first_name="Anna")
    ten = panel.load_persona_snapshot()
    assert ten.persona_due is True
    assert panel._view.render_overview(ten, "en").text.startswith(
        "My persona\nNo persona yet. It is written at the next dossier cycle.")
    assert "Она появится после следующего цикла обработки." in panel._view.render_overview(ten, "ru").text
    assert "owner persona due at the next cycle," in cli(plug, "dossier", "status")[1]

    assert cycle(ds, FakeLlm(doc(PERSONA)), target=("persona", None)).persona == "refreshed"
    reads: list[str] = []
    monkeypatch.setattr(ds, "read_chat_view", lambda *_a, **_k: reads.append("read"))
    built = panel.load_persona_snapshot()
    assert built.persona_due is False and reads == []  # a written persona reads no history for the screen
    assert "owner persona yes," in cli(plug, "dossier", "status")[1] and reads == []


@pytest.mark.asyncio
async def test_overview_in_english_and_russian(plug, ds, panel):
    await build(plug, ds)

    en = await act(panel)
    ru = await act(panel, "o.c", lang="ru-RU")

    assert en.html.startswith("<h3>@anna_k</h3>")
    assert COMMITMENT in en.html and PREFERENCE in en.html and "<h4>Commitments</h4>" in en.html
    assert "[m11]" not in en.html and "[m10]" not in en.html  # refs stay on disk
    assert "Your note (not posted)" in en.text and NOTE in en.text
    assert "Ваша заметка (не публикуется)" in ru.html and COMMITMENT in ru.html
    assert args_of(en) == ["o.p"]  # no Refresh: the model is not bound here
    assert all(len(args) <= 30 for args in args_of(en))


@pytest.mark.asyncio
async def test_old_and_malformed_args_show_the_dossier_and_write_nothing(plug, ds, panel):
    await build(plug, ds)
    before = snapshot_tree(ds.dossier_root())

    for args in ("zz", "m.c.0", "f.c.abcdef.1", "pn.c.abcdef.1", "up.c.abcdef.1", "ra.c.abcdef.2", "rd.c.abcdef.2",
                 "ea", "ed", "ds", "rf.p", "x" * 80, "o.c\n"):
        view = await act(panel, args)
        assert COMMITMENT in view.html and view.notice is None, args

    assert snapshot_tree(ds.dossier_root()) == before


@pytest.mark.asyncio
async def test_persona_screen_has_only_the_way_back(plug, ds, panel):
    await build(plug, ds)

    view = await act(panel, "o.p")

    assert view.html.startswith("<h3>My persona</h3>") and PERSONA_TEXT in view.html
    assert COMMITMENT not in view.html and NOTE not in view.html
    assert args_of(view) == ["o.c"]
    assert "Моя персона" in (await act(panel, "o.p", lang="ru")).html


@pytest.mark.asyncio
async def test_switch_wakes_an_idle_worker_and_old_switch_buttons_write_nothing(dossier_history, panel, monkeypatch):
    ds = dossier_history._dossier_support
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    outcomes = spy_worker_cycles(monkeypatch, ds)
    ds.bind_llm(FakeLlm())
    try:
        assert ds.ensure_worker_started()
        assert not wait_until(lambda: outcomes, timeout=0.3)  # off: the worker idles

        for stale in ("ea", "ed", "ds"):  # Enable/Disable on a panel sent before the switch moved to Settings
            view = await act(panel, stale)
            assert view.notice is None and "Turn them on in Settings." in view.text
        assert read_settings(ds) is None and not outcomes

        panel.switch(True)  # what Settings calls after its confirm
        settings = read_settings(ds)
        assert (settings["enabled"], settings["updated_via"]) == (True, "telegram")
        assert wait_until(lambda: outcomes, timeout=5)  # well before the 60 s poll
    finally:
        ds.stop_workers(timeout=2)
        ds.bind_llm(None)


@pytest.mark.asyncio
async def test_disable_keeps_stored_documents(plug, ds, panel):
    await build(plug, ds)

    panel.switch(False)
    off = await act(panel, lang="ru")

    settings = read_settings(ds)
    assert (settings["enabled"], settings["updated_via"]) == (False, "telegram")
    assert read_dossier(ds, CHAT) is not None
    assert COMMITMENT in off.html and "Досье выключены. Включите их в Настройках." in off.text
    assert args_of(off) == ["o.p"]


@pytest.mark.asyncio
async def test_refresh_refused_when_disabled_or_without_a_model(plug, ds, panel):
    await build(plug, ds)

    no_model = await act(panel, "rf.c")
    ds.bind_llm(FakeLlm())
    ds.set_enabled(False, via="cli")
    disabled = await act(panel, "rf.c")

    for view in (no_model, disabled):
        assert view.notice == UNAVAILABLE and args_of(view) == ["o.c"]
    assert panel._REFRESHES == {}


@pytest.mark.asyncio
async def test_refresh_reports_a_busy_cycle(plug, ds, panel):
    await build(plug, ds)
    ds.bind_llm(FakeLlm())

    with ds._cycle_lock() as acquired:
        assert acquired
        view = await act(panel, "rf.c")

    assert view.notice == "Another dossier cycle is running. Try again in a minute."
    assert panel._REFRESHES == {}


@pytest.mark.asyncio
async def test_refresh_with_nothing_new_is_up_to_date_without_a_thread_or_a_call(plug, ds, panel):
    await build(plug, ds, note=False)
    llm = FakeLlm()
    ds.bind_llm(llm)
    before = snapshot_tree(dossier_dir(ds, CHAT))

    view = await act(panel, "rf.c")

    assert view.notice == "Already up to date" and COMMITMENT in view.html and "rf.c" in args_of(view)
    assert panel.start_refresh(BUSINESS_ID, CHAT, OWNER_ID) == "up_to_date"
    assert panel._REFRESHES == {} and llm.calls == []
    assert snapshot_tree(dossier_dir(ds, CHAT)) == before

    ds.set_note(("contact", catalog_entry(plug, CHAT)), NOTE)  # a changed note is something to do
    assert (await act(panel, "rf.c")).notice == STARTED
    panel.join_refreshes(timeout=5)
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_refresh_after_a_failure_is_not_up_to_date(plug, ds, panel):
    await build(plug, ds, note=False)
    state = ds.load_state()
    state["failures"][ds.chat_key(BUSINESS_ID, CHAT)] = {"at": NOW.isoformat(), "error": "RuntimeError", "count": 1}
    ds.save_state(state)
    ds.bind_llm(FakeLlm())

    assert panel.start_refresh(BUSINESS_ID, CHAT, OWNER_ID) == "started"


@pytest.mark.asyncio
async def test_refresh_runs_one_real_cycle_per_chat_in_the_background(plug, ds, panel):
    await build(plug, ds)
    await seed(plug, CHAT, "anna_k", [(12, "in", "My new phone number is +1 555 555-0100.", 5)], first_name="Anna")
    llm, release, entered = holding_llm(doc(f"{DOCUMENT}\n\n## Contacts\n- The contact has a new phone number. [m12]"))
    ds.bind_llm(llm)
    try:
        started = await act(panel, "rf.c")
        assert started.notice == STARTED and args_of(started) == ["o.c"]
        assert await asyncio.to_thread(entered.wait, 5)
        again = await act(panel, "rf.c")
        assert again.notice == STARTED
        assert "Refreshing now" in again.text and "rf.c" not in args_of(again)
        assert panel.load_contact_snapshot(BUSINESS_ID, CHAT).refreshing is True
    finally:
        release.set()
        panel.join_refreshes(timeout=5)

    assert len(llm.calls) == 1 and llm.calls[0]["purpose"] == "telesec_dossier"
    assert "The contact has a new phone number." in read_dossier(ds, CHAT)["document"]
    assert panel.load_contact_snapshot(BUSINESS_ID, CHAT).refreshing is False
    assert "rf.c" in args_of(await act(panel))


@pytest.mark.asyncio
async def test_unload_stops_a_running_refresh_without_writing(plug, ds, panel, monkeypatch):
    await build(plug, ds)
    await seed(plug, CHAT, "anna_k", [(12, "in", "My new phone number is +1 555 555-0100.", 5),
                                      (13, "in", "And a new mail too: anna@example.com.", 4)], first_name="Anna")
    monkeypatch.setattr(ds, "BATCH_MAX_MESSAGES", 1)  # two batches: a second model call is possible
    before = read_dossier(ds, CHAT)
    llm, release, entered = holding_llm(doc(f"{DOCUMENT}\n\n## Contacts\n- A new phone number. [m12]"))
    ctx = FakeCtx(llm, with_unload=True)
    plug.register(ctx)
    ds.bind_llm(llm)
    try:
        assert (await act(panel, "rf.c")).notice == STARTED
        assert await asyncio.to_thread(entered.wait, 5)
        (unload,) = ctx.unload_callbacks
        unload()  # exactly what the host calls on unload: returns at once
    finally:
        release.set()
        panel.join_refreshes(timeout=5)

    assert len(llm.calls) == 1  # no model call after the stop
    assert read_dossier(ds, CHAT) == before  # the answer that came back after the stop was dropped
    assert panel.load_contact_snapshot(BUSINESS_ID, CHAT).refreshing is False


@pytest.mark.asyncio
async def test_persona_and_switch_need_every_connection_owned(plug, ds, panel):
    await build(plug, ds)
    other = "business-other"
    await seed(plug, 992, "boris_v", ROWS, business_id=other, first_name="Boris")
    cache = plug._history_support._OWNER_CACHE
    assert cache[other] == str(OWNER_ID)  # the operator owns both connections
    assert args_of(await act(panel)) == ["o.p"]
    assert panel.manages_global(OWNER_ID, BUSINESS_ID) is True

    for owner in ("4242", None):  # another account connected the bot, or its owner is not known yet
        if owner is None:
            del cache[other]
        else:
            cache[other] = owner
        overview = await act(panel)
        assert args_of(overview) == [] and PERSONA_TEXT not in overview.html
        refused = await act(panel, "o.p")
        assert refused.notice == OPERATOR_ONLY and PERSONA_TEXT not in refused.html and args_of(refused) == ["o.c"]
        for args in ("ea", "ed", "ds"):  # old switch buttons only show the dossier
            assert args_of(await act(panel, args)) == []
        assert ds.load_config().enabled is True and panel.manages_global(OWNER_ID, BUSINESS_ID) is False
    assert panel.manages_global(None, BUSINESS_ID) is False and panel.manages_global(OWNER_ID, None) is False


@pytest.mark.asyncio
async def test_persona_and_switch_stay_closed_when_history_is_gone(plug, ds, panel, tmp_path):
    await build(plug, ds)
    assert read_persona(ds)["chats"]  # the persona names the chats it read
    history, cache = plug._history_support, plug._history_support._OWNER_CACHE
    other = "business-B"
    shutil.rmtree(history.history_root())  # the archive is removed, documents are kept
    assert ds.private_entries() == []
    cache.clear()
    cache[BUSINESS_ID] = str(OWNER_ID)
    assert panel.manages_global(OWNER_ID, BUSINESS_ID) is True  # the operator keeps access

    cache.clear()  # a gateway restart
    panel._modes.default_store().bind_connection(other, "4242")  # another account sent /transcribe once
    cache[other] = "4242"

    async def as_other(args: str | None) -> Any:
        return await panel.handle_panel_action(owner_user_id="4242", business_connection_id=other, chat_id=992,
                                               language_code="en", args=args)

    assert panel.manages_global("4242", other) is False  # the persona and a contact document cite the operator
    assert args_of(await as_other(None)) == []
    refused = await as_other("o.p")
    assert refused.notice == OPERATOR_ONLY and PERSONA_TEXT not in refused.html
    for args in ("ed", "ds"):  # old switch buttons write nothing
        assert (await as_other(args)).notice is None
    assert ds.load_config().enabled is True

    chat_dir = ds.dossier_root() / "chats" / history._path_key(BUSINESS_ID)
    shutil.move(chat_dir, tmp_path / "chat")
    assert panel.manages_global("4242", other) is False  # only the persona cites the operator's chat
    shutil.move(tmp_path / "chat", chat_dir)
    shutil.rmtree(persona_dir(ds))
    assert panel.manages_global("4242", other) is False  # a contact document of a connection nobody vouched for
    shutil.rmtree(chat_dir)
    cache[BUSINESS_ID] = str(OWNER_ID)
    assert panel.manages_global("4242", other) is False  # this process looked up the operator's connection
    del cache[BUSINESS_ID]
    assert panel.manages_global("4242", other) is True  # nothing else is known: B owns all of it
    plug.register(FakeCtx(None))  # wires the gateway's in-memory Business connections
    plug._note_business_chat(BUSINESS_ID, CHAT)  # a message on the operator's connection since the start
    assert panel.manages_global("4242", other) is False
    plug._PANEL_SEEN_CHATS.clear()
    plug._PANEL_KNOWN_CONNECTIONS[BUSINESS_ID] = str(OWNER_ID)  # Telegram reported it, then it was disabled
    assert panel.manages_global("4242", other) is False
    plug._PANEL_KNOWN_CONNECTIONS.clear()
    persona_dir(ds).mkdir(parents=True)
    (persona_dir(ds) / "document.json").write_text("{", encoding="utf-8")
    assert panel.manages_global("4242", other) is True  # an unreadable persona adds no connections
    shutil.rmtree(persona_dir(ds))
    panel._modes.default_store().bind_connection(BUSINESS_ID, OWNER_ID)
    assert panel.manages_global("4242", other) is False  # the operator's connection is bound in modes


@pytest.mark.asyncio
async def test_unreadable_persona_keeps_the_operator_in_control(plug, ds, panel):
    await build(plug, ds)
    (persona_dir(ds) / "document.json").write_text("{", encoding="utf-8")

    assert args_of(await act(panel)) == ["o.p"]
    persona = await act(panel, "o.p")
    assert "The persona file can't be read." in persona.text and "dossier forget --persona" in persona.text
    assert panel.manages_global(OWNER_ID, BUSINESS_ID) is True  # so Settings can still turn dossiers off


@pytest.mark.asyncio
async def test_history_capture_off_is_explained(plug, ds, panel):
    await build(plug, ds)
    ds.bind_llm(FakeLlm())
    assert "rf.c" in args_of(await act(panel))
    test_history.set_switch(plug, "history", False)

    view = await act(panel)

    assert "History is off, so dossiers are paused. Turn it on in Settings." in view.text
    assert "rf.c" not in args_of(view)  # start_refresh would refuse it
    assert "История выключена, поэтому досье на паузе" in (await act(panel, lang="ru")).text
    assert (await act(panel, "rf.c")).notice == UNAVAILABLE


@pytest.mark.asyncio
async def test_unexpected_failure_gives_a_generic_view_and_logs_the_type_only(plug, ds, panel, monkeypatch, caplog):
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"secret {COMMITMENT}")

    monkeypatch.setattr(panel, "load_contact_snapshot", boom)
    with caplog.at_level(logging.DEBUG):
        view = await act(panel, "o.c")

    assert view.text == "That did not work (RuntimeError)."
    assert "RuntimeError" in caplog.text and COMMITMENT not in caplog.text


@pytest.mark.asyncio
async def test_unknown_business_connection_explains_what_to_do(plug, panel, caplog):
    with caplog.at_level(logging.INFO):
        view = await panel.handle_panel_action(owner_user_id=OWNER_ID, business_connection_id=None, chat_id=CHAT,
                                               language_code="ru", args=None)
    assert view.text == ("Business-подключение этого чата пока неизвестно. Отправьте любое сообщение в том чате и "
                         "снова нажмите кнопку управления ботом.")
    assert view.buttons == ()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class OwnerPanelBot(PanelBot):
    """The owner panel's Bot API calls: answers, rich edits (``do_api_request``) and plain edits."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []

    async def answer_callback_query(self, **kwargs: Any) -> bool:
        self.answers.append(kwargs)
        return True

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.edits.append(kwargs)
        return True


def panel_callback(data: str, *, lang: str | None = "en") -> Any:
    query = SimpleNamespace(id="query-1", data=data, from_user=SimpleNamespace(id=OWNER_ID, language_code=lang),
                            message=SimpleNamespace(chat=SimpleNamespace(id=OWNER_ID, type="private"),
                                                    message_id=700))
    return SimpleNamespace(update_id=3, message=None, callback_query=query, effective_message=query.message)


def keyboard_of(markup: Any) -> list[list[tuple[str, str]]]:
    return [[(button.text, button.callback_data) for button in row] for row in markup.inline_keyboard]


async def open_section(plug: ModuleType, bot: OwnerPanelBot, data: str, *, lang: str | None = "en") -> None:
    plug._history_support._OWNER_CACHE[BUSINESS_ID] = str(OWNER_ID)  # the panel's verified lookup
    plug._note_business_chat(BUSINESS_ID, CHAT)
    assert await plug._handle_owner_panel_update(panel_callback(data, lang=lang), bot=bot) is True


def test_register_adds_the_dossier_section(dossier_plugin):
    owner_panel = dossier_plugin._owner_panel
    assert owner_panel.get_section("dossier") is None
    dossier_plugin.register(FakeCtx(None))
    section = owner_panel.get_section("dossier")
    assert (section.key, section.label) == ("dossier", "Dossier")
    view = owner_panel.render_main(owner_panel.PanelContext("1000", str(CHAT), None, BUSINESS_ID), None,
                                   owner_panel.sections(), True)
    assert ("Dossier", f"tb:s:{CHAT}:dossier") in [button for row in view.buttons for button in row]


@pytest.mark.asyncio
async def test_panel_section_sends_a_rich_localized_dossier_with_panel_callbacks(plug, ds, panel):
    await build(plug, ds)
    ds.bind_llm(FakeLlm())
    panel.register_owner_panel_section(plug._owner_panel)
    bot = OwnerPanelBot()

    await open_section(plug, bot, f"tb:s:{CHAT}:dossier", lang="ru")

    assert bot.answers == [{"callback_query_id": "query-1", "text": "Досье"}]
    assert bot.edits == [] and bot.sent == []
    ((endpoint, kwargs),) = bot.api
    assert endpoint == "editmessagetext"
    assert (kwargs["chat_id"], kwargs["message_id"]) == (OWNER_ID, 700)
    assert set(kwargs["rich_message"]) == {"html", "skip_entity_detection"}
    html = kwargs["rich_message"]["html"]
    assert COMMITMENT in html and NOTE in html and "Ваша заметка (не публикуется)" in html  # the owner sees notes
    rows = keyboard_of(kwargs["reply_markup"])
    assert rows == [[("Обновить", f"tb:s:{CHAT}:dossier/rf.c"), ("Моя персона", f"tb:s:{CHAT}:dossier/o.p")],
                    [("« Назад", f"tb:b:{CHAT}")]]
    for row in rows:
        for _label, data in row:
            assert len(data.encode()) <= 64 and plug._owner_panel.parse_callback(data) is not None
    # PTB JSON-encodes the nested api_kwargs objects as single request fields.
    from telegram.request import RequestData
    from telegram.request._requestparameter import RequestParameter

    fields = RequestData([RequestParameter.from_input(k, v) for k, v in kwargs.items()]).json_parameters
    assert json.loads(fields["rich_message"]) == {"html": html, "skip_entity_detection": True}
    assert json.loads(fields["reply_markup"])["inline_keyboard"][-1][0]["callback_data"] == f"tb:b:{CHAT}"


@pytest.mark.asyncio
async def test_panel_sub_args_reach_the_section_and_notices_head_the_text(plug, ds, panel):
    await build(plug, ds, note=False)
    ds.bind_llm(FakeLlm())
    panel.register_owner_panel_section(plug._owner_panel)
    bot = OwnerPanelBot()

    await open_section(plug, bot, f"tb:s:{CHAT}:dossier/rf.c")

    html = bot.api[-1][1]["rich_message"]["html"]
    assert html.startswith("<p><b>Already up to date</b></p><h3>@anna_k</h3>") and COMMITMENT in html


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "ru"])
async def test_section_back_buttons_differ_from_the_panel_back(plug, ds, panel, lang):
    await build(plug, ds)
    panel.register_owner_panel_section(plug._owner_panel)
    bot = OwnerPanelBot()
    for args in ("o.p", "o.c"):
        await open_section(plug, bot, f"tb:s:{CHAT}:dossier/{args}", lang=lang)
    assert len(bot.api) == 2
    for _endpoint, kwargs in bot.api:
        labels = [label for row in keyboard_of(kwargs["reply_markup"]) for label, _data in row]
        panel_back = labels[-1]
        assert panel_back in ("« Back", "« Назад")
        assert all(label.strip("«‹ ") != panel_back.strip("«‹ ") for label in labels[:-1]), labels


@pytest.mark.asyncio
async def test_panel_falls_back_to_plain_text_when_the_rich_edit_fails(plug, ds, panel, caplog):
    await build(plug, ds)
    panel.register_owner_panel_section(plug._owner_panel)
    bot = OwnerPanelBot()
    bot.api_error = BadRequest(f"Bad Request: can't parse {COMMITMENT}")

    with caplog.at_level(logging.DEBUG):
        await open_section(plug, bot, f"tb:s:{CHAT}:dossier", lang=None)

    (edit,) = bot.edits
    assert edit["text"].startswith("@anna_k\n") and COMMITMENT in edit["text"] and "Commitments" in edit["text"]
    assert len(edit["text"].encode("utf-16-le")) // 2 <= 4096
    assert keyboard_of(edit["reply_markup"])[-1] == [("« Back", f"tb:b:{CHAT}")]
    assert "BadRequest" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_panel_keeps_an_unchanged_rich_view(plug, ds, panel, caplog):
    await build(plug, ds)
    panel.register_owner_panel_section(plug._owner_panel)
    bot = OwnerPanelBot()
    bot.api_error = BadRequest("Bad Request: message is not modified: specified new message content and reply "
                               "markup are exactly the same")

    with caplog.at_level(logging.DEBUG):
        await open_section(plug, bot, f"tb:s:{CHAT}:dossier")  # a double tap

    assert [endpoint for endpoint, _ in bot.api] == ["editmessagetext"] and bot.edits == []
    assert "edit failed" not in caplog.text


@pytest.mark.asyncio
async def test_panel_back_returns_to_a_plain_main_view(plug, ds, panel):
    await build(plug, ds)
    panel.register_owner_panel_section(plug._owner_panel)
    bot = OwnerPanelBot()

    await open_section(plug, bot, f"tb:b:{CHAT}")

    assert bot.api == []
    (edit,) = bot.edits
    assert edit["text"].startswith("Owner panel:")
    assert ("Dossier", f"tb:s:{CHAT}:dossier") in [button for row in keyboard_of(edit["reply_markup"]) for button in row]


# ---------------------------------------------------------------------------
# /dossier in the Business chat
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "command"), [
    ("/dossier", True), ("/DOSSIER", True), ("/dossier@my_bot", True), ("/dossier \n", True),
    ("/dossier please", False), ("/dossier\nnow", False), ("/dossiers", False), ("dossier", False), (" /dossier", False),
    ("hello /dossier", False), (None, False),
])
def test_dossier_command_predicate(dossier_plugin, text, command):
    assert dossier_plugin._history_support.is_dossier_command_text(text) is command


def test_dossier_control_message_predicate(dossier_plugin):
    history = dossier_plugin._history_support
    owner = command_update().business_message
    assert history.is_dossier_control_message(owner, "/dossier")
    for name in ("forward_origin", "via_bot"):
        assert not history.is_dossier_control_message(SimpleNamespace(**{**vars(owner), name: object()}), "/dossier")
    assert not history.is_dossier_control_message(command_update(bot_sender=BOT_ID).business_message, "/dossier")
    echo = echo_update().business_message
    assert history.is_dossier_post(echo) and history.is_dossier_control_message(echo, None)
    assert history.is_dossier_post({"sender_business_bot": {"id": 1}, "api_kwargs": {"rich_message": {}}})
    assert not history.is_dossier_post({"api_kwargs": {"rich_message": {}}})  # not bot-sent
    assert not history.is_dossier_post(command_update(None, bot_sender=BOT_ID).business_message)


@pytest.mark.asyncio
async def test_owner_command_deletes_itself_and_posts_one_rich_summary(plug, ds, panel):
    await build(plug, ds)
    llm = FakeLlm(doc(SUMMARY))
    ds.bind_llm(llm)
    bot = PanelBot()

    assert await run_command(panel, command_update(), bot) is True

    assert bot.deletes == [{"business_connection_id": BUSINESS_ID, "message_ids": [500]}]
    assert bot.sent == []
    (post,) = posts(bot)
    assert [endpoint for endpoint, _ in bot.api] == ["sendRichMessage"]
    assert set(post) == {"business_connection_id", "chat_id", "rich_message"}
    assert (post["business_connection_id"], post["chat_id"]) == (BUSINESS_ID, CHAT)
    assert set(post["rich_message"]) == {"html", "skip_entity_detection"}
    assert post["rich_message"]["skip_entity_detection"] is True
    html = post["rich_message"]["html"]
    assert html.startswith("<h3>Summary of our chat</h3>") and SUMMARY_LINE in html
    assert "Prepared automatically from this chat on" in html
    for owner_only in (NOTE, COMMITMENT, PREFERENCE, PERSONA_TEXT, "Dossier", "dossier", "Refresh", "hermes"):
        assert owner_only not in html
    (call,) = llm.calls
    assert (call["purpose"], call["timeout"], call["system"]) == ("telesec_post", None,
                                                                  panel._post._writer.POST_SYSTEM)
    assert [(m["from"], m["text"]) for m in call["user"]["messages"]] == [("contact", ROWS[0][2]),
                                                                         ("owner", ROWS[1][2])]
    assert all(set(m) <= {"at", "from", "text", "voice"} for m in call["user"]["messages"])
    for private in (NOTE, COMMITMENT, PERSONA_TEXT, "m10", "m11", BUSINESS_ID):
        assert private not in call["raw"]
    history = plug._history_support
    assert history._is_control_message(BUSINESS_ID, CHAT, 500) and history._is_control_message(BUSINESS_ID, CHAT, 900)
    assert panel._POST_TASKS == {}


@pytest.mark.asyncio
async def test_duplicate_delivery_posts_once(plug, ds, panel):
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    bot, seen = PanelBot(), set()

    assert await run_command(panel, command_update(), bot, seen)
    assert await run_command(panel, command_update(), bot, seen)

    assert len(posts(bot)) == 1 and len(bot.deletes) == 1
    assert seen == {("dossier-command", BUSINESS_ID, str(CHAT), "message:500")}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/dossier", "/dossiers", "hello"])
async def test_contact_command_is_ordinary_text(plug, ds, panel, text):
    await build(plug, ds)
    ds.bind_llm(FakeLlm())
    bot = PanelBot()

    assert await run_command(panel, command_update(text, sender=CONTACT_ID), bot) is False

    assert bot.api == [] and bot.deletes == [] and bot.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/dossier off", "/dossier is what the bot calls it", "/dossier\nlonger private note"])
async def test_owner_command_with_more_text_is_ordinary_text(plug, ds, panel, text):
    await build(plug, ds)
    bot = PanelBot()

    assert await run_command(panel, command_update(text), bot) is False

    assert bot.api == [] and bot.deletes == [] and bot.sent == [] and bot.lookups == []


@pytest.mark.asyncio
async def test_unverifiable_owner_is_ordinary_text(plug, ds, panel):
    bot = PanelBot()

    async def down(_conn: str) -> Any:
        raise RuntimeError("lookup down")

    bot.get_business_connection = down  # type: ignore[method-assign]
    assert await run_command(panel, command_update(), bot) is False
    assert bot.api == [] and bot.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["edited", "echo"])
async def test_edited_command_and_echo_are_claimed_without_executing(plug, ds, panel, kind):
    await build(plug, ds)
    llm = FakeLlm()
    ds.bind_llm(llm)
    bot = PanelBot()
    update = command_update(edited=True) if kind == "edited" else echo_update()

    assert await run_command(panel, update, bot) is True

    assert bot.api == [] and bot.deletes == [] and bot.sent == [] and llm.calls == []
    # The echo's id is remembered, so the owner deleting the post later stays out of history.
    assert plug._history_support._is_control_message(BUSINESS_ID, CHAT, 900) is (kind == "echo")


def up_two_days(plug: ModuleType) -> None:
    """This gateway process has run longer than the window, so it would have noted any newer contact message."""
    plug._dossier_panel._post._STARTED_AT = time.monotonic() - 2 * 86400  # a fresh plugin copy per test


async def old_chat(plug: ModuleType) -> None:
    """The contact's last text is 30 hours old, the owner's 29 hours."""
    await seed(plug, CHAT, "anna_k", [(10, "in", ROWS[0][2], timedelta(hours=30)),
                                      (11, "out", ROWS[1][2], timedelta(hours=29))], first_name="Anna")


async def _refusal_case(plug: ModuleType, ds: ModuleType, reason: str) -> None:
    if reason == "window":  # the contact wrote 30 hours ago, and the gateway has run since then
        await old_chat(plug)
        up_two_days(plug)
    elif reason == "empty":  # only the contact wrote
        await seed(plug, CHAT, "anna_k", [(10, "in", ROWS[0][2], 30)], first_name="Anna")
    else:
        await build(plug, ds)
    if reason == "disabled":
        ds.set_enabled(False, via="cli")
    elif reason == "history_off":
        test_history.set_switch(plug, "history", False)
    elif reason == "chat_off":
        plug._chat_settings.default_store().set_flag(OWNER_ID, CHAT, "dossier", False, via="cli")


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "expected"), [
    ("disabled", "Dossiers are off. Turn them on in Settings (/telesec)."),
    ("no_model", "The model is not connected."),
    ("history_off", "History is off, so deleted messages can't be tracked. Turn history on in Settings (/telesec)."),
    ("chat_off", "Dossier is off for this chat."),
    ("window", ("The contact hasn't written in the last 24 hours, and Telegram lets a bot post only within 24 hours "
                "of their last message.")),
    ("empty", "This chat has nothing to summarise yet."),
])
async def test_refusals_tell_only_the_owner_and_call_no_model(plug, ds, panel, reason, expected):
    await _refusal_case(plug, ds, reason)
    llm = FakeLlm(doc(SUMMARY))
    if reason != "no_model":
        ds.bind_llm(llm)
    bot = PanelBot()

    assert await run_command(panel, command_update(), bot) is True

    assert posts(bot) == [] and bot.api == [] and llm.calls == []
    assert bot.deletes == [{"business_connection_id": BUSINESS_ID, "message_ids": [500]}]
    (sent,) = bot.sent
    assert sent["chat_id"] == OWNER_ID and sent["parse_mode"] is None and "business_connection_id" not in sent
    assert sent["text"] == f"{HEAD}\n{expected}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "expected"), [
    (doc("EMPTY"), "This chat has nothing to summarise yet."),
    ("A summary without the end line", "The summary could not be written (no_end_marker)."),
    (RuntimeError(f"provider said {COMMITMENT}"), "The summary could not be written (RuntimeError)."),
])
async def test_an_empty_or_failed_summary_is_told_to_the_owner(plug, ds, panel, caplog, response, expected):
    await build(plug, ds)
    llm = FakeLlm(response)
    ds.bind_llm(llm)
    bot = PanelBot()

    with caplog.at_level(logging.DEBUG):
        assert await run_command(panel, command_update(), bot) is True

    assert posts(bot) == [] and len(llm.calls) == 1
    assert [sent["text"] for sent in bot.sent] == [f"{HEAD}\n{expected}"]
    assert "dossier not posted chat_ref=" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_refusal_is_localized(dossier_history, panel):
    bot = PanelBot()

    assert await run_command(panel, command_update(lang="ru"), bot)

    assert "Досье выключены. Включите их в Настройках (/telesec)." in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_handler_returns_before_the_summary_is_written_and_a_second_command_is_busy(plug, ds, panel):
    await build(plug, ds)
    llm, release, entered = holding_llm(doc(SUMMARY))
    ds.bind_llm(llm)
    bot = PanelBot()
    try:
        assert await panel.handle_dossier_command_update(command_update(), bot=bot, adapter=None,
                                                         mark_seen=lambda _key: True)
        assert await asyncio.to_thread(entered.wait, 5)  # the model call runs after the handler returned
        assert posts(bot) == [] and bot.sent == []
        assert await panel.handle_dossier_command_update(command_update(message_id=501), bot=bot, adapter=None,
                                                         mark_seen=lambda _key: True)
        assert [sent["text"] for sent in bot.sent] == [f"{HEAD}\nA summary for this chat is already being prepared."]
    finally:
        release.set()
        await panel.join_posts()

    assert len(posts(bot)) == 1 and len(llm.calls) == 1
    assert [d["message_ids"] for d in bot.deletes] == [[500], [501]]  # both commands leave the chat
    assert panel._POST_TASKS == {}


@pytest.mark.asyncio
async def test_wall_cap_tells_the_owner_and_stops_the_build(plug, ds, panel, monkeypatch):
    await build(plug, ds)
    llm, release, entered = holding_llm(LONG_SUMMARY)
    ds.bind_llm(llm)
    built = watch_build(panel, monkeypatch)
    monkeypatch.setattr(panel._post, "POST_WALL_SECONDS", 0.2)
    bot = PanelBot()
    try:
        assert await run_command(panel, command_update(), bot)
    finally:
        release.set()

    assert entered.is_set() and posts(bot) == []
    assert [sent["text"] for sent in bot.sent] == [f"{HEAD}\nThe summary could not be written (timeout)."]
    assert await asyncio.to_thread(built.wait, 5)
    assert [call["purpose"] for call in llm.calls] == ["telesec_post"]  # no shorten call after the cap


@pytest.mark.asyncio
async def test_wall_cap_never_cuts_a_send_that_has_started(plug, ds, panel, monkeypatch):
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    monkeypatch.setattr(panel._post, "POST_WALL_SECONDS", 0.5)

    class SlowBot(PanelBot):
        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any] | None = None, **kwargs: Any) -> Any:
            sent = await super().do_api_request(endpoint, api_kwargs, **kwargs)
            await asyncio.sleep(0.5)  # Telegram has the post; its answer comes after the cap
            return sent

    bot = SlowBot()
    assert await run_command(panel, command_update(), bot)

    assert len(posts(bot)) == 1 and bot.sent == []  # the owner is never told that nothing was posted
    assert plug._history_support._is_control_message(BUSINESS_ID, CHAT, 900)


@pytest.mark.asyncio
async def test_an_unreadable_catalog_tells_the_owner_with_a_code_only(plug, ds, panel, monkeypatch):
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("SECRET-PATH")

    monkeypatch.setattr(panel._history, "_resolve_history_entry", broken)
    monkeypatch.setattr(panel._history, "_catalog_entry_display_name", broken)
    monkeypatch.setattr(panel, "contact_entry", broken)
    bot = PanelBot()
    assert await run_command(panel, command_update(), bot)

    assert posts(bot) == []
    assert [sent["text"] for sent in bot.sent] == [
        f"Nothing was posted to the chat with chat:{CHAT}.\nThe summary could not be written (PermissionError)."]
    assert "SECRET" not in bot.sent[0]["text"]
    assert panel._POST_TASKS == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["stop_refresh", "stop_background"])
async def test_stopping_cancels_a_post_in_flight(plug, ds, panel, monkeypatch, stop):
    await build(plug, ds)
    llm, release, entered = holding_llm(LONG_SUMMARY)
    ds.bind_llm(llm)
    built = watch_build(panel, monkeypatch)
    bot = PanelBot()
    try:
        assert await panel.handle_dossier_command_update(command_update(), bot=bot, adapter=None,
                                                         mark_seen=lambda _key: True)
        assert await asyncio.to_thread(entered.wait, 5)
        if stop == "stop_refresh":  # "No dossier", "Don't save" and an erase call it from a worker thread
            await asyncio.to_thread(panel.stop_refresh, BUSINESS_ID, 992)
            task, event = panel._POST_TASKS[next(iter(panel._POST_TASKS))]
            assert not task.done() and not event.is_set()  # another chat's stop
            await asyncio.to_thread(panel.stop_refresh, BUSINESS_ID, CHAT)
        else:
            panel.stop_background()  # on_unload
        await panel.join_posts()
    finally:
        release.set()

    assert posts(bot) == [] and bot.sent == []  # no post and no message to the owner
    assert panel._POST_TASKS == {}
    assert await asyncio.to_thread(built.wait, 5)
    assert [call["purpose"] for call in llm.calls] == ["telesec_post"]  # the build made no shorten call


@pytest.mark.asyncio
async def test_a_build_the_owner_stopped_tells_the_owner_nothing(plug, ds, panel, monkeypatch):
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))

    async def stopped_build(bot, entry, business_connection_id, chat_id, started, stop):
        stop.set()  # stop_refresh set the event, and the build saw it before the cancel landed
        return "failed", "Stopped"

    monkeypatch.setattr(panel, "_build_and_send", stopped_build)
    bot = PanelBot()

    assert await run_command(panel, command_update(), bot) is True

    assert posts(bot) == [] and bot.sent == []


@pytest.mark.asyncio
async def test_rejected_post_and_failed_delete_are_told_and_logged_by_type(plug, ds, panel, caplog):
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    bot = PanelBot()
    bot.api_error = BadRequest(f"Bad Request: can't parse {COMMITMENT}")
    bot.delete_error = RuntimeError(f"no rights for {NOTE}")

    with caplog.at_level(logging.DEBUG):
        assert await run_command(panel, command_update(), bot) is True

    assert len(posts(bot)) == 1 and [endpoint for endpoint, _ in bot.api] == ["sendRichMessage"]  # no plain retry
    (sent,) = bot.sent
    assert sent["chat_id"] == OWNER_ID
    assert "Telegram did not accept the summary (BadRequest)." in sent["text"]
    assert "BadRequest" in caplog.text and "RuntimeError" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


class Forbidden(Exception):
    pass


class NetworkError(Exception):  # PTB's names: TimedOut and BadRequest are NetworkErrors there
    pass


class TimedOut(NetworkError):
    pass


class PtbBadRequest(NetworkError):
    pass


PtbBadRequest.__name__ = "BadRequest"


WINDOW = ("The contact hasn't written in the last 24 hours, and Telegram lets a bot post only within 24 hours of "
          "their last message.")
MAYBE_WINDOW = "One possible cause: a bot can post in a Business chat only if the contact wrote there in the last 24 hours."


@pytest.mark.asyncio
@pytest.mark.parametrize(("noted", "error", "expected", "reason"), [
    # Only Telegram's own inactive-chat error is told as the window, even when the window looked open.
    (False, BadRequest("Bad Request: BUSINESS_PEER_USAGE_MISSING"), WINDOW, "window"),
    (False, BadRequest("Bad Request: business peer usage missing"), WINDOW, "window"),
    (True, BadRequest("Bad Request: BUSINESS_PEER_USAGE_MISSING"), WINDOW, "window"),
    # Any other refusal while the window is unknown names the 24 hours as a possible cause only.
    (False, BadRequest("Bad Request: can't parse entities"),
     f"Telegram did not accept the summary (BadRequest). {MAYBE_WINDOW}", "send_refused"),
    (False, Forbidden("Forbidden: bot can't send messages to the user"),
     f"Telegram did not accept the summary (Forbidden). {MAYBE_WINDOW}", "send_refused"),
    (False, PtbBadRequest("Bad Request: can't parse entities"),
     f"Telegram did not accept the summary (BadRequest). {MAYBE_WINDOW}", "send_refused"),  # not a lost connection
    (False, RuntimeError("Unexpected"), "Telegram did not accept the summary (RuntimeError).", "send_failed"),
    (True, BadRequest("Bad Request: can't parse entities"), "Telegram did not accept the summary (BadRequest).",
     "send_failed"),
])
async def test_a_fresh_gateway_tries_the_post_and_tells_only_telegram_window_error_as_the_window(
        plug, ds, panel, caplog, noted, error, expected, reason):
    await old_chat(plug)  # the gateway started less than 24 hours ago: a voice note before it may count
    if noted:  # e.g. a sticker this process saw: the window is known open, so a refusal is something else
        panel._post.note_inbound(BUSINESS_ID, CHAT, NOW - timedelta(minutes=30))
    llm = FakeLlm(doc(SUMMARY))
    ds.bind_llm(llm)
    bot = PanelBot()
    bot.api_error = error

    with caplog.at_level(logging.INFO):
        assert await run_command(panel, command_update(), bot) is True

    assert len(posts(bot)) == 1 and len(llm.calls) == 1  # it tried
    (sent,) = bot.sent
    assert sent["chat_id"] == OWNER_ID and sent["text"] == f"{HEAD}\n{expected}"
    assert f"reason={reason} error={type(error).__name__}" in caplog.text
    assert str(error) not in caplog.text  # compared in memory only


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimedOut("Timed out"), NetworkError("httpx.ReadError: Anna"),
                                   TimeoutError("Anna"), ConnectionResetError("Anna")])
async def test_a_send_that_timed_out_may_have_posted(plug, ds, panel, caplog, error):
    await old_chat(plug)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    bot = PanelBot()
    bot.api_error = error

    with caplog.at_level(logging.INFO):
        assert await run_command(panel, command_update(), bot) is True

    assert len(posts(bot)) == 1  # it was sent once and never retried: the post may be in the chat
    (sent,) = bot.sent
    name = type(error).__name__
    assert sent["chat_id"] == OWNER_ID and sent["text"] == (
        "The summary may have been posted to the chat with @anna_k.\n"
        f"Telegram did not confirm it ({name}). Check the chat before you send /dossier again.")
    assert "Nothing was posted" not in sent["text"]
    assert "dossier post unconfirmed chat_ref=" in caplog.text and f"reason=send_unsure error={name}" in caplog.text
    assert "not posted" not in caplog.text and "Anna" not in caplog.text


@pytest.mark.asyncio
async def test_failed_private_reason_is_logged_by_type(dossier_history, panel, caplog):
    bot = PanelBot()
    bot.send_error = RuntimeError("Forbidden: bot was blocked by the user Anna")

    with caplog.at_level(logging.DEBUG):
        assert await run_command(panel, command_update(), bot) is True

    assert "could not send the /dossier reason to the owner error=RuntimeError" in caplog.text
    assert "Anna" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("where", "logged"), [
    ("post_precheck", "dossier not posted chat_ref="),
    ("post_recheck", "dossier not posted chat_ref="),
    ("_start_post", "/dossier command failed error=PermissionError"),
    ("_post_task", "dossier post failed error=PermissionError"),
])
async def test_a_failure_inside_the_post_is_told_and_logged_by_type_only(plug, ds, panel, monkeypatch, caplog,
                                                                        where, logged):
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("SECRET-PATH/chats/7654321")  # e.g. a history path with its ids

    async def broken_async(*args: Any, **kwargs: Any) -> Any:
        return broken(*args, **kwargs)

    if where.startswith("post_"):
        monkeypatch.setattr(panel._post, where, broken)
    else:
        monkeypatch.setattr(panel, where, broken_async)
    bot = PanelBot()
    with caplog.at_level(logging.DEBUG):
        assert await run_command(panel, command_update(), bot) is True

    assert posts(bot) == [] and logged in caplog.text
    told = [sent["text"] for sent in bot.sent]
    if where.startswith("post_"):
        assert told == [f"{HEAD}\nThe summary could not be written (PermissionError)."]
        assert "reason=failed error=PermissionError" in caplog.text
    else:
        assert told == []
    assert not [r for r in caplog.records if r.exc_info]
    for secret in ("SECRET", "7654321"):
        assert secret not in caplog.text and not [text for text in told if secret in text]


def _business_handler(plug: ModuleType) -> Any:
    handlers: list[tuple[Any, int]] = []
    plug._register_telegram_handlers(SimpleNamespace(add_handler=lambda h, group: handlers.append((h, group))),
                                     SimpleNamespace(platform="telegram"))
    (handler,) = [h for h, _group in handlers if type(h).__name__ == "BusinessHandler"]
    return handler


@pytest.mark.asyncio
async def test_business_handler_keeps_command_and_echo_out_of_history_and_core(plug, ds, panel, monkeypatch):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")  # even when other Business updates reach the agent
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    history_before = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    bot = PanelBot()
    handler = _business_handler(plug)
    context = SimpleNamespace(bot=bot)

    for _ in range(2):  # the second is an in-process redelivery
        with pytest.raises(ApplicationHandlerStop):
            await handler.callback(command_update(message_id=600), context)
    await panel.join_posts()
    with pytest.raises(ApplicationHandlerStop):
        await handler.callback(echo_update(901), context)
    with pytest.raises(ApplicationHandlerStop):
        await handler.callback(command_update("/dossier", edited=True, message_id=600), context)
    await panel.join_posts()
    assert len(posts(bot)) == 1 and len(bot.deletes) == 1
    assert load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT) == history_before

    await handler.callback(command_update("/dossier", sender=CONTACT_ID, message_id=601), context)
    records = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    assert [(r["message_id"], r["direction"], r["text"]) for r in records[len(history_before):]] == [
        (601, "inbound", "/dossier")]


@pytest.mark.asyncio
async def test_deleting_the_command_or_the_post_stays_out_of_history(plug, ds, panel):
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    history = plug._history_support
    before = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    bot = PanelBot()  # sendRichMessage answers message_id 900
    with pytest.raises(ApplicationHandlerStop):
        await _business_handler(plug).callback(command_update(message_id=610), SimpleNamespace(bot=bot))
    await panel.join_posts()
    assert len(posts(bot)) == 1 and bot.deletes[0]["message_ids"] == [610]

    fake = test_history.FakeBot(owner_id=OWNER_ID)
    for message_ids in ((610,), (900,)):  # the bot deletes the command; later the owner deletes the post
        deleted = make_deleted_update(business_id=BUSINESS_ID, chat_id=CHAT, chat_type="private",
                                      message_ids=message_ids, update_id=8000 + message_ids[0])
        assert not await history.observe_ptb_update(deleted, bot=fake)
    assert load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT) == before

    other = make_deleted_update(business_id=BUSINESS_ID, chat_id=CHAT, chat_type="private", message_ids=(611,),
                                update_id=8611)  # a message from before history still counts
    assert await history.observe_ptb_update(other, bot=fake)
    added = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)[len(before):]
    assert [(r["event_type"], r["message_id"]) for r in added] == [("message.deleted", 611)]


@pytest.mark.asyncio
async def test_handler_failure_never_breaks_dispatch(plug, ds, panel, monkeypatch, caplog):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")

    async def broken(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError(f"boom {NOTE}")

    monkeypatch.setattr(plug._dossier_panel, "handle_dossier_command_update", broken)
    handler = _business_handler(plug)
    with caplog.at_level(logging.WARNING):
        await handler.callback(command_update("hello", message_id=602), SimpleNamespace(bot=PanelBot()))

    assert "dossier command handling failed (RuntimeError)" in caplog.text and NOTE not in caplog.text
    assert [r["text"] for r in load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)] == ["hello"]


def chat_update(kind: str, *, message_id: int, at: Any, sender: int = CONTACT_ID, edited: bool = False) -> Any:
    """A Business message, from the contact by default: ``text``, or media without text (a photo, a sticker, a file)."""
    text = "Thanks!" if kind == "text" else None
    update = command_update(text, sender=sender, edited=edited, message_id=message_id)  # type: ignore[arg-type]
    message = update.effective_message
    message.date = at
    if kind == "photo":
        message.photo, message.caption = (SimpleNamespace(file_id="photo-1"),), "please check by Friday"
    elif kind != "text":
        setattr(message, kind, SimpleNamespace(file_id=f"{kind}-1"))
    return update


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["photo", "sticker", "document"])
async def test_a_contact_who_lately_sent_only_media_still_gets_the_summary(plug, ds, panel, monkeypatch, kind):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")  # media reaches the agent; the handler notes it first
    await _refusal_case(plug, ds, "window")  # the contact's last text is 30 hours old, the gateway is up 2 days
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    bot = PanelBot()
    handler = _business_handler(plug)
    context = SimpleNamespace(bot=bot)

    # An edit is not a new message, even one Telegram delivers now (its date stays the original one).
    await handler.callback(chat_update("text", message_id=10, at=NOW - timedelta(hours=30), edited=True), context)
    await handler.callback(chat_update("photo", message_id=31, at=NOW - timedelta(minutes=40), sender=OWNER_ID),
                           context)  # nor is the owner's own message
    assert panel._post._LAST_INBOUND == {}
    assert await run_command(panel, command_update(message_id=500), bot)
    assert posts(bot) == [] and [sent["text"].split("\n")[1] for sent in bot.sent] == [WINDOW]

    await handler.callback(chat_update(kind, message_id=30, at=NOW - timedelta(minutes=30)), context)
    assert await run_command(panel, command_update(message_id=501), bot)

    assert len(posts(bot)) == 1 and len(bot.sent) == 1
    assert not any(r.get("message_id") == 30 for r in load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voice", "video_note"])
async def test_a_contact_voice_or_video_note_is_noted_before_transcription_claims_it(plug, ds, panel, kind):
    """Transcription claims the update and history keeps no voice message, yet it opens the 24-hour window."""
    await _refusal_case(plug, ds, "window")  # the contact's last text is 30 hours old, the gateway is up 2 days
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    bot = PanelBot()
    at = NOW - timedelta(minutes=30)

    with pytest.raises(ApplicationHandlerStop):  # claimed for transcription, so it never reaches core
        await _business_handler(plug).callback(chat_update(kind, message_id=30, at=at), SimpleNamespace(bot=bot))

    assert panel._post._LAST_INBOUND == {(BUSINESS_ID, str(CHAT)): at}
    assert await run_command(panel, command_update(message_id=501), bot)
    assert len(posts(bot)) == 1 and bot.sent == []


def test_without_a_cached_owner_the_chat_tells_the_contact_and_telegram_is_not_asked(dossier_history, panel):
    at = NOW - timedelta(minutes=5)
    assert panel._history.cached_business_owner(BUSINESS_ID) is None

    panel.note_inbound_update(chat_update("sticker", message_id=33, at=at, sender=OWNER_ID))
    assert panel._post._LAST_INBOUND == {}
    panel.note_inbound_update(chat_update("sticker", message_id=34, at=at, sender=CHAT))  # a private chat's id
    assert panel._post._LAST_INBOUND == {(BUSINESS_ID, str(CHAT)): at}


@pytest.mark.asyncio
async def test_a_failed_inbound_note_never_breaks_dispatch(plug, ds, panel, monkeypatch, caplog):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(f"boom {NOTE}")

    monkeypatch.setattr(plug._dossier_panel, "note_inbound_update", broken)
    update = chat_update("text", message_id=32, at=NOW)
    with caplog.at_level(logging.DEBUG):
        await _business_handler(plug).callback(update, SimpleNamespace(bot=PanelBot()))

    assert "inbound message note failed (RuntimeError)" in caplog.text and NOTE not in caplog.text
    assert [r["text"] for r in load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)] == ["Thanks!"]


@pytest.mark.asyncio
async def test_history_filter_is_a_second_safeguard(plug, ds):
    history = plug._history_support
    bot = test_history.FakeBot(owner_id=OWNER_ID)

    assert not await history.observe_ptb_update(command_update(message_id=700), bot=bot)
    assert not await history.observe_ptb_update(command_update("/dossier", edited=True, message_id=700), bot=bot)
    edited_echo = echo_update(701)  # without text (see the next test for one with text)
    edited_echo.edited_business_message, edited_echo.business_message = edited_echo.business_message, None
    assert not await history.observe_ptb_update(edited_echo, bot=bot)
    echo = echo_update(701)
    echo.business_message.text = "a client that adds text to rich messages"
    assert not await history.observe_ptb_update(echo, bot=bot)
    assert load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT) == []

    assert await history.observe_ptb_update(command_update("/dossier", sender=CONTACT_ID, message_id=702), bot=bot)
    assert await history.observe_ptb_update(command_update("/dossiers", message_id=703), bot=bot)
    records = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    assert [(r["message_id"], r["direction"], r["text"]) for r in records] == [
        (702, "inbound", "/dossier"), (703, "outbound", "/dossiers")]


@pytest.mark.asyncio
async def test_owner_edit_into_the_command_still_updates_history(plug, ds, panel, monkeypatch):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")
    handler = _business_handler(plug)
    bot = PanelBot()
    context = SimpleNamespace(bot=bot)
    await handler.callback(command_update("See you at 5", message_id=710), context)

    with pytest.raises(ApplicationHandlerStop):  # claimed and never executed
        await handler.callback(command_update("/dossier", edited=True, message_id=710), context)
    with pytest.raises(ApplicationHandlerStop):  # an edit of a command history never stored
        await handler.callback(command_update("/dossier", edited=True, message_id=711), context)
    await panel.join_posts()

    assert posts(bot) == [] and bot.deletes == []
    records = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    assert [(r["event_type"], r["message_id"], r["text"]) for r in records] == [
        ("message.created", 710, "See you at 5"), ("message.edited", 710, "/dossier")]


@pytest.mark.asyncio
async def test_owner_edit_uses_the_resolved_owner_when_the_lookup_fails(plug, ds, panel, monkeypatch):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")
    handler = _business_handler(plug)
    panel._modes.default_store().bind_connection(BUSINESS_ID, OWNER_ID)  # the recorded owner
    plug._history_support._OWNER_CACHE.clear()  # a fresh gateway

    class OfflineBot(PanelBot):
        async def get_business_connection(self, business_connection_id: str) -> Any:
            self.lookups.append(business_connection_id)
            raise TimeoutError("Telegram is slow")

    bot = OfflineBot()
    with pytest.raises(ApplicationHandlerStop):  # claimed as the owner's, never executed
        await handler.callback(command_update("/dossier", edited=True, message_id=711), SimpleNamespace(bot=bot))

    assert load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT) == []  # history never had 711
    assert len(bot.lookups) == 1 and posts(bot) == []


@pytest.mark.asyncio
async def test_edited_dossier_post_over_a_stored_id_is_not_recorded(plug, ds):
    history = plug._history_support
    bot = test_history.FakeBot(owner_id=OWNER_ID)
    assert await history.observe_ptb_update(command_update("See you at 5", message_id=701), bot=bot)
    before = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    edited_echo = echo_update(701)
    edited_echo.edited_business_message, edited_echo.business_message = edited_echo.business_message, None
    edited_echo.edited_business_message.text = "a client that adds text to rich messages"
    assert not await history.observe_ptb_update(edited_echo, bot=bot)
    assert load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT) == before


@pytest.mark.asyncio
async def test_logs_never_carry_dossier_text(plug, ds, panel, caplog):
    with caplog.at_level(logging.DEBUG):
        await build(plug, ds)
        ds.bind_llm(FakeLlm(doc(SUMMARY), doc(DOCUMENT)))
        for args in (None, "o.c", "o.p", "zz", "m.c.0"):
            await act(panel, args)
        await run_command(panel, command_update(), PanelBot())
        await act(panel, "rf.c")
        panel.join_refreshes(timeout=5)

    assert "dossier posted chat_ref=" in caplog.text and "dossier call purpose=telesec_post" in caplog.text
    assert "dossier refresh started chat_ref=" in caplog.text
    assert not [r for r in caplog.records if r.exc_info]
    for secret in SECRETS:
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Real PTB 22.8 dispatch and serialization
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def telegram_app() -> Any:
    request = TelegramRequest()
    request.responses.update({
        "deleteBusinessMessages": (200, {"ok": True, "result": True}),
        "sendRichMessage": (200, {"ok": True, "result": {
            "message_id": 900, "date": 1, "chat": {"id": CHAT, "type": "private"},
            "rich_message": {"blocks": []}}}),
    })
    bot = ExtBot(token="123:test-token", request=request)
    app = Application.builder().bot(bot).updater(None).build()
    async with app:
        yield app, request


@pytest.mark.asyncio
async def test_real_ptb_dossier_command_serializes_the_rich_message_and_skips_core(plug, ds, telegram_app,
                                                                                  monkeypatch):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")  # ordinary Business messages reach the agent
    app, request = telegram_app
    await build(plug, ds)
    ds.bind_llm(FakeLlm(doc(SUMMARY)))
    history_before = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    plug._register_telegram_handlers(app, SimpleNamespace(platform="telegram", _bot=app.bot))
    core: list[Any] = []

    async def on_core(update: Any, _context: Any) -> None:
        core.append(update)

    app.add_handler(TypeHandler(Update, on_core))
    sender = {"id": OWNER_ID, "is_bot": False, "first_name": "Owner", "language_code": "en"}
    chat = {"id": CHAT, "type": "private", "first_name": "Anna", "username": "anna_k"}
    command = Update.de_json({"update_id": 1, "business_message": {
        "message_id": 500, "date": 1, "business_connection_id": BUSINESS_ID, "chat": chat, "from": sender,
        "text": "/dossier", "entities": [{"type": "bot_command", "offset": 0, "length": 8}]}}, app.bot)
    echo = Update.de_json({"update_id": 2, "business_message": {
        "message_id": 900, "date": 2, "business_connection_id": BUSINESS_ID, "chat": chat, "from": sender,
        "sender_business_bot": {"id": 123, "is_bot": True, "first_name": "Test", "username": "test_bot"},
        "rich_message": {"blocks": [{"type": "paragraph"}]}}}, app.bot)
    plain = Update.de_json({"update_id": 3, "business_message": {
        "message_id": 501, "date": 3, "business_connection_id": BUSINESS_ID, "chat": chat, "from": sender,
        "text": "See you Friday"}}, app.bot)

    await app.process_update(command)
    await plug._dossier_panel.join_posts()
    await app.process_update(echo)
    await app.process_update(plain)

    assert core == [plain]
    endpoints = [endpoint for endpoint, _ in request.calls]
    assert endpoints.count("sendRichMessage") == 1 and "deleteBusinessMessages" in endpoints
    params = dict(request.calls)["sendRichMessage"]
    assert set(params) == {"business_connection_id", "chat_id", "rich_message"}
    assert (params["business_connection_id"], params["chat_id"]) == (BUSINESS_ID, str(CHAT))
    rich = json.loads(params["rich_message"])
    assert rich["skip_entity_detection"] is True and SUMMARY_LINE in rich["html"] and NOTE not in rich["html"]
    assert json.loads(dict(request.calls)["deleteBusinessMessages"]["message_ids"]) == [500]
    texts = [r["text"] for r in load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)[len(history_before):]]
    assert texts == ["See you Friday"]
