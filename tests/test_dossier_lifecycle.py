"""Dossier lifecycle tests: races, the kept v1 rules, failures, writer results, the owner note, the message
check, fact hints, accounting, the persona, the forget contract and state hygiene.

Every time is relative to ``NOW`` and every cycle gets an explicit ``now``, so the tests are deterministic.
``FakeLlm`` stands in for the host facade. The message check (Jev) and the fact hints (Clairic) run through
the real ``model_client`` with a ``FakeOpener`` and a fake secret scope, so no test reaches the network.
What a failure must never write, the persona erase and the logging rules are in ``test_dossier_guarantees.py``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import re
import threading
import urllib.error
from collections.abc import Callable
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
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
    dossier_dir,
    edit,
    load_records,
    maintain,
    make_business_text_update,
    persona_dir,
    quote_first,
    raw,
    read_dossier,
    read_persona,
    read_state,
    seed,
    snapshot_history_tree,
    snapshot_tree,
)
from model_fakes import FakeOpener, fake_secret_scope

ALICE = 991
BOB = 992
CAROL = 993
ALICE_ROWS = [
    (10, "in", "Hi, I prefer email for invoices please", 60),
    (11, "out", "Noted, I will send the invoice by Friday", 55),
    (12, "in", "Great, see you at the harbour office", 50),
]
SECOND = timedelta(seconds=1)
MARK = chr(0x1F399) + chr(0xFE0F)  # the transcript marker as the plugin posts it
GATEWAY_KEY = {"AI_GATEWAY_API_KEY": "test-gateway-key"}
CF_KEYS = {"CLOUDFLARE_ACCOUNT_ID": "0123456789abcdef0123456789abcdef", "CLOUDFLARE_API_TOKEN": "test-cf-token"}
KEYS = {**GATEWAY_KEY, **CF_KEYS}
MONTH = f"{NOW:%Y-%m}"
_UPDATE_IDS = iter(range(900_000, 1_000_000))


@pytest.fixture(autouse=True)
def _isolated_env(dossier_env: Callable[..., None]) -> Callable[..., None]:
    # Every test starts without dossier, cleanup or model key env vars.
    return dossier_env


@pytest.fixture
def plug(dossier_plugin: ModuleType, dossiers: ModuleType) -> ModuleType:
    """The plugin module with history and dossiers enabled (same load as ``dossiers``)."""
    return dossier_plugin


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def contact_calls(fake: FakeLlm) -> list[dict[str, Any]]:
    return fake.by_purpose("telesec_dossier")


def persona_calls(fake: FakeLlm) -> list[dict[str, Any]]:
    return fake.by_purpose("telesec_persona")


def contact_payloads(fake: FakeLlm) -> list[dict[str, Any]]:
    return [call["user"] for call in contact_calls(fake)]


def refs(payload: dict[str, Any]) -> list[str]:
    return [m["ref"] for m in payload["messages"]]


def quoted(text: str) -> str:
    """The document ``quote_first`` writes for a batch whose first contact message is ``text``."""
    return f'**The contact.**\n\n## Notes\n- The contact said "{text}".'


def local(minutes: Any) -> str:
    """The writer's ``at`` (UTC in tests) for a time ``minutes`` before NOW."""
    return f"{ago(minutes):%Y-%m-%d %H:%M}"


def key_of(ds: ModuleType, chat_id: int) -> str:
    return ds.chat_key(BUSINESS_ID, chat_id)


def doc_bytes(ds: ModuleType, chat_id: int) -> bytes:
    return (dossier_dir(ds, chat_id) / "document.json").read_bytes()


async def seed_alice(plugin: ModuleType, rows: Any = ALICE_ROWS, **kw: Any) -> dict[str, Any]:
    await seed(plugin, ALICE, "alice", rows, **kw)
    return catalog_entry(plugin, ALICE)


async def seed_owner_chat(plugin: ModuleType, chat_id: int, tag: str, *, owner: int = 5) -> dict[str, Any]:
    """One contact question two hours ago, then ``owner`` owner messages a minute apart."""
    rows = [(1, "in", f"{tag} question from the contact", 120)]
    rows += [(i, "out", f"{tag} owner message {i}", 120 - i) for i in range(2, owner + 2)]
    await seed(plugin, chat_id, f"user{chat_id}", rows)
    return catalog_entry(plugin, chat_id)


async def capture(plugin: ModuleType, chat_id: int, rows: list[tuple[int, str, str, datetime, int | None]]) -> None:
    """Capture text messages at exact times: ``(message_id, who, text, at, reply_to)``; who is in, out or bot."""
    senders = {
        "in": {"from_user_id": CONTACT_ID},
        "out": {"from_user_id": OWNER_ID},
        "bot": {"from_user_id": OWNER_ID, "sender_business_bot_id": BOT_ID},
    }
    history = plugin._history_support
    for message_id, who, text, at, reply_to in rows:
        update = make_business_text_update(
            text=text, business_id=BUSINESS_ID, chat_id=chat_id, chat_username=f"user{chat_id}",
            message_id=message_id, update_id=next(_UPDATE_IDS), date=at, reply_to_message_id=reply_to,
            **senders[who])
        assert await history.observe_ptb_update(update, bot=FakeBot(owner_id=OWNER_ID), now=at)


def pause(plugin: ModuleType, chat_id: int) -> None:
    """Set the chat to "No dossier", as its panel does."""
    plugin._chat_settings.default_store().set_flag(OWNER_ID, chat_id, "dossier", False, via="cli")


def jev_reply(update: Any, promise: Any, *, tokens: int = 1200, cost: str = "0.00005") -> tuple[int, bytes]:
    body = {"answers": {"update": {"probability": update}, "promise": {"probability": promise}},
            "usage": {"inputTokens": tokens}, "providerMetadata": {"gateway": {"cost": cost}}}
    return 200, json.dumps(body).encode()


def answer(value: Any, *, confidence: float = 0.98, p_null: float = 0.01) -> dict[str, Any]:
    return {"type": "list", "value": value, "guess": value, "confidence": confidence, "p_null": p_null}


DEFAULT_ANSWERS = {
    "dates": answer(["2026-10-28"]),
    "amounts": answer([900.0]),
    "contacts": answer(["+7 912 345-67-13"], confidence=0.5),  # not sure enough: dropped
}
DEFAULT_HINTS = [{"kind": "date", "value": "2026-10-28"}, {"kind": "amount", "value": "900"}]


def clairic_reply(answers: dict[str, Any] | None = None, *, tokens: int = 2000) -> tuple[int, bytes]:
    body = {"result": {"answers": DEFAULT_ANSWERS if answers is None else answers,
                       "usage": {"input_tokens": tokens}}, "success": True}
    return 200, json.dumps(body).encode()


def use_gate(monkeypatch: pytest.MonkeyPatch, ds: ModuleType, *responses: Any) -> FakeOpener:
    """Turn the message check on and answer it from ``responses``; the test supplies the key.

    An unexpected check finds no queued response and fails the refresh, so a bypass shows up as a failure.
    """
    ds.set_gate("auto", via="cli")
    opener = FakeOpener(responses)
    monkeypatch.setattr(ds._writer, "gate_check", functools.partial(ds._writer.gate_check, opener=opener))
    return opener


def use_hints(monkeypatch: pytest.MonkeyPatch, ds: ModuleType, *responses: Any) -> FakeOpener:
    """Turn fact hints on and answer them from ``responses``; the test supplies the credentials."""
    ds.set_clairic("on", via="cli")
    opener = FakeOpener(responses)
    monkeypatch.setattr(ds._writer, "clairic_hints", functools.partial(ds._writer.clairic_hints, opener=opener))
    return opener


class PluginLlmTrustError(PermissionError):
    """Same name and base as the host's trust-gate error."""


