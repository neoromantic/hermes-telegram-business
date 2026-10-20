"""Run the live dossier eval: made-up chats replayed through the real plugin code.

Opt-in and live: it calls Vercel AI Gateway (and Cloudflare for ``v2-clairic``) with the keys in the
process environment and stops before any call that could take the spend over ``--budget-usd``.
``--dry-run`` answers every request offline instead, to check the plumbing for free.

    uv run python tests/dossier_eval/run.py --out ../telesec-eval \\
        --arms v1,v2,v2-gate,v2-think,v2-clairic --v1-worktree ../telesec-v1

Each arm, scenario and run gets a fresh temp HERMES_HOME. History is written step by step as canonical
records (the test helper ``raw``); each checkpoint runs ``run_cycle(llm=LiveLlm, trigger="cli", ...)``
and post steps call ``dossier_post.build_post``. Results go only to ``--out/<UTC timestamp>/``: one
JSON per arm with every document per step, and ``summary.md`` with numbers and pass/fail only.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import importlib.util
import itertools
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
import types
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "tests",):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import dossier_helpers as dh
import pytest

from dossier_eval import judge as jd
from dossier_eval import live_llm as ll
from dossier_eval import scenarios as sc

V1_TAG = "v0.13.0"
V2_API = ("set_task_registered", "set_gate", "set_note", "load_contact", "load_persona")
GATE_THRESHOLD = 0.35  # dossier_writer.GATE_THRESHOLD
THRESHOLDS = (0.25, 0.3, 0.35, 0.4, 0.45, 0.5)
V1_SECTION = {"contact": "relationship", "persona": "tone"}
PERSONA_ONLY = frozenset({"S12"})
GATE_SCENARIOS = ("S1", "S2", "S3")  # O.6 rule 2 also names S10 and S11, which this eval does not have
THINK_SCENARIOS = ("S1", "S2", "S3", "S4")
CLAIRIC_SCENARIOS = ("S3", "S4")  # O.6 rule 5 also names S10 and S11
FACT_KINDS = ("date", "amount", "contact")
LEGACY_ENV = ("TELESEC_DOSSIER_PROVIDER", "TELESEC_DOSSIER_MODEL")
REPLY_DIRECTIONS = {"in": ("inbound", dh.CONTACT_ID), "out": ("outbound", dh.OWNER_ID)}  # as dossier_helpers.raw
_REPLY_UPDATE_IDS = itertools.count(5_000_000)  # apart from the helper's own update ids


@dataclasses.dataclass(frozen=True)
class Arm:
    name: str
    version: str  # "v1" | "v2"
    reasoning: str = "low"
    gate: str = "off"
    clairic: bool = False
    scenarios: tuple[str, ...] | None = None  # None: every scenario


ARMS = {
    "v1": Arm("v1", "v1"),
    "v2": Arm("v2", "v2"),
    "v2-gate": Arm("v2-gate", "v2", gate="auto"),
    "v2-think": Arm("v2-think", "v2", reasoning="high", scenarios=THINK_SCENARIOS),
    "v2-clairic": Arm("v2-clairic", "v2", clairic=True),
}


class HarnessError(RuntimeError):
    """The arm cannot run with this code (for example dossier_support is not v2 yet)."""


# ---------------------------------------------------------------------------
# One plugin load in a fresh HERMES_HOME
# ---------------------------------------------------------------------------


class Clock:
    """The plugin's ``_utcnow``: the time of the current step, so every run is the same."""

    def __init__(self) -> None:
        self.at = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.at


def load_plugin(mp: pytest.MonkeyPatch, home: Path, root: Path, prefix: str) -> types.ModuleType:
    """Import the plugin at ``root`` under a unique package name with HERMES_HOME = ``home``."""
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: home
    mp.setitem(sys.modules, "hermes_constants", hermes_constants)
    name = f"{prefix}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, root / "__init__.py")
    if spec is None or spec.loader is None:
        raise HarnessError(f"cannot load the plugin from {root}")
    module = importlib.util.module_from_spec(spec)
    mp.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def unload(name: str) -> None:
    for module_name in [m for m in sys.modules if m.startswith(f"{name}.")]:
        sys.modules.pop(module_name, None)


def _submodule(plugin: types.ModuleType, name: str) -> types.ModuleType | None:
    try:
        return importlib.import_module(f"{plugin.__name__}.{name}")
    except ImportError:
        return None


def _stub(chat: int) -> dict[str, Any]:
    return {"business_connection_id": dh.BUSINESS_ID, "chat_id": chat}


def v1_markdown(dossier: dict[str, Any] | None, sections: Iterable[str]) -> str:
    """A v1 dossier as one Markdown document: active model facts by section (owner notes left out)."""
    if not dossier:
        return ""
    parts: list[str] = []
    for section in sections:
        bullets = [f"- {f['text']}" for f in dossier.get("facts") or []
                   if f.get("section") == section and f.get("status") == "active" and f.get("kind") != "owner"]
        if bullets:
            parts.append(f"## {section.replace('_', ' ').capitalize()}\n" + "\n".join(bullets))
    return "\n\n".join(parts)


