"""Dossier guarantees on the refresh side: what a retention prune, a deletion, a failed call, a forget and the
message check may never do, and that no log record carries private data.

Each test states one hard rule and checks it on the files themselves: ``snapshot_tree`` compares bytes and
mtimes, so "nothing was written" means exactly that. The model, the message check and the fact hints are
offline fakes; all data is made up.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from types import ModuleType
from typing import Any

import pytest
import test_history
from dossier_helpers import (
    BUSINESS_ID,
    NOW,
    PERSONA_DEFAULT,
    FakeLlm,
    ago,
    catalog_entry,
    cli,
    cycle,
    delete,
    doc,
    dossier_dir,
    maintain,
    persona_dir,
    quote_first,
    raw,
    read_dossier,
    read_persona,
    read_state,
    seed,
    snapshot_tree,
)
from model_fakes import FakeOpener, fake_secret_scope
from test_dossier_lifecycle import pause
from test_dossier_migrate import fact, put, v1_contact

ALICE = 991
BOB = 992
CAROL = 993
ALICE_ROWS = [
    (10, "in", "Hi, I prefer email for invoices please", 60),
    (11, "out", "Noted, I will send the invoice by Friday", 55),
    (12, "in", "Great, see you at the harbour office", 50),
]
GATEWAY_KEY = {"AI_GATEWAY_API_KEY": "test-gateway-key"}
CF_KEYS = {"CLOUDFLARE_ACCOUNT_ID": "0123456789abcdef0123456789abcdef", "CLOUDFLARE_API_TOKEN": "test-cf-token"}
KEYS = {**GATEWAY_KEY, **CF_KEYS}
LONG = "**The contact.**\n\n## Notes\n" + "\n".join(f"- Fact number {i}: " + "detail " * 10 for i in range(60))
WIRE_FIELDS = {"ref", "at", "from", "text", "voice", "edited", "deleted"}


@pytest.fixture(autouse=True)
def _isolated_env(dossier_env: Callable[..., None]) -> Callable[..., None]:
    # Every test starts without dossier, cleanup or model key env vars.
    return dossier_env


@pytest.fixture
def plug(dossier_plugin: ModuleType, dossiers: ModuleType) -> ModuleType:
    """The plugin module with history and dossiers enabled (same load as ``dossiers``)."""
    return dossier_plugin


def contact_payloads(fake: FakeLlm) -> list[dict[str, Any]]:
    return [call["user"] for call in fake.by_purpose("telesec_dossier")]


def persona_payloads(fake: FakeLlm) -> list[dict[str, Any]]:
    return [call["user"] for call in fake.by_purpose("telesec_persona")]


def refs(payload: dict[str, Any]) -> list[str]:
    return [m["ref"] for m in payload["messages"]]


def doc_bytes(ds: ModuleType, chat_id: int) -> bytes:
    return (dossier_dir(ds, chat_id) / "document.json").read_bytes()


async def seed_alice(plugin: ModuleType, rows: Any = ALICE_ROWS, **kw: Any) -> dict[str, Any]:
    await seed(plugin, ALICE, "alice", rows, **kw)
    return catalog_entry(plugin, ALICE)


async def seed_owner_chat(plugin: ModuleType, chat_id: int, tag: str, *, owner: int = 5) -> dict[str, Any]:
    """One contact question, then ``owner`` owner messages a minute apart."""
    rows = [(1, "in", f"{tag} question from the contact", 120)]
    rows += [(i, "out", f"{tag} owner message {i}", 120 - i) for i in range(2, owner + 2)]
    await seed(plugin, chat_id, f"user{chat_id}", rows)
    return catalog_entry(plugin, chat_id)


def jev_reply(update: float, promise: float) -> tuple[int, bytes]:
    body = {"answers": {"update": {"probability": update}, "promise": {"probability": promise}},
            "usage": {"inputTokens": 1200}, "providerMetadata": {"gateway": {"cost": "0.00005"}}}
    return 200, json.dumps(body).encode()


def clairic_reply() -> tuple[int, bytes]:
    answers = {"dates": {"type": "list", "value": ["2026-10-28"], "guess": ["2026-10-28"], "confidence": 0.98,
                         "p_null": 0.01}}
    return 200, json.dumps({"result": {"answers": answers, "usage": {"input_tokens": 655}}, "success": True}).encode()


def use_gate(monkeypatch: pytest.MonkeyPatch, ds: ModuleType, *responses: Any) -> FakeOpener:
    ds.set_gate("auto", via="cli")
    opener = FakeOpener(responses)
    monkeypatch.setattr(ds._writer, "gate_check", functools.partial(ds._writer.gate_check, opener=opener))
    return opener


def use_hints(monkeypatch: pytest.MonkeyPatch, ds: ModuleType, *responses: Any) -> FakeOpener:
    ds.set_clairic("on", via="cli")
    opener = FakeOpener(responses)
    monkeypatch.setattr(ds._writer, "clairic_hints", functools.partial(ds._writer.clairic_hints, opener=opener))
    return opener


class PluginLlmTrustError(PermissionError):
    """Same name and base as the host's trust-gate error."""


