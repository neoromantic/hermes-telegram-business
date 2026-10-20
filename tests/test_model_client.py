"""model_client: the Jev message check and Clairic fact extraction over a fake opener.

Every test is offline: a socket guard fails any real connection, and the Hermes secret
scope is either blocked (an ImportError) or the fake from ``model_fakes``. The one test of
the default opener plugs a fake https handler into the real urllib redirect machinery.
"""

from __future__ import annotations

import email.message
import http.client
import importlib
import importlib.util
import io
import json
import logging
import socket
import sys
import urllib.error
import urllib.request
import urllib.response
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from model_fakes import FakeOpener, FakeResponse, fake_secret_scope

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "__init__.py"

GATEWAY_KEY = "fake-gateway-key"
CF_TOKEN = "fake-cf-token"
ACCOUNT_ID = "0f" * 16
GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/evaluate"
CLAIRIC_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/cloudflare/clairic"
STATE = {
    "current_dossier": "**Mira Tanaka.** Design lead at Northwind Studio.",
    "new_messages": [
        {"ref": "m1", "at": "2026-10-19 09:15", "from": "contact", "text": "Мой новый номер +1 555 555-0101"},
    ],
}
QUESTIONS = {
    "update": {"type": "boolean", "instructions": "Does `new_messages` change `current_dossier`?"},
    "promise": {"type": "boolean", "instructions": "Does any message make or change a promise?"},
}
CLAIRIC_STATE = "2026-10-19 09:15 contact: Созвонимся 23 октября, мой номер +1 555 555-0102"
FIELDS = {
    "dates": {"type": "list", "items": "date", "instructions": "Dates of meetings."},
    "contacts": {"type": "list", "items": "text", "instructions": "Phone numbers."},
}
CLAIRIC_ANSWERS = {
    "dates": {"type": "list", "value": ["2026-10-23"], "p_null": 0.01, "confidence": 0.97},
    "contacts": {"type": "list", "value": ["+1 555 555-0102"], "p_null": 0.02, "confidence": 0.95},
}


def _json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()


def jev_reply(update: object = 0.9, promise: object = 0.2, **extra: object) -> tuple[int, bytes]:
    answers = {
        "update": {"type": "boolean", "probability": update},
        "promise": {"type": "boolean", "probability": promise},
    }
    return 200, _json({"model": "typesafe-ai/jev-1.13.0", "answers": answers, **extra})


@pytest.fixture(scope="module")
def mc() -> Iterator[ModuleType]:
    """``model_client`` as a submodule of a fresh plugin package.

    The package ``__init__`` is not run: model_client imports nothing from the plugin.
    """
    name = f"telesec_model_client_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_FILE)
    assert spec is not None and spec.submodule_search_locations
    sys.modules[name] = importlib.util.module_from_spec(spec)
    try:
        yield importlib.import_module(f"{name}.model_client")
    finally:
        sys.modules.pop(f"{name}.model_client", None)
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any real connection or DNS lookup, and block the real Hermes secret scope."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access in an offline test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # importing it raises ImportError
    monkeypatch.delenv("AI_GATEWAY_BASE_URL", raising=False)


@pytest.fixture
def scope(mc: ModuleType) -> Iterator[ModuleType]:
    """A fake secret scope that holds the gateway key and the Cloudflare credentials."""
    values = {mc.KEY_NAME: GATEWAY_KEY, mc.CF_ACCOUNT_NAME: ACCOUNT_ID, mc.CF_TOKEN_NAME: CF_TOKEN}
    with fake_secret_scope(values) as module:
        yield module


def _fails(mc: ModuleType, kind: str, call, *, status: int | None = None):
    """Run ``call``, expect ``ModelCallError(kind, status)`` with no chained error on show."""
    with pytest.raises(mc.ModelCallError) as caught:
        call()
    exc = caught.value
    assert (exc.kind, exc.status) == (kind, status)
    assert str(exc) == (kind if status is None else f"{kind} {status}")
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__
    return exc


# ---------------------------------------------------------------------------
# Secrets and the base URL
# ---------------------------------------------------------------------------


