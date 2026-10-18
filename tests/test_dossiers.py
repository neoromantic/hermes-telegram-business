"""Issue #17 acceptance regressions; all histories and LLMs are synthetic."""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import multiprocessing
import os
import stat
import sys
import threading
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 18, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path / "home"
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    name = "dossier_test_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    for key in list(os.environ):
        if key.startswith("HERMES_TELEGRAM_BUSINESS_DOSSIER_") or key in {
            "TG_BUSINESS_VOICE_CLEANUP_PROVIDER",
            "TG_BUSINESS_VOICE_CLEANUP_MODEL",
        }:
            monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_DOSSIER_ENABLE", "1")
    monkeypatch.setattr(module._history_support, "_utcnow", lambda: NOW)
    yield module
    module._history_support.reset_in_memory_caches()


def contact(chat=1, *, connection="test-connection", chat_type="private", name="Alice"):
    return {
        "business_connection_id": connection,
        "chat_id": chat,
        "chat_type": chat_type,
        "current_profile": {"first_name": name, "username": name.lower()},
        "aliases": [],
    }


@pytest.fixture
def source(plugin, monkeypatch):
    data = {}
    contacts = []
    history = plugin._history_support
    monkeypatch.setattr(
        history,
        "_filter_catalog_entries",
        lambda **kwargs: [
            e
            for e in contacts
            if all(
                v is None
                or str(e.get({"business_connection_id": "business_connection_id", "chat_id": "chat_id"}[k])) == str(v)
                for k, v in kwargs.items()
            )
        ],
    )
    monkeypatch.setattr(
        history, "canonical_text_messages", lambda e: (copy.deepcopy(data[e["chat_id"]]), NOW - timedelta(hours=1))
    )

    def add(chat=1, *, received="Let's meet Tuesday.", sent="Sure, Tuesday works!", chat_type="private", name="Alice"):
        entry = contact(chat, chat_type=chat_type, name=name)
        contacts.append(entry)
        data[chat] = [message(1, received, "inbound"), message(2, sent, "outbound")]
        return entry

    return SimpleNamespace(data=data, contacts=contacts, add=add)


def message(mid, text="Synthetic text", direction="outbound", *, date=None, deleted=False):
    return {
        "ref": f"m{mid}",
        "text": text,
        "direction": direction,
        "date": (date or NOW - timedelta(hours=1)).isoformat(),
        "deleted": deleted,
    }


def fact(ref="m1", quote="Let's meet Tuesday.", text="Meeting proposed for Tuesday.", **extra):
    return {
        "section": "shared_facts",
        "kind": "observed",
        "text": text,
        "sources": [{"ref": ref, "quote": quote}],
        **extra,
    }


class FakeLLM:
    def __init__(self, response=None, callback=None):
        self.response = response if response is not None else {"facts": [fact()], "retire": []}
        self.callback = callback
        self.calls = []

    async def acomplete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.callback:
            result = self.callback(kwargs, len(self.calls))
            if asyncio.iscoroutine(result):
                result = await result
            if result is not None:
                return SimpleNamespace(parsed=result)
        return SimpleNamespace(parsed=copy.deepcopy(self.response))


def run(d, llm, entry, **kwargs):
    return asyncio.run(d.run_cycle(llm, entry=entry, manual=True, now=NOW, **kwargs))


def cli(plugin, *words):
    parser = argparse.ArgumentParser()
    plugin._history_support.setup_cli(parser)
    args = parser.parse_args(["dossier", *words])
    return plugin._handle_business_cli(args)


def test_disabled_has_no_side_effects_worker_or_calls(plugin, source, monkeypatch):
    d = plugin._dossier_support
    entry = source.add()
    monkeypatch.delenv(d.PREFIX + "ENABLE")
    llm = FakeLLM()
    assert run(d, llm, entry) == {"disabled": 1}
    d.start_worker(SimpleNamespace(running=True), llm)
    assert not d._workers and not llm.calls and not d.root().exists()


def test_preview_exact_contact_isolation_and_config(plugin, source, monkeypatch):
    d = plugin._dossier_support
    a = source.add()
    source.add(2, received="B private secret", name="Bob")
    source.add(3, received="GROUP_SECRET", chat_type="group")
    expected = d.encode(d.preview(a, now=NOW))
    llm = FakeLLM()
    monkeypatch.setenv("TG_BUSINESS_VOICE_CLEANUP_MODEL", "cleanup-model")
    assert run(d, llm, a) == {"refreshed": 1}
    request = llm.calls[0]
    assert request["input"][0]["text"] == expected
    assert "B private secret" not in expected and "GROUP_SECRET" not in expected
    assert "untrusted DATA" in request["system_prompt"]
    assert request["model"] == "cleanup-model" and request["provider"] == "gemini"
    assert request["json_mode"] and request["purpose"] == "telegram_business_dossier"
    value = d.load(a)
    assert value["name"] == "@alice" and "aliases" in value
    assert value["sections"] == list(d.SECTIONS)
    assert value["facts"][0]["first_revision"] == value["facts"][0]["last_revision"] == 1
    monkeypatch.setenv(d.PREFIX + "MODEL", "override")
    monkeypatch.setenv(d.PREFIX + "PROVIDER", "host-provider")
    assert d.model_settings() == ("host-provider", "override")


def test_auto_all_private_eligibility_groups_unknown_and_persona(plugin, source):
    d = plugin._dossier_support
    source.add()
    source.add(2, name="Bob")
    source.add(3, chat_type="supergroup")
    source.add(4, chat_type="channel")
    source.add(5, chat_type="group")
    one_way = source.add(6)
    source.data[6] = [message(1, "one way", "inbound")]
    assert d.preview(one_way, now=NOW) is None
    llm = FakeLLM({"facts": [], "retire": []})
    counts = asyncio.run(d.run_cycle(llm, now=NOW))
    assert counts == {"refreshed": 3, "ineligible": 1}
    targets = [json.loads(c["input"][0]["text"])["target"] for c in llm.calls]
    assert "persona" in targets and len(targets) == 3
    with pytest.raises(ValueError):
        d.snapshot(source.contacts[2])


