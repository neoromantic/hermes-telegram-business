"""Shared fixtures for the dossier tests.

Plain helpers live in ``dossier_helpers.py``. Nothing here is autouse and no fixture
reuses a name from an existing test module, so existing tests resolve exactly the
fixtures they did before.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import dossier_helpers
import pytest
import test_history

DOSSIER_ENV_PREFIX = "TELESEC_DOSSIER_"
CLEANUP_ENV = (
    "TELESEC_CLEANUP_PROVIDER",
    "TELESEC_CLEANUP_MODEL",
    "TELESEC_CLEANUP_TIMEOUT",
)


def _reset_history_caches() -> None:
    # Same sweep as test_history.plugin: every loaded plugin copy keeps its own caches and timers.
    for loaded in list(sys.modules.values()):
        history_support = getattr(loaded, "_history_support", None)
        if history_support is not None and hasattr(history_support, "reset_in_memory_caches"):
            history_support.reset_in_memory_caches()


@pytest.fixture
def dossier_plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ModuleType]:
    """A fresh plugin load with HERMES_HOME at ``tmp_path`` (like ``test_history.plugin``)."""
    module = test_history._load_plugin_module(monkeypatch, tmp_path)
    _reset_history_caches()
    yield module
    # A test that started a worker without the ``dossiers`` fixture must not leak its thread.
    dossier_support = sys.modules.get(f"{module.__name__}.dossier_support")
    if dossier_support is not None and hasattr(dossier_support, "stop_workers"):
        dossier_support.stop_workers(timeout=2)
    _reset_history_caches()


@pytest.fixture
def dossier_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Clear every dossier env var and the cleanup provider/model/timeout vars.

    Returns ``set_env(**values)``: ``set_env(BACKFILL_MESSAGES=10)`` sets
    ``TELESEC_DOSSIER_<NAME>``; a value of None unsets it. The on/off switch is stored:
    use ``dossier_support.set_enabled``.
    """
    for name in list(os.environ):
        if name.startswith(DOSSIER_ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    for name in CLEANUP_ENV:
        monkeypatch.delenv(name, raising=False)

    def set_env(**values: Any) -> None:
        for suffix, value in values.items():
            name = DOSSIER_ENV_PREFIX + suffix.upper()
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, str(value))

    return set_env


@pytest.fixture
def dossier_history(
    monkeypatch: pytest.MonkeyPatch,
    dossier_plugin: ModuleType,
    dossier_env: Callable[..., None],
) -> ModuleType:
    """The plugin with history capture on (like ``test_history.enabled_history``) and dossiers off."""
    test_history.set_switch(dossier_plugin, "history", True)
    monkeypatch.setenv("TELESEC_HISTORY_CORRECTION_WINDOW", "10")
    dossier_plugin._history_support.reset_in_memory_caches()
    return dossier_plugin


@pytest.fixture
def dossiers(
    monkeypatch: pytest.MonkeyPatch,
    dossier_history: ModuleType,
    dossier_env: Callable[..., None],
) -> Iterator[ModuleType]:
    """History on, dossiers switched on (stored switch, via the CLI); yields this plugin's ``dossier_support``.

    ``INITIAL_DELAY_SECONDS`` is 3600, so a started worker never runs a cycle unless the
    test lowers it. Teardown stops workers, checks that no worker thread survived, and
    unbinds the LLM facade.
    """
    ds = dossier_helpers.dossier_module(dossier_history)
    ds.set_enabled(True, via="cli")
    monkeypatch.setattr(ds, "INITIAL_DELAY_SECONDS", 3600)
    yield ds
    ds.stop_workers(timeout=2)
    leaked = dossier_helpers.live_worker_threads()
    ds.bind_llm(None)
    assert not leaked, f"dossier worker threads still alive after stop_workers(timeout=2): {leaked}"
