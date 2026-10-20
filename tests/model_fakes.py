"""Offline fakes for ``model_client``: an HTTP opener and the Hermes secret scope.

``FakeOpener`` stands in for ``urllib.request.build_opener(...).open``: it records every
request and answers from a queue, raising ``HTTPError`` for a 3xx, 4xx or 5xx status the way
a real opener without redirects does. ``fake_secret_scope`` installs a fake
``agent.secret_scope`` module for the duration of a ``with`` block.
"""

from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from types import ModuleType
from typing import Any, Self

__all__ = ["FakeOpener", "FakeResponse", "fake_secret_scope"]

_MISSING = object()


class FakeResponse:
    """A 2xx answer: ``read(n)``, ``status``, ``getcode()``, ``close()`` and the context manager."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.closed = False
        self._body = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _ErrorBody(io.BytesIO):
    """An error body that remembers whether anyone read it."""

    was_read = False

    def read(self, size: int | None = -1) -> bytes:
        self.was_read = True
        return super().read(size)


class FakeOpener:
    """Call it like an opener's ``open``: ``opener(request, timeout=...)``.

    Each queued response is ``(status, body_bytes)`` or an exception instance, which is raised.
    ``calls`` holds one dict per request: ``method``, ``url``, ``headers``,
    ``unredirected_headers``, ``body`` (decoded JSON), ``data`` (the raw bytes) and ``timeout``.
    ``responses`` holds the ``FakeResponse`` objects handed out and ``error_bodies`` the bodies
    of the raised ``HTTPError``s, so a test can check that both were closed or never read.
    """

    def __init__(self, responses: Iterable[tuple[int, bytes] | BaseException] = ()) -> None:
        self.queue: list[tuple[int, bytes] | BaseException] = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.responses: list[FakeResponse] = []
        self.error_bodies: list[_ErrorBody] = []

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> FakeResponse:
        data = request.data
        assert isinstance(data, bytes), "model_client always sends a bytes body"
        self.calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": dict(request.headers),
                "unredirected_headers": dict(request.unredirected_hdrs),
                "body": json.loads(data),
                "data": data,
                "timeout": timeout,
            }
        )
        if not self.queue:
            raise AssertionError("FakeOpener: no response queued")
        response = self.queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        status, body = response
        if status >= 300:
            error_body = _ErrorBody(body)
            self.error_bodies.append(error_body)
            raise urllib.error.HTTPError(request.full_url, status, "fake", email.message.Message(), error_body)
        answer = FakeResponse(status, body)
        self.responses.append(answer)
        return answer


@contextmanager
def fake_secret_scope(secrets: dict[str, str] | None = None, *, unscoped: bool = False) -> Iterator[ModuleType]:
    """Install a fake ``agent`` package with a ``secret_scope`` module; remove it afterwards.

    The yielded module has ``get_secret(name, default=None)``, which reads the mutable dict
    ``module.secrets`` and records each name in ``module.reads``. While ``module.unscoped`` is
    true it raises ``module.UnscopedSecretError`` instead, like a multiplexing gateway with no
    profile scope.
    """
    package = ModuleType("agent")
    package.__path__ = []
    module = ModuleType("agent.secret_scope")

    class UnscopedSecretError(RuntimeError):
        pass

    def get_secret(name: str, default: str | None = None) -> str | None:
        module.reads.append(name)
        if module.unscoped:
            raise UnscopedSecretError(name)
        return module.secrets.get(name, default)

    module.UnscopedSecretError = UnscopedSecretError
    module.get_secret = get_secret
    module.secrets = dict(secrets or {})
    module.reads = []
    module.unscoped = unscoped
    package.secret_scope = module

    saved = {name: sys.modules.get(name, _MISSING) for name in ("agent", "agent.secret_scope")}
    sys.modules["agent"] = package
    sys.modules["agent.secret_scope"] = module
    try:
        yield module
    finally:
        for name, previous in saved.items():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