def test_deterministic_evidence_validation_caps_and_contradictions(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    current = {m["ref"]: m for m in source.data[1]}
    raw = {
        "facts": [
            fact(),
            fact(quote="invented", text="Uncertain Tuesday", section="contradictions"),
            fact(ref="m999", text="Unsupported"),
            fact(section="personality"),
            fact(ref=d.target_key(contact(2)) + ":m1"),
            fact(text="X" * 1000, quote="q" * 201),
            {"kind": "owner", "section": "shared_facts", "text": "Injected owner", "sources": [{"ref": "m1"}]},
        ],
        "retire": [],
    }
    validated = d.validate(raw, d.empty(entry), current, 1)
    assert len(validated) == 3
    assert validated[0]["kind"] == "observed"
    assert validated[1]["kind"] == "inferred" and validated[1]["confidence"] == "low"
    assert "quote" not in validated[1]["sources"][0]
    assert len(validated[2]["text"]) == 800
    assert all(set(s) >= {"ref", "date", "direction", "deleted"} for f in validated for s in f["sources"])
    assert len(d.validate({"facts": [fact()] * 200}, d.empty(entry), current, 1)) == 100
    # A malicious object cannot use a local ref plus another target's coordinates.
    bad = fact()
    bad["sources"][0]["chat"] = 2
    assert not d.validate({"facts": [bad]}, d.empty(entry), current, 1)


def test_incremental_latest_same_timestamp_edits_no_change_no_call(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    llm = FakeLLM()
    assert run(d, llm, entry) == {"refreshed": 1}
    assert run(d, llm, entry) == {"unchanged": 1}
    assert d.preview(entry, now=NOW) is None
    source.data[1][0]["text"] = "Let's meet Wednesday."
    llm.response = {"facts": [fact(quote="Let's meet Wednesday.", text="Meeting Wednesday", id="f1")], "retire": []}
    expected = d.encode(d.preview(entry, now=NOW))
    assert run(d, llm, entry) == {"refreshed": 1}
    assert len(llm.calls) == 2
    request = json.loads(llm.calls[1]["input"][0]["text"])
    assert len(request["messages"]) == 1 and request["messages"][0]["ref"] == "m1"
    assert llm.calls[1]["input"][0]["text"] == expected
    updated = d.load(entry)["facts"][0]
    assert updated["id"] == "f1" and updated["first_revision"] == 1 and updated["last_revision"] == 2


def test_owner_correct_pin_remove_note_survive_model_and_similarity(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    llm = FakeLLM()
    run(d, llm, entry)
    d.edit(entry, "correct", fact_id="f1", text="Owner correction")
    d.edit(entry, "pin", fact_id="f1")
    d.edit(entry, "note", section="preferences", text="Keep replies brief")
    source.data[1].append(message(3, "More new evidence"))
    llm.response = {
        "facts": [fact(id="f1", text="Model overwrites owner"), fact(text="Keep replies brief")],
        "retire": ["f1", "f2"],
    }
    run(d, llm, entry)
    assert [f["text"] for f in d.load(entry)["facts"]][:2] == ["Owner correction", "Keep replies brief"]
    request = json.loads(llm.calls[-1]["input"][0]["text"])
    assert {f["id"] for f in request["owner_read_only"]} == {"f1", "f2"}
    d.edit(entry, "unpin", fact_id="f1")
    assert not d.load(entry)["facts"][0]["pinned"]
    d.edit(entry, "remove", fact_id="f1")
    source.data[1].append(message(4, "Next update"))
    llm.response = {"facts": [fact(text="Owner correction!"), fact(text="Owner correction")], "retire": []}
    run(d, llm, entry)
    assert all(f["text"] != "Owner correction!" for f in d.load(entry)["facts"])
    assert json.loads(llm.calls[-1]["input"][0]["text"])["do_not_readd"][0]["text"] == "Owner correction"
    assert any(
        c["reason"] == "explicit owner instruction" for r in d.revision_log(entry, d.load(entry)) for c in r["changes"]
    )


def test_show_stale_no_write_pruning_unsupported_and_source_gone(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    llm = FakeLLM()
    run(d, llm, entry)
    path = d.directory(entry) / "dossier.json"
    before = path.read_bytes()
    source.data[1][0]["text"] = "Changed source"
    assert d.inspect_dossier(entry)["facts"][0]["status"] == "stale"
    assert path.read_bytes() == before
    source.data[1] = source.data[1][1:]
    assert d.inspect_dossier(entry)["facts"][0]["status"] == "unsupported"
    assert path.read_bytes() == before
    llm.response = {"facts": [], "retire": []}
    run(d, llm, entry)
    assert not d.load(entry)["facts"]
    # A protected fact survives the identical pruning path.
    source.data[1].insert(0, message(1, "Let's meet Tuesday.", "inbound"))
    llm.response = {"facts": [fact()], "retire": []}
    run(d, llm, entry)
    fid = d.load(entry)["facts"][0]["id"]
    d.edit(entry, "pin", fact_id=fid)
    source.data[1] = source.data[1][1:]
    run(d, llm, entry)
    kept = d.load(entry)["facts"][0]
    assert kept["status"] == "source gone" and kept["sources"] == []


def test_deletion_label_and_changed_fact_unconfirmed_is_invalidated(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    llm = FakeLLM()
    run(d, llm, entry)
    source.data[1][0]["deleted"] = True
    run(d, llm, entry)
    assert d.load(entry)["facts"][0]["sources"][0]["deleted"] is True
    source.data[1][0]["text"] = "An entirely changed claim"
    llm.response = {"facts": [], "retire": []}
    run(d, llm, entry)
    assert not d.load(entry)["facts"]
    events = d.revision_log(entry, d.load(entry))[-1]["changes"]
    assert any(e["action"] == "invalidated" and "not re-supported" in e["reason"] for e in events)


def test_backfill_days_messages_bounded_chunks_and_failure_mid_batch(plugin, source, monkeypatch):
    d = plugin._dossier_support
    entry = source.add()
    source.data[1] = [
        message(0, "ANCIENT", "inbound", date=NOW - timedelta(days=91)),
        *[message(i, f"Synthetic {i}", "inbound" if i % 2 else "outbound") for i in range(1, 121)],
    ]
    monkeypatch.setenv(d.PREFIX + "BACKFILL_MESSAGES", "110")
    calls = []

    def fail_second(kwargs, count):
        calls.extend(json.loads(kwargs["input"][0]["text"])["messages"])
        if count == 2:
            raise RuntimeError("MODEL_SECRET shouldn't be printed")
        return {"facts": [], "retire": []}

    llm = FakeLLM(callback=fail_second)
    assert run(d, llm, entry) == {"failed": 1}
    assert not d.load(entry)["last_refresh"]
    assert len(calls) == 100
    llm = FakeLLM({"facts": [], "retire": []})
    assert run(d, llm, entry) == {"refreshed": 1}
    assert len(llm.calls) == 3
    batches = [json.loads(c["input"][0]["text"])["messages"] for c in llm.calls]
    assert sum(map(len, batches)) == 110
    assert all(len(b) <= 50 and sum(len(d.encode(m)) for m in b) <= d.CHUNK_CHARS for b in batches)
    assert "ANCIENT" not in str(batches)
    value = d.load(entry)
    assert value["coverage"]["backfill_cut"] and value["coverage"]["messages_processed"] == 110
    assert run(d, llm, entry) == {"unchanged": 1}


def test_chunk_character_bound_and_oversized_input(plugin):
    d = plugin._dossier_support
    batches = d.chunks([message(i, "x" * 4096) for i in range(50)])
    assert len(batches) > 1 and all(sum(len(d.encode(m)) for m in batch) <= d.CHUNK_CHARS for batch in batches)
    with pytest.raises(ValueError):
        d.chunks([message(1, "x" * 25000)])


def test_retry_per_contact_no_text_in_logs_or_cli_errors(plugin, source, caplog, capsys):
    d = plugin._dossier_support
    a = source.add()
    source.add(2, name="Bob")

    def fail_a(kwargs, _):
        if json.loads(kwargs["input"][0]["text"])["target"] == d.target_key(a):
            raise ValueError("SECRET_TEXT_MODEL_OUTPUT")
        return {"facts": [], "retire": []}

    llm = FakeLLM(callback=fail_a)
    result = asyncio.run(d.run_cycle(llm, now=NOW))
    assert result == {"failed": 1, "refreshed": 2}
    assert "SECRET_TEXT_MODEL_OUTPUT" not in caplog.text
    assert "Let's meet" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    llm.callback = None
    assert run(d, llm, a) == {"refreshed": 1}
    plugin._llm_facade = llm
    assert cli(plugin, "correct", "--chat", "1", "--fact", "bad", "--text", "SECRET_TEXT_MODEL_OUTPUT") == 1
    assert "SECRET_TEXT_MODEL_OUTPUT" not in capsys.readouterr().out


def test_persona_unique_cross_chat_refs_sent_only_cadence_and_sources(plugin, source):
    d = plugin._dossier_support
    source.add()
    source.add(2, sent="cheers", name="Bob")
    source.add(3, sent="GROUP_SENT", chat_type="group")
    seen = d.preview(None, now=NOW)
    assert len(seen["messages"]) == 2
    refs = [m["ref"] for m in seen["messages"]]
    assert len(set(refs)) == 2 and all(ref.endswith(":m2") for ref in refs)
    raw = {
        "facts": [
            fact(ref=refs[0], quote="Sure, Tuesday works!", section="tone", text="Short informal responses"),
            fact(ref="m2", quote="cheers", section="tone"),
        ],
        "retire": [],
    }
    llm = FakeLLM(raw)
    assert asyncio.run(d.run_cycle(llm, persona=True, now=NOW)) == {"refreshed": 1}
    assert len(d.load(None)["facts"]) == 1
    source.data[1].append(message(3, "one new sent"))
    assert asyncio.run(d.run_cycle(llm, persona=True, now=NOW + timedelta(hours=23))) == {"not_due": 1}
    assert asyncio.run(d.run_cycle(llm, persona=True, now=NOW + timedelta(hours=24))) == {"refreshed": 1}
    assert len(llm.calls) == 2
    for i in range(4, 24):
        source.data[1].append(message(i, f"Sent {i}"))
    assert asyncio.run(d.run_cycle(llm, persona=True, now=NOW + timedelta(hours=25))) == {"refreshed": 1}
    inspected = d.inspect_dossier(None)
    assert inspected["summary"] and inspected["sections"] == list(d.PERSONA_SECTIONS)
    source.data[1][1]["text"] = "Edited sent message"
    assert asyncio.run(d.run_cycle(llm, persona=True, now=NOW + timedelta(hours=25))) == {"refreshed": 1}


@pytest.mark.asyncio
async def test_rolling_rate_limit_persisted_across_cycles_and_failures(plugin):
    d = plugin._dossier_support
    clock = [1000.0]
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = d.RateLimiter(100, wall_clock=lambda: clock[0], sleep=sleep)
    stamps = []
    for _ in range(45):
        await limiter.wait()
        stamps.append(clock[0])
    assert len(sleeps) == 2 and stamps[20] >= 1060 and stamps[40] >= 1120
    for stamp in stamps:
        assert sum(stamp <= other < stamp + 60 for other in stamps) <= 20
    another = d.RateLimiter(1, wall_clock=lambda: clock[0], sleep=sleep)
    await another.wait()
    assert clock[0] >= 1180


@pytest.mark.asyncio
async def test_quiet_period_and_manual_override(plugin, source, monkeypatch):
    d = plugin._dossier_support
    a = source.add()
    monkeypatch.setattr(
        plugin._history_support,
        "canonical_text_messages",
        lambda e: (source.data[e["chat_id"]], NOW - timedelta(minutes=9)),
    )
    llm = FakeLLM()
    assert await d.run_cycle(llm, entry=a, now=NOW) == {"busy": 1}
    assert not llm.calls
    assert await d.run_cycle(llm, entry=a, now=NOW, manual=True) == {"refreshed": 1}


@pytest.mark.asyncio
async def test_worker_delayed_gateway_factory_only_registration_cli(plugin, source, monkeypatch):
    d = plugin._dossier_support
    source.add()
    recorded = []
    ctx = SimpleNamespace(
        llm=FakeLLM(),
        register_telegram_handler=lambda fn: recorded.append(fn),
        register_hook=lambda *args: None,
        register_cli_command=lambda **kw: None,
        register_skill=lambda *args: None,
    )
    monkeypatch.setattr(
        plugin._history_support, "run_startup_maintenance", lambda: SimpleNamespace(cap_exceeded=False, warnings=[])
    )
    plugin.register(ctx)
    assert not d._workers and not ctx.llm.calls
    app = SimpleNamespace(running=True, add_handler=lambda *a, **k: None)
    sleeps = []
    real_sleep = asyncio.sleep
    ready = asyncio.Event()
    resume = asyncio.Event()

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            ready.set()
            await resume.wait()
        else:
            await real_sleep(0)
            app.running = False

    monkeypatch.setattr(d.asyncio, "sleep", sleep)
    # This is the real gateway connect factory; no media/messages are supplied.
    recorded[0](app, SimpleNamespace())
    await ready.wait()
    assert sleeps == [180] and not ctx.llm.calls
    key = str(d.root())
    task = d._workers[key]
    d.start_worker(app, ctx.llm)
    assert d._workers[key] is task
    resume.set()
    await task
    assert sleeps == [180, 1800] and len(ctx.llm.calls) == 2 and not d._workers


@pytest.mark.asyncio
async def test_owner_correction_concurrent_snapshot_rebase_and_cycle_lock(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run_llm = FakeLLM()
    assert await d.run_cycle(run_llm, entry=entry, now=NOW, manual=True) == {"refreshed": 1}
    source.data[1].append(message(3, "new text"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def callback(kwargs, count):
        entered.set()
        await release.wait()
        return {"facts": [fact(id="f1", text="Generated overwrite")], "retire": ["f1"]}

    llm = FakeLLM(callback=callback)
    task = asyncio.create_task(d.run_cycle(llm, entry=entry, now=NOW, manual=True))
    await entered.wait()
    assert await d.run_cycle(llm, entry=entry, now=NOW) == {"locked": 1}
    await asyncio.to_thread(d.edit, entry, "correct", fact_id="f1", text="Concurrent owner correction")
    await asyncio.to_thread(d.edit, entry, "note", section="preferences", text="Concurrent note")
    release.set()
    assert await task == {"refreshed": 1}
    value = d.load(entry)
    assert [f["text"] for f in value["facts"]] == ["Concurrent owner correction", "Concurrent note"]
    revisions = [line["revision"] for line in d.revision_log(entry, value)]
    assert revisions == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_forget_during_first_generation_prevents_resurrection(plugin, source):
    d = plugin._dossier_support
    entry = source.add()

    async def callback(kwargs, count):
        d.forget(entry)

    assert await d.run_cycle(FakeLLM(callback=callback), entry=entry, manual=True, now=NOW) == {"reset": 1}
    assert not (d.directory(entry) / "dossier.json").exists()


def test_permissions_atomic_snapshots_locking_and_no_archive(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    long_text = "Long canonical message " * 100
    source.data[1][0]["text"] = long_text
    llm = FakeLLM({"facts": [fact(quote="Long canonical message ", text="Synthetic fact")], "retire": []})
    previous = os.umask(0)
    try:
        run(d, llm, entry)
    finally:
        os.umask(previous)
    for path in [d.root(), *d.root().rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.is_file():
            assert long_text not in path.read_text()
    assert {p.name for p in d.directory(entry).iterdir()} == {"dossier.json", "changes.jsonl"}
    errors = []

    def reader():
        try:
            for _ in range(30):
                assert d.load(entry)["revision"] >= 1
        except Exception as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=reader)
    thread.start()
    for _ in range(10):
        d.edit(entry, "pin", fact_id="f1")
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    assert not list(d.directory(entry).glob(".dossier-*"))


def _check_flock(path, result):
    import fcntl

    with open(path, "r+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.put("locked")
        else:
            result.put("unlocked")


def test_cycle_lock_is_process_safe(plugin):
    d = plugin._dossier_support
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    with d.lock(d.root() / ".cycle.lock"):
        process = context.Process(target=_check_flock, args=(str(d.root() / ".cycle.lock"), result))
        process.start()
        assert result.get(timeout=10) == "locked"
        process.join(timeout=10)
        assert process.exitcode == 0


def test_revision_log_recovers_interrupted_dual_write(plugin, source, monkeypatch):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    real_atomic = d.atomic

    def fail_snapshot(path, text):
        if path.name == "dossier.json":
            raise OSError("synthetic disk failure")
        return real_atomic(path, text)

    monkeypatch.setattr(d, "atomic", fail_snapshot)
    with pytest.raises(OSError):
        d.edit(entry, "pin", fact_id="f1")
    monkeypatch.setattr(d, "atomic", real_atomic)
    # Physical log contains an uncommitted revision, never visible to readers.
    physical = [json.loads(line) for line in (d.directory(entry) / "changes.jsonl").read_text().splitlines()]
    assert [r["revision"] for r in physical] == [1, 2]
    assert [r["revision"] for r in d.revision_log(entry, d.load(entry))] == [1]
    assert not d.load(entry)["facts"][0].get("pinned")
    d.edit(entry, "unpin", fact_id="f1")
    assert [r["revision"] for r in d.revision_log(entry, d.load(entry))] == [1, 2]
    # Missing mirror tail is recoverable from the authoritative snapshot.
    (d.directory(entry) / "changes.jsonl").write_text(d.encode(physical[0]) + "\n")
    assert [r["revision"] for r in d.revision_log(entry, d.load(entry))] == [1, 2]


def test_cli_owner_workflow_export_follow_sources_and_forget(plugin, source, capsys):
    d = plugin._dossier_support
    source.add()
    plugin._llm_facade = FakeLLM()
    assert cli(plugin, "status") == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True
    assert cli(plugin, "preview", "--contact", "@alice") == 0
    preview = capsys.readouterr().out.strip()
    assert cli(plugin, "run", "--contact", "@alice") == 0
    capsys.readouterr()
    assert plugin._llm_facade.calls[0]["input"][0]["text"] == preview
    for words in [
        ("list",),
        ("show", "--contact", "@alice"),
        ("show", "--contact", "@alice", "--fact", "f1", "--format", "json"),
        ("correct", "--contact", "@alice", "--fact", "f1", "--text", "Owner correction"),
        ("pin", "--contact", "@alice", "--fact", "f1"),
        ("unpin", "--contact", "@alice", "--fact", "f1"),
        ("note", "--contact", "@alice", "--section", "preferences", "--text", "Be brief"),
        ("changes", "--contact", "@alice"),
        ("export", "--chat", "1", "--connection", "test-connection", "--format", "markdown"),
        ("export", "--contact", "@alice", "--format", "json"),
        ("remove", "--contact", "@alice", "--fact", "f1"),
        ("run", "--persona"),
        ("show", "--persona"),
        ("forget", "--persona"),
    ]:
        assert cli(plugin, *words) == 0, words
        output = capsys.readouterr().out
        if "--fact" in words and words[0] == "show":
            assert json.loads(output)["canonical_sources"][0]["text"] == "Let's meet Tuesday."
        if words[0] == "export" and words[-1] == "markdown":
            assert "## preferences" in output and "Owner correction" in output
    canonical_before = copy.deepcopy(source.data)
    assert cli(plugin, "forget", "--contact", "@alice") == 0
    assert not (d.directory(source.contacts[0]) / "changes.jsonl").exists()
    assert source.data == canonical_before
    assert cli(plugin, "run") == 0
    assert cli(plugin, "forget", "--all") == 0
    assert not list(d.root().glob("*/dossier.json"))


def test_cli_resolver_ambiguous_unknown_group_and_conflicting_flags(plugin, source):
    source.add()
    source.add(2)  # same catalog name: ambiguity must propagate safely.
    source.add(3, name="Group", chat_type="group")
    for words in [
        ("show", "--contact", "Alice"),
        ("show", "--contact", "Nobody"),
        ("show", "--chat", "3"),
        ("show", "--persona", "--connection", "test-connection"),
    ]:
        assert cli(plugin, *words) == 1
    with pytest.raises(SystemExit):
        cli(plugin, "show", "--chat", "1", "--contact", "Alice")


def seed_records(plugin, *records):
    h = plugin._history_support
    for record in records:
        path = h._history_chat_dir_path(record["business_connection_id"], record["chat_id"])
        h._ensure_history_chat_dir(path)
        h._ensure_private_file(h._root_lock_path())
        h._ensure_private_file(h._chat_lock_path(path))
        h._append_record(path, record)


def event(
    plugin,
    mid=1,
    text="hello",
    direction="inbound",
    *,
    kind="message.created",
    observed=None,
    date=None,
    chat=1,
    connection="test-connection",
    **extra,
):
    return plugin._history_support._build_event(
        event_type=kind,
        source="synthetic",
        observed_at=observed or NOW - timedelta(hours=1),
        telegram_update_id=uuid.uuid4().hex,
        business_connection_id=connection,
        chat_id=chat,
        message_id=mid,
        message_at=date,
        sender_id=100,
        direction=direction,
        reply_to_message_id=None,
        text=text,
        chat_profile={"type": "private", "first_name": "Alice", "username": "alice"},
        **extra,
    )


def test_real_canonical_history_latest_edit_dedup_send_time_unknown_deletion_policy(plugin):
    h = plugin._history_support
    entry = contact()
    older_send = NOW - timedelta(days=2)
    create = event(plugin, 1, "Original", date=older_send)
    late = event(plugin, 2, "sent later", date=NOW - timedelta(days=1))
    edit = event(plugin, 1, "Latest edited", kind="message.edited", observed=NOW, date=older_send)
    unknown = event(plugin, 3, "Unknown source", direction="unknown", observed=NOW - timedelta(hours=3))
    deleted = event(plugin, 2, text=None, kind="message.deleted", observed=NOW)
    duplicate = event(plugin, 4, "Replacement duplicate", direction="outbound", date=older_send)
    deletion_duplicate = event(plugin, 4, text=None, kind="message.deleted", observed=NOW)
    classify = event(
        plugin,
        4,
        text=None,
        kind="deletion.classified",
        observed=NOW,
        deleted_event_id=deletion_duplicate["event_id"],
        classification="likely_duplicate",
    )
    correction = event(plugin, 5, "Replacement correction", direction="outbound", date=older_send)
    deletion_correction = event(plugin, 5, text=None, kind="message.deleted", observed=NOW)
    classify_correction = event(
        plugin,
        5,
        text=None,
        kind="deletion.classified",
        observed=NOW,
        deleted_event_id=deletion_correction["event_id"],
        classification="likely_correction",
    )
    seed_records(
        plugin,
        create,
        late,
        edit,
        create,
        unknown,
        deleted,
        duplicate,
        deletion_duplicate,
        classify,
        correction,
        deletion_correction,
        classify_correction,
    )
    actual, activity = h.canonical_text_messages(entry)
    assert [m["ref"] for m in actual] == ["m1", "m2", "m3"]
    assert actual[0]["text"] == "Latest edited" and actual[1]["deleted"] is True
    assert actual[2]["direction"] == "unknown" and actual[2]["date"] == h._isoformat_utc(NOW - timedelta(hours=3))
    assert activity == NOW
    # Reading never repairs/appends canonical data, even when duplicate events exist.
    path = h._entry_chat_dir(entry)
    before = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
    h.canonical_text_messages(entry)
    assert {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()} == before


def test_real_fixture_history_worker_cli_and_catalog_names_only(plugin, capsys):
    h, d = plugin._history_support, plugin._dossier_support
    seed_records(
        plugin,
        event(plugin, 1, "Let's meet Tuesday.", date=NOW - timedelta(hours=2)),
        event(plugin, 2, "Sure, Tuesday works!", "outbound", date=NOW - timedelta(hours=1)),
    )
    h.rebuild_contact_catalog()
    llm = FakeLLM()
    assert asyncio.run(d.run_cycle(llm, now=NOW)) == {"refreshed": 2}
    assert len(llm.calls) == 2
    plugin._llm_facade = llm
    assert cli(plugin, "show", "--contact", "@alice", "--fact", "f1", "--format", "json") == 0
    assert json.loads(capsys.readouterr().out)["canonical_sources"][0]["text"] == "Let's meet Tuesday."
    # An actual append with identical observation and send timestamps is still new.
    seed_records(plugin, event(plugin, 3, "same timestamp new message", "outbound", date=NOW - timedelta(hours=1)))
    h.rebuild_contact_catalog()
    assert cli(plugin, "run", "--contact", "@alice") == 0
    request = json.loads(llm.calls[-1]["input"][0]["text"])
    assert [m["ref"] for m in request["messages"]] == ["m3"]


def test_preview_and_show_are_nonmutating_even_metadata(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    assert not d.root().exists()
    assert d.preview(entry, now=NOW)["messages"]
    assert not d.root().exists()
    run(d, FakeLLM(), entry)
    before = {
        p: (p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.read_bytes() if p.is_file() else None)
        for p in [d.root(), *d.root().rglob("*")]
    }
    source.data[1][0]["text"] = "A changed source"
    assert d.inspect_dossier(entry)["facts"][0]["status"] == "stale"
    assert d.preview(entry, now=NOW)["messages"][0]["text"] == "A changed source"
    assert before == {
        p: (p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.read_bytes() if p.is_file() else None) for p in before
    }


def test_canonical_stats_cli_and_dossiers_use_same_message_view(plugin, capsys):
    h, d = plugin._history_support, plugin._dossier_support
    incoming = event(plugin, 1, "Let's meet Tuesday.", date=NOW - timedelta(hours=2))
    outgoing = event(plugin, 2, "Sure, Tuesday works!", "outbound", date=NOW - timedelta(hours=1))
    deletion = event(plugin, 1, None, kind="message.deleted")
    seed_records(plugin, incoming, outgoing, deletion)
    h.rebuild_contact_catalog()
    parser = argparse.ArgumentParser()
    h.setup_cli(parser)
    assert h.handle_cli(parser.parse_args(["history", "stats", "--messages", "--contact", "@alice"])) == 0
    counts = json.loads(capsys.readouterr().out)
    request = d.preview(h._resolve_contact_entry(business_connection_id=None, contact="alice"), now=NOW)
    assert counts == {
        "chats": 1,
        "text_messages": len(request["messages"]),
        "inbound": 1,
        "outbound": 1,
        "unknown": 0,
        "deleted": 1,
    }


def test_retention_orphan_reconciliation_and_explicit_id_owner_access(plugin, capsys):
    h, d = plugin._history_support, plugin._dossier_support
    seed_records(plugin, event(plugin, 1, "Let's meet Tuesday."), event(plugin, 2, "Sure, Tuesday works!", "outbound"))
    h.rebuild_contact_catalog()
    entry = h._resolve_chat_entry(business_connection_id=None, chat_id="1")
    llm = FakeLLM()
    run(d, llm, entry)
    d.edit(entry, "pin", fact_id="f1")
    for path in h._iter_history_files(h._entry_chat_dir(entry)):
        path.unlink()
    h.rebuild_contact_catalog()
    assert h._filter_catalog_entries() == []
    assert len(d.cycle_entries()) == 1
    llm.response = {"facts": [], "retire": []}
    assert asyncio.run(d.run_cycle(llm, now=NOW)) == {"refreshed": 1, "ineligible": 1}
    plugin._llm_facade = llm
    assert (
        cli(plugin, "show", "--chat", "1", "--connection", "test-connection", "--fact", "f1", "--format", "json") == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["fact"]["status"] == "source gone"
    assert result["canonical_sources"] == [{"ref": "m1", "source_gone": True}]
    assert cli(plugin, "forget", "--chat", "1", "--connection", "test-connection") == 0
    assert not (d.directory(entry) / "dossier.json").exists()


@pytest.mark.asyncio
async def test_forget_all_cancels_inflight_first_target_and_rest_of_cycle(plugin, source):
    d = plugin._dossier_support
    source.add()
    source.add(2, name="Bob")

    async def callback(kwargs, count):
        d.forget_all()

    llm = FakeLLM(callback=callback)
    assert await d.run_cycle(llm, now=NOW) == {"reset": 1}
    assert len(llm.calls) == 1
    assert not list(d.root().glob("*/dossier.json"))
    llm.callback = None
    assert await d.run_cycle(llm, now=NOW) == {"refreshed": 3}


@pytest.mark.asyncio
async def test_remove_during_llm_call_filters_returned_paraphrase(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    await d.run_cycle(FakeLLM(), entry=entry, now=NOW, manual=True)
    source.data[1].append(message(3, "next update"))

    def callback(kwargs, count):
        d.edit(entry, "remove", fact_id="f1")
        return {"facts": [fact(text="Meeting proposed for Tuesday!")], "retire": []}

    assert await d.run_cycle(FakeLLM(callback=callback), entry=entry, now=NOW, manual=True) == {"refreshed": 1}
    assert not d.load(entry)["facts"] and d.load(entry)["removed"]


@pytest.mark.asyncio
async def test_worker_reconnect_replaces_old_task_and_cancellation_cleans_up(plugin, monkeypatch):
    d = plugin._dossier_support
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def sleep(seconds):
        started.set()
        await blocker.wait()

    monkeypatch.setattr(d.asyncio, "sleep", sleep)
    old_app = SimpleNamespace(running=True)
    d.start_worker(old_app, FakeLLM())
    await started.wait()
    key = str(d.root())
    old = d._workers[key]
    new_app = SimpleNamespace(running=True)
    d.start_worker(new_app, FakeLLM())
    new = d._workers[key]
    assert old is not new
    with pytest.raises(asyncio.CancelledError):
        await old
    assert d._workers[key] is new
    new.cancel()
    with pytest.raises(asyncio.CancelledError):
        await new
    assert not d._workers and not d._worker_apps


def test_initial_default_500_limit_and_baseline_older_messages_not_resent(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    source.data[1] = [
        message(i, f"Message {i}", "inbound" if i % 2 else "outbound", date=NOW - timedelta(minutes=600 - i))
        for i in range(600)
    ]
    llm = FakeLLM({"facts": [], "retire": []})
    assert len(d.preview(entry, now=NOW)["messages"]) == 50
    assert run(d, llm, entry) == {"refreshed": 1}
    assert len(llm.calls) == 10
    processed = [m for call in llm.calls for m in json.loads(call["input"][0]["text"])["messages"]]
    assert len(processed) == 500 and processed[0]["ref"] == "m100"
    assert run(d, llm, entry) == {"unchanged": 1}
    source.data[1].append(message(600, "Fresh text"))
    assert run(d, llm, entry) == {"refreshed": 1}
    assert [m["ref"] for m in json.loads(llm.calls[-1]["input"][0]["text"])["messages"]] == ["m600"]


@pytest.mark.parametrize(
    "raw", [None, {"facts": "invalid", "retire": []}, {"facts": [], "retire": "invalid"}, "untrusted response body"]
)
def test_invalid_structured_responses_fail_without_consuming_cursor(plugin, source, raw, caplog):
    d = plugin._dossier_support
    entry = source.add()

    def callback(kwargs, count):
        return SimpleNamespace(parsed=raw)

    class InvalidLLM:
        async def acomplete_structured(self, **kwargs):
            return callback(kwargs, 1)

    assert run(d, InvalidLLM(), entry) == {"failed": 1}
    assert d.load(entry)["cursor"] == {} and d.load(entry)["last_refresh"] is None
    assert "untrusted response body" not in caplog.text
    assert run(d, FakeLLM(), entry) == {"refreshed": 1}


def test_revision_events_have_before_after_and_distinct_reasons(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    d.edit(entry, "correct", fact_id="f1", text="Owner says Wednesday")
    changes = d.revision_log(entry, d.load(entry))
    assert changes[0]["changes"][0]["after"]["text"] == "Meeting proposed for Tuesday."
    correction = changes[1]["changes"][0]
    assert correction["before"]["text"] == "Meeting proposed for Tuesday."
    assert correction["after"]["text"] == "Owner says Wednesday"
    assert correction["reason"] == "explicit owner instruction"
    assert changes[0]["reason"] == "initial backfill"


def test_terminal_control_escape_and_connection_without_target_rejected(plugin, source, capsys):
    d = plugin._dossier_support
    entry = source.add()
    d.edit(entry, "note", section="preferences", text="Owner \x1b[2J\ntext")
    assert cli(plugin, "show", "--chat", "1") == 0
    shown = capsys.readouterr().out
    assert "\x1b" not in shown and "\\x1b" in shown
    assert cli(plugin, "forget", "--all", "--connection", "test-connection") == 1
    assert (d.directory(entry) / "dossier.json").exists()


def test_capture_is_llm_free_with_dossier_enabled_and_history_separate(plugin, monkeypatch):
    h, d = plugin._history_support, plugin._dossier_support
    llm = FakeLLM()
    plugin._llm_facade = llm
    payload = SimpleNamespace(
        business_connection_id="test-connection",
        chat=SimpleNamespace(id=1, type="private"),
        message_id=1,
        from_user=SimpleNamespace(id=100),
        date=NOW,
        text="Captured synthetic text",
    )
    update = SimpleNamespace(business_message=payload, update_id=1)
    monkeypatch.delenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", raising=False)
    assert not asyncio.run(h.observe_ptb_update(update, now=NOW))
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.delenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", raising=False)
    monkeypatch.delenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS", raising=False)
    assert asyncio.run(h.observe_ptb_update(update, now=NOW))
    assert not llm.calls and not d.root().exists()


@pytest.mark.asyncio
async def test_multichunk_rebase_keeps_final_model_version_when_owner_claims_provisional_id(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    source.data[1] = [message(i, f"Evidence {i}", "inbound" if i % 2 else "outbound") for i in range(60)]

    def callback(kwargs, count):
        if count == 1:
            d.edit(entry, "note", section="preferences", text="Owner note claims f1")
            return {"facts": [fact(ref="m1", quote="Evidence 1", text="First model version")], "retire": []}
        return {"facts": [fact(ref="m1", quote="Evidence 1", text="Final model version", id="f1")], "retire": []}

    llm = FakeLLM(callback=callback)
    assert await d.run_cycle(llm, entry=entry, now=NOW, manual=True) == {"refreshed": 1}
    facts = d.load(entry)["facts"]
    assert [(f["id"], f["text"]) for f in facts] == [("f1", "Owner note claims f1"), ("f2", "Final model version")]


def test_snapshot_publish_failure_does_not_advance_cursor_or_leave_public_temporary_file(plugin, source, monkeypatch):
    d = plugin._dossier_support
    entry = source.add()
    original = d.os.replace

    def fail_snapshot(src, dst):
        if Path(dst).name == "dossier.json":
            assert stat.S_IMODE(Path(src).stat().st_mode) == 0o600
            raise OSError("Synthetic snapshot replace failure")
        return original(src, dst)

    monkeypatch.setattr(d.os, "replace", fail_snapshot)
    assert run(d, FakeLLM(), entry) == {"failed": 1}
    assert d.load(entry)["cursor"] == {} and d.load(entry)["revision"] == 0
    assert not list(d.directory(entry).glob(".dossier-*"))
    monkeypatch.setattr(d.os, "replace", original)
    assert run(d, FakeLLM(), entry) == {"refreshed": 1}


@pytest.mark.asyncio
async def test_worker_cancel_before_first_await_cleans_registry(plugin):
    d = plugin._dossier_support
    d.start_worker(SimpleNamespace(running=True), FakeLLM())
    task = d._workers[str(d.root())]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not d._workers and not d._worker_apps


def test_late_edit_does_not_undelete_and_repeated_delete_does_not_undo_classification(plugin):
    h = plugin._history_support
    deletion = event(plugin, 1, None, kind="message.deleted")
    seed_records(
        plugin,
        event(plugin, 1, "Original"),
        deletion,
        event(plugin, 1, "Latest text arrived after deletion", kind="message.edited"),
    )
    actual, _ = h.canonical_text_messages(contact())
    assert len(actual) == 1 and actual[0]["deleted"] is True
    assert actual[0]["text"] == "Latest text arrived after deletion"
    seed_records(
        plugin,
        event(
            plugin,
            1,
            None,
            kind="deletion.classified",
            deleted_event_id=deletion["event_id"],
            classification="likely_correction",
        ),
        event(plugin, 1, None, kind="message.deleted"),
    )
    assert h.canonical_text_messages(contact())[0] == []


def test_contradictory_evidence_stays_visible_with_exact_sources_and_catalog_identity(plugin, source):
    d = plugin._dossier_support
    entry = source.add(received="Tuesday is agreed.", sent="Wednesday is agreed.")
    raw = {
        "facts": [
            fact(quote="Tuesday is agreed.", text="Contact says Tuesday."),
            fact(
                ref="m2",
                quote="Wednesday is agreed.",
                text="Owner says Wednesday; timing conflicts.",
                section="contradictions",
            ),
        ],
        "retire": [],
        "name": "Model invented name",
        "aliases": ["Model invented alias"],
    }
    llm = FakeLLM(raw)
    assert run(d, llm, entry) == {"refreshed": 1}
    shown = d.inspect_dossier(entry)
    assert shown["name"] == "@alice" and "Model invented alias" not in shown["aliases"]
    assert len(shown["facts"]) == 2 and shown["facts"][1]["section"] == "contradictions"
    assert shown["facts"][1]["sources"][0]["quote"] == "Wednesday is agreed."


@pytest.mark.asyncio
async def test_toggle_off_while_waiting_for_rate_slot_prevents_call(plugin, source, monkeypatch):
    d = plugin._dossier_support
    entry = source.add()

    class Limiter:
        async def wait(self):
            monkeypatch.setenv(d.PREFIX + "ENABLE", "0")

    monkeypatch.setattr(d, "RateLimiter", lambda calls: Limiter())
    llm = FakeLLM()
    assert await d.run_cycle(llm, entry=entry, now=NOW, manual=True) == {"disabled": 1}
    assert not llm.calls and d.load(entry)["last_refresh"] is None


@pytest.mark.asyncio
async def test_gateway_worker_uses_host_supervised_task_api_without_spawning_at_registration(
    plugin, source, monkeypatch
):
    d = plugin._dossier_support
    source.add()
    spawned = []
    factories = []

    def spawn(coroutine, *, name):
        task = asyncio.create_task(coroutine, name=name)
        spawned.append(task)
        return task

    ctx = SimpleNamespace(
        llm=FakeLLM(),
        spawn_task=spawn,
        register_telegram_handler=factories.append,
        register_hook=lambda *a: None,
        register_cli_command=lambda **k: None,
        register_skill=lambda *a: None,
    )
    monkeypatch.setattr(
        plugin._history_support, "run_startup_maintenance", lambda: SimpleNamespace(cap_exceeded=False, warnings=[])
    )
    plugin.register(ctx)
    assert not spawned
    factories[0](SimpleNamespace(running=True, add_handler=lambda *a, **k: None), SimpleNamespace())
    assert len(spawned) == 1
    spawned[0].cancel()  # host unload performs this cancellation
    with pytest.raises(asyncio.CancelledError):
        await spawned[0]
    assert not d._workers


def test_corrupt_dossier_does_not_block_other_contacts_or_leak_content(plugin, source, caplog):
    d = plugin._dossier_support
    a = source.add()
    source.add(2, name="Bob")
    run(d, FakeLLM(), a)
    (d.directory(a) / "dossier.json").write_text("SECRET_DAMAGED_CONTENT")
    llm = FakeLLM({"facts": [], "retire": []})
    assert asyncio.run(d.run_cycle(llm, now=NOW)) == {"failed": 1, "refreshed": 2}
    assert len(llm.calls) == 2 and "SECRET_DAMAGED_CONTENT" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_deletion_metadata_and_protected_invalidation_have_revision_reasons(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    source.data[1][0]["deleted"] = True
    run(d, FakeLLM({"facts": [], "retire": []}), entry)
    value = d.load(entry)
    assert value["facts"][0]["sources"][0]["deleted"] is True
    change = d.revision_log(entry, value)[-1]["changes"][0]
    assert change["reason"] == "canonical source changed"
    assert change["before"]["source_metadata"][0]["deleted"] is False
    assert change["after"]["source_metadata"][0]["deleted"] is True
    d.edit(entry, "pin", fact_id="f1")
    source.data[1] = source.data[1][1:]
    run(d, FakeLLM({"facts": [], "retire": []}), entry)
    assert any(c["reason"] == "protected source gone" for c in d.revision_log(entry, d.load(entry))[-1]["changes"])


def test_unpin_source_gone_generated_fact_reconciles_without_new_history(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    d.edit(entry, "pin", fact_id="f1")
    source.data[1] = source.data[1][1:]
    llm = FakeLLM({"facts": [], "retire": []})
    run(d, llm, entry)
    assert d.load(entry)["facts"][0]["status"] == "source gone"
    d.edit(entry, "unpin", fact_id="f1")
    assert d.inspect_dossier(entry)["facts"][0]["status"] == "unsupported"
    assert run(d, llm, entry) == {"refreshed": 1}
    assert not d.load(entry)["facts"]
    assert run(d, llm, entry) == {"unchanged": 1}


def test_manual_cli_failed_contact_exit_is_nonzero_and_retry_succeeds(plugin, source, capsys, caplog):
    d = plugin._dossier_support
    entry = source.add()

    def fail(kwargs, count):
        raise RuntimeError("PRIVATE_MODEL_RESPONSE")

    plugin._llm_facade = FakeLLM(callback=fail)
    assert cli(plugin, "run", "--chat", "1") == 1
    assert json.loads(capsys.readouterr().out) == {"failed": 1}
    assert "PRIVATE_MODEL_RESPONSE" not in caplog.text and not d.load(entry)["last_refresh"]
    plugin._llm_facade = FakeLLM()
    assert cli(plugin, "run", "--chat", "1") == 0
    assert d.load(entry)["last_refresh"]


def test_forget_all_removes_log_only_directory_after_failed_initial_publication(plugin, source, monkeypatch):
    d = plugin._dossier_support
    entry = source.add()
    real_atomic = d.atomic

    def fail_snapshot(path, text):
        if path.name == "dossier.json":
            raise OSError("Synthetic publish failure")
        return real_atomic(path, text)

    monkeypatch.setattr(d, "atomic", fail_snapshot)
    assert run(d, FakeLLM(), entry) == {"failed": 1}
    assert (d.directory(entry) / "changes.jsonl").exists()
    assert not (d.directory(entry) / "dossier.json").exists()
    d.forget_all()
    assert not d.directory(entry).exists() and not list(d.root().glob("*/changes.jsonl"))


@pytest.mark.asyncio
async def test_worker_waits_for_slow_application_start_after_initial_delay(plugin, monkeypatch):
    d = plugin._dossier_support
    app = SimpleNamespace(running=False)
    sleeps, cycles = [], []

    async def sleep(seconds):
        sleeps.append(seconds)
        if seconds == 5:
            app.running = True
        elif seconds == 1800:
            app.running = False

    async def cycle(llm):
        cycles.append("ran")
        return {}

    monkeypatch.setattr(d.asyncio, "sleep", sleep)
    monkeypatch.setattr(d, "run_cycle", cycle)
    d.start_worker(app, FakeLLM())
    await d._workers[str(d.root())]
    assert sleeps == [180, 5, 1800] and cycles == ["ran"] and not d._workers
