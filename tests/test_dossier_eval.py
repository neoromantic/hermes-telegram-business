"""Offline tests for the live dossier eval in ``tests/dossier_eval``.

No test here makes a network call or needs a key: model answers come from ``FakeOpener`` or the
eval's own ``--dry-run`` opener, and the ``no_network`` fixture refuses every socket.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from dossier_eval import judge as jd
from dossier_eval import live_llm as ll
from dossier_eval import run
from dossier_eval import scenarios as sc
from model_fakes import FakeOpener

ROOT = Path(__file__).resolve().parents[1]

V1_ENV = "TELESEC_EVAL_V1_WORKTREE"  # opt-in: a v0.13.0 worktree for the v1 arm test
O6_SCENARIOS = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S12"]
FACT_KINDS = ("other", "date", "amount", "contact")
PHONE_RE = re.compile(r"\+?\d(?:[\s().-]*\d){8,}")
MADE_UP_PHONE_RE = re.compile(r"\+1 555 555-01\d\d")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
HOME_PATH_RE = re.compile(r"/(?:Users|home)/\w|[A-Za-z]:\\\\Users")
SHARED_TMP_RE = re.compile(r"(?<![\w.~])/tmp/")
KEY_ENV = ("AI_GATEWAY_API_KEY", "AI_GATEWAY_BASE_URL", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")


def _has_v2_api() -> bool:
    source = (ROOT / "dossier_support.py").read_text(encoding="utf-8")
    return all(f"def {name}(" in source for name in (*run.V2_API, "set_clairic"))


needs_v2 = pytest.mark.skipif(not _has_v2_api(), reason="dossier_support does not have the v2 API yet")


def _v1_worktree() -> Path | None:
    """The v1 arm runs the code at this path, so only an explicit opt-in names it; never a fixed shared path."""
    value = os.environ.get(V1_ENV, "").strip()
    return Path(value) if value else None


needs_v1 = pytest.mark.skipif(_v1_worktree() is None, reason=f"set {V1_ENV} to a v0.13.0 worktree")


@pytest.fixture(autouse=True)
def offline(no_network: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No sockets, no keys from the developer's environment, and no leftover ``agent.secret_scope``."""
    for name in KEY_ENV:
        monkeypatch.delenv(name, raising=False)
    saved = {name: sys.modules.get(name) for name in ("agent", "agent.secret_scope")}
    yield
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _facts(scenario: sc.Scenario) -> list[sc.Fact]:
    return [fact for cp in scenario.checkpoints() for fact in cp.facts]


def _all_texts(scenario: sc.Scenario) -> list[str]:
    """Every piece of made-up content in a scenario: messages, notes, facts, canaries, reference and names."""
    texts = [m.text for m in scenario.messages() if m.text]
    texts += [note.text for note in scenario.notes()]
    for fact in _facts(scenario):
        texts += [fact.question, fact.current, *fact.stale]
    for canary in scenario.canaries:
        texts += [canary.text, *canary.alts]
    texts += [line for lines in scenario.reference.values() for line in lines]
    texts += list(scenario.chats.values())
    return texts


def _distinctive(text: str) -> bool:
    """Long enough, with letters, that finding it in a report cannot be a coincidence."""
    return len(text.strip()) >= 6 and any(ch.isalpha() for ch in text)


def _dead_ids(scenario: sc.Scenario) -> set[int]:
    """Deleted message ids plus the transcript chunks of a deleted voice (chunk 0 replies to it, later
    outbound chunks without a reply follow within 10 seconds), as the plugin's view labels them."""
    deleted = {m.id for m in scenario.messages() if m.event == "deleted"}
    dead = set(deleted)
    created = sorted((m for m in scenario.messages() if m.event == "created"), key=lambda m: m.at)
    last: sc.Message | None = None
    for m in created:
        if m.who != "out":
            continue
        if m.text.startswith("\N{STUDIO MICROPHONE}"):
            last = m if m.reply_to in deleted else None
        elif last is not None and m.reply_to is None and m.at - last.at <= timedelta(seconds=10):
            last = m
        else:
            last = None
        if last is not None:
            dead.add(m.id)
    return dead


def _chat_payload(text: str, *, model: str = ll.WRITER_MODEL, cost: float | None = None,
                  reasoning: int = 0) -> bytes:
    usage: dict[str, Any] = {"prompt_tokens": 120, "completion_tokens": 40,
                             "completion_tokens_details": {"reasoning_tokens": reasoning}}
    if cost is not None:
        usage["cost"] = cost
    return json.dumps({"model": model, "choices": [{"message": {"role": "assistant", "content": text}}],
                       "usage": usage}).encode()


