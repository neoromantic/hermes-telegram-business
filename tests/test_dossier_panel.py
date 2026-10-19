"""Dossiers in Telegram (issue #17): snapshot loading, the owner panel section and the owner's ``/dossier``.

Dossiers are built on disk by the real capture path and cycle with a scripted model; the Bot API is a
recording fake, plus one run through real PTB 22.8 dispatch and request serialization.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import shutil
import threading
from collections.abc import Callable
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
    change,
    cycle,
    dossier_dir,
    make_deleted_update,
    output,
    persona_dir,
    read_changes,
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
from test_history import load_records
from test_telegram_integration import TelegramRequest

CHAT = 991
CONTACT_ID = 2000
ROWS = [(10, "in", "I prefer calls after 6pm on weekdays.", 60),
        (11, "out", "Sure, I will send the contract by Friday.", 59)]
COMMITMENT = "The owner will send the contract by Friday."
PREFERENCE = "The contact prefers calls after 6pm."
PERSONA_FACT = "The owner writes short friendly replies."
NOTE = "Owner-only note: never mention the discount."
CONTACT_FACTS = output(
    change(section="commitments", text=COMMITMENT, sources=(("m11", "I will send the contract by Friday"),)),
    change(section="preferences", text=PREFERENCE, sources=(("m10", "I prefer calls after 6pm"),)),
)
PERSONA_FACTS = output(change(section="tone", kind="inferred", conf="medium", text=PERSONA_FACT, sources=("c1:m11",)))
SECRETS = (COMMITMENT, PREFERENCE, PERSONA_FACT, NOTE, "I prefer calls", "send the contract", "Anna", "anna_k")


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
    """A contact dossier (f1 commitment, f2 preference, f3 owner note) and a persona (f1) on disk."""
    await seed(plugin, CHAT, "anna_k", ROWS, first_name="Anna")
    report = cycle(ds, FakeLlm(CONTACT_FACTS, PERSONA_FACTS))
    assert (report.refreshed, report.persona) == (1, "refreshed")
    if note:
        ds.apply_owner_edit(("contact", catalog_entry(plugin, CHAT)), "note", section="relationship", text=NOTE)
    return read_dossier(ds, CHAT)


def prefix(dossier: dict[str, Any]) -> str:
    return dossier["dossier_id"][:6]


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
    """The bot-posted dossier as Telegram echoes it: no text, ``rich_message`` in PTB's read-only api_kwargs."""
    update = command_update(None, message_id=message_id, bot_sender=BOT_ID)  # type: ignore[arg-type]
    update.business_message.api_kwargs = MappingProxyType({"rich_message": {"blocks": [{"type": "paragraph"}]}})
    return update


async def run_command(panel: Any, update: Any, bot: PanelBot, seen: set[Any] | None = None) -> bool:
    seen = set() if seen is None else seen

    def mark_seen(key: Any) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    return await panel.handle_dossier_command_update(update, bot=bot, adapter=None, mark_seen=mark_seen)


def posts(bot: PanelBot) -> list[dict[str, Any]]:
    return [kwargs for endpoint, kwargs in bot.api if endpoint == "sendRichMessage"]


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_snapshot_ok(plug, ds, panel):
    dossier = await build(plug, ds)

    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)

    entry = catalog_entry(plug, CHAT)
    assert snap.kind == "contact" and snap.status == "ok" and snap.dossier == dossier
    assert snap.label == plug._history_support._catalog_entry_display_name(entry)
    assert (snap.enabled, snap.llm_ready, snap.refreshing, snap.cycle_busy) == (True, False, False, False)
    cfg = ds.load_config()
    assert (snap.provider, snap.model, snap.backfill_days) == (cfg.provider, cfg.model, cfg.backfill_days)
    assert snap.failure_error is None and snap.stale == {} and snap.stale_error is None
    assert sorted(snap.sources_now) == ["f1", "f2", "f3"]
    assert [s["state"] for s in snap.sources_now["f1"]] == ["present"]
    assert snap.sources_now["f3"] == []  # the note has no sources
    assert (snap.business_connection_id, snap.chat_id) == (BUSINESS_ID, CHAT)


@pytest.mark.asyncio
async def test_contact_snapshot_without_catalog_entry_uses_a_stub(plug, ds, panel):
    snap = panel.load_contact_snapshot(BUSINESS_ID, 4242, now=NOW)

    assert (snap.status, snap.label, snap.missing_reason) == ("missing", "chat:4242", "unqualified")


