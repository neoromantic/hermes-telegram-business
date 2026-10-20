"""A ``ctx.llm`` stand-in for the dossier eval, over Vercel AI Gateway, with a hard budget.

Every model call of the eval goes through one ``Meter``: the writer calls (``LiveLlm``), the Jev gate
and Clairic (wrapped from the plugin's own ``model_client``) and the judge. Before each call the meter
checks a conservative cost estimate against the budget and raises ``BudgetExceeded`` without calling
when the spend could go over it. After each call it records the purpose, tokens, cost and latency.

Requests go through ``model_client.post_json`` (https only, no redirects, a read cap). The key is read
from the process environment by ``run.py`` and is never printed, logged or stored.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Self

ROOT = Path(__file__).resolve().parents[2]
PROVIDER = "ai-gateway"
WRITER_MODEL = "google/gemini-3.8-flash"
V2_MAX_TOKENS = 8192  # TASK_DEFAULTS extra_body.max_tokens
V1_MAX_TOKENS = 16_384  # v1 MAX_OUTPUT_TOKENS
WRITER_TIMEOUT = 120.0
MAX_REQUEST_BYTES = 4_000_000  # the eval sends long first passes; the plugin's own cap is for the gate
BYTES_PER_TOKEN = 2.5  # fewer bytes per token than any real text, so the estimate stays high
RETRIES = 2
RETRY_KINDS = frozenset({"timeout", "network"})
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
# USD per million tokens (input, output), used when the gateway reports no cost.
PRICES_PER_M = {
    "google/gemini-3.8-flash": (0.75, 3.75),
    "anthropic/claude-sonnet-5.5": (2.0, 10.0),
}
UNKNOWN_PRICE_PER_M = (5.0, 25.0)  # an unlisted judge model: priced high so the budget stays safe
JEV_PRICE_PER_M = 0.042  # input tokens, as dossier_writer.JEV_PRICE_PER_M
CLAIRIC_PRICE_PER_M = 0.10  # an upper bound for the Workers AI preview; the API reports no cost
SECRET_NAMES = ("AI_GATEWAY_API_KEY", "AI_GATEWAY_BASE_URL", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")
# Hermes ``complete_structured`` adds this to the system prompt (v1 calls).
STRUCTURED_SUFFIX = ("Respond with a single JSON object that matches the requested shape. "
                     "Do not include prose or markdown fences.")
# Call purposes of both plugin versions -> the kind the report groups by.
PURPOSE_KINDS = {
    "telesec_dossier": "contact",
    "telesec_persona": "persona",
    "telesec_post": "post",
    "telesec_shorten": "shorten",
    "telegram_business_dossier": "contact",
    "telegram_business_persona": "persona",
}
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


class BudgetExceeded(RuntimeError):
    """The next call could take the spend over the budget, so it was not made."""


class LlmCallError(RuntimeError):
    """A failed model call. ``str(exc)`` is the error kind (and HTTP status), never a body or a key."""

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(kind if status is None else f"{kind} {status}")
        self.kind = kind
        self.status = status


def load_model_client() -> ModuleType:
    """The repo's ``model_client`` loaded on its own: it imports nothing from the plugin."""
    name = "telesec_eval_model_client"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, ROOT / "model_client.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def install_secret_scope(secrets: dict[str, str]) -> ModuleType:
    """Install ``agent.secret_scope`` for this process: the plugin reads its keys through it.

    Only the eval does this; ``secrets`` holds the few names in SECRET_NAMES and nothing else.
    """
    package = sys.modules.get("agent")
    if package is None:
        package = ModuleType("agent")
        package.__path__ = []
        sys.modules["agent"] = package
    module = ModuleType("agent.secret_scope")
    allowed = {name: value for name, value in secrets.items() if name in SECRET_NAMES and value}

    def get_secret(name: str, default: str | None = None) -> str | None:
        return allowed.get(name, default)

    module.get_secret = get_secret
    package.secret_scope = module
    sys.modules["agent.secret_scope"] = module
    return module


