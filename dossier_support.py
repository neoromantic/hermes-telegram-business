"""Opt-in contact dossiers and the owner persona, built from Telegram Business history.

Stdlib only. The gateway worker reads canonical history under its read locks, asks the host
``ctx.llm`` facade to rewrite one Markdown document per contact (``dossier_writer``), checks the
answer and stores it in ``document.json`` (``dossier_doc``) next to, never inside, history.
Logs carry counts, codes, timings and hashed chat refs only: never message, document or note
text, names, raw ids or keys.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from . import dossier_doc as _doc
from . import dossier_migrate, model_client
from . import dossier_writer as _writer
from . import history_support as _history

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Dossiers follow only the stored switch (Settings or `dossier enable|disable`). Before 0.11.0 this variable
# or the Desktop setting turned them on; migrate_legacy_switch() carries an "on" into the switch once.
LEGACY_ENABLE_ENV = "TELESEC_DOSSIER_ENABLE"
LEGACY_ENABLE_SETTING = "dossier_enable"
ENABLE_COMMAND = "hermes telesec dossier enable"
DISABLE_COMMAND = "hermes telesec dossier disable"
# A chat set to "No dossier" (or "Don't save") in its panel or with `hermes telesec chat set`.
CHAT_OFF = "Dossier is off for this chat."
INTERVAL_ENV = "TELESEC_DOSSIER_INTERVAL_MINUTES"
QUIET_ENV = "TELESEC_DOSSIER_QUIET_MINUTES"
BACKFILL_DAYS_ENV = "TELESEC_DOSSIER_BACKFILL_DAYS"
BACKFILL_MESSAGES_ENV = "TELESEC_DOSSIER_BACKFILL_MESSAGES"
MAX_CALLS_ENV = "TELESEC_DOSSIER_MAX_CALLS_PER_MINUTE"
# Deprecated: an explicit writer route as in v1, used only while the host config names no task provider.
PROVIDER_ENV = "TELESEC_DOSSIER_PROVIDER"
MODEL_ENV = "TELESEC_DOSSIER_MODEL"
CLEANUP_PROVIDER_ENV = "TELESEC_CLEANUP_PROVIDER"  # the voice-cleanup route; never routes dossiers

TASK_KEY = "telesec_dossier"  # the Hermes auxiliary task ("hermes model" -> Telesec dossiers)
TASK_DEFAULTS: dict[str, Any] = {
    "provider": "ai-gateway",
    "model": "google/gemini-3.8-flash",
    "timeout": 120,
    "reasoning_effort": "low",
    "extra_body": {"max_tokens": 8192, "providerOptions": {"gateway": {"zeroDataRetention": True}}},
}
FALLBACK_PROVIDER = "ai-gateway"  # explicit route when the host cannot register the task
FALLBACK_MODEL = "google/gemini-3.8-flash"
LEGACY_GEMINI_MODEL = "gemini-3.8-flash"  # v1 default model name on the native gemini/google providers
NOT_SET = "(not set)"
GATE_DEFAULT = "off"  # the message check (Jev) mode when settings.json has no "gate"
GATE_MODES = ("auto", "off")
CLAIRIC_MODES = ("on", "off")

SCHEMA_VERSION = 1
INITIAL_DELAY_SECONDS = 180  # "a few minutes after gateway start"
DISABLED_POLL_SECONDS = 60  # how often an idle worker re-reads the stored switch
SETTINGS_VIA = ("cli", "telegram")
MODEL_TIMEOUT_SECONDS = 120.0  # explicit routes only; the task route uses auxiliary.telesec_dossier.timeout
FOLD_MARGIN = timedelta(days=2)  # message_at vs observed_at skew
BATCH_MAX_MESSAGES = 500
BATCH_MAX_CHARS = 120_000  # sum(len(text) + 80) per call; a Telegram text is <= 4096 chars
PERSONA_MAX_MESSAGES = 300
PERSONA_MAX_CHARS = 60_000
PERSONA_MSG_MAX_CHARS = 1000
PERSONA_MIN_FIRST = 10
PERSONA_MIN_NEW = 20
PERSONA_MIN_AGE = timedelta(days=7)
PERSONA_CHECK_EVERY = timedelta(hours=24)
TRANSCRIPT_CHAIN_SECONDS = 10
BACKOFF_AFTER = 3
BACKOFF_MAX_HOURS = 24
CLAIRIC_PRICE_PER_M = 0.10  # USD per million input tokens
USAGE_MONTHS = 3
CATALOG_RETRY_SECONDS = 5
TRANSCRIPT_MARKER = "\U0001F399"  # first code point of the plugin transcript marker
REPLACED = frozenset({"likely_duplicate", "likely_correction"})
LABELS = {"inbound": "contact", "outbound": "owner"}
SETTINGS_FILE = "settings.json"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class DossierConfig:
    enabled: bool
    interval_minutes: int
    quiet_minutes: int
    backfill_days: int
    backfill_messages: int
    max_calls_per_minute: int
    provider: str  # effective writer provider; "auto" when the task follows the main model
    model: str  # effective writer model; "main model" when provider is "auto"
    reasoning: str = "low"  # effective reasoning_effort; "" = provider default; NOT_SET on an explicit route
    max_tokens: int | None = 8192  # None when extra_body has no max_tokens
    zdr: bool = True  # extra_body asks the gateway for zero data retention
    task_registered: bool = True
    legacy_env: bool = False  # TELESEC_DOSSIER_PROVIDER routes the writer
    gate: str = "off"  # message check: on | off | no_key | not_gateway | bad_base_url
    gate_setting: str = GATE_DEFAULT  # auto | off
    clairic: str = "off"  # fact hints: on | off | no_key
    warnings: tuple[str, ...] = ()


def _env_int_bounded(name: str, default: int, lo: int, hi: int, warnings: list[str]) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not lo <= value <= hi:
        warnings.append(f"warning: {name}={raw} ignored (allowed {lo}..{hi}); using {default}")
        return default
    return value


_TASK_REGISTERED = False  # set by register() once the host accepted the auxiliary task


def set_task_registered(ok: bool) -> None:
    global _TASK_REGISTERED
    _TASK_REGISTERED = bool(ok)


def _host_task_config() -> dict[str, Any]:
    """``auxiliary.telesec_dossier`` from the Hermes config, read only; {} outside Hermes or on any error."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore[import-not-found]

        task = load_config_readonly()["auxiliary"][TASK_KEY]
    except Exception:  # noqa: BLE001 - an unreadable host config means the task defaults apply
        return {}
    return dict(task) if isinstance(task, dict) else {}