@pytest.mark.asyncio
@pytest.mark.parametrize(("rows", "reason"), [
    ([(10, "in", "Hello there, how are you?", 60)], "unqualified"),
    (ROWS, "pending"),
])
async def test_contact_snapshot_missing_reason(plug, ds, panel, rows, reason):
    await seed(plug, CHAT, "anna_k", rows, first_name="Anna")

    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)

    assert (snap.status, snap.dossier, snap.missing_reason) == ("missing", None, reason)
    assert ds.load_state()["failures"] == {}


@pytest.mark.asyncio
async def test_contact_snapshot_failed_attempt(plug, ds, panel):
    await seed(plug, CHAT, "anna_k", ROWS, first_name="Anna")
    report = cycle(ds, FakeLlm(RuntimeError("model down with secret text")))
    assert report.failed == 1

    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)

    assert (snap.status, snap.missing_reason, snap.failure_error) == ("missing", "failed", "RuntimeError")
    assert "RuntimeError" in panel._view.render_overview(snap, "en").text


@pytest.mark.asyncio
async def test_contact_snapshot_corrupt(plug, ds, panel):
    await build(plug, ds)
    (dossier_dir(ds, CHAT) / "dossier.json").write_text("{not json", encoding="utf-8")

    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)
    view = await act(panel)

    assert (snap.status, snap.dossier) == ("corrupt", None)
    assert "can't be read" in view.text
    assert f"forget --connection {BUSINESS_ID} --chat {CHAT}" in view.text


@pytest.mark.asyncio
async def test_disabled_without_dossier_explains_and_offers_enable(dossier_history, panel):
    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)
    view = await act(panel)

    assert (snap.enabled, snap.status, snap.missing_reason) == (False, "missing", None)
    assert "Dossiers are off." in view.text
    assert "ea" in args_of(view) and "rf.c" not in args_of(view)


@pytest.mark.asyncio
async def test_history_read_failure_still_renders_the_stored_dossier(plug, ds, panel, monkeypatch):
    await build(plug, ds)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("history lock gone")

    monkeypatch.setattr(ds, "views_for_dossier", broken)
    snap = panel.load_contact_snapshot(BUSINESS_ID, CHAT, now=NOW)

    assert (snap.status, snap.stale_error, snap.stale, snap.sources_now) == ("ok", "OSError", {}, {})
    assert COMMITMENT in panel._view.render_overview(snap, "en").text


@pytest.mark.asyncio
async def test_persona_snapshot_has_chat_names(plug, ds, panel):
    await build(plug, ds)

    snap = panel.load_persona_snapshot(now=NOW)

    assert (snap.kind, snap.status) == ("persona", "ok")
    assert snap.chat_names == {"c1": plug._history_support._catalog_entry_display_name(catalog_entry(plug, CHAT))}
    assert [f["text"] for f in snap.dossier["facts"]] == [PERSONA_FACT]
    assert sorted(snap.sources_now) == ["f1"]


@pytest.mark.asyncio
async def test_persona_snapshot_missing_and_corrupt(plug, ds, panel):
    assert panel.load_persona_snapshot(now=NOW).missing_reason == "pending"
    persona_dir(ds).mkdir(parents=True)
    (persona_dir(ds) / "dossier.json").write_text("[]", encoding="utf-8")
    assert panel.load_persona_snapshot(now=NOW).status == "corrupt"


# ---------------------------------------------------------------------------
# Owner panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overview_in_english_and_russian(plug, ds, panel):
    dossier = await build(plug, ds)

    en = await act(panel)
    ru = await act(panel, "o.c", lang="ru-RU")

    assert COMMITMENT in en.html and NOTE in en.html and "Commitments" in en.html
    assert "Обязательства" in ru.html and COMMITMENT in ru.html
    assert args_of(en) == ["m.c.0", "o.p", "ds"]  # no Refresh: the model is not bound here
    assert all(len(args) <= 30 for args in args_of(en))
    assert prefix(dossier) not in args_of(en)


@pytest.mark.asyncio
async def test_malformed_args_show_the_overview(plug, ds, panel):
    await build(plug, ds)

    for args in ("zz", "pn.c.xyz.1", "f.c.abcdef", "x" * 80, "o.c\n"):
        view = await act(panel, args)
        assert COMMITMENT in view.html and view.notice is None