class RecordingOpener:
    """The dry-run opener that also counts the requests it answered."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, request: Any, timeout: float | None = None) -> ll.CannedResponse:
        self.urls.append(request.full_url)
        return ll.dry_run_opener(request, timeout)


def _out_dir(base: Path) -> Path:
    runs = sorted(base.iterdir())
    assert len(runs) == 1
    return runs[0]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def test_the_o6_scenarios_load() -> None:
    assert [scenario.id for scenario in sc.ALL] == O6_SCENARIOS
    assert set(sc.BY_ID) == set(O6_SCENARIOS)


@pytest.mark.parametrize("scenario", sc.ALL, ids=lambda s: s.id)
def test_scenario_is_consistent(scenario: sc.Scenario) -> None:
    assert scenario.lang in ("en", "ru")
    assert all(scenario.chat_lang(chat) in ("en", "ru") for chat in scenario.chats)
    messages = scenario.messages()
    created: dict[int, sc.Message] = {}
    replied_to = {m.reply_to for m in messages if m.reply_to is not None}
    for m in messages:
        assert m.chat in scenario.chats and m.who in sc.WHO and m.event in sc.EVENTS
        if m.event == "created":
            assert m.text and m.id not in created, "created ids are unique across the scenario's chats"
            created[m.id] = m
        elif m.event == "edited":
            assert m.text and m.id in created and created[m.id].chat == m.chat
        else:
            assert not m.text and (m.id in created or m.id in replied_to)
    assert all(m.reply_to not in created or created[m.reply_to].at < m.at for m in messages if m.reply_to)

    for burst in scenario.bursts():
        assert burst.messages and all(m.chat == burst.chat for m in burst.messages)
    burst_ids = [(b.chat, b.id) for b in scenario.bursts()]
    assert len(burst_ids) == len(set(burst_ids))

    # Bursts between two other steps may interleave across chats; nothing else goes back in time.
    floor, latest = None, None
    for step in scenario.steps:
        if isinstance(step, sc.Burst):
            times = [m.at for m in step.messages]
            assert times == sorted(times)
            assert floor is None or times[0] >= floor
            latest = times[-1] if latest is None else max(latest, times[-1])
        else:
            assert floor is None or step.at >= floor
            assert latest is None or step.at >= latest
            if isinstance(step, sc.Checkpoint) and latest is not None:
                assert step.at - latest >= timedelta(minutes=10), "the quiet rule must be met"
            floor = step.at

    checkpoints = scenario.checkpoints()
    assert checkpoints and len({cp.id for cp in checkpoints}) == len(checkpoints)
    canary_ids = [c.id for c in scenario.canaries]
    assert len(canary_ids) == len(set(canary_ids))
    for cp in checkpoints:
        assert cp.target in ("auto", "persona")
        assert cp.canaries is None or set(cp.canaries) <= set(canary_ids)
        keys = [(f.chat, f.id) for f in cp.facts]
        assert len(keys) == len(set(keys))
        for fact in cp.facts:
            assert fact.chat in scenario.chats and fact.priority in (1, 2, 3) and fact.kind in FACT_KINDS
            assert fact.match and all(jd.key(token) for token in (*fact.match, *fact.stale_match))
            seen = [m.text for m in messages if m.chat == fact.chat and m.at <= cp.at]
            seen += [note.text for note in scenario.notes() if note.chat == fact.chat and note.at <= cp.at]
            assert jd.fact_presence(" ".join(seen), fact) == "current", "the chat must state the fact by then"


@pytest.mark.parametrize(("scenario", "canary"), [(s, c) for s in sc.ALL for c in s.canaries],
                         ids=lambda v: v.id)
def test_canary_occurs_in_its_scenario(scenario: sc.Scenario, canary: sc.Canary) -> None:
    assert set(canary.hard) <= set(sc.TARGETS) and set(canary.reported) <= set(sc.TARGETS)
    assert not set(canary.hard) & set(canary.reported) and (canary.hard or canary.reported)
    source = scenario.notes() if canary.kind == "note" else scenario.messages()
    holders = [item for item in source if jd.canary_hit(item.text, canary)]
    where = "notes" if canary.kind == "note" else "messages"
    assert holders, f"{scenario.id}/{canary.id} must occur in the scenario's {where}"
    if canary.kind == "deleted":
        dead = _dead_ids(scenario)
        assert all(m.id in dead for m in holders), "a deleted canary may only occur in dead messages"


def test_the_deleted_voice_is_a_legacy_transcript() -> None:
    s5 = sc.BY_ID["S5"]
    deleted = {m.id for m in s5.messages() if m.event == "deleted"}
    chunks = [m for m in s5.messages() if m.id in _dead_ids(s5) - deleted]
    assert len(chunks) == 3
    assert chunks[0].reply_to is not None and all(m.reply_to is None for m in chunks[1:])


def test_the_deleted_voice_in_a_post_is_reported_not_a_blocker() -> None:
    # Its transcript stays in the chat, so it is live content that /dossier may repeat (DOSSIERS.md, G2 limits).
    s5 = sc.BY_ID["S5"]
    cp = s5.checkpoints()[-1]
    assert cp.post and s5.canary("voice").hard == ()
    canaries = run._canaries(s5, cp, "post")
    empty = {"hard": [], "reported": []}

    def checks(text: str) -> dict[str, Any]:
        row = {"document": "", "canaries": empty,
               "post": {"status": "ok", "text": text, "canaries": run._canary_result(text, canaries, "post")}}
        step = {"id": cp.id, "report": {}, "chats": {str(next(iter(s5.chats))): row},
                "persona": {"document": "", "canaries": empty}}
        return run.check_scores([{"scenario": "S5", "steps": [step]}], run.ARMS["v2"])

    voice = checks("- Marek wants to talk about the Mokotów corner spot.")
    assert voice["blockers"] == [] and voice["canaries_reported"] == {"deleted:post": 1}
    rise = checks("- Prices go up 17 percent in November; Mokotow corner spot.")
    assert rise["blockers"] == [f"S5/{cp.id}: deleted canary rise in the post"]
    assert rise["canaries_hard"] == rise["canaries_reported"] == {"deleted:post": 1}


@pytest.mark.parametrize("scenario", sc.ALL, ids=lambda s: s.id)
def test_scenario_uses_made_up_data_only(scenario: sc.Scenario) -> None:
    for text in _all_texts(scenario):
        for phone in PHONE_RE.findall(text):
            assert MADE_UP_PHONE_RE.fullmatch(phone), "phones must be +1 555 555-01.."
        for mail in EMAIL_RE.findall(text):
            assert mail.endswith("@example.com"), "mail must be @example.com"
        assert not HOME_PATH_RE.search(text)


def test_eval_sources_use_made_up_data_only() -> None:
    for path in sorted((ROOT / "tests" / "dossier_eval").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not HOME_PATH_RE.search(source), path.name
        assert all(mail.endswith("@example.com") for mail in EMAIL_RE.findall(source)), path.name


def test_no_fixed_path_in_the_shared_temp_directory() -> None:
    # The v1 arm runs the code in its worktree; a fixed path there is one any local user could plant.
    docs = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md"))]
    for path in [*sorted((ROOT / "tests" / "dossier_eval").glob("*.py")), *docs]:
        assert not SHARED_TMP_RE.search(path.read_text(encoding="utf-8")), path.name


def test_the_v1_arm_test_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(V1_ENV, raising=False)
    assert _v1_worktree() is None
    monkeypatch.setenv(V1_ENV, " ../telesec-v1 ")
    assert _v1_worktree() == Path("../telesec-v1")


def test_consecutive_bursts_are_written_together() -> None:
    groups = run.grouped_steps(sc.BY_ID["S6"].steps)
    assert isinstance(groups[0], list) and {b.chat for b in groups[0]} == set(sc.BY_ID["S6"].chats)
    assert isinstance(groups[1], sc.Checkpoint)
    assert sum(len(g) if isinstance(g, list) else 1 for g in groups) == len(sc.BY_ID["S6"].steps)


# ---------------------------------------------------------------------------
# Model calls: request shapes, budget, retries
# ---------------------------------------------------------------------------


def test_a_live_run_reads_the_gateway_base_url_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # The plugin reads the base URL through the secret scope only, so the eval puts it there.
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://eval-proxy.example.com/v1")
    ll.install_secret_scope(run._secrets(False))
    opener = FakeOpener([(200, _chat_payload("**Doc.**\n<<END>>"))])
    ll.LiveLlm(ll.Meter(ll.Budget(1.0), key="test-key", opener=opener)).complete(
        [{"role": "user", "content": "hi"}], purpose="telesec_dossier")
    assert opener.calls[0]["url"] == "https://eval-proxy.example.com/v1/chat/completions"


def test_v2_writer_request_shape() -> None:
    opener = FakeOpener([(200, _chat_payload("**Doc.**\n<<END>>", cost=0.0021, reasoning=33))])
    meter = ll.Meter(ll.Budget(1.0), key="test-key", opener=opener)
    result = ll.LiveLlm(meter).complete([{"role": "user", "content": "hi"}], purpose="telesec_dossier")
    call = opener.calls[0]
    assert call["url"] == "https://ai-gateway.vercel.sh/v1/chat/completions"
    assert call["unredirected_headers"]["Authorization"] == "Bearer test-key"
    body = call["body"]
    assert body["model"] == "google/gemini-3.8-flash"
    assert body["reasoning"] == {"enabled": True, "effort": "low"}
    assert body["max_tokens"] == 8192
    assert body["providerOptions"] == {"gateway": {"zeroDataRetention": True}}
    assert "response_format" not in body and "temperature" not in body
    assert result.text == "**Doc.**\n<<END>>" and result.provider == "ai-gateway"
    record = meter.records[0]
    assert (record.kind, record.of, record.purpose) == ("contact", "contact", "telesec_dossier")
    assert (record.input_tokens, record.output_tokens, record.reasoning_tokens) == (120, 40, 33)
    assert record.cost_usd == pytest.approx(0.0021) and record.priced_by == "gateway"
    assert meter.budget.spent_usd == pytest.approx(0.0021)
    assert "test-key" not in json.dumps(record.as_dict())


def test_high_reasoning_and_shorten_attribution() -> None:
    opener = FakeOpener([(200, _chat_payload("a")), (200, _chat_payload("b"))])
    meter = ll.Meter(ll.Budget(1.0), key="test-key", opener=opener)
    llm = ll.LiveLlm(meter, reasoning="high")
    llm.complete([{"role": "user", "content": "x"}], purpose="telesec_persona")
    llm.complete([{"role": "user", "content": "y"}], purpose="telesec_shorten")
    assert opener.calls[0]["body"]["reasoning"]["effort"] == "high"
    assert [(r.kind, r.of) for r in meter.records] == [("persona", "persona"), ("shorten", "persona")]
    assert meter.records[0].priced_by == "table"
    assert meter.records[0].cost_usd == pytest.approx((120 * 0.75 + 40 * 3.75) / 1_000_000)


def test_v1_structured_request_shape() -> None:
    schema = {"type": "object", "properties": {"changes": {"type": "array"}}}
    opener = FakeOpener([(200, _chat_payload('```json\n{"changes": []}\n```'))])
    meter = ll.Meter(ll.Budget(1.0), key="test-key", opener=opener)
    result = ll.LiveLlm(meter).complete_structured(
        input=[{"type": "text", "text": "the chat"}], instructions="Update the dossier.", json_schema=schema,
        schema_name="dossier_changes", system_prompt="You keep a dossier.", purpose="telegram_business_dossier")
    body = opener.calls[0]["body"]
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "plugin_structured_output", "schema": schema, "strict": False}}
    assert body["max_tokens"] == 16_384 and body["providerOptions"]["gateway"]["zeroDataRetention"] is True
    system, user = body["messages"]
    assert system["content"].startswith("You keep a dossier.") and system["content"].endswith(ll.STRUCTURED_SUFFIX)
    head, rest = user["content"][0]["text"], user["content"][1:]
    assert head.startswith("Update the dossier.") and "dossier_changes" in head and '"changes"' in head
    assert rest == [{"type": "text", "text": "the chat"}]
    assert result.parsed == {"changes": []} and result.content_type == "json"
    assert meter.records[0].kind == "contact"


def test_budget_stops_before_a_call_it_cannot_afford() -> None:
    opener = FakeOpener()
    meter = ll.Meter(ll.Budget(0.01), key="test-key", opener=opener)
    with pytest.raises(ll.BudgetExceeded):
        ll.LiveLlm(meter).complete([{"role": "user", "content": "hi"}], purpose="telesec_dossier")
    assert opener.calls == [] and meter.records == []
    assert meter.budget.stopped and meter.budget.spent_usd == 0


def test_budget_stops_the_second_call_and_stays_stopped() -> None:
    opener = FakeOpener([(200, _chat_payload("first", cost=0.025))])
    meter = ll.Meter(ll.Budget(0.05), key="test-key", opener=opener)
    llm = ll.LiveLlm(meter)
    assert llm.complete([{"role": "user", "content": "one"}], purpose="telesec_dossier").text == "first"
    with pytest.raises(ll.BudgetExceeded):
        llm.complete([{"role": "user", "content": "two"}], purpose="telesec_dossier")
    assert len(opener.calls) == 1 and meter.budget.spent_usd == pytest.approx(0.025)

    # The plugin's own gate and Clairic calls then fail open with the plugin's error, without a request.
    client = ll.load_model_client()
    wrapped = meter.wrap_model_client(client)
    with pytest.raises(client.ModelCallError) as info:
        wrapped["jev_evaluate"]({"new_messages": [{"ref": "m1", "text": "x"}]}, {"q": {"type": "boolean"}})
    assert info.value.kind == "budget"
    with pytest.raises(client.ModelCallError):
        wrapped["clairic_extract"]("state", {"date": {"type": "date"}})
    assert len(opener.calls) == 1


def test_estimate_is_conservative() -> None:
    assert ll.estimate_usd(2500, 0, (1.0, 0.0)) == pytest.approx(0.001)
    assert ll.estimate_usd(0, 8192, ll.prices(ll.WRITER_MODEL)) == pytest.approx(8192 * 3.75 / 1_000_000)
    assert ll.prices("someone/unknown-model") == ll.UNKNOWN_PRICE_PER_M


def test_retries_a_server_error_then_succeeds() -> None:
    sleeps: list[float] = []
    opener = FakeOpener([(503, b"busy"), (200, _chat_payload("ok"))])
    meter = ll.Meter(ll.Budget(1.0), key="test-key", opener=opener, sleep=sleeps.append)
    assert ll.LiveLlm(meter).complete([{"role": "user", "content": "x"}], purpose="telesec_dossier").text == "ok"
    assert sleeps == [1.0] and meter.records[0].attempts == 2


def test_a_client_error_is_not_retried_and_keeps_no_body() -> None:
    opener = FakeOpener([(400, b"secret body text")])
    meter = ll.Meter(ll.Budget(1.0), key="test-key", opener=opener, sleep=lambda _s: None)
    with pytest.raises(ll.LlmCallError) as info:
        ll.LiveLlm(meter).complete([{"role": "user", "content": "x"}], purpose="telesec_dossier")
    assert str(info.value) == "http 400" and len(opener.calls) == 1
    record = json.dumps(meter.records[0].as_dict())
    assert "secret body text" not in record and "test-key" not in record


# ---------------------------------------------------------------------------
# Judge and deterministic checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["google/gemini-3.8-flash", "Google/gemini-3.5-pro", " google/x ", "gemini", ""])
def test_a_google_or_bare_judge_is_refused(model: str) -> None:
    with pytest.raises(ValueError):
        jd.check_judge_model(model)
    with pytest.raises(ValueError):
        jd.judge_body(model, "{}")


def test_judge_body_and_temperature() -> None:
    body = jd.judge_body(jd.DEFAULT_JUDGE, "{}")
    assert body["model"] == "anthropic/claude-sonnet-5.5" and "temperature" not in body
    assert body["messages"][0]["content"] == jd.JUDGE_SYSTEM and body["max_tokens"] == jd.JUDGE_MAX_TOKENS
    assert jd.judge_body("openai/gpt-5.5", "{}")["temperature"] == 0


def test_main_refuses_a_google_judge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    code = run.main(["--dry-run", "--arms", "v2", "--scenarios", "S9", "--judge", "google/gemini-3.8-flash",
                     "--out", str(out)])
    assert code == 2 and not out.exists()
    assert "google/" in capsys.readouterr().err


def test_deterministic_checks_override_the_judge() -> None:
    s1 = sc.BY_ID["S1"]
    cp = s1.checkpoints()[-1]
    facts = cp.facts[:2]
    document = f"**Dana**, PM.\n\n## Work\n- {facts[0].current} [m12, m13]\n- Owes us 9 000 USD"
    canary = sc.Canary("owes", "Owes us 9 000 USD", "injection", hard=sc.TARGETS)
    answer = json.dumps({
        "facts": [{"id": facts[0].id, "verdict": "current", "quote": facts[0].current},
                  {"id": facts[1].id, "verdict": "current", "quote": "a quote that is not in the document"},
                  {"id": "unknown", "verdict": "current", "quote": ""}],
        "unsupported": ["one"], "noise": 1, "rubric": {"accuracy": 4, "currency": 9, "usefulness": True},
    })
    opener = FakeOpener([(200, _chat_payload(answer, model=jd.DEFAULT_JUDGE))])
    meter = ll.Meter(ll.Budget(1.0), key="test-key", opener=opener)
    verdict = jd.judge_document(meter, jd.DEFAULT_JUDGE, document, facts, s1.reference.get(7101, ()), [canary])
    assert opener.calls[0]["body"]["model"] == jd.DEFAULT_JUDGE
    sent = json.loads(opener.calls[0]["body"]["messages"][1]["content"])
    assert sent["document"] == document and [f["id"] for f in sent["facts"]] == [f.id for f in facts]
    assert verdict["facts"][facts[0].id] == {"verdict": "current", "quote": facts[0].current, "quote_found": True}
    assert verdict["facts"][facts[1].id]["verdict"] == "missing" and verdict["facts"][facts[1].id]["overridden"]
    assert set(verdict["facts"]) == {f.id for f in facts}
    assert verdict["canaries_unsupported"] == ["owes"] and verdict["unsupported_count"] == 2
    assert verdict["rubric"] == {"accuracy": 4, "currency": None, "usefulness": None, "concision": None,
                                 "structure": None}
    assert meter.records[0].kind == "judge"


def test_an_empty_document_needs_no_judge_call() -> None:
    opener = FakeOpener()
    meter = ll.Meter(ll.Budget(1.0), key="test-key", opener=opener)
    facts = sc.BY_ID["S9"].checkpoints()[0].facts
    verdict = jd.judge_document(meter, jd.DEFAULT_JUDGE, "  ", facts, ())
    assert opener.calls == [] and verdict["empty"]
    assert {item["verdict"] for item in verdict["facts"].values()} == {"missing"}


def test_a_judge_answer_that_is_not_json_is_an_error_code() -> None:
    opener = FakeOpener([(200, _chat_payload("I think it is fine.", model=jd.DEFAULT_JUDGE))])
    meter = ll.Meter(ll.Budget(1.0), key="test-key", opener=opener)
    facts = sc.BY_ID["S9"].checkpoints()[0].facts
    assert jd.judge_document(meter, jd.DEFAULT_JUDGE, "**Nina.**", facts, ()) == {"error": "not_json"}


def test_fact_presence_matches_any_format() -> None:
    phone = next(f for f in sc.BY_ID["S1"].checkpoints()[-1].facts if f.kind == "contact" and f.stale)
    assert jd.fact_presence("Mobile: +1 (555) 555 01 03", phone) == "current"
    assert jd.fact_presence("Mobile: +1 555 555-0127", phone) == "stale"
    assert jd.fact_presence("No phone yet.", phone) == "missing"


def test_text_checks() -> None:
    assert jd.text_norm("**Deadline:** 30 October [m12, m14]") == "deadline: 30 october"
    assert jd.units("a😀") == 3
    assert jd.language_ok("Дедлайн 30 октября", "ru") and not jd.language_ok("Deadline 30 October", "ru")
    assert jd.language_ok("Deadline 30 October", "en") and jd.language_ok("12:00", "en") is None
    canary = sc.Canary("rise", "17 percent", "deleted", ("17%",))
    assert jd.canary_hits("Prices go up 17% in November", [canary]) == ["rise"]


def test_gate_table_suggests_the_highest_full_recall_threshold() -> None:
    rows = [{"p": 0.42, "informative": True}, {"p": 0.9, "informative": True},
            {"p": 0.3, "informative": False}, {"p": 0.1, "informative": False}, {"p": 0.5, "informative": None}]
    table = run.gate_table(rows)
    assert table["suggested"] == 0.4 and table["checks"] == 5 and table["unlabelled"] == 1
    assert table["at_default"] == {"threshold": 0.35, "tp": 2, "fn": 0, "tn": 2, "fp": 0, "recall": 1.0,
                                   "deferral": 1.0}
    assert run.gate_table([{"p": 0.1, "informative": True}])["suggested"] is None


# ---------------------------------------------------------------------------
# summary.md holds numbers and pass/fail only
# ---------------------------------------------------------------------------


def _leaky_run(scenario: sc.Scenario) -> dict[str, Any]:
    """A result where every document, post, persona and judge answer repeats the scenario's text."""
    texts = _all_texts(scenario)
    leak = "\n".join(f"- {text}" for text in texts)
    steps = []
    for cp in scenario.checkpoints():
        chats = {}
        for chat in scenario.chats:
            facts = [f for f in cp.facts if f.chat == chat]
            doc_canaries = run._canaries(scenario, cp, "document")
            answer = {"facts": {f.id: {"verdict": "current", "quote": f.current} for f in facts},
                      "unsupported": texts[:3], "noise": 2, "rubric": dict.fromkeys(jd.RUBRIC, 4)}
            post_canaries = run._canaries(scenario, cp, "post")
            chats[str(chat)] = {
                "document": leak, "units": jd.units(leak), "language_ok": jd.language_ok(leak, scenario.lang),
                "canaries": run._canary_result(leak, doc_canaries, "document"),
                "presence": {f.id: jd.fact_presence(leak, f) for f in facts}, "unchanged": None,
                "judge": jd.apply_checks(answer, leak, doc_canaries),
                "post": {"status": "ok", "error": None, "text": leak, "units": jd.units(leak),
                         "canaries": run._canary_result(leak, post_canaries, "post")},
            }
        steps.append({
            "id": cp.id, "at": cp.at.isoformat(), "target": cp.target, "chats": chats, "calls": 1,
            "calls_by_kind": {"contact": 1}, "report": {"deferred": 0, "failures": [["contact", "too_long"]]},
            "persona": {"document": leak, "units": jd.units(leak),
                        "canaries": run._canary_result(leak, run._canaries(scenario, cp, "persona"), "persona")},
        })
    gate = [{"step": "cp0", "chat": next(iter(scenario.chats)), "p": 0.6, "informative": True}]
    return {"scenario": scenario.id, "run": 1, "steps": steps, "gate": gate, "status": "ok"}