class Harness:
    """The plugin calls one scenario run needs, for v1 (0.13.0) and v2 code alike."""

    def __init__(self, plugin: types.ModuleType, arm: Arm) -> None:
        self.plugin = plugin
        self.arm = arm
        self.v2 = arm.version == "v2"
        self.ds = plugin._dossier_support
        self.history = plugin._history_support

    def setup(self) -> dict[str, Any]:
        if self.v2:
            needed = V2_API + (("set_clairic",) if self.arm.clairic else ())
            missing = [name for name in needed if not hasattr(self.ds, name)]
            if missing:
                raise HarnessError(f"dossier_support is not v2 yet (missing {', '.join(missing)})")
        self.history.reset_in_memory_caches()
        self.plugin._global_settings.set_enabled("history", True, via="cli")
        if self.v2:
            self.ds.set_task_registered(True)
        self.ds.set_enabled(True, via="cli")
        if self.v2:
            self.ds.set_gate(self.arm.gate, via="cli")
            if self.arm.clairic:
                self.ds.set_clairic("on", via="cli")
        cfg = self.ds.load_config()
        fields = ("provider", "model", "reasoning", "max_tokens", "zdr", "gate", "clairic", "blocked")
        return {name: getattr(cfg, name) for name in fields if hasattr(cfg, name)}

    def write(self, scenario: sc.Scenario, messages: Iterable[sc.Message]) -> None:
        for m in messages:
            lag = m.at - (m.sent_at or m.at)
            if m.reply_to is None:
                dh.raw(self.plugin, m.chat, [(m.id, m.who, m.text, m.at, m.event)], base=m.at,
                       first_name=scenario.chats[m.chat], message_lag=lag, rebuild=False)
            else:
                self._write_reply(scenario, m, lag)
        self.history.rebuild_contact_catalog()

    def _write_reply(self, scenario: sc.Scenario, m: sc.Message, lag: Any) -> None:
        # ``raw`` has no reply_to, so a reply (a transcript chunk 0) is built from the same helpers.
        if m.event != "created" or m.who not in REPLY_DIRECTIONS:
            raise HarnessError("only created in/out messages can carry reply_to")
        direction, sender_id = REPLY_DIRECTIONS[m.who]
        record = dh.make_history_record(
            self.plugin, event_type="message.created", source="business_message", observed_at=m.at,
            telegram_update_id=next(_REPLY_UPDATE_IDS), business_connection_id=dh.BUSINESS_ID, chat_id=m.chat,
            message_id=m.id, message_at=m.at - lag, sender_id=sender_id, direction=direction,
            reply_to_message_id=m.reply_to, text=m.text,
        )
        profile = self.history._normalize_profile_snapshot(
            {"id": m.chat, "type": "private", "username": None, "first_name": scenario.chats[m.chat],
             "last_name": None, "title": None})
        if profile is not None:
            record["chat_profile"] = profile
        dh.append_raw_history_records(self.plugin, record)

    def entry(self, chat: int) -> dict[str, Any] | None:
        try:
            return dh.catalog_entry(self.plugin, chat)
        except (LookupError, ValueError):
            return None

    def set_note(self, note: sc.SetNote) -> None:
        if note.chat is None:
            target: tuple[str, Any] = ("persona", None)
        else:
            entry = self.entry(note.chat)
            if entry is None:
                raise HarnessError("the note's chat is not in the catalog")
            target = ("contact", entry)
        if self.v2:
            self.ds.set_note(target, note.text, now=note.at)
        else:
            self.ds.apply_owner_edit(target, "note", text=note.text, section=V1_SECTION[target[0]], now=note.at)

    def prune(self, prune: sc.Prune) -> None:
        self.plugin._global_settings.set_retention(prune.retention_days, via="cli", now=prune.at)
        self.history.maintain_history(now=prune.at)

    def erase(self, chat: int) -> None:
        """Erase the chat's saved history and forget its dossier, as Erase saved history does."""
        forget_chat = getattr(self.history, "forget_chat", None)
        if forget_chat is not None:
            forget_chat(dh.BUSINESS_ID, chat)
            return
        # 0.13.0 has no erase: remove the chat's history directory, then forget the dossier.
        shutil.rmtree(self.history._history_chat_dir_path(dh.BUSINESS_ID, chat), ignore_errors=True)
        self.history.reset_in_memory_caches()
        self.history.rebuild_contact_catalog()
        self.ds.forget(("contact", _stub(chat)))

    def cycle(self, llm: ll.LiveLlm, target: str, now: datetime) -> dict[str, Any]:
        report = self.ds.run_cycle(llm=llm, trigger="cli", target=("persona", None) if target == "persona" else None,
                                   now=now)
        return dataclasses.asdict(report) if dataclasses.is_dataclass(report) else dict(vars(report))

    def contact_doc(self, chat: int) -> dict[str, Any] | None:
        try:
            if self.v2:
                return self.ds.load_contact(dh.BUSINESS_ID, chat)
            return self.ds.load_for_target(("contact", _stub(chat)))
        except (OSError, ValueError):
            return None

    def persona_doc(self) -> dict[str, Any] | None:
        try:
            return self.ds.load_persona() if self.v2 else self.ds.load_dossier(self.ds.persona_path())
        except (OSError, ValueError):
            return None

    def text(self, doc: dict[str, Any] | None, kind: str) -> str:
        if doc is None:
            return ""
        if self.v2:
            return str(doc.get("document") or "")
        return v1_markdown(doc, self.ds.SECTIONS[kind])

    @staticmethod
    def marker(doc: dict[str, Any] | None) -> tuple[Any, ...]:
        """Changes whenever the writer processed messages (v2 ``checked_at``, v1 ``refreshed_at``)."""
        if doc is None:
            return (None,)
        return doc.get("checked_at"), doc.get("refreshed_at"), doc.get("revision")

    def post(self, chat: int, llm: ll.LiveLlm, now: datetime, lang: str) -> dict[str, Any]:
        entry = self.entry(chat)
        if entry is None:
            return {"status": "skipped", "error": "chat not in the catalog", "text": ""}
        if not self.v2:
            panel, view = _submodule(self.plugin, "dossier_panel"), _submodule(self.plugin, "dossier_view")
            snap = panel.load_contact_snapshot(dh.BUSINESS_ID, chat, now=now)
            reason = view.post_blocker(snap)
            if reason is not None:
                return {"status": "refused", "error": reason, "text": ""}
            return {"status": "ok", "error": None, "text": view.render_overview(snap, lang, audience="contact").text}
        post = _submodule(self.plugin, "dossier_post")
        if post is None or not hasattr(post, "build_post"):
            return {"status": "skipped", "error": "dossier_post not available", "text": ""}
        try:
            result = post.build_post(entry, llm=llm, now=now)
        except ll.BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - the report keeps the type name only
            return {"status": "error", "error": type(exc).__name__, "text": ""}
        return {"status": result.status, "error": result.error, "text": result.text or ""}


# ---------------------------------------------------------------------------
# One scenario run
# ---------------------------------------------------------------------------