@pytest.mark.asyncio
async def test_manage_fact_card_and_buttons(plug, ds, panel):
    dossier = await build(plug, ds)
    p = prefix(dossier)

    picker = await act(panel, "m.c.0")
    card = await act(panel, f"f.c.{p}.1")

    assert {f"f.c.{p}.1", f"f.c.{p}.2", f"f.c.{p}.3"} <= set(args_of(picker))
    assert COMMITMENT in card.html and "I will send the contract by Friday" in card.html
    assert args_of(card)[:2] == [f"pn.c.{p}.1", f"ra.c.{p}.1"]


@pytest.mark.asyncio
async def test_pin_and_unpin_change_the_dossier_and_log_an_owner_revision(plug, ds, panel):
    dossier = await build(plug, ds)
    p = prefix(dossier)

    pinned = await act(panel, f"pn.c.{p}.1")
    assert pinned.notice == "f1 pinned."
    assert f"up.c.{p}.1" in args_of(pinned)
    stored = read_dossier(ds, CHAT)
    assert stored["facts"][0]["pinned"] is True and stored["revision"] == dossier["revision"] + 1
    line = read_changes(dossier_dir(ds, CHAT))[-1]
    assert (line["actor"], line["trigger"], [op["op"] for op in line["changes"]]) == ("owner", "owner", ["pin"])

    unpinned = await act(panel, f"up.c.{p}.1", lang="ru")
    assert unpinned.notice == "f1 откреплён."
    assert read_dossier(ds, CHAT)["facts"][0]["pinned"] is False
    assert read_changes(dossier_dir(ds, CHAT))[-1]["changes"][0]["op"] == "unpin"


@pytest.mark.asyncio
async def test_remove_asks_then_removes(plug, ds, panel):
    dossier = await build(plug, ds)
    p = prefix(dossier)

    ask = await act(panel, f"ra.c.{p}.2")
    assert PREFERENCE in ask.html and args_of(ask) == [f"rd.c.{p}.2", f"f.c.{p}.2"]
    assert [f["id"] for f in read_dossier(ds, CHAT)["facts"]] == ["f1", "f2", "f3"]

    done = await act(panel, f"rd.c.{p}.2")
    assert done.notice == "f2 removed."
    stored = read_dossier(ds, CHAT)
    assert [f["id"] for f in stored["facts"]] == ["f1", "f3"]
    assert PREFERENCE in [r["text"] for r in stored["removed"]]
    assert read_changes(dossier_dir(ds, CHAT))[-1]["changes"][0]["op"] == "remove"
    assert f"f.c.{p}.2" not in args_of(done)


@pytest.mark.asyncio
async def test_stale_buttons_never_edit_a_rebuilt_dossier(plug, ds, panel):
    old = await build(plug, ds, note=False)
    ds.forget(("contact", catalog_entry(plug, CHAT)))
    assert cycle(ds, FakeLlm(CONTACT_FACTS)).refreshed == 1
    new = read_dossier(ds, CHAT)
    assert prefix(new) != prefix(old)
    before = snapshot_tree(dossier_dir(ds, CHAT))

    for args in (f"pn.c.{prefix(old)}.1", f"up.c.{prefix(old)}.1", f"rd.c.{prefix(old)}.1", f"f.c.{prefix(old)}.1",
                 f"ra.c.{prefix(old)}.1", f"pn.c.{prefix(new)}.99"):
        view = await act(panel, args)
        assert view.text == "This dossier changed. Open it again.", args
        assert args_of(view) == ["o.c"]

    assert snapshot_tree(dossier_dir(ds, CHAT)) == before


@pytest.mark.asyncio
async def test_persona_overview_and_pin(plug, ds, panel):
    await build(plug, ds)
    persona = read_persona(ds)
    p = prefix(persona)

    overview = await act(panel, "o.p")
    assert PERSONA_FACT in overview.html and "My persona" in overview.html
    assert args_of(overview) == ["m.p.0", "o.c"]
    assert f"f.p.{p}.1" in args_of(await act(panel, "m.p.0"))

    pinned = await act(panel, f"pn.p.{p}.1")
    assert pinned.notice == "f1 pinned." and read_persona(ds)["facts"][0]["pinned"] is True
    assert read_dossier(ds, CHAT)["facts"][0]["pinned"] is False