# ---------------------------------------------------------------------------
# Races
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_during_an_update_call_discards_the_answer(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    assert cycle(ds, FakeLlm()).refreshed == 1
    old_id = read_dossier(ds, ALICE)["id"]
    generation = ds.generation()
    await seed_alice(plug, [(13, "in", "Phone works better for me now", 30)])

    fake = FakeLlm()
    fake.on_call = lambda _payload: ds.forget(("contact", entry))
    report = cycle(ds, fake)
    assert (len(fake.calls), report.calls, report.refreshed, report.waiting) == (1, 1, 0, 1)  # discarded: waiting
    assert not dossier_dir(ds, ALICE).exists()
    assert ds.generation() == generation + 1

    rebuild = FakeLlm()
    assert cycle(ds, rebuild, now=ago(-5)).refreshed == 1
    (payload,) = contact_payloads(rebuild)
    assert (payload["mode"], refs(payload)) == ("first", ["m10", "m11", "m12", "m13"])
    assert read_dossier(ds, ALICE)["id"] != old_id


@pytest.mark.asyncio
async def test_forget_all_during_a_first_pass_call_discards_it_and_sends_no_persona(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA", owner=10)  # the persona is due as well
    fake = FakeLlm()
    fake.on_call = lambda _payload: ds.forget(("all", None))  # also deletes state.json
    report = cycle(ds, fake)
    assert len(fake.calls) == 1  # the persona step sends nothing after the forget
    assert (report.refreshed, report.waiting, report.persona) == (0, 1, "discarded")
    assert read_dossier(ds, ALICE) is None and read_persona(ds) is None

    fake.on_call = None
    report = cycle(ds, fake, now=ago(-5))  # the next cycle rebuilds both
    assert (report.refreshed, report.persona) == (1, "refreshed")


@pytest.mark.asyncio
async def test_forget_of_a_chat_during_the_persona_call_discards_the_persona(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    alice = await seed_owner_chat(plug, ALICE, "ALPHA")
    await seed_owner_chat(plug, BOB, "BRAVO")  # 10 owner messages in all: the persona is due
    fake = FakeLlm()

    def forget_alice(payload: dict[str, Any]) -> None:
        if "mode" not in payload:  # the persona call
            ds.forget(("contact", alice))

    fake.on_call = forget_alice
    report = cycle(ds, fake)
    assert (report.refreshed, report.persona, len(persona_calls(fake))) == (2, "discarded", 1)
    assert read_persona(ds) is None and read_dossier(ds, ALICE) is None

    fake.on_call = None
    report = cycle(ds, fake, now=ago(-5))  # the chat's history is still there, so both come back
    assert (report.refreshed, report.persona) == (1, "refreshed")


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["dossiers", "history", "chat"])
async def test_switching_off_while_waiting_for_the_rate_limit_sends_nothing(
    plug: ModuleType, dossiers: ModuleType, switch: str
) -> None:
    ds = dossiers
    await seed_alice(plug)

    class SwitchingLimiter(ds.RateLimiter):
        def acquire(self) -> bool:
            if switch == "dossiers":
                ds.set_enabled(False, via="telegram")
            elif switch == "history":
                test_history.set_switch(plug, "history", False)
            else:
                pause(plug, ALICE)
            return super().acquire()

    fake = FakeLlm()
    report = cycle(ds, fake, limiter=SwitchingLimiter(20))
    assert (fake.calls, report.calls) == ([], 0)
    assert report.outcome == ("completed" if switch == "chat" else "stopped")
    assert read_dossier(ds, ALICE) is None


@pytest.mark.asyncio
async def test_stop_during_a_model_call_writes_nothing(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA", owner=10)
    stop = threading.Event()
    fake = FakeLlm()
    fake.on_call = lambda _payload: stop.set()  # the gateway shuts down while the model answers
    report = cycle(ds, fake, stop=stop)
    assert (len(fake.calls), report.outcome, report.persona) == (1, "stopped", "skipped")
    assert read_dossier(ds, ALICE) is None and read_persona(ds) is None

    stop = threading.Event()
    fake.on_call = lambda _payload: stop.set()
    report = cycle(ds, fake, stop=stop, target=("persona", None))
    assert (len(persona_calls(fake)), report.persona) == (1, "stopped")
    assert read_persona(ds) is None


@pytest.mark.asyncio
async def test_a_chat_switched_off_during_the_call_keeps_its_document(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm()).refreshed == 1
    before = doc_bytes(ds, ALICE)
    await seed_alice(plug, [(13, "in", "One more thing about the invoice", 30)])
    fake = FakeLlm()
    fake.on_call = lambda _payload: pause(plug, ALICE)
    report = cycle(ds, fake)
    assert (len(fake.calls), report.refreshed, report.failed) == (1, 0, 0)
    assert doc_bytes(ds, ALICE) == before


@pytest.mark.asyncio
async def test_a_chat_switched_off_during_the_persona_call_keeps_it_out_of_the_persona(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA")
    await seed_owner_chat(plug, BOB, "BRAVO")
    fake = FakeLlm()

    def pause_alice(payload: dict[str, Any]) -> None:
        if "mode" not in payload:  # the persona call
            pause(plug, ALICE)

    fake.on_call = pause_alice
    report = cycle(ds, fake, target=("persona", None))
    assert (report.persona, len(persona_calls(fake))) == ("paused", 1)
    assert read_persona(ds) is None


@pytest.mark.asyncio
async def test_switching_off_during_a_writer_call_skips_the_shorten_call(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_alice(plug)
    long_text = "\n".join(f"- Fact number {i}: " + "detail " * 10 for i in range(60))
    fake = FakeLlm(doc(f"**The contact.**\n\n## Notes\n{long_text}"))
    fake.on_call = lambda _payload: ds.set_enabled(False, via="telegram")
    report = cycle(ds, fake)
    assert ([c["purpose"] for c in fake.calls], report.calls, report.outcome) == (["telesec_dossier"], 1, "stopped")
    assert read_dossier(ds, ALICE) is None
    assert read_state(ds)["usage"][MONTH]["writer"]["calls"] == 1  # the call that went out is still counted


async def seed_two_checked_contacts(plug: ModuleType, ds: ModuleType) -> None:
    """ALICE and BOB with a document each, then one short new message each: a message check applies to both."""
    await seed_alice(plug)
    await seed(plug, BOB, "bob", [(20, "in", "Hi, do you have the catalog?", 60), (21, "out", "Sending it today", 55)])
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 2
    await seed_alice(plug, [(13, "in", "Let's meet on 28 October, the price is 900", 30)])
    await seed(plug, BOB, "bob", [(22, "in", "The deposit is 300, due on 1 November", 30)])


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["dossiers", "history"])
async def test_switching_off_during_a_message_check_sends_no_further_check(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    ds = dossiers
    await seed_two_checked_contacts(plug, ds)
    opener = use_gate(monkeypatch, ds, jev_reply(0.1, 0.1), jev_reply(0.1, 0.1))
    check = ds._writer.gate_check

    def check_then_switch_off(*args: Any, **kwargs: Any) -> Any:
        try:
            return check(*args, **kwargs)
        finally:
            if switch == "dossiers":
                ds.set_enabled(False, via="telegram")
            else:
                test_history.set_switch(plug, "history", False)

    monkeypatch.setattr(ds._writer, "gate_check", check_then_switch_off)
    fake = FakeLlm()
    with fake_secret_scope(GATEWAY_KEY):
        report = cycle(ds, fake)
    assert (len(opener.calls), fake.calls, report.deferred, report.outcome) == (1, [], 1, "stopped")


@pytest.mark.asyncio
@pytest.mark.parametrize("option", ["gate", "clairic"])
async def test_turning_a_check_off_during_a_cycle_stops_its_requests(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    ds = dossiers
    await seed_two_checked_contacts(plug, ds)
    if option == "gate":
        opener, secrets = use_gate(monkeypatch, ds, jev_reply(0.9, 0.9), jev_reply(0.9, 0.9)), GATEWAY_KEY
    else:
        opener, secrets = use_hints(monkeypatch, ds, clairic_reply(), clairic_reply()), CF_KEYS
    fake = FakeLlm()
    fake.on_call = lambda _payload: getattr(ds, f"set_{option}")("off", via="cli")
    with fake_secret_scope(secrets):
        report = cycle(ds, fake)
    assert (report.refreshed, report.calls, len(opener.calls)) == (2, 2, 1)  # the second contact has no request
    assert ["hints" in payload for payload in contact_payloads(fake)] == [option == "clairic", False]


@pytest.mark.asyncio
async def test_forget_never_touches_history_or_the_switch(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    alice = await seed_owner_chat(plug, ALICE, "ALPHA")
    await seed_owner_chat(plug, BOB, "BRAVO")
    report = cycle(ds, FakeLlm())
    assert (report.refreshed, report.persona) == (2, "refreshed")
    history_before = snapshot_history_tree(plug)
    switch = ds.settings_path().read_bytes()

    assert ds.forget(("contact", alice))["existed"] is True
    assert ds.forget(("persona", None))["existed"] is True
    assert ds.forget(("all", None))["existed"] is True
    assert snapshot_history_tree(plug) == history_before
    assert ds.settings_path().read_bytes() == switch and ds.load_config().enabled is True
    root = ds.dossier_root()
    assert not (root / "chats").exists() and not persona_dir(ds).exists() and not ds.state_path().exists()
    assert (root / ".lock").exists() and (root / ".cycle.lock").exists()

    ds.set_enabled(False, via="telegram")
    switch = ds.settings_path().read_bytes()
    assert ds.forget(("all", None))["existed"] is False
    assert ds.settings_path().read_bytes() == switch and ds.load_config().enabled is False


# ---------------------------------------------------------------------------
# Kept v1 rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_same_text_edit_or_a_classification_stamp_makes_no_call(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    before = snapshot_tree(dossier_dir(ds, ALICE))
    fake = FakeLlm()

    await edit(plug, ALICE, 11, "Noted, I will send the invoice by Friday", 30)  # the same text again
    report = cycle(ds, fake, now=ago(15))
    assert (report.calls, report.unchanged) == (0, 1)
    assert snapshot_tree(dossier_dir(ds, ALICE)) == before

    await delete(plug, ALICE, 11, when=14)
    report = cycle(ds, fake, now=ago(3))
    assert report.calls == 1 and refs(contact_payloads(fake)[0]) == ["m11"]
    after = snapshot_tree(dossier_dir(ds, ALICE))

    maintain(plug, 2)
    classified = [r for r in load_records(plug, chat_id=ALICE) if r["event_type"] == "deletion.classified"]
    assert [r["classification"] for r in classified] == ["unexplained"]
    report = cycle(ds, fake, now=ago(-15))
    assert (report.calls, report.unchanged) == (0, 1)  # the stamp changes no message
    assert snapshot_tree(dossier_dir(ds, ALICE)) == after


@pytest.mark.asyncio
async def test_the_rolling_floor_keeps_aged_out_messages_unsent(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None]
) -> None:
    """Seen messages that age past the rolling floor are not resent, even inside the fold margin."""
    ds = dossiers
    dossier_env(BACKFILL_DAYS=30)
    raw(plug, ALICE, [(1, "in", "old question", timedelta(days=25)),
                      (2, "out", "old answer", timedelta(days=25, minutes=-1))], username="alice")
    fake = FakeLlm()
    cycle(ds, fake)
    assert [refs(p) for p in contact_payloads(fake)] == [["m1", "m2"]]

    t1 = NOW + timedelta(days=6)  # the floor is NOW - 24 d: 1 and 2 leave `seen` but stay in the fold margin
    raw(plug, ALICE, [(3, "in", "new question", t1 - timedelta(hours=2)),
                      (4, "out", "new answer", t1 - timedelta(hours=2) + timedelta(minutes=1))], username="alice")
    fake = FakeLlm()
    cycle(ds, fake, now=t1)
    assert [refs(p) for p in contact_payloads(fake)] == [["m3", "m4"]]
    assert sorted(read_dossier(ds, ALICE)["seen"]) == ["3", "4"]

    t2 = t1 + timedelta(hours=3)
    raw(plug, ALICE, [(5, "in", "newer question", t2 - timedelta(hours=1))], username="alice")
    fake = FakeLlm()
    cycle(ds, fake, now=t2)
    assert [refs(p) for p in contact_payloads(fake)] == [["m5"]]
    assert read_dossier(ds, ALICE)["messages"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("middle_days", [30, 10])  # a constant window, or one lowered before the raise
async def test_a_raised_backfill_window_never_resends_aged_out_messages(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], middle_days: int
) -> None:
    """The floor never moves back, so raising BACKFILL_DAYS does not re-admit aged-out messages."""
    ds = dossiers
    dossier_env(BACKFILL_DAYS=30)
    raw(plug, ALICE, [(1, "in", "old question", timedelta(days=25)),
                      (2, "out", "old answer", timedelta(days=25, minutes=-1))], username="alice")
    cycle(ds, FakeLlm())

    dossier_env(BACKFILL_DAYS=middle_days)
    t1 = NOW + timedelta(days=6)
    raw(plug, ALICE, [(3, "in", "new question", t1 - timedelta(hours=2)),
                      (4, "out", "new answer", t1 - timedelta(hours=2) + timedelta(minutes=1))], username="alice")
    cycle(ds, FakeLlm(), now=t1)
    assert sorted(read_dossier(ds, ALICE)["seen"]) == ["3", "4"]

    dossier_env(BACKFILL_DAYS=90)
    t2 = t1 + timedelta(hours=3)
    raw(plug, ALICE, [(5, "in", "newer question", t2 - timedelta(hours=1))], username="alice")
    fake = FakeLlm()
    cycle(ds, fake, now=t2)
    assert [refs(p) for p in contact_payloads(fake)] == [["m5"]]
    assert read_dossier(ds, ALICE)["messages"] == 5


@pytest.mark.asyncio
async def test_the_first_pass_cutoff_uses_the_message_time(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None]
) -> None:
    """The fold reads by observed time; the first pass still drops messages older than the window."""
    ds = dossiers
    dossier_env(BACKFILL_DAYS=30)
    fresh = [(2, "in", "fresh question", timedelta(days=1)), (3, "out", "fresh answer", timedelta(days=1, minutes=-1))]
    # Alice: 31 days old, inside the 2-day fold margin. Bob: observed 5 days ago, written 65 days ago.
    raw(plug, ALICE, [(1, "in", "too old", timedelta(days=31)), *fresh], username="alice")
    raw(plug, BOB, [(1, "in", "late-observed old", timedelta(days=5))], username="bob", message_lag=timedelta(days=60))
    raw(plug, BOB, fresh, username="bob")
    raw(plug, CAROL, fresh, username="carol")
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 3
    assert [refs(p) for p in contact_payloads(fake)] == [["m2", "m3"]] * 3
    older = {chat: read_dossier(ds, chat)["older_history_not_sent"] for chat in (ALICE, BOB, CAROL)}
    assert older == {ALICE: True, BOB: True, CAROL: False}


@pytest.mark.asyncio
async def test_the_quiet_rule_applies_only_when_something_is_pending(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    fake = FakeLlm()

    await edit(plug, ALICE, 11, "Noted, I will send the invoice by Friday", 3)  # activity, but nothing to send
    report = cycle(ds, fake)
    assert (report.unchanged, report.waiting, report.calls) == (1, 0, 0)

    await seed_alice(plug, [(13, "in", "Are you around this week?", 2)])
    report = cycle(ds, fake)
    assert (report.waiting, report.calls) == (1, 0)
    assert cycle(ds, fake, now=ago(-9)).refreshed == 1  # 11 quiet minutes


@pytest.mark.asyncio
async def test_edits_and_deletions_are_activity_but_a_classification_stamp_is_not(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed_alice(plug, [(10, "in", "Hi, I prefer email for invoices please", 90),
                            (11, "out", "Noted, I will send the invoice by Friday", 85),
                            (12, "in", "Great, see you at the harbour office", 80)])
    assert cycle(ds, FakeLlm(), now=ago(60)).refreshed == 1

    await delete(plug, ALICE, 10, when=31)
    await seed_alice(plug, [(13, "in", "Are you around this week?", 30)])
    fake = FakeLlm()
    report = cycle(ds, fake, now=ago(25))  # 5 min after the last message
    assert (report.waiting, report.calls) == (1, 0)  # the deletion waits with the new message
    report = cycle(ds, fake, now=ago(19))
    assert (report.refreshed, report.calls) == (1, 1)
    assert [(m["ref"], m.get("deleted", False)) for m in contact_payloads(fake)[0]["messages"]] == [
        ("m10", True), ("m13", False)]

    # An edit is activity: quiet counts from when the edit was observed, not from the message time.
    await edit(plug, ALICE, 12, "Great, see you at the harbour office at noon", 10)
    assert cycle(ds, fake, now=ago(7)).waiting == 1  # 3 min after the edit of an 80 min old message
    assert cycle(ds, fake, now=ago(-1)).refreshed == 1

    # A deletion is activity; a classification stamp is not.
    await seed_alice(plug, [(14, "in", "Ping", -2)])
    await delete(plug, ALICE, 11, when=-8)
    assert cycle(ds, fake, now=ago(-15)).waiting == 1  # 13 min after 14, 7 min after the deletion
    maintain(plug, -14)  # the deletion.classified stamp is now the newest record
    assert cycle(ds, fake, now=ago(-19)).refreshed == 1  # 11 min after the deletion, 5 after the stamp


@pytest.mark.asyncio
async def test_a_trust_denial_aborts_the_cycle_but_a_file_permission_error_does_not(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await seed_alice(plug)
    await seed(plug, BOB, "bob", [(20, "in", "BRAVO question", 50), (21, "out", "BRAVO answer", 45)])

    denied = FakeLlm(PluginLlmTrustError("plugins.entries.telesec.llm does not allow provider gemini"))
    report = cycle(ds, denied)
    assert (report.outcome, report.calls, len(denied.calls), report.persona) == ("aborted_trust", 1, 1, "skipped")
    assert "does not allow provider" in report.error  # for the CLI only
    assert read_dossier(ds, ALICE) is None and read_dossier(ds, BOB) is None
    state = ds.state_path().read_text(encoding="utf-8")
    assert json.loads(state)["last_cycle"]["outcome"] == "aborted_trust"
    assert json.loads(state)["failures"] == {} and "does not allow" not in state  # a denial is no target failure

    alice_dir = dossier_dir(ds, ALICE)
    real_read = ds._doc.read_doc

    def read_doc(path: Any) -> Any:
        if path == alice_dir:
            raise PermissionError("SECRET-FS")
        return real_read(path)

    monkeypatch.setattr(ds._doc, "read_doc", read_doc)
    report = cycle(ds, FakeLlm(), now=ago(-5))
    assert (report.outcome, report.failed, report.refreshed) == ("completed", 1, 1)
    assert report.failures == [(f"{BUSINESS_ID}:{ALICE}", "PermissionError")]
    assert read_dossier(ds, BOB) is not None and read_dossier(ds, ALICE) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["legacy_env", "not_registered"])
async def test_an_explicit_route_needs_trust_config_and_a_denial_aborts_the_cycle(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], route: str
) -> None:
    ds = dossiers
    if route == "legacy_env":
        dossier_env(PROVIDER="gemini")
        expected = ("gemini", "gemini-3.8-flash")
    else:
        ds.set_task_registered(False)
        expected = ("ai-gateway", "google/gemini-3.8-flash")
    await seed_owner_chat(plug, ALICE, "ALPHA", owner=10)

    def host(payload: dict[str, Any], kwargs: dict[str, Any]) -> str:
        # Like Hermes: an explicit provider needs plugins.entries.telesec.llm; a registered task does not.
        if "task" not in kwargs:
            raise PluginLlmTrustError(f"plugins.entries.telesec.llm does not allow provider {kwargs['provider']}")
        return quote_first(payload, kwargs)

    fake = FakeLlm(host)
    report = cycle(ds, fake)
    assert (report.outcome, report.calls, report.persona) == ("aborted_trust", 1, "skipped")
    assert "plugins.entries.telesec.llm" in report.error
    (call,) = fake.calls
    assert (call["provider"], call["model"]) == expected and "task" not in call
    assert read_dossier(ds, ALICE) is None and read_persona(ds) is None
    assert read_state(ds)["last_cycle"]["outcome"] == "aborted_trust"

    trusted = FakeLlm()  # the operator added the trust config
    report = cycle(ds, trusted, now=ago(-5))
    assert (report.outcome, report.refreshed, report.persona) == ("completed", 1, "refreshed")
    assert {(c["provider"], c["model"]) for c in trusted.calls} == {expected}
    assert read_dossier(ds, ALICE)["last_call"]["provider"] == expected[0]


# ---------------------------------------------------------------------------
# Failures and backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_contact_is_retried_next_cycle_and_the_others_continue(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed_alice(plug, [(10, "in", "ALPHA hello there", 60), (11, "out", "ALPHA hi back", 55)])
    await seed(plug, BOB, "bob", [(20, "in", "BRAVO question", 50), (21, "out", "BRAVO answer", 45)])

    def flaky(payload: dict[str, Any], kwargs: dict[str, Any]) -> str:
        if any("ALPHA" in m.get("text", "") for m in payload["messages"]):
            raise TimeoutError("SECRET-TIMEOUT")
        return quote_first(payload, kwargs)

    report = cycle(ds, FakeLlm(flaky, flaky))
    assert (report.outcome, report.failed, report.refreshed) == ("completed", 1, 1)
    assert report.failures == [(f"{BUSINESS_ID}:{ALICE}", "TimeoutError")]
    failure = read_state(ds)["failures"][key_of(ds, ALICE)]
    assert failure == {"at": ds._iso(NOW), "error": "TimeoutError", "count": 1}
    assert "SECRET" not in ds.state_path().read_text(encoding="utf-8")
    assert read_dossier(ds, ALICE) is None and read_dossier(ds, BOB) is not None

    report = cycle(ds, FakeLlm(), now=ago(-5))  # before the third failure no chat change is needed
    assert (report.failed, report.refreshed, report.unchanged) == (0, 1, 1)
    assert read_dossier(ds, ALICE)["document"] == quoted("ALPHA hello there")
    assert read_state(ds)["failures"] == {}


def test_the_backoff_doubles_from_the_third_failure_up_to_a_day(dossiers: ModuleType) -> None:
    ds = dossiers
    assert (ds.BACKOFF_AFTER, ds.BACKOFF_MAX_HOURS) == (3, 24)
    at = ds._iso(NOW)
    for count, hours in ((3, 1), (4, 2), (5, 4), (6, 8), (7, 16), (8, 24), (20, 24)):
        record = {"at": at, "error": "TimeoutError", "count": count}
        assert ds._in_backoff(record, NOW + timedelta(hours=hours) - SECOND), count
        assert not ds._in_backoff(record, NOW + timedelta(hours=hours)), count
    for record in ({"at": at, "error": "TimeoutError", "count": 2}, {"at": at, "count": "5"}, {"count": 5}, None, []):
        assert not ds._in_backoff(record, NOW), record


@pytest.mark.asyncio
async def test_three_failures_back_off_and_set_the_stat_gate(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    key = key_of(ds, ALICE)
    for minute in (0, 1, 2):
        report = cycle(ds, FakeLlm(TimeoutError), now=ago(-minute))
        assert (report.failed, report.calls) == (1, 1), minute
        assert (key in read_state(ds)["chats"]) is (minute == 2)  # only the third failure sets the stat gate
    third = ago(-2)
    assert read_state(ds)["failures"][key] == {"at": ds._iso(third), "error": "TimeoutError", "count": 3}
    gate = read_state(ds)["chats"][key]
    assert (gate["dossier_id"], gate["recheck_at"]) == (None, None)

    report = cycle(ds, FakeLlm(), now=third + timedelta(minutes=59))
    assert (report.backoff, report.calls) == (1, 0)
    report = cycle(ds, FakeLlm(), now=third + timedelta(minutes=61))
    assert (report.unchanged, report.calls) == (1, 0)  # after the backoff a retry needs a chat change

    await seed_alice(plug, [(13, "in", "Still there?", -70)])
    report = cycle(ds, FakeLlm(TimeoutError), now=ago(-85))
    assert (report.failed, report.calls) == (1, 1)
    assert read_state(ds)["failures"][key]["count"] == 4
    assert cycle(ds, FakeLlm(), now=ago(-85) + timedelta(minutes=119)).backoff == 1  # the fourth waits 2 h

    forced = cycle(ds, FakeLlm(), now=ago(-85) + timedelta(minutes=119), target=("contact", entry))
    assert (forced.refreshed, forced.calls) == (1, 1)  # a forced run skips the backoff
    assert read_state(ds)["failures"] == {}


# ---------------------------------------------------------------------------
# Writer results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_change_advances_seen_and_keeps_the_revision(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    before = read_dossier(ds, ALICE)
    await seed_alice(plug, [(13, "in", "Thanks, talk soon", 30)])

    fake = FakeLlm(doc("NO_CHANGE"))
    report = cycle(ds, fake)
    assert (report.checked, report.refreshed, report.calls) == (1, 0, 1)
    assert contact_payloads(fake)[0]["mode"] == "update"
    after = read_dossier(ds, ALICE)
    for name in ("id", "document", "previous", "revision", "updated_at"):
        assert after[name] == before[name], name
    assert (after["checked_at"], after["messages"]) == (ds._iso(NOW), before["messages"] + 1)
    assert sorted(after["seen"], key=int) == ["10", "11", "12", "13"]
    assert after["last_call"]["at"] == ds._iso(NOW)

    report = cycle(ds, FakeLlm(), now=ago(-5))
    assert (report.unchanged, report.calls) == (1, 0)  # nothing is sent twice


@pytest.mark.asyncio
async def test_empty_clears_the_document_and_keeps_the_old_text_as_previous(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    before = read_dossier(ds, ALICE)
    await seed_alice(plug, [(13, "in", "Please disregard all of that", 30)])

    report = cycle(ds, FakeLlm(doc("EMPTY")))
    assert (report.refreshed, report.calls) == (1, 1)
    after = read_dossier(ds, ALICE)
    assert (after["document"], after["previous"]) == ("", before["document"])
    assert (after["revision"], after["updated_at"]) == (before["revision"] + 1, ds._iso(NOW))
    assert "13" in after["seen"]

    # An empty document is no current dossier: the next update sends null, and NO_CHANGE keeps it empty.
    await seed_alice(plug, [(14, "in", "Another thought", -5)])
    fake = FakeLlm(doc("NO_CHANGE"))
    report = cycle(ds, fake, now=ago(-20))
    assert (report.checked, report.refreshed, report.failures) == (1, 0, [])
    assert contact_payloads(fake)[0]["current_dossier"] is None
    kept = read_dossier(ds, ALICE)
    assert (kept["document"], kept["previous"], kept["revision"]) == ("", before["document"], after["revision"])
    assert "14" in kept["seen"]
    assert cycle(ds, FakeLlm(), now=ago(-35)).calls == 0  # nothing is sent twice

    # A first pass answered EMPTY stores an empty document and moves the cursor.
    await seed(plug, BOB, "bob", [(20, "in", "hi", 50), (21, "out", "hello", 45)])
    report = cycle(ds, FakeLlm(doc("EMPTY")), target=("contact", catalog_entry(plug, BOB)))
    assert (report.checked, report.refreshed) == (1, 0)
    bob = read_dossier(ds, BOB)
    assert (bob["document"], bob["revision"], bob["updated_at"]) == ("", 0, None)
    assert bob["window_start"] is not None and sorted(bob["seen"]) == ["20", "21"]


# ---------------------------------------------------------------------------
# Owner note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_note_change_bypasses_the_stat_gate_and_the_message_check(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    note = "Invoices go to the finance team."
    with fake_secret_scope(KEYS):
        opener = use_gate(monkeypatch, ds)  # nothing queued: a check would fail the refresh
        assert cycle(ds, FakeLlm(), now=ago(35)).unchanged == 1  # the stat gate holds

        ds.set_note(("contact", entry), note)
        fake = FakeLlm(doc("NO_CHANGE"))
        report = cycle(ds, fake, now=ago(30))
        assert (report.checked, report.calls) == (1, 1)
        (payload,) = contact_payloads(fake)
        assert (payload["mode"], payload["owner_note"], payload["messages"]) == ("update", note, [])
        stored = read_dossier(ds, ALICE)
        assert stored["note_sha"] == ds._doc.note_sha(stored) is not None
        assert cycle(ds, fake, now=ago(29)).unchanged == 1 and len(fake.calls) == 1

        # A changed note with a small burst of new messages: the writer runs without a check.
        await seed_alice(plug, [(13, "in", "Thanks!", 25)])
        ds.set_note(("contact", entry), "Invoices go to Mira from now on.")
        fake = FakeLlm()
        report = cycle(ds, fake)
        assert (report.calls, report.deferred, report.failed) == (1, 0, 0)
        assert contact_payloads(fake)[0]["owner_note"] == "Invoices go to Mira from now on."
    assert opener.calls == []


@pytest.mark.asyncio
async def test_a_note_saved_during_a_call_survives_and_reruns(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    fake = FakeLlm()
    fake.on_call = lambda _payload: ds.set_note(("contact", entry), "Call her Mira.")
    assert cycle(ds, fake).refreshed == 1
    stored = read_dossier(ds, ALICE)
    assert (stored["note"], stored["note_sha"]) == ("Call her Mira.", None)  # written with no note
    assert "owner_note" not in contact_payloads(fake)[0]

    fake.on_call = lambda _payload: ds.set_note(("contact", entry), "Call her Mira, never Miroslava.")
    assert cycle(ds, fake, now=ago(-5)).calls == 1  # the note change reruns without a new message
    assert contact_payloads(fake)[1]["owner_note"] == "Call her Mira."
    stored = read_dossier(ds, ALICE)
    assert stored["note"] == "Call her Mira, never Miroslava."
    assert stored["note_sha"] == ds._doc.note_sha({"note": "Call her Mira."})

    fake.on_call = None
    assert cycle(ds, fake, now=ago(-10)).calls == 1
    assert contact_payloads(fake)[2]["owner_note"] == "Call her Mira, never Miroslava."
    assert cycle(ds, fake, now=ago(-15)).calls == 0

    ds.set_note(("contact", entry), None)  # a cleared note reruns too, now without owner_note
    assert cycle(ds, fake, now=ago(-20)).calls == 1
    assert "owner_note" not in contact_payloads(fake)[3]
    assert read_dossier(ds, ALICE)["note_sha"] is None


# ---------------------------------------------------------------------------
# Message check (Jev)
# ---------------------------------------------------------------------------

GATE_BYPASSES = ("forced", "first_pass", "empty_document", "note", "edit", "deletion", "pruned_deletion", "too_many",
                 "too_long", "too_old", "setting_off", "no_key", "not_gateway")


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", GATE_BYPASSES)
async def test_the_message_check_is_bypassed(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
    bypass: str,
) -> None:
    ds = dossiers
    if bypass == "pruned_deletion":  # a message whose month file retention will drop
        raw(plug, ALICE, [(1, "in", "Old order: two crates of lemons", timedelta(days=70))], username="alice")
    entry = await seed_alice(plug)
    if bypass != "first_pass":
        first = FakeLlm(doc("EMPTY") if bypass == "empty_document" else quote_first)
        assert cycle(ds, first, now=ago(40)).calls == 1
    opener = use_gate(monkeypatch, ds)  # nothing queued: a check would fail the refresh
    now, target, secrets = NOW, None, KEYS
    new = [(13, "in", "My new number is +7 912 345-67-13", 30)]
    if bypass == "forced":
        target = ("contact", entry)
    elif bypass == "note":
        ds.set_note(("contact", entry), "Invoices go to the finance team.")
    elif bypass == "edit":
        await edit(plug, ALICE, 10, "Hi, I now prefer phone calls", 30)
    elif bypass == "deletion":
        await delete(plug, ALICE, 12, when=30)
    elif bypass == "pruned_deletion":
        await delete(plug, ALICE, 1, when=30)
        test_history.set_retention(plug, 30)
        assert maintain(plug, 20).pruned_files >= 1  # the create record of 1 is gone, its deletion is not
    elif bypass == "too_many":
        monkeypatch.setattr(ds._writer, "GATE_MAX_MESSAGES", 1)
        new.append((14, "in", "And my e-mail is mira@example.com", 29))
    elif bypass == "too_long":
        monkeypatch.setattr(ds._writer, "GATE_MAX_CHARS", len(new[0][2]) - 1)
    elif bypass == "too_old":
        now = ago(30) + ds._writer.GATE_MAX_AGE
    elif bypass == "setting_off":
        ds.set_gate("off", via="cli")
    elif bypass == "no_key":
        secrets = CF_KEYS
    elif bypass == "not_gateway":
        dossier_env(PROVIDER="openrouter")
    await seed_alice(plug, new)
    fake = FakeLlm()
    with fake_secret_scope(secrets):
        report = cycle(ds, fake, now=now, target=target)
    assert (report.calls, report.deferred, report.failed) == (1, 0, 0)
    assert opener.calls == []
    if bypass == "pruned_deletion":
        assert {"ref": "m1", "deleted": True} in contact_payloads(fake)[0]["messages"]


@pytest.mark.asyncio
async def test_a_check_at_the_threshold_lets_the_writer_run(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    document = read_dossier(ds, ALICE)["document"]
    ds.set_note(("contact", catalog_entry(plug, ALICE)), "SECRET-NOTE never goes to the check.")
    assert cycle(ds, FakeLlm(doc("NO_CHANGE")), now=ago(35)).checked == 1
    with fake_secret_scope(KEYS):
        opener = use_gate(monkeypatch, ds, jev_reply(0.2, 0.35))  # p is the higher answer
        await seed_alice(plug, [(13, "in", "I'll pay the deposit on Friday", 30)])
        fake = FakeLlm()
        report = cycle(ds, fake)
    assert (report.refreshed, report.deferred, report.calls) == (1, 0, 1)
    (request,) = opener.calls
    assert request["url"] == "https://ai-gateway.vercel.sh/v1/evaluate"
    body = request["body"]
    assert body["model"] == "typesafe-ai/jev-1.13.0"
    assert body["providerOptions"] == {"gateway": {"zeroDataRetention": True, "only": ["typesafe-ai"]}}
    assert body["state"] == {"current_dossier": document,
                             "new_messages": [{"ref": "m13", "at": local(30), "from": "contact",
                                               "text": "I'll pay the deposit on Friday"}]}
    assert "SECRET-NOTE" not in request["data"].decode()
    assert read_state(ds)["usage"][MONTH]["check"] == {"calls": 1, "input_tokens": 1200, "cost_usd": 0.00005}


@pytest.mark.asyncio
async def test_a_low_check_defers_and_recheck_at_forces_the_writer_after_72_hours(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    before = doc_bytes(ds, ALICE)
    key = key_of(ds, ALICE)
    recheck = ds._iso(ago(30) + timedelta(hours=72))
    fake = FakeLlm()
    with fake_secret_scope(KEYS):
        opener = use_gate(monkeypatch, ds, jev_reply(0.1, 0.2), jev_reply(0.05, 0.3))
        await seed_alice(plug, [(13, "in", "Ha, thanks!", 30)])
        report = cycle(ds, fake)
        assert (report.deferred, report.calls, fake.calls) == (1, 0, [])
        assert doc_bytes(ds, ALICE) == before  # seen is untouched: the message stays pending
        state = read_state(ds)
        assert state["chats"][key]["recheck_at"] == recheck
        assert state["usage"][MONTH]["check"] == {"calls": 1, "input_tokens": 1200, "cost_usd": 0.00005,
                                                  "deferred": 1}
        assert cycle(ds, fake, now=ago(-60)).unchanged == 1 and len(opener.calls) == 1  # a quiet chat waits

        # The next message reopens the stat gate; the check sees the old and the new message together.
        await seed_alice(plug, [(14, "in", "See you!", -70)])
        assert cycle(ds, fake, now=ago(-85)).deferred == 1
        assert [m["ref"] for m in opener.calls[1]["body"]["state"]["new_messages"]] == ["m13", "m14"]
        assert read_state(ds)["chats"][key]["recheck_at"] == recheck  # still counted from the oldest

        # At recheck_at the oldest pending message is 72 h old, so the writer runs without a check.
        report = cycle(ds, fake, now=ago(30) + timedelta(hours=72))
    assert (report.refreshed, report.calls, len(opener.calls)) == (1, 1, 2)
    assert refs(contact_payloads(fake)[0]) == ["m13", "m14"]
    assert read_state(ds)["chats"][key]["recheck_at"] is None
    assert read_state(ds)["usage"][MONTH]["check"]["deferred"] == 2


GATE_ERRORS = {
    "http_401": (401, b'{"error": "SECRET-BODY"}'),
    "http_429": (429, b"{}"),
    "http_500": (500, b"{}"),
    "timeout": urllib.error.URLError(TimeoutError("timed out")),
    "network": urllib.error.URLError(ConnectionRefusedError("refused")),
    "not_json": (200, b"<html>SECRET-BODY</html>"),
    "missing": (200, json.dumps({"answers": {"update": {"probability": 0.9}}}).encode()),
    "out_of_range": jev_reply(1.5, 0.2),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", list(GATE_ERRORS))
async def test_a_failed_check_lets_the_writer_run_and_counts_an_error(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error: str,
) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    with fake_secret_scope(KEYS):
        opener = use_gate(monkeypatch, ds, GATE_ERRORS[error])
        await seed_alice(plug, [(13, "in", "Thanks, bye", 30)])
        with caplog.at_level(logging.WARNING):
            report = cycle(ds, FakeLlm())
    assert (report.refreshed, report.deferred, report.failed, len(opener.calls)) == (1, 0, 0, 1)
    assert read_state(ds)["usage"][MONTH]["check"] == {"errors": 1}
    warnings = [r.getMessage() for r in caplog.records if "model check failed" in r.getMessage()]
    assert len(warnings) == 1 and "SECRET" not in caplog.text  # logged once, by kind and status


# ---------------------------------------------------------------------------
# Fact hints (Clairic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("setting", "secrets", "asked"), [
    ("on", CF_KEYS, True),
    ("on", GATEWAY_KEY, False),
    ("off", CF_KEYS, False),
])
async def test_fact_hints_need_the_setting_and_cloudflare_credentials(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, setting: str,
    secrets: dict[str, str], asked: bool,
) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    opener = use_hints(monkeypatch, ds, clairic_reply())
    ds.set_clairic(setting, via="cli")
    await seed_alice(plug, [(13, "in", "Let's meet on 28 October, the price is 900", 30)])
    fake = FakeLlm()
    with fake_secret_scope(secrets):
        assert cycle(ds, fake).calls == 1
    (payload,) = contact_payloads(fake)
    if not asked:
        assert "hints" not in payload and opener.calls == []
        return
    assert payload["hints"] == DEFAULT_HINTS
    (request,) = opener.calls
    assert request["url"] == ("https://api.cloudflare.com/client/v4/accounts/0123456789abcdef0123456789abcdef/"
                              "ai/run/@cf/cloudflare/clairic")
    assert request["unredirected_headers"] == {"Authorization": "Bearer test-cf-token"}
    body = request["body"]
    assert body["state"] == f"{local(30)} contact: Let's meet on 28 October, the price is 900"  # no refs or ids
    assert (body["model"], body["reference_date"], body["fields"]) == ("clairic", f"{NOW:%Y-%m-%d}",
                                                                     ds._writer.CLAIRIC_FIELDS)


@pytest.mark.asyncio
async def test_fact_hints_skip_the_persona_and_a_deferred_refresh(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA", owner=10)
    with fake_secret_scope(KEYS):
        hints = use_hints(monkeypatch, ds, clairic_reply())
        fake = FakeLlm()
        report = cycle(ds, fake)
        assert (report.refreshed, report.persona, len(hints.calls)) == (1, "refreshed", 1)  # the contact only
        assert "hints" in contact_payloads(fake)[0]
        assert "hints" not in persona_calls(fake)[0]["user"]
        assert cycle(ds, fake, now=ago(-5), target=("persona", None)).persona == "refreshed"
        assert len(hints.calls) == 1

        gate = use_gate(monkeypatch, ds, jev_reply(0.1, 0.1))
        await seed_alice(plug, [(40, "in", "Ha, thanks!", -10)])
        report = cycle(ds, fake, now=ago(-30))
        assert (report.deferred, len(gate.calls), len(hints.calls)) == (1, 1, 1)  # no fact hints for a deferral


HINT_CASES = ("note_only", "edit_only", "deletion_only", "edit_and_new", "at_the_limits", "too_many", "too_long")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", HINT_CASES)
async def test_fact_hints_need_one_to_forty_new_messages_within_the_size_limit(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    opener = use_hints(monkeypatch, ds, clairic_reply())
    new = [(13, "in", "The deposit is 900", 30), (14, "in", "Due on 28 October", 29)]
    size = sum(len(text) for _mid, _who, text, _at in new)
    if case == "note_only":
        ds.set_note(("contact", entry), "Deposits go to the finance team.")
        new = []
    elif case in ("edit_only", "edit_and_new"):
        await edit(plug, ALICE, 10, "Hi, I now prefer phone calls", 30)
        new = new[:1] if case == "edit_and_new" else []
    elif case == "deletion_only":
        await delete(plug, ALICE, 12, when=30)
        new = []
    else:
        monkeypatch.setattr(ds._writer, "GATE_MAX_MESSAGES", 1 if case == "too_many" else 2)
        monkeypatch.setattr(ds._writer, "GATE_MAX_CHARS", size - 1 if case == "too_long" else size)
    await seed_alice(plug, new)
    fake = FakeLlm()
    with fake_secret_scope(CF_KEYS):
        assert cycle(ds, fake).calls == 1
    asked = case in ("edit_and_new", "at_the_limits")
    assert (len(opener.calls), "hints" in contact_payloads(fake)[0]) == (int(asked), asked)
    if case == "edit_and_new":  # only new messages go to Cloudflare
        assert opener.calls[0]["body"]["state"] == f"{local(30)} contact: The deposit is 900"


@pytest.mark.asyncio
async def test_fact_hints_go_out_once_with_the_first_part(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    monkeypatch.setattr(ds, "BATCH_MAX_MESSAGES", 3)
    rows = [(i, "in" if i % 2 else "out", f"message {i}", 100 - 5 * i) for i in range(1, 8)]
    await seed(plug, ALICE, "alice", rows)
    opener = use_hints(monkeypatch, ds, clairic_reply())
    fake = FakeLlm()
    with fake_secret_scope(CF_KEYS):
        assert cycle(ds, fake).calls == 3
    assert len(opener.calls) == 1  # all seven new messages, asked once
    assert opener.calls[0]["body"]["state"].count(" contact: ") == 4
    assert [("hints" in p) for p in contact_payloads(fake)] == [True, False, False]


HINT_ERRORS = {
    "http_401": (401, b"{}"),
    "http_429": (429, b"{}"),
    "timeout": urllib.error.URLError(TimeoutError("timed out")),
    "network": urllib.error.URLError(ConnectionRefusedError("refused")),
    "not_success": (200, json.dumps({"success": False, "result": None, "errors": [{"code": 5007}]}).encode()),
    "bad_answers": (200, json.dumps({"result": {"answers": ["SECRET-ANSWER"]}, "success": True}).encode()),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", list(HINT_ERRORS))
async def test_failed_fact_hints_let_the_writer_run_and_count_an_error(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error: str,
) -> None:
    ds = dossiers
    await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    opener = use_hints(monkeypatch, ds, HINT_ERRORS[error])
    await seed_alice(plug, [(13, "in", "The deposit is 900", 30)])
    fake = FakeLlm()
    with fake_secret_scope(CF_KEYS), caplog.at_level(logging.WARNING):
        report = cycle(ds, fake)
    assert (report.refreshed, report.failed, len(opener.calls)) == (1, 0, 1)
    assert "hints" not in contact_payloads(fake)[0]
    assert read_state(ds)["usage"][MONTH]["clairic"] == {"errors": 1}
    warnings = [r.getMessage() for r in caplog.records if "fact extraction failed" in r.getMessage()]
    assert len(warnings) == 1 and "SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_fact_hints_have_their_own_usage_bucket_and_run_for_forced_refreshes(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    with fake_secret_scope(CF_KEYS):
        opener = use_hints(monkeypatch, ds, clairic_reply(tokens=2000), clairic_reply(tokens=1000),
                           (500, b"{}"))
        report = cycle(ds, FakeLlm())
        writer_price = ds._writer.price("ai-gateway", "google/gemini-3.8-flash", 100, 50)
        assert report.cost_usd == pytest.approx(0.0002 + writer_price)  # the full float, never rounded
        await seed_alice(plug, [(13, "in", "The deposit is 900", 30)])
        assert cycle(ds, FakeLlm(), target=("contact", entry)).calls == 1  # forced
        await seed_alice(plug, [(14, "in", "Due on 28 October", -10)])
        assert cycle(ds, FakeLlm(), now=ago(-25)).calls == 1
    assert len(opener.calls) == 3
    usage = read_state(ds)["usage"][MONTH]
    assert usage["clairic"] == {"calls": 2, "input_tokens": 3000, "cost_usd": pytest.approx(0.0003), "errors": 1}
    assert usage["writer"]["calls"] == 3  # the writer bucket is separate


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_counts_writer_calls_tokens_cost_and_unpriced_answers(
    plug: ModuleType, dossiers: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    price = ds._writer.price("ai-gateway", "google/gemini-3.8-flash", 100, 50)
    with caplog.at_level(logging.INFO):
        report = cycle(ds, FakeLlm())
    assert report.cost_usd == price  # the full float: status rounds it only when it shows it
    assert read_state(ds)["usage"][MONTH]["writer"] == {"calls": 1, "input_tokens": 100, "output_tokens": 50,
                                                        "cost_usd": price, "unpriced": 0}
    assert read_state(ds)["last_cycle"]["cost_usd"] == price
    (line,) = [r.getMessage() for r in caplog.records if "dossier call" in r.getMessage()]
    assert re.fullmatch(rf"telesec: dossier call purpose=telesec_dossier chat_ref={ds._log_ref(entry)} "
                        r"ms=\d+ in=100 out=50", line)

    # A too long answer costs one shorten call more.
    long_text = "\n".join(f"- Fact number {i}: " + "detail " * 10 for i in range(60))
    await seed_alice(plug, [(13, "in", "Here is a long story", 30)])
    fake = FakeLlm(doc(f"**The contact.**\n\n## Notes\n{long_text}"))
    report = cycle(ds, fake)
    assert (report.calls, [c["purpose"] for c in fake.calls]) == (2, ["telesec_dossier", "telesec_shorten"])
    writer = read_state(ds)["usage"][MONTH]["writer"]
    assert (writer["calls"], writer["input_tokens"], writer["output_tokens"], writer["unpriced"]) == (3, 300, 150, 0)
    assert writer["cost_usd"] == pytest.approx(3 * price)  # the sum of full floats, never rounded on the way


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unstructured", "too_long"])
async def test_usage_counts_writer_calls_whose_answer_was_refused(
    plug: ModuleType, dossiers: ModuleType, case: str
) -> None:
    ds = dossiers
    await seed_alice(plug)
    price = ds._writer.price("ai-gateway", "google/gemini-3.8-flash", 100, 50)
    long_text = doc("**The contact.**\n\n## Notes\n" + "\n".join(f"- Fact {i}: " + "detail " * 10 for i in range(60)))
    replies = [doc("Sorry, I cannot help with that request.")] if case == "unstructured" else [long_text, long_text]
    report = cycle(ds, FakeLlm(*replies))
    calls = len(replies)
    assert (report.failed, report.calls, report.cost_usd) == (1, calls, pytest.approx(calls * price))
    assert read_state(ds)["usage"][MONTH]["writer"] == {
        "calls": calls, "input_tokens": 100 * calls, "output_tokens": 50 * calls,
        "cost_usd": pytest.approx(calls * price), "unpriced": 0}


@pytest.mark.asyncio
async def test_last_call_names_the_answering_provider_after_a_host_fallback(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA", owner=10)
    fallback = FakeLlm(provider="openrouter", model="example/fallback-1")  # the host fell back to the main model
    report = cycle(ds, fallback)
    assert (report.refreshed, report.persona, report.cost_usd) == (1, "refreshed", 0.0)
    expected = {"provider": "openrouter", "model": "example/fallback-1", "at": ds._iso(NOW)}
    assert read_dossier(ds, ALICE)["last_call"] == expected
    assert read_persona(ds)["last_call"] == expected
    writer = read_state(ds)["usage"][MONTH]["writer"]
    assert (writer["calls"], writer["cost_usd"], writer["unpriced"]) == (2, 0, 2)  # its price is unknown


@pytest.mark.asyncio
async def test_usage_keeps_the_newest_three_months(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_alice(plug)
    old = {"2000-01": {"writer": {"calls": 1}}, "2000-02": {"check": {"calls": 2}}, "2000-03": {"clairic": {"calls": 3}}}
    ds.state_path().write_text(json.dumps({"schema_version": 1, "usage": old}), encoding="utf-8")
    assert cycle(ds, FakeLlm()).refreshed == 1
    usage = read_state(ds)["usage"]
    assert sorted(usage) == ["2000-02", "2000-03", MONTH]
    assert usage["2000-03"] == {"clairic": {"calls": 3}} and usage[MONTH]["writer"]["calls"] == 1


# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------


def owner_messages(ds: ModuleType, count: int, at: datetime) -> list[Any]:
    return [ds.Msg(key=str(i), chat_key="chat", business_connection_id=BUSINESS_ID, chat_id=ALICE, message_id=i,
                   direction="outbound", label="owner", text=f"owner text {i}", message_at=at, order_at=at,
                   first_seen=at, deleted=False, fp="fp") for i in range(count)]


def test_the_persona_due_rules(dossiers: ModuleType) -> None:
    ds = dossiers
    assert (ds.PERSONA_MIN_FIRST, ds.PERSONA_MIN_NEW, ds.PERSONA_MIN_AGE) == (10, 20, timedelta(days=7))
    due = ds.persona_due
    recent = owner_messages(ds, 25, NOW - timedelta(hours=1))
    assert not due(None, recent[:9], NOW) and due(None, recent[:10], NOW)  # a first build needs 10

    reset = {**ds._doc.new_persona(NOW), "checked_at": None}
    assert not due(reset, recent[:9], NOW) and due(reset, recent[:10], NOW)  # erased: rebuilt like a first one
    assert due(reset, recent[:1], NOW, forced=True) and not due(reset, [], NOW, forced=True)
    noted = {**reset, "note": "Short replies.", "updated_at": ds._iso(NOW), "document": "## Style\n- Short."}
    assert due(noted, recent[:1], NOW) and not due(noted, [], NOW)  # a changed note needs one message

    built = {**noted, "note_sha": ds._doc.note_sha(noted), "updated_at": ds._iso(NOW - timedelta(days=7)),
             "newest_at": ds._iso(NOW - timedelta(days=2))}
    assert due(built, recent[:20], NOW) and not due(built, recent[:19], NOW)  # 20 new messages after 7 days
    assert not due(built, recent[:20], NOW - SECOND)  # updated less than 7 days ago
    older = owner_messages(ds, 25, NOW - timedelta(days=3))
    assert not due(built, older, NOW)  # not newer than newest_at
    assert due(built, older[:1], NOW, forced=True)


@pytest.mark.asyncio
async def test_the_persona_is_built_from_ten_owner_messages_and_checked_once_a_day(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA", owner=9)
    fake = FakeLlm()
    assert cycle(ds, fake).persona == "not_due"  # nine owner messages
    assert read_persona(ds) is None and persona_calls(fake) == []

    await seed_alice(plug, [(20, "out", "ALPHA owner message 20", 30)])
    built_at = ago(-5)
    assert cycle(ds, fake, now=built_at).persona == "refreshed"
    persona = read_persona(ds)
    assert (persona["messages"], persona["updated_at"], persona["checked_at"]) == (10, ds._iso(built_at),
                                                                                    ds._iso(built_at))
    assert persona["chats"] == [{"business_connection_id": BUSINESS_ID, "chat_id": ALICE}]
    assert persona["newest_at"] == ds._iso(ago(30))

    later = [(100 + i, "out", f"ALPHA later owner message {i}", -60 - i) for i in range(25)]
    await seed_alice(plug, later)
    before = (persona_dir(ds) / "document.json").read_bytes()
    folds: list[str] = []
    real_view = ds.read_chat_view
    monkeypatch.setattr(ds, "read_chat_view", lambda entry, **kw: (folds.append("read"), real_view(entry, **kw))[1])
    report = cycle(ds, fake, now=built_at + timedelta(hours=23), target=("contact", catalog_entry(plug, ALICE)))
    assert report.persona == "skipped"  # a contact run never touches the persona
    folds.clear()
    assert cycle(ds, fake, now=built_at + timedelta(hours=23, minutes=10)).persona == "not_due"
    assert folds == [] and (persona_dir(ds) / "document.json").read_bytes() == before  # checked < 24 h ago

    checked = built_at + timedelta(hours=24)
    assert cycle(ds, fake, now=checked).persona == "not_due"  # 25 new messages, but built < 7 days ago
    persona = read_persona(ds)
    assert (persona["checked_at"], persona["updated_at"]) == (ds._iso(checked), ds._iso(built_at))
    assert len(persona_calls(fake)) == 1

    report = cycle(ds, fake, now=built_at + timedelta(days=7))
    assert report.persona == "refreshed" and len(persona_calls(fake)) == 2
    assert read_persona(ds)["messages"] == 35


@pytest.mark.asyncio
async def test_a_forced_run_or_a_note_change_rebuilds_the_persona_at_once(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA", owner=10)
    fake = FakeLlm()
    assert cycle(ds, fake).persona == "refreshed"
    await seed_alice(plug, [(30, "out", "ALPHA one more owner line", -5)])
    assert cycle(ds, fake, now=ago(-20)).persona == "not_due"

    assert cycle(ds, fake, now=ago(-21), target=("persona", None)).persona == "refreshed"
    assert len(persona_calls(fake)[-1]["user"]["messages"]) == 11

    ds.set_note(("persona", None), "I never use emoji with clients.")
    assert cycle(ds, fake, now=ago(-30)).persona == "refreshed"  # inside the 24 h check interval
    assert persona_calls(fake)[-1]["user"]["owner_note"] == "I never use emoji with clients."
    assert cycle(ds, fake, now=ago(-40)).persona == "not_due"
    assert len(persona_calls(fake)) == 3

    pause(plug, ALICE)  # no eligible message left: even a forced run has nothing to describe
    assert cycle(ds, fake, now=ago(-50), target=("persona", None)).persona == "not_due"
    assert len(persona_calls(fake)) == 3


@pytest.mark.asyncio
async def test_the_persona_is_rebuilt_from_scratch_from_the_owners_own_text(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    start = ago(120)

    def at(minutes: int, seconds: int = 0) -> datetime:
        return start + timedelta(minutes=minutes, seconds=seconds)

    await capture(plug, ALICE, [
        (1, "in", "ALPHA question", at(0), None),
        (2, "out", "Owner line one", at(1), None),
        (3, "bot", f"{MARK} TRANSCRIPT chunk zero", at(2), 1),
        (4, "bot", "TRANSCRIPT continuation chunk", at(2, 5), None),
        (5, "out", "DELETED owner line", at(3), None),
        (6, "out", "Owner line two", at(4), None),
        (7, "out", "Owner line three", at(5), 1),  # a reply is never a transcript chunk
    ])
    await delete(plug, ALICE, 5, when=at(6))
    await seed(plug, BOB, "bob", [(1, "in", "BRAVO question", 100)] +
               [(i, "out", f"Bob chat owner line {i}", 100 - i) for i in range(2, 9)])
    await seed(plug, CAROL, "carol", [(1, "in", "CHARLIE question", 90), (2, "out", "CHARLIE owner line", 89)])
    pause(plug, CAROL)  # a paused chat's messages leave the persona input
    maintain(plug, 50)

    fake = FakeLlm()
    assert cycle(ds, fake, target=("persona", None)).persona == "refreshed"
    assert cycle(ds, fake, now=ago(-5), target=("persona", None)).persona == "refreshed"
    first, second = (call["user"] for call in persona_calls(fake))
    assert first == second  # rebuilt from scratch: the stored persona never goes back in
    assert set(first) == {"today", "messages"}
    texts = [m["text"] for m in first["messages"]]
    assert texts == ["Owner line one", "Owner line two", "Owner line three"] + [
        f"Bob chat owner line {i}" for i in range(2, 9)]  # oldest first, across chats
    assert {m["chat"] for m in first["messages"]} == {"c1", "c2"}
    assert all(set(m) == {"chat", "at", "text"} for m in first["messages"])
    raw_text = persona_calls(fake)[1]["raw"]
    for leaked in ("TRANSCRIPT", "DELETED", "CHARLIE", "ALPHA", "BRAVO", "Style", str(ALICE), BUSINESS_ID):
        assert leaked not in raw_text, leaked
    assert read_persona(ds)["chats"] == [{"business_connection_id": BUSINESS_ID, "chat_id": ALICE},
                                         {"business_connection_id": BUSINESS_ID, "chat_id": BOB}]


# ---------------------------------------------------------------------------
# Forget contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_returns_the_contract_for_each_target(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    alice = await seed_owner_chat(plug, ALICE, "ALPHA")
    await seed_owner_chat(plug, BOB, "BRAVO")
    assert cycle(ds, FakeLlm()).persona == "refreshed"
    ds.set_note(("contact", alice), "Prefers mornings.")
    ds.set_note(("persona", None), "Short replies.")
    revision = read_dossier(ds, ALICE)["revision"]
    persona_revision = read_persona(ds)["revision"]
    generation = ds.generation()

    assert ds.forget(("contact", alice)) == {"existed": True, "revision": revision, "note": True,
                                             "persona_reset": True}
    assert ds.forget(("contact", alice)) == {"existed": False, "revision": None, "note": False,
                                             "persona_reset": False}
    assert ds.forget(("persona", None)) == {"existed": True, "revision": persona_revision, "note": True}
    assert not persona_dir(ds).exists()
    assert ds.forget(("persona", None)) == {"existed": False, "revision": None, "note": False}

    stub = {"business_connection_id": "biz-old", "chat_id": 700001}
    backup_only = ds.contact_dir("biz-old", 700001)
    backup_only.mkdir(parents=True)
    (backup_only / "v1-dossier.json").write_text("{}", encoding="utf-8")
    assert ds.forget(("contact", stub)) == {"existed": True, "revision": None, "note": False, "persona_reset": False}
    backup_only.mkdir(parents=True)
    (backup_only / "v1-dossier.json").write_text("{}", encoding="utf-8")
    (ds.contact_dir("biz-old", 700002)).mkdir(parents=True)  # an empty directory is no dossier

    assert ds.forget(("all", None)) == {"existed": True, "contacts": 2, "persona": False}  # bob and the backup
    assert ds.forget(("all", None)) == {"existed": False, "contacts": 0, "persona": False}
    assert ds.generation() == generation + 7  # every forget bumps the generation, even with nothing to delete


@pytest.mark.asyncio
async def test_any_forget_discards_an_answer_in_flight(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_alice(plug)
    fake = FakeLlm()
    fake.on_call = lambda _payload: ds.forget(("contact", {"business_connection_id": "biz-other", "chat_id": 5}))
    report = cycle(ds, fake)
    assert (report.calls, report.refreshed, report.waiting) == (1, 0, 1)  # the generation changed under it
    assert read_dossier(ds, ALICE) is None
    fake.on_call = None
    assert cycle(ds, fake, now=ago(-5)).refreshed == 1


# ---------------------------------------------------------------------------
# State hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_drops_departed_chats_and_failure_records_hold_no_raw_ids(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    departed = ds.chat_key("biz-gone", 700002)
    persona_failure = {"at": ds._iso(NOW - timedelta(days=2)), "error": "TimeoutError", "count": 1}
    ds.state_path().write_text(json.dumps({
        "schema_version": 1,
        "chats": {departed: {"signature": [], "dossier_id": "0123456789ab", "at": ds._iso(NOW), "recheck_at": None}},
        "failures": {departed: {"at": ds._iso(NOW), "error": "TimeoutError", "count": 1},
                     "persona": persona_failure},
    }), encoding="utf-8")

    failing = FakeLlm(TimeoutError(f"SECRET {BUSINESS_ID} {ALICE}"))
    assert cycle(ds, failing, target=("contact", entry)).failed == 1
    saved = read_state(ds)
    assert departed in saved["chats"] and departed in saved["failures"]  # a targeted run keeps other keys

    assert cycle(ds, FakeLlm(TimeoutError), now=ago(-5)).failed == 1
    saved = read_state(ds)
    assert departed not in saved["chats"] and departed not in saved["failures"]
    assert saved["failures"]["persona"] == persona_failure  # the persona key is never dropped
    record = saved["failures"][key_of(ds, ALICE)]
    assert record == {"at": ds._iso(ago(-5)), "error": "TimeoutError", "count": 2}
    assert all(set(r) == {"at", "error", "count"} for r in saved["failures"].values())  # the key is the chat key
    values = json.dumps(list(saved["failures"].values()))
    assert BUSINESS_ID not in values and str(ALICE) not in values
    assert "SECRET" not in ds.state_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# History retention (ported from the settings-retention change)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retention_prune_drops_seen_keys_without_a_call(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    old = timedelta(days=70)
    raw(plug, ALICE, [(1, "in", "Old order: two crates of lemons", old),
                      (2, "out", "Old reply: crates ship Tuesday", old - timedelta(minutes=1))], username="alice")
    await seed_alice(plug, ALICE_ROWS[:2])
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    assert refs(contact_payloads(fake)[0]) == ["m1", "m2", "m10", "m11"]
    before = read_dossier(ds, ALICE)

    test_history.set_retention(plug, 30)
    assert maintain(plug, 0).pruned_files >= 1
    report = cycle(ds, fake, now=ago(-5))
    assert (report.calls, report.unchanged) == (0, 1)
    after = read_dossier(ds, ALICE)
    assert after == {**before, "seen": {k: v for k, v in before["seen"].items() if k not in ("1", "2")}}

    await seed_alice(plug, [(12, "in", "New crates please", -10)])
    assert cycle(ds, fake, now=ago(-25)).refreshed == 1
    assert contact_payloads(fake)[1]["messages"] == [
        {"ref": "m12", "at": local(-10), "from": "contact", "text": "New crates please"}]  # never as deleted


@pytest.mark.asyncio
async def test_an_aged_out_month_is_not_read_and_its_prune_makes_no_call(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    history = plug._history_support
    dossier_env(BACKFILL_DAYS=30)
    raw(plug, ALICE, [(1, "in", "Old order: two crates of lemons", timedelta(days=25)),
                      (2, "out", "Old reply: crates ship Tuesday", timedelta(days=25, minutes=-1))], username="alice")
    entry = catalog_entry(plug, ALICE)
    assert cycle(ds, FakeLlm()).refreshed == 1

    later = NOW + timedelta(days=40)  # 1 and 2 are now older than the rolling 30-day floor
    raw(plug, ALICE, [(30, "in", "Checking in again", later - timedelta(days=1)),
                      (31, "out", "All good here", later - timedelta(days=1) + timedelta(minutes=1))],
        username="alice")
    reads: list[tuple[datetime, list[Any]]] = []
    real = history._streamed_records_locked

    @contextlib.contextmanager
    def spy(chat_dir: Any, *, since: Any, until: Any) -> Any:
        with real(chat_dir, since=since, until=until) as records:
            ids: list[Any] = []
            reads.append((since, ids))

            def tee() -> Any:
                for record in records:
                    ids.append(record.get("message_id"))
                    yield record

            yield tee()

    fake = FakeLlm()
    with monkeypatch.context() as m:
        m.setattr(history, "_streamed_records_locked", spy)
        assert cycle(ds, fake, now=later, target=("contact", entry)).refreshed == 1
    assert refs(contact_payloads(fake)[0]) == ["m30", "m31"]
    assert reads and all(since >= later - timedelta(days=30) - ds.FOLD_MARGIN for since, _ids in reads)
    assert all(1 not in ids and "1" not in ids for _since, ids in reads)  # the old month was not read
    before = doc_bytes(ds, ALICE)

    test_history.set_retention(plug, 20)
    assert maintain(plug, later).pruned_files >= 1
    report = cycle(ds, fake, now=later + timedelta(minutes=1))
    assert (report.calls, len(fake.calls)) == (0, 1)
    assert doc_bytes(ds, ALICE) == before  # nothing it still tracked was pruned


@pytest.mark.asyncio
async def test_a_dossier_whose_whole_chat_was_pruned_stays_frozen_until_forgotten(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    old = timedelta(days=70)
    raw(plug, ALICE, [(1, "in", "Old order: two crates of lemons", old),
                      (2, "out", "Old reply: crates ship Tuesday", old - timedelta(minutes=1))], username="alice")
    await seed(plug, BOB, "bob", [(20, "in", "Bob only writes here", 60)])
    entry = catalog_entry(plug, ALICE)
    report = cycle(ds, FakeLlm())
    assert (report.refreshed, report.unqualified) == (1, 1)
    before = doc_bytes(ds, ALICE)

    test_history.set_retention(plug, 30)
    assert maintain(plug, 0).pruned_files >= 1
    assert ALICE not in [e["chat_id"] for e in ds.private_entries()]

    fake = FakeLlm()
    report = cycle(ds, fake, now=ago(-5))
    assert (report.orphans, report.calls, fake.calls) == (0, 0, [])
    assert doc_bytes(ds, ALICE) == before
    assert key_of(ds, ALICE) not in read_state(ds)["chats"]  # its cache key left with the chat
    assert ds.forget(("contact", entry))["existed"] is True
    assert read_dossier(ds, ALICE) is None