def prices(model: str) -> tuple[float, float]:
    return PRICES_PER_M.get(model.strip().lower(), UNKNOWN_PRICE_PER_M)


def estimate_usd(request_bytes: int, max_tokens: int, rates: tuple[float, float]) -> float:
    """An upper estimate of one call: every request byte priced as 1/2.5 input token, every output token used."""
    input_tokens = math.ceil(request_bytes / BYTES_PER_TOKEN)
    return (input_tokens * rates[0] + max_tokens * rates[1]) / 1_000_000


@dataclass
class Budget:
    limit_usd: float
    spent_usd: float = 0.0
    stopped: bool = False

    def check(self, estimate: float) -> None:
        """Raise BudgetExceeded when a call costing ``estimate`` could go over the limit; it stays stopped."""
        if self.stopped or self.spent_usd + estimate > self.limit_usd:
            self.stopped = True
            raise BudgetExceeded(f"budget {self.limit_usd:.2f} USD reached")

    def charge(self, usd: float) -> None:
        self.spent_usd += usd


@dataclass
class CallRecord:
    step: str
    purpose: str
    kind: str  # contact | persona | post | shorten | gate | clairic | judge
    of: str  # the kind a shorten call shortened; otherwise the same as kind
    model: str
    ok: bool
    error: str | None
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    priced_by: str  # gateway | table | estimate | none
    ms: int
    attempts: int = 1
    p: float | None = None  # the gate's higher answer
    refs: list[str] | None = None  # the gate's message refs ("m123"), to tell which chat it checked

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _content(payload: dict) -> str | None:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else None


def gateway_cost(payload: dict) -> float | None:
    """The cost the gateway reports: ``usage.cost``, else ``providerMetadata.gateway.cost``."""
    usage = payload.get("usage")
    cost = _float(usage.get("cost")) if isinstance(usage, dict) else None
    if cost is None:
        metadata = payload.get("providerMetadata")
        gateway = metadata.get("gateway") if isinstance(metadata, dict) else None
        cost = _float(gateway.get("cost")) if isinstance(gateway, dict) else None
    return cost


def parse_json_text(text: str) -> Any:
    """``json.loads`` of the first fenced block or of the whole text, as Hermes does; None when it fails."""
    match = _FENCE_RE.search(text)
    candidate = match.group(1) if match else text.strip()
    try:
        return json.loads(candidate)
    except ValueError:
        return None