# ---------------------------------------------------------------------------
# Retention is never a deletion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retention_prune_of_untracked_months_makes_no_call_and_writes_nothing(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None]
) -> None:
    """Months that aged past the rolling floor are no longer in ``seen``: their prune leaves the file byte-identical."""
    ds = dossiers
    dossier_env(BACKFILL_DAYS=30)
    raw(plug, ALICE, [(1, "in", "Old order: two crates of lemons", timedelta(days=25)),
                      (2, "out", "Old reply: crates ship Tuesday", timedelta(days=25, minutes=-1))], username="alice")
    assert cycle(ds, FakeLlm()).refreshed == 1
    later = NOW + timedelta(days=40)
    raw(plug, ALICE, [(30, "in", "Checking in again", timedelta(days=1)),
                      (31, "out", "All good here", timedelta(days=1, minutes=-1))], username="alice", base=later)
    assert cycle(ds, FakeLlm(), now=later).refreshed == 1
    assert sorted(read_dossier(ds, ALICE)["seen"]) == ["30", "31"]
    before = snapshot_tree(dossier_dir(ds, ALICE))

    test_history.set_retention(plug, 20)
    assert maintain(plug, later).pruned_files >= 1
    fake = FakeLlm()
    for minutes in (1, 30):
        report = cycle(ds, fake, now=later + timedelta(minutes=minutes))
        assert (report.calls, report.failed) == (0, 0)
    report = cycle(ds, fake, now=later + timedelta(minutes=40), target=("contact", catalog_entry(plug, ALICE)))
    assert (report.calls, fake.calls) == (0, [])  # even a forced run has nothing to send
    assert snapshot_tree(dossier_dir(ds, ALICE)) == before