def _clean_env(mp: pytest.MonkeyPatch, arm: Arm) -> None:
    for name in [n for n in os.environ if n.startswith("TELESEC_")]:
        mp.delenv(name)
    if arm.version == "v1":
        # 0.13.0 reads its route from these; the v2 arms leave them unset (v2 blocks non-gateway values).
        mp.setenv(LEGACY_ENV[0], ll.PROVIDER)
        mp.setenv(LEGACY_ENV[1], ll.WRITER_MODEL)


def _canaries(scenario: sc.Scenario, cp: sc.Checkpoint, target: str) -> list[sc.Canary]:
    wanted = cp.canaries
    return [c for c in scenario.canaries if (wanted is None or c.id in wanted) and target in (*c.hard, *c.reported)]


def _canary_result(text: str, canaries: list[sc.Canary], target: str) -> dict[str, list[str]]:
    hits = jd.canary_hits(text, canaries)
    by_id = {c.id: c for c in canaries}
    return {"hard": [h for h in hits if target in by_id[h].hard], "reported": [h for h in hits if target not in by_id[h].hard]}


def grouped_steps(steps: Iterable[sc.Step]) -> list[sc.Step | list[sc.Burst]]:
    """The steps with consecutive bursts merged into one list: their messages are written in time order across
    chats, as they would arrive."""
    out: list[sc.Step | list[sc.Burst]] = []
    for step in steps:
        last = out[-1] if out else None
        if isinstance(step, sc.Burst) and isinstance(last, list):
            last.append(step)
        else:
            out.append([step] if isinstance(step, sc.Burst) else step)
    return out


def run_scenario(arm: Arm, scenario: sc.Scenario, run_index: int, *, root: Path, meter: ll.Meter,
                 judge_model: str | None) -> dict[str, Any]:
    """Replay one scenario for one arm. Raises BudgetExceeded and HarnessError; the caller records them."""
    result: dict[str, Any] = {"scenario": scenario.id, "run": run_index, "steps": [], "gate": [], "status": "ok"}
    ids = {m.id: m.chat for m in scenario.messages()}
    pending: dict[int, list[sc.Burst]] = defaultdict(list)
    clock = Clock()
    with tempfile.TemporaryDirectory(prefix="telesec-eval-") as home, pytest.MonkeyPatch.context() as mp:
        _clean_env(mp, arm)
        plugin = load_plugin(mp, Path(home), root, f"telesec_eval_{arm.version}")
        harness = Harness(plugin, arm)
        try:
            mp.setattr(harness.history, "_utcnow", clock.now)
            client = _submodule(plugin, "model_client")
            if client is not None:
                for name, wrapper in meter.wrap_model_client(client).items():
                    mp.setattr(client, name, wrapper)
            result["config"] = harness.setup()
            llm = ll.LiveLlm(meter, reasoning=arm.reasoning)
            previous: dict[int, str] = {}
            try:
                for step in grouped_steps(scenario.steps):
                    if isinstance(step, list):
                        messages = sorted((m for burst in step for m in burst.messages), key=lambda m: m.at)
                        clock.at = messages[-1].at
                        harness.write(scenario, messages)
                        for burst in step:
                            pending[burst.chat].append(burst)
                    elif isinstance(step, sc.SetNote):
                        clock.at = step.at
                        harness.set_note(step)
                    elif isinstance(step, sc.Prune):
                        clock.at = step.at
                        harness.prune(step)
                    elif isinstance(step, sc.Erase):
                        clock.at = step.at
                        harness.erase(step.chat)
                    else:
                        clock.at = step.at
                        result["steps"].append(_checkpoint(harness, scenario, step, run_index, meter, llm, judge_model,
                                                           pending, ids, previous, result["gate"]))
            except ll.BudgetExceeded:
                result["status"] = "budget"
            except HarnessError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the other runs; the report keeps the type name only
                result["status"] = f"error {type(exc).__name__}"
        finally:
            harness.history.reset_in_memory_caches()
            unload(plugin.__name__)
    return result


def _checkpoint(harness: Harness, scenario: sc.Scenario, cp: sc.Checkpoint, run_index: int, meter: ll.Meter,
                llm: ll.LiveLlm, judge_model: str | None, pending: dict[int, list[sc.Burst]], ids: dict[int, int],
                previous: dict[int, str], gate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    meter.step = f"{scenario.id}#{run_index}/{cp.id}"
    start = len(meter.records)
    before = {chat: harness.marker(harness.contact_doc(chat)) for chat in scenario.chats}
    out: dict[str, Any] = {"id": cp.id, "at": cp.at.isoformat(), "target": cp.target, "chats": {}}
    out["report"] = harness.cycle(llm, cp.target, cp.at)
    calls = [r for r in meter.records[start:] if r.kind != "judge"]
    out["calls"] = len(calls)
    out["calls_by_kind"] = dict(Counter(r.kind for r in calls))
    for record in calls:
        if record.kind == "gate" and record.p is not None:
            chats = {ids.get(int(ref[1:])) for ref in record.refs or () if ref[1:].isdigit()}
            chat = next(iter(chats)) if len(chats) == 1 else None
            truth = any(b.informative for b in pending.get(chat, ())) if chat is not None else None
            gate_rows.append({"step": cp.id, "chat": chat, "p": record.p, "informative": truth})
    for chat in scenario.chats if cp.target == "auto" else ():
        doc = harness.contact_doc(chat)
        if harness.marker(doc) != before[chat]:
            pending[chat].clear()
        text = harness.text(doc, "contact")
        facts = tuple(f for f in cp.facts if f.chat == chat)
        row: dict[str, Any] = {
            "document": text,
            "units": jd.units(text),
            "language_ok": jd.language_ok(text, scenario.chat_lang(chat)),
            "canaries": _canary_result(text, _canaries(scenario, cp, "document"), "document"),
            "presence": {f.id: jd.fact_presence(text, f) for f in facts},
            "unchanged": previous.get(chat) == text if chat in previous else None,
        }
        previous[chat] = text
        if cp.judge and judge_model and facts:
            try:
                row["judge"] = jd.judge_document(meter, judge_model, text, facts, scenario.reference.get(chat, ()),
                                                 _canaries(scenario, cp, "document"))
            except ll.LlmCallError as exc:
                row["judge"] = {"error": str(exc)}
        if cp.post:
            post = harness.post(chat, llm, cp.at, scenario.chat_lang(chat))
            post["canaries"] = _canary_result(post["text"], _canaries(scenario, cp, "post"), "post")
            post["units"] = jd.units(post["text"])
            row["post"] = post
        out["chats"][str(chat)] = row
    persona = harness.text(harness.persona_doc(), "persona")
    out["persona"] = {
        "document": persona,
        "units": jd.units(persona),
        "canaries": _canary_result(persona, _canaries(scenario, cp, "persona"), "persona"),
    }
    if cp.expect_no_calls:
        unchanged = [row["unchanged"] for row in out["chats"].values()]
        out["no_calls_ok"] = out["calls"] == 0 and all(value is not False for value in unchanged)
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(q * len(ordered)) - 1)
    return ordered[index]


