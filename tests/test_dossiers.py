"""Tests for contact dossiers and the owner persona (issue #17).

Fixtures come from ``conftest.py`` and plain helpers from ``dossier_helpers.py``.
History is written through the real capture path; every cycle passes ``now=NOW``
so payloads and plans are deterministic [L4].
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import stat
import time
from collections.abc import Callable
from datetime import timedelta
from types import ModuleType
from typing import Any

import pytest
import test_history
from dossier_helpers import (
    BUSINESS_ID,
    NOW,
    FakeCtx,
    FakeLlm,
    ago,
    catalog_entry,
    change,
    cli,
    connect_gateway,
    cycle,
    delete,
    dossier_dir,
    dossier_module,
    edit,
    live_worker_threads,
    load_records,
    maintain,
    output,
    persona_dir,
    quote_first,
    raw,
    read_changes,
    read_dossier,
    read_persona,
    read_settings,
    read_state,
    seed,
    snapshot_history_tree,
    snapshot_tree,
    spy_worker_cycles,
    wait_until,
    wait_worker_cycle,
)

SECOND = timedelta(seconds=1)


@pytest.fixture(autouse=True)
def _isolated_env(dossier_env: Callable[..., None]) -> Callable[..., None]:
    # Every test starts without dossier or cleanup env vars, even the ones that never enable dossiers.
    return dossier_env


@pytest.fixture
def plug(dossier_plugin: ModuleType, dossiers: ModuleType) -> ModuleType:
    """The plugin module with history and dossiers enabled (same load as ``dossiers``)."""
    return dossier_plugin


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def by_task(contact: Any = None, persona: Any = None) -> Callable[[dict[str, Any], dict[str, Any]], Any]:
    """Responder that answers contact and persona calls differently (None means no changes)."""

    def respond(payload: dict[str, Any], kwargs: dict[str, Any]) -> Any:
        answer = contact if payload["task"] == "contact_dossier" else persona
        if answer is None:
            return output()
        return answer(payload, kwargs) if callable(answer) else answer

    return respond


def llm(contact: Any = None, persona: Any = None, *, calls: int = 40) -> FakeLlm:
    """A FakeLlm whose every call goes through ``by_task(contact, persona)``."""
    return FakeLlm(*([by_task(contact, persona)] * calls))


def contact_payloads(fake: FakeLlm) -> list[dict[str, Any]]:
    return [p for p in fake.payloads() if p["task"] == "contact_dossier"]


def persona_payloads(fake: FakeLlm) -> list[dict[str, Any]]:
    return [p for p in fake.payloads() if p["task"] == "owner_persona"]


def refs(payload: dict[str, Any]) -> list[str]:
    return [m["ref"] for m in payload["messages"]]


def message(payload: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(m for m in payload["messages"] if m["ref"] == ref)


def last_line(ds: ModuleType, chat_id: int) -> dict[str, Any]:
    lines = read_changes(dossier_dir(ds, chat_id))
    assert lines, "expected a revision log line"
    return lines[-1]


def facts_by_text(dossier: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["text"]: f for f in dossier["facts"]}


def chat_args(chat_id: int) -> tuple[str, ...]:
    return ("--connection", BUSINESS_ID, "--chat", str(chat_id))


async def two_sided(plugin: ModuleType, chat_id: int, tag: str, *, minutes: int = 60, username: str | None = None,
                    first_id: int = 1) -> None:
    """One received and one sent text message, ``minutes`` and ``minutes - 5`` before NOW."""
    await seed(plugin, chat_id, username or f"user{chat_id}", [
        (first_id, "in", f"{tag} question from the contact", minutes),
        (first_id + 1, "out", f"{tag} answer from the owner", minutes - 5),
    ])


# ---------------------------------------------------------------------------
# Gating and worker (1-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_worker_idles_without_cycle_call_or_write(
    dossier_history: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = dossier_history
    await two_sided(plugin, 5501, "OFF")
    ds = dossier_module(plugin)
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 0.02)  # many switch checks during the test
    outcomes = spy_worker_cycles(monkeypatch, ds)
    fake = FakeLlm(quote_first)
    try:
        assert ds.ensure_worker_started() is False  # no facade bound: nothing starts
        assert ds._WORKERS == {}
        ds.bind_llm(fake)
        connect_gateway(plugin)  # the facade is bound, so the worker runs while disabled
        assert ds.ensure_worker_started() is True
        threads = live_worker_threads()
        assert len(ds._WORKERS) == 1 and len(threads) == 1
        await asyncio.sleep(0.3)
        worker = next(iter(ds._WORKERS.values()))
        assert threads[0].is_alive() and not worker.cycle_done.is_set()
        assert outcomes == [] and fake.calls == []
        assert not ds.dossier_root().exists()  # checking the switch writes nothing
        assert cycle(ds, fake).outcome == "disabled"
        assert fake.calls == [] and not ds.dossier_root().exists()
    finally:
        ds.stop_workers(timeout=2)
        ds.bind_llm(None)
    assert live_worker_threads() == []


@pytest.mark.asyncio
async def test_run_cli_refuses_when_disabled(dossier_history: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = dossier_history
    await two_sided(plugin, 5502, "OFF")
    ds = dossier_module(plugin)
    fake = FakeLlm(quote_first)
    ds.bind_llm(fake)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("history must not be read when dossiers are disabled")

    history = plugin._history_support
    monkeypatch.setattr(history, "_streamed_records_locked", forbidden)
    monkeypatch.setattr(history, "_catalog_entries_for_cli", forbidden)
    monkeypatch.setattr(ds, "read_chat_view", forbidden)
    monkeypatch.setattr(ds, "private_entries", forbidden)
    try:
        code, out, err = cli(plugin, "dossier", "run")
    finally:
        ds.bind_llm(None)
    assert code == 1
    assert "dossiers are disabled" in out + err
    assert "'hermes telesec dossier enable'" in out + err
    assert "DOSSIER_ENABLE" not in out + err
    assert fake.calls == []
    assert not ds.dossier_root().exists()  # no lock file was created


@pytest.mark.asyncio
async def test_register_does_not_start_worker_in_cli(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await two_sided(plug, 5503, "CLI")
    fake = FakeLlm(quote_first)
    ctx = FakeCtx(fake, with_unload=True)
    plug.register(ctx)
    assert ds._LLM is fake  # register binds the facade for CLI `run`, but starts nothing

    for argv in (("status",), ("list",), ("preview", *chat_args(5503))):
        code, out, _err = cli(plug, "dossier", *argv)
        assert code == 0, argv
    assert json.loads(out)["task"] == "contact_dossier"  # preview printed the payload
    assert ds._WORKERS == {}
    assert live_worker_threads() == []
    assert fake.calls == []


@pytest.mark.asyncio
async def test_factory_starts_one_worker_idempotent(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    fake = FakeLlm()
    ds.bind_llm(fake)
    pending_before = set(plug._pending_tasks)

    connect_gateway(plug)
    connect_gateway(plug)  # a reconnect must not start a second worker

    assert len(ds._WORKERS) == 1
    threads = live_worker_threads()
    assert len(threads) == 1 and threads[0].daemon
    assert set(plug._pending_tasks) == pending_before
    ds.stop_workers(timeout=2)
    assert not threads[0].is_alive()
    assert ds._WORKERS == {}
    assert fake.calls == []  # the initial delay (3600 s) never elapsed


@pytest.mark.asyncio
async def test_worker_builds_contact_and_persona_from_fixture_history(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    dossier_env(INTERVAL_MINUTES=1440)
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    await two_sided(plug, 5504, "WORKER-A", minutes=90)
    await two_sided(plug, 5505, "WORKER-B", minutes=80)
    fake = FakeLlm(quote_first, quote_first, quote_first)
    ds.bind_llm(fake)

    connect_gateway(plug)
    wait_worker_cycle(ds, timeout=10)

    assert len(fake.calls) == 3
    assert len(contact_payloads(fake)) == 2 and len(persona_payloads(fake)) == 1
    for chat_id, tag in ((5504, "WORKER-A"), (5505, "WORKER-B")):
        dossier = read_dossier(ds, chat_id)
        assert dossier is not None
        assert [f["text"] for f in dossier["facts"]] == [f'The contact said "{tag} question from the contact".']
        assert dossier["last_refresh"]["trigger"] == "worker"
    persona = read_persona(ds)
    assert persona is not None and len(persona["facts"]) == 1
    assert read_state(ds)["last_cycle"]["outcome"] == "completed"
    assert read_state(ds)["last_cycle"]["trigger"] == "worker"

    code, out, _err = cli(plug, "dossier", "show", *chat_args(5504))
    assert code == 0
    assert "WORKER-A question from the contact" in out


@pytest.mark.asyncio
async def test_on_unload_registered_and_stops_worker(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    fake = FakeLlm()
    ctx = FakeCtx(fake, with_unload=True)
    plug.register(ctx)
    assert ctx.unload_callbacks == [plug._dossier_panel.stop_background]  # also stops panel refreshes
    assert "dossiers" in [name for name, _path, _desc in ctx.skills]

    connect_gateway(plug)
    threads = live_worker_threads()
    assert len(threads) == 1
    ctx.unload_callbacks[0]()  # the host calls it without arguments; it must not block
    threads[0].join(2)
    assert not threads[0].is_alive()
    assert ds._WORKERS == {}

    plug.register(FakeCtx(fake))  # a host without on_unload still registers fine
    assert live_worker_threads() == []


@pytest.mark.asyncio
async def test_cycle_lock_prevents_concurrent_cycles(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await two_sided(plug, 5507, "LOCK")
    fake = FakeLlm(quote_first)
    ds.bind_llm(fake)

    with ds._cycle_lock() as acquired:
        assert acquired
        assert ds.cycle_running()
        assert cycle(ds, fake).outcome == "busy"
        code, out, err = cli(plug, "dossier", "run")
        assert code == 1
        assert "another dossier cycle" in out + err
    assert fake.calls == []
    assert not ds.cycle_running()
    assert cycle(ds, fake).outcome == "completed"
    assert fake.calls


# ---------------------------------------------------------------------------
# Stored on/off switch
# ---------------------------------------------------------------------------

ENABLE_HINT = "Turn on with 'hermes telesec dossier enable' or Settings (/telesec) in Telegram."
STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


def iso(value: Any) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.asyncio
async def test_enable_disable_cli_round_trip_idempotent_with_status(dossier_history: ModuleType) -> None:
    plugin = dossier_history
    ds = dossier_module(plugin)
    code, out, _err = cli(plugin, "dossier", "status")
    assert code == 0
    assert f"Dossiers: disabled. {ENABLE_HINT}" in out.splitlines()
    assert "warning" not in out
    assert not ds.dossier_root().exists()  # status never writes the switch

    code, out, _err = cli(plugin, "dossier", "enable")
    assert code == 0, out
    assert re.search(rf"^Dossiers: enabled \({STAMP} via cli\)\.$", out, re.MULTILINE), out
    assert "first cycle within a few minutes; no gateway restart is needed" in out
    assert f"The text of all private Business chats is sent to {ds.DEFAULT_PROVIDER} / {ds.DEFAULT_MODEL}." in out
    assert "history capture is off" not in out
    stored = read_settings(ds)
    assert stored is not None and set(stored) == {"schema_version", "enabled", "updated_at", "updated_via"}
    assert (stored["schema_version"], stored["enabled"], stored["updated_via"]) == (1, True, "cli")
    path = ds.dossier_root() / "settings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ds.dossier_root().stat().st_mode) == 0o700
    assert ds.load_config().enabled is True
    before = path.read_bytes()

    code, out, _err = cli(plugin, "dossier", "enable")
    assert code == 0
    assert f"Dossiers are already enabled (since {stored['updated_at']} via cli)." in out
    assert path.read_bytes() == before  # idempotent: the switch time stays the first one
    code, out, _err = cli(plugin, "dossier", "status")
    assert f"Dossiers: enabled (since {stored['updated_at']} via cli)" in out.splitlines()

    code, out, _err = cli(plugin, "dossier", "disable")
    assert code == 0
    assert re.search(rf"^Dossiers: disabled \({STAMP} via cli\)\. No more chat text is sent to the model", out,
                     re.MULTILINE), out
    assert "Stored dossiers are kept; delete them with 'hermes telesec dossier forget --all'." in out
    stored = read_settings(ds)
    assert (stored["enabled"], stored["updated_via"]) == (False, "cli")
    assert ds.load_config().enabled is False
    before = path.read_bytes()
    code, out, _err = cli(plugin, "dossier", "disable")
    assert code == 0
    assert f"Dossiers are already disabled (since {stored['updated_at']} via cli)." in out
    assert path.read_bytes() == before
    code, out, _err = cli(plugin, "dossier", "status")
    assert f"Dossiers: disabled (since {stored['updated_at']} via cli). {ENABLE_HINT}" in out.splitlines()


def test_set_enabled_api_validates_and_records_time_and_source(
    dossier_history: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = dossier_history
    ds = dossier_module(plugin)
    assert ds.load_settings() == {"schema_version": 1, "enabled": False, "updated_at": None, "updated_via": None}
    with pytest.raises(ValueError, match="unknown switch source"):
        ds.set_enabled(True, via="env")
    assert not ds.dossier_root().exists()

    at = NOW - timedelta(minutes=5)
    on = ds.set_enabled(True, via="telegram", now=at)
    assert on == {"schema_version": 1, "enabled": True, "updated_at": iso(at), "updated_via": "telegram",
                  "changed": True}
    assert read_settings(ds) == ds.load_settings() == {k: v for k, v in on.items() if k != "changed"}
    assert ds.set_enabled(True, via="cli", now=NOW) == {**on, "changed": False}
    off = ds.set_enabled(False, via="cli", now=NOW)
    assert (off["changed"], off["enabled"], off["updated_at"], off["updated_via"]) == (True, False, iso(NOW), "cli")

    (ds.dossier_root() / "settings.json").write_text("{", encoding="utf-8")
    assert ds.set_enabled(False, via="cli", now=NOW)["changed"] is True  # an explicit switch repairs a bad file
    assert "error" not in ds.load_settings()

    test_history.set_switch(plugin, "history", False)
    code, out, _err = cli(plugin, "dossier", "enable")
    assert code == 0
    assert "note: history is off; turn it on with 'hermes telesec settings set history on' or /telesec" in out


@pytest.mark.asyncio
async def test_old_env_or_desktop_setting_moves_into_the_switch_once(
    dossier_history: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin = dossier_history
    ds = dossier_module(plugin)
    await two_sided(plugin, 5560, "SEED")
    settings_file = ds.dossier_root() / "settings.json"
    settings_file.unlink(missing_ok=True)
    dossier_env(ENABLE="1")
    monkeypatch.setattr(ds._history, "get_config", {"dossier_enable": True}.get)  # Desktop setting
    cfg = ds.load_config()
    assert (cfg.enabled, cfg.warnings) == (False, ())  # neither switches dossiers
    assert not settings_file.exists()

    dossier_env(ENABLE="0")  # the variable beats the setting for the one-time move
    assert ds.migrate_legacy_switch() is False and not settings_file.exists()
    dossier_env(ENABLE=None)
    with caplog.at_level(logging.INFO):
        assert ds.migrate_legacy_switch(now=NOW) is True
    assert read_settings(ds) == {"schema_version": 1, "enabled": True, "updated_at": iso(NOW),
                                 "updated_via": "migration"}
    assert "the dossier_enable setting was moved to the stored switch and is no longer read" in caplog.text
    _code, out, _err = cli(plugin, "dossier", "status")
    assert f"Dossiers: enabled (since {iso(NOW)} via migration)" in out.splitlines()

    ds.set_enabled(False, via="telegram")
    dossier_env(ENABLE="1")
    assert ds.migrate_legacy_switch() is False  # once only: a stored switch is never overwritten
    cfg = ds.load_config()
    assert (cfg.enabled, cfg.warnings) == (False, ())
    fake = FakeLlm(quote_first)
    assert cycle(ds, fake).outcome == "disabled"
    assert fake.calls == []
    _code, out, _err = cli(plugin, "dossier", "disable")
    assert "DOSSIER_ENABLE" not in out and "dossier_enable" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{not json", '{"schema_version": 1, "enabled": "yes"}', '{"enabled": true}',
                                     "[]", ""])
async def test_corrupt_switch_reads_disabled_with_warning_until_rewritten(
    plug: ModuleType, dossiers: ModuleType, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    ds = dossiers
    await two_sided(plug, 5561, "BADSWITCH")
    path = ds.dossier_root() / "settings.json"
    path.write_text(content, encoding="utf-8")
    settings = ds.load_settings()
    assert settings["enabled"] is False and "unreadable or corrupt" in settings["error"]
    cfg = ds.load_config()
    assert cfg.enabled is False
    assert len(cfg.warnings) == 1 and "settings.json is unreadable or corrupt" in cfg.warnings[0]
    fake = FakeLlm(quote_first)
    assert cycle(ds, fake).outcome == "disabled" and fake.calls == []
    code, out, _err = cli(plug, "dossier", "status")
    assert code == 0
    assert out.splitlines()[0].startswith("warning: ") and "settings.json is unreadable or corrupt" in out
    assert f"Dossiers: disabled. {ENABLE_HINT}" in out.splitlines()
    code, out, _err = cli(plug, "dossier", "run")
    assert code == 1 and "dossiers are disabled" in out
    assert path.read_text(encoding="utf-8") == content  # never overwritten silently
    ds.bind_llm(FakeLlm())
    with caplog.at_level(logging.WARNING):
        assert ds.ensure_worker_started() is True  # the gateway log says why dossiers stay off
    assert "dossier switch settings.json is unreadable or corrupt" in caplog.text
    ds.stop_workers(timeout=2)

    code, out, _err = cli(plug, "dossier", "enable")  # an explicit switch rewrites it
    assert code == 0
    assert "Dossiers: enabled (" in out and "corrupt" not in out
    assert read_settings(ds)["enabled"] is True
    assert ds.load_config().warnings == ()


@pytest.mark.asyncio
async def test_switch_on_starts_idle_worker_cycle_without_restart(
    dossier_history: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = dossier_history
    ds = dossier_module(plugin)
    await two_sided(plugin, 5562, "SWITCHON")
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 3600)  # only a wake ends the idle wait
    outcomes = spy_worker_cycles(monkeypatch, ds)
    fake = FakeLlm(quote_first, quote_first)
    ds.bind_llm(fake)
    try:
        connect_gateway(plugin)
        threads = live_worker_threads()
        assert len(threads) == 1
        await asyncio.sleep(0.2)
        assert outcomes == [] and fake.calls == []

        assert ds.set_enabled(True, via="telegram")["changed"] is True
        assert ds.wake_worker() is True
        wait_worker_cycle(ds, timeout=10)
        assert outcomes == ["completed"]
        assert len(contact_payloads(fake)) == 1 and len(persona_payloads(fake)) == 1
        assert read_dossier(ds, 5562) is not None
        assert read_state(ds)["last_cycle"]["trigger"] == "worker"
        assert live_worker_threads() == threads  # the same thread: no restart
    finally:
        ds.stop_workers(timeout=2)
        ds.bind_llm(None)
    assert ds.wake_worker() is False  # nothing left to wake


@pytest.mark.asyncio
async def test_cli_enable_reaches_gateway_worker_by_polling_after_connect_delay(
    dossier_history: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = dossier_history
    ds = dossier_module(plugin)
    await two_sided(plugin, 5563, "POLL")
    delay = 0.8
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", delay)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 0.05)
    outcomes = spy_worker_cycles(monkeypatch, ds)
    fake = FakeLlm(quote_first, quote_first)
    ds.bind_llm(fake)
    try:
        started = time.monotonic()
        connect_gateway(plugin)
        code, _out, _err = cli(plugin, "dossier", "enable")  # another process: no wake reaches the worker
        assert code == 0
        assert ds.wake_worker() is True  # and a wake never shortens the post-connect delay
        await asyncio.sleep(0.3)
        assert outcomes == [] and fake.calls == []
        wait_worker_cycle(ds, timeout=10)
        assert time.monotonic() - started >= delay
        assert outcomes == ["completed"] and len(fake.calls) == 2
    finally:
        ds.stop_workers(timeout=2)
        ds.bind_llm(None)


@pytest.mark.asyncio
async def test_wake_while_enabled_never_causes_back_to_back_cycles(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await two_sided(plug, 5564, "NOBURST")
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 0.02)  # the interval wait re-reads the switch often
    outcomes = spy_worker_cycles(monkeypatch, ds)
    fake = FakeLlm(quote_first, quote_first)
    ds.bind_llm(fake)
    connect_gateway(plug)
    wait_worker_cycle(ds, timeout=10)
    assert outcomes == ["completed"]
    for _ in range(5):
        assert ds.set_enabled(True, via="telegram")["changed"] is False
        assert ds.wake_worker() is True
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    assert outcomes == ["completed"]  # the next cycle still waits for the 30 min interval
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_busy_cycle_retries_after_a_poll_not_an_interval(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await two_sided(plug, 5566, "BUSY")
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 0.05)
    outcomes = spy_worker_cycles(monkeypatch, ds)
    ds.bind_llm(FakeLlm(quote_first))
    with ds._cycle_lock() as acquired:  # a panel Refresh holds the lock when the worker's cycle is due
        assert acquired
        connect_gateway(plug)
        assert wait_until(lambda: outcomes[:1] == ["busy"], timeout=10), outcomes
    assert wait_until(lambda: "completed" in outcomes, timeout=10), outcomes  # long before the 30 min interval


@pytest.mark.asyncio
async def test_disabling_stops_worker_cycles_and_enabling_resumes(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await two_sided(plug, 5565, "TOGGLE")
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 0.02)
    real_config = ds.load_config
    # A 60 ms interval, so a worker that is on keeps cycling during the test.
    monkeypatch.setattr(ds, "load_config", lambda: dataclasses.replace(real_config(), interval_minutes=0.001))
    outcomes = spy_worker_cycles(monkeypatch, ds)
    fake = FakeLlm(quote_first, quote_first)
    ds.bind_llm(fake)
    connect_gateway(plug)
    assert wait_until(lambda: len(outcomes) >= 3, timeout=10), outcomes

    ds.set_enabled(False, via="telegram")
    ds.wake_worker()
    await asyncio.sleep(0.2)  # a cycle that was already running may finish
    settled = len(outcomes)
    await asyncio.sleep(0.5)
    assert len(outcomes) == settled, outcomes[settled:]
    assert len(live_worker_threads()) == 1  # still running, only idle
    calls = len(fake.calls)

    ds.set_enabled(True, via="telegram")
    ds.wake_worker()
    assert wait_until(lambda: len(outcomes) > settled, timeout=10)  # back on without a restart
    assert outcomes[-1] == "completed"
    assert len(fake.calls) == calls  # nothing new in history: no model call


@pytest.mark.asyncio
async def test_switch_on_after_off_skips_the_rest_of_the_interval(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await two_sided(plug, 5567, "RESUME")
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 0.02)  # the default 30 min interval stays
    outcomes = spy_worker_cycles(monkeypatch, ds)
    ds.bind_llm(FakeLlm(quote_first, quote_first))
    connect_gateway(plug)
    assert wait_until(lambda: outcomes == ["completed"], timeout=10), outcomes  # the next one is due in 30 min

    ds.set_enabled(False, via="telegram")
    ds.wake_worker()
    await asyncio.sleep(0.2)  # several polls while off
    assert outcomes == ["completed"]

    ds.set_enabled(True, via="telegram")
    ds.wake_worker()
    assert wait_until(lambda: len(outcomes) >= 2, timeout=2), outcomes  # not after the old deadline
    assert outcomes[-1] == "completed"


@pytest.mark.asyncio
async def test_stop_workers_ends_disabled_poll_promptly(
    dossier_history: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossier_module(dossier_history)
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 3600)
    ds.bind_llm(FakeLlm())
    try:
        connect_gateway(dossier_history)
        [thread] = live_worker_threads()
        await asyncio.sleep(0.1)  # inside the hour-long disabled poll by now
        started = time.monotonic()
        ds.stop_workers(timeout=2)
        assert not thread.is_alive()
        assert time.monotonic() - started < 1.5
        assert ds._WORKERS == {} and ds.wake_worker() is False
    finally:
        ds.stop_workers(timeout=2)
        ds.bind_llm(None)


# ---------------------------------------------------------------------------
# Input rules (8-18)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_latest_edit_order_and_refs(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5508
    # Written out of send order: message 12 arrives first, 11 (sent earlier) afterwards.
    await seed(plug, chat, "casey5508", [
        (12, "out", "second message original", 50),
        (11, "in", "first message", 60),
        (13, "in", "third message", 40),
    ], first_name="Casey", last_name="Customer")
    await edit(plug, chat, 12, "second message edited", 45)
    fake = llm()
    report = cycle(ds, fake)
    assert report.refreshed == 1

    (payload,) = contact_payloads(fake)
    assert set(payload) == {"task", "mode", "batch", "sections", "current_facts", "do_not_add", "messages"}
    assert payload["mode"] == "initial"
    assert refs(payload) == ["m11", "m12", "m13"]
    assert [m["from"] for m in payload["messages"]] == ["contact", "owner", "contact"]
    assert [m["text"] for m in payload["messages"]] == ["first message", "second message edited", "third message"]
    assert [m["at"] for m in payload["messages"]] == [ds._iso(ago(minutes)) for minutes in (60, 50, 40)]
    for item in payload["messages"]:
        assert set(item) <= {"ref", "at", "from", "text", "changed", "deleted"}
    text = fake.calls[0]["input"][0]["text"]
    for leaked in ("original", "Casey", "Customer", "casey5508", "owner_account", str(chat), BUSINESS_ID,
                   "1000", "2000", "sender"):
        assert leaked not in text, leaked


@pytest.mark.asyncio
async def test_order_falls_back_to_first_observation(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5509
    raw(plug, chat, [
        (1, "in", "alpha later", 30),
        (2, "out", "bravo earlier", 40),
        (2, "out", "bravo edited", 20, "edited"),  # a later edit must not move it
    ], username="u5509", message_lag=None)
    fake = llm()
    cycle(ds, fake)
    (payload,) = contact_payloads(fake)
    assert refs(payload) == ["m2", "m1"]
    assert message(payload, "m2")["text"] == "bravo edited"
    assert message(payload, "m2")["at"] == ds._iso(ago(40))
    assert message(payload, "m1")["at"] == ds._iso(ago(30))


@pytest.mark.asyncio
async def test_deleted_kept_labelled_duplicate_and_correction_skipped(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5510
    await seed(plug, chat, "u5510", [(1, "in", "Can we meet on Tuesday?", 120), (2, "out", "Sure, see you then", 118)])
    # Duplicate: 3 is deleted and re-sent as 4 with the same text.
    t = ago(100)
    await seed(plug, chat, "u5510", [(3, "in", "Hello there", t)])
    await delete(plug, chat, 3, when=t + SECOND)
    await seed(plug, chat, "u5510", [(4, "in", "hello   there", t + 5 * SECOND)])
    maintain(plug, t + 20 * SECOND)
    # Correction: 5 is deleted and re-sent fixed as 6.
    t = ago(80)
    await seed(plug, chat, "u5510", [(5, "in", "Hello wrld", t)])
    await delete(plug, chat, 5, when=t + SECOND)
    await seed(plug, chat, "u5510", [(6, "in", "Hello world", t + 5 * SECOND)])
    maintain(plug, t + 20 * SECOND)
    # Unexplained deletion: kept and labelled.
    t = ago(60)
    await seed(plug, chat, "u5510", [(7, "out", "Price is 40 euros", t)])
    await delete(plug, chat, 7, when=t + SECOND)
    maintain(plug, t + 20 * SECOND)

    classified = {str(r["message_id"]): r["classification"]
                  for r in load_records(plug, chat_id=chat) if r["event_type"] == "deletion.classified"}
    assert classified == {"3": "likely_duplicate", "5": "likely_correction", "7": "unexplained"}

    fake = llm()
    cycle(ds, fake)
    (payload,) = contact_payloads(fake)
    assert refs(payload) == ["m1", "m2", "m4", "m6", "m7"]
    assert message(payload, "m7")["deleted"] is True
    assert all("deleted" not in m for m in payload["messages"] if m["ref"] != "m7")
    assert "Hello wrld" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_unknown_direction_labelled_and_known_direction_preferred(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    chat = 5511
    raw(plug, chat, [(1, "unknown", "who wrote this", 50), (2, "in", "hi there", 45)], username="u5511")
    raw(plug, 5590, [(1, "unknown", "who wrote this", 50), (2, "out", "owner here", 45)], username="u5590")
    fake = llm()
    report = cycle(ds, fake)
    assert report.unqualified == 2  # unknown counts for neither side
    assert contact_payloads(fake) == []
    assert read_dossier(ds, chat) is None and read_dossier(ds, 5590) is None
    (persona,) = persona_payloads(fake)
    assert [m["text"] for m in persona["messages"]] == ["owner here"]  # unknown-direction text never enters it

    raw(plug, chat, [(3, "out", "reply text", 40), (3, "unknown", "reply text edited", 35, "edited")],
        username="u5511")
    report = cycle(ds, fake)
    assert report.refreshed == 1
    (payload,) = contact_payloads(fake)
    assert [(m["ref"], m["from"]) for m in payload["messages"]] == [
        ("m1", "unknown"), ("m2", "contact"), ("m3", "owner")]
    assert message(payload, "m3")["text"] == "reply text edited"


@pytest.mark.asyncio
async def test_transcript_marker_labelled_and_excluded_from_persona(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed(plug, 5512, "u5512", [
        (1, "in", "hello there", 60),
        (2, "bot", "🎙️ hello from a voice note", 55),
        (3, "out", "normal owner reply", 50),
    ])
    await seed(plug, 5513, "u5513", [(1, "in", "only inbound", 60), (2, "bot", "🎙️ transcript only", 55)])
    fake = llm()
    report = cycle(ds, fake)

    assert report.refreshed == 1 and report.unqualified == 1
    (payload,) = contact_payloads(fake)
    assert [(m["ref"], m["from"]) for m in payload["messages"]] == [
        ("m1", "contact"), ("m2", "transcript"), ("m3", "owner")]
    assert read_dossier(ds, 5513) is None
    (persona,) = persona_payloads(fake)
    assert [m["text"] for m in persona["messages"]] == ["normal owner reply"]
    assert "🎙" not in json.dumps(persona, ensure_ascii=False)


@pytest.mark.asyncio
async def test_qualification_requires_received_and_sent_in_window(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed(plug, 5514, "u5514", [(1, "in", "anyone there?", 60)])
    raw(plug, 5515, [(1, "out", "old owner message", timedelta(days=100))], username="u5515")
    await seed(plug, 5515, "u5515", [(2, "in", "recent contact message", 60)])
    fake = llm()
    report = cycle(ds, fake)
    assert report.unqualified == 2
    assert fake.calls == []  # neither contacts nor persona (the old sent message is outside the window)
    assert read_dossier(ds, 5514) is None and read_dossier(ds, 5515) is None

    await seed(plug, 5514, "u5514", [(2, "out", "yes, here", 30)])
    report = cycle(ds, fake)
    (payload,) = contact_payloads(fake)
    assert refs(payload) == ["m1", "m2"]
    assert read_dossier(ds, 5514) is not None
    assert read_dossier(ds, 5515) is None


@pytest.mark.asyncio
async def test_groups_never_processed(plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    ds = dossiers
    monkeypatch.setenv("TELESEC_HISTORY_CHAT_TYPES", "private,group")
    plug._history_support.reset_in_memory_caches()
    await seed(plug, -5516, None, [
        (1, "in", "GROUP-SECRET question", 60),
        (2, "out", "GROUP-SECRET answer", 55),
    ], chat_type="group", title="Team room")
    assert load_records(plug, chat_id=-5516), "the group must be in history for this test to mean anything"
    await two_sided(plug, 5517, "PRIVATE")
    fake = llm()
    cycle(ds, fake)

    assert [refs(p) for p in contact_payloads(fake)] == [["m1", "m2"]]
    assert "PRIVATE question" in json.dumps(contact_payloads(fake))
    assert all("GROUP-SECRET" not in call["input"][0]["text"] for call in fake.calls)
    assert [m["text"] for m in persona_payloads(fake)[0]["messages"]] == ["PRIVATE answer from the owner"]
    assert read_dossier(ds, -5516) is None


@pytest.mark.asyncio
async def test_backfill_window_and_exact_message_cap(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None]
) -> None:
    ds = dossiers
    dossier_env(BACKFILL_MESSAGES=10)
    chat = 5518
    raw(plug, chat, [(1, "in", "ancient question", timedelta(days=100)),
                     (2, "out", "ancient answer", timedelta(days=100, minutes=-1))], username="u5518")
    tie = 200  # messages 12-15 share one timestamp at the cut
    rows = [(i, "in" if i % 2 else "out", f"recent message {i}", 300 - i) for i in range(10, 12)]
    rows += [(i, "in" if i % 2 else "out", f"recent message {i}", tie) for i in range(12, 16)]
    rows += [(i, "in" if i % 2 else "out", f"recent message {i}", 180 - i) for i in range(16, 24)]
    await seed(plug, chat, "u5518", rows)
    fake = llm()
    cycle(ds, fake)

    (payload,) = contact_payloads(fake)
    assert refs(payload) == [f"m{i}" for i in range(14, 24)]
    assert payload["batch"] == {"index": 1, "total": 1}
    dossier = read_dossier(ds, chat)
    assert dossier["coverage"]["older_history_not_sent"] is True
    assert ds._parse(dossier["coverage"]["window_start"]) == ago(tie)
    assert dossier["coverage"]["messages_processed"] == 10

    # Messages cut at the boundary (12, 13 share window_start) are never sent later.
    forced = cycle(ds, fake, target=("contact", catalog_entry(plug, chat)))
    assert forced.calls == 0
    assert len(contact_payloads(fake)) == 1


@pytest.mark.asyncio
async def test_first_pass_split_into_bounded_batches_and_resumes(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    monkeypatch.setattr(ds, "BATCH_MAX_MESSAGES", 3)
    words = ("apples arrive tuesday", "noted", "zebra crossing downtown", "fine", "violin lessons at noon",
             "sure", "quarterly tax paperwork")  # dissimilar, so quote_first facts are never merged as duplicates
    rows = [(i, "in" if i % 2 else "out", words[i - 1], 100 - 5 * i) for i in range(1, 8)]
    await seed(plug, 5519, "u5519", rows)
    await seed(plug, 5520, "u5520", rows)

    fake = FakeLlm(quote_first, quote_first, quote_first)
    report = cycle(ds, fake, target=("contact", catalog_entry(plug, 5519)))
    assert report.refreshed == 1 and report.calls == 3
    payloads = contact_payloads(fake)
    assert [p["batch"] for p in payloads] == [{"index": i, "total": 3} for i in (1, 2, 3)]
    assert [refs(p) for p in payloads] == [["m1", "m2", "m3"], ["m4", "m5", "m6"], ["m7"]]
    assert [p["mode"] for p in payloads] == ["initial", "initial", "initial"]
    assert [f["text"] for f in payloads[1]["current_facts"]] == ['The contact said "apples arrive tuesday".']
    assert len(read_dossier(ds, 5519)["facts"]) == 3

    # Variant: batch 2 times out; batch 1 stays committed and the next cycle sends only the rest.
    failing = FakeLlm(quote_first, TimeoutError("SECRET timeout"))
    report = cycle(ds, failing, target=("contact", catalog_entry(plug, 5520)))
    assert report.failed == 1 and report.failures == [(f"{BUSINESS_ID}:5520", "TimeoutError")]
    partial = read_dossier(ds, 5520)
    assert sorted(partial["seen"]) == ["1", "2", "3"]
    assert [f["text"] for f in partial["facts"]] == ['The contact said "apples arrive tuesday".']

    resumed = FakeLlm()
    report = cycle(ds, resumed, target=("contact", catalog_entry(plug, 5520)))
    assert report.refreshed == 1
    assert [refs(p) for p in contact_payloads(resumed)] == [["m4", "m5", "m6"], ["m7"]]
    assert sorted(read_dossier(ds, 5520)["seen"], key=int) == [str(i) for i in range(1, 8)]


@pytest.mark.asyncio
async def test_preview_prints_exact_payload_without_call_or_write(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await two_sided(plug, 5521, "PREVIEW")
    await seed(plug, 5522, "u5522", [(1, "in", "not answered yet", 60)])
    fake = FakeLlm(quote_first)
    ds.bind_llm(fake)
    history_before = snapshot_history_tree(plug)
    dossiers_before = snapshot_tree(ds.dossier_root())

    code, out, err = cli(plug, "dossier", "preview", *chat_args(5521))
    assert code == 0
    assert fake.calls == []
    assert snapshot_history_tree(plug) == history_before
    assert snapshot_tree(ds.dossier_root()) == dossiers_before
    assert out.endswith("\n") and json.loads(out)["messages"]
    assert err.startswith("#")
    assert "provider=gemini model=gemini-3.8-flash" in err
    assert "PREVIEW" not in err  # metadata only on stderr

    code, _run_out, _run_err = cli(plug, "dossier", "run", *chat_args(5521))
    assert code == 0
    assert fake.calls[0]["input"][0]["text"] == out[:-1]

    code, out, err = cli(plug, "dossier", "preview", *chat_args(5521))
    assert (code, out) == (0, "")
    assert "no model call is due" in err

    code, out, err = cli(plug, "dossier", "preview", *chat_args(5522))
    assert (code, out) == (0, "")
    assert "does not qualify yet" in err
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_plan_functions_deterministic_with_explicit_now(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed(plug, 5523, "u5523", [(1, "in", "first", 90), (2, "out", "second", 80), (3, "in", "third", 70)])
    entry = catalog_entry(plug, 5523)
    cfg = ds.load_config()

    def payload_once() -> str:
        view = ds.read_chat_view(entry, since=ds.fold_since(None, None, NOW, cfg))
        plan = ds.plan_contact(None, view, now=NOW, cfg=cfg, cycle_id=ds._iso(NOW))
        return ds.payload_text(ds.build_contact_payload(None, plan.pending, plan))

    first, second = payload_once(), payload_once()
    assert first == second
    preview, meta = ds.build_preview(("contact", entry), now=NOW)
    assert meta["status"] == "due"
    assert preview == first
    fake = FakeLlm()
    cycle(ds, fake, target=("contact", entry))
    assert fake.calls[0]["input"][0]["text"] == first


# ---------------------------------------------------------------------------
# Isolation (19-22)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_prompt_has_no_text_from_other_contact(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await two_sided(plug, 5524, "ALPHA-ONLY")
    await two_sided(plug, 5525, "BRAVO-ONLY")
    fake = llm(quote_first)
    cycle(ds, fake)

    payloads = contact_payloads(fake)
    assert len(payloads) == 2
    for payload in payloads:
        text = json.dumps(payload)
        assert ("ALPHA-ONLY" in text) != ("BRAVO-ONLY" in text)
    for chat_id, own, other in ((5524, "ALPHA-ONLY", "BRAVO-ONLY"), (5525, "BRAVO-ONLY", "ALPHA-ONLY")):
        stored = (dossier_dir(ds, chat_id) / "dossier.json").read_text(encoding="utf-8")
        assert own in stored and other not in stored


@pytest.mark.asyncio
async def test_cross_chat_reference_rejected(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await seed(plug, 5526, "u5526", [(10, "in", "alpha text here", 60), (11, "out", "alpha reply", 55)])
    await seed(plug, 5527, "u5527", [(20, "in", "BRAVO-QUOTE secret", 60), (21, "out", "bravo reply", 55)])
    answer = output(
        change(section="shared_facts", text="The contact mentioned a bravo secret.",
               sources=(("m20", "BRAVO-QUOTE secret"),)),
        change(section="preferences", text="The contact wrote alpha text.", sources=(("m10", "alpha text"),)),
    )
    fake = FakeLlm(answer)
    cycle(ds, fake, target=("contact", catalog_entry(plug, 5526)))

    dossier = read_dossier(ds, 5526)
    assert [f["text"] for f in dossier["facts"]] == ["The contact wrote alpha text."]
    line = last_line(ds, 5526)
    assert line["dropped"]["invalid_ref"] == 1
    assert line["dropped"]["no_source"] == 1
    assert "BRAVO" not in (dossier_dir(ds, 5526) / "dossier.json").read_text(encoding="utf-8")
    assert "BRAVO" not in json.dumps(read_changes(dossier_dir(ds, 5526)))


@pytest.mark.asyncio
async def test_ref_not_shown_in_call_rejected(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5528
    await seed(plug, chat, "u5528", [(1, "in", "I like tea", 60), (2, "out", "noted, tea it is", 55)])
    fake = FakeLlm(output(change(text="The contact likes tea.", sources=(("m1", "I like tea"),))))
    cycle(ds, fake)
    await seed(plug, chat, "u5528", [(3, "in", "also coffee sometimes", 20)])
    fake = FakeLlm(output(
        change(section="shared_facts", text="The owner confirmed tea.", sources=(("m2", "tea it is"),)),
        change(section="relationship", text="The contact drinks tea and coffee.",
               sources=(("m1", "I like tea"), ("m3", "coffee"))),
    ))
    cycle(ds, fake)

    (payload,) = contact_payloads(fake)
    assert refs(payload) == ["m3"]
    assert [s["ref"] for f in payload["current_facts"] for s in f["sources"]] == ["m1"]
    dossier = read_dossier(ds, chat)
    texts = facts_by_text(dossier)
    assert "The owner confirmed tea." not in texts  # m2 exists in this chat but was not shown
    assert [s["ref"] for s in texts["The contact drinks tea and coffee."]["sources"]] == ["m1", "m3"]
    line = last_line(ds, chat)
    assert line["dropped"]["invalid_ref"] == 1
    assert line["dropped"]["no_source"] == 1


@pytest.mark.asyncio
async def test_persona_refs_resolve_only_known_aliases_and_owner_messages(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed(plug, 5529, "u5529", [(1, "in", "hello A", 60), (2, "out", "owner A reply", 55)])
    await seed(plug, 5530, "u5530", [(3, "in", "hello B", 50), (4, "out", "owner B reply", 45)])

    def persona_answer(payload: dict[str, Any], _kwargs: dict[str, Any]) -> dict[str, Any]:
        ref_a = next(m["ref"] for m in payload["messages"] if m["text"] == "owner A reply")
        alias = ref_a.split(":")[0]
        return output(
            change(section="greetings", text="Bad alias fact.", sources=(("c9:m2", "owner A reply"),)),
            change(section="tone", text="Inbound under a valid alias.", sources=((f"{alias}:m1", "hello A"),)),
            change(section="vocabulary", text='The owner wrote "owner A reply".', sources=((ref_a, "owner A reply"),)),
        )

    fake = llm(persona=persona_answer)
    cycle(ds, fake)

    (payload,) = persona_payloads(fake)
    assert refs(payload) == ["c1:m2", "c2:m4"]
    persona = read_persona(ds)
    assert [f["text"] for f in persona["facts"]] == ['The owner wrote "owner A reply".']
    (source,) = persona["facts"][0]["sources"]
    assert source["ref"] == "c1:m2" and source["chat"] == "c1"
    assert (source["business_connection_id"], source["chat_id"], source["message_id"]) == (BUSINESS_ID, 5529, 2)
    assert source["quote"] == "owner A reply"
    line = read_changes(persona_dir(ds))[-1]
    assert line["dropped"]["invalid_ref"] == 2
    aliases = {alias: info["chat_id"] for alias, info in persona["chat_aliases"].items()}
    assert aliases == {"c1": 5529, "c2": 5530}

    # Two more refreshes: known chats keep their alias, a new chat gets the next one.
    await seed(plug, 5530, "u5530", [(5, "out", "owner B again", 30)])
    await seed(plug, 5529, "u5529", [(6, "out", "owner A again", 25)])
    await seed(plug, 5531, "u5531", [(7, "in", "hello C", 24), (8, "out", "owner C reply", 22)])
    fake = FakeLlm()
    cycle(ds, fake, target=("persona", None))
    (payload,) = persona_payloads(fake)
    assert refs(payload) == ["c2:m5", "c1:m6", "c3:m8"]
    await seed(plug, 5531, "u5531", [(9, "out", "owner C again", 20)])
    cycle(ds, fake, target=("persona", None))
    assert refs(persona_payloads(fake)[-1]) == ["c3:m9"]
    aliases = {alias: info["chat_id"] for alias, info in read_persona(ds)["chat_aliases"].items()}
    assert aliases == {"c1": 5529, "c2": 5530, "c3": 5531}


# ---------------------------------------------------------------------------
# Validation (23-29)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quote_exact_substring_after_whitespace_normalization(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5532
    await seed(plug, chat, "u5532", [(1, "in", "Please   send the\ninvoice by Friday", 60), (2, "out", "ok", 55)])
    fake = FakeLlm(output(
        change(section="commitments", text="The contact asked for the invoice by Friday.",
               sources=(("m1", "send the\n invoice   by Friday"),)),
        change(section="decisions", text="The invoice deadline is Fridey.", sources=(("m1", "invoice by Fridey"),)),
    ))
    cycle(ds, fake)

    facts = facts_by_text(read_dossier(ds, chat))
    kept = facts["The contact asked for the invoice by Friday."]
    assert (kept["kind"], kept["confidence"]) == ("observed", None)
    assert kept["sources"][0]["quote"] == "send the invoice by Friday"
    dropped = facts["The invoice deadline is Fridey."]
    assert (dropped["kind"], dropped["confidence"]) == ("inferred", "low")
    assert dropped["sources"][0]["quote"] is None
    line = last_line(ds, chat)
    assert line["dropped"]["quote_mismatch"] == 1
    assert line["dropped"]["downgraded"] == 1


@pytest.mark.asyncio
async def test_quote_over_200_chars_dropped(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5533
    long_text = "abcdefghij" * 30
    await seed(plug, chat, "u5533", [(1, "in", long_text, 60), (2, "out", "ok", 55)])
    fake = FakeLlm(output(
        change(section="shared_facts", text="A quote of exactly 200 characters.", sources=(("m1", long_text[:200]),)),
        change(section="decisions", text="A quote of 201 characters.", sources=(("m1", long_text[:201]),)),
    ))
    cycle(ds, fake)

    facts = facts_by_text(read_dossier(ds, chat))
    assert facts["A quote of exactly 200 characters."]["sources"][0]["quote"] == long_text[:200]
    assert facts["A quote of exactly 200 characters."]["kind"] == "observed"
    too_long = facts["A quote of 201 characters."]
    assert too_long["sources"][0]["quote"] is None
    assert (too_long["kind"], too_long["confidence"]) == ("inferred", "low")
    assert last_line(ds, chat)["dropped"]["quote_too_long"] == 1
    assert long_text[:201] not in (dossier_dir(ds, chat) / "dossier.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_fact_without_valid_source_dropped(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5534
    await seed(plug, chat, "u5534", [(1, "in", "I live near the station", 60), (2, "out", "good to know", 55)])
    not_a_dict = change(section="decisions", text="Fact with a non-object source.")
    not_a_dict["sources"] = [42]
    fake = FakeLlm(output(
        change(section="relationship", kind="inferred", text="Fact without sources.", sources=()),
        change(section="shared_facts", text="Fact citing a missing message.", sources=(("m999", "station"),)),
        change(section="commitments", text="Fact with a malformed ref.", sources=("garbage",)),
        not_a_dict,
        change(section="preferences", text="The contact lives near the station.", sources=(("m1", "near the station"),)),
    ))
    cycle(ds, fake)

    assert [f["text"] for f in read_dossier(ds, chat)["facts"]] == ["The contact lives near the station."]
    dropped = last_line(ds, chat)["dropped"]
    assert dropped["no_source"] == 4
    assert dropped["invalid_ref"] == 2
    assert dropped["invalid"] == 1


@pytest.mark.asyncio
async def test_sections_kinds_and_caps(plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    ds = dossiers
    monkeypatch.setattr(ds, "MAX_FACTS_CONTACT", 3)
    chat = 5535
    rows = [(i, "in" if i % 2 else "out", f"distinct message number {i}", 100 - 5 * i) for i in range(1, 9)]
    await seed(plug, chat, "u5535", rows)
    owner_kind = change(section="preferences", text="The model claims owner authority.",
                        sources=(("m1", "distinct message number 1"),))
    owner_kind["kind"] = "owner"
    long_fact = "Long fact " + "z" * 390
    fake = llm(output(
        change(section="mood", text="Bad section fact.", sources=(("m1", "distinct"),)),
        owner_kind,
        change(section="relationship", text=long_fact, sources=(("m1", "distinct message number 1"),)),
        change(section="shared_facts", kind="inferred", text="The chat lists numbered messages.",
               sources=[(f"m{i}", "") for i in range(1, 8)]),
        change(section="decisions", text="The owner sent message two.", sources=(("m2", "number 2"),)),
        change(section="commitments", text="A fourth fact over the cap.", sources=(("m3", "number 3"),)),
    ))
    cycle(ds, fake)

    dossier = read_dossier(ds, chat)
    by_section = {f["section"]: f for f in dossier["facts"]}
    assert sorted(by_section) == ["decisions", "relationship", "shared_facts"]
    assert len(by_section["relationship"]["text"]) == 300
    assert by_section["relationship"]["text"].endswith("…")
    assert [s["ref"] for s in by_section["shared_facts"]["sources"]] == ["m1", "m2", "m3", "m4", "m5"]
    assert all(f["kind"] != "owner" for f in dossier["facts"])
    dropped = last_line(ds, chat)["dropped"]
    assert dropped["invalid"] == 1 and dropped["invalid_kind"] == 1 and dropped["cap"] == 1
    ids_before = sorted(f["id"] for f in dossier["facts"])

    await seed(plug, chat, "u5535", [(9, "in", "one more message", 20)])
    fake = FakeLlm(output(change(section="open_questions", text="Another fact over the cap.",
                                 sources=(("m9", "one more"),))))
    cycle(ds, fake)
    assert sorted(f["id"] for f in read_dossier(ds, chat)["facts"]) == ids_before  # never evicted


@pytest.mark.asyncio
async def test_names_come_from_catalog_not_model(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 991
    await seed(plug, chat, "customer991", [(1, "in", "hi, it is me", 60), (2, "out", "hello!", 55)],
               first_name="Casey", last_name="Customer")
    answer = change(text="The contact greeted the owner.", sources=(("m1", "hi"),))
    answer["display_name"] = "Mallory"  # extra model fields are ignored
    fake = FakeLlm(output(answer))
    cycle(ds, fake)

    entry = catalog_entry(plug, chat)
    dossier = read_dossier(ds, chat)
    assert dossier["contact"] == ds.catalog_snapshot(entry)
    assert dossier["contact"]["display_name"] == "@customer991"
    assert "Mallory" not in json.dumps(dossier)
    text = fake.calls[0]["input"][0]["text"]
    assert "customer991" not in text and "Casey" not in text and "Customer" not in text

    code, out, _err = cli(plug, "dossier", "show", *chat_args(chat))
    assert code == 0
    assert "@customer991" in out.splitlines()[0]

    # A later profile change reaches the dossier on the next refresh, still never through the model.
    await seed(plug, chat, "casey_new", [(3, "in", "new handle", 20)], first_name="Casey", last_name="Customer")
    cycle(ds, FakeLlm())
    dossier = read_dossier(ds, chat)
    assert dossier["contact"] == ds.catalog_snapshot(catalog_entry(plug, chat))
    assert dossier["contact"]["display_name"] == "@casey_new"


@pytest.mark.asyncio
async def test_contradictions_and_unsupported_reported_in_show(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5536
    await seed(plug, chat, "u5536", [
        (1, "in", "Friday works for the delivery", 120),
        (2, "out", "noted", 118),
        (3, "in", "Monday works for the delivery", 110),
    ])
    t = ago(100)
    await seed(plug, chat, "u5536", [(5, "in", "Hello there", t)])
    fake = FakeLlm(output(
        change(section="contradictions", kind="inferred", conf="medium",
               text="The contact gave Friday and later Monday as the delivery day.",
               sources=(("m1", "Friday works"), ("m3", "Monday works"))),
        change(section="relationship", text="The contact greets with hello.", sources=(("m5", "Hello there"),)),
    ))
    cycle(ds, fake)
    # m5 is deleted and re-sent with the same text, so it is classified a duplicate and its fact loses its source.
    await delete(plug, chat, 5, when=t + SECOND)
    await seed(plug, chat, "u5536", [(6, "in", "hello   there", t + 5 * SECOND)])
    maintain(plug, t + 20 * SECOND)
    cycle(ds, FakeLlm())

    dossier = read_dossier(ds, chat)
    statuses = {f["section"]: f["status"] for f in dossier["facts"]}
    assert statuses == {"contradictions": "active", "relationship": "unsupported"}

    code, out, _err = cli(plug, "dossier", "show", *chat_args(chat))
    assert code == 0
    facts_line = next(line for line in out.splitlines() if line.startswith("Facts:"))
    assert "1 unsupported" in facts_line and "1 contradiction" in facts_line
    lines = out.splitlines()
    assert "## contradictions" in lines
    unsupported_at = next(i for i, line in enumerate(lines) if line.startswith("## unsupported"))
    assert any("The contact greets with hello." in line for line in lines[unsupported_at:])
    assert any("The contact gave Friday" in line for line in lines[:unsupported_at])


@pytest.mark.asyncio
async def test_unparseable_output_fails_contact_and_retries_next_cycle(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    chat = 5537
    await two_sided(plug, chat, "PARSE")
    key = ds.chat_key(BUSINESS_ID, chat)
    report = cycle(ds, llm("this is not json at all"))
    assert report.failed == 1
    assert report.failures == [(f"{BUSINESS_ID}:{chat}", "ValueError")]
    state = read_state(ds)
    assert state["failures"][key]["error"] == "ValueError" and state["failures"][key]["count"] == 1
    assert key not in state["chats"]  # no gate: retried next cycle
    assert read_dossier(ds, chat) is None

    fenced = "```json\n" + json.dumps(output(change(text="The contact asked a PARSE question.",
                                                     sources=(("m1", "PARSE question"),)))) + "\n```"
    retry = llm(fenced)
    report = cycle(ds, retry)
    assert report.refreshed == 1 and report.failed == 0
    assert len(contact_payloads(retry)) == 1
    assert key not in read_state(ds)["failures"]
    assert [f["text"] for f in read_dossier(ds, chat)["facts"]] == ["The contact asked a PARSE question."]


# ---------------------------------------------------------------------------
# Rate limit (56-58)
# ---------------------------------------------------------------------------


class FakeClock:
    """Clock plus wait for RateLimiter: waiting advances the clock instead of sleeping."""

    def __init__(self, start: float = 1_000.0, *, stop: bool = False) -> None:
        self.now = start
        self.waits: list[float] = []
        self.stop = stop

    def __call__(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        if self.stop:
            return True
        self.now += seconds
        return False


def test_rate_limiter_sliding_window_and_stop(dossiers: ModuleType) -> None:
    ds = dossiers
    clock = FakeClock()
    limiter = ds.RateLimiter(3, clock=clock, wait=clock.wait)
    for step in (0, 10, 20):
        clock.now = 1_000.0 + step
        assert limiter.acquire()
    assert clock.waits == []
    clock.now = 1_030.0
    assert limiter.acquire()
    assert clock.waits == [pytest.approx(30.0)]  # 60 s after the first call
    assert clock.now == pytest.approx(1_060.0)
    assert limiter.snapshot() == [1_010.0, 1_020.0, 1_060.0]

    stopper = FakeClock(stop=True)
    stopped = ds.RateLimiter(1, clock=stopper, wait=stopper.wait)
    assert stopped.acquire()
    assert stopped.acquire() is False
    assert stopper.waits == [pytest.approx(60.0)]

    seeded = ds.RateLimiter(2, recent=[1_000.0 - 70, 1_000.0 - 30, 1_000.0 - 10], clock=FakeClock())
    assert seeded.snapshot() == [970.0, 990.0]  # older than 60 s is dropped


@pytest.mark.asyncio
async def test_cycle_respects_max_calls_per_minute_without_cycle_cap(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None]
) -> None:
    ds = dossiers
    dossier_env(MAX_CALLS_PER_MINUTE=2)
    for chat_id in (5538, 5539, 5540):
        await two_sided(plug, chat_id, f"RATE{chat_id}")
    clock = FakeClock()
    fake = llm(quote_first, quote_first)
    times: list[float] = []
    fake.on_call = lambda _payload: times.append(clock())
    report = cycle(ds, fake, limiter=ds.RateLimiter(2, clock=clock, wait=clock.wait))

    assert report.refreshed == 3 and report.persona == "refreshed"
    assert report.calls == 4
    assert times[:2] == [1_000.0, 1_000.0]
    assert times[2] >= 1_060.0  # the third call waited for the window
    assert times == sorted(times)
    assert all(read_dossier(ds, chat_id) is not None for chat_id in (5538, 5539, 5540))


@pytest.mark.asyncio
async def test_recent_calls_persist_across_cycles(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    dossier_env(MAX_CALLS_PER_MINUTE=2)
    for chat_id in (5541, 5542, 5543):
        await two_sided(plug, chat_id, f"TAIL{chat_id}")
    clock = FakeClock()
    real_limiter = ds.RateLimiter
    built: list[int] = []

    class ClockedLimiter(real_limiter):  # the cycle builds its own limiter: give it the fake clock
        def __init__(self, max_per_minute: int, *, recent: Any = (), **_kwargs: Any) -> None:
            built.append(max_per_minute)
            super().__init__(max_per_minute, recent=recent, clock=clock, wait=clock.wait)

    monkeypatch.setattr(ds, "RateLimiter", ClockedLimiter)
    fake = FakeLlm()
    for chat_id in (5541, 5542):
        assert cycle(ds, fake, target=("contact", catalog_entry(plug, chat_id))).calls == 1
    assert clock.waits == []
    assert read_state(ds)["recent_calls"] == [1_000.0, 1_000.0]

    assert cycle(ds, fake, target=("contact", catalog_entry(plug, 5543))).calls == 1
    assert clock.waits == [pytest.approx(60.0)]  # the persisted tail filled the window
    assert read_state(ds)["recent_calls"] == [pytest.approx(1_060.0)]
    assert built == [2, 2, 2]  # MAX_CALLS_PER_MINUTE reached every cycle's limiter


# ---------------------------------------------------------------------------
# Logs, storage, CLI wiring (59-63)
# ---------------------------------------------------------------------------


class PluginLlmTrustError(PermissionError):
    """Same name and base as the host's trust error."""