@pytest.mark.asyncio
async def test_a_retention_prune_of_tracked_messages_makes_no_call_and_keeps_the_document(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    """Pruned messages still in ``seen`` leave it silently (B.2 "gone"); nothing else in the file changes."""
    ds = dossiers
    old = timedelta(days=70)
    raw(plug, ALICE, [(1, "in", "Old order: two crates of lemons", old),
                      (2, "out", "Old reply: crates ship Tuesday", old - timedelta(minutes=1))], username="alice")
    await seed_alice(plug, ALICE_ROWS[:2])
    assert cycle(ds, FakeLlm()).refreshed == 1
    before = read_dossier(ds, ALICE)
    assert sorted(before["seen"], key=int) == ["1", "2", "10", "11"]

    test_history.set_retention(plug, 30)
    assert maintain(plug, 0).pruned_files >= 1
    fake = FakeLlm()
    report = cycle(ds, fake, now=ago(-5))
    assert (report.calls, report.unchanged, fake.calls) == (0, 1, [])
    assert read_dossier(ds, ALICE) == {**before, "seen": {k: before["seen"][k] for k in ("10", "11")}}

    settled = snapshot_tree(dossier_dir(ds, ALICE))
    report = cycle(ds, fake, now=ago(-10), target=("contact", catalog_entry(plug, ALICE)))
    assert (report.calls, fake.calls) == (0, [])
    assert snapshot_tree(dossier_dir(ds, ALICE)) == settled


@pytest.mark.asyncio
async def test_a_dossier_whose_whole_chat_was_pruned_is_never_written_again(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    old = timedelta(days=70)
    raw(plug, ALICE, [(1, "in", "Old order: two crates of lemons", old),
                      (2, "out", "Old reply: crates ship Tuesday", old - timedelta(minutes=1))], username="alice")
    await seed(plug, BOB, "bob", [(20, "in", "Bob keeps the catalog alive", 60)])
    assert cycle(ds, FakeLlm()).refreshed == 1
    before = snapshot_tree(dossier_dir(ds, ALICE))

    test_history.set_retention(plug, 30)
    assert maintain(plug, 0).pruned_files >= 1
    fake = FakeLlm()
    for minutes in (5, 60, 60 * 25):
        assert cycle(ds, fake, now=ago(-minutes)).calls == 0
    assert fake.calls == [] and snapshot_tree(dossier_dir(ds, ALICE)) == before


# ---------------------------------------------------------------------------
# Deletions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deletion_is_reported_once(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    await delete(plug, ALICE, 11, when=30)
    await seed_alice(plug, [(13, "in", "Typo, ignore this one", 29)])
    await delete(plug, ALICE, 13, when=28)  # never sent, so its deletion is not news either

    fake = FakeLlm(doc("NO_CHANGE"))
    assert cycle(ds, fake).checked == 1
    (payload,) = contact_payloads(fake)
    assert payload["messages"] == [{"ref": "m11", "at": f"{ago(55):%Y-%m-%d %H:%M}", "from": "owner",
                                    "text": "Noted, I will send the invoice by Friday", "deleted": True}]
    assert sorted(read_dossier(ds, ALICE)["seen"], key=int) == ["10", "12"]

    maintain(plug, -2)  # the deletions get classified
    assert cycle(ds, fake, now=ago(-15)).calls == 0
    assert cycle(ds, fake, now=ago(-16), target=("contact", entry)).calls == 0  # forced: still nothing to send
    await seed_alice(plug, [(14, "in", "Back to the invoice", -20)])
    assert cycle(ds, fake, now=ago(-35)).calls == 1
    payload = contact_payloads(fake)[1]
    assert refs(payload) == ["m14"] and not any(m.get("deleted") for m in payload["messages"])
    assert "m11" not in [m["ref"] for m in payload["context"]]
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_a_deletion_whose_message_was_pruned_is_sent_as_a_bare_ref(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    old = timedelta(days=70)
    raw(plug, ALICE, [(1, "in", "PRUNED-TEXT two crates of lemons", old),
                      (2, "out", "PRUNED-TEXT crates ship Tuesday", old - timedelta(minutes=1))], username="alice")
    await seed_alice(plug, ALICE_ROWS[:2])
    assert cycle(ds, FakeLlm(doc("**The contact.**\n\n## Notes\n- Orders crates.")), now=ago(40)).refreshed == 1
    await delete(plug, ALICE, 1, when=30)
    test_history.set_retention(plug, 30)
    assert maintain(plug, 20).pruned_files >= 1  # the created record of 1 is gone, its deletion is not

    fake = FakeLlm(doc("NO_CHANGE"))
    assert cycle(ds, fake).checked == 1
    (call,) = fake.calls
    assert call["user"]["messages"] == [{"ref": "m1", "deleted": True}]
    assert "PRUNED-TEXT" not in call["raw"] and "lemons" not in call["raw"]
    assert sorted(read_dossier(ds, ALICE)["seen"], key=int) == ["10", "11"]  # 2 is gone, silently
    assert cycle(ds, fake, now=ago(-15)).calls == 0


# ---------------------------------------------------------------------------
# A failure writes nothing
# ---------------------------------------------------------------------------

FAILURES: dict[str, tuple[list[Any], str | None]] = {
    "no_end_marker": (["**The contact.**\n\n## Notes\n- Cut off in the mid"], "no_end_marker"),
    "empty": ([doc("")], "empty"),
    "unstructured": ([doc("Sorry, I cannot help with that request.")], "unstructured"),
    "reasoning": ([doc("<think>Leftover reasoning\n**The contact.**\n\n## Notes\n- A fact")], "unclosed_reasoning"),
    "shorten_no_change": ([doc(LONG), doc("NO_CHANGE")], "bad_no_change"),
    "too_long": ([doc(LONG), doc(LONG)], "too_long"),
    "timeout": ([TimeoutError("SECRET-EXC")], "TimeoutError"),
    "runtime": ([RuntimeError("SECRET-EXC")], "RuntimeError"),
    "connection": ([ConnectionError("SECRET-EXC")], "ConnectionError"),
    "value": ([ValueError("SECRET-EXC")], "ValueError"),
    "trust": ([PluginLlmTrustError("SECRET-EXC")], None),  # aborts the cycle; no target failure
}


def persona_kind(responses: list[Any]) -> list[Any]:
    """The same failure for the persona: its documents are sections and bullets too."""
    return [doc(r[: -len("\n<<END>>")].replace("**The contact.**", "## Style")) if isinstance(r, str)
            and r.endswith("<<END>>") else r for r in responses]


def check_failure(ds: ModuleType, report: Any, key: str, code: str | None) -> None:
    if code is None:
        assert (report.outcome, report.failed) == ("aborted_trust", 0)
        assert read_state(ds)["failures"] == {}
        return
    assert (report.outcome, report.failed) == ("completed", 1)
    record = read_state(ds)["failures"][key]
    assert (set(record), record["error"], record["count"]) == ({"at", "error", "count"}, code, 1)
    assert "SECRET" not in ds.state_path().read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(FAILURES))
async def test_a_failed_update_leaves_the_document_byte_identical(
    plug: ModuleType, dossiers: ModuleType, case: str
) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    await seed_alice(plug, [(13, "in", "One more thing about the invoice", 30)])
    before = snapshot_tree(dossier_dir(ds, ALICE))
    responses, code = FAILURES[case]

    report = cycle(ds, FakeLlm(*responses), target=("contact", entry))
    check_failure(ds, report, ds.chat_key(BUSINESS_ID, ALICE), code)
    assert snapshot_tree(dossier_dir(ds, ALICE)) == before

    fake = FakeLlm()  # the messages stayed pending
    assert cycle(ds, fake, now=ago(-5), target=("contact", entry)).refreshed == 1
    assert refs(contact_payloads(fake)[0]) == ["m13"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(FAILURES))
async def test_a_failed_first_pass_creates_no_document(plug: ModuleType, dossiers: ModuleType, case: str) -> None:
    ds = dossiers
    entry = await seed_alice(plug)
    responses, code = FAILURES[case]
    report = cycle(ds, FakeLlm(*responses), target=("contact", entry))
    check_failure(ds, report, ds.chat_key(BUSINESS_ID, ALICE), code)
    assert snapshot_tree(dossier_dir(ds, ALICE)) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [*FAILURES, "no_change"])
async def test_a_failed_persona_rebuild_leaves_the_persona_byte_identical(
    plug: ModuleType, dossiers: ModuleType, case: str
) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA", owner=10)
    assert cycle(ds, FakeLlm()).persona == "refreshed"
    before = snapshot_tree(persona_dir(ds))
    responses, code = ([doc("NO_CHANGE")], "bad_no_change") if case == "no_change" else FAILURES[case]

    fake = FakeLlm(*persona_kind(responses))
    report = cycle(ds, fake, now=ago(-5), target=("persona", None))
    assert len(persona_payloads(fake)) == 1
    if code is None:
        assert (report.outcome, report.persona) == ("aborted_trust", "skipped")
    else:
        assert (report.persona, report.failures) == ("failed", [("persona", code)])
        record = read_state(ds)["failures"]["persona"]
        assert (record["error"], record["count"]) == (code, 1)
        assert "SECRET" not in ds.state_path().read_text(encoding="utf-8")
    assert snapshot_tree(persona_dir(ds)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("SECRET-EXC"), doc("I cannot continue this task.")])