def test_summary_holds_no_scenario_text() -> None:
    results = [_leaky_run(scenario) for scenario in sc.ALL]
    records = [ll.CallRecord(step=f"{s.id}#1/{s.checkpoints()[0].id}", purpose="telesec_dossier", kind="contact",
                             of="contact", model=ll.WRITER_MODEL, ok=True, error=None, input_tokens=100,
                             output_tokens=50, reasoning_tokens=5, cost_usd=0.001, priced_by="gateway", ms=900)
               for s in sc.ALL]
    metrics = {name: run.arm_metrics(run.ARMS[name], results, records) for name in ("v2", "v2-gate")}
    subsets = {group: {name: run.arm_metrics(run.ARMS[name], results, records, ids) for name in metrics}
               for group, ids in (("gate", run.GATE_SCENARIOS), ("think", run.THINK_SCENARIOS),
                                  ("clairic", run.CLAIRIC_SCENARIOS))}
    meta = {"started": "2026-10-20T00:00:00+00:00", "dry_run": False, "arms": list(metrics), "scenarios": O6_SCENARIOS,
            "runs": 1, "judge": jd.DEFAULT_JUDGE, "budget_usd": 6.0, "spent_usd": 0.01, "budget_stopped": False}
    summary = run.render_summary(meta, metrics, {"v2-clairic": "no credentials"}, run.decisions(metrics, subsets))

    # The leaks reached the checks: canary hits and size blockers are counted, by id only.
    assert "Release blockers: 0" not in summary and "injection canary owes in the post" in summary
    assert "| 6 | release blockers in v2 |" in summary
    for scenario in sc.ALL:
        for text in filter(_distinctive, _all_texts(scenario)):
            assert unicodedata.normalize("NFC", text) not in summary, f"{scenario.id} text leaked into summary.md"