def test_gateway_key_reads_the_profile_secret(mc, scope):
    assert mc.gateway_key() == GATEWAY_KEY
    scope.secrets[mc.KEY_NAME] = f"  {GATEWAY_KEY}\n"
    assert mc.gateway_key() == GATEWAY_KEY
    assert scope.reads == [mc.KEY_NAME, mc.KEY_NAME]


@pytest.mark.parametrize("value", [None, "", "   ", "two words", "line\nbreak", "ключ"])
def test_gateway_key_is_none_without_a_usable_secret(mc, scope, value):
    if value is None:
        del scope.secrets[mc.KEY_NAME]
    else:
        scope.secrets[mc.KEY_NAME] = value
    assert mc.gateway_key() is None


def test_gateway_key_never_reads_the_process_environment(mc, monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", GATEWAY_KEY)
    assert mc.gateway_key() is None  # ImportError: no Hermes secret scope
    with fake_secret_scope({}):
        assert mc.gateway_key() is None


def test_gateway_key_is_none_when_the_secret_scope_is_missing(mc):
    with fake_secret_scope({mc.KEY_NAME: GATEWAY_KEY}, unscoped=True) as scope:
        assert mc.gateway_key() is None
        scope.unscoped = False
        assert mc.gateway_key() == GATEWAY_KEY


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "https://ai-gateway.vercel.sh"),
        ("", "https://ai-gateway.vercel.sh"),
        ("https://gw.example.com", "https://gw.example.com"),
        ("https://gw.example.com/", "https://gw.example.com"),
        ("https://gw.example.com/v1", "https://gw.example.com"),
        ("  https://gw.example.com/v1/  ", "https://gw.example.com"),
        ("https://gw.example.com:8443/proxy/v1", "https://gw.example.com:8443/proxy"),
    ],
)
def test_gateway_base(mc, value, expected):
    with fake_secret_scope({} if value is None else {mc.BASE_ENV: value}):
        assert mc.gateway_base() == expected


def test_gateway_base_reads_the_profile_secret_scope_not_the_process_environment(mc, monkeypatch):
    # Under a multiplexing gateway os.environ holds the launch profile's URL; the key must go to this profile's.
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://launch-profile.example.com/v1")
    with fake_secret_scope({mc.BASE_ENV: "https://routed-profile.example.com/v1"}) as scope:
        assert mc.gateway_base() == "https://routed-profile.example.com"
        assert scope.reads == [mc.BASE_ENV]
        del scope.secrets[mc.BASE_ENV]
        assert mc.gateway_base() == "https://ai-gateway.vercel.sh"
        scope.unscoped = True
        assert mc.gateway_base() == "https://ai-gateway.vercel.sh"
    assert mc.gateway_base() == "https://ai-gateway.vercel.sh"  # ImportError: no Hermes secret scope


@pytest.mark.parametrize(
    "value",
    [
        "http://gw.example.com",
        "gw.example.com",
        "ftp://gw.example.com",
        "https://",
        "https:///v1",
        "https://user:secret@gw.example.com",
        "https://gw.example.com?route=x",
        "https://gw.example.com/#top",
        "https://gw.example.com:99999",
        "https://gw.example.com:0",
        "https://gw example.com",
        "https://gw.example.com/\tv1",
        "https://шлюз.example.com",
    ],
)
def test_gateway_base_is_none_unless_https_with_a_host(mc, value):
    with fake_secret_scope({mc.BASE_ENV: value}):
        assert mc.gateway_base() is None


# ---------------------------------------------------------------------------
# post_json
# ---------------------------------------------------------------------------


def test_post_json_sends_json_with_an_unredirected_bearer(mc):
    opener = FakeOpener([(200, b'{"ok": true}')])
    body = {"text": "Привет", "n": 1}
    assert mc.post_json("https://api.example.com/run", body, key="k-1", timeout=3.5, opener=opener) == {"ok": True}
    call = opener.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.example.com/run"
    assert call["body"] == body
    assert "Привет".encode() in call["data"] and b"\\u" not in call["data"]
    assert call["headers"] == {"Content-type": "application/json", "User-agent": "telesec"}
    assert call["unredirected_headers"] == {"Authorization": "Bearer k-1"}
    assert call["timeout"] == 3.5
    assert opener.responses[0].closed