@pytest.mark.asyncio
async def test_enable_through_the_panel_wakes_an_idle_worker(dossier_history, panel, monkeypatch):
    ds = dossier_history._dossier_support
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    outcomes = spy_worker_cycles(monkeypatch, ds)
    ds.bind_llm(FakeLlm())
    assert ds.ensure_worker_started()
    assert not wait_until(lambda: outcomes, timeout=0.3)  # off: the worker idles

    ask = await act(panel, "ea")
    assert "ALL private chats" in ask.text and f"{ds.DEFAULT_PROVIDER} / {ds.DEFAULT_MODEL}" in ask.text
    assert args_of(ask) == ["ed", "o.c"] and read_settings(ds) is None

    on = await act(panel, "ed")
    assert on.notice == "Dossiers are on. The first cycle starts within a few minutes."
    settings = read_settings(ds)
    assert (settings["enabled"], settings["updated_via"]) == (True, "telegram")
    assert wait_until(lambda: outcomes, timeout=5)  # well before the 60 s poll
    assert "ds" in args_of(on)


@pytest.mark.asyncio
async def test_disable_keeps_stored_dossiers(plug, ds, panel):
    await build(plug, ds)

    off = await act(panel, "ds", lang="ru")

    assert off.notice == "Досье выключены. Сохранённые досье остаются."
    settings = read_settings(ds)
    assert (settings["enabled"], settings["updated_via"]) == (False, "telegram")
    assert read_dossier(ds, CHAT) is not None
    assert COMMITMENT in off.html and "Обработка выключена" in off.text
    assert "ea" in args_of(off)


@pytest.mark.asyncio
async def test_refresh_refused_when_disabled_or_without_a_model(plug, ds, panel):
    await build(plug, ds)

    no_model = await act(panel, "rf.c")
    ds.bind_llm(FakeLlm())
    ds.set_enabled(False, via="cli")
    disabled = await act(panel, "rf.c")

    for view in (no_model, disabled):
        assert view.notice == "Refresh is unavailable: dossiers are off or the model is not connected."
    assert panel.load_contact_snapshot(BUSINESS_ID, CHAT).refreshing is False


@pytest.mark.asyncio
async def test_refresh_reports_a_busy_cycle(plug, ds, panel):
    await build(plug, ds)
    ds.bind_llm(FakeLlm())

    with ds._cycle_lock() as acquired:
        assert acquired
        view = await act(panel, "rf.c")

    assert view.notice == "Another dossier cycle is running. Try again in a minute."


@pytest.mark.asyncio
async def test_refresh_runs_one_real_cycle_per_chat_in_the_background(plug, ds, panel):
    await build(plug, ds)
    await seed(plug, CHAT, "anna_k", [(12, "in", "My new phone number is 555-0100.", 5)], first_name="Anna")
    release, entered = threading.Event(), threading.Event()
    llm = FakeLlm(output(change(section="shared_facts", text="The contact has a new phone number.",
                                sources=(("m12", "My new phone number is 555-0100"),))))

    def hold(_payload: dict[str, Any]) -> None:
        entered.set()
        release.wait(5)

    llm.on_call = hold
    ds.bind_llm(llm)
    try:
        started = await act(panel, "rf.c")
        assert started.notice == "Refresh started. Open the dossier again in a minute."
        assert args_of(started) == ["o.c"]
        assert await asyncio.to_thread(entered.wait, 5)
        again = await act(panel, "rf.c")
        assert again.notice == "Refresh started. Open the dossier again in a minute."
        assert "Refreshing now" in again.text and "rf.c" not in args_of(again)
        assert panel.load_contact_snapshot(BUSINESS_ID, CHAT).refreshing is True
    finally:
        release.set()
        panel.join_refreshes(timeout=5)

    assert len(llm.calls) == 1
    stored = read_dossier(ds, CHAT)
    assert "The contact has a new phone number." in [f["text"] for f in stored["facts"]]
    assert stored["last_refresh"]["trigger"] == "telegram"
    assert panel.load_contact_snapshot(BUSINESS_ID, CHAT).refreshing is False
    assert "rf.c" in args_of(await act(panel))