async def test_a_failure_in_part_two_keeps_part_one(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    ds = dossiers
    monkeypatch.setattr(ds, "BATCH_MAX_MESSAGES", 3)
    rows = [(i, "in" if i % 2 else "out", f"message {i}", 100 - 5 * i) for i in range(1, 8)]
    entry = (await seed(plug, ALICE, "alice", rows)) and catalog_entry(plug, ALICE)
    after_part_one: dict[str, Any] = {}
    fake = FakeLlm(quote_first, failure)

    def snapshot_before_part_two(_payload: dict[str, Any]) -> None:
        if len(fake.calls) == 2:
            after_part_one.update(snapshot_tree(dossier_dir(ds, ALICE)))

    fake.on_call = snapshot_before_part_two
    report = cycle(ds, fake, target=("contact", entry))
    assert (report.failed, report.calls) == (1, 2)
    assert after_part_one and snapshot_tree(dossier_dir(ds, ALICE)) == after_part_one
    part_one = read_dossier(ds, ALICE)
    assert (sorted(part_one["seen"], key=int), part_one["revision"]) == (["1", "2", "3"], 1)


# ---------------------------------------------------------------------------
# Persona erase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_erasing_a_chat_erases_the_persona_text_and_keeps_the_note(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed_owner_chat(plug, ALICE, "ALPHA")
    await seed_owner_chat(plug, BOB, "BRAVO")
    assert cycle(ds, FakeLlm()).persona == "refreshed"
    styled = FakeLlm(doc("## Style\n- Signs off with ALPHA-STYLE greetings."))
    assert cycle(ds, styled, now=ago(-5), target=("persona", None)).persona == "refreshed"
    ds.set_note(("persona", None), "Keep replies short.")
    built = read_persona(ds)
    assert built["previous"] == PERSONA_DEFAULT.removesuffix("\n<<END>>") and "ALPHA-STYLE" in built["document"]
    (persona_dir(ds) / "v1-dossier.json").write_text('{"facts": ["ALPHA-V1 fact"]}', encoding="utf-8")

    code, out, _err = cli(plug, "history", "forget", "--chat", str(ALICE), "--yes")
    assert code == 0 and "Its dossier was forgotten too." in out

    reset = read_persona(ds)
    assert reset["id"] != built["id"]
    assert (reset["document"], reset["previous"], reset["chats"], reset["newest_at"], reset["messages"]) == (
        "", None, [], None, 0)
    assert (reset["updated_at"], reset["checked_at"], reset["note_sha"]) == (None, None, None)
    assert (reset["note"], reset["note_updated_at"]) == ("Keep replies short.", built["note_updated_at"])
    assert sorted(p.name for p in persona_dir(ds).iterdir()) == ["document.json"]
    assert "ALPHA" not in (persona_dir(ds) / "document.json").read_text(encoding="utf-8")

    fake = FakeLlm()
    assert cycle(ds, fake, now=ago(-10)).persona == "refreshed"  # inside the 24 h check: the reset forces it
    (call,) = fake.by_purpose("telesec_persona")
    assert "ALPHA" not in call["raw"] and "BRAVO owner message 2" in call["raw"]
    assert call["user"]["owner_note"] == "Keep replies short."
    assert read_persona(ds)["chats"] == [{"business_connection_id": BUSINESS_ID, "chat_id": BOB}]


@pytest.mark.asyncio
@pytest.mark.parametrize("erase", ["history", "dossier"])
async def test_erasing_a_chat_only_the_previous_persona_read_erases_it(
    plug: ModuleType, dossiers: ModuleType, erase: str
) -> None:
    ds = dossiers
    alice = await seed_owner_chat(plug, ALICE, "ALPHA")
    await seed_owner_chat(plug, BOB, "BRAVO")
    assert cycle(ds, FakeLlm()).persona == "refreshed"
    styled = FakeLlm(doc("## Style\n- Signs off with ALPHA-STYLE greetings."))
    assert cycle(ds, styled, now=ago(-5), target=("persona", None)).persona == "refreshed"
    pause(plug, ALICE)  # the next persona reads BOB only; the ALPHA text moves to "previous"
    assert cycle(ds, FakeLlm(), now=ago(-10), target=("persona", None)).persona == "refreshed"
    built = read_persona(ds)
    assert [c["chat_id"] for c in built["chats"]] == [BOB] and "ALPHA-STYLE" in built["previous"]
    assert [c["chat_id"] for c in built["previous_chats"]] == [ALICE, BOB]

    if erase == "history":
        code, out, _err = cli(plug, "history", "forget", "--chat", str(ALICE), "--yes")
        assert code == 0 and "Its dossier was forgotten too." in out
    else:
        assert ds.forget(("contact", alice))["persona_reset"] is True

    reset = read_persona(ds)
    assert (reset["document"], reset["previous"], reset["chats"]) == ("", None, [])
    assert "previous_chats" not in reset
    assert "ALPHA" not in (persona_dir(ds) / "document.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_forgetting_a_chat_the_persona_never_read_leaves_the_persona_alone(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed_owner_chat(plug, BOB, "BRAVO")
    await seed_owner_chat(plug, CAROL, "CHARLIE")
    assert cycle(ds, FakeLlm()).persona == "refreshed"
    alice = await seed_alice(plug)  # arrives after the persona was built
    assert cycle(ds, FakeLlm(), target=("contact", alice)).refreshed == 1
    assert {c["chat_id"] for c in read_persona(ds)["chats"]} == {BOB, CAROL}
    before = snapshot_tree(persona_dir(ds))

    assert ds.forget(("contact", alice))["persona_reset"] is False
    assert snapshot_tree(persona_dir(ds)) == before

    # A v1 persona may have read any chat, so a v1 backup makes any contact forget erase it.
    (persona_dir(ds) / "v1-dossier.json").write_text("{}", encoding="utf-8")
    assert ds.forget(("contact", alice)) == {"existed": False, "revision": None, "note": False,
                                             "persona_reset": True}
    assert read_persona(ds)["document"] == "" and not (persona_dir(ds) / "v1-dossier.json").exists()


# ---------------------------------------------------------------------------
# The message check sees no note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_message_check_and_fact_hints_never_see_the_note_names_or_ids(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await seed(plug, ALICE, "mira_demo", ALICE_ROWS, first_name="Miraveta")
    entry = catalog_entry(plug, ALICE)
    ds.set_note(("contact", entry), "NOTE-CANARY: call after six.")
    assert cycle(ds, FakeLlm(), now=ago(40)).refreshed == 1
    with fake_secret_scope(KEYS):
        gate = use_gate(monkeypatch, ds, jev_reply(0.9, 0.1), jev_reply(0.1, 0.1))
        hints = use_hints(monkeypatch, ds, clairic_reply())
        await seed(plug, ALICE, "mira_demo", [(13, "in", "Deposit of 900 on 28 October", 30)], first_name="Miraveta")
        assert cycle(ds, FakeLlm()).refreshed == 1
        await seed(plug, ALICE, "mira_demo", [(14, "in", "Thanks!", -10)], first_name="Miraveta")
        assert cycle(ds, FakeLlm(), now=ago(-25)).deferred == 1

    assert len(gate.calls) == 2 and len(hints.calls) == 1
    for request in gate.calls:
        state = request["body"]["state"]
        assert set(state) == {"current_dossier", "new_messages"}
        assert all(set(m) <= WIRE_FIELDS for m in state["new_messages"])
    for request in [*gate.calls, *hints.calls]:
        data = request["data"].decode()
        for leaked in ("NOTE-CANARY", "owner_note", "Miraveta", "mira_demo", BUSINESS_ID, str(ALICE)):
            assert leaked not in data, leaked
    stat_gate = read_state(ds)["chats"][ds.chat_key(BUSINESS_ID, ALICE)]
    assert set(stat_gate) == {"signature", "dossier_id", "at", "recheck_at"}
    assert "NOTE-CANARY" not in ds.state_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Logs carry no private data
# ---------------------------------------------------------------------------

CANARY_CONN = "biz-quokka-canary"
CANARY_CHAT = 7314159265
CANARY_OLD_CHAT = 7271828182
CANARIES = {
    "text": "QUOKKA-TEXT",
    "note": "QUOKKA-NOTE",
    "document": "QUOKKA-DOC",
    "username": "quokka_canary",
    "first_name": "Quokkanaya",
    "v1_name": "Zebrina Canaryeva",
    "v1_fact": "ZEBRA-FACT",
    "corrupt": "ZEBRA-CORRUPT",
    "exception": "QUOKKA-EXC",
    "http_body": "QUOKKA-BODY",
    "chat_id": str(CANARY_CHAT),
    "old_chat_id": str(CANARY_OLD_CHAT),
    "connection": CANARY_CONN,
    "gateway_key": "gw-quokka-key-31337",
    "cf_token": "cf-quokka-token-31337",
    "cf_account": "c0ffee00c0ffee00c0ffee00c0ffee00",
}
CANARY_KEYS = {"AI_GATEWAY_API_KEY": CANARIES["gateway_key"], "CLOUDFLARE_API_TOKEN": CANARIES["cf_token"],
               "CLOUDFLARE_ACCOUNT_ID": CANARIES["cf_account"]}


def plant_v1_files(ds: ModuleType) -> None:
    """A v1 contact that migrates, one that cannot be read and one whose migration fails."""
    good = v1_contact(fact("f1", f"{CANARIES['v1_fact']}: likes long calls."), conn=CANARY_CONN, chat=CANARY_OLD_CHAT)
    good["contact"] = {"display_name": CANARIES["v1_name"], "aliases": ["@zebrina_demo"]}
    put(ds.contact_dir(CANARY_CONN, CANARY_OLD_CHAT), good)
    put(ds.contact_dir(CANARY_CONN, CANARY_OLD_CHAT + 1), f"{{{CANARIES['corrupt']}".encode())
    failing = ds.contact_dir(CANARY_CONN, CANARY_OLD_CHAT + 2)
    put(failing, v1_contact(fact("f1", CANARIES["v1_fact"]), conn=CANARY_CONN, chat=CANARY_OLD_CHAT + 2))
    (failing / "document.json").write_text(f"{{{CANARIES['corrupt']}", encoding="utf-8")


def check_records(records: list[logging.LogRecord]) -> None:
    for record in records:
        assert record.exc_info is None and record.exc_text is None and record.stack_info is None, record.getMessage()
        text = f"{record.getMessage()}\n{record.msg!r}\n{record.args!r}"
        for name, canary in CANARIES.items():
            assert canary not in text, (name, record.name, record.getMessage())


@pytest.mark.asyncio
async def test_logs_carry_no_message_text_note_document_name_id_or_key(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ds = dossiers
    text = CANARIES["text"]
    rows = [(1, "in", f"{text} question about the deposit on 28 October", 120)]
    rows += [(i, "out", f"{text} owner answer {i}", 120 - i) for i in range(2, 12)]
    await seed(plug, CANARY_CHAT, CANARIES["username"], rows, business_id=CANARY_CONN,
               first_name=CANARIES["first_name"])
    entry = catalog_entry(plug, CANARY_CHAT, business_id=CANARY_CONN)
    ds.set_note(("contact", entry), f"{CANARIES['note']}: prefers e-mail.")
    ds.set_note(("persona", None), f"{CANARIES['note']}: no emoji.")
    plant_v1_files(ds)

    def written(payload: dict[str, Any], kwargs: dict[str, Any]) -> str:
        if kwargs["purpose"] == "telesec_persona":
            return doc(f"## Style\n- {CANARIES['document']} short replies.")
        return doc(f"**{CANARIES['first_name']}.**\n\n## Notes\n- {CANARIES['document']} prefers e-mail.")

    async def new_message(message_id: int, minutes: int) -> None:
        await seed(plug, CANARY_CHAT, CANARIES["username"], [(message_id, "in", f"{text} message {message_id}",
                                                              minutes)], business_id=CANARY_CONN,
                   first_name=CANARIES["first_name"])

    body = json.dumps({"error": {"message": CANARIES["http_body"]}}).encode()
    caplog.set_level(logging.DEBUG)
    with fake_secret_scope(CANARY_KEYS):
        gate = use_gate(monkeypatch, ds, jev_reply(0.9, 0.2), (401, body), jev_reply(0.8, 0.1))
        hints = use_hints(monkeypatch, ds, clairic_reply(), (500, body), clairic_reply(), clairic_reply(),
                          clairic_reply())
        caplog.clear()
        # 1: migration, a first pass with fact hints, and the persona.
        report = cycle(ds, FakeLlm(written, written))
        assert (report.refreshed, report.persona, report.calls) == (1, "refreshed", 2)
        # 2: the check passes, fact hints fail, the writer raises.
        await new_message(20, 30)
        report = cycle(ds, FakeLlm(RuntimeError(f"{CANARIES['exception']} {CANARY_CONN}")))
        assert report.failures == [(f"{CANARY_CONN}:{CANARY_CHAT}", "RuntimeError")]
        # 3: the check fails, fact hints pass, the reply has no end marker.
        await new_message(21, -5)
        report = cycle(ds, FakeLlm(f"{CANARIES['document']} {text} cut off"), now=ago(-20))
        assert report.failures == [(f"{CANARY_CONN}:{CANARY_CHAT}", "no_end_marker")]
        # 4: everything works.
        report = cycle(ds, FakeLlm(written), now=ago(-25))
        assert (report.calls, report.checked, report.failed) == (1, 1, 0)  # the same text: checked
        # 5: the host trust gate denies the call.
        await new_message(22, -30)
        report = cycle(ds, FakeLlm(PluginLlmTrustError(f"{CANARIES['exception']} denied")), now=ago(-45),
                       target=("contact", entry))
        assert report.outcome == "aborted_trust"
        # 6: the persona rebuild fails.
        report = cycle(ds, FakeLlm(RuntimeError(f"{CANARIES['exception']} persona")), now=ago(-50),
                       target=("persona", None))
        assert report.failures == [("persona", "RuntimeError")]
    assert (len(gate.calls), len(hints.calls)) == (3, 5)
    assert (ds.contact_dir(CANARY_CONN, CANARY_OLD_CHAT) / "document.json").exists()

    messages = [r.getMessage() for r in caplog.records]
    for expected in ("dossier migration failed", "old dossier file could not be read", "dossier migration migrated=",
                     "dossier call purpose=telesec_dossier", "dossier call purpose=telesec_persona",
                     "model check failed kind=http status=401", "fact extraction failed kind=http status=500",
                     "dossier refresh failed", "dossier persona refresh failed", "trust gate denied",
                     "dossier target="):
        assert any(expected in m for m in messages), expected
    assert any(r.levelno == logging.DEBUG for r in caplog.records)
    check_records(caplog.records)