@pytest.mark.parametrize(
    ("url", "key", "kind"),
    [
        ("http://api.example.com/run", "k-1", "bad_base_url"),
        ("https://api.example.com/run?x=1", "k-1", "bad_base_url"),
        ("https://api.example.com/run", "", "no_key"),
        ("https://api.example.com/run", "two words", "no_key"),
        ("https://api.example.com/run", "k-1\r\nX-Injected: 1", "no_key"),
    ],
)
def test_post_json_checks_url_and_key_before_any_io(mc, url, key, kind):
    opener = FakeOpener()
    _fails(mc, kind, lambda: mc.post_json(url, {}, key=key, timeout=1, opener=opener))
    assert opener.calls == []


def test_post_json_too_large_counts_bytes_before_any_io(mc):
    opener = FakeOpener([(200, b"{}")])
    ascii_body = {"text": "t" * (mc.MAX_REQUEST_BYTES // 2)}
    cyrillic_body = {"text": "т" * (mc.MAX_REQUEST_BYTES // 2)}  # the same length in characters, twice the bytes
    assert mc.post_json("https://api.example.com/run", ascii_body, key="k-1", timeout=1, opener=opener) == {}
    _fails(
        mc,
        "too_large",
        lambda: mc.post_json("https://api.example.com/run", cyrillic_body, key="k-1", timeout=1, opener=opener),
    )
    assert len(opener.calls) == 1


def test_post_json_rejects_a_non_2xx_answer_that_did_not_raise(mc):
    def opener(request, timeout=None):
        return FakeResponse(302, b"{}")

    _fails(
        mc,
        "http",
        lambda: mc.post_json("https://api.example.com/run", {}, key="k-1", timeout=1, opener=opener),
        status=302,
    )


def test_default_opener_never_follows_a_redirect(mc, monkeypatch):
    seen: list[urllib.request.Request] = []

    class FakeHttps(urllib.request.BaseHandler):
        handler_order = 1  # before the real HTTPSHandler and the proxy handler

        def https_open(self, request):
            seen.append(request)
            headers = email.message.Message()
            if len(seen) == 1:
                headers["Location"] = "https://elsewhere.example.com/collect"
                status, body = 302, b""
            else:
                status, body = 200, b'{"followed": true}'
            response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, status)
            response.msg = "fake"
            return response

    build_opener = urllib.request.build_opener

    # The control: a stock opener follows the redirect and drops only the unredirected header.
    request = urllib.request.Request("https://gw.example.com/v1/evaluate", b"{}", method="POST")
    request.add_unredirected_header("Authorization", "Bearer k-1")
    assert build_opener(FakeHttps).open(request, timeout=1).read() == b'{"followed": true}'
    assert [r.full_url for r in seen] == ["https://gw.example.com/v1/evaluate", "https://elsewhere.example.com/collect"]
    assert not seen[1].has_header("Authorization")

    seen.clear()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: build_opener(*handlers, FakeHttps))
    _fails(mc, "http", lambda: mc.post_json("https://gw.example.com/v1/evaluate", {}, key="k-1", timeout=1), status=302)
    assert [r.full_url for r in seen] == ["https://gw.example.com/v1/evaluate"]
    assert seen[0].unredirected_hdrs["Authorization"] == "Bearer k-1"
    assert "Authorization" not in seen[0].headers


@pytest.mark.parametrize("status", [301, 302, 401, 429, 500])
def test_post_json_http_errors_keep_only_the_status(mc, status):
    opener = FakeOpener([(status, _json({"error": f"bad key {GATEWAY_KEY}"}))])
    exc = _fails(
        mc, "http", lambda: mc.post_json(GATEWAY_URL, {}, key=GATEWAY_KEY, timeout=1, opener=opener), status=status
    )
    assert not opener.error_bodies[0].was_read
    assert GATEWAY_KEY not in repr(exc) and "vercel" not in repr(exc)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError(), "timeout"),  # socket.timeout is the same class
        (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
        (urllib.error.URLError("no route to gw.example.com"), "network"),
        (urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")), "network"),
        (ConnectionResetError(), "network"),
        (http.client.RemoteDisconnected("closed"), "network"),
        (http.client.IncompleteRead(b"{"), "network"),
    ],
)
def test_post_json_transport_errors(mc, error, kind):
    opener = FakeOpener([error])
    exc = _fails(mc, kind, lambda: mc.post_json(GATEWAY_URL, {}, key=GATEWAY_KEY, timeout=1, opener=opener))
    assert "gw.example.com" not in repr(exc)


def test_post_json_read_timeout(mc):
    class SlowResponse(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            raise TimeoutError("The read operation timed out")

    response = SlowResponse(200, b"")
    _fails(
        mc, "timeout", lambda: mc.post_json(GATEWAY_URL, {}, key="k-1", timeout=1, opener=lambda r, timeout: response)
    )
    assert response.closed


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"', b"", b"\xff\xfe\x00", b"[" * 100_000])
def test_post_json_needs_a_json_object(mc, body):
    opener = FakeOpener([(200, body)])
    _fails(mc, "bad_response", lambda: mc.post_json(GATEWAY_URL, {}, key="k-1", timeout=1, opener=opener))


def test_post_json_reads_at_most_one_mib(mc):
    empty = len(_json({"pad": ""}))
    exact = _json({"pad": "x" * (mc.MAX_RESPONSE_BYTES - empty)})
    over = _json({"pad": "x" * (mc.MAX_RESPONSE_BYTES - empty + 1)})
    assert len(exact) == mc.MAX_RESPONSE_BYTES
    opener = FakeOpener([(200, exact), (200, over)])
    assert len(mc.post_json(GATEWAY_URL, {}, key="k-1", timeout=1, opener=opener)["pad"]) == len(exact) - empty
    _fails(mc, "bad_response", lambda: mc.post_json(GATEWAY_URL, {}, key="k-1", timeout=1, opener=opener))


# ---------------------------------------------------------------------------
# jev_evaluate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "url"),
    [
        (None, GATEWAY_URL),
        ("https://gw.example.com", "https://gw.example.com/v1/evaluate"),
        ("https://gw.example.com/v1/", "https://gw.example.com/v1/evaluate"),
    ],
)
def test_jev_url_with_and_without_v1(mc, scope, base, url):
    if base is not None:
        scope.secrets[mc.BASE_ENV] = base
    opener = FakeOpener([jev_reply()])
    mc.jev_evaluate(STATE, QUESTIONS, opener=opener)
    assert opener.calls[0]["url"] == url


def test_jev_request_is_pinned_zdr_typesafe_only(mc, scope):
    opener = FakeOpener([jev_reply()])
    mc.jev_evaluate(STATE, QUESTIONS, opener=opener)
    call = opener.calls[0]
    assert call["body"] == {
        "model": "typesafe-ai/jev-1.13.0",
        "state": STATE,
        "questions": QUESTIONS,
        "providerOptions": {"gateway": {"zeroDataRetention": True, "only": ["typesafe-ai"]}},
    }
    assert "Мой новый номер".encode() in call["data"] and b"\\u" not in call["data"]
    assert "Authorization" not in call["headers"]
    assert call["unredirected_headers"] == {"Authorization": f"Bearer {GATEWAY_KEY}"}
    assert call["headers"]["User-agent"] == "telesec"
    assert call["timeout"] == mc.GATE_TIMEOUT == 10.0


def test_jev_answers_usage_and_cost(mc, scope):
    cost = {"providerMetadata": {"gateway": {"cost": "0.000067452"}}}
    opener = FakeOpener([jev_reply(0.99, 0, usage={"inputTokens": 1606, "outputTokens": 12}, **cost)])
    result = mc.jev_evaluate(STATE, QUESTIONS, opener=opener, timeout=2.5)
    assert result.answers == {"update": 0.99, "promise": 0.0}
    assert isinstance(result.answers["promise"], float)
    assert result.input_tokens == 1606
    assert result.cost_usd == pytest.approx(0.000067452)
    assert result.ms >= 0
    assert opener.calls[0]["timeout"] == 2.5


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"usage": {"inputTokens": "12"}, "providerMetadata": {"gateway": {"cost": "free"}}},
        {"usage": {"inputTokens": True}, "providerMetadata": {"gateway": {"cost": "NaN"}}},
        {"usage": [], "providerMetadata": {"gateway": []}},
        {"usage": {"inputTokens": -5}, "providerMetadata": {"gateway": {"cost": "-0.1"}}},
    ],
)
def test_jev_usage_and_cost_are_optional(mc, scope, extra):
    result = mc.jev_evaluate(STATE, QUESTIONS, opener=FakeOpener([jev_reply(**extra)]))
    assert (result.input_tokens, result.cost_usd) == (0, None)