@pytest.mark.asyncio
async def test_unload_stops_a_running_refresh_without_writing(plug, ds, panel, monkeypatch):
    await build(plug, ds)
    await seed(plug, CHAT, "anna_k", [(12, "in", "My new phone number is 555-0100.", 5),
                                      (13, "in", "And a new email too.", 4)], first_name="Anna")
    monkeypatch.setattr(ds, "BATCH_MAX_MESSAGES", 1)  # two batches: a second model call is possible
    before = read_dossier(ds, CHAT)
    release, entered = threading.Event(), threading.Event()
    llm = FakeLlm(output(change(section="shared_facts", text="The contact has a new phone number.",
                                sources=(("m12", "My new phone number is 555-0100"),))))

    def hold(_payload: dict[str, Any]) -> None:
        entered.set()
        release.wait(5)

    llm.on_call = hold
    ctx = FakeCtx(llm, with_unload=True)
    plug.register(ctx)
    ds.bind_llm(llm)
    try:
        assert (await act(panel, "rf.c")).notice == "Refresh started. Open the dossier again in a minute."
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
    await seed(plug, 992, "boris", ROWS, business_id=other, first_name="Boris")
    cache = plug._history_support._OWNER_CACHE
    assert cache[other] == str(OWNER_ID)  # the operator owns both connections
    assert args_of(await act(panel)) == ["m.c.0", "o.p", "ds"]
    assert panel.manages_global(OWNER_ID, BUSINESS_ID) is True

    for owner in ("4242", None):  # another account connected the bot, or its owner is not known yet
        if owner is None:
            del cache[other]
        else:
            cache[other] = owner
        overview = await act(panel)
        assert args_of(overview) == ["m.c.0"] and PERSONA_FACT not in overview.html
        for args in ("o.p", "m.p.0", "f.p.abcdef.1", "ea", "ed", "ds"):
            refused = await act(panel, args)
            assert refused.notice == ("Only the owner of every Business connection the bot knows about can open "
                                      "the persona or turn dossiers on and off.")
            assert PERSONA_FACT not in refused.html and args_of(refused) == ["o.c"]
        assert ds.load_config().enabled is True and panel.manages_global(OWNER_ID, BUSINESS_ID) is False
    assert panel.manages_global(None, BUSINESS_ID) is False and panel.manages_global(OWNER_ID, None) is False


@pytest.mark.asyncio
async def test_persona_and_switch_stay_closed_when_history_is_gone(plug, ds, panel, tmp_path):
    await build(plug, ds)
    history, cache = plug._history_support, plug._history_support._OWNER_CACHE
    other = "business-B"
    shutil.rmtree(history.history_root())  # the archive is removed, dossiers are kept
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

    assert panel.manages_global("4242", other) is False  # the persona and a contact dossier cite the operator
    assert args_of(await as_other(None)) == []
    for args in ("o.p", "ed", "ds"):
        refused = await as_other(args)
        assert refused.notice.startswith("Only the owner") and PERSONA_FACT not in refused.html
    assert ds.load_config().enabled is True

    chat_dir = ds.dossier_root() / "chats" / history._path_key(BUSINESS_ID)
    shutil.move(chat_dir, tmp_path / "chat")
    assert panel.manages_global("4242", other) is False  # only the persona cites the operator's chat
    shutil.move(tmp_path / "chat", chat_dir)
    shutil.rmtree(persona_dir(ds))
    assert panel.manages_global("4242", other) is False  # a contact dossier of a connection nobody vouched for
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
    (persona_dir(ds) / "dossier.json").write_text("{", encoding="utf-8")
    assert panel.manages_global("4242", other) is True  # an unreadable persona adds no connections
    shutil.rmtree(persona_dir(ds))
    panel._modes.default_store().bind_connection(BUSINESS_ID, OWNER_ID)
    assert panel.manages_global("4242", other) is False  # the operator's connection is bound in modes


@pytest.mark.asyncio
async def test_unreadable_persona_keeps_the_operator_in_control(plug, ds, panel):
    await build(plug, ds)
    (persona_dir(ds) / "dossier.json").write_text("{", encoding="utf-8")

    assert args_of(await act(panel)) == ["m.c.0", "o.p", "ds"]
    persona = await act(panel, "o.p")
    assert "The stored persona file can't be read." in persona.text and "dossier forget --persona" in persona.text
    assert (await act(panel, "ds")).notice == "Dossiers are off. Stored dossiers are kept."
    assert ds.load_config().enabled is False


@pytest.mark.asyncio
async def test_history_capture_off_is_explained(plug, ds, panel, monkeypatch):
    await build(plug, ds)
    monkeypatch.delenv("TELESEC_HISTORY_ENABLE")

    view = await act(panel)

    assert "History capture is off, so dossiers get no new messages" in view.text
    assert "Сбор истории выключен" in (await act(panel, lang="ru")).text


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
    assert [("Dossier", f"tb:s:{CHAT}:dossier")] in view.buttons