@pytest.mark.asyncio
async def test_logs_have_no_message_text_facts_or_quotes(
    plug: ModuleType, dossiers: ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    ds = dossiers
    await seed(plug, 5544, "secretname_alpha", [
        (1, "in", "SECRET-MSG-A please call me", 60),
        (2, "out", "SECRET-MSG-A2 sure", 55),
    ], first_name="SECRETFIRST", last_name="SECRETLAST")
    await seed(plug, 5545, "secretname_bravo", [(1, "in", "SECRET-MSG-B hi", 50), (2, "out", "SECRET-MSG-B2 yo", 45)])
    await seed(plug, 5546, "secretname_charlie", [(1, "in", "SECRET-MSG-C hello", 40)])

    def contact_answer(payload: dict[str, Any], _kwargs: dict[str, Any]) -> dict[str, Any]:
        if "SECRET-MSG-B" in json.dumps(payload):
            raise RuntimeError("SECRET-EXC-x")
        return output(change(text="SECRET-FACT the contact asks for calls",
                             sources=(("m1", "SECRET-MSG-A please"),), reason="SECRET-REASON"))

    caplog.clear()
    caplog.set_level(logging.DEBUG)
    report = cycle(ds, llm(contact_answer, persona=output(
        change(section="tone", text="SECRET-PERSONA fact", sources=(("c1:m2", "SECRET-MSG-A2"),),
               reason="SECRET-REASON"))))
    assert report.failed == 1 and report.refreshed == 1

    await seed(plug, 5546, "secretname_charlie", [(2, "out", "SECRET-MSG-C2 reply", 30)])
    caplog.set_level(logging.DEBUG)
    report = cycle(ds, FakeLlm(PluginLlmTrustError("SECRET-TRUST denied")))
    assert report.outcome == "aborted_trust"

    logged = caplog.text
    assert "SECRET" not in logged.upper()
    assert "RuntimeError" in logged
    assert "PluginLlmTrustError" in logged
    assert "5545" not in logged and BUSINESS_ID not in logged  # raw ids stay out of logs
    assert f"chat_ref={ds._log_ref({'business_connection_id': BUSINESS_ID, 'chat_id': 5545})}" in logged
    dossier_records = [r for r in caplog.records if r.name == ds.__name__]
    assert dossier_records, "expected dossier log lines"
    assert all(r.exc_info is None for r in dossier_records)


@pytest.mark.asyncio
async def test_storage_permissions_atomic_and_outside_history(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5547
    await two_sided(plug, chat, "PERMS")
    cycle(ds, llm(quote_first, quote_first))
    ds.apply_owner_edit(("contact", catalog_entry(plug, chat)), "note", section="preferences", text="Owner note.")

    root = ds.dossier_root()
    history_root = plug._history_support.history_root()
    assert root.parent == history_root.parent and root != history_root
    assert history_root not in root.parents
    paths = [root, *root.rglob("*")]
    assert any(p.name == "dossier.json" for p in paths) and any(p.name == "changes.jsonl" for p in paths)
    assert (persona_dir(ds) / "dossier.json").exists()
    for path in paths:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), (path, oct(mode))
        assert not path.name.endswith(".tmp"), path
    assert all("dossier" not in key for key in snapshot_history_tree(plug))

    code, out, _err = cli(plug, "history", "verify")
    assert code == 0, out
    code, out, _err = cli(plug, "history", "catalog", "--rebuild")
    assert code == 0, out


@pytest.mark.asyncio
async def test_capture_is_llm_free_with_dossier_enabled_and_history_separate(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    fake = FakeLlm(quote_first)
    ds.bind_llm(fake)
    connect_gateway(plug)  # the worker exists, but its first cycle is an hour away
    test_history.set_switch(plug, "history", False)
    assert await seed(plug, 5552, "capture", [(1, "in", "CAPTURE off", 10)], check=False) == [False]
    test_history.set_switch(plug, "history", True)
    await two_sided(plug, 5552, "CAPTURE", minutes=8, first_id=2)
    assert fake.calls == []
    assert ds.list_dossiers() == [] and not (ds.dossier_root() / "chats").exists()
    assert any(key.endswith(".jsonl") for key in snapshot_history_tree(plug))
    assert all("dossier" not in key for key in snapshot_history_tree(plug))


@pytest.mark.asyncio
async def test_cli_text_escapes_terminal_controls_from_contact_text(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5553
    await seed(plug, chat, None, [
        (1, "in", "Call me \x1b[31mtomorrow\x07", 60),
        (2, "out", "Sure", 55),
    ], first_name="Eve\x1b]0;pwned\x07", last_name="\x9bX")
    fact = change(text="The contact asks for a call.", sources=(("m1", "Call me"),))
    assert cycle(ds, llm(output(fact))).refreshed == 1

    outputs = {}
    for argv in (("show", *chat_args(chat)), ("show", *chat_args(chat), "--fact", "f1"), ("list",)):
        code, out, err = cli(plug, "dossier", *argv)
        assert code == 0, (argv, out, err)
        assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out, argv
        outputs[argv[-1]] = out
    for name in (str(chat), "list"):
        assert "Eve\\x1b]0;pwned\\x07" in outputs[name], name  # the contact name, escaped but readable
    assert "Call me \\x1b[31mtomorrow\\x07" in outputs["f1"]  # the history text behind the source


@pytest.mark.asyncio
async def test_changes_log_torn_tail_isolated(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5548
    await two_sided(plug, chat, "TORN")
    cycle(ds, llm(quote_first))
    log = dossier_dir(ds, chat) / "changes.jsonl"
    with log.open("ab") as handle:
        handle.write(b'{"garbage": tru')

    ds.apply_owner_edit(("contact", catalog_entry(plug, chat)), "pin", fact_id="f1")

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert lines[1] == '{"garbage": tru'
    assert json.loads(lines[2])["changes"] == [{"op": "pin", "fact_id": "f1"}]
    parsed = ds.read_changes(dossier_dir(ds, chat))
    assert [line["actor"] for line in parsed] == ["model", "owner"]

    code, out, _err = cli(plug, "dossier", "changes", *chat_args(chat))
    assert code == 0
    assert re.search(r"rev 2 .*owner.*pin f1", out)


@pytest.mark.asyncio
async def test_changes_log_torn_utf8_tail_isolated(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5551
    await two_sided(plug, chat, "TORNUTF")
    cycle(ds, llm(quote_first))
    log = dossier_dir(ds, chat) / "changes.jsonl"
    with log.open("ab") as handle:
        handle.write('{"text":"Клиент'.encode()[:-1])  # cut inside a 2-byte character
    ds.apply_owner_edit(("contact", catalog_entry(plug, chat)), "pin", fact_id="f1")

    assert [line["actor"] for line in ds.read_changes(dossier_dir(ds, chat))] == ["model", "owner"]
    for command in ("changes", "export"):
        code, out, err = cli(plug, "dossier", command, *chat_args(chat))
        assert code == 0, (command, out, err)
    code, out, err = cli(plug, "dossier", "forget", *chat_args(chat))
    assert code == 0, (out, err)
    assert not dossier_dir(ds, chat).exists()


@pytest.mark.asyncio
async def test_corrupt_dossier_not_overwritten_and_reported(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await two_sided(plug, 5549, "CORRUPT")
    await two_sided(plug, 5550, "HEALTHY")
    cycle(ds, llm(quote_first))
    broken = dossier_dir(ds, 5549) / "dossier.json"
    broken.write_text("{not json", encoding="utf-8")
    await seed(plug, 5549, "user5549", [(3, "in", "CORRUPT follow-up", 20)])
    await seed(plug, 5550, "user5550", [(3, "in", "HEALTHY follow-up", 20)])

    report = cycle(ds, llm(quote_first))
    assert report.failures == [(f"{BUSINESS_ID}:5549", "ValueError")]
    assert report.refreshed == 1
    assert broken.read_text(encoding="utf-8") == "{not json"
    assert read_state(ds)["failures"][ds.chat_key(BUSINESS_ID, 5549)]["error"] == "ValueError"
    assert read_dossier(ds, 5550)["seen"].keys() == {"1", "2", "3"}
    errors = {item["path"]: item["error"] for item in ds.list_dossiers()}
    assert "corrupt" in errors[dossier_dir(ds, 5549)]

    code, out, err = cli(plug, "dossier", "show", *chat_args(5549))
    assert code == 1
    assert "corrupt" in out + err


@pytest.mark.asyncio
async def test_dossier_subcommand_coexists_with_history_cli(plug: ModuleType, dossiers: ModuleType) -> None:
    await two_sided(plug, 5551, "COEXIST")

    code, out, _err = cli(plug, "history", "show", *chat_args(5551))
    assert code == 0
    assert "COEXIST question from the contact" in out

    code, out, _err = cli(plug, "dossier")
    assert code == 1
    assert out.startswith("Usage: hermes telesec dossier <")
    for command in ("status", "enable", "disable", "list", "show", "preview", "correct", "pin", "unpin", "remove",
                    "note", "changes", "export", "forget", "run"):
        assert command in out.splitlines()[0]

    code, out, _err = cli(plug, "history")
    assert code == 1
    assert out == ("Usage: hermes telesec history "
                   "<chats|contacts|catalog|stats|show|search|deletions|export|verify|maintain>\n")

    code, _out, _err = cli(plug, "dossier", "no-such-command")
    assert code == 2