class Meter:
    """The single path for every model call of one eval run: budget, retries and call records."""

    def __init__(
        self,
        budget: Budget,
        *,
        key: str | None,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.mc = load_model_client()
        self.budget = budget
        self.opener = opener
        self.records: list[CallRecord] = []
        self.step = ""
        self._key = key
        self._sleep = sleep
        self._last_writer_kind = "contact"

    @property
    def base(self) -> str:
        base = self.mc.gateway_base()
        if base is None:
            raise LlmCallError("bad_base_url")
        return base

    def kind_of(self, purpose: str) -> tuple[str, str]:
        """(kind, of) for a writer purpose; a shorten call counts toward the call it shortened."""
        kind = PURPOSE_KINDS.get(purpose, "contact")
        if kind == "shorten":
            return kind, self._last_writer_kind
        self._last_writer_kind = kind
        return kind, kind

    def chat(self, body: dict, *, purpose: str, kind: str, of: str, timeout: float) -> tuple[str, CallRecord]:
        """POST one chat completion with up to RETRIES retries and return (text, record).

        Raises BudgetExceeded before a call the budget does not allow, or LlmCallError.
        """
        model = str(body.get("model") or "")
        rates = prices(model)
        request_bytes = len(json.dumps(body, ensure_ascii=False).encode())
        estimate = estimate_usd(request_bytes, _int(body.get("max_tokens")) or V2_MAX_TOKENS, rates)
        attempts = 0
        started = time.monotonic()
        while True:
            self.budget.check(estimate)
            attempts += 1
            try:
                payload = self.mc.post_json(f"{self.base}/v1/chat/completions", body, key=self._key or "",
                                            timeout=timeout, opener=self.opener, max_request_bytes=MAX_REQUEST_BYTES)
            except self.mc.ModelCallError as exc:
                # A timed-out call may still be billed, so it is charged the estimate.
                charged = estimate if exc.kind == "timeout" else 0.0
                self.budget.charge(charged)
                retry = exc.kind in RETRY_KINDS or (exc.kind == "http" and exc.status in RETRY_STATUSES)
                if retry and attempts <= RETRIES:
                    self._sleep(float(attempts))
                    continue
                self._record(purpose, kind, of, model, ok=False, error=str(exc), cost=charged,
                             priced_by="estimate" if charged else "none", started=started, attempts=attempts)
                raise LlmCallError(exc.kind, exc.status) from None
            break
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details")
        input_tokens = _int(usage.get("prompt_tokens"))
        output_tokens = _int(usage.get("completion_tokens"))
        reasoning = _int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0
        cost = gateway_cost(payload)
        priced_by = "gateway"
        if cost is None:
            cost = (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
            priced_by = "table" if model.strip().lower() in PRICES_PER_M else "estimate"
        self.budget.charge(cost)
        text = _content(payload)
        record = self._record(purpose, kind, of, str(payload.get("model") or model), ok=text is not None,
                              error=None if text is not None else "bad_response", cost=cost, priced_by=priced_by,
                              started=started, attempts=attempts, input_tokens=input_tokens,
                              output_tokens=output_tokens, reasoning_tokens=reasoning)
        if text is None:
            raise LlmCallError("bad_response")
        return text, record

    def _record(self, purpose: str, kind: str, of: str, model: str, *, ok: bool, error: str | None, cost: float,
                priced_by: str, started: float, attempts: int = 1, input_tokens: int = 0, output_tokens: int = 0,
                reasoning_tokens: int = 0, p: float | None = None, refs: list[str] | None = None) -> CallRecord:
        record = CallRecord(step=self.step, purpose=purpose, kind=kind, of=of, model=model, ok=ok, error=error,
                            input_tokens=input_tokens, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens,
                            cost_usd=cost, priced_by=priced_by, ms=int((time.monotonic() - started) * 1000),
                            attempts=attempts, p=p, refs=refs)
        self.records.append(record)
        return record

    def wrap_model_client(self, module: ModuleType) -> dict[str, Callable[..., Any]]:
        """Metered versions of the plugin's ``jev_evaluate`` and ``clairic_extract`` (same signatures).

        A call the budget does not allow raises the plugin's own ModelCallError, so the plugin fails open
        exactly as on a network error; the writer call that follows then stops on the budget itself.
        """
        jev, clairic = module.jev_evaluate, module.clairic_extract

        def check(payload: Any, per_m: float) -> float:
            size = len(json.dumps(payload, ensure_ascii=False).encode())
            estimate = estimate_usd(size, 0, (per_m, 0.0))
            try:
                self.budget.check(estimate)
            except BudgetExceeded:
                raise module.ModelCallError("budget") from None
            return estimate

        def jev_evaluate(state: dict, questions: dict, *, timeout: float = module.GATE_TIMEOUT,
                         opener: Any = None) -> Any:
            estimate = check({"state": state, "questions": questions}, JEV_PRICE_PER_M)
            refs = [str(m.get("ref")) for m in state.get("new_messages") or [] if isinstance(m, dict)]
            started = time.monotonic()
            try:
                evaluation = jev(state, questions, timeout=timeout, opener=opener or self.opener)
            except module.ModelCallError as exc:
                self.budget.charge(estimate if exc.kind == "timeout" else 0.0)
                self._record("telesec_gate", "gate", "gate", module.JEV_MODEL, ok=False, error=str(exc),
                             cost=estimate if exc.kind == "timeout" else 0.0, priced_by="estimate", started=started,
                             refs=refs)
                raise
            cost, priced_by = evaluation.cost_usd, "gateway"
            if cost is None:
                cost, priced_by = evaluation.input_tokens * JEV_PRICE_PER_M / 1_000_000, "table"
            self.budget.charge(cost)
            answers = [value for value in evaluation.answers.values() if isinstance(value, float)]
            self._record("telesec_gate", "gate", "gate", module.JEV_MODEL, ok=True, error=None, cost=cost,
                         priced_by=priced_by, started=started, input_tokens=evaluation.input_tokens,
                         p=max(answers) if answers else None, refs=refs)
            return evaluation

        def clairic_extract(state: str, fields: dict, *, reference_date: str | None = None,
                            timeout: float = module.CLAIRIC_TIMEOUT, opener: Any = None) -> Any:
            estimate = check({"state": state, "fields": fields}, CLAIRIC_PRICE_PER_M)
            started = time.monotonic()
            try:
                extraction = clairic(state, fields, reference_date=reference_date, timeout=timeout,
                                     opener=opener or self.opener)
            except module.ModelCallError as exc:
                self.budget.charge(estimate if exc.kind == "timeout" else 0.0)
                self._record("telesec_clairic", "clairic", "clairic", module.CLAIRIC_MODEL, ok=False, error=str(exc),
                             cost=estimate if exc.kind == "timeout" else 0.0, priced_by="estimate", started=started)
                raise
            cost = extraction.input_tokens * CLAIRIC_PRICE_PER_M / 1_000_000
            self.budget.charge(cost)
            self._record("telesec_clairic", "clairic", "clairic", module.CLAIRIC_MODEL, ok=True, error=None,
                         cost=cost, priced_by="table", started=started, input_tokens=extraction.input_tokens)
            return extraction

        return {"jev_evaluate": jev_evaluate, "clairic_extract": clairic_extract}


def _usage(record: CallRecord) -> SimpleNamespace:
    return SimpleNamespace(input_tokens=record.input_tokens, output_tokens=record.output_tokens,
                           total_tokens=record.input_tokens + record.output_tokens,
                           reasoning_tokens=record.reasoning_tokens, cost_usd=None)


class LiveLlm:
    """``ctx.llm`` for one arm.

    ``complete`` sends what the registered ``telesec_dossier`` task sends (v2): reasoning ``effort``,
    ``max_tokens`` 8192 and zero data retention. ``complete_structured`` sends what Hermes sends for v1:
    the JSON-shape system suffix, the schema in the first user part and a ``json_schema`` response format.
    Results carry ``text``, ``provider``, ``model`` and ``usage`` like Hermes, plus ``parsed`` for v1.
    """

    def __init__(self, meter: Meter, *, reasoning: str = "low", model: str = WRITER_MODEL) -> None:
        self.meter = meter
        self.reasoning = reasoning
        self.model = model

    def complete(self, messages: list[dict], *, purpose: str | None = None, timeout: float | None = None,
                 model: str | None = None, **_route: Any) -> SimpleNamespace:
        body = {
            "model": model or self.model,
            "messages": list(messages),
            "reasoning": {"enabled": True, "effort": self.reasoning},
            "max_tokens": V2_MAX_TOKENS,
            "providerOptions": {"gateway": {"zeroDataRetention": True}},
        }
        kind, of = self.meter.kind_of(purpose or "")
        text, record = self.meter.chat(body, purpose=purpose or "", kind=kind, of=of,
                                       timeout=timeout or WRITER_TIMEOUT)
        return SimpleNamespace(text=text, provider=PROVIDER, model=record.model, usage=_usage(record))

    def complete_structured(self, *, input: list[dict], instructions: str = "", json_schema: dict | None = None,
                            schema_name: str = "", system_prompt: str | None = None, model: str | None = None,
                            max_tokens: int | None = None, timeout: float | None = None,
                            purpose: str | None = None, **_options: Any) -> SimpleNamespace:
        system = f"{system_prompt.strip()}\n\n{STRUCTURED_SUFFIX}" if system_prompt else STRUCTURED_SUFFIX
        head = instructions.strip()
        if schema_name:
            head += f"\n\nSchema name: {schema_name}"
        if json_schema is not None:
            head += "\n\nJSON schema:\n" + json.dumps(json_schema, ensure_ascii=False, sort_keys=True)
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [{"type": "text", "text": head}, *input]},
            ],
            "max_tokens": max_tokens or V1_MAX_TOKENS,
            "providerOptions": {"gateway": {"zeroDataRetention": True}},
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "plugin_structured_output", "schema": json_schema, "strict": False},
            }
        kind, of = self.meter.kind_of(purpose or "")
        text, record = self.meter.chat(body, purpose=purpose or "", kind=kind, of=of,
                                       timeout=timeout or WRITER_TIMEOUT)
        parsed = parse_json_text(text)
        return SimpleNamespace(text=text, parsed=parsed, content_type="json" if parsed is not None else "text",
                               provider=PROVIDER, model=record.model, usage=_usage(record))