def test_jev_without_a_key_makes_no_call(mc):
    opener = FakeOpener([jev_reply()])
    _fails(mc, "no_key", lambda: mc.jev_evaluate(STATE, QUESTIONS, opener=opener))  # ImportError
    with fake_secret_scope({}):
        _fails(mc, "no_key", lambda: mc.jev_evaluate(STATE, QUESTIONS, opener=opener))
    with fake_secret_scope({mc.KEY_NAME: GATEWAY_KEY}, unscoped=True):
        _fails(mc, "no_key", lambda: mc.jev_evaluate(STATE, QUESTIONS, opener=opener))
    assert opener.calls == []


@pytest.mark.parametrize("base", ["http://gw.example.com", "gw.example.com/v1", "https://"])
def test_jev_with_a_bad_base_url_makes_no_call(mc, scope, base):
    scope.secrets[mc.BASE_ENV] = base
    opener = FakeOpener([jev_reply()])
    _fails(mc, "bad_base_url", lambda: mc.jev_evaluate(STATE, QUESTIONS, opener=opener))
    assert opener.calls == []


def test_jev_state_over_the_request_cap_makes_no_call(mc, scope):
    opener = FakeOpener([jev_reply()])
    state = {"current_dossier": "", "new_messages": [{"text": "т" * (mc.MAX_REQUEST_BYTES // 2)}]}
    _fails(mc, "too_large", lambda: mc.jev_evaluate(state, QUESTIONS, opener=opener))
    assert opener.calls == []


@pytest.mark.parametrize("status", [401, 429, 500])
def test_jev_http_errors(mc, scope, status):
    _fails(mc, "http", lambda: mc.jev_evaluate(STATE, QUESTIONS, opener=FakeOpener([(status, b"{}")])), status=status)


@pytest.mark.parametrize(
    "reply",
    [
        (200, b'{"answers": {"update": {"probability": 0.5}}}'),  # promise is missing
        (200, b'{"answers": {"update": {"probability": 0.5}, "promise": {"type": "boolean"}}}'),
        (200, b'{"answers": {"update": {"probability": 0.5}, "promise": 0.5}}'),
        (200, b'{"answers": []}'),
        (200, b"{}"),
        jev_reply(1.5),
        jev_reply(-0.01),
        jev_reply("0.5"),
        jev_reply(True),
        jev_reply(None),
        (200, b'{"answers": {"update": {"probability": NaN}, "promise": {"probability": 0.1}}}'),
        (200, b'{"answers": {"update": {"probability": Infinity}, "promise": {"probability": 0.1}}}'),
        (200, b"not json"),
        (200, b"x" * (1_048_576 + 1)),
    ],
)
def test_jev_bad_responses(mc, scope, reply):
    _fails(mc, "bad_response", lambda: mc.jev_evaluate(STATE, QUESTIONS, opener=FakeOpener([reply])))


def test_jev_logs_and_errors_carry_no_key_state_questions_or_url(mc, scope, caplog):
    caplog.set_level(logging.DEBUG)
    scope.secrets[mc.BASE_ENV] = "https://gw.example.com/v1"
    opener = FakeOpener(
        [
            jev_reply(usage={"inputTokens": 1606}),
            (500, _json({"error": f"upstream echoed {GATEWAY_KEY}"})),
            jev_reply(1.5),
            urllib.error.URLError(TimeoutError("timed out to gw.example.com")),
        ]
    )
    errors = []
    mc.jev_evaluate(STATE, QUESTIONS, opener=opener)
    for _ in range(3):
        with pytest.raises(mc.ModelCallError) as caught:
            mc.jev_evaluate(STATE, QUESTIONS, opener=opener)
        errors.append(caught.value)

    text = caplog.text + "".join(f"{exc!s} {exc!r}" for exc in errors)
    for secret in (GATEWAY_KEY, "Bearer", "Mira", "Мой новый номер", "Does `new_messages`", "gw.example.com", "/v1"):
        assert secret not in text
    assert all(record.exc_info is None for record in caplog.records)
    assert "model check model=typesafe-ai/jev-1.13.0 status=200" in caplog.text
    assert "in=1606" in caplog.text
    for kind, status in (("http", "500"), ("bad_response", "None"), ("timeout", "None")):
        assert f"model check failed kind={kind} status={status}" in caplog.text


# ---------------------------------------------------------------------------
# Cloudflare Clairic
# ---------------------------------------------------------------------------


def test_cloudflare_credentials(mc, scope):
    assert mc.cloudflare_credentials() == (ACCOUNT_ID, CF_TOKEN)
    scope.secrets[mc.CF_ACCOUNT_NAME] = f" {ACCOUNT_ID}\n"
    assert mc.cloudflare_credentials() == (ACCOUNT_ID, CF_TOKEN)


@pytest.mark.parametrize(
    "account_id",
    [
        "0F" * 16,
        "0f" * 15,
        "0f" * 17,
        "evil.example.com",
        "0f" * 16 + "/../../zones",
        "0f" * 8 + "@evil.example.com",
        "0f" * 8 + " " + "0f" * 8,
        "",
        None,
    ],
)
def test_cloudflare_credentials_need_a_32_hex_account_id(mc, scope, account_id):
    if account_id is None:
        del scope.secrets[mc.CF_ACCOUNT_NAME]
    else:
        scope.secrets[mc.CF_ACCOUNT_NAME] = account_id
    assert mc.cloudflare_credentials() is None


def test_cloudflare_credentials_need_a_token_and_the_secret_scope(mc, scope):
    del scope.secrets[mc.CF_TOKEN_NAME]
    assert mc.cloudflare_credentials() is None
    scope.secrets[mc.CF_TOKEN_NAME] = CF_TOKEN
    scope.unscoped = True
    assert mc.cloudflare_credentials() is None


def test_cloudflare_credentials_never_read_the_process_environment(mc, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", CF_TOKEN)
    assert mc.cloudflare_credentials() is None


@pytest.mark.parametrize("account_id", [None, "evil.example.com/x", "0F" * 16])
def test_clairic_without_credentials_makes_no_call(mc, scope, account_id):
    if account_id is None:
        del scope.secrets[mc.CF_TOKEN_NAME]
    else:
        scope.secrets[mc.CF_ACCOUNT_NAME] = account_id
    opener = FakeOpener([(200, _json({"answers": CLAIRIC_ANSWERS}))])
    _fails(mc, "no_key", lambda: mc.clairic_extract(CLAIRIC_STATE, FIELDS, opener=opener))
    assert opener.calls == []


def test_clairic_request(mc, scope):
    opener = FakeOpener([(200, _json({"answers": CLAIRIC_ANSWERS}))] * 2)
    mc.clairic_extract(CLAIRIC_STATE, FIELDS, reference_date="2026-10-20", opener=opener)
    mc.clairic_extract(CLAIRIC_STATE, FIELDS, opener=opener, timeout=4.0)
    first, second = opener.calls
    assert first["url"] == CLAIRIC_URL
    assert first["body"] == {
        "model": "clairic",
        "state": CLAIRIC_STATE,
        "fields": FIELDS,
        "reference_date": "2026-10-20",
    }
    assert second["body"] == {"model": "clairic", "state": CLAIRIC_STATE, "fields": FIELDS}
    assert "Созвонимся".encode() in first["data"] and b"\\u" not in first["data"]
    assert "Authorization" not in first["headers"]
    assert first["unredirected_headers"] == {"Authorization": f"Bearer {CF_TOKEN}"}
    assert (first["timeout"], second["timeout"]) == (mc.CLAIRIC_TIMEOUT, 4.0)
    assert mc.CLAIRIC_TIMEOUT == 10.0


@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"answers": CLAIRIC_ANSWERS, "usage": {"input_tokens": 655}}, "success": True, "errors": []},
        {"model": "clairic", "answers": CLAIRIC_ANSWERS, "usage": {"input_tokens": 655, "output_tokens": 0}},
    ],
)
def test_clairic_accepts_the_envelope_or_a_bare_answer(mc, scope, payload):
    result = mc.clairic_extract(CLAIRIC_STATE, FIELDS, opener=FakeOpener([(200, _json(payload))]))
    assert result.answers == CLAIRIC_ANSWERS
    assert result.input_tokens == 655
    assert result.ms >= 0


def test_clairic_usage_is_optional(mc, scope):
    result = mc.clairic_extract(CLAIRIC_STATE, FIELDS, opener=FakeOpener([(200, _json({"answers": {}}))]))
    assert (result.answers, result.input_tokens) == ({}, 0)


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "result": None, "errors": [{"code": 5007, "message": "No such model"}]},
        {"success": False, "result": {"answers": CLAIRIC_ANSWERS}},
        {"success": True, "result": None},
        {"result": "answers"},
        {"result": {"answers": []}},
        {"result": {"answers": {"dates": ["2026-10-23"]}}},
        {"answers": {"dates": None}},
        {"answers": None},
        {"usage": {"input_tokens": 655}},
    ],
)
def test_clairic_bad_responses(mc, scope, payload):
    _fails(
        mc,
        "bad_response",
        lambda: mc.clairic_extract(CLAIRIC_STATE, FIELDS, opener=FakeOpener([(200, _json(payload))])),
    )


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_clairic_http_errors(mc, scope, status):
    opener = FakeOpener([(status, _json({"success": False, "errors": [{"code": 10000}]}))])
    _fails(mc, "http", lambda: mc.clairic_extract(CLAIRIC_STATE, FIELDS, opener=opener), status=status)
    assert not opener.error_bodies[0].was_read


