"""Tests for the dossier refresh engine: config, settings, the history view, planning, writer input and the worker.

Fixtures come from ``conftest.py`` and plain helpers from ``dossier_helpers.py``. History is written through the
real capture path except where ``raw`` is needed; every cycle passes ``now=NOW`` so plans are deterministic.
Races, failures, the message check, the persona rules and the forget contract are tested in
``test_dossier_lifecycle.py`` and ``test_dossier_guarantees.py``; the CLI in ``test_dossier_cli.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import logging
import stat
import sys
import time
from collections.abc import Callable
from datetime import timedelta
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from dossier_helpers import (
    BOT_ID,
    BUSINESS_ID,
    CONTACT_ID,
    NOW,
    OWNER_ID,
    FakeBot,
    FakeCtx,
    FakeLlm,
    ago,
    catalog_entry,
    cli,
    connect_gateway,
    cycle,
    delete,
    doc,
    dossier_module,
    edit,
    live_worker_threads,
    load_records,
    maintain,
    make_business_text_update,
    quote_first,
    raw,
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
from model_fakes import fake_secret_scope

SECOND = timedelta(seconds=1)
MARK = chr(0x1F399) + chr(0xFE0F)  # the transcript marker as the plugin posts it
TOKYO = "Asia/Tokyo"  # UTC+9 all year
DEFAULT_ROUTE = ("telesec_dossier", None, None)
GATEWAY_KEY = {"AI_GATEWAY_API_KEY": "test-gateway-key"}
CF_KEYS = {"CLOUDFLARE_ACCOUNT_ID": "0123456789abcdef0123456789abcdef", "CLOUDFLARE_API_TOKEN": "test-cf-token"}
_UPDATE_IDS = itertools.count(800_000)


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


def contact_calls(fake: FakeLlm) -> list[dict[str, Any]]:
    return fake.by_purpose("telesec_dossier")


def persona_calls(fake: FakeLlm) -> list[dict[str, Any]]:
    return fake.by_purpose("telesec_persona")


def contact_payloads(fake: FakeLlm) -> list[dict[str, Any]]:
    return [call["user"] for call in contact_calls(fake)]


def refs(payload: dict[str, Any]) -> list[str]:
    return [m["ref"] for m in payload["messages"]]


def message(payload: dict[str, Any], ref: str) -> dict[str, Any]:
    return next(m for m in payload["messages"] if m["ref"] == ref)


def quoted(text: str) -> str:
    """The document ``quote_first`` writes for a batch whose first contact message is ``text``."""
    return f'**The contact.**\n\n## Notes\n- The contact said "{text}".'


def local(minutes: Any, zone: Any = None) -> str:
    """The writer's ``at`` for a time ``minutes`` before NOW (UTC unless ``zone`` is given)."""
    at = ago(minutes)
    return f"{at.astimezone(zone) if zone else at:%Y-%m-%d %H:%M}"


def route_of(ds: ModuleType) -> tuple[str | None, str | None, str | None]:
    r = ds.route()
    return r.task, r.provider, r.model


async def two_sided(plugin: ModuleType, chat_id: int, tag: str, *, minutes: int = 60) -> None:
    """One received and one sent text message, ``minutes`` and ``minutes - 5`` before NOW."""
    await seed(plugin, chat_id, f"user{chat_id}", [
        (1, "in", f"{tag} question from the contact", minutes),
        (2, "out", f"{tag} answer from the owner", minutes - 5),
    ])


async def capture(plugin: ModuleType, chat_id: int, rows: list[tuple[int, str, str, Any, int | None]]) -> None:
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


def host_config(monkeypatch: pytest.MonkeyPatch, task: dict[str, Any] | None) -> None:
    """A fake ``hermes_cli.config`` whose ``auxiliary.telesec_dossier`` is ``task`` (None: no such section)."""
    package = ModuleType("hermes_cli")
    package.__path__ = []
    config = ModuleType("hermes_cli.config")
    auxiliary = {} if task is None else {"telesec_dossier": task}
    config.load_config_readonly = lambda: {"auxiliary": auxiliary}
    package.config = config
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)