@pytest.mark.asyncio
async def test_panel_section_sends_a_rich_localized_dossier_with_panel_callbacks(plug, ds, panel):
    await build(plug, ds)
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
    assert "Обязательства" in html and COMMITMENT in html and NOTE in html  # the owner sees owner notes
    rows = keyboard_of(kwargs["reply_markup"])
    assert rows == [[("Управлять фактами", f"tb:s:{CHAT}:dossier/m.c.0")],
                    [("Моя персона", f"tb:s:{CHAT}:dossier/o.p"), ("Выключить", f"tb:s:{CHAT}:dossier/ds")],
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
    dossier = await build(plug, ds)
    panel.register_owner_panel_section(plug._owner_panel)
    bot = OwnerPanelBot()

    await open_section(plug, bot, f"tb:s:{CHAT}:dossier/pn.c.{prefix(dossier)}.1")

    html = bot.api[-1][1]["rich_message"]["html"]
    assert html.startswith("<p><b>f1 pinned.</b></p>")
    assert read_dossier(ds, CHAT)["facts"][0]["pinned"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "ru"])
async def test_section_back_buttons_differ_from_the_panel_back(plug, ds, panel, lang):
    dossier = await build(plug, ds)
    panel.register_owner_panel_section(plug._owner_panel)
    bot = OwnerPanelBot()
    for args in ("o.p", "m.c.0", f"f.c.{prefix(dossier)}.1"):
        await open_section(plug, bot, f"tb:s:{CHAT}:dossier/{args}", lang=lang)
    for edit in bot.edits:
        labels = [label for row in keyboard_of(edit["reply_markup"]) for label, _data in row]
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
    assert [("Dossier", f"tb:s:{CHAT}:dossier")] in keyboard_of(edit["reply_markup"])


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
async def test_owner_command_deletes_itself_and_posts_one_rich_dossier(plug, ds, panel):
    await build(plug, ds)
    bot = PanelBot()

    assert await run_command(panel, command_update(), bot) is True

    assert bot.deletes == [{"business_connection_id": BUSINESS_ID, "message_ids": [500]}]
    assert bot.sent == []
    (post,) = posts(bot)
    assert [endpoint for endpoint, _ in bot.api] == ["sendRichMessage"]
    assert set(post) == {"business_connection_id", "chat_id", "rich_message"}
    assert (post["business_connection_id"], post["chat_id"]) == (BUSINESS_ID, CHAT)
    assert set(post["rich_message"]) == {"html", "skip_entity_detection"}
    html = post["rich_message"]["html"]
    assert COMMITMENT in html and PREFERENCE in html and "Commitments" in html
    assert NOTE not in html and PERSONA_FACT not in html
    for owner_only in ("Processing is off", "hermes telesec", "Refresh", "not connected"):
        assert owner_only not in html


@pytest.mark.asyncio
async def test_owner_command_never_posts_a_corrected_note(plug, ds, panel):
    await build(plug, ds)
    corrected = "Never mention the 20% discount."
    ds.apply_owner_edit(("contact", catalog_entry(plug, CHAT)), "correct", fact_id="f3", text=corrected)
    assert read_dossier(ds, CHAT)["facts"][2]["owner"]["noted"] is True
    bot = PanelBot()

    assert await run_command(panel, command_update(), bot)

    html = posts(bot)[0]["rich_message"]["html"]
    assert COMMITMENT in html and corrected not in html and NOTE not in html and "<b>f3</b>" not in html


@pytest.mark.asyncio
async def test_command_language_follows_the_sender(plug, ds, panel):
    await build(plug, ds)
    bot = PanelBot()

    assert await run_command(panel, command_update(lang="ru"), bot)

    assert "Обязательства" in posts(bot)[0]["rich_message"]["html"]


@pytest.mark.asyncio
async def test_duplicate_delivery_posts_once(plug, ds, panel):
    await build(plug, ds)
    bot, seen = PanelBot(), set()

    assert await run_command(panel, command_update(), bot, seen)
    assert await run_command(panel, command_update(), bot, seen)

    assert len(posts(bot)) == 1 and len(bot.deletes) == 1
    assert seen == {("dossier-command", BUSINESS_ID, str(CHAT), "message:500")}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/dossier", "/dossiers", "hello"])
