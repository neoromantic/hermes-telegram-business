"""Direct HTTPS calls to the two models that ``ctx.llm`` cannot reach.

TypeSafe Jev answers the message check through Vercel AI Gateway (``/v1/evaluate`` is not a
chat endpoint). Cloudflare Clairic picks dates, amounts and contact details out of new
messages on Workers AI, only when the owner turns fact extraction on.

Stdlib-only, and nothing is imported from the plugin. Keys come from the Hermes profile secret
scope and are never stored, logged or put into an exception. Requests go to https only, never
follow a redirect and read at most ``MAX_RESPONSE_BYTES``. Logs carry the model, an error kind,
an HTTP status, timings and token counts only: never the state, questions, answers, bodies,
headers, URLs or keys.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_NAME = "telesec"
USER_AGENT = "telesec"  # no version: releases bump versions by hand in a fixed list of files
JEV_MODEL = "typesafe-ai/jev-1.13.0"  # pinned: the alias drifts
DEFAULT_BASE = "https://ai-gateway.vercel.sh"
KEY_NAME = "AI_GATEWAY_API_KEY"  # existing Hermes secret; read, never stored or logged
BASE_ENV = "AI_GATEWAY_BASE_URL"  # existing Hermes variable; not a new behaviour switch
GATE_TIMEOUT = 10.0
MAX_REQUEST_BYTES = 200_000
MAX_RESPONSE_BYTES = 1_048_576
CF_ACCOUNT_NAME = "CLOUDFLARE_ACCOUNT_ID"
CF_TOKEN_NAME = "CLOUDFLARE_API_TOKEN"
CLAIRIC_MODEL = "@cf/cloudflare/clairic"
CLAIRIC_TIMEOUT = 10.0
# The host is fixed and the account id is 32 hex digits, so the secrets cannot send the token elsewhere.
CLAIRIC_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/" + CLAIRIC_MODEL
_ACCOUNT_RE = re.compile(r"[0-9a-f]{32}")
_SECRET_RE = re.compile(r"[!-~]+")  # printable ASCII without spaces: anything else breaks the header


class ModelCallError(Exception):
    """kind: no_key | bad_base_url | too_large | http | timeout | network | bad_response.

    ``str(exc)`` is the kind, or ``"<kind> <status>"``; it never holds a body, URL, header or key.
    """

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(kind if status is None else f"{kind} {status}")
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class Evaluation:
    answers: dict[str, float]  # boolean question id -> probability in [0, 1]
    input_tokens: int
    cost_usd: float | None
    ms: int


@dataclass(frozen=True)
class Extraction:
    answers: dict[str, dict]  # field name -> Clairic's answer object (value, p_null, confidence, ...)
    input_tokens: int
    ms: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the 3xx then raises ``HTTPError``, and the key stays with the first host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _secret(name: str) -> str | None:
    # Never os.environ: under a multiplexing gateway it holds the launch profile's keys, not this profile's.
    try:
        from agent.secret_scope import get_secret

        value = get_secret(name, None)
    except Exception:  # noqa: BLE001 - ImportError, UnscopedSecretError or a host bug: no key
        return None
    value = value.strip() if isinstance(value, str) else ""
    return value if _SECRET_RE.fullmatch(value) else None


def _https_url(url: str) -> bool:
    # Printable ASCII without spaces: http.client would raise ValueError on anything else.
    if not url.isascii() or not url.isprintable() or " " in url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        return (
            parts.scheme == "https"
            and bool(parts.hostname)
            and parts.port != 0  # .port raises ValueError for a port outside 0..65535
            and "@" not in parts.netloc
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        return False


def gateway_key() -> str | None:
    """The AI Gateway key of the active Hermes profile, or None (the message check is then off)."""
    return _secret(KEY_NAME)


def gateway_base() -> str | None:
    """The AI Gateway base URL without a trailing ``/`` or ``/v1``; None unless it is https with a host."""
    base = os.environ.get(BASE_ENV, "").strip() or DEFAULT_BASE
    base = base.rstrip("/").removesuffix("/v1")
    return base if _https_url(base) else None


def cloudflare_credentials() -> tuple[str, str] | None:
    """``(account_id, token)`` for Workers AI, or None when either is missing or the id is not 32 hex digits."""
    account_id = _secret(CF_ACCOUNT_NAME)
    token = _secret(CF_TOKEN_NAME)
    if account_id is None or token is None or not _ACCOUNT_RE.fullmatch(account_id):
        return None
    return account_id, token


def post_json(
    url: str,
    body: dict,
    *,
    key: str,
    timeout: float,
    opener: Callable[..., Any] | None = None,
    max_request_bytes: int = MAX_REQUEST_BYTES,
) -> dict:
    """POST ``body`` as JSON with a Bearer key and return the JSON object of the answer.

    ``opener`` is called as ``opener(request, timeout=timeout)``; the default opener never
    follows redirects. Raises ``ModelCallError`` only, always ``from None``.
    """
    return _send(url, body, key=key, timeout=timeout, opener=opener, max_request_bytes=max_request_bytes)[1]


def _send(
    url: str,
    body: dict,
    *,
    key: str,
    timeout: float,
    opener: Callable[..., Any] | None,
    max_request_bytes: int = MAX_REQUEST_BYTES,
) -> tuple[int, dict]:
    if not _https_url(url):
        raise ModelCallError("bad_base_url")
    if not isinstance(key, str) or not _SECRET_RE.fullmatch(key):
        raise ModelCallError("no_key")
    data = json.dumps(body, ensure_ascii=False).encode()
    if len(data) > max_request_bytes:
        raise ModelCallError("too_large")
    request = urllib.request.Request(url, data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    # urllib copies unredirected headers to no other request, so the key never follows a redirect.
    request.add_unredirected_header("Authorization", f"Bearer {key}")
    open_url = opener or urllib.request.build_opener(_NoRedirect).open
    try:
        with open_url(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()  # the error body is never read
        raise ModelCallError("http", exc.code) from None
    except urllib.error.URLError as exc:
        raise ModelCallError("timeout" if isinstance(exc.reason, TimeoutError) else "network") from None
    except TimeoutError:  # socket.timeout is the same class
        raise ModelCallError("timeout") from None
    except (OSError, http.client.HTTPException):
        raise ModelCallError("network") from None
    if not 200 <= status < 300:
        raise ModelCallError("http", status)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelCallError("bad_response")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise ModelCallError("bad_response") from None
    if not isinstance(payload, dict):
        raise ModelCallError("bad_response")
    return status, payload


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _count(usage: Any, name: str) -> int:
    value = usage.get(name) if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _probability(answers: Any, question_id: str) -> float:
    answer = answers.get(question_id) if isinstance(answers, dict) else None
    value = answer.get("probability") if isinstance(answer, dict) else None
    # The range check also rejects NaN and the infinities, which json.loads accepts.
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ModelCallError("bad_response")
    return float(value)


def _cost(payload: dict) -> float | None:
    # The gateway reports the cost in USD as a string.
    try:
        cost = float(payload["providerMetadata"]["gateway"]["cost"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def jev_evaluate(
    state: dict,
    questions: dict,
    *,
    timeout: float = GATE_TIMEOUT,
    opener: Callable[..., Any] | None = None,
) -> Evaluation:
    """Ask Jev the boolean ``questions`` about ``state`` in one call, without retries."""
    started = time.monotonic()
    try:
        key = gateway_key()
        if key is None:
            raise ModelCallError("no_key")
        base = gateway_base()
        if base is None:
            raise ModelCallError("bad_base_url")
        body = {
            "model": JEV_MODEL,
            "state": state,
            "questions": questions,
            # Always zero data retention and TypeSafe only: no fallback provider sees the messages.
            "providerOptions": {"gateway": {"zeroDataRetention": True, "only": ["typesafe-ai"]}},
        }
        status, payload = _send(f"{base}/v1/evaluate", body, key=key, timeout=timeout, opener=opener)
        answers = {question_id: _probability(payload.get("answers"), question_id) for question_id in questions}
    except ModelCallError as exc:
        logger.warning("%s: model check failed kind=%s status=%s", PLUGIN_NAME, exc.kind, exc.status)
        raise
    ms = _ms(started)
    input_tokens = _count(payload.get("usage"), "inputTokens")
    logger.debug("%s: model check model=%s status=%s ms=%d in=%d", PLUGIN_NAME, JEV_MODEL, status, ms, input_tokens)
    return Evaluation(answers=answers, input_tokens=input_tokens, cost_usd=_cost(payload), ms=ms)


def clairic_extract(
    state: str,
    fields: dict,
    *,
    reference_date: str | None = None,
    timeout: float = CLAIRIC_TIMEOUT,
    opener: Callable[..., Any] | None = None,
) -> Extraction:
    """Ask Clairic for ``fields`` in ``state`` in one call, without retries."""
    started = time.monotonic()
    try:
        credentials = cloudflare_credentials()
        if credentials is None:
            raise ModelCallError("no_key")
        account_id, token = credentials
        body: dict[str, Any] = {"model": "clairic", "state": state, "fields": fields}
        if reference_date is not None:
            body["reference_date"] = reference_date
        url = CLAIRIC_URL.format(account_id=account_id)
        status, payload = _send(url, body, key=token, timeout=timeout, opener=opener)
        # The REST API wraps the model output as {"result": {...}, "success": true}; accept it bare too.
        result = payload.get("result", payload)
        if payload.get("success") is False or not isinstance(result, dict):
            raise ModelCallError("bad_response")
        answers = result.get("answers")
        if not isinstance(answers, dict) or not all(isinstance(answer, dict) for answer in answers.values()):
            raise ModelCallError("bad_response")
    except ModelCallError as exc:
        logger.warning("%s: fact extraction failed kind=%s status=%s", PLUGIN_NAME, exc.kind, exc.status)
        raise
    ms = _ms(started)
    input_tokens = _count(result.get("usage"), "input_tokens")
    logger.debug(
        "%s: fact extraction model=%s status=%s ms=%d in=%d", PLUGIN_NAME, CLAIRIC_MODEL, status, ms, input_tokens
    )
    return Extraction(answers=answers, input_tokens=input_tokens, ms=ms)