# ---------------------------------------------------------------------------
# Dry run: canned answers for every request type, so the plumbing runs offline
# ---------------------------------------------------------------------------


class CannedResponse:
    """A 200 answer as ``urllib`` returns it: ``status``, ``read(n)`` and the context manager."""

    def __init__(self, payload: dict) -> None:
        self.status = 200
        self._body = json.dumps(payload).encode()

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


DRY_DOCUMENT = "**A contact of yours.**\n\n## Notes\n- Dry run, no model was called.\n<<END>>"
DRY_PERSONA = "## Style\n- Dry run, no model was called.\n<<END>>"
DRY_POST = "## Summary\n- Dry run, no model was called.\n<<END>>"


def _dry_judge(user_text: str) -> str:
    try:
        facts = json.loads(user_text).get("facts") or []
    except (ValueError, AttributeError):
        facts = []
    verdicts = [{"id": fact.get("id"), "verdict": "missing", "quote": ""} for fact in facts if isinstance(fact, dict)]
    rubric = dict.fromkeys(("accuracy", "currency", "usefulness", "concision", "structure"), 3)
    return json.dumps({"facts": verdicts, "unsupported": [], "noise": 0, "rubric": rubric})


def _dry_chat(body: dict) -> str:
    messages = body.get("messages") or [{}]
    system = str(messages[0].get("content") or "")
    user = messages[-1].get("content") if len(messages) > 1 else ""
    if "response_format" in body:
        return json.dumps({"changes": []})
    if not str(body.get("model", "")).startswith("google/"):
        return _dry_judge(user if isinstance(user, str) else "")
    if system.startswith("You describe how the account owner writes"):
        return DRY_PERSONA
    if system.startswith("You write a short summary"):
        return DRY_POST
    return DRY_DOCUMENT


def dry_run_opener(request: Any, timeout: float | None = None) -> CannedResponse:
    """An opener that answers every eval request offline: writer, v1 structured, judge, Jev and Clairic."""
    body = json.loads(request.data)
    url = request.full_url
    size = len(request.data) // 4
    if url.endswith("/v1/evaluate"):
        # Long bursts pass, short ones are deferred, so a dry run takes both gate paths.
        new = body.get("state", {}).get("new_messages") or []
        p = 0.9 if sum(len(str(m.get("text") or "")) for m in new if isinstance(m, dict)) > 150 else 0.1
        answers = {question: {"probability": p} for question in body.get("questions") or {}}
        return CannedResponse({"answers": answers, "usage": {"inputTokens": size}})
    if "api.cloudflare.com" in url:
        empty = {"value": None, "p_null": 0.9, "confidence": 0.5}
        answers = dict.fromkeys(body.get("fields") or {}, empty)
        return CannedResponse({"result": {"answers": answers, "usage": {"input_tokens": size}}, "success": True})
    text = _dry_chat(body)
    return CannedResponse({
        "model": body.get("model"),
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": size, "completion_tokens": len(text) // 4},
    })