def _stripped(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def without_credentials(value: str) -> str:
    """``value`` without user info (up to the last "@", so an "@" in a password is covered), query or fragment.

    For showing a provider or model that may be a URL. With a scheme any "@" ends user info; without
    one only "name:secret@", so "model@version" stays.
    """
    scheme, sep, rest = value.strip().partition("://")
    if not sep:
        scheme, rest = "", scheme
    userinfo, at, host = rest.rpartition("@")
    if at and (sep or ":" in userinfo):
        rest = host
    return re.split(r"[?#]", scheme + sep + rest, maxsplit=1)[0].strip()


@dataclass(frozen=True)
class _Effective:
    route: _writer.Route
    provider: str
    model: str
    reasoning: str
    max_tokens: int | None
    zdr: bool
    legacy: bool
    notes: tuple[tuple[str, str], ...]  # (log code, warning line)


def _effective() -> _Effective:
    """The writer route and its effective settings, merged the way Hermes merges task defaults."""
    user = _host_task_config()
    env_provider = os.environ.get(PROVIDER_ENV, "").strip()
    env_model = os.environ.get(MODEL_ENV, "").strip()
    notes: list[tuple[str, str]] = []
    if env_provider and not _stripped(user.get("provider")):
        model = env_model or (LEGACY_GEMINI_MODEL if env_provider.lower() in ("gemini", "google") else FALLBACK_MODEL)
        # The route keeps the values as set; everything shown or logged drops URL credentials.
        shown_provider, shown_model = without_credentials(env_provider), without_credentials(model)
        text = (f"warning: {PROVIDER_ENV}/{MODEL_ENV} are deprecated; dossiers use {shown_provider} / {shown_model} "
                "from them. Remove them to use the Telesec dossiers task (choose its model with 'hermes model').")
        return _Effective(_writer.Route(None, env_provider, model), shown_provider, shown_model, NOT_SET, None, False,
                          True, (("legacy_env", text),))
    if env_provider or env_model:
        notes.append(("env_ignored", f"warning: {PROVIDER_ENV}/{MODEL_ENV} are ignored; remove them."))
    if not _TASK_REGISTERED:
        text = ("warning: the Telesec dossiers model task is not registered (Hermes older than 0.21.6?); calls go to "
                f"{FALLBACK_PROVIDER} / {FALLBACK_MODEL} directly and need "
                f"plugins.entries.{_history.PLUGIN_NAME}.llm trust config.")
        notes.append(("not_registered", text))
        eff = _Effective(_writer.Route(None, FALLBACK_PROVIDER, FALLBACK_MODEL), FALLBACK_PROVIDER, FALLBACK_MODEL,
                         NOT_SET, None, False, False, ())
    else:
        eff = _task_effective({**TASK_DEFAULTS, **user}, notes)
    if os.environ.get(CLEANUP_PROVIDER_ENV, "").strip() and not env_provider:
        text = (f"note: dossiers no longer follow {CLEANUP_PROVIDER_ENV}; they use the Telesec dossiers task "
                f"({eff.provider} / {eff.model}).")
        notes.append(("cleanup_env", text))
    return _Effective(eff.route, eff.provider, eff.model, eff.reasoning, eff.max_tokens, eff.zdr, False, tuple(notes))


def _task_effective(merged: dict[str, Any], notes: list[tuple[str, str]]) -> _Effective:
    provider = _stripped(merged.get("provider"))
    if provider in ("", "auto"):
        provider, model = "auto", "main model"
        notes.append(("auto", ("warning: the Telesec dossiers task follows the main model (auto): dossier text "
                               "goes to the main model. Choose a model with 'hermes model' → Telesec dossiers.")))
    else:
        model = _stripped(merged.get("model")) or "provider default"
    reasoning = _stripped(merged.get("reasoning_effort"))
    if reasoning != "low":
        notes.append(("reasoning", (f"warning: reasoning_effort for Telesec dossiers is "
                                    f"'{reasoning or 'provider default'}'; 'low' is about 3x cheaper. "
                                    f"Set auxiliary.{TASK_KEY}.reasoning_effort: low.")))
    extra = merged.get("extra_body")
    extra = extra if isinstance(extra, dict) else {}
    max_tokens = extra.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        max_tokens = None
        notes.append(("max_tokens", (f"warning: auxiliary.{TASK_KEY}.extra_body replaces the default and has no "
                                     "max_tokens; long answers are not capped.")))
    options = extra.get("providerOptions")
    gateway = options.get("gateway") if isinstance(options, dict) else None
    zdr = isinstance(gateway, dict) and gateway.get("zeroDataRetention") is True
    if provider.lower() in _writer.GATEWAY_ALIASES and not zdr:
        notes.append(("zdr", ("warning: zero data retention is off for Telesec dossiers "
                              f"(auxiliary.{TASK_KEY}.extra_body has no providerOptions.gateway.zeroDataRetention).")))
    return _Effective(_writer.Route(TASK_KEY, None, None), without_credentials(provider), without_credentials(model),
                      reasoning, max_tokens, zdr, False, ())


def route() -> _writer.Route:
    """How writer calls are routed: the legacy env route, the registered task, or the explicit fallback."""
    return _effective().route


def _gate_status(setting: str, provider: str) -> str:
    if setting != "auto":
        return "off"
    if provider.lower() not in _writer.GATEWAY_ALIASES:
        return "not_gateway"  # chat text would reach TypeSafe through a gateway the writer does not use
    if model_client.gateway_base() is None:
        return "bad_base_url"
    if model_client.gateway_key() is None:
        return "no_key"
    return "on"


def _clairic_status(setting: str) -> str:
    if setting != "on":
        return "off"
    return "on" if model_client.cloudflare_credentials() is not None else "no_key"


_WARNED: set[str] = set()  # warning codes already logged by this process


def load_config() -> DossierConfig:
    """Read the stored switch, the env surface and the host task config; never cached."""
    warnings: list[str] = []
    codes: list[str] = []
    settings = load_settings()
    if settings.get("error"):
        warnings.append(f"warning: {settings['error']}")
        codes.append("settings")
    ints = {name: _env_int_bounded(name, default, lo, hi, warnings) for name, default, lo, hi in (
        (INTERVAL_ENV, 30, 5, 1440), (QUIET_ENV, 10, 0, 1440), (BACKFILL_DAYS_ENV, 90, 1, 3650),
        (BACKFILL_MESSAGES_ENV, 500, 10, 5000), (MAX_CALLS_ENV, 20, 1, 600))}
    if len(warnings) > len(codes):
        codes.append("env_range")
    eff = _effective()
    for code, line in eff.notes:
        codes.append(code)
        warnings.append(line)
    for code in dict.fromkeys(codes):
        if code not in _WARNED:
            _WARNED.add(code)
            logger.warning("%s: dossier config warning=%s", _history.PLUGIN_NAME, code)
    gate_setting = settings.get("gate", GATE_DEFAULT)
    return DossierConfig(
        enabled=settings["enabled"],
        interval_minutes=ints[INTERVAL_ENV],
        quiet_minutes=ints[QUIET_ENV],
        backfill_days=ints[BACKFILL_DAYS_ENV],
        backfill_messages=ints[BACKFILL_MESSAGES_ENV],
        max_calls_per_minute=ints[MAX_CALLS_ENV],
        provider=eff.provider,
        model=eff.model,
        reasoning=eff.reasoning,
        max_tokens=eff.max_tokens,
        zdr=eff.zdr,
        task_registered=_TASK_REGISTERED,
        legacy_env=eff.legacy,
        gate=_gate_status(gate_setting, eff.provider),
        gate_setting=gate_setting,
        clairic=_clairic_status(settings.get("clairic", "off")),
        warnings=tuple(warnings),
    )


def unreadable_v1_warnings() -> list[str]:
    """One warning line per v1 file the migration could not read, for ``status``.

    It parses every backup, so load_config() never calls it.
    """
    root = dossier_root()
    if not root.is_dir():
        return []
    try:
        found = dossier_migrate.unreadable_backups(root)
    except OSError:
        return []
    return [f"warning: 1 old dossier file could not be read; it is kept as {_doc.V1_BACKUP} in {rel}." for rel in found]


def skill_path() -> Path:
    return Path(__file__).resolve().parent / "skills" / "dossiers" / "SKILL.md"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """Second-precision UTC ISO form used in payloads and stored dates."""
    return None if value is None else _history._isoformat_utc(value.replace(microsecond=0))


def _parse(value: Any) -> datetime | None:
    return _history._parse_datetime(value)


def chat_key(business_connection_id: Any, chat_id: Any) -> str:
    return f"{_history._path_key(business_connection_id)}/{_history._safe_part(chat_id)}"


def fingerprint(label: str, text: str, message_at: datetime | None) -> str:
    # 'deleted' is not part of fp: a deletion is reported by status, not by a changed fingerprint.
    raw = json.dumps([label, text, _iso(message_at)], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def ws(text: Any) -> str:
    return " ".join(str(text).split())


def clip(text: Any, limit: int) -> str:
    cleaned = ws(_CONTROL_RE.sub(" ", str(text or "")))
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _id_key(message_id: Any) -> tuple[int, Any]:
    try:
        return (0, int(str(message_id)))
    except ValueError:
        return (1, str(message_id))


def _local_zone() -> tzinfo:
    try:
        from . import correspondence_stats  # lazy: it imports history_support at load

        return correspondence_stats.resolve_timezone()[0]
    except Exception:  # noqa: BLE001 - a broken zone setting falls back to UTC, never stops a cycle
        return UTC


def today_text(now: datetime) -> tuple[str, tzinfo]:
    """("2026-10-20, Tuesday", local zone): the writer's "today" in Hermes's time zone."""
    zone = _local_zone()
    local = now.astimezone(zone)
    return f"{local:%Y-%m-%d}, {WEEKDAYS[local.weekday()]}", zone


def _local_at(value: datetime, zone: tzinfo) -> str:
    return f"{value.astimezone(zone):%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# Storage layout and locks
# ---------------------------------------------------------------------------


def dossier_root() -> Path:
    # A sibling of history/, never nested in it, so history scans never see it.
    return _history.history_root().parent / "dossiers"


def contact_dir(business_connection_id: Any, chat_id: Any) -> Path:
    return dossier_root() / "chats" / _history._path_key(business_connection_id) / _history._safe_part(chat_id)


def persona_dir() -> Path:
    return dossier_root() / "persona"


persona_path = persona_dir  # the v1 name


def state_path() -> Path:
    return dossier_root() / "state.json"


def settings_path() -> Path:
    # The on/off switch: forget never deletes it, so forgetting dossiers never turns them on or off.
    return dossier_root() / SETTINGS_FILE


def generation() -> int:
    """Forget counter, kept outside state.json (forget --all deletes that); 0 when absent."""
    try:
        return int(json.loads((dossier_root() / "generation.json").read_text(encoding="utf-8"))["generation"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


@contextmanager
def _data_lock() -> Iterator[None]:
    root = dossier_root()
    _history._ensure_private_dir(root)
    _history._ensure_private_file(root / ".lock")
    with _history._locked_existing_path(root / ".lock", shared=False):
        yield


@contextmanager
def _cycle_lock() -> Iterator[bool]:
    """Non-blocking whole-cycle lock; yields False when another cycle holds it."""
    root = dossier_root()
    _history._ensure_private_dir(root)
    path = root / ".cycle.lock"
    _history._ensure_private_file(path)
    with path.open("r+b") as handle:
        if fcntl is None:
            yield True
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def cycle_running() -> bool:
    if not (dossier_root() / ".cycle.lock").exists():
        return False
    with _cycle_lock() as acquired:
        return not acquired


def load_contact(business_connection_id: Any, chat_id: Any) -> dict[str, Any] | None:
    """The contact's document, or None. Raises dossier_doc.DocError when it is corrupt."""
    return _doc.read_doc(contact_dir(business_connection_id, chat_id))


def load_persona() -> dict[str, Any] | None:
    """The persona document, or None. Raises dossier_doc.DocError when it is corrupt."""
    return _doc.read_doc(persona_dir())


def _target_dir(target: tuple[str, Any]) -> Path:
    kind, entry = target
    if kind == "persona":
        return persona_dir()
    return contact_dir(entry.get("business_connection_id"), entry.get("chat_id"))


def set_note(target: tuple[str, Any], text: str | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Store the owner note of a contact or the persona; None or "" clears it.

    The next cycle rewrites the document with it (the note's hash differs). Returns
    ``{"created": bool, "chars": int}``. Raises ValueError("too long") over NOTE_MAX_CHARS.
    """
    note = "" if text is None else _doc.clean_note(text)
    if len(note) > _doc.NOTE_MAX_CHARS:
        raise ValueError("too long")
    now = now or _history._utcnow()
    path = _target_dir(target)
    with _data_lock():
        doc = _doc.read_doc(path)
        created = doc is None
        if doc is None:
            if not note:
                return {"created": False, "chars": 0}
            entry = target[1]
            doc = (_doc.new_persona(now) if target[0] == "persona"
                   else _doc.new_contact(entry["business_connection_id"], entry["chat_id"], now))
        if doc["note"] != note or created:
            doc["note"] = note
            doc["note_updated_at"] = _iso(now)
            _doc.write_doc(path, doc)
    wake_worker()
    return {"created": created, "chars": len(note)}


def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "chats": {}, "failures": {}, "recent_calls": [], "last_cycle": None,
            "usage": {}}


def load_state() -> dict[str, Any]:
    """Worker cache; a bad key falls back to its default (costs at most one fold per chat)."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    state = _empty_state()
    for key, default in state.items():
        value = data.get(key, default)
        state[key] = value if isinstance(value, type(default)) or default is None else default
    for key in ("chats", "failures"):
        state[key] = {k: v for k, v in state[key].items() if isinstance(v, dict)}
    state["usage"] = {month: {part: dict(bucket) for part, bucket in parts.items() if isinstance(bucket, dict)}
                      for month, parts in state["usage"].items() if isinstance(parts, dict)}
    return state


def save_state(state: dict[str, Any]) -> None:
    with _data_lock():
        _history._write_json_atomic(state_path(), state)


# ---------------------------------------------------------------------------
# Stored switch and options (settings.json)
# ---------------------------------------------------------------------------


def _default_settings() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "enabled": False, "updated_at": None, "updated_via": None}


def _legacy_enable_source() -> str | None:
    """The pre-0.11.0 variable or Desktop setting when it turns dossiers on, else None."""
    if LEGACY_ENABLE_ENV in os.environ:
        return LEGACY_ENABLE_ENV if _history._truthy_env(LEGACY_ENABLE_ENV) else None
    value = _history.get_config(LEGACY_ENABLE_SETTING, None)
    on = value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"})
    return f"the {LEGACY_ENABLE_SETTING} setting" if on else None


def migrate_legacy_switch(now: datetime | None = None) -> bool:
    """Store "on" once when no switch is stored yet and the old variable or Desktop setting says on.

    Afterwards neither is read again. Returns True when it wrote the switch.
    """
    source = _legacy_enable_source()
    if source is None or settings_path().exists():
        return False
    with _data_lock():
        if settings_path().exists():
            return False
        _history._write_json_atomic(settings_path(), {
            "schema_version": SCHEMA_VERSION, "enabled": True,
            "updated_at": _iso(now or _history._utcnow()), "updated_via": "migration"})
    logger.warning("%s: dossiers stay on: %s was moved to the stored switch and is no longer read; use Settings "
                   "(/telesec) or '%s' to change it", _history.PLUGIN_NAME, source, DISABLE_COMMAND)
    return True


def load_settings() -> dict[str, Any]:
    """The stored switch plus "gate" and "clairic" when set. Never writes.

    Off until first set ("updated_via" None). An unreadable or corrupt file reads as disabled, with
    "error" set (surfaced as a load_config() warning); only an explicit set_enabled() replaces it.
    """
    path = settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_settings()
    except (OSError, ValueError):
        data = None
    if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
            or not isinstance(data.get("enabled"), bool)):
        return {**_default_settings(), "error": f"{path} is unreadable or corrupt, so dossiers stay disabled; "
                                                 f"'{ENABLE_COMMAND}' or 'disable' rewrites it"}
    at, via = data.get("updated_at"), data.get("updated_via")
    out = {"schema_version": SCHEMA_VERSION, "enabled": data["enabled"],
           "updated_at": at if isinstance(at, str) else None,
           "updated_via": via if via in (*SETTINGS_VIA, "migration") else None}
    if data.get("gate") in GATE_MODES:
        out["gate"] = data["gate"]
    if data.get("clairic") in CLAIRIC_MODES:
        out["clairic"] = data["clairic"]
    return out


def _options(current: dict[str, Any]) -> dict[str, Any]:
    return {key: current[key] for key in ("gate", "clairic") if key in current}


def set_enabled(enabled: bool, *, via: str, now: datetime | None = None) -> dict[str, Any]:
    """Turn dossiers on or off for all private chats (atomic, private, under the data lock).

    Idempotent: an unchanged switch keeps its stored time and source. "gate" and "clairic" are
    kept. Returns the settings plus "changed". Gateway callers call wake_worker() afterwards; the
    worker also polls the switch.
    """
    if via not in SETTINGS_VIA:
        raise ValueError(f"unknown switch source {via!r}; use one of: {', '.join(SETTINGS_VIA)}")
    with _data_lock():
        current = load_settings()
        if not current.get("error") and current["updated_via"] is not None and current["enabled"] is bool(enabled):
            return {**current, "changed": False}  # an explicit choice is always stored, even if it matches
        settings = {"schema_version": SCHEMA_VERSION, "enabled": bool(enabled),
                    "updated_at": _iso(now or _history._utcnow()), "updated_via": via, **_options(current)}
        _history._write_json_atomic(settings_path(), settings)
    logger.info("%s: dossiers switched enabled=%s via=%s", _history.PLUGIN_NAME, bool(enabled), via)
    return {**settings, "changed": True}


def _set_option(name: str, mode: str, modes: tuple[str, ...], via: str) -> dict[str, Any]:
    if mode not in modes:
        raise ValueError(f"unknown {name} mode {mode!r}; use one of: {', '.join(modes)}")
    if via not in SETTINGS_VIA:
        raise ValueError(f"unknown switch source {via!r}; use one of: {', '.join(SETTINGS_VIA)}")
    with _data_lock():
        current = load_settings()
        if current.get("error"):
            raise ValueError(current["error"])  # only an explicit enable or disable rewrites a corrupt file
        if current.get(name) == mode:
            return {**current, "changed": False}
        settings = {**current, name: mode}
        _history._write_json_atomic(settings_path(), settings)
    logger.info("%s: dossier option %s=%s via=%s", _history.PLUGIN_NAME, name, mode, via)
    return {**settings, "changed": True}


def set_gate(mode: str, *, via: str, now: datetime | None = None) -> dict[str, Any]:
    """Store the message check mode ("auto" or "off"); the switch and its time and source are kept."""
    return _set_option("gate", mode, GATE_MODES, via)


def set_clairic(mode: str, *, via: str, now: datetime | None = None) -> dict[str, Any]:
    """Store the fact hint mode ("on" or "off"); the switch and its time and source are kept."""
    return _set_option("clairic", mode, CLAIRIC_MODES, via)


# ---------------------------------------------------------------------------
# v1 migration
# ---------------------------------------------------------------------------

_MIGRATED_ROOTS: set[str] = set()  # roots migrated by this process: later calls only look for new v1 files


def _v1_pending(root: Path) -> bool:
    return (root / "persona" / _doc.V1_FILE).exists() or next(root.glob(f"chats/*/*/{_doc.V1_FILE}"), None) is not None


def v1_backups() -> int:
    """v1 files kept as ``v1-dossier.json`` until the first v2 pass (or a forget) removes them."""
    root = dossier_root()
    return len(list(root.glob(f"chats/*/*/{_doc.V1_BACKUP}"))) + int((root / "persona" / _doc.V1_BACKUP).exists())


def migrate_v1(now: datetime | None = None) -> dict[str, int]:
    """Move v1 ``dossier.json`` files to ``document.json`` (dossier_migrate); never calls a model.

    Runs at cycle start, when the worker thread starts and at CLI entry. Returns
    ``{"migrated", "unreadable", "backups"}``.
    """
    root = dossier_root()
    if not root.is_dir():
        return {"migrated": 0, "unreadable": 0, "backups": 0}
    if str(root) in _MIGRATED_ROOTS and not _v1_pending(root):
        return {"migrated": 0, "unreadable": 0, "backups": v1_backups()}
    with _data_lock():
        report = dossier_migrate.migrate_root(root, now or _history._utcnow())
    _MIGRATED_ROOTS.add(str(root))
    return report


# ---------------------------------------------------------------------------
# History view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Msg:
    key: str  # str(message_id)
    chat_key: str
    business_connection_id: str
    chat_id: Any
    message_id: Any
    direction: str  # raw: inbound | outbound | unknown
    label: str  # contact | owner | unknown | transcript
    text: str  # text of the latest edit, untruncated
    message_at: datetime | None
    order_at: datetime  # message_at, else first observation
    first_seen: datetime  # first observation inside this fold
    deleted: bool  # deleted, or replaced by a later message (likely_duplicate / likely_correction)
    fp: str
    reply_to: str | None = None  # str(reply_to_message_id)


@dataclass
class ChatView:
    entry: dict[str, Any]
    messages: dict[str, Msg]  # keyed by str(message_id)
    deleted_seen: set[str]  # ids with a message.deleted record inside the fold
    last_activity_at: datetime | None  # max observed_at of created/edited/deleted records
    signature: list[list[Any]]  # taken before the fold


@dataclass
class _Raw:
    mid: str
    message_id: Any
    direction: str
    label: str
    text: str
    message_at: datetime | None
    order_at: datetime
    first_seen: datetime
    deleted: bool
    reply_to: str | None


def _signature(chat_dir: Path) -> list[list[Any]]:
    return [list(f) for f in _history._chat_file_signature(chat_dir).files]


def sort_key(m: Msg) -> tuple[Any, ...]:
    return (m.order_at, m.chat_key, _id_key(m.message_id))


def _label_transcripts(rows: list[_Raw]) -> None:
    """Label plugin-posted voice transcripts, in first-seen order.

    Chunk 0 starts with the marker. A later chunk has no marker and no reply, and is first seen at
    most TRANSCRIPT_CHAIN_SECONDS after the previous chunk, so chains of any length work.
    """
    chain = timedelta(seconds=TRANSCRIPT_CHAIN_SECONDS)
    last: datetime | None = None
    for row in sorted(rows, key=lambda r: (r.first_seen, _id_key(r.mid))):
        if row.direction != "outbound":
            continue
        chunk0 = row.text.lstrip().startswith(TRANSCRIPT_MARKER)
        if chunk0 or (last is not None and row.reply_to is None and row.first_seen - last <= chain):
            row.label, last = "transcript", row.first_seen


def read_chat_view(entry: dict[str, Any], *, since: datetime) -> ChatView:
    """Fold one chat's history since ``since`` under its read lock; pure after release."""
    chat_dir = _history._entry_chat_dir(entry)
    sig = _signature(chat_dir)
    key = chat_key(entry["business_connection_id"], entry["chat_id"])
    if not chat_dir.exists():
        return ChatView(entry, {}, set(), None, sig)
    first_obs: dict[str, datetime] = {}
    known_dir: dict[str, str] = {}
    replaced: set[str] = set()
    deleted_seen: set[str] = set()
    last_activity: list[datetime] = []

    def tee(records: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for r in records:
            et, mid = r.get("event_type"), str(r.get("message_id"))
            obs = _parse(r.get("observed_at"))
            if et in ("message.created", "message.edited"):
                if obs is not None:
                    first_obs[mid] = min(first_obs.get(mid, obs), obs)
                if r.get("direction") in LABELS:
                    known_dir[mid] = r["direction"]  # prefer a known direction over a later "unknown"
            elif et == "message.deleted":
                deleted_seen.add(mid)
            elif et == "deletion.classified" and r.get("classification") in REPLACED:
                replaced.add(mid)
            if et in ("message.created", "message.edited", "message.deleted") and obs is not None:
                last_activity[:] = [max(last_activity[0], obs)] if last_activity else [obs]
            yield r

    try:
        with _history._streamed_records_locked(chat_dir, since=since, until=None) as records:
            state = _history._build_chat_state(tee(records))
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed history record") from exc
    # History lock released; pure from here.
    rows: list[_Raw] = []
    for mid, m in state.messages.items():
        text = m.text if isinstance(m.text, str) else ""
        if not text.strip():
            continue  # text messages only
        direction = m.direction if m.direction in LABELS else known_dir.get(mid, "unknown")
        message_at = _parse(m.message_at)
        first = first_obs.get(mid) or m.last_observed_at
        order_at = message_at or first
        if order_at is None:
            continue
        reply_to = None if m.reply_to_message_id is None else str(m.reply_to_message_id)
        rows.append(_Raw(mid, m.message_id, direction, LABELS.get(direction, "unknown"), text, message_at, order_at,
                         first or order_at, m.deleted or mid in replaced, reply_to))
    _label_transcripts(rows)
    conn = str(entry["business_connection_id"])
    msgs = {r.mid: Msg(r.mid, key, conn, entry["chat_id"], r.message_id, r.direction, r.label, r.text, r.message_at,
                       r.order_at, r.first_seen, r.deleted, fingerprint(r.label, r.text, r.message_at), r.reply_to)
            for r in rows}
    return ChatView(entry, msgs, deleted_seen, last_activity[0] if last_activity else None, sig)


def private_entries() -> list[dict[str, Any]]:
    """Private catalog entries, most recently active first (refreshed first under the rate limit)."""
    entries = [e for e in _history._catalog_entries_for_cli() if e.get("chat_type") == "private"]
    entries.sort(key=lambda e: (str(e.get("business_connection_id")), str(e.get("chat_id"))))
    floor = datetime.min.replace(tzinfo=UTC)
    entries.sort(key=lambda e: _parse(e.get("last_seen_at")) or floor, reverse=True)
    return entries


def as_wmsg(m: Msg, tz: tzinfo, *, edited: bool = False, deleted: bool = False) -> _writer.WMsg:
    """A message as the writer sees it: ref, local time, sender, text; a transcript is from "unknown"."""
    transcript = m.label == "transcript"
    return _writer.WMsg(ref=f"m{m.message_id}", at=_local_at(m.order_at, tz),
                        sender="unknown" if transcript else m.label, text=m.text, voice=transcript,
                        edited=edited, deleted=deleted)


# ---------------------------------------------------------------------------
# Planning (B.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One pending message: new, edited or deleted. ``msg`` is None for a deletion whose create was pruned."""

    key: str
    status: str
    order_at: datetime
    msg: Msg | None


@dataclass
class Plan:
    first_pass: bool
    qualified: bool
    new: list[Msg]
    edited: list[Msg]
    deleted: list[Msg]
    pruned: list[str]  # seen keys deleted after their create record was pruned: sent as a ref only
    gone: list[str]  # seen keys no longer in history (retention): dropped without a call
    window_start: datetime
    floor: datetime
    older_history_not_sent: bool
    note_changed: bool
    current: str | None  # the document sent as current_dossier (the migrated seed on a first pass)
    items: list[Item] = field(default_factory=list)  # new, edited and deleted, in time order


def _first_pass_candidates(msgs: list[Msg], *, now: datetime, cfg: DossierConfig,
                           entries: list[dict[str, Any]]) -> tuple[list[Msg], datetime, bool]:
    cutoff = now - timedelta(days=cfg.backfill_days)
    cand = [m for m in msgs if m.order_at >= cutoff]
    truncated = len(cand) > cfg.backfill_messages
    cand = cand[-cfg.backfill_messages:]  # exact cap, no tie overflow
    window_start = cand[0].order_at if truncated else cutoff
    older = truncated or any((_parse(e.get("first_message_at")) or now) < cutoff for e in entries)
    return cand, window_start, older


def _tracking_floor(doc: dict[str, Any], ws_at: datetime, now: datetime, cfg: DossierConfig) -> datetime:
    """Rolling floor that never moves back, so raising the backfill window cannot re-admit aged-out keys."""
    return max(now - timedelta(days=cfg.backfill_days), ws_at, _parse(doc.get("floor")) or ws_at)


def _age_seen(d: dict[str, Any], floor: datetime) -> None:
    for key, value in list(d["seen"].items()):
        at = _parse(value[1] if isinstance(value, list) and len(value) > 1 else None)
        if at is None or at < floor:
            del d["seen"][key]


def fold_since(doc: dict[str, Any] | None, now: datetime, cfg: DossierConfig) -> datetime:
    """Start of the history fold: the contact's floor and the persona window, minus the skew margin."""
    window = now - timedelta(days=cfg.backfill_days)
    ws_at = _parse(doc.get("window_start")) if doc else None
    floor = _tracking_floor(doc, ws_at, now, cfg) if doc and ws_at else window
    return min(floor, window) - FOLD_MARGIN


def _note_changed(doc: dict[str, Any] | None) -> bool:
    # A first pass sends the note anyway, so only a built document can be behind its note.
    return doc is not None and doc.get("window_start") is not None and _doc.note_sha(doc) != doc["note_sha"]


def plan_contact(doc: dict[str, Any] | None, view: ChatView, *, now: datetime, cfg: DossierConfig) -> Plan:
    """What changed since the document was written (B.2). Pure."""
    msgs = sorted(view.messages.values(), key=sort_key)
    if doc is None or doc.get("window_start") is None:
        live = [m for m in msgs if not m.deleted]
        cand, ws_at, older = _first_pass_candidates(live, now=now, cfg=cfg, entries=[view.entry])
        qualified = any(m.label == "contact" for m in cand) and any(m.label == "owner" for m in cand)
        plan = Plan(True, qualified, cand, [], [], [], [], ws_at, ws_at, older, False, (doc or {}).get("seed") or None)
        plan.items = [Item(m.key, "new", m.order_at, m) for m in cand] if qualified else []
        return plan
    ws_at = _parse(doc["window_start"]) or now
    floor = _tracking_floor(doc, ws_at, now, cfg)
    seen = doc["seen"]
    new: list[Msg] = []
    edited: list[Msg] = []
    deleted: list[Msg] = []
    for m in msgs:
        if m.key in seen:
            if m.deleted:
                deleted.append(m)
            elif seen[m.key][0] != m.fp:
                edited.append(m)
        elif not m.deleted and m.order_at >= floor and m.order_at > ws_at:
            # New messages must be strictly after window_start so boundary ties cut by the first-pass cap stay out.
            new.append(m)
    pruned = [k for k in seen if k not in view.messages and k in view.deleted_seen]
    gone = [k for k in seen if k not in view.messages and k not in view.deleted_seen]
    plan = Plan(False, True, new, edited, deleted, pruned, gone, ws_at, floor,
                bool(doc.get("older_history_not_sent")), _note_changed(doc), doc["document"])
    items = [Item(m.key, "new", m.order_at, m) for m in new]
    items += [Item(m.key, "edited", m.order_at, m) for m in edited]
    items += [Item(m.key, "deleted", m.order_at, m) for m in deleted]
    items += [Item(k, "deleted", _parse(seen[k][1]) or ws_at, None) for k in pruned]
    plan.items = sorted(items, key=lambda i: (i.order_at, _id_key(i.key)))
    return plan


def _item_size(item: Item) -> int:
    return len(item.msg.text if item.msg is not None else "") + 80


def batches(items: list[Item]) -> list[list[Item]]:
    out: list[list[Item]] = []
    cur: list[Item] = []
    chars = 0
    for item in items:  # sorted, so message order is kept across batches
        size = _item_size(item)
        if cur and (len(cur) >= BATCH_MAX_MESSAGES or chars + size > BATCH_MAX_CHARS):
            out.append(cur)
            cur, chars = [], 0
        cur.append(item)
        chars += size
    if cur:
        out.append(cur)
    return out


def _item_wmsg(item: Item, tz: tzinfo) -> _writer.WMsg:
    if item.msg is None:
        return _writer.WMsg(ref=f"m{item.key}", at=None, sender=None, text=None, deleted=True)
    return as_wmsg(item.msg, tz, edited=item.status == "edited", deleted=item.status == "deleted")


def _context(view: ChatView, batch: list[Item], seen: set[str], tz: tzinfo) -> list[_writer.WMsg]:
    """Live, already processed messages right before the batch's first one (the writer keeps the newest)."""
    keys = {item.key for item in batch}
    first = (batch[0].order_at, _id_key(batch[0].key)) if batch else None
    before = [m for m in sorted(view.messages.values(), key=sort_key)
              if not m.deleted and m.key in seen and m.key not in keys
              and (first is None or (m.order_at, _id_key(m.key)) < first)]
    return [as_wmsg(m, tz) for m in before[-_writer.CONTEXT_MAX:]]


def _batch_input(plan: Plan, view: ChatView, batch: list[Item], *, index: int, total: int, current: str | None,
                 note: str, seen: set[str], today: str, tz: tzinfo,
                 hints: list[dict[str, str]] | None = None) -> tuple[str, str]:
    """(mode, writer user message) for one batch."""
    mode = "first" if plan.first_pass and index == 1 else "update"
    return mode, _writer.contact_input(
        today=today, mode=mode, current=current, note=note,
        context=[] if mode == "first" else _context(view, batch, seen, tz),
        messages=[_item_wmsg(item, tz) for item in batch],
        part=(index, total) if plan.first_pass and total > 1 else None, hints=hints)


@dataclass(frozen=True)
class Pending:
    first: bool
    qualified: bool
    new: int
    edited: int
    deleted: int
    note_changed: bool

    def nothing(self) -> bool:
        return not self.first and self.new == self.edited == self.deleted == 0 and not self.note_changed


def _pending_of(plan: Plan) -> Pending:
    return Pending(plan.first_pass, plan.qualified, len(plan.new), len(plan.edited),
                   len(plan.deleted) + len(plan.pruned), plan.note_changed)


def _plan_for(entry: dict[str, Any], now: datetime, cfg: DossierConfig) -> tuple[dict[str, Any] | None, ChatView, Plan]:
    doc = load_contact(entry["business_connection_id"], entry["chat_id"])
    view = read_chat_view(entry, since=fold_since(doc, now, cfg))
    return doc, view, plan_contact(doc, view, now=now, cfg=cfg)


def pending(entry: dict[str, Any], *, now: datetime | None = None) -> Pending:
    """What the next refresh of this contact would send. Reads history; writes nothing."""
    now = now or _history._utcnow()
    return _pending_of(_plan_for(entry, now, load_config())[2])


def preview_contact(entry: dict[str, Any], *, now: datetime | None = None) -> tuple[str, str, Pending, bool]:
    """(system prompt, user message of the first batch, pending, gate would apply); no model call, no writes.

    The user message is "" when nothing is due.
    """
    now = now or _history._utcnow()
    cfg = load_config()
    doc, view, plan = _plan_for(entry, now, cfg)
    info = _pending_of(plan)
    if (plan.first_pass and not plan.qualified) or info.nothing():
        return _writer.WRITER_SYSTEM, "", info, False
    today, tz = today_text(now)
    parts = batches(plan.items) or [[]]
    seen = set() if plan.first_pass or doc is None else set(doc["seen"])
    _mode, user = _batch_input(plan, view, parts[0], index=1, total=len(parts), current=plan.current,
                               note=doc["note"] if doc else "", seen=seen, today=today, tz=tz)
    return _writer.WRITER_SYSTEM, user, info, _gate_applies(cfg, doc, plan, now)


def _gate_applies(cfg: DossierConfig, doc: dict[str, Any] | None, plan: Plan, now: datetime) -> bool:
    """D.1 for an unforced contact refresh: only small, recent bursts of new messages are checked."""
    if cfg.gate != "on" or doc is None or plan.first_pass or not doc["document"] or plan.note_changed:
        return False
    if plan.edited or plan.deleted or plan.pruned or not plan.new:
        return False
    if len(plan.new) > _writer.GATE_MAX_MESSAGES or sum(len(m.text) for m in plan.new) > _writer.GATE_MAX_CHARS:
        return False
    return now - min(m.order_at for m in plan.new) < _writer.GATE_MAX_AGE


# ---------------------------------------------------------------------------
# Rate limiter and cycle
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding 60 s window over writer calls, seeded from the persisted tail."""

    def __init__(self, max_per_minute: int, *, recent: Any = (), clock: Callable[[], float] = time.time,
                 wait: Callable[[float], bool] | None = None):
        self.max, self.clock = max_per_minute, clock
        self.wait = wait or _sleep_wait
        now = clock()
        self.calls: deque[float] = deque(t for t in sorted(recent) if 0 <= now - t < 60)

    def acquire(self) -> bool:
        while True:
            now = self.clock()
            while self.calls and now - self.calls[0] >= 60:
                self.calls.popleft()
            if len(self.calls) < self.max:
                self.calls.append(now)
                return True
            if self.wait(max(60 - (now - self.calls[0]), 0.05)):
                return False  # stopped while waiting

    def snapshot(self) -> list[float]:
        return list(self.calls)


def _sleep_wait(seconds: float) -> bool:
    time.sleep(seconds)
    return False


_COUNTED = ("refreshed", "checked", "deferred", "waiting", "backoff", "failed", "unchanged", "unqualified")


@dataclass
class CycleReport:
    outcome: str = "completed"
    trigger: str = "worker"
    calls: int = 0
    refreshed: int = 0
    checked: int = 0
    deferred: int = 0
    waiting: int = 0
    backoff: int = 0
    failed: int = 0
    unchanged: int = 0
    unqualified: int = 0
    orphans: int = 0  # always 0: a dossier whose chat left the catalog stays as it is until forgotten
    persona: str = "skipped"  # not_due | refreshed | empty | failed | paused | skipped
    failures: list[tuple[str, str]] = field(default_factory=list)  # (target label, error kind)
    error: str | None = None  # host trust message for the CLI only; never logged
    duration_ms: int = 0
    started_at: str | None = None
    cost_usd: float = 0.0

    def count(self, outcome: str) -> None:
        name = "waiting" if outcome == "discarded" else outcome
        if name in _COUNTED:
            setattr(self, name, getattr(self, name) + 1)

    def as_state(self) -> dict[str, Any]:
        return {"trigger": self.trigger, "started_at": self.started_at, "finished_at": _iso(_history._utcnow()),
                "outcome": self.outcome, "calls": self.calls,
                **{name: getattr(self, name) for name in _COUNTED},
                "persona": self.persona, "cost_usd": self.cost_usd, "duration_ms": self.duration_ms}


@dataclass
class _Cycle:
    """Per-cycle context shared by the process_* steps."""

    llm: Any
    cfg: DossierConfig
    now: datetime
    state: dict[str, Any]
    limiter: RateLimiter
    rep: CycleReport
    trigger: str
    stop: threading.Event | None
    route: _writer.Route
    today: str
    tz: tzinfo
    generation: int = 0  # forget counter at cycle start
    views: dict[str, ChatView] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    dirty: bool = False  # state changed since the last save
    target_gate: tuple[list[list[Any]], str | None] | None = None  # (signature, doc id) of the current contact

    def stopped(self) -> bool:
        return self.stop is not None and self.stop.is_set()


class _TrustGateDenied(Exception):
    """The host trust gate refused the call; aborts the whole cycle."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _is_trust_error(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) or any(c.__name__ == "PluginLlmTrustError" for c in type(exc).__mro__)


def _gate_ok(gate: Any, sig: list[list[Any]], ident: str | None) -> bool:
    return isinstance(gate, dict) and gate.get("signature") == sig and gate.get("dossier_id") == ident


def _recheck_due(gate: Any, now: datetime) -> bool:
    at = _parse(gate.get("recheck_at")) if isinstance(gate, dict) else None
    return at is not None and now >= at


def _set_gate(cx: _Cycle, key: str, sig: list[list[Any]], ident: str | None,
              recheck_at: datetime | None = None) -> None:
    cx.state["chats"][key] = {"signature": sig, "dossier_id": ident, "at": _iso(cx.now), "recheck_at": _iso(recheck_at)}
    cx.dirty = True


def _in_backoff(record: Any, now: datetime) -> bool:
    if not isinstance(record, dict):
        return False
    count, at = record.get("count"), _parse(record.get("at"))
    if not isinstance(count, int) or count < BACKOFF_AFTER or at is None:
        return False
    return now < at + timedelta(hours=min(2 ** (count - BACKOFF_AFTER), BACKOFF_MAX_HOURS))


def _failure_kind(exc: BaseException) -> str:
    if isinstance(exc, _doc.DocError):
        return "corrupt"
    if isinstance(exc, _writer.OutputError):
        return exc.code
    return type(exc).__name__


def _record_failure(state: dict[str, Any], key: str, kind: str, now: datetime) -> int:
    prev = state["failures"].get(key) or {}
    count = prev.get("count") if isinstance(prev.get("count"), int) else 0
    state["failures"][key] = {"at": _iso(now), "error": kind, "count": count + 1}
    return count + 1


def _clear_failure(cx: _Cycle, key: str) -> None:
    if cx.state["failures"].pop(key, None) is not None:
        cx.dirty = True


def _add_usage(cx: _Cycle, part: str, **amounts: float) -> None:
    """Add to this UTC month's usage bucket; only the newest USAGE_MONTHS months are kept."""
    usage = cx.state["usage"]
    month = usage.setdefault(f"{cx.now.astimezone(UTC):%Y-%m}", {})
    bucket = month.get(part) if isinstance(month.get(part), dict) else {}
    for name, amount in amounts.items():
        value = bucket.get(name)
        value = value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        # The full float is kept; status rounds it when it shows it, so small costs never drift.
        bucket[name] = value + amount if name == "cost_usd" else int(value) + int(amount)
    month[part] = bucket
    for old in sorted(usage)[:-USAGE_MONTHS]:
        del usage[old]
    if "cost_usd" in amounts:
        cx.rep.cost_usd += amounts["cost_usd"]
    cx.dirty = True


def _call_writer(cx: _Cycle, *, kind: str, payload: str, mode: str, current: str | None, ref: str,
                 paused: Callable[[], bool]) -> _writer.CallResult:
    """One writer run, counted even when its answer is refused.

    Raises OutputError, Stopped (something was switched off right before a call) or what the host raises.
    """
    try:
        # A timeout of None lets Hermes read auxiliary.telesec_dossier.timeout on the task route.
        result = _writer.write(cx.llm, kind=kind, payload=payload, mode=mode, current=current, route=cx.route,
                               timeout=None if cx.route.task else MODEL_TIMEOUT_SECONDS,
                               allowed=lambda: _refused(cx, paused) is None)
    except Exception as exc:
        spent = getattr(exc, "spent", None)
        if spent is not None:
            _count_writer(cx, kind, ref, spent)
        if _is_trust_error(exc):
            raise _TrustGateDenied(type(exc).__name__, str(exc)) from exc
        raise
    _count_writer(cx, kind, ref, result)
    return result


def _count_writer(cx: _Cycle, kind: str, ref: str, spent: _writer.Usage) -> None:
    cx.rep.calls += spent.calls - 1  # _call_allowed counted the first call before it was made
    cx.stats["calls"] = cx.stats.get("calls", 0) + spent.calls - 1
    if not spent.calls:
        return
    purpose = _writer.PURPOSE_CONTACT if kind == "contact" else _writer.PURPOSE_PERSONA
    logger.info("%s: dossier call purpose=%s chat_ref=%s ms=%d in=%d out=%d", _history.PLUGIN_NAME, purpose, ref,
                spent.ms, spent.input_tokens, spent.output_tokens)
    _add_usage(cx, "writer", calls=spent.calls, input_tokens=spent.input_tokens,
               output_tokens=spent.output_tokens, cost_usd=spent.cost_usd or 0.0,
               unpriced=spent.calls if spent.cost_usd is None else 0)


def _last_call(result: _writer.CallResult, now: datetime) -> dict[str, Any]:
    return {"provider": result.provider, "model": result.model, "at": _iso(now)}


def _set_text(d: dict[str, Any], text: str) -> bool:
    if text == d["document"]:
        return False
    if d["document"]:
        d["previous"] = d["document"]
    d["document"] = text
    d["revision"] += 1
    return True


def apply_result(doc: dict[str, Any], result: _writer.CallResult, batch: list[Item], plan: Plan, *,
                 note: str, now: datetime) -> tuple[dict[str, Any], bool]:
    """The contact document after one batch (B.10). Returns (document, content changed). Pure."""
    d = {**doc, "seen": dict(doc["seen"])}
    changed = False
    if result.output.kind in ("document", "empty"):
        changed = _set_text(d, result.output.text if result.output.kind == "document" else "")
        if changed:
            d["updated_at"] = _iso(now)
    d["checked_at"] = _iso(now)
    for item in batch:
        if item.status == "deleted":
            d["seen"].pop(item.key, None)  # a deletion is reported once
        elif item.msg is not None:
            d["seen"][item.key] = [item.msg.fp, _iso(item.msg.order_at)]
    for key in plan.gone:
        d["seen"].pop(key, None)
    if d["window_start"] is None:
        d["window_start"] = _iso(plan.window_start)
        d["older_history_not_sent"] = plan.older_history_not_sent
    d["floor"] = _iso(plan.floor)
    _age_seen(d, plan.floor)
    d["messages"] += sum(1 for item in batch if item.status == "new")
    d["note_sha"] = _doc.note_sha({"note": note})  # the note that was sent, not one saved during the call
    d["last_call"] = _last_call(result, now)
    d.pop("seed", None)
    return d, changed


def _commit_seen(cx: _Cycle, path: Path, ident: str, plan: Plan) -> None:
    """Drop history-pruned keys from ``seen`` without a model call (retention is never a deletion)."""
    with _data_lock():
        if generation() != cx.generation:
            return
        fresh = _doc.read_doc(path)
        if fresh is None or fresh["id"] != ident:
            return
        for key in plan.gone:
            fresh["seen"].pop(key, None)
        _doc.write_doc(path, fresh)


def _check_gate(cx: _Cycle, entry: dict[str, Any], key: str, doc: dict[str, Any], plan: Plan,
                view: ChatView) -> str | None:
    """Ask Jev about the new messages: "deferred", or "stopped" / "paused" when a switch went off.

    None lets the writer run: the check failed, or the owner turned it off during the cycle.
    """
    refused = _refused(cx, lambda: _entry_paused(entry), "gate")
    if refused:
        return None if refused == "off" else refused
    try:
        result = _writer.gate_check(doc["document"], [as_wmsg(m, cx.tz) for m in plan.new])
    except model_client.ModelCallError:
        _add_usage(cx, "check", errors=1)  # model_client already logged the kind
        return None
    _add_usage(cx, "check", calls=1, input_tokens=result.input_tokens, cost_usd=result.cost_usd or 0.0)
    if result.p >= _writer.GATE_THRESHOLD:
        return None
    oldest = min(m.order_at for m in plan.new)
    _set_gate(cx, key, view.signature, doc["id"], recheck_at=oldest + _writer.GATE_MAX_AGE)
    _add_usage(cx, "check", deferred=1)
    return "deferred"


def _hints(cx: _Cycle, plan: Plan, paused: Callable[[], bool]) -> list[dict[str, str]] | None:
    """Clairic fact hints for a small burst of new messages; None when off, not worth it, or failed."""
    if cx.cfg.clairic != "on" or not plan.new or len(plan.new) > _writer.GATE_MAX_MESSAGES:
        return None
    if sum(len(m.text) for m in plan.new) > _writer.GATE_MAX_CHARS:
        return None
    if _refused(cx, paused, "clairic"):
        return None  # turned off during the cycle, or the writer call is not going out either
    try:
        result = _writer.clairic_hints([as_wmsg(m, cx.tz) for m in plan.new],
                                       today_iso=f"{cx.now.astimezone(cx.tz):%Y-%m-%d}")
    except model_client.ModelCallError:
        _add_usage(cx, "clairic", errors=1)  # model_client already logged the kind
        return None
    _add_usage(cx, "clairic", calls=1, input_tokens=result.input_tokens,
               cost_usd=result.input_tokens * CLAIRIC_PRICE_PER_M / 1_000_000)
    return result.hints or None


def _quiet_met(view: ChatView, now: datetime, cfg: DossierConfig) -> bool:
    return view.last_activity_at is None or now - view.last_activity_at >= timedelta(minutes=cfg.quiet_minutes)


def chat_paused(business_connection_id: Any, chat_id: Any) -> bool:
    """The owner set "No dossier" or "Don't save" for this chat; its dossier stays frozen (chat_settings)."""
    from . import chat_settings  # lazy: chat_settings imports this module through global_settings

    return chat_settings.paused(business_connection_id, chat_id, "dossier")


def _entry_paused(entry: dict[str, Any]) -> bool:
    return chat_paused(entry.get("business_connection_id"), entry.get("chat_id"))


def _refused(cx: _Cycle, paused: Callable[[], bool], option: str | None = None) -> str | None:
    """The check right before chat text goes to any model: None when it may, else why not.

    "stopped" when the cycle is stopping or dossiers or history were switched off, "paused" when a chat in
    the input was, and "off" when ``option`` ("gate" or "clairic") was turned off during the cycle.
    """
    if cx.stopped():
        return "stopped"
    settings = load_settings()
    if not settings["enabled"] or not _history.capture_enabled():
        return "stopped"
    if paused():
        return "paused"
    if option is not None and settings.get(option, GATE_DEFAULT if option == "gate" else "off") == "off":
        return "off"  # the same defaults as load_config()
    return None


def _call_allowed(cx: _Cycle, paused: Callable[[], bool]) -> str | None:
    """None when the next writer call may go out, else the outcome that stops this target."""
    if not cx.limiter.acquire():
        return "stopped"
    refused = _refused(cx, paused)  # anything may have been switched off while waiting for the rate limit
    if refused:
        return refused
    cx.rep.calls += 1
    cx.stats["calls"] = cx.stats.get("calls", 0) + 1
    return None


def process_contact(cx: _Cycle, entry: dict[str, Any], *, forced: bool = False) -> str:
    """Refresh one contact (B.3). Forced runs skip backoff, the stat gate, the quiet rule and the gate."""
    if _entry_paused(entry):
        return "paused"  # switched off since the cycle started: nothing read, nothing written
    conn, chat = entry["business_connection_id"], entry["chat_id"]
    key = chat_key(conn, chat)
    if not forced and _in_backoff(cx.state["failures"].get(key), cx.now):
        return "backoff"
    path = contact_dir(conn, chat)
    sig = _signature(_history._entry_chat_dir(entry))
    cx.target_gate = (sig, None)
    doc = _doc.read_doc(path)
    ident = doc["id"] if doc else None
    cx.target_gate = (sig, ident)
    gate = cx.state["chats"].get(key)
    if not forced and not _note_changed(doc) and _gate_ok(gate, sig, ident) and not _recheck_due(gate, cx.now):
        return "unchanged"  # stat gate: nothing read
    view = read_chat_view(entry, since=fold_since(doc, cx.now, cx.cfg))
    cx.views[key] = view
    plan = plan_contact(doc, view, now=cx.now, cfg=cx.cfg)
    cx.stats.update(new=len(plan.new), edited=len(plan.edited), deleted=len(plan.deleted) + len(plan.pruned))
    if plan.first_pass and not plan.qualified:
        _set_gate(cx, key, view.signature, ident)
        _clear_failure(cx, key)
        return "unqualified"
    if not plan.items and not plan.note_changed:
        if plan.gone and ident is not None:
            _commit_seen(cx, path, ident, plan)
        _set_gate(cx, key, view.signature, ident)
        _clear_failure(cx, key)
        return "unchanged"
    if not forced and not _quiet_met(view, cx.now, cx.cfg):
        return "waiting"
    if not forced and doc is not None and _gate_applies(cx.cfg, doc, plan, cx.now):
        outcome = _check_gate(cx, entry, key, doc, plan, view)
        if outcome:
            return outcome
    outcome, new_id = _run_contact(cx, entry, path, doc, plan, view)
    if outcome in ("refreshed", "checked"):
        _set_gate(cx, key, view.signature, new_id)
        _clear_failure(cx, key)
    return outcome


def _run_contact(cx: _Cycle, entry: dict[str, Any], path: Path, doc: dict[str, Any] | None, plan: Plan,
                 view: ChatView) -> tuple[str, str | None]:
    """One writer call and one commit per batch. Returns (outcome, document id)."""
    expected = doc["id"] if doc else None
    note = doc["note"] if doc else ""
    current = plan.current
    seen = set() if plan.first_pass or doc is None else set(doc["seen"])
    parts = batches(plan.items) or [[]]  # a note change alone is one call without messages
    changed = False
    cx.stats["batches"] = len(parts)

    def paused() -> bool:
        return _entry_paused(entry)

    for index, batch in enumerate(parts, 1):
        if cx.stopped():
            return "stopped", expected
        if generation() != cx.generation:
            return "discarded", None  # forgotten since the cycle started: send nothing more
        refused = _call_allowed(cx, paused)
        if refused:
            return refused, expected
        # Fact hints go out only once the writer call is certain, with the first batch.
        hints = _hints(cx, plan, paused) if index == 1 else None
        mode, payload = _batch_input(plan, view, batch, index=index, total=len(parts), current=current, note=note,
                                     seen=seen, today=cx.today, tz=cx.tz, hints=hints)
        try:
            result = _call_writer(cx, kind="contact", payload=payload, mode=mode, current=current,
                                  ref=_log_ref(entry), paused=paused)
        except _writer.Stopped:
            return _refused(cx, paused) or "stopped", expected
        if cx.stopped():
            return "stopped", expected  # shutting down: write nothing
        if _entry_paused(entry):
            return "paused", expected  # switched off for this chat during the call: write nothing
        with _data_lock():
            if generation() != cx.generation:
                return "discarded", None  # forget ran during the call
            fresh = _doc.read_doc(path)
            if expected is not None and (fresh is None or fresh["id"] != expected):
                return "discarded", None  # forgotten or recreated during the call
            if expected is None and fresh is not None and fresh["window_start"] is not None:
                return "discarded", None
            base = fresh or _doc.new_contact(entry["business_connection_id"], entry["chat_id"], cx.now)
            updated, batch_changed = apply_result(base, result, batch, plan, note=note, now=cx.now)
            # Only a backup whose owner text reached the note goes; an unreadable one stays until a forget.
            migrated = updated.pop("migrated_from", None) == "v1"
            _doc.write_doc(path, updated)
            if migrated:
                (path / _doc.V1_BACKUP).unlink(missing_ok=True)
        changed = changed or batch_changed
        expected, current = updated["id"], updated["document"]
        seen = set(updated["seen"])
    return ("refreshed" if changed else "checked"), expected


def _persona_clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= PERSONA_MSG_MAX_CHARS else text[: PERSONA_MSG_MAX_CHARS - 1].rstrip() + "…"


def _persona_pool(cx: _Cycle, entries: list[dict[str, Any]]) -> list[Msg]:
    """The owner's messages for the persona: newest first capped, then trimmed from the oldest by size."""
    cutoff = cx.now - timedelta(days=cx.cfg.backfill_days)
    since = fold_since(None, cx.now, cx.cfg)
    pool: list[Msg] = []
    for entry in entries:
        key = chat_key(entry["business_connection_id"], entry["chat_id"])
        view = cx.views.get(key)
        if view is None:
            view = cx.views[key] = read_chat_view(entry, since=since)
        pool.extend(m for m in view.messages.values()
                    if m.label == "owner" and not m.deleted and m.text.strip() and m.order_at >= cutoff)
    pool.sort(key=sort_key)
    pool = pool[-PERSONA_MAX_MESSAGES:]
    total = sum(len(_persona_clip(m.text)) for m in pool)
    start = 0
    while start < len(pool) and total > PERSONA_MAX_CHARS:
        total -= len(_persona_clip(pool[start].text))
        start += 1
    return pool[start:]


def persona_due(doc: dict[str, Any] | None, messages: list[Msg], now: datetime, *, forced: bool = False) -> bool:
    """I.3: whether the persona is rebuilt from ``messages`` (the eligible owner messages)."""
    count = len(messages)
    if forced or (doc is not None and _doc.note_sha(doc) != doc["note_sha"]):
        return count >= 1
    if doc is None or doc["updated_at"] is None or not doc["document"]:
        return count >= PERSONA_MIN_FIRST
    newest, updated = _parse(doc.get("newest_at")), _parse(doc["updated_at"])
    fresh = sum(1 for m in messages if newest is None or m.order_at > newest)
    return fresh >= PERSONA_MIN_NEW and updated is not None and now - updated >= PERSONA_MIN_AGE


def _persona_input(cx: _Cycle, pool: list[Msg], note: str) -> tuple[str, list[dict[str, Any]]]:
    aliases: dict[str, str] = {}
    chats: list[dict[str, Any]] = []
    rows: list[tuple[str, str, str]] = []
    for m in pool:
        if m.chat_key not in aliases:
            aliases[m.chat_key] = f"c{len(aliases) + 1}"
            chats.append({"business_connection_id": m.business_connection_id, "chat_id": m.chat_id})
        rows.append((aliases[m.chat_key], _local_at(m.order_at, cx.tz), _persona_clip(m.text)))
    return _writer.persona_input(today=cx.today, note=note, messages=rows), chats


def _touch_persona(cx: _Cycle, path: Path, ident: str) -> None:
    with _data_lock():
        if generation() != cx.generation:
            return
        fresh = _doc.read_doc(path)
        if fresh is not None and fresh["id"] == ident:
            fresh["checked_at"] = _iso(cx.now)
            _doc.write_doc(path, fresh)


def process_persona(cx: _Cycle, entries: list[dict[str, Any]], *, forced: bool = False) -> str:
    """Rebuild the owner persona from scratch when it is due (I)."""
    if not forced and _in_backoff(cx.state["failures"].get("persona"), cx.now):
        return "not_due"
    path = persona_dir()
    doc = _doc.read_doc(path)
    checked = _parse(doc.get("checked_at")) if doc else None
    note_changed = doc is not None and _doc.note_sha(doc) != doc["note_sha"]
    if not forced and not note_changed and checked is not None and cx.now - checked < PERSONA_CHECK_EVERY:
        return "not_due"
    eligible = [e for e in entries if not _entry_paused(e)]
    pool = _persona_pool(cx, eligible)
    cx.stats.update(new=len(pool))
    if not persona_due(doc, pool, cx.now, forced=forced):
        if doc is not None:
            _touch_persona(cx, path, doc["id"])
        return "not_due"
    note = doc["note"] if doc else ""
    expected = doc["id"] if doc else None
    payload, chats = _persona_input(cx, pool, note)
    used = [e for e in eligible if {"business_connection_id": str(e["business_connection_id"]),
                                    "chat_id": e["chat_id"]} in chats]

    def paused() -> bool:
        return any(_entry_paused(e) for e in used)

    if cx.stopped():
        return "stopped"
    if generation() != cx.generation:
        return "discarded"
    refused = _call_allowed(cx, paused)
    if refused:
        return refused
    try:
        result = _call_writer(cx, kind="persona", payload=payload, mode="first", current=None, ref="persona",
                              paused=paused)
    except _writer.Stopped:
        return _refused(cx, paused) or "stopped"
    if cx.stopped():
        return "stopped"
    if paused():
        return "paused"  # a chat in the input was switched off during the call: write nothing
    with _data_lock():
        if generation() != cx.generation:
            return "discarded"
        fresh = _doc.read_doc(path)
        if expected is not None and (fresh is None or fresh["id"] != expected):
            return "discarded"
        d = dict(fresh or _doc.new_persona(cx.now))
        empty = result.output.kind == "empty"
        had_text = bool(d["document"])
        if _set_text(d, "" if empty else result.output.text) and had_text:
            d["previous_chats"] = d["chats"]  # so erasing one of them also erases the previous version
        d["updated_at"] = d["checked_at"] = _iso(cx.now)
        d["chats"] = chats
        d["newest_at"] = _iso(max(m.order_at for m in pool))
        d["messages"] = len(pool)
        d["note_sha"] = _doc.note_sha({"note": note})
        d["last_call"] = _last_call(result, cx.now)
        d.pop("seed", None)
        # Only a backup whose owner text reached the note goes; an unreadable one stays until a forget.
        migrated = d.pop("migrated_from", None) == "v1"
        _doc.write_doc(path, d)
        if migrated:
            (path / _doc.V1_BACKUP).unlink(missing_ok=True)
    _clear_failure(cx, "persona")
    return "empty" if empty else "refreshed"


def preview_persona(*, now: datetime | None = None) -> tuple[str, str, bool]:
    """(system prompt, user message of a persona rebuild, due without --persona); no model call, no writes.

    The user message is the one ``run --persona`` sends, or "" when there is no owner message to read.
    """
    now = now or _history._utcnow()
    today, tz = today_text(now)
    cx = _Cycle(llm=None, cfg=load_config(), now=now, state=_empty_state(), limiter=RateLimiter(1),
                rep=CycleReport(), trigger="preview", stop=None, route=route(), today=today, tz=tz)
    pool = _persona_pool(cx, [e for e in private_entries() if not _entry_paused(e)])
    if not pool:
        return _writer.PERSONA_SYSTEM, "", False
    doc = load_persona()
    payload, _chats = _persona_input(cx, pool, doc["note"] if doc else "")
    return _writer.PERSONA_SYSTEM, payload, persona_due(doc, pool, now)


# ---------------------------------------------------------------------------
# Forget
# ---------------------------------------------------------------------------


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(p.is_file() for p in path.iterdir())


def _forget_summary(path: Path) -> dict[str, Any]:
    try:
        doc = _doc.read_doc(path)
    except (OSError, ValueError):
        doc = None
    return {"existed": _has_files(path), "revision": doc["revision"] if doc else None,
            "note": bool(doc and doc["note"])}


def _reset_persona(entry: dict[str, Any]) -> bool:
    """Erase the persona text when it or its previous version read this chat (or may have: a v1 backup).

    The caller holds the data lock.
    """
    path = persona_dir()
    backup = path / _doc.V1_BACKUP
    ids = (str(entry.get("business_connection_id")), str(entry.get("chat_id")))
    try:
        doc = _doc.read_doc(path)
    except (OSError, ValueError):
        (path / _doc.DOC_FILE).unlink(missing_ok=True)  # unreadable, so it may hold this chat's text
        backup.unlink(missing_ok=True)
        return True
    cited = doc is not None and any(
        (str(c.get("business_connection_id")), str(c.get("chat_id"))) == ids
        for c in doc["chats"] + doc.get("previous_chats", []))
    if not cited and not backup.exists():
        return False
    if doc is not None:
        doc.update(id=secrets.token_hex(6), document="", previous=None, chats=[], newest_at=None, messages=0,
                   updated_at=None, checked_at=None, note_sha=None)
        for name in ("seed", "migrated_from", "previous_chats"):
            doc.pop(name, None)
        _doc.write_doc(path, doc)
    backup.unlink(missing_ok=True)
    return True


def forget(target: tuple[str, Any]) -> dict[str, Any]:
    """Delete a dossier (or all of them) with its note. Never touches history or the switch.

    ``target`` is ("contact", entry_or_stub) | ("persona", None) | ("all", None). A contact forget
    also erases the persona text when the persona or its previous version read that chat.
    """
    root = dossier_root()
    with _data_lock():
        # Bumped first: a cycle that started before this forget discards its in-flight results.
        _history._write_json_atomic(root / "generation.json", {"generation": generation() + 1})
        if (persona_dir() / _doc.V1_FILE).exists():
            dossier_migrate.migrate_root(root, _history._utcnow())  # owner text reaches the note before a reset
        if target[0] == "all":
            chats = root / "chats"
            contacts = sum(1 for d in chats.glob("*/*") if _has_files(d)) if chats.is_dir() else 0
            persona = _has_files(persona_dir())
            for path in (chats, persona_dir()):
                if path.exists():
                    shutil.rmtree(path)
            state_path().unlink(missing_ok=True)
            return {"existed": bool(contacts or persona), "contacts": contacts, "persona": persona}
        path = _target_dir(target)
        summary = _forget_summary(path)
        if path.exists():
            shutil.rmtree(path)
            if target[0] == "contact":
                try:
                    path.parent.rmdir()  # the connection dir, only when now empty
                except OSError:
                    pass
        if target[0] == "contact":
            summary["persona_reset"] = _reset_persona(target[1])
        return summary


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


def _private_entries_with_retry(stop: threading.Event | None) -> tuple[list[dict[str, Any]], str | None]:
    for attempt in (0, 1):
        try:
            return private_entries(), None
        except _history.HistoryLockUninitializedError:
            return [], "no_history"
        except ValueError:
            if attempt:
                return [], "catalog_unavailable"  # never rebuild: that would write into history
            if stop is not None:
                if stop.wait(CATALOG_RETRY_SECONDS):
                    return [], "stopped"
            else:
                time.sleep(CATALOG_RETRY_SECONDS)
    return [], "catalog_unavailable"


def _entry_label(entry: dict[str, Any]) -> str:
    return f"{entry.get('business_connection_id')}:{entry.get('chat_id')}"


def _log_ref(entry: dict[str, Any]) -> str:
    """Short stable hash of the chat for logs: never the raw connection or chat id."""
    key = chat_key(entry.get("business_connection_id"), entry.get("chat_id"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _guarded(cx: _Cycle, key: str, entry: dict[str, Any] | None, label: str, step: Callable[[], str]) -> str:
    """Run one contact or persona step; any failure except a trust denial stays local and writes nothing."""
    started = time.monotonic()
    cx.stats, cx.target_gate = {}, None
    try:
        outcome = step()
    except _TrustGateDenied:
        raise
    except Exception as exc:  # noqa: BLE001 - one target must not stop the cycle
        outcome, kind = "failed", _failure_kind(exc)
        count = _record_failure(cx.state, key, kind, cx.now)
        if count >= BACKOFF_AFTER and entry is not None and cx.target_gate is not None:
            # After the backoff a retry needs a chat change, a note change or a forced run.
            _set_gate(cx, key, *cx.target_gate)
        cx.dirty = True
        cx.rep.failures.append((label, kind))
        if entry is None:
            logger.warning("%s: dossier persona refresh failed error=%s", _history.PLUGIN_NAME, kind)
        else:
            logger.warning("%s: dossier refresh failed chat_ref=%s error=%s", _history.PLUGIN_NAME, _log_ref(entry),
                           kind)
    s = cx.stats
    logger.debug("%s: dossier target=%s outcome=%s calls=%d batches=%d new=%d edited=%d deleted=%d duration_ms=%d",
                 _history.PLUGIN_NAME, "persona" if entry is None else _log_ref(entry), outcome, s.get("calls", 0),
                 s.get("batches", 0), s.get("new", 0), s.get("edited", 0), s.get("deleted", 0),
                 int((time.monotonic() - started) * 1000))
    if cx.dirty or s.get("calls"):  # an idle target (gate hit) writes nothing; the cycle end saves anyway
        cx.state["recent_calls"] = cx.limiter.snapshot()
        save_state(cx.state)
        cx.dirty = False
    return outcome


def run_cycle(*, llm: Any, now: datetime | None = None, trigger: str = "worker",
              target: tuple[str, Any] | None = None, stop: threading.Event | None = None,
              limiter: RateLimiter | None = None) -> CycleReport:
    """One dossier cycle. ``target`` is None | ("contact", entry) | ("persona", None) and forces that item."""
    try:
        migrate_v1(now)  # before the switch check: v1 owner text moves even while dossiers are off
    except Exception as exc:  # noqa: BLE001 - a failed migration is retried at the next cycle
        logger.warning("%s: dossier migration failed error=%s", _history.PLUGIN_NAME, type(exc).__name__)
    cfg = load_config()
    if not cfg.enabled:
        return CycleReport(outcome="disabled", trigger=trigger)
    if not _history.capture_enabled():
        return CycleReport(outcome="history_off", trigger=trigger)  # dossiers need history
    now = now or _history._utcnow()
    started = time.monotonic()
    with _cycle_lock() as acquired:
        if not acquired:
            return CycleReport(outcome="busy", trigger=trigger)
        state = load_state()
        rep = CycleReport(trigger=trigger, started_at=_iso(now))
        limiter = limiter or RateLimiter(cfg.max_calls_per_minute, recent=state.get("recent_calls") or (),
                                         wait=stop.wait if stop is not None else None)
        today, tz = today_text(now)
        cx = _Cycle(llm, cfg, now, state, limiter, rep, trigger, stop, route(), today, tz, generation())
        try:
            _run_cycle_steps(cx, target)
        except _TrustGateDenied as exc:
            rep.outcome, rep.error = "aborted_trust", str(exc)
            logger.warning("%s: dossier cycle aborted: Hermes LLM trust gate denied the call error=%s",
                           _history.PLUGIN_NAME, exc.error_type)
        rep.duration_ms = int((time.monotonic() - started) * 1000)
        state["recent_calls"] = limiter.snapshot()
        state["last_cycle"] = rep.as_state()
        save_state(state)
        return rep


def _drop_departed(state: dict[str, Any], known: set[str]) -> None:
    """Forget cache and failure keys of chats that left the catalog (the persona key stays)."""
    for section in ("chats", "failures"):
        for key in [k for k in state[section] if k != "persona" and k not in known]:
            del state[section][key]


def _run_cycle_steps(cx: _Cycle, target: tuple[str, Any] | None) -> None:
    rep = cx.rep
    known: set[str] | None = None
    if target is not None and target[0] == "contact":
        all_entries = [target[1]]
    else:
        all_entries, problem = _private_entries_with_retry(cx.stop)
        if problem:
            rep.outcome = problem
            return
        known = {chat_key(e["business_connection_id"], e["chat_id"]) for e in all_entries}
    # A chat set to "No dossier" or "Don't save" is skipped; its dossier stays as it is.
    entries = [e for e in all_entries if not _entry_paused(e)]
    if target is not None and target[0] == "contact" and not entries:
        rep.outcome = "chat_off"
        return
    if target is None or target[0] == "contact":
        for entry in entries:
            if cx.stopped():
                rep.outcome = "stopped"
                break
            key = chat_key(entry["business_connection_id"], entry["chat_id"])
            out = _guarded(cx, key, entry, _entry_label(entry),
                           lambda e=entry: process_contact(cx, e, forced=target is not None))
            if out == "stopped":
                rep.outcome = "stopped"
                break
            rep.count(out)
    if (target is None or target[0] == "persona") and rep.outcome == "completed":
        rep.persona = _guarded(cx, "persona", None, "persona",
                               lambda: process_persona(cx, entries, forced=target is not None))
    if known is not None:
        _drop_departed(cx.state, known)


# ---------------------------------------------------------------------------
# Gateway worker
# ---------------------------------------------------------------------------

_LLM: Any = None
_WORKERS_LOCK = threading.Lock()


@dataclass
class _Worker:
    key: str
    stop: threading.Event
    thread: threading.Thread | None = None
    cycle_done: threading.Event = field(default_factory=threading.Event)  # test hook
    wake: threading.Event = field(default_factory=threading.Event)  # re-check the switch now (also set on stop)


_WORKERS: dict[str, _Worker] = {}  # key = str(dossier_root()); module-level, so fresh per plugin load


def bind_llm(llm: Any) -> None:
    global _LLM
    _LLM = llm


def _llm_ready(llm: Any) -> bool:
    return llm is not None and callable(getattr(llm, "complete", None))


def ensure_worker_started() -> bool:
    """Start the gateway worker once per dossier root whenever the host LLM facade is bound.

    It runs while dossiers are disabled too and checks the stored switch on every tick, so turning
    dossiers on needs no restart. Never raises.
    """
    try:
        cfg = load_config()
        llm = _LLM
        if not _llm_ready(llm):
            if cfg.enabled:
                logger.warning("%s: dossier worker not started: host LLM facade unavailable", _history.PLUGIN_NAME)
            return False
        key = str(dossier_root())
        with _WORKERS_LOCK:
            worker = _WORKERS.get(key)
            if worker is not None and worker.thread is not None and worker.thread.is_alive() \
                    and not worker.stop.is_set():
                return True  # reconnects are no-ops
            worker = _Worker(key, threading.Event())
            snapshot = contextvars.copy_context()  # keeps the profile scope (HERMES_HOME ContextVar)
            worker.thread = threading.Thread(target=snapshot.run, args=(_worker_loop, llm, worker),
                                             name="telesec-dossiers", daemon=True)
            _WORKERS[key] = worker
            worker.thread.start()
        logger.info("%s: dossier worker started enabled=%s initial_delay_s=%d interval_m=%d", _history.PLUGIN_NAME,
                    cfg.enabled, INITIAL_DELAY_SECONDS, cfg.interval_minutes)
        settings = load_settings()
        if settings.get("error"):
            logger.warning("%s: dossier switch %s is unreadable or corrupt, so dossiers stay off until '%s' or "
                           "Settings (/telesec) rewrites it", _history.PLUGIN_NAME, SETTINGS_FILE, ENABLE_COMMAND)
        return True
    except Exception as exc:  # noqa: BLE001 - dossier start must never break adapter connect
        logger.warning("%s: dossier worker start failed error=%s", _history.PLUGIN_NAME, type(exc).__name__)
        return False


def wake_worker() -> bool:
    """Make the live worker of this dossier root re-check the switch now (after set_enabled in the gateway).

    A worker idling while disabled starts a cycle at once if dossiers are on, but never before its
    post-connect delay; a worker waiting out its interval while enabled keeps waiting. Never raises.
    """
    try:
        with _WORKERS_LOCK:
            worker = _WORKERS.get(str(dossier_root()))
    except Exception:  # noqa: BLE001 - a wake is best effort
        return False
    if worker is None or worker.stop.is_set() or worker.thread is None or not worker.thread.is_alive():
        return False
    worker.wake.set()
    return True


def stop_workers(timeout: float = 0.0) -> None:
    """on_unload callback; does not block the loop unless a timeout is given."""
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.stop.set()
        worker.wake.set()  # ends an idle wait at once
    if timeout:
        for worker in workers:
            if worker.thread is not None:
                worker.thread.join(timeout)


def _config_if_on() -> DossierConfig | None:
    """The config while dossiers and history are on, else None; a failing read counts as off."""
    try:
        cfg = load_config()
        history = cfg.enabled and _history.capture_enabled()
    except Exception as exc:  # noqa: BLE001 - keep the worker alive
        logger.warning("%s: dossier switch check failed error=%s", _history.PLUGIN_NAME, type(exc).__name__)
        return None
    return cfg if history else None


def _worker_idle(worker: _Worker, due: float | None) -> bool:
    """Wait for the next cycle: until ``due`` (monotonic) while on, until switched on while off.

    The switch is re-read every DISABLED_POLL_SECONDS and on each wake. Switching off ends an
    interval wait, so a later switch-on starts a cycle at once; a wake while on changes nothing,
    so it never causes back-to-back cycles. Returns True when stopped.
    """
    while True:
        timeout = DISABLED_POLL_SECONDS if due is None else min(DISABLED_POLL_SECONDS, due - time.monotonic())
        worker.wake.wait(max(timeout, 0))
        worker.wake.clear()
        if worker.stop.is_set():
            return True
        if _config_if_on() is None:
            due = None
        elif due is None or time.monotonic() >= due:
            return False


def _worker_loop(llm: Any, worker: _Worker) -> None:
    try:
        migrate_v1()
    except Exception as exc:  # noqa: BLE001 - the next cycle retries the migration
        logger.warning("%s: dossier migration failed error=%s", _history.PLUGIN_NAME, type(exc).__name__)
    if worker.stop.wait(INITIAL_DELAY_SECONDS):  # a wake never shortens the post-connect delay
        return
    worker.wake.clear()
    while not worker.stop.is_set():
        cfg, due = _config_if_on(), None
        if cfg is not None:
            wait = cfg.interval_minutes * 60
            try:
                r = run_cycle(llm=llm, trigger="worker", stop=worker.stop)
                if r.outcome == "busy":  # a panel Refresh or CLI run holds the lock: retry soon, not in an interval
                    wait = DISABLED_POLL_SECONDS
                logger.info("%s: dossier cycle outcome=%s calls=%d refreshed=%d checked=%d deferred=%d waiting=%d "
                            "backoff=%d failed=%d unchanged=%d unqualified=%d persona=%s duration_ms=%d",
                            _history.PLUGIN_NAME, r.outcome, r.calls, r.refreshed, r.checked, r.deferred, r.waiting,
                            r.backoff, r.failed, r.unchanged, r.unqualified, r.persona, r.duration_ms)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                logger.warning("%s: dossier cycle failed error=%s", _history.PLUGIN_NAME, type(exc).__name__)
            worker.cycle_done.set()
            due = time.monotonic() + wait
        if _worker_idle(worker, due):
            return