def _scenario_of(step: str) -> str:
    return step.split("#", 1)[0]


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def gate_table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The confusion matrix at GATE_THRESHOLD, the threshold table and the suggested threshold.

    The suggestion is the highest threshold in THRESHOLDS that still lets every informative check through:
    the lowest one would always be 0.25. Without informative checks there is no suggestion.
    """
    labelled = [r for r in rows if r["informative"] is not None]
    table = []
    for threshold in THRESHOLDS:
        tp = sum(1 for r in labelled if r["informative"] and r["p"] >= threshold)
        fn = sum(1 for r in labelled if r["informative"] and r["p"] < threshold)
        tn = sum(1 for r in labelled if not r["informative"] and r["p"] < threshold)
        fp = sum(1 for r in labelled if not r["informative"] and r["p"] >= threshold)
        table.append({"threshold": threshold, "tp": tp, "fn": fn, "tn": tn, "fp": fp,
                      "recall": _rate(tp, tp + fn), "deferral": _rate(tn, tn + fp)})
    full = [row["threshold"] for row in table if row["recall"] == 1.0]
    current = next(row for row in table if row["threshold"] == GATE_THRESHOLD)
    return {"checks": len(rows), "unlabelled": len(rows) - len(labelled), "at_default": current, "table": table,
            "suggested": max(full) if full else None}


def _judged(results: list[dict[str, Any]], final_only: bool = False) -> list[tuple[str, dict, dict]]:
    """(scenario, fact metadata by id, judge result) for every judged document."""
    out = []
    for run in results:
        scenario = sc.BY_ID[run["scenario"]]
        steps = run["steps"]
        if final_only and run["status"] != "ok":
            continue
        for index, step in enumerate(steps):
            if final_only and index != len(scenario.checkpoints()) - 1:
                continue
            cp = scenario.checkpoints()[index]
            for chat, row in step["chats"].items():
                verdict = row.get("judge")
                if verdict and "error" not in verdict:
                    facts = {f.id: f for f in cp.facts if str(f.chat) == chat}
                    out.append((run["scenario"], facts, verdict))
    return out


def fact_scores(results: list[dict[str, Any]], final_only: bool = False) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    p1: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    unsupported, noise, docs, errors = 0, 0, 0, 0
    rubric: dict[str, list[int]] = defaultdict(list)
    for _scenario, facts, verdict in _judged(results, final_only):
        docs += 1
        unsupported += verdict["unsupported_count"]
        noise += verdict["noise"]
        for name, value in verdict["rubric"].items():
            if value is not None:
                rubric[name].append(value)
        for fid, item in verdict["facts"].items():
            counts[item["verdict"]] += 1
            fact = facts.get(fid)
            if fact is not None and fact.priority == 1:
                p1[item["verdict"]] += 1
            if fact is not None and fact.kind in FACT_KINDS and item["verdict"] != "current":
                kinds["errors"] += 1
            if fact is not None and fact.kind in FACT_KINDS:
                kinds["total"] += 1
    for run in results:
        for step in run["steps"]:
            errors += sum(1 for row in step["chats"].values() if (row.get("judge") or {}).get("error"))
    total = sum(counts.values())
    return {
        "documents": docs,
        "facts": total,
        "recall": _rate(counts["current"], total),
        "stale": _rate(counts["stale"], total),
        "missing": _rate(counts["missing"], total),
        "p1_recall": _rate(p1["current"], sum(p1.values())),
        "p1_missing": p1["missing"],
        "unsupported_per_doc": _rate(unsupported, docs),
        "noise_per_doc": _rate(noise, docs),
        "rubric": {name: statistics.mean(values) for name, values in rubric.items() if values},
        "kind_errors": kinds["errors"],
        "kind_facts": kinds["total"],
        "judge_errors": errors,
    }


def presence_scores(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic token checks: final-checkpoint presence and the fact delay in bursts."""
    final: Counter[str] = Counter()
    p1_missing = 0
    delays: list[int] = []
    never = 0
    for run in results:
        scenario = sc.BY_ID[run["scenario"]]
        cps = scenario.checkpoints()
        steps = run["steps"]
        if not steps or cps[0].target != "auto":
            continue
        if run["status"] == "ok" and len(steps) == len(cps):
            last, cp = steps[-1], cps[-1]
            for fact in cp.facts:
                verdict = last["chats"].get(str(fact.chat), {}).get("presence", {}).get(fact.id, "missing")
                final[verdict] += 1
                if fact.priority == 1 and verdict == "missing":
                    p1_missing += 1
        first_seen: dict[tuple[int, str, str], int] = {}
        for index, cp in enumerate(cps[:len(steps)]):
            for fact in cp.facts:
                first_seen.setdefault((fact.chat, fact.id, fact.current), index)
        for (chat, fid, current), start in first_seen.items():
            delay = None
            for index in range(start, len(steps)):
                cp = cps[index]
                version = next((f for f in cp.facts if f.chat == chat and f.id == fid), None)
                if version is None or version.current != current:
                    break
                if steps[index]["chats"].get(str(chat), {}).get("presence", {}).get(fid) == "current":
                    delay = index - start
                    break
            if delay is None:
                never += 1
            else:
                delays.append(delay)
    total = sum(final.values())
    return {
        "final_facts": total,
        "final_recall": _rate(final["current"], total),
        "final_stale": _rate(final["stale"], total),
        "final_p1_missing": p1_missing,
        "delay_median": statistics.median(delays) if delays else None,
        "delay_max": max(delays) if delays else None,
        "never_seen": never,
    }