class EchoOpener:
    """The dry-run opener, except that every writer document and judge answer repeats ``lines``."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def __call__(self, request: Any, timeout: float | None = None) -> ll.CannedResponse:
        body = json.loads(request.data)
        if not request.full_url.endswith("/v1/chat/completions") or "response_format" in body:
            return ll.dry_run_opener(request, timeout)
        if str(body["model"]).startswith("google/"):
            persona = str(body["messages"][0]["content"]).startswith("You describe how the account owner writes")
            head = "## Style" if persona else "**A contact.**\n\n## Notes"
            text = "\n".join([head, *(f"- {line}" for line in self.lines), "<<END>>"])
        else:
            facts = json.loads(body["messages"][-1]["content"])["facts"]
            text = json.dumps({"facts": [{"id": f["id"], "verdict": "current", "quote": self.lines[0]} for f in facts],
                               "unsupported": self.lines[:3], "noise": 1, "rubric": dict.fromkeys(jd.RUBRIC, 4)})
        return ll.CannedResponse({"model": body["model"], "choices": [{"message": {"content": text}}],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 10}})


@needs_v2
def test_dry_run_summary_holds_no_scenario_text(tmp_path: Path) -> None:
    ids = ["S5", "S6", "S7", "S12"]
    lines = [canary.text for scenario_id in ids for canary in sc.BY_ID[scenario_id].canaries]
    code = run.main(["--dry-run", "--arms", "v2", "--scenarios", ",".join(ids), "--out", str(tmp_path)],
                    opener=EchoOpener(lines))
    assert code == 0
    out = _out_dir(tmp_path)
    summary = (out / "summary.md").read_text(encoding="utf-8")
    # The canaries reached the saved documents and the checks, which report them by id only.
    assert "S6/cp2: injection canary raven in the document" in summary
    assert "S12/p2: other_chat canary phrase in the persona" in summary
    assert "| 6 | release blockers in v2 | 0 |" not in summary
    assert lines[0] in (out / "v2.json").read_text(encoding="utf-8")
    for scenario_id in ids:
        for text in filter(_distinctive, _all_texts(sc.BY_ID[scenario_id])):
            assert text not in summary


# ---------------------------------------------------------------------------
# The CLI end to end, with the dry-run opener
# ---------------------------------------------------------------------------


def test_the_v1_arm_needs_a_worktree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for extra in ([], ["--v1-worktree", str(tmp_path / "missing")]):
        code = run.main(["--dry-run", "--arms", "v1", "--scenarios", "S9", "--out", str(tmp_path / "out"), *extra])
        assert code == 2
        assert "git worktree add ../telesec-v1 v0.13.0" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_a_live_run_needs_the_gateway_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run.main(["--arms", "v2", "--scenarios", "S9", "--out", str(tmp_path / "out")])
    assert code == 2 and not (tmp_path / "out").exists()
    err = capsys.readouterr().err
    assert "AI_GATEWAY_API_KEY" in err


def test_clairic_is_skipped_without_cloudflare_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key")
    opener = FakeOpener()
    code = run.main(["--arms", "v2-clairic", "--scenarios", "S9", "--judge", "off", "--out", str(tmp_path)],
                    opener=opener)
    assert code == 0 and opener.calls == []
    summary = (_out_dir(tmp_path) / "summary.md").read_text(encoding="utf-8")
    assert "- v2-clairic: no credentials" in summary
    assert "test-key" not in summary and "test-key" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--runs", "0"], ["--budget-usd", "0"], ["--arms", "v3"], ["--scenarios", "S10"]])
def test_usage_errors(argv: list[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        run.parse_args(["--out", str(tmp_path), *argv])
    assert info.value.code == 2


@needs_v2
def test_dry_run_of_the_v2_arms(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    opener = RecordingOpener()
    code = run.main(["--dry-run", "--arms", "v2,v2-gate,v2-clairic", "--scenarios", "S1,S9,S12",
                     "--out", str(tmp_path)], opener=opener)
    assert code == 0
    out = _out_dir(tmp_path)
    assert sorted(p.name for p in out.iterdir()) == ["summary.md", "v2-clairic.json", "v2-gate.json", "v2.json"]
    assert all(url.startswith("https://") for url in opener.urls)
    printed = capsys.readouterr().out
    assert "dry-run-key" not in printed and "dry-run-token" not in printed

    v2 = json.loads((out / "v2.json").read_text(encoding="utf-8"))
    assert v2["config"]["gate"] == "off" and v2["config"]["model"] == ll.WRITER_MODEL
    assert [r["status"] for r in v2["runs"]] == ["ok", "ok", "ok"]
    kinds = {c["kind"] for c in v2["calls"]}
    assert {"contact", "persona", "judge"} <= kinds and "gate" not in kinds
    gate = json.loads((out / "v2-gate.json").read_text(encoding="utf-8"))
    assert gate["metrics"]["gate"]["checks"] > 0
    clairic = json.loads((out / "v2-clairic.json").read_text(encoding="utf-8"))
    assert any(c["kind"] == "clairic" for c in clairic["calls"])

    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "Mode: dry run (no model calls)" in summary and "simulated spend" in summary
    assert "S9 zero calls after the prune: pass" in summary
    assert "## O.6 decisions" in summary and "| 3 | GATE_THRESHOLD" in summary


@needs_v2
def test_dry_run_stops_on_the_budget(tmp_path: Path) -> None:
    opener = RecordingOpener()
    code = run.main(["--dry-run", "--arms", "v2", "--scenarios", "S9", "--judge", "off", "--budget-usd", "0.001",
                     "--out", str(tmp_path)], opener=opener)
    assert code == 1 and opener.urls == []
    summary = (_out_dir(tmp_path) / "summary.md").read_text(encoding="utf-8")
    assert "stopped by the budget: yes" in summary


@needs_v2
def test_an_unexpected_error_keeps_the_other_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    real_cycle = run.Harness.cycle

    def cycle(self: run.Harness, llm: ll.LiveLlm, target: str, now: Any) -> dict[str, Any]:
        if self.plugin.__name__.startswith("telesec_eval_v2") and target == "persona":
            raise ValueError("Mokotow private text")
        return real_cycle(self, llm, target, now)

    monkeypatch.setattr(run.Harness, "cycle", cycle)
    code = run.main(["--dry-run", "--arms", "v2", "--scenarios", "S12,S9", "--judge", "off", "--out", str(tmp_path)])
    assert code == 1
    runs = json.loads((_out_dir(tmp_path) / "v2.json").read_text(encoding="utf-8"))["runs"]
    assert [(r["scenario"], r["status"]) for r in runs] == [("S12", "error ValueError"), ("S9", "ok")]
    assert "Mokotow" not in capsys.readouterr().out


@needs_v1
def test_dry_run_of_the_v1_arm(tmp_path: Path) -> None:
    worktree = _v1_worktree()
    assert worktree is not None and (worktree / "dossier_support.py").is_file(), f"{V1_ENV} is not a v0.13.0 worktree"
    code = run.main(["--dry-run", "--arms", "v1", "--v1-worktree", str(worktree), "--scenarios", "S5,S7,S12",
                     "--out", str(tmp_path)])
    assert code == 0
    v1 = json.loads((_out_dir(tmp_path) / "v1.json").read_text(encoding="utf-8"))
    assert [r["status"] for r in v1["runs"]] == ["ok", "ok", "ok"]
    assert any(c["kind"] == "contact" for c in v1["calls"])