def test_clairic_logs_and_errors_carry_no_token_account_state_or_url(mc, scope, caplog):
    caplog.set_level(logging.DEBUG)
    opener = FakeOpener(
        [
            (200, _json({"result": {"answers": CLAIRIC_ANSWERS, "usage": {"input_tokens": 655}}, "success": True})),
            (401, _json({"errors": [{"message": f"bad token {CF_TOKEN}"}]})),
            (200, _json({"success": False, "errors": [{"message": "account " + ACCOUNT_ID}]})),
            TimeoutError(),
        ]
    )
    errors = []
    mc.clairic_extract(CLAIRIC_STATE, FIELDS, opener=opener)
    for _ in range(3):
        with pytest.raises(mc.ModelCallError) as caught:
            mc.clairic_extract(CLAIRIC_STATE, FIELDS, opener=opener)
        errors.append(caught.value)

    text = caplog.text + "".join(f"{exc!s} {exc!r}" for exc in errors)
    for secret in (
        CF_TOKEN,
        ACCOUNT_ID,
        "Bearer",
        "Созвонимся",
        "+1 555",
        "2026-10-23",
        "Dates of meetings",
        "cloudflare.com",
    ):
        assert secret not in text
    assert all(record.exc_info is None for record in caplog.records)
    assert "fact extraction model=@cf/cloudflare/clairic status=200" in caplog.text
    assert "in=655" in caplog.text
    for kind, status in (("http", "401"), ("bad_response", "None"), ("timeout", "None")):
        assert f"fact extraction failed kind={kind} status={status}" in caplog.text