def call_scores(records: list[ll.CallRecord], results: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in ("contact", "persona", "post", "shorten", "gate", "clairic", "judge"):
        rows = [r for r in records if r.kind == kind]
        if not rows:
            continue
        ms = [float(r.ms) for r in rows if r.ok]
        by_kind[kind] = {
            "calls": len(rows),
            "failed": sum(1 for r in rows if not r.ok),
            "input_tokens": sum(r.input_tokens for r in rows),
            "output_tokens": sum(r.output_tokens for r in rows),
            "reasoning_tokens": sum(r.reasoning_tokens for r in rows),
            "usd": sum(r.cost_usd for r in rows),
            "p50_ms": percentile(ms, 0.5),
            "p95_ms": percentile(ms, 0.95),
            "priced_by": dict(Counter(r.priced_by for r in rows)),
        }
    contact_scenarios = {r["scenario"] for r in results if r["scenario"] not in PERSONA_ONLY}
    contact_usd = sum(r.cost_usd for r in records
                      if r.of in ("contact", "gate", "clairic") and _scenario_of(r.step) in contact_scenarios)
    contact_days = sum(sc.BY_ID[r["scenario"]].contact_days() for r in results
                       if r["scenario"] not in PERSONA_ONLY and r["status"] == "ok")
    writer_calls = sum(by_kind.get(kind, {}).get("calls", 0) for kind in ("contact", "persona", "post"))
    return {
        "by_kind": by_kind,
        "contact_usd": contact_usd,
        "contact_days": contact_days,
        "usd_per_contact_day": _rate(contact_usd, contact_days) if contact_days else None,
        "arm_usd": sum(r.cost_usd for r in records if r.kind != "judge"),
        "judge_usd": sum(r.cost_usd for r in records if r.kind == "judge"),
        "shorten_rate": _rate(by_kind.get("shorten", {}).get("calls", 0), writer_calls),
        "refreshes": by_kind.get("contact", {}).get("calls", 0),
    }


def check_scores(results: list[dict[str, Any]], arm: Arm) -> dict[str, Any]:
    """Deterministic checks. ``blockers`` lists the hard failures (O.6 rule 6) by id, never by text."""
    canaries: dict[str, Counter[str]] = {"hard": Counter(), "reported": Counter()}
    failures: Counter[str] = Counter()
    sizes: dict[str, int] = defaultdict(int)
    over: Counter[str] = Counter()
    language = Counter()
    blockers: list[str] = []
    post_status: Counter[str] = Counter()
    deferred = 0
    s9 = []
    for run in results:
        scenario = sc.BY_ID[run["scenario"]]
        for step in run["steps"]:
            label = f"{run['scenario']}/{step['id']}"
            report = step["report"]
            deferred += int(report.get("deferred") or 0)
            for failure in report.get("failures") or []:
                failures[str(failure[-1]) if isinstance(failure, list | tuple) else str(failure)] += 1
            if "no_calls_ok" in step:
                s9.append(step["no_calls_ok"])
                if not step["no_calls_ok"]:
                    blockers.append(f"{label}: model calls or a changed document after the prune")
            documents = [("document", row["document"], row["canaries"]) for row in step["chats"].values()]
            documents.append(("persona", step["persona"]["document"], step["persona"]["canaries"]))
            for row in step["chats"].values():
                if row.get("language_ok") is not None:
                    language["ok" if row["language_ok"] else "off"] += 1
                if "post" in row:
                    post_status[row["post"]["status"]] += 1
                    if row["post"]["status"] == "ok":
                        documents.append(("post", row["post"]["text"], row["post"]["canaries"]))
            for kind, text, hits in documents:
                size = jd.units(text)
                sizes[kind] = max(sizes[kind], size)
                if size > jd.DOC_MAX_UNITS:
                    over[kind] += 1
                    if arm.version == "v2":
                        blockers.append(f"{label}: {kind} over {jd.DOC_MAX_UNITS} units")
                for canary_id in hits["hard"]:
                    canaries["hard"][f"{scenario.canary(canary_id).kind}:{kind}"] += 1
                    blockers.append(f"{label}: {scenario.canary(canary_id).kind} canary {canary_id} in the {kind}")
                for canary_id in hits["reported"]:
                    canaries["reported"][f"{scenario.canary(canary_id).kind}:{kind}"] += 1
    return {
        "canaries_hard": dict(canaries["hard"]),
        "canaries_reported": dict(canaries["reported"]),
        "validation_failures": dict(failures),
        "max_units": dict(sizes),
        "over_limit": dict(over),
        "language": dict(language),
        "post_status": dict(post_status),
        "deferred": deferred,
        "s9_no_calls": all(s9) if s9 else None,
        "blockers": blockers,
    }


def arm_metrics(arm: Arm, results: list[dict[str, Any]], records: list[ll.CallRecord],
                scenarios: Iterable[str] | None = None) -> dict[str, Any]:
    wanted = set(scenarios) if scenarios is not None else None
    runs = [r for r in results if wanted is None or r["scenario"] in wanted]
    calls = [r for r in records if wanted is None or _scenario_of(r.step) in wanted]
    gate_rows = [row for run in runs for row in run["gate"]]
    return {
        "calls": call_scores(calls, runs),
        "facts": fact_scores(runs),
        "final_facts": fact_scores(runs, final_only=True),
        "presence": presence_scores(runs),
        "checks": check_scores(runs, arm),
        "gate": gate_table(gate_rows),
        "runs": {"ok": sum(1 for r in runs if r["status"] == "ok"), "total": len(runs)},
    }


# ---------------------------------------------------------------------------
# O.6 decision table
# ---------------------------------------------------------------------------


def _cmp(ok: bool | None) -> str:
    return "n/a" if ok is None else ("pass" if ok else "fail")


def _le(a: float | None, b: float | None, factor: float = 1.0) -> bool | None:
    return None if a is None or b is None else a <= b * factor + 1e-12


def _ge(a: float | None, b: float | None, delta: float = 0.0) -> bool | None:
    return None if a is None or b is None else a >= b + delta - 1e-12


def decisions(metrics: dict[str, dict[str, Any]], subsets: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """O.6 rules 1-6 as rows: rule, check, values (numbers only) and pass/fail."""
    rows: list[dict[str, Any]] = []

    def row(rule: str, check: str, values: str, ok: bool | None) -> None:
        rows.append({"rule": rule, "check": check, "values": values, "result": _cmp(ok)})

    def num(value: float | None, pct: bool = False) -> str:
        if value is None:
            return "n/a"
        return f"{value * 100:.1f}%" if pct else f"{value:.4g}"

    v1, v2 = metrics.get("v1"), metrics.get("v2")
    if v1 and v2:
        a, b = v1["facts"], v2["facts"]
        row("1", "recall >= v1 - 2 points", f"v1 {num(a['recall'], True)}, v2 {num(b['recall'], True)}",
            _ge(b["recall"], a["recall"], -0.02))
        row("1", "stale <= v1", f"v1 {num(a['stale'], True)}, v2 {num(b['stale'], True)}", _le(b["stale"], a["stale"]))
        row("1", "unsupported <= v1", f"v1 {num(a['unsupported_per_doc'])}, v2 {num(b['unsupported_per_doc'])}",
            _le(b["unsupported_per_doc"], a["unsupported_per_doc"]))
        post_hits = sum(n for k, n in v2["checks"]["canaries_hard"].items() if k.endswith(":post"))
        row("1", "post canaries = 0", f"v2 {post_hits}", post_hits == 0)
        ca, cb = v1["calls"]["usd_per_contact_day"], v2["calls"]["usd_per_contact_day"]
        row("1", "cost per contact-day <= v1", f"v1 {num(ca)}, v2 {num(cb)}", _le(cb, ca))
        ua, ub = a["rubric"].get("usefulness"), b["rubric"].get("usefulness")
        row("1", "usefulness >= v1 + 0.5", f"v1 {num(ua)}, v2 {num(ub)}", _ge(ub, ua, 0.5))
    else:
        row("1", "v2 replaces v1", "needs the v1 and v2 arms", None)

    gate, base = subsets["gate"].get("v2-gate"), subsets["gate"].get("v2")
    if gate and base:
        cg, cb = gate["calls"]["contact_usd"], base["calls"]["contact_usd"]
        row("2", "cost(v2-gate) <= 0.75 x cost(v2), S1-S3", f"v2 {num(cb)}, v2-gate {num(cg)}", _le(cg, cb, 0.75))
        fg, fb = gate["final_facts"]["recall"], base["final_facts"]["recall"]
        if fg is None or fb is None:
            fg, fb = gate["presence"]["final_recall"], base["presence"]["final_recall"]
        row("2", "final key-fact rate within 2 points", f"v2 {num(fb, True)}, v2-gate {num(fg, True)}",
            _ge(fg, fb, -0.02))
        delay = gate["presence"]["delay_median"]
        row("2", "median fact delay <= 1 burst", f"v2-gate {num(delay)}", None if delay is None else delay <= 1)
        missing = gate["final_facts"]["p1_missing"] if gate["final_facts"]["facts"] else gate["presence"][
            "final_p1_missing"]
        row("2", "no priority-1 fact missing at the end", f"v2-gate {missing}", missing == 0)
        verdict = all(r["result"] == "pass" for r in rows if r["rule"] == "2")
        row("2", "GATE_DEFAULT", "auto" if verdict else "off", verdict)
    else:
        row("2", "GATE_DEFAULT", "needs the v2 and v2-gate arms", None)

    table = metrics.get("v2-gate", {}).get("gate")
    if table and table["checks"]:
        suggested = table["suggested"]
        row("3", "GATE_THRESHOLD: highest value with 100% recall on informative checks",
            f"suggested {num(suggested)} (default {GATE_THRESHOLD})", suggested is not None)
    else:
        row("3", "GATE_THRESHOLD", f"no gate checks; stays {GATE_THRESHOLD}", None)

    think, low = subsets["think"].get("v2-think"), subsets["think"].get("v2")
    if think and low:
        a, b = low["facts"], think["facts"]
        gain = _ge(b["recall"], a["recall"], 0.05)
        halves = _le(b["stale"], a["stale"], 0.5) and _le(b["unsupported_per_doc"], a["unsupported_per_doc"], 0.5)
        cost_low, cost_high = low["calls"]["contact_usd"], think["calls"]["contact_usd"]
        row("4", "high: recall +5 points, or stale and unsupported halved",
            f"recall low {num(a['recall'], True)}, high {num(b['recall'], True)}",
            None if gain is None else bool(gain or halves))
        row("4", "high: cost < 2x low", f"low {num(cost_low)}, high {num(cost_high)}",
            cost_high < 2 * cost_low if cost_low else None)
        switch = all(r["result"] == "pass" for r in rows if r["rule"] == "4")
        row("4", "reasoning", "high" if switch else "low", None)
    else:
        row("4", "reasoning", "needs the v2 and v2-think arms; stays low", None)

    clairic, plain = subsets["clairic"].get("v2-clairic"), subsets["clairic"].get("v2")
    if clairic and plain:
        ec, ep = clairic["facts"]["kind_errors"], plain["facts"]["kind_errors"]
        row("5", "date, amount and contact errors drop >= 50%, S3-S4", f"v2 {ep}, v2-clairic {ec}",
            None if not plain["facts"]["kind_facts"] else ec <= ep * 0.5)
        extra = clairic["calls"]["by_kind"].get("clairic", {})
        refreshes = clairic["calls"]["refreshes"]
        per = _rate(extra.get("usd", 0.0), refreshes) if refreshes else None
        row("5", "added cost < 0.001 USD per refresh", f"{num(per)}", None if per is None else per < 0.001)
        p95 = extra.get("p95_ms")
        row("5", "added p95 latency < 1.5 s", f"{num(p95)} ms", None if p95 is None else p95 < 1500)
        row("5", "Russian field accuracy >= 90%", "not measured", None)
    else:
        row("5", "Clairic (H-C)", "needs the v2 and v2-clairic arms", None)

    for name, data in metrics.items():
        if ARMS[name].version != "v2":
            continue
        blockers = data["checks"]["blockers"]
        row("6", f"release blockers in {name}", str(len(blockers)), not blockers)
    return rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _n(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _arm_lines(name: str, data: dict[str, Any]) -> list[str]:
    calls, facts, final, presence, checks, gate = (
        data[k] for k in ("calls", "facts", "final_facts", "presence", "checks", "gate"))
    at = gate["at_default"]
    rubric = ", ".join(f"{k} {v:.2f}" for k, v in sorted(facts["rubric"].items())) or "n/a"
    lines = [f"## Arm {name}", "", f"Runs complete: {data['runs']['ok']} of {data['runs']['total']}", "",
             "| Calls | n | failed | in tokens | out tokens | reasoning | USD | p50 ms | p95 ms |",
             "|---|---|---|---|---|---|---|---|---|"]
    for kind, row in calls["by_kind"].items():
        lines.append(f"| {kind} | {row['calls']} | {row['failed']} | {row['input_tokens']} | {row['output_tokens']} "
                     f"| {row['reasoning_tokens']} | {_n(row['usd'])} | {_n(row['p50_ms'])} | {_n(row['p95_ms'])} |")
    bullets = [
        f"USD without the judge: {_n(calls['arm_usd'])}; judge: {_n(calls['judge_usd'])}",
        (f"Contact USD (writer, shorten, gate, Clairic): {_n(calls['contact_usd'])} over {calls['contact_days']} "
         f"contact-days = {_n(calls['usd_per_contact_day'])} per contact-day"),
        f"Shorten rate: {_pct(calls['shorten_rate'])}",
        f"Validation failures: {json.dumps(checks['validation_failures'], sort_keys=True)}",
        (f"Judge, all judged checkpoints: recall {_pct(facts['recall'])}, stale {_pct(facts['stale'])}, missing "
         f"{_pct(facts['missing'])}, priority-1 recall {_pct(facts['p1_recall'])}, unsupported per document "
         f"{_n(facts['unsupported_per_doc'])}, noise per document {_n(facts['noise_per_doc'])} "
         f"({facts['documents']} documents, {facts['facts']} facts, {facts['judge_errors']} judge errors)"),
        (f"Judge, final checkpoints: recall {_pct(final['recall'])}, stale {_pct(final['stale'])}, priority-1 "
         f"missing {final['p1_missing']}"),
        f"Rubric means: {rubric}",
        (f"Token check, final checkpoints: recall {_pct(presence['final_recall'])}, stale-only "
         f"{_pct(presence['final_stale'])}, priority-1 missing {presence['final_p1_missing']}; fact delay median "
         f"{_n(presence['delay_median'])} bursts, max {_n(presence['delay_max'])}, never seen "
         f"{presence['never_seen']}"),
        (f"Gate: {gate['checks']} checks, {checks['deferred']} deferrals; at {GATE_THRESHOLD}: TP {at['tp']}, "
         f"FN {at['fn']}, TN {at['tn']}, FP {at['fp']}; suggested threshold {_n(gate['suggested'])}"),
        (f"Hard canary hits: {json.dumps(checks['canaries_hard'], sort_keys=True)}; reported: "
         f"{json.dumps(checks['canaries_reported'], sort_keys=True)}"),
        (f"Max units: {json.dumps(checks['max_units'], sort_keys=True)}; over {jd.DOC_MAX_UNITS}: "
         f"{json.dumps(checks['over_limit'], sort_keys=True)}"),
        (f"Language checks: {json.dumps(checks['language'], sort_keys=True)}; posts: "
         f"{json.dumps(checks['post_status'], sort_keys=True)}"),
        f"S9 zero calls after the prune: {_cmp(checks['s9_no_calls'])}",
        f"Release blockers: {len(checks['blockers'])}",
    ]
    lines += ["", *(f"- {bullet}" for bullet in bullets), *(f"  - {blocker}" for blocker in checks["blockers"])]
    if gate["checks"]:
        lines += ["", "| Threshold | recall | deferral | TP | FN | TN | FP |", "|---|---|---|---|---|---|---|"]
        for row in gate["table"]:
            lines.append(f"| {row['threshold']} | {_pct(row['recall'])} | {_pct(row['deferral'])} | {row['tp']} "
                         f"| {row['fn']} | {row['tn']} | {row['fp']} |")
    return [*lines, ""]


def render_summary(meta: dict[str, Any], metrics: dict[str, dict[str, Any]], skipped: dict[str, str],
                   decision_rows: list[dict[str, Any]]) -> str:
    """summary.md: ids, numbers and pass/fail only. It never holds a document, quote, canary or message text."""
    stopped = "yes" if meta["budget_stopped"] else "no"
    spent = "simulated spend" if meta["dry_run"] else "spent"
    lines = [
        "# Telesec dossier eval",
        "",
        f"- Started: {meta['started']}",
        f"- Mode: {'dry run (no model calls)' if meta['dry_run'] else 'live'}",
        f"- Arms: {', '.join(meta['arms'])}",
        f"- Scenarios: {', '.join(meta['scenarios'])}; runs per arm: {meta['runs']}",
        f"- Judge: {meta['judge'] or 'off'}",
        f"- Budget: {meta['budget_usd']:.2f} USD; {spent}: {meta['spent_usd']:.4f} USD; stopped by the budget: {stopped}",
        "",
    ]
    if skipped:
        lines += ["## Skipped arms", "", *(f"- {name}: {reason}" for name, reason in skipped.items()), ""]
    for name, data in metrics.items():
        lines += _arm_lines(name, data)
    lines += ["## O.6 decisions", "", "| Rule | Check | Values | Result |", "|---|---|---|---|"]
    lines += [f"| {r['rule']} | {r['check']} | {r['values']} | {r['result']} |" for r in decision_rows]
    return "\n".join([*lines, ""])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _list(value: str, known: Iterable[str], what: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in items if item not in known]
    if unknown or not items:
        raise argparse.ArgumentTypeError(f"unknown {what}: {', '.join(unknown) or value!r}")
    return items


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live dossier eval with made-up chats (costs money unless --dry-run).")
    parser.add_argument("--arms", default=",".join(ARMS), type=lambda v: _list(v, ARMS, "arm"),
                        help=f"comma-separated arms (default all: {','.join(ARMS)})")
    parser.add_argument("--scenarios", default=",".join(sc.BY_ID), type=lambda v: _list(v, sc.BY_ID, "scenario"),
                        help="comma-separated scenario ids (default all)")
    parser.add_argument("--runs", type=int, default=1, help="runs per arm and scenario (default 1)")
    parser.add_argument("--budget-usd", type=float, default=6.0, help="hard spend limit in USD (default 6)")
    parser.add_argument("--judge", default=jd.DEFAULT_JUDGE, help=f"judge model (default {jd.DEFAULT_JUDGE}); "
                        "'off' skips the judge; google/ models are refused")
    parser.add_argument("--v1-worktree", type=Path, help=f"a worktree of {V1_TAG}, needed by the v1 arm")
    parser.add_argument("--out", type=Path, required=True, help="results go to OUT/<UTC timestamp>/")
    parser.add_argument("--dry-run", action="store_true", help="answer every request offline; no key, no cost")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if not args.budget_usd > 0:
        parser.error("--budget-usd must be positive")
    return args


def _secrets(dry_run: bool) -> dict[str, str]:
    if dry_run:
        return {"AI_GATEWAY_API_KEY": "dry-run-key", "CLOUDFLARE_ACCOUNT_ID": "0" * 32,
                "CLOUDFLARE_API_TOKEN": "dry-run-token"}
    return {name: os.environ.get(name, "").strip() for name in ll.SECRET_NAMES}


def main(argv: list[str] | None = None, *, opener: Callable[..., Any] | None = None) -> int:
    args = parse_args(argv)
    judge_model = None if args.judge.strip().lower() == "off" else args.judge
    if judge_model is not None:
        try:
            judge_model = jd.check_judge_model(judge_model)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    roots = {"v2": ROOT}
    if "v1" in args.arms:
        worktree = args.v1_worktree
        if worktree is None or not (worktree / "__init__.py").is_file() or not (worktree / "dossier_support.py").is_file():
            print(f"error: the v1 arm needs --v1-worktree PATH with the {V1_TAG} code. Create it with:\n"
                  f"  git worktree add ../telesec-v1 {V1_TAG}", file=sys.stderr)
            return 2
        roots["v1"] = worktree.resolve()
    secrets = _secrets(args.dry_run)
    if not secrets.get("AI_GATEWAY_API_KEY"):
        print("error: AI_GATEWAY_API_KEY is not set in the environment", file=sys.stderr)
        return 2
    ll.install_secret_scope(secrets)
    skipped: dict[str, str] = {}
    arms = [ARMS[name] for name in args.arms]
    if any(arm.clairic for arm in arms) and not (secrets.get("CLOUDFLARE_ACCOUNT_ID")
                                                 and secrets.get("CLOUDFLARE_API_TOKEN")):
        skipped["v2-clairic"] = "no credentials"
        arms = [arm for arm in arms if not arm.clairic]

    started = datetime.now(UTC)
    out_dir = args.out / started.strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=False)
    budget = ll.Budget(args.budget_usd)
    meters = {arm.name: ll.Meter(budget, key=secrets["AI_GATEWAY_API_KEY"],
                                 opener=opener or (ll.dry_run_opener if args.dry_run else None)) for arm in arms}
    results: dict[str, list[dict[str, Any]]] = {arm.name: [] for arm in arms}
    configs: dict[str, Any] = {}
    errors = False
    # Scenario-major order: when the budget stops the run, every arm has the same scenarios done.
    for run_index in range(1, args.runs + 1):
        for scenario_id in args.scenarios:
            for arm in arms:
                if arm.name in skipped or (arm.scenarios is not None and scenario_id not in arm.scenarios):
                    continue
                if budget.stopped:
                    break
                began = time.monotonic()
                try:
                    run = run_scenario(arm, sc.BY_ID[scenario_id], run_index, root=roots[arm.version],
                                       meter=meters[arm.name], judge_model=judge_model)
                except HarnessError as exc:
                    skipped[arm.name] = str(exc)
                    errors = True
                    continue
                configs.setdefault(arm.name, run.pop("config", None))
                results[arm.name].append(run)
                spent = sum(r.cost_usd for r in meters[arm.name].records if _scenario_of(r.step) == scenario_id)
                print(f"{scenario_id} run {run_index} {arm.name}: {run['status']}, "
                      f"{sum(s['calls'] for s in run['steps'])} calls, {spent:.4f} USD "
                      f"({time.monotonic() - began:.1f} s)")

    ran = {name: rows for name, rows in results.items() if rows}
    metrics = {name: arm_metrics(ARMS[name], rows, meters[name].records) for name, rows in ran.items()}
    subsets = {
        "gate": {n: arm_metrics(ARMS[n], r, meters[n].records, GATE_SCENARIOS) for n, r in ran.items()},
        "think": {n: arm_metrics(ARMS[n], r, meters[n].records, THINK_SCENARIOS) for n, r in ran.items()},
        "clairic": {n: arm_metrics(ARMS[n], r, meters[n].records, CLAIRIC_SCENARIOS) for n, r in ran.items()},
    }
    rows = decisions(metrics, subsets)
    for name, runs in ran.items():
        payload = {"arm": dataclasses.asdict(ARMS[name]), "config": configs.get(name), "runs": runs,
                   "calls": [r.as_dict() for r in meters[name].records], "metrics": metrics[name]}
        (out_dir / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n",
                                              encoding="utf-8")
    meta = {"started": started.isoformat(timespec="seconds"), "dry_run": args.dry_run, "arms": args.arms,
            "scenarios": args.scenarios, "runs": args.runs, "judge": judge_model, "budget_usd": args.budget_usd,
            "spent_usd": budget.spent_usd, "budget_stopped": budget.stopped}
    (out_dir / "summary.md").write_text(render_summary(meta, metrics, skipped, rows), encoding="utf-8")
    print(f"results: {out_dir}")
    incomplete = any(run["status"] != "ok" for rows_ in ran.values() for run in rows_)
    return 1 if budget.stopped or errors or incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