def local_zone(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``name`` Hermes's time zone (a fake ``hermes_time``)."""
    module = ModuleType("hermes_time")
    module.get_timezone_name = lambda: name
    monkeypatch.setitem(sys.modules, "hermes_time", module)


# ---------------------------------------------------------------------------
# Effective config and the writer route (F, O4)
# ---------------------------------------------------------------------------

AUTO_WARNING = ("warning: the Telesec dossiers task follows the main model (auto): dossier text goes to the main "
                "model. Choose a model with 'hermes model' → Telesec dossiers.")
MAX_TOKENS_WARNING = ("warning: auxiliary.telesec_dossier.extra_body replaces the default and has no max_tokens; "
                      "long answers are not capped.")
ZDR_WARNING = ("warning: zero data retention is off for Telesec dossiers (auxiliary.telesec_dossier.extra_body has "
               "no providerOptions.gateway.zeroDataRetention).")
NOT_REGISTERED_WARNING = ("warning: the Telesec dossiers model task is not registered (Hermes older than 0.21.6?); "
                          "calls go to ai-gateway / google/gemini-3.8-flash directly and need "
                          "plugins.entries.telesec.llm trust config.")
IGNORED_WARNING = "warning: TELESEC_DOSSIER_PROVIDER/TELESEC_DOSSIER_MODEL are ignored; remove them."


def reasoning_warning(value: str) -> str:
    return (f"warning: reasoning_effort for Telesec dossiers is '{value}'; 'low' is about 3x cheaper. "
            "Set auxiliary.telesec_dossier.reasoning_effort: low.")


def gateway_options_warning(who: str) -> str:
    return (f"warning: auxiliary.telesec_dossier.extra_body has providerOptions, which only ai-gateway accepts, so {who} "
            "may reject every dossier call. Set auxiliary.telesec_dossier.extra_body for it, e.g. {max_tokens: 8192}, "
            "or {} for none.")


def legacy_warning(provider: str, model: str) -> str:
    return (f"warning: TELESEC_DOSSIER_PROVIDER/TELESEC_DOSSIER_MODEL are deprecated; dossiers use {provider} / "
            f"{model} from them. Remove them to use the Telesec dossiers task (choose its model with 'hermes model').")


@pytest.mark.asyncio
async def test_default_config_routes_through_the_registered_task(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    assert ds.TASK_DEFAULTS == {
        "provider": "ai-gateway",
        "model": "google/gemini-3.8-flash",
        "timeout": 120,
        "reasoning_effort": "low",
        "extra_body": {"max_tokens": 8192, "providerOptions": {"gateway": {"zeroDataRetention": True}}},
    }
    for task in (None, {}):  # no Hermes config section, or an empty one
        host_config(monkeypatch, task)
        cfg = ds.load_config()
        assert (cfg.provider, cfg.model, cfg.reasoning, cfg.max_tokens, cfg.zdr) == (
            "ai-gateway", "google/gemini-3.8-flash", "low", 8192, True)
        assert (cfg.task_registered, cfg.legacy_env, cfg.warnings) == (True, False, ())
        assert route_of(ds) == DEFAULT_ROUTE

    await two_sided(plug, 5601, "ROUTE")
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    (call,) = contact_calls(fake)
    # No timeout: Hermes reads auxiliary.telesec_dossier.timeout, so the owner's value wins over the default.
    assert (call["task"], call["timeout"]) == ("telesec_dossier", None)
    assert "provider" not in call and "model" not in call  # the host applies the task's own config
    assert read_dossier(ds, 5601)["last_call"] == {"provider": "ai-gateway", "model": "google/gemini-3.8-flash",
                                                   "at": ds._iso(NOW)}


@pytest.mark.parametrize(("task", "expected", "warnings"), [
    # The task defaults stay under a 'hermes model' pick, so a gateway-only providerOptions reaches other providers.
    ({"provider": "auto"}, ("auto", "main model", "low", 8192, True),
     [AUTO_WARNING, gateway_options_warning("the main model")]),
    ({"provider": " ", "model": "example/model-1"}, ("auto", "main model", "low", 8192, True),
     [AUTO_WARNING, gateway_options_warning("the main model")]),
    ({"provider": "anthropic", "model": "claude-example"}, ("anthropic", "claude-example", "low", 8192, True),
     [gateway_options_warning("anthropic")]),
    ({"provider": "gemini", "model": "gemini-3.8-flash", "extra_body": {"max_tokens": 8192}},
     ("gemini", "gemini-3.8-flash", "low", 8192, False), []),
    ({"provider": "vercel", "model": "google/gemini-3.9-pro"}, ("vercel", "google/gemini-3.9-pro", "low", 8192, True),
     []),
    ({"reasoning_effort": ""}, ("ai-gateway", "google/gemini-3.8-flash", "", 8192, True),
     [reasoning_warning("provider default")]),
    ({"reasoning_effort": "high"}, ("ai-gateway", "google/gemini-3.8-flash", "high", 8192, True),
     [reasoning_warning("high")]),
    ({"extra_body": {"temperature": 0.2}}, ("ai-gateway", "google/gemini-3.8-flash", "low", None, False),
     [MAX_TOKENS_WARNING, ZDR_WARNING]),
    ({"provider": "openrouter", "model": "example/model-1", "extra_body": {"max_tokens": 4000}},
     ("openrouter", "example/model-1", "low", 4000, False), []),
    ({"model": "google/gemini-3.9-pro", "timeout": 60}, ("ai-gateway", "google/gemini-3.9-pro", "low", 8192, True),
     []),
])
def test_host_task_config_merges_shallow_and_warns_per_setting(
    dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, task: dict[str, Any], expected: tuple[Any, ...],
    warnings: list[str],
) -> None:
    ds = dossiers
    host_config(monkeypatch, task)
    cfg = ds.load_config()
    assert (cfg.provider, cfg.model, cfg.reasoning, cfg.max_tokens, cfg.zdr) == expected
    assert list(cfg.warnings) == warnings
    assert ("gateway_options" in cfg.warning_codes) is any("providerOptions, which" in w for w in warnings)
    assert route_of(ds) == DEFAULT_ROUTE  # whatever the task says, the plugin passes only the task name


@pytest.mark.asyncio
async def test_an_unregistered_task_uses_the_explicit_fallback_route(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    ds.set_task_registered(False)
    cfg = ds.load_config()
    assert (cfg.provider, cfg.model, cfg.reasoning, cfg.max_tokens, cfg.zdr, cfg.task_registered) == (
        "ai-gateway", "google/gemini-3.8-flash", ds.NOT_SET, 8192, False, False)
    assert cfg.warnings == (NOT_REGISTERED_WARNING,)
    assert route_of(ds) == (None, "ai-gateway", "google/gemini-3.8-flash")

    await two_sided(plug, 5602, "FALLBACK")
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    (call,) = contact_calls(fake)
    assert (call["provider"], call["model"]) == ("ai-gateway", "google/gemini-3.8-flash")
    assert "task" not in call  # never task= for a task the host does not know
    assert call["timeout"] == ds.MODEL_TIMEOUT_SECONDS  # no task config to read it from
    assert call["max_tokens"] == 8192  # nor an extra_body to cap the output


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "model", "expected_model"), [
    ("gemini", None, "gemini-3.8-flash"),
    ("Google", None, "gemini-3.8-flash"),
    ("openrouter", None, "google/gemini-3.8-flash"),
    ("ai-gateway", None, "google/gemini-3.8-flash"),
    ("gemini", "gemini-3.8-pro", "gemini-3.8-pro"),
])
async def test_the_legacy_provider_env_is_an_explicit_route_with_a_warning(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
    provider: str, model: str | None, expected_model: str,
) -> None:
    ds = dossiers
    host_config(monkeypatch, {"model": "google/gemini-3.9-pro"})  # a task section without a provider overrides nothing
    dossier_env(PROVIDER=f" {provider} ", MODEL=model)
    cfg = ds.load_config()
    assert (cfg.provider, cfg.model, cfg.legacy_env) == (provider, expected_model, True)
    assert (cfg.reasoning, cfg.max_tokens, cfg.zdr) == (ds.NOT_SET, 8192, False)
    assert cfg.warnings == (legacy_warning(provider, expected_model),)
    assert route_of(ds) == (None, provider, expected_model)

    await two_sided(plug, 5603, "LEGACY")
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    (call,) = contact_calls(fake)
    assert (call["provider"], call["model"]) == (provider, expected_model) and "task" not in call
    assert call["max_tokens"] == 8192


def test_a_legacy_route_with_url_credentials_is_shown_without_them(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    ds = dossiers
    provider = "https://demo-user:SECRET-PASS@llm.example.com/v1?key=SECRET-KEY#frag"
    dossier_env(PROVIDER=provider, MODEL="demo-model?token=SECRET-TOKEN")
    with caplog.at_level(logging.DEBUG):
        cfg = ds.load_config()
    assert (cfg.provider, cfg.model) == ("https://llm.example.com/v1", "demo-model")
    assert cfg.warnings == (legacy_warning("https://llm.example.com/v1", "demo-model"),)
    assert route_of(ds) == (None, provider, "demo-model?token=SECRET-TOKEN")  # the call keeps the values as set
    assert "SECRET" not in caplog.text
    code, out, err = cli(plug, "dossier", "status")
    assert code == 0 and "Model: https://llm.example.com/v1 / demo-model" in out and "SECRET" not in out + err
    assert ds.without_credentials("name:secret@model") == "model"
    assert ds.without_credentials("model@version") == "model@version"  # no scheme and no ":": not user info


@pytest.mark.parametrize(("env", "task", "expected"), [
    ({"MODEL": "gemini-3.8-pro"}, None, ("ai-gateway", "google/gemini-3.8-flash")),
    ({"PROVIDER": "gemini", "MODEL": "gemini-3.8-pro"}, {"provider": "ai-gateway"},
     ("ai-gateway", "google/gemini-3.8-flash")),
    ({"PROVIDER": "gemini"}, {"provider": "openrouter", "model": "example/model-1", "extra_body": {"max_tokens": 8192}},
     ("openrouter", "example/model-1")),
])
def test_the_legacy_env_is_ignored_alone_or_under_an_explicit_task_provider(
    dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch, env: dict[str, str],
    task: dict[str, Any] | None, expected: tuple[str, str],
) -> None:
    ds = dossiers
    host_config(monkeypatch, task)
    dossier_env(**env)
    cfg = ds.load_config()
    assert ((cfg.provider, cfg.model), cfg.legacy_env) == (expected, False)
    assert cfg.warnings == (IGNORED_WARNING,)
    assert route_of(ds) == DEFAULT_ROUTE


def test_the_cleanup_provider_never_routes_dossiers(
    dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    monkeypatch.setenv("TELESEC_CLEANUP_PROVIDER", "gemini")
    cfg = ds.load_config()
    assert route_of(ds) == DEFAULT_ROUTE and cfg.provider == "ai-gateway"
    note = ("note: dossiers no longer follow TELESEC_CLEANUP_PROVIDER; they use the Telesec dossiers task "
            "(ai-gateway / google/gemini-3.8-flash).")
    assert cfg.warnings == (note,)

    dossier_env(PROVIDER="openrouter")  # the dossier variable is the one that routes, with its own warning
    cfg = ds.load_config()
    assert route_of(ds) == (None, "openrouter", "google/gemini-3.8-flash")
    assert cfg.warnings == (legacy_warning("openrouter", "google/gemini-3.8-flash"),)


def test_the_message_check_needs_the_setting_a_gateway_route_and_a_key(
    dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    assert ds.load_config().gate_setting == ds.GATE_DEFAULT
    with fake_secret_scope(GATEWAY_KEY) as scope:
        ds.set_gate("off", via="cli")
        assert ds.load_config().gate == "off"
        ds.set_gate("auto", via="cli")
        cfg = ds.load_config()
        assert (cfg.gate_setting, cfg.gate) == ("auto", "on")
        scope.secrets["AI_GATEWAY_BASE_URL"] = "http://gateway.example.com/v1"
        assert ds.load_config().gate == "bad_base_url"
        scope.secrets["AI_GATEWAY_BASE_URL"] = "https://gateway.example.com/v1"
        assert ds.load_config().gate == "on"

        dossier_env(PROVIDER="gemini")  # chat text would reach TypeSafe through a gateway the writer does not use
        assert ds.load_config().gate == "not_gateway"
        dossier_env(PROVIDER="vercel")
        assert ds.load_config().gate == "on"
        dossier_env(PROVIDER=None)
        for provider in ("openrouter", "auto"):
            host_config(monkeypatch, {"provider": provider})
            assert ds.load_config().gate == "not_gateway"
        host_config(monkeypatch, None)
    assert ds.load_config().gate == "no_key"


def test_fact_hints_need_the_setting_and_cloudflare_credentials(dossiers: ModuleType) -> None:
    ds = dossiers
    with fake_secret_scope(CF_KEYS):
        assert ds.load_config().clairic == "off"  # an absent setting is off, even with credentials
        ds.set_clairic("on", via="telegram")
        assert ds.load_config().clairic == "on"
    assert ds.load_config().clairic == "no_key"
    with fake_secret_scope({**CF_KEYS, "CLOUDFLARE_ACCOUNT_ID": "not-a-hex-account-id"}):
        assert ds.load_config().clairic == "no_key"


def test_config_warnings_are_logged_once_per_process_as_codes(
    dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ds = dossiers
    host_config(monkeypatch, {"provider": "auto", "reasoning_effort": "medium"})
    dossier_env(MODEL="example-secret-model")
    with caplog.at_level(logging.WARNING):
        first, second = ds.load_config(), ds.load_config()
    assert first.warnings == second.warnings and len(first.warnings) == 4
    assert first.warning_codes == ("env_ignored", "auto", "reasoning", "gateway_options")
    lines = sorted(r.getMessage() for r in caplog.records if "dossier config warning" in r.getMessage())
    assert lines == [f"telesec: dossier config warning={code}"
                     for code in ("auto", "env_ignored", "gateway_options", "reasoning")]
    assert "example-secret-model" not in caplog.text and "main model" not in caplog.text


# ---------------------------------------------------------------------------
# Settings and state
# ---------------------------------------------------------------------------


def test_gate_and_clairic_settings_round_trip_and_survive_the_switch(dossiers: ModuleType) -> None:
    ds = dossiers
    assert ds.SCHEMA_VERSION == 1
    switch = read_settings(ds)
    assert set(switch) == {"schema_version", "enabled", "updated_at", "updated_via"}
    assert ds.load_settings() == switch  # gate and clairic are absent until set

    assert ds.set_gate("auto", via="telegram", now=NOW) == {**switch, "gate": "auto", "changed": True}
    assert ds.set_gate("auto", via="cli")["changed"] is False
    assert ds.set_clairic("on", via="cli") == {**switch, "gate": "auto", "clairic": "on", "changed": True}
    assert read_settings(ds) == ds.load_settings() == {**switch, "gate": "auto", "clairic": "on"}

    off = ds.set_enabled(False, via="telegram", now=NOW)
    assert (off["changed"], off["enabled"], off["gate"], off["clairic"]) == (True, False, "auto", "on")
    on = ds.set_enabled(True, via="cli", now=NOW)
    assert (on["changed"], on["enabled"], on["gate"], on["clairic"]) == (True, True, "auto", "on")
    assert ds.set_gate("off", via="cli")["gate"] == "off"
    assert ds.set_clairic("off", via="telegram")["clairic"] == "off"
    assert ds.load_settings()["enabled"] is True and ds.load_settings()["updated_via"] == "cli"

    for call in (lambda: ds.set_gate("on", via="cli"), lambda: ds.set_clairic("auto", via="cli"),
                 lambda: ds.set_gate("auto", via="env"), lambda: ds.set_clairic("on", via="migration")):
        with pytest.raises(ValueError, match="unknown"):
            call()

    path = ds.settings_path()
    stored = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**stored, "gate": "sometimes", "clairic": True}), encoding="utf-8")
    assert "gate" not in ds.load_settings() and "clairic" not in ds.load_settings()  # unknown values read as absent
    cfg = ds.load_config()
    assert (cfg.enabled, cfg.gate_setting, cfg.clairic) == (True, ds.GATE_DEFAULT, "off")

    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable or corrupt"):
        ds.set_gate("auto", via="cli")  # only an explicit enable or disable rewrites a corrupt switch
    assert path.read_text(encoding="utf-8") == "{"


@pytest.mark.asyncio
async def test_a_v1_state_loads_without_its_old_and_unknown_keys(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    key = ds.chat_key(BUSINESS_ID, 5604)
    gate = {"signature": [], "dossier_id": "0123456789ab", "at": ds._iso(NOW)}
    v1 = {
        "schema_version": 1,
        "chats": {key: gate, "other/1": 7},
        "persona_gates": {key: {"signature": []}},
        "failures": ["not", "a", "dict"],
        "recent_calls": [1.0],
        "last_cycle": {"outcome": "completed"},
        "usage": {"2026-09": {"writer": {"calls": 2}, "check": 3}, "2026-10": "bad"},
        "maintenance": {"at": "2026-01-01T00:00:00Z"},
    }
    ds.state_path().write_text(json.dumps(v1), encoding="utf-8")
    assert ds.load_state() == {"schema_version": 1, "chats": {key: gate}, "failures": {}, "recent_calls": [1.0],
                               "last_cycle": {"outcome": "completed"}, "usage": {"2026-09": {"writer": {"calls": 2}}}}
    for broken in ("[]", "{", ""):
        ds.state_path().write_text(broken, encoding="utf-8")
        assert ds.load_state() == {"schema_version": 1, "chats": {}, "failures": {}, "recent_calls": [],
                                   "last_cycle": None, "usage": {}}

    ds.state_path().write_text(json.dumps(v1), encoding="utf-8")
    await two_sided(plug, 5604, "STATE")
    assert cycle(ds, FakeLlm()).refreshed == 1  # the old stat gate names another document: the chat is read
    saved = read_state(ds)
    assert "persona_gates" not in saved and "maintenance" not in saved
    assert saved["chats"][key]["dossier_id"] == read_dossier(ds, 5604)["id"]


# ---------------------------------------------------------------------------
# History view (B.1, O2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_view_keeps_replaced_and_deleted_messages_as_deleted(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5605
    await seed(plug, chat, "u5605", [(1, "in", "Can we meet on Tuesday?", 120), (2, "out", "Sure, see you then", 118)])
    # Duplicate: 3 is deleted and re-sent as 4 with the same text.
    t = ago(100)
    await seed(plug, chat, "u5605", [(3, "in", "Hello there", t)])
    await delete(plug, chat, 3, when=t + SECOND)
    await seed(plug, chat, "u5605", [(4, "in", "hello   there", t + 5 * SECOND)])
    maintain(plug, t + 20 * SECOND)
    # Correction: 5 is deleted and re-sent fixed as 6.
    t = ago(80)
    await seed(plug, chat, "u5605", [(5, "in", "Hello wrld", t)])
    await delete(plug, chat, 5, when=t + SECOND)
    await seed(plug, chat, "u5605", [(6, "in", "Hello world", t + 5 * SECOND)])
    maintain(plug, t + 20 * SECOND)
    # Unexplained deletion.
    t = ago(60)
    await seed(plug, chat, "u5605", [(7, "out", "Price is 40 euros", t)])
    await delete(plug, chat, 7, when=t + SECOND)
    maintain(plug, t + 20 * SECOND)
    classified = {str(r["message_id"]): r["classification"]
                  for r in load_records(plug, chat_id=chat) if r["event_type"] == "deletion.classified"}
    assert classified == {"3": "likely_duplicate", "5": "likely_correction", "7": "unexplained"}

    view = ds.read_chat_view(catalog_entry(plug, chat), since=NOW - timedelta(days=1))
    assert view.deleted_seen == {"3", "5", "7"}
    assert {k: m.deleted for k, m in view.messages.items()} == {
        "1": False, "2": False, "3": True, "4": False, "5": True, "6": False, "7": True}
    assert (view.messages["5"].text, view.messages["7"].label) == ("Hello wrld", "owner")  # the last text stays

    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    (payload,) = contact_payloads(fake)
    assert refs(payload) == ["m1", "m2", "m4", "m6"]  # a first pass leaves deleted messages out entirely
    assert "Hello wrld" not in json.dumps(payload) and "Price is 40 euros" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_transcript_chunks_chain_by_first_seen_within_ten_seconds(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5606
    start = ago(60)

    def at(seconds: int) -> Any:
        return start + seconds * SECOND

    await capture(plug, chat, [
        (1, "in", "Sending you a voice note", at(0), None),
        (2, "bot", f"{MARK} first chunk of the voice note", at(1), 1),  # chunk 0 replies to the voice
        (3, "bot", "second chunk", at(9), None),
        (4, "bot", "third chunk, 16 s after the first", at(17), None),  # chains from the chunk before it
        (5, "out", "owner text 11 s after the last chunk", at(28), None),
        (6, "bot", f"{MARK} another voice note", at(60), 1),
        (7, "out", "a chunk exactly 10 s later", at(70), None),
        (8, "out", "owner reply within 10 s", at(75), 6),  # a reply is never a chunk
        (9, "in", "contact text within 10 s", at(76), None),
        (10, "out", "owner text 11 s after chunk 7", at(81), None),
    ])
    view = ds.read_chat_view(catalog_entry(plug, chat), since=NOW - timedelta(days=1))
    assert {k: m.label for k, m in view.messages.items()} == {
        "1": "contact", "2": "transcript", "3": "transcript", "4": "transcript", "5": "owner",
        "6": "transcript", "7": "transcript", "8": "owner", "9": "contact", "10": "owner"}
    assert [view.messages[k].reply_to for k in ("1", "2", "3", "8")] == [None, "1", None, "6"]

    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    (payload,) = contact_payloads(fake)
    assert {m["ref"]: (m["from"], m.get("voice", False)) for m in payload["messages"]} == {
        "m1": ("contact", False), "m2": ("unknown", True), "m3": ("unknown", True), "m4": ("unknown", True),
        "m5": ("owner", False), "m6": ("unknown", True), "m7": ("unknown", True), "m8": ("owner", False),
        "m9": ("contact", False), "m10": ("owner", False)}


# ---------------------------------------------------------------------------
# Message status (B.2) and writer input (B.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_message_status_reaches_the_writer_as_the_table_says(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    history = plug._history_support
    chat = 5607
    old = timedelta(days=40)  # always in an earlier month file than NOW
    raw(plug, chat, [(1, "in", "old question", old), (2, "out", "old answer", old - timedelta(minutes=1))],
        username="u5607")
    await seed(plug, chat, "u5607", [(3, "in", "price question", 120), (4, "out", "it is 40 euros", 115)])
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    assert refs(contact_payloads(fake)[0]) == ["m1", "m2", "m3", "m4"]
    first = read_dossier(ds, chat)

    # Retention prunes the old month file (1 and 2), then 1's deletion arrives: its create record is gone.
    chat_dir = history._history_chat_dir_path(BUSINESS_ID, chat)
    (pruned,) = [p for p in chat_dir.iterdir() if p.name.startswith(f"{ago(old):%Y-%m}")]
    pruned.unlink()
    history.reset_in_memory_caches()
    await delete(plug, chat, 1, when=100)
    await edit(plug, chat, 3, "price question, edited", 90)
    await delete(plug, chat, 4, when=80)
    await seed(plug, chat, "u5607", [(5, "in", "a new question", 60), (6, "in", "sent and deleted unread", 58)])
    await delete(plug, chat, 6, when=57)
    maintain(plug, 50)

    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    (payload,) = contact_payloads(fake)
    assert (payload["mode"], payload["current_dossier"]) == ("update", first["document"])
    assert payload["messages"] == [
        {"ref": "m1", "deleted": True},  # deleted after its create record was pruned: a ref only
        {"ref": "m3", "at": local(120), "from": "contact", "text": "price question, edited", "edited": True},
        {"ref": "m4", "at": local(115), "from": "owner", "text": "it is 40 euros", "deleted": True},
        {"ref": "m5", "at": local(60), "from": "contact", "text": "a new question"},
    ]  # 2 is gone (pruned, never deleted) and 6 was deleted before it was ever read: neither is sent
    assert sorted(read_dossier(ds, chat)["seen"], key=int) == ["3", "5"]  # a deletion is reported once

    report = cycle(ds, FakeLlm(), target=("contact", catalog_entry(plug, chat)))
    assert (report.calls, report.unchanged) == (0, 1)


@pytest.mark.asyncio
async def test_writer_input_is_in_time_order_in_local_time_with_no_names_or_ids(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    local_zone(monkeypatch, TOKYO)
    zone = ZoneInfo(TOKYO)
    chat = 5608
    # Written out of send order: message 12 arrives first, 11 (sent earlier) afterwards.
    await seed(plug, chat, "casey5608", [
        (12, "out", "second message original", 50),
        (11, "in", "first message", 60),
        (13, "in", "third message", 40),
    ], first_name="Casey", last_name="Customer")
    await edit(plug, chat, 12, "second message edited", 45)
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1

    (call,) = contact_calls(fake)
    today = NOW.astimezone(zone)
    assert call["user"] == {
        "today": f"{today:%Y-%m-%d}, {ds.WEEKDAYS[today.weekday()]}",
        "mode": "first",
        "current_dossier": None,
        "messages": [
            {"ref": "m11", "at": local(60, zone), "from": "contact", "text": "first message"},
            {"ref": "m12", "at": local(50, zone), "from": "owner", "text": "second message edited"},
            {"ref": "m13", "at": local(40, zone), "from": "contact", "text": "third message"},
        ],
    }
    assert (call["system"], call["purpose"]) == (ds._writer.WRITER_SYSTEM, "telesec_dossier")
    for leaked in ("original", "Casey", "Customer", "casey5608", "owner_account", str(chat), BUSINESS_ID,
                   str(OWNER_ID), str(CONTACT_ID)):
        assert leaked not in call["raw"], leaked


@pytest.mark.asyncio
async def test_an_update_flags_changes_and_sends_the_ten_newest_seen_messages_as_context(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    chat = 5609
    await seed(plug, chat, "u5609", [(i, "in" if i % 2 else "out", f"message {i}", 200 - 5 * i) for i in range(1, 15)])
    assert cycle(ds, FakeLlm()).refreshed == 1
    first = read_dossier(ds, chat)["document"]

    await edit(plug, chat, 14, "message 14, edited", 100)
    await seed(plug, chat, "u5609", [(15, "in", "a new question", 60)])
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    (payload,) = contact_payloads(fake)
    assert set(payload) == {"today", "mode", "current_dossier", "context", "messages"}
    assert (payload["mode"], payload["current_dossier"]) == ("update", first)
    assert refs(payload) == ["m14", "m15"]
    assert message(payload, "m14")["edited"] is True and "edited" not in message(payload, "m15")
    assert [c["ref"] for c in payload["context"]] == [f"m{i}" for i in range(4, 14)]  # the newest before m14
    assert all(set(c) == {"ref", "at", "from", "text"} for c in payload["context"])


@pytest.mark.asyncio
async def test_update_context_stays_under_its_character_limit(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    chat = 5610
    rows = [(i, "in" if i % 2 else "out", f"message {i:02d} " + "word " * 119, 200 - 5 * i) for i in range(1, 13)]
    await seed(plug, chat, "u5610", rows)
    assert cycle(ds, FakeLlm()).refreshed == 1
    await seed(plug, chat, "u5610", [(13, "in", "a short new question", 60)])
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    (payload,) = contact_payloads(fake)
    context = payload["context"]
    assert sum(len(c["text"]) for c in context) <= ds._writer.CONTEXT_MAX_CHARS
    assert 0 < len(context) < ds._writer.CONTEXT_MAX and context[-1]["ref"] == "m12"


# ---------------------------------------------------------------------------
# First pass and batching (B.4, B.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_first_pass_needs_a_received_and_a_sent_message_in_the_window(
    plug: ModuleType, dossiers: ModuleType
) -> None:
    ds = dossiers
    await seed(plug, 5611, "u5611", [(1, "in", "anyone there?", 60)])
    raw(plug, 5612, [(1, "out", "old owner message", timedelta(days=100))], username="u5612")
    await seed(plug, 5612, "u5612", [(2, "in", "recent contact message", 60)])
    await seed(plug, 5613, "u5613", [(1, "in", "a voice note for you", 60), (2, "bot", f"{MARK} transcript only", 55)])
    await seed(plug, 5614, "u5614", [(1, "in", "a question, deleted", 60), (2, "out", "an answer", 55)])
    await delete(plug, 5614, 1, when=50)
    maintain(plug, 40)
    fake = FakeLlm()
    report = cycle(ds, fake)
    assert (report.unqualified, report.calls) == (4, 0)  # a transcript is not the owner; deleted messages never count
    assert all(read_dossier(ds, chat) is None for chat in (5611, 5612, 5613, 5614))

    await seed(plug, 5611, "u5611", [(2, "out", "yes, here", 30)])
    report = cycle(ds, fake)
    assert (report.refreshed, report.unchanged) == (1, 3)
    (payload,) = contact_payloads(fake)
    assert refs(payload) == ["m1", "m2"]


@pytest.mark.asyncio
async def test_the_first_pass_takes_the_window_and_the_newest_messages_up_to_the_cap(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None]
) -> None:
    ds = dossiers
    dossier_env(BACKFILL_MESSAGES=10)
    chat = 5615
    raw(plug, chat, [(1, "in", "ancient question", timedelta(days=100)),
                     (2, "out", "ancient answer", timedelta(days=100, minutes=-1))], username="u5615")
    tie = 200  # messages 12-15 share one timestamp at the cut
    rows = [(i, "in" if i % 2 else "out", f"recent message {i}", 300 - i) for i in range(10, 12)]
    rows += [(i, "in" if i % 2 else "out", f"recent message {i}", tie) for i in range(12, 16)]
    rows += [(i, "in" if i % 2 else "out", f"recent message {i}", 180 - i) for i in range(16, 24)]
    await seed(plug, chat, "u5615", rows)
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1

    (payload,) = contact_payloads(fake)
    assert refs(payload) == [f"m{i}" for i in range(14, 24)] and "part" not in payload
    doc = read_dossier(ds, chat)
    assert (doc["older_history_not_sent"], doc["messages"]) == (True, 10)
    assert ds._parse(doc["window_start"]) == ago(tie) == ds._parse(doc["floor"])
    assert sorted(doc["seen"], key=int) == [str(i) for i in range(14, 24)]

    # Messages cut at the boundary (12, 13 share window_start) are never sent later.
    forced = cycle(ds, fake, target=("contact", catalog_entry(plug, chat)))
    assert (forced.calls, forced.unchanged) == (0, 1)


def test_batches_split_by_message_count_and_by_characters(dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    ds = dossiers
    assert (ds.BATCH_MAX_MESSAGES, ds.BATCH_MAX_CHARS) == (500, 120_000)
    items = [ds.Item(str(i), "deleted", NOW, None) for i in range(5)]  # a ref-only deletion counts 80 characters
    assert [len(b) for b in ds.batches(items)] == [5]
    assert ds.batches([]) == []
    monkeypatch.setattr(ds, "BATCH_MAX_CHARS", 200)
    assert [len(b) for b in ds.batches(items)] == [2, 2, 1]
    monkeypatch.setattr(ds, "BATCH_MAX_CHARS", 120_000)
    monkeypatch.setattr(ds, "BATCH_MAX_MESSAGES", 3)
    assert [[item.key for item in b] for b in ds.batches(items)] == [["0", "1", "2"], ["3", "4"]]


@pytest.mark.asyncio
async def test_a_large_first_pass_runs_in_parts_that_each_commit(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    monkeypatch.setattr(ds, "BATCH_MAX_MESSAGES", 3)
    rows = [(i, "in" if i % 2 else "out", f"message {i}", 100 - 5 * i) for i in range(1, 8)]
    await seed(plug, 5616, "u5616", rows)
    await seed(plug, 5617, "u5617", rows)

    fake = FakeLlm()
    report = cycle(ds, fake, target=("contact", catalog_entry(plug, 5616)))
    assert (report.refreshed, report.calls) == (1, 3)
    payloads = contact_payloads(fake)
    assert [p["part"] for p in payloads] == [{"index": i, "total": 3} for i in (1, 2, 3)]
    assert [p["mode"] for p in payloads] == ["first", "update", "update"]
    assert [refs(p) for p in payloads] == [["m1", "m2", "m3"], ["m4", "m5", "m6"], ["m7"]]
    assert [p["current_dossier"] for p in payloads] == [None, quoted("message 1"), quoted("message 5")]
    assert [c["ref"] for c in payloads[1]["context"]] == ["m1", "m2", "m3"]
    doc = read_dossier(ds, 5616)
    assert (doc["document"], doc["revision"], doc["messages"]) == (quoted("message 7"), 3, 7)

    # Part 2 times out: part 1 stays committed and the next run sends only the rest.
    failing = FakeLlm(quote_first, TimeoutError("timed out"))
    report = cycle(ds, failing, target=("contact", catalog_entry(plug, 5617)))
    assert (report.failed, report.failures) == (1, [(f"{BUSINESS_ID}:5617", "TimeoutError")])
    partial = read_dossier(ds, 5617)
    assert sorted(partial["seen"], key=int) == ["1", "2", "3"]
    assert (partial["document"], partial["revision"]) == (quoted("message 1"), 1)

    resumed = FakeLlm()
    assert cycle(ds, resumed, target=("contact", catalog_entry(plug, 5617))).refreshed == 1
    payloads = contact_payloads(resumed)
    assert [refs(p) for p in payloads] == [["m4", "m5", "m6"], ["m7"]]
    assert all(p["mode"] == "update" and "part" not in p for p in payloads)  # an ordinary update now
    assert sorted(read_dossier(ds, 5617)["seen"], key=int) == [str(i) for i in range(1, 8)]


@pytest.mark.asyncio
async def test_preview_matches_the_next_call_and_writes_nothing(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await two_sided(plug, 5618, "PREVIEW")
    await seed(plug, 5619, "u5619", [(1, "in", "not answered yet", 60)])
    entry = catalog_entry(plug, 5618)
    history_before = snapshot_history_tree(plug)
    dossiers_before = snapshot_tree(ds.dossier_root())

    system, user, info, gate, hints = ds.preview_contact(entry, now=NOW)
    assert (system, gate, hints) == (ds._writer.WRITER_SYSTEM, False, 0)
    assert info == ds.Pending(first=True, qualified=True, new=2, edited=0, deleted=0, note_changed=False)
    assert ds.pending(entry, now=NOW) == info and not info.nothing()
    assert snapshot_history_tree(plug) == history_before
    assert snapshot_tree(ds.dossier_root()) == dossiers_before

    fake = FakeLlm()
    assert cycle(ds, fake, target=("contact", entry)).refreshed == 1
    assert contact_calls(fake)[0]["raw"] == user
    _system, user, info, _gate, _hints = ds.preview_contact(entry, now=NOW)
    assert user == "" and info.nothing()
    _system, user, info, _gate, _hints = ds.preview_contact(catalog_entry(plug, 5619), now=NOW)
    assert user == "" and (info.first, info.qualified) == (True, False)


@pytest.mark.asyncio
async def test_fact_hints_join_the_writer_input_and_fail_open(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    ds.set_clairic("on", via="cli")
    asked: list[list[str]] = []

    def hints(messages: list[Any], **_kwargs: Any) -> Any:
        asked.append([m.text for m in messages])
        return ds._writer.HintResult(hints=[{"kind": "date", "value": "2026-10-28"}], input_tokens=2000, ms=3)

    def fails(messages: list[Any], **_kwargs: Any) -> Any:
        asked.append([m.text for m in messages])
        raise ds.model_client.ModelCallError("timeout")

    month = f"{NOW:%Y-%m}"
    await two_sided(plug, 5620, "HINT")
    monkeypatch.setattr(ds._writer, "clairic_hints", hints)
    with fake_secret_scope(CF_KEYS):
        fake = FakeLlm()
        assert cycle(ds, fake).refreshed == 1
        (payload,) = contact_payloads(fake)
        assert payload["hints"] == [{"kind": "date", "value": "2026-10-28"}]
        assert asked == [["HINT question from the contact", "HINT answer from the owner"]]
        assert read_state(ds)["usage"][month]["clairic"] == {"calls": 1, "input_tokens": 2000, "cost_usd": 0.0002}

        monkeypatch.setattr(ds._writer, "clairic_hints", fails)
        await seed(plug, 5620, "user5620", [(3, "in", "HINT follow-up", 30)])
        fake = FakeLlm()
        assert cycle(ds, fake).refreshed == 1  # the writer runs without hints
        (payload,) = contact_payloads(fake)
        assert "hints" not in payload and refs(payload) == ["m3"]
        assert read_state(ds)["usage"][month]["clairic"]["errors"] == 1

    await seed(plug, 5620, "user5620", [(4, "in", "HINT again", 20)])
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1  # without credentials nothing goes to Cloudflare
    assert len(asked) == 2 and "hints" not in contact_payloads(fake)[0]


# ---------------------------------------------------------------------------
# Gating and worker
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
    fake = FakeLlm()
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
async def test_a_disabled_cycle_reads_no_history_and_writes_nothing(
    dossier_history: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = dossier_history
    await two_sided(plugin, 5502, "OFF")
    ds = dossier_module(plugin)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("history must not be read when dossiers are disabled")

    history = plugin._history_support
    monkeypatch.setattr(history, "_streamed_records_locked", forbidden)
    monkeypatch.setattr(history, "_catalog_entries_for_cli", forbidden)
    monkeypatch.setattr(ds, "read_chat_view", forbidden)
    monkeypatch.setattr(ds, "private_entries", forbidden)
    fake = FakeLlm()
    assert cycle(ds, fake).outcome == "disabled"
    assert cycle(ds, fake, target=("persona", None)).outcome == "disabled"
    assert fake.calls == []
    assert not ds.dossier_root().exists()  # no lock file was created


@pytest.mark.asyncio
async def test_register_binds_the_facade_but_starts_no_worker(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await two_sided(plug, 5503, "REGISTER")
    fake = FakeLlm()
    ctx = FakeCtx(fake, with_unload=True)
    plug.register(ctx)
    assert ds._LLM is fake  # register binds the facade for CLI runs, but starts nothing

    entry = catalog_entry(plug, 5503)
    assert ds.pending(entry, now=NOW).first is True
    assert json.loads(ds.preview_contact(entry, now=NOW)[1])["mode"] == "first"
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
async def test_worker_builds_contacts_and_the_persona_from_fixture_history(
    plug: ModuleType, dossiers: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    dossier_env(INTERVAL_MINUTES=1440)
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    for chat_id, tag in ((5504, "WORKER-A"), (5505, "WORKER-B")):
        owner = [(i, "out", f"{tag} owner message {i}", 90 - i) for i in range(2, 7)]  # 10 owner messages in all
        await seed(plug, chat_id, f"user{chat_id}", [(1, "in", f"{tag} question from the contact", 95), *owner])
    fake = FakeLlm()
    ds.bind_llm(fake)

    connect_gateway(plug)
    wait_worker_cycle(ds, timeout=10)

    assert (len(fake.calls), len(contact_calls(fake)), len(persona_calls(fake))) == (3, 2, 1)
    for chat_id, tag in ((5504, "WORKER-A"), (5505, "WORKER-B")):
        doc = ds.load_contact(BUSINESS_ID, chat_id)
        assert doc["document"] == quoted(f"{tag} question from the contact")
    persona = read_persona(ds)
    assert persona["document"] == "## Style\n- Short, polite messages without emoji."
    assert (len(persona["chats"]), persona["messages"]) == (2, 10)
    last = read_state(ds)["last_cycle"]
    assert (last["outcome"], last["trigger"], last["persona"]) == ("completed", "worker", "refreshed")


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
    fake = FakeLlm()
    ds.bind_llm(fake)

    with ds._cycle_lock() as acquired:
        assert acquired
        assert ds.cycle_running()
        assert cycle(ds, fake).outcome == "busy"
        assert cycle(ds, fake, target=("contact", catalog_entry(plug, 5507))).outcome == "busy"
    assert fake.calls == []
    assert not ds.cycle_running()
    assert cycle(ds, fake).outcome == "completed"
    assert fake.calls


# ---------------------------------------------------------------------------
# Stored on/off switch
# ---------------------------------------------------------------------------


def iso(value: Any) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_set_enabled_api_validates_and_records_time_and_source(dossier_history: ModuleType) -> None:
    ds = dossier_module(dossier_history)
    assert ds.load_settings() == {"schema_version": 1, "enabled": False, "updated_at": None, "updated_via": None}
    with pytest.raises(ValueError, match="unknown switch source"):
        ds.set_enabled(True, via="env")
    assert not ds.dossier_root().exists()

    at = NOW - timedelta(minutes=5)
    on = ds.set_enabled(True, via="telegram", now=at)
    assert on == {"schema_version": 1, "enabled": True, "updated_at": iso(at), "updated_via": "telegram",
                  "changed": True}
    path = ds.settings_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ds.dossier_root().stat().st_mode) == 0o700
    assert read_settings(ds) == ds.load_settings() == {k: v for k, v in on.items() if k != "changed"}
    assert ds.load_config().enabled is True
    before = path.read_bytes()
    assert ds.set_enabled(True, via="cli", now=NOW) == {**on, "changed": False}
    assert path.read_bytes() == before  # idempotent: the switch time stays the first one
    off = ds.set_enabled(False, via="cli", now=NOW)
    assert (off["changed"], off["enabled"], off["updated_at"], off["updated_via"]) == (True, False, iso(NOW), "cli")
    assert ds.load_config().enabled is False

    path.write_text("{", encoding="utf-8")
    assert ds.set_enabled(False, via="cli", now=NOW)["changed"] is True  # an explicit switch repairs a bad file
    assert "error" not in ds.load_settings()


@pytest.mark.asyncio
async def test_old_env_or_desktop_setting_moves_into_the_switch_once(
    dossier_history: ModuleType, dossier_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin = dossier_history
    ds = dossier_module(plugin)
    ds.set_task_registered(True)
    await two_sided(plugin, 5560, "SEED")
    settings_file = ds.settings_path()
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
    assert ds.load_settings()["updated_via"] == "migration"

    ds.set_enabled(False, via="telegram")
    dossier_env(ENABLE="1")
    assert ds.migrate_legacy_switch() is False  # once only: a stored switch is never overwritten
    cfg = ds.load_config()
    assert (cfg.enabled, cfg.warnings) == (False, ())
    fake = FakeLlm()
    assert cycle(ds, fake).outcome == "disabled"
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{not json", '{"schema_version": 1, "enabled": "yes"}', '{"enabled": true}',
                                     "[]", ""])
async def test_corrupt_switch_reads_disabled_with_warning_until_rewritten(
    plug: ModuleType, dossiers: ModuleType, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    ds = dossiers
    await two_sided(plug, 5561, "BADSWITCH")
    path = ds.settings_path()
    path.write_text(content, encoding="utf-8")
    settings = ds.load_settings()
    assert settings["enabled"] is False and "unreadable or corrupt" in settings["error"]
    cfg = ds.load_config()
    assert cfg.enabled is False
    assert len(cfg.warnings) == 1 and "settings.json is unreadable or corrupt" in cfg.warnings[0]
    assert cfg.warnings[0].startswith("warning: ")
    fake = FakeLlm()
    assert cycle(ds, fake).outcome == "disabled" and fake.calls == []
    assert path.read_text(encoding="utf-8") == content  # never overwritten silently
    ds.bind_llm(FakeLlm())
    with caplog.at_level(logging.WARNING):
        assert ds.ensure_worker_started() is True  # the gateway log says why dossiers stay off
    assert "dossier switch settings.json is unreadable or corrupt" in caplog.text
    ds.stop_workers(timeout=2)

    assert ds.set_enabled(True, via="cli")["changed"] is True  # an explicit switch rewrites it
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
    fake = FakeLlm()
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
        assert len(contact_calls(fake)) == 1 and persona_calls(fake) == []  # one owner message: no persona yet
        assert read_dossier(ds, 5562) is not None
        assert read_state(ds)["last_cycle"]["trigger"] == "worker"
        assert live_worker_threads() == threads  # the same thread: no restart
    finally:
        ds.stop_workers(timeout=2)
        ds.bind_llm(None)
    assert ds.wake_worker() is False  # nothing left to wake


@pytest.mark.asyncio
async def test_an_enable_from_another_process_reaches_the_worker_by_polling_after_the_connect_delay(
    dossier_history: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = dossier_history
    ds = dossier_module(plugin)
    await two_sided(plugin, 5563, "POLL")
    delay = 0.8
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", delay)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 0.05)
    outcomes = spy_worker_cycles(monkeypatch, ds)
    fake = FakeLlm()
    ds.bind_llm(fake)
    try:
        started = time.monotonic()
        connect_gateway(plugin)
        ds.set_enabled(True, via="cli")  # as from another process: no wake reaches the worker
        assert ds.wake_worker() is True  # and a wake never shortens the post-connect delay
        await asyncio.sleep(0.3)
        assert outcomes == [] and fake.calls == []
        wait_worker_cycle(ds, timeout=10)
        assert time.monotonic() - started >= delay
        assert outcomes == ["completed"] and len(fake.calls) == 1
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
    fake = FakeLlm()
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
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_busy_cycle_retries_after_a_poll_not_an_interval(
    plug: ModuleType, dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ds = dossiers
    await two_sided(plug, 5566, "BUSY")
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(ds, "DISABLED_POLL_SECONDS", 0.05)
    outcomes = spy_worker_cycles(monkeypatch, ds)
    ds.bind_llm(FakeLlm())
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
    fake = FakeLlm()
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
    ds.bind_llm(FakeLlm())
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
# Rate limit
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
    fake = FakeLlm()
    times: list[float] = []
    fake.on_call = lambda _payload: times.append(clock())
    report = cycle(ds, fake, limiter=ds.RateLimiter(2, clock=clock, wait=clock.wait))

    assert (report.refreshed, report.calls, report.persona) == (3, 3, "not_due")
    assert times[:2] == [1_000.0, 1_000.0]
    assert times[2] >= 1_060.0  # the third call waited for the window
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


LONG_REPLY = doc("**The contact.**\n\n## Notes\n" + "\n".join(f"- Fact number {i}: " + "detail " * 10 for i in range(60)))


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit", "waits", "recent"), [
    (2, [], [1_000.0, 1_000.0]),
    (1, [pytest.approx(60.0)], [pytest.approx(1_060.0)]),  # one call a minute: the shorten call waits for the window
])
async def test_a_shorten_call_takes_its_own_rate_limit_slot(
    plug: ModuleType, dossiers: ModuleType, limit: int, waits: list[float], recent: list[float]
) -> None:
    ds = dossiers
    await two_sided(plug, 5544, "SHORTEN")
    clock = FakeClock()
    fake = FakeLlm(LONG_REPLY)  # too long: the default reply answers the shorten call
    report = cycle(ds, fake, target=("contact", catalog_entry(plug, 5544)),
                   limiter=ds.RateLimiter(limit, clock=clock, wait=clock.wait))

    assert [c["purpose"] for c in fake.calls] == ["telesec_dossier", "telesec_shorten"]
    assert (report.refreshed, report.calls) == (1, 2)
    assert clock.waits == waits
    assert read_state(ds)["recent_calls"] == recent  # the window `dossier run` shares


@pytest.mark.asyncio
async def test_no_shorten_call_without_a_rate_limit_slot(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    await two_sided(plug, 5545, "NOSLOT")
    stopper = FakeClock(stop=True)  # stopped while waiting for the window
    fake = FakeLlm(LONG_REPLY)
    report = cycle(ds, fake, target=("contact", catalog_entry(plug, 5545)),
                   limiter=ds.RateLimiter(1, clock=stopper, wait=stopper.wait))

    assert [c["purpose"] for c in fake.calls] == ["telesec_dossier"]
    assert (report.refreshed, report.calls) == (0, 1)
    assert read_dossier(ds, 5545) is None  # as when a switch goes off before the shorten call: nothing written
    assert read_state(ds)["usage"][f"{NOW:%Y-%m}"]["writer"]["calls"] == 1  # the call that went out is counted
    assert read_state(ds)["recent_calls"] == [1_000.0]


# ---------------------------------------------------------------------------
# Groups and history forget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_groups_never_processed(plug: ModuleType, dossiers: ModuleType) -> None:
    ds = dossiers
    # Capture never records a group since 0.14; one recorded by an older version's TELESEC_HISTORY_CHAT_TYPES.
    raw(plug, -5516, [
        (1, "in", "GROUP-SECRET question", 60),
        (2, "out", "GROUP-SECRET answer", 55),
    ], chat_type="group", title="Team room")
    assert load_records(plug, chat_id=-5516), "the group must be in history for this test to mean anything"
    await two_sided(plug, 5517, "PRIVATE")
    fake = FakeLlm()
    assert cycle(ds, fake).refreshed == 1
    assert cycle(ds, fake, target=("persona", None)).persona == "refreshed"

    assert [refs(p) for p in contact_payloads(fake)] == [["m1", "m2"]]
    assert "PRIVATE question" in contact_calls(fake)[0]["raw"]
    assert all("GROUP-SECRET" not in call["raw"] for call in fake.calls)
    assert [m["text"] for m in persona_calls(fake)[0]["user"]["messages"]] == ["PRIVATE answer from the owner"]
    assert read_dossier(ds, -5516) is None


@pytest.mark.asyncio
async def test_history_forget_erases_one_chat_and_its_dossier(plug: ModuleType, dossiers: ModuleType) -> None:
    ds, history = dossiers, plug._history_support
    await seed(plug, 5504, "forget_me", [(1, "in", "FORGET-TAG question", 90), (2, "out", "FORGET-TAG reply", 85)])
    await seed(plug, 5505, "keep_me", [(1, "in", "KEEP-TAG question", 80), (2, "out", "KEEP-TAG reply", 75)])
    assert cycle(ds, FakeLlm()).refreshed == 2
    assert cycle(ds, FakeLlm(), target=("persona", None)).persona == "refreshed"  # the persona read both chats
    ds.set_note(("persona", None), "Keep replies short.")
    persona = read_persona(ds)
    before = snapshot_history_tree(plug)

    code, out, _err = cli(plug, "history", "forget", "--chat", "5504")

    assert code == 1
    size = history._saved_in_chat_dir(history._history_chat_dir_path(BUSINESS_ID, 5504)).bytes
    assert out.splitlines() == [
        f"Would erase 2 saved messages (2 records, 1 file, {size} bytes) of @forget_me "
        + f"(connection={BUSINESS_ID} chat=5504) and its dossier.",
        "Nothing was erased. Run it again with --yes to erase; this cannot be undone.",
    ]
    assert snapshot_history_tree(plug) == before and read_dossier(ds, 5504) is not None
    assert read_persona(ds) == persona

    code, out, _err = cli(plug, "history", "forget", "--chat", "5504", "--yes")

    assert code == 0
    assert out.startswith("Erased 2 saved messages (2 records, 1 file, ")
    assert "Its dossier was forgotten too." in out
    assert "History is still on for this chat, so new messages are saved again." in out
    assert not history._history_chat_dir_path(BUSINESS_ID, 5504).exists()
    assert read_dossier(ds, 5504) is None and read_dossier(ds, 5505) is not None
    reset = read_persona(ds)  # the persona read the forgotten chat: its text goes, the owner's note stays
    assert (reset["document"], reset["previous"], reset["chats"], reset["note"]) == ("", None, [],
                                                                                    "Keep replies short.")
    assert reset["id"] != persona["id"]
    assert [str(entry["chat_id"]) for entry in history._catalog_entries_for_cli()] == ["5505"]
    assert cli(plug, "history", "show", "--chat", "5504")[0] == 1
    stats = cli(plug, "stats", "--format", "json")[1]
    assert "forget_me" not in stats and "keep_me" in stats
    fake = FakeLlm()
    report = cycle(ds, fake, now=NOW + timedelta(hours=25))
    assert (report.refreshed, report.calls, report.persona) == (0, 1, "refreshed")  # rebuilt from the chat left
    assert [m["text"] for m in persona_calls(fake)[0]["user"]["messages"]] == ["KEEP-TAG reply"]
    assert read_dossier(ds, 5504) is None

    code, out, _err = cli(plug, "history", "forget", "--chat", "5504", "--yes")
    assert code == 1 and "no matching history chat found" in out


@pytest.mark.asyncio
async def test_history_forget_skips_the_still_on_note_for_a_chat_set_to_dont_save(plug: ModuleType,
                                                                                 dossiers: ModuleType) -> None:
    ds = dossiers
    await seed(plug, 5504, "paused", [(1, "in", "question", 90), (2, "out", "reply", 85)])
    plug._chat_settings.default_store().set_flag(OWNER_ID, 5504, "history", False, via="cli")

    code, out, _err = cli(plug, "history", "forget", "--chat", "5504", "--yes")

    assert code == 0 and "It had no dossier." in out and "History is still on" not in out
    assert read_persona(ds) is None  # no persona to reset either


@pytest.mark.asyncio
async def test_history_forget_all_needs_yes_and_erases_every_chat_and_dossier(plug: ModuleType,
                                                                             dossiers: ModuleType) -> None:
    ds, history = dossiers, plug._history_support
    await seed(plug, 5504, "first", [(1, "in", "ONE question", 90), (2, "out", "ONE reply", 85)])
    await seed(plug, 5505, "second", [(1, "in", "TWO question", 80), (2, "out", "TWO reply", 75)])
    assert cycle(ds, FakeLlm()).refreshed == 2
    assert cycle(ds, FakeLlm(), target=("persona", None)).persona == "refreshed"
    before = snapshot_history_tree(plug)

    code, out, _err = cli(plug, "history", "forget", "--all")
    assert code == 1
    assert out.startswith("Would erase all saved history: 2 chats, 4 saved messages (4 records, 2 files, ")
    assert "and every dossier (contacts and the owner persona)." in out
    code, out, _err = cli(plug, "history", "forget", "--all", "--chat", "5504", "--yes")
    assert code == 1 and "--all cannot be combined" in out
    assert snapshot_history_tree(plug) == before and read_persona(ds) is not None

    code, out, _err = cli(plug, "history", "forget", "--all", "--yes")

    assert code == 0
    assert out.startswith("Erased all saved history: 2 chats, 4 saved messages (4 records, 2 files, ")
    assert "Forgot 2 contact dossiers and the owner persona." in out
    assert "History is still on, so new messages are saved again." in out
    assert read_dossier(ds, 5504) is None and read_dossier(ds, 5505) is None and read_persona(ds) is None
    assert ds.settings_path().exists() and ds.load_config().enabled is True  # forget never touches the switch
    assert history._catalog_entries_for_cli() == []
    assert cli(plug, "history", "chats")[1].strip() == "No Telegram Business history found."