async def test_contact_command_is_ordinary_text(plug, ds, panel, text):
    await build(plug, ds)
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
    bot = PanelBot()
    update = command_update(edited=True) if kind == "edited" else echo_update()

    assert await run_command(panel, update, bot) is True

    assert bot.api == [] and bot.deletes == [] and bot.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "expected"), [
    ("unchecked", "could not be checked against the chat history (OSError)"),
    ("disabled", "Dossiers are off."),
    ("missing", "No dossier yet.\nIt will be built in the next processing cycle."),
    ("corrupt", f"forget --connection {BUSINESS_ID} --chat {CHAT}"),
    ("empty", "The dossier has no facts to show yet."),
])
async def test_nothing_to_show_tells_the_owner_privately(plug, ds, panel, monkeypatch, state, expected):
    if state == "missing":
        await seed(plug, CHAT, "anna_k", ROWS, first_name="Anna")
    else:
        dossier = await build(plug, ds)
        if state == "disabled":
            ds.set_enabled(False, via="cli")
        elif state == "unchecked":  # e.g. a torn history tail: quotes cannot be checked against the chat

            def broken(*_args: Any, **_kwargs: Any) -> Any:
                raise OSError(f"history read failed {COMMITMENT}")

            monkeypatch.setattr(ds, "views_for_dossier", broken)
        elif state == "corrupt":
            (dossier_dir(ds, CHAT) / "dossier.json").write_text("{", encoding="utf-8")
        else:
            for fact in dossier["facts"]:
                if fact["id"] != "f3":
                    ds.apply_owner_edit(("contact", catalog_entry(plug, CHAT)), "remove", fact_id=fact["id"])
    bot = PanelBot()

    assert await run_command(panel, command_update(), bot) is True

    assert posts(bot) == [] and bot.api == []
    assert bot.deletes == [{"business_connection_id": BUSINESS_ID, "message_ids": [500]}]
    (sent,) = bot.sent
    assert sent["chat_id"] == OWNER_ID and sent["parse_mode"] is None and "business_connection_id" not in sent
    assert sent["text"].startswith("Nothing was posted to the chat with @anna_k.") and expected in sent["text"]
    for secret in (NOTE, COMMITMENT, "I will send the contract"):
        assert secret not in sent["text"]


@pytest.mark.asyncio
async def test_reason_is_localized(dossier_history, panel):
    bot = PanelBot()

    assert await run_command(panel, command_update(lang="ru"), bot)

    assert "Досье выключены." in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_rejected_post_and_failed_delete_are_told_and_logged_by_type(plug, ds, panel, caplog):
    await build(plug, ds)
    bot = PanelBot()
    bot.api_error = BadRequest(f"Bad Request: can't parse {COMMITMENT}")
    bot.delete_error = RuntimeError(f"no rights for {NOTE}")
    bot.send_error = None

    with caplog.at_level(logging.DEBUG):
        assert await run_command(panel, command_update(), bot) is True

    assert len(posts(bot)) == 1
    (sent,) = bot.sent
    assert "Telegram did not accept the dossier message (BadRequest)." in sent["text"]
    assert "BadRequest" in caplog.text and "RuntimeError" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_failed_private_reason_is_logged_by_type(dossier_history, panel, caplog):
    bot = PanelBot()
    bot.send_error = RuntimeError("Forbidden: bot was blocked by the user Anna")

    with caplog.at_level(logging.DEBUG):
        assert await run_command(panel, command_update(), bot) is True

    assert "could not send the /dossier reason to the owner error=RuntimeError" in caplog.text
    assert "Anna" not in caplog.text


@pytest.mark.asyncio
async def test_business_handler_keeps_command_and_echo_out_of_history_and_core(plug, ds, panel, monkeypatch):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")  # even when other Business updates reach the agent
    await build(plug, ds)
    history_before = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    bot = PanelBot()
    handlers: list[tuple[Any, int]] = []
    plug._register_telegram_handlers(SimpleNamespace(add_handler=lambda h, group: handlers.append((h, group))),
                                     SimpleNamespace(platform="telegram"))
    (handler,) = [h for h, _group in handlers if type(h).__name__ == "BusinessHandler"]
    context = SimpleNamespace(bot=bot)

    for _ in range(2):  # the second is an in-process redelivery
        with pytest.raises(ApplicationHandlerStop):
            await handler.callback(command_update(message_id=600), context)
    with pytest.raises(ApplicationHandlerStop):
        await handler.callback(echo_update(901), context)
    with pytest.raises(ApplicationHandlerStop):
        await handler.callback(command_update("/dossier", edited=True, message_id=600), context)
    assert len(posts(bot)) == 1 and len(bot.deletes) == 1
    assert load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT) == history_before

    await handler.callback(command_update("/dossier", sender=CONTACT_ID, message_id=601), context)
    records = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    assert [(r["message_id"], r["direction"], r["text"]) for r in records[len(history_before):]] == [
        (601, "inbound", "/dossier")]


@pytest.mark.asyncio
async def test_deleting_the_command_or_the_post_stays_out_of_history(plug, ds, panel):
    await build(plug, ds)
    history = plug._history_support
    before = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    bot = PanelBot()  # sendRichMessage answers message_id 900
    handlers: list[tuple[Any, int]] = []
    plug._register_telegram_handlers(SimpleNamespace(add_handler=lambda h, group: handlers.append((h, group))),
                                     SimpleNamespace(platform="telegram"))
    (handler,) = [h for h, _group in handlers if type(h).__name__ == "BusinessHandler"]
    with pytest.raises(ApplicationHandlerStop):
        await handler.callback(command_update(message_id=610), SimpleNamespace(bot=bot))
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
    handlers: list[tuple[Any, int]] = []
    plug._register_telegram_handlers(SimpleNamespace(add_handler=lambda h, group: handlers.append((h, group))),
                                     SimpleNamespace(platform="telegram"))
    (handler,) = [h for h, _group in handlers if type(h).__name__ == "BusinessHandler"]
    with caplog.at_level(logging.WARNING):
        await handler.callback(command_update("hello", message_id=602), SimpleNamespace(bot=PanelBot()))

    assert "dossier command handling failed (RuntimeError)" in caplog.text and NOTE not in caplog.text
    assert [r["text"] for r in load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)] == ["hello"]


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
    handlers: list[tuple[Any, int]] = []
    plug._register_telegram_handlers(SimpleNamespace(add_handler=lambda h, group: handlers.append((h, group))),
                                     SimpleNamespace(platform="telegram"))
    (handler,) = [h for h, _group in handlers if type(h).__name__ == "BusinessHandler"]
    bot = PanelBot()
    context = SimpleNamespace(bot=bot)
    await handler.callback(command_update("See you at 5", message_id=710), context)

    with pytest.raises(ApplicationHandlerStop):  # claimed and never executed
        await handler.callback(command_update("/dossier", edited=True, message_id=710), context)
    with pytest.raises(ApplicationHandlerStop):  # an edit of a command history never stored
        await handler.callback(command_update("/dossier", edited=True, message_id=711), context)

    assert posts(bot) == [] and bot.deletes == []
    records = load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)
    assert [(r["event_type"], r["message_id"], r["text"]) for r in records] == [
        ("message.created", 710, "See you at 5"), ("message.edited", 710, "/dossier")]


@pytest.mark.asyncio
async def test_owner_edit_uses_the_resolved_owner_when_the_lookup_fails(plug, ds, panel, monkeypatch):
    monkeypatch.setenv("TELESEC_AGENT_ENABLE", "1")
    handlers: list[tuple[Any, int]] = []
    plug._register_telegram_handlers(SimpleNamespace(add_handler=lambda h, group: handlers.append((h, group))),
                                     SimpleNamespace(platform="telegram"))
    (handler,) = [h for h, _group in handlers if type(h).__name__ == "BusinessHandler"]
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
        dossier = await build(plug, ds)
        p = prefix(dossier)
        for args in (None, "m.c.0", f"f.c.{p}.1", f"pn.c.{p}.1", f"ra.c.{p}.2", f"rd.c.{p}.2", "o.p", "ds", "ed"):
            await act(panel, args)
        await run_command(panel, command_update(), PanelBot())

    assert "dossier panel pin target=contact" in caplog.text and "dossier posted chat_ref=" in caplog.text
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
    await app.process_update(echo)
    await app.process_update(plain)

    assert core == [plain]
    endpoints = [endpoint for endpoint, _ in request.calls]
    assert endpoints.count("sendRichMessage") == 1 and "deleteBusinessMessages" in endpoints
    params = dict(request.calls)["sendRichMessage"]
    assert set(params) == {"business_connection_id", "chat_id", "rich_message"}
    assert (params["business_connection_id"], params["chat_id"]) == (BUSINESS_ID, str(CHAT))
    rich = json.loads(params["rich_message"])
    assert rich["skip_entity_detection"] is True and COMMITMENT in rich["html"] and NOTE not in rich["html"]
    assert json.loads(dict(request.calls)["deleteBusinessMessages"]["message_ids"]) == [500]
    texts = [r["text"] for r in load_records(plug, business_id=BUSINESS_ID, chat_id=CHAT)[len(history_before):]]
    assert texts == ["See you Friday"]
