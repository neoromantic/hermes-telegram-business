from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import hashlib
import io
import importlib.util
import json
import os
import stat
import sys
import threading
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = ROOT / "__init__.py"


def _load_plugin_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)

    module_name = f"telegram_business_history_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _load_plugin_module(monkeypatch, tmp_path)
    for loaded in list(sys.modules.values()):
        history_support = getattr(loaded, "_history_support", None)
        if history_support is not None and hasattr(history_support, "reset_in_memory_caches"):
            history_support.reset_in_memory_caches()
    yield module
    for loaded in list(sys.modules.values()):
        history_support = getattr(loaded, "_history_support", None)
        if history_support is not None and hasattr(history_support, "reset_in_memory_caches"):
            history_support.reset_in_memory_caches()


@pytest.fixture
def enabled_history(monkeypatch: pytest.MonkeyPatch, plugin):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW", "10")
    plugin._history_support.reset_in_memory_caches()
    return plugin


class FakeBot:
    def __init__(self, *, owner_id: int = 1000):
        self.owner_id = owner_id
        self.lookup_error: Exception | None = None
        self.calls: list[str] = []

    async def get_business_connection(self, business_connection_id: str):
        self.calls.append(business_connection_id)
        if self.lookup_error is not None:
            raise self.lookup_error
        return SimpleNamespace(user=SimpleNamespace(id=self.owner_id))


def make_business_text_update(
    *,
    text: str | None = "hello",
    caption: str | None = None,
    business_id: str = "business-123",
    chat_id: int = 991,
    chat_type: str | None = "private",
    chat_title: str | None = None,
    chat_username: str | None = "customer991",
    chat_first_name: str | None = "Casey",
    chat_last_name: str | None = "Customer",
    message_id: int = 77,
    from_user_id: int | None = 2000,
    from_user_is_bot: bool | None = False,
    from_user_username: str | None = "casey_customer",
    from_user_first_name: str | None = "Casey",
    from_user_last_name: str | None = "Customer",
    from_user_language_code: str | None = "en",
    update_id: int = 42,
    date: datetime | None = None,
    reply_to_message_id: int | None = None,
    edited: bool = False,
    sender_business_bot_id: int | None = None,
):
    payload = SimpleNamespace(
        business_connection_id=business_id,
        chat=SimpleNamespace(
            id=chat_id,
            type=chat_type,
            title=chat_title,
            username=chat_username,
            first_name=chat_first_name,
            last_name=chat_last_name,
        ),
        message_id=message_id,
        from_user=None
        if from_user_id is None
        else SimpleNamespace(
            id=from_user_id,
            is_bot=from_user_is_bot,
            username=from_user_username,
            first_name=from_user_first_name,
            last_name=from_user_last_name,
            language_code=from_user_language_code,
        ),
        date=date or datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
        text=text,
        caption=caption,
        reply_to_message=None
        if reply_to_message_id is None
        else SimpleNamespace(message_id=reply_to_message_id),
        sender_business_bot=None
        if sender_business_bot_id is None
        else SimpleNamespace(id=sender_business_bot_id),
        api_kwargs={},
    )
    return SimpleNamespace(
        update_id=update_id,
        business_message=None if edited else payload,
        edited_business_message=payload if edited else None,
        deleted_business_messages=None,
    )


def make_business_media_update(
    *,
    caption: str = "caption-only",
    business_id: str = "business-123",
    chat_id: int = 991,
    chat_type: str | None = "private",
    message_id: int = 77,
    update_id: int = 42,
    date: datetime | None = None,
):
    payload = SimpleNamespace(
        business_connection_id=business_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type, first_name="Casey", last_name="Customer", username="customer991"),
        message_id=message_id,
        from_user=SimpleNamespace(
            id=2000,
            is_bot=False,
            username="casey_customer",
            first_name="Casey",
            last_name="Customer",
            language_code="en",
        ),
        date=date or datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
        text=None,
        caption=caption,
        voice=SimpleNamespace(file_id="voice-1"),
        reply_to_message=None,
        sender_business_bot=None,
        api_kwargs={},
    )
    return SimpleNamespace(
        update_id=update_id,
        business_message=payload,
        edited_business_message=None,
        deleted_business_messages=None,
    )


def make_deleted_update(
    *,
    business_id: str = "business-123",
    chat_id: int = 991,
    chat_type: str | None = "private",
    message_ids: tuple[int, ...] = (77,),
    update_id: int = 99,
):
    payload = SimpleNamespace(
        business_connection_id=business_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type, first_name="Casey", last_name="Customer", username="customer991"),
        message_ids=list(message_ids),
        api_kwargs={},
    )
    return SimpleNamespace(
        update_id=update_id,
        business_message=None,
        edited_business_message=None,
        deleted_business_messages=payload,
    )


def load_records(plugin, *, business_id: str = "business-123", chat_id: int = 991):
    history = plugin._history_support
    chat_dir = history._history_chat_dir_path(business_id, chat_id)
    if not chat_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in history._iter_history_files(chat_dir):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    records.append(json.loads(line))
    return records


def load_history_file(plugin, *, business_id: str = "business-123", chat_id: int = 991, month: str = "2026-07.jsonl") -> Path:
    return plugin._history_support._history_chat_dir_path(business_id, chat_id) / month


def load_catalog(plugin) -> dict[str, Any]:
    path = plugin._history_support.history_root() / "contacts.json"
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_dirty_path(plugin) -> Path:
    return plugin._history_support.history_root() / ".contacts.json.dirty"


def snapshot_history_tree(plugin) -> dict[str, bytes]:
    root = plugin._history_support.history_root()
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def snapshot_history_state(plugin) -> dict[str, tuple[str, int, bytes | None]]:
    root = plugin._history_support.history_root()
    if not root.exists():
        return {}
    state: dict[str, tuple[str, int, bytes | None]] = {
        ".": ("dir", stat.S_IMODE(root.stat().st_mode), None),
    }
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            state[key] = ("dir", mode, None)
        else:
            state[key] = ("file", mode, path.read_bytes())
    return state


def remove_history_locks(plugin) -> None:
    history = plugin._history_support
    root_lock = history._root_lock_path()
    if root_lock.exists():
        root_lock.unlink()
    for chat_dir in history._iter_chat_dirs():
        chat_lock = history._chat_lock_path(chat_dir)
        if chat_lock.exists():
            chat_lock.unlink()


def make_legacy_history_record(
    plugin,
    *,
    event_type: str,
    source: str,
    observed_at: datetime,
    telegram_update_id: int,
    business_connection_id: str = "business-123",
    chat_id: int = 991,
    message_id: int = 77,
    message_at: datetime | None = None,
    sender_id: int | None = 2000,
    direction: str,
    reply_to_message_id: int | None = None,
    text: str | None = None,
    deleted_event_id: str | None = None,
    classification: str | None = None,
    replacement_message_id: int | None = None,
    classification_reason: str | None = None,
    classification_score: float | None = None,
    evaluated_at: datetime | None = None,
    deleted_observed_at: datetime | None = None,
):
    history = plugin._history_support
    record = {
        "schema_version": history.SCHEMA_VERSION,
        "event_type": event_type,
        "source": source,
        "observed_at": history._isoformat_utc(observed_at),
        "telegram_update_id": telegram_update_id,
        "business_connection_id": str(business_connection_id),
        "chat_id": chat_id,
        "message_id": message_id,
        "message_at": history._isoformat_utc(message_at),
        "sender_id": sender_id,
        "direction": direction,
        "reply_to_message_id": reply_to_message_id,
    }
    if text is not None:
        record["text"] = text
    if deleted_event_id is not None:
        record["deleted_event_id"] = deleted_event_id
    if classification is not None:
        record["classification"] = classification
    if replacement_message_id is not None:
        record["replacement_message_id"] = replacement_message_id
    if classification_reason is not None:
        record["classification_reason"] = classification_reason
        record["classification_method"] = classification_reason
    if classification_score is not None:
        record["classification_score"] = round(float(classification_score), 4)
    if evaluated_at is not None:
        record["evaluated_at"] = history._isoformat_utc(evaluated_at)
    if deleted_observed_at is not None:
        record["deleted_observed_at"] = history._isoformat_utc(deleted_observed_at)
    payload = json.dumps(
        history._record_signature(record),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    record["event_id"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return record


def append_raw_history_records(plugin, *records: dict[str, Any]) -> None:
    history = plugin._history_support
    initialized: set[Path] = set()
    for record in records:
        observed_at = history._parse_datetime(record.get("observed_at"))
        assert observed_at is not None
        chat_dir = history._history_chat_dir_path(record["business_connection_id"], record["chat_id"])
        if chat_dir not in initialized:
            history._ensure_history_chat_dir(chat_dir)
            history._ensure_private_file(history._root_lock_path())
            history._ensure_private_file(history._chat_lock_path(chat_dir))
            initialized.add(chat_dir)
        history._append_record(chat_dir, record)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync only")
def test_fsync_parent_directory_uses_posix_directory_fsync(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    target = history.history_root() / "contacts.json"
    calls: list[tuple[str, Any, Any | None]] = []

    monkeypatch.setattr(
        history.os,
        "open",
        lambda path, flags: calls.append(("open", path, flags)) or 17,
    )
    monkeypatch.setattr(history.os, "fsync", lambda descriptor: calls.append(("fsync", descriptor, None)))
    monkeypatch.setattr(history.os, "close", lambda descriptor: calls.append(("close", descriptor, None)))

    assert history._fsync_parent_directory(target) is True
    assert calls == [
        ("open", str(target.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)),
        ("fsync", 17, None),
        ("close", 17, None),
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync only")
def test_fsync_parent_directory_ignores_unsupported_directory_fsync(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    target = history.history_root() / "contacts.json"
    closed: list[int] = []

    monkeypatch.setattr(history.os, "open", lambda _path, _flags: 23)

    def _unsupported_fsync(_descriptor: int) -> None:
        raise OSError(errno.EINVAL, "directory fsync unsupported")

    monkeypatch.setattr(history.os, "fsync", _unsupported_fsync)
    monkeypatch.setattr(history.os, "close", lambda descriptor: closed.append(descriptor))

    assert history._fsync_parent_directory(target) is False
    assert closed == [23]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync only")
def test_fsync_parent_directory_surfaces_real_io_failures(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    target = history.history_root() / "contacts.json"
    closed: list[int] = []

    monkeypatch.setattr(history.os, "open", lambda _path, _flags: 29)

    def _broken_fsync(_descriptor: int) -> None:
        raise OSError(errno.EIO, "directory fsync failed")

    monkeypatch.setattr(history.os, "fsync", _broken_fsync)
    monkeypatch.setattr(history.os, "close", lambda descriptor: closed.append(descriptor))

    with pytest.raises(OSError, match="directory fsync failed"):
        history._fsync_parent_directory(target)
    assert closed == [29]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync only")
def test_first_append_fsyncs_new_directories_and_first_month_file_in_order(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    root = history.history_root()
    data_dir = root.parent.parent
    transport_dir = root.parent
    connection_dir = root / history._path_key("business-123")
    chat_dir_path = connection_dir / history._safe_part(991)
    month_path = chat_dir_path / history._month_filename(base)
    parent_fsyncs: list[Path] = []
    original_fsync_parent = history._fsync_parent_directory

    def _record_parent_fsync(path: Path) -> bool:
        parent_fsyncs.append(path)
        return original_fsync_parent(path)

    monkeypatch.setattr(history, "_fsync_parent_directory", _record_parent_fsync)

    chat_dir = history._history_chat_dir("business-123", 991)
    record = history._build_event(
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=1,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        reply_to_message_id=None,
        text="hello",
    )
    history._append_record(chat_dir, record)

    assert chat_dir == chat_dir_path
    assert parent_fsyncs == [data_dir, transport_dir, root, connection_dir, chat_dir_path, month_path]
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(connection_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(chat_dir_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(month_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync only")
def test_subsequent_append_to_existing_month_skips_parent_directory_fsync(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    chat_dir = history._history_chat_dir("business-123", 991)
    first_record = history._build_event(
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=1,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        reply_to_message_id=None,
        text="hello",
    )
    history._append_record(chat_dir, first_record)

    parent_fsyncs: list[Path] = []
    monkeypatch.setattr(history, "_fsync_parent_directory", lambda path: parent_fsyncs.append(path) or True)

    second_record = history._build_event(
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(seconds=1),
        telegram_update_id=2,
        business_connection_id="business-123",
        chat_id=991,
        message_id=78,
        message_at=base + timedelta(seconds=1),
        sender_id=2000,
        direction="inbound",
        reply_to_message_id=None,
        text="again",
    )
    history._append_record(chat_dir, second_record)

    month_path = chat_dir / history._month_filename(base)
    assert parent_fsyncs == []
    assert stat.S_IMODE(month_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync only")
def test_first_append_surfaces_directory_fsync_failures(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    chat_dir = history._history_chat_dir("business-123", 991)
    month_path = chat_dir / history._month_filename(base)

    def _broken_parent_fsync(path: Path) -> bool:
        if path == month_path:
            raise OSError(errno.EIO, "directory fsync failed")
        return True

    monkeypatch.setattr(history, "_fsync_parent_directory", _broken_parent_fsync)

    record = history._build_event(
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=1,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        reply_to_message_id=None,
        text="hello",
    )

    with pytest.raises(OSError, match="directory fsync failed"):
        history._append_record(chat_dir, record)


class TimerHarness:
    class FakeTimer:
        def __init__(self, registry: list["TimerHarness.FakeTimer"], interval: float, function, args=None, kwargs=None):
            self._registry = registry
            self.interval = interval
            self.function = function
            self.args = tuple(args or ())
            self.kwargs = dict(kwargs or {})
            self.daemon = False
            self.cancelled = False
            self.started = False
            self.fired = False
            self._registry.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled and not self.fired

        def fire(self):
            if self.cancelled or self.fired:
                return
            self.fired = True
            self.function(*self.args, **self.kwargs)

    def __init__(self):
        self.timers: list[TimerHarness.FakeTimer] = []

    def timer(self, interval: float, function, args=None, kwargs=None):
        return self.FakeTimer(self.timers, interval, function, args=args, kwargs=kwargs)


@pytest.mark.asyncio
async def test_history_is_fail_closed_when_disabled_or_missing_chat_type(plugin, monkeypatch: pytest.MonkeyPatch):
    update = make_business_text_update(chat_type=None)

    await plugin._history_support.observe_ptb_update(update, bot=FakeBot())
    assert not load_records(plugin, chat_id=991)

    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    await plugin._history_support.observe_ptb_update(update, bot=FakeBot())
    assert not load_records(plugin, chat_id=991)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "expected_exit", "expected_output"),
    [
        (("history", "catalog"), 0, "status=missing "),
        (("history", "chats"), 0, "No Telegram Business history found."),
        (("history", "contacts"), 0, "No matching contacts found."),
        (("history", "stats"), 0, "chat_count=0"),
        (("history", "verify"), 0, "ok=True chats=0 files=0 records=0"),
        (("history", "show", "--chat", "991", "--limit", "1"), 1, "no matching history chat found"),
        (("history", "search", "--text", "hello", "--limit", "5"), 0, "No matching history records found."),
        (("history", "export", "--chat", "991", "--format", "text", "--limit", "1"), 1, "no matching history chat found"),
        (("history", "deletions", "--chat", "991", "--status", "pending", "--limit", "1"), 1, "no matching history chat found"),
    ],
)
async def test_absent_history_read_paths_leave_tree_absent(
    enabled_history,
    argv: tuple[str, ...],
    expected_exit: int,
    expected_output: str,
):
    plugin = enabled_history

    assert snapshot_history_state(plugin) == {}

    exit_code, output = _run_history_cli(plugin, *argv)

    assert exit_code == expected_exit
    assert expected_output in output
    assert snapshot_history_state(plugin) == {}
    assert not plugin._history_support.history_root().exists()


@pytest.mark.asyncio
async def test_absent_history_unknown_type_probe_rejects_without_side_effects(enabled_history):
    plugin = enabled_history
    before = snapshot_history_state(plugin)

    wrote = await plugin._history_support.observe_ptb_update(
        make_business_text_update(chat_type=None, text="unknown type probe"),
        bot=FakeBot(),
    )

    assert wrote is False
    assert before == {}
    assert snapshot_history_state(plugin) == before
    assert not plugin._history_support.history_root().exists()


@pytest.mark.asyncio
async def test_enable_only_private_capture_spans_multiple_connections_and_chats(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(business_id="business-123", chat_id=991, text="alpha"),
        bot=FakeBot(),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(business_id="business-456", chat_id=992, text="beta"),
        bot=FakeBot(),
    )

    assert [record["text"] for record in load_records(plugin, business_id="business-123", chat_id=991)] == ["alpha"]
    assert [record["text"] for record in load_records(plugin, business_id="business-456", chat_id=992)] == ["beta"]


@pytest.mark.asyncio
async def test_connection_filter_still_narrows_capture(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", "business-123")

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(business_id="business-123", chat_id=991, text="kept"),
        bot=FakeBot(),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(business_id="business-456", chat_id=992, text="dropped"),
        bot=FakeBot(),
    )

    assert [record["text"] for record in load_records(plugin, business_id="business-123", chat_id=991)] == ["kept"]
    assert not load_records(plugin, business_id="business-456", chat_id=992)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_history_skips_non_private_chat_types_by_default(plugin, monkeypatch: pytest.MonkeyPatch, chat_type: str):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")

    wrote = await plugin._history_support.observe_ptb_update(
        make_business_text_update(chat_type=chat_type, chat_title=f"{chat_type} room", chat_first_name=None, chat_last_name=None),
        bot=FakeBot(),
    )

    assert wrote is False
    assert not load_records(plugin, chat_id=991)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_history_chat_type_opt_in_allows_additional_chat_types(
    plugin,
    monkeypatch: pytest.MonkeyPatch,
    chat_type: str,
):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHAT_TYPES", f"private,{chat_type}")

    wrote = await plugin._history_support.observe_ptb_update(
        make_business_text_update(chat_type=chat_type, chat_title=f"{chat_type} room", chat_first_name=None, chat_last_name=None),
        bot=FakeBot(),
    )

    assert wrote is True
    assert load_records(plugin)[0]["chat_profile"]["type"] == chat_type


@pytest.mark.asyncio
async def test_exact_chat_id_opt_in_allows_group_chat_even_without_chat_type_opt_in(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS", "991")

    wrote = await plugin._history_support.observe_ptb_update(
        make_business_text_update(chat_type="group", chat_title="support room", chat_first_name=None, chat_last_name=None),
        bot=FakeBot(),
    )

    assert wrote is True
    assert load_records(plugin)[0]["chat_profile"]["type"] == "group"


@pytest.mark.asyncio
async def test_delete_updates_follow_same_chat_boundary_and_known_type_fallback(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")

    skipped = await plugin._history_support.observe_ptb_update(
        make_deleted_update(chat_type="group", message_ids=(77,), update_id=1),
        bot=FakeBot(),
    )
    assert skipped is False

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(chat_type="private", message_id=77, update_id=2, text="hello"),
        bot=FakeBot(),
    )
    wrote = await plugin._history_support.observe_ptb_update(
        make_deleted_update(chat_type=None, message_ids=(77,), update_id=3),
        bot=FakeBot(),
    )

    assert wrote is True
    assert [record["event_type"] for record in load_records(plugin)] == ["message.created", "message.deleted"]


def test_history_config_defaults_and_env_overrides(plugin, monkeypatch: pytest.MonkeyPatch):
    config = plugin._history_support.history_config_from_env()
    assert config.connections is None
    assert config.chats is None
    assert config.chat_types == {"private"}
    assert config.nearby_before_seconds == 15
    assert config.retention_days == 0
    assert config.max_bytes == 1024 * 1024 * 1024

    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_NEARBY_BEFORE_SECONDS", "7")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHAT_TYPES", "private,group")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS", "0")
    config = plugin._history_support.history_config_from_env()
    assert config.chat_types == {"group", "private"}
    assert config.nearby_before_seconds == 7
    assert config.retention_days == 0


@pytest.mark.asyncio
async def test_caption_only_media_update_creates_no_history_file(enabled_history):
    plugin = enabled_history

    wrote = await plugin._history_support.observe_ptb_update(
        make_business_media_update(),
        bot=FakeBot(),
    )

    assert wrote is False
    assert not plugin._history_support.history_root().exists()
    assert not load_records(plugin, chat_id=991)


@pytest.mark.asyncio
async def test_raw_history_records_create_edit_delete_without_needing_gateway_dispatch(enabled_history, tmp_path: Path, monkeypatch):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    bot = FakeBot()

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="hello", message_id=77, update_id=1, date=base),
        bot=bot,
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="hello again", message_id=77, update_id=2, date=base, edited=True),
        bot=bot,
        now=base + timedelta(seconds=1),
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=3),
        bot=bot,
        now=base + timedelta(seconds=2),
    )

    records = load_records(plugin)
    assert [record["event_type"] for record in records] == [
        "message.created",
        "message.edited",
        "message.deleted",
    ]

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="hello", message_id=77, update_id=1, date=base),
        bot=bot,
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="hello again", message_id=77, update_id=2, date=base, edited=True),
        bot=bot,
        now=base + timedelta(seconds=1),
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=3),
        bot=bot,
        now=base + timedelta(seconds=2),
    )
    assert len(load_records(plugin)) == 3

    reloaded = _load_plugin_module(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", "business-123")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS", "991")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW", "10")
    await reloaded._history_support.observe_ptb_update(
        make_business_text_update(text="hello", message_id=77, update_id=1, date=base),
        bot=bot,
        now=base,
    )
    await reloaded._history_support.observe_ptb_update(
        make_business_text_update(text="hello again", message_id=77, update_id=2, date=base, edited=True),
        bot=bot,
        now=base + timedelta(seconds=1),
    )
    await reloaded._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=3),
        bot=bot,
        now=base + timedelta(seconds=2),
    )
    assert len(load_records(reloaded, business_id="business-123", chat_id=991)) == 3


@pytest.mark.asyncio
async def test_history_records_store_canonical_source_for_all_event_types(enabled_history):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    bot = FakeBot()

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="hello", message_id=77, update_id=1, date=base),
        bot=bot,
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="hello again", message_id=77, update_id=2, date=base, edited=True),
        bot=bot,
        now=base + timedelta(seconds=1),
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=3),
        bot=bot,
        now=base + timedelta(seconds=2),
    )
    result = plugin._history_support.maintain_history(now=base + timedelta(seconds=20))
    records = load_records(plugin)

    assert result.classified == 1
    assert {record["event_type"] for record in records} == {
        "message.created",
        "message.edited",
        "message.deleted",
        "deletion.classified",
    }
    assert {record["event_type"]: record["source"] for record in records} == {
        "message.created": "business_message",
        "message.edited": "edited_business_message",
        "message.deleted": "deleted_business_messages",
        "deletion.classified": "deleted_business_messages",
    }


@pytest.mark.asyncio
async def test_create_and_edit_records_store_profile_snapshots_and_catalog_aliases(enabled_history):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            text="hello",
            update_id=1,
            date=base,
            chat_username="alice-old",
            chat_first_name="Alice",
            chat_last_name="Smith",
            from_user_username="alice_sender_old",
            from_user_first_name="Alice",
            from_user_last_name="Smith",
        ),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            text="hello again",
            update_id=2,
            date=base,
            edited=True,
            chat_username="alice-new",
            chat_first_name="Alicia",
            chat_last_name="Smith",
            from_user_username="alice_sender_new",
            from_user_first_name="Alicia",
            from_user_last_name="Smith",
        ),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    records = load_records(plugin)
    assert records[0]["chat_profile"] == {
        "id": 991,
        "type": "private",
        "username": "alice-old",
        "first_name": "Alice",
        "last_name": "Smith",
    }
    assert records[0]["sender_profile"] == {
        "id": 2000,
        "is_bot": False,
        "username": "alice_sender_old",
        "first_name": "Alice",
        "last_name": "Smith",
        "language_code": "en",
    }
    catalog = load_catalog(plugin)
    entry = catalog["contacts"][0]
    assert entry["current_profile"]["username"] == "alice-new"
    assert entry["current_profile"]["first_name"] == "Alicia"
    assert "@alice-old" in entry["aliases"]
    assert "Alice Smith" in entry["aliases"]
    assert "@alice-new" not in entry["aliases"]


@pytest.mark.asyncio
async def test_catalog_update_failure_does_not_block_canonical_append(enabled_history, monkeypatch: pytest.MonkeyPatch, caplog):
    plugin = enabled_history
    monkeypatch.setattr(
        plugin._history_support,
        "_refresh_contact_catalog_locked",
        lambda _records, *, base_source_signature=None: (_ for _ in ()).throw(RuntimeError("catalog broke")),
    )

    wrote = await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="still written", update_id=1),
        bot=FakeBot(),
    )

    assert wrote is True
    assert load_records(plugin)[0]["text"] == "still written"
    assert catalog_dirty_path(plugin).exists()
    assert "history contact catalog update failed" in caplog.text
    assert "still written" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "corrupt", "dirty", "stale"])
async def test_catalog_status_is_read_only_and_contacts_fail_closed_without_mutation(
    enabled_history,
    state: str,
):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="first contact", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    history.rebuild_contact_catalog()

    if state == "missing":
        history._catalog_path().unlink()
    elif state == "corrupt":
        history._catalog_path().write_text("{broken", encoding="utf-8")
    elif state == "dirty":
        history._mark_catalog_dirty("forced dirty state")
    else:
        append_raw_history_records(
            plugin,
            make_legacy_history_record(
                plugin,
                event_type="message.created",
                source="business_message",
                observed_at=base + timedelta(seconds=1),
                telegram_update_id=2,
                business_connection_id="business-123",
                chat_id=991,
                message_id=78,
                message_at=base + timedelta(seconds=1),
                sender_id=2000,
                direction="incoming",
                text="stale append",
            ),
        )

    before = snapshot_history_state(plugin)

    exit_code, output = _run_history_cli(plugin, "history", "catalog")

    assert exit_code == 0
    assert output.startswith(f"status={state} ")
    assert "history catalog --rebuild" in output
    assert snapshot_history_state(plugin) == before

    exit_code, output = _run_history_cli(plugin, "history", "contacts")

    assert exit_code == 1
    assert f"history contact catalog is {state}" in output
    assert "history catalog --rebuild" in output
    assert snapshot_history_state(plugin) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ("history", "chats"),
        ("history", "contacts"),
        ("history", "show", "--chat", "991", "--limit", "1"),
        ("history", "search", "--text", "first", "--limit", "5"),
        ("history", "export", "--chat", "991", "--format", "text", "--limit", "1"),
        ("history", "deletions", "--chat", "991", "--status", "pending", "--limit", "1"),
    ],
)
async def test_catalog_backed_cli_reads_fail_closed_on_stale_catalog_without_mutation(
    enabled_history,
    argv: tuple[str, ...],
):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="first contact", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    history.rebuild_contact_catalog()
    append_raw_history_records(
        plugin,
        make_legacy_history_record(
            plugin,
            event_type="message.created",
            source="business_message",
            observed_at=base + timedelta(seconds=1),
            telegram_update_id=2,
            business_connection_id="business-123",
            chat_id=991,
            message_id=78,
            message_at=base + timedelta(seconds=1),
            sender_id=2000,
            direction="incoming",
            text="stale append",
        ),
    )
    before = snapshot_history_state(plugin)

    exit_code, output = _run_history_cli(plugin, *argv)

    assert exit_code == 1
    assert "history contact catalog is stale" in output
    assert "history catalog --rebuild" in output
    assert snapshot_history_state(plugin) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ("history", "catalog"),
        ("history", "stats"),
        ("history", "verify"),
    ],
)
async def test_missing_root_lock_fails_closed_without_mutation(
    enabled_history,
    argv: tuple[str, ...],
):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="root lock archive", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    root_lock = history._root_lock_path()
    root_lock.unlink()
    before = snapshot_history_state(plugin)

    exit_code, output = _run_history_cli(plugin, *argv)

    assert exit_code == 1
    assert "history root lock is uninitialized" in output
    assert snapshot_history_state(plugin) == before
    assert not root_lock.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ("history", "catalog"),
        ("history", "show", "--chat", "991", "--limit", "1"),
        ("history", "verify"),
    ],
)
async def test_missing_chat_lock_fails_closed_without_mutation(
    enabled_history,
    argv: tuple[str, ...],
):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="chat lock archive", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    chat_dir = history._history_chat_dir_path("business-123", 991)
    chat_lock = history._chat_lock_path(chat_dir)
    chat_lock.unlink()
    before = snapshot_history_state(plugin)

    exit_code, output = _run_history_cli(plugin, *argv)

    assert exit_code == 1
    assert "history chat lock is uninitialized" in output
    assert snapshot_history_state(plugin) == before
    assert not chat_lock.exists()


def test_catalog_rebuild_supports_old_jsonl_without_profile_snapshots(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    append_raw_history_records(
        plugin,
        make_legacy_history_record(
            plugin,
            event_type="message.created",
            source="business_message",
            observed_at=datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
            telegram_update_id=1,
            direction="incoming",
            text="legacy",
        ),
    )

    catalog = plugin._history_support.rebuild_contact_catalog()

    assert catalog["contact_count"] == 1
    entry = catalog["contacts"][0]
    assert entry["business_connection_id"] == "business-123"
    assert entry["chat_id"] == 991
    assert entry["current_profile"] is None
    assert entry["message_count"] == 1


@pytest.mark.asyncio
async def test_catalog_missing_and_corrupt_can_be_rebuilt_explicitly_without_touching_canonical_history(
    enabled_history,
):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="hello", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    catalog_path = history.history_root() / "contacts.json"
    canonical_before = snapshot_history_tree(plugin)
    catalog_path.unlink()

    exit_code, status = _run_history_cli(plugin, "history", "catalog")

    assert exit_code == 0
    assert status.startswith("status=missing ")
    assert snapshot_history_tree(plugin) == {k: v for k, v in canonical_before.items() if k != "contacts.json"}

    exit_code, rebuilt = _run_history_cli(plugin, "history", "catalog", "--rebuild")

    assert exit_code == 0
    assert rebuilt.startswith("status=rebuilt ")
    assert catalog_path.exists()
    assert {
        path: data for path, data in snapshot_history_tree(plugin).items() if not path.endswith("contacts.json")
    } == {
        path: data for path, data in canonical_before.items() if not path.endswith("contacts.json")
    }

    catalog_path.write_text("{broken", encoding="utf-8")
    before_corrupt_rebuild = snapshot_history_tree(plugin)

    exit_code, corrupt = _run_history_cli(plugin, "history", "catalog")

    assert exit_code == 0
    assert corrupt.startswith("status=corrupt ")
    assert snapshot_history_tree(plugin) == before_corrupt_rebuild

    exit_code, rebuilt = _run_history_cli(plugin, "history", "catalog", "--rebuild")

    assert exit_code == 0
    assert rebuilt.startswith("status=rebuilt ")
    assert {
        path: data for path, data in snapshot_history_tree(plugin).items() if not path.endswith("contacts.json")
    } == {
        path: data for path, data in before_corrupt_rebuild.items() if not path.endswith("contacts.json")
    }


@pytest.mark.asyncio
async def test_catalog_rebuild_updates_only_derived_state(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="first contact", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    history.rebuild_contact_catalog()
    append_raw_history_records(
        plugin,
        make_legacy_history_record(
            plugin,
            event_type="message.created",
            source="business_message",
            observed_at=base + timedelta(seconds=1),
            telegram_update_id=2,
            business_connection_id="business-123",
            chat_id=991,
            message_id=78,
            message_at=base + timedelta(seconds=1),
            sender_id=2000,
            direction="incoming",
            text="second contact",
        ),
    )
    history._mark_catalog_dirty("force rebuild path")
    before = snapshot_history_tree(plugin)

    exit_code, output = _run_history_cli(plugin, "history", "catalog", "--rebuild")

    assert exit_code == 0
    assert output.startswith("status=rebuilt ")
    after = snapshot_history_tree(plugin)
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    assert changed <= {"contacts.json", ".contacts.json.dirty"}
    assert ".contacts.json.dirty" not in after
    assert load_catalog(plugin)["contacts"][0]["message_count"] == 2


@pytest.mark.asyncio
async def test_catalog_rebuild_fails_on_torn_tail_without_mutation_then_succeeds_after_verify_repair(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="hello", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    history_file = load_history_file(plugin)
    with history_file.open("ab") as handle:
        handle.write(b"{\"event_id\":\"broken\"")
    before = snapshot_history_tree(plugin)

    exit_code, output = _run_history_cli(plugin, "history", "catalog", "--rebuild")

    assert exit_code == 1
    assert "verify --repair-tails" in output
    assert "history catalog --rebuild" in output
    assert snapshot_history_tree(plugin) == before

    exit_code, output = _run_history_cli(plugin, "history", "verify", "--repair-tails")

    assert exit_code == 0
    assert "ok=True" in output
    after_verify = snapshot_history_tree(plugin)

    exit_code, output = _run_history_cli(plugin, "history", "catalog", "--rebuild")

    assert exit_code == 0
    assert output.startswith("status=rebuilt ")
    after_rebuild = snapshot_history_tree(plugin)
    changed = {path for path in set(after_verify) | set(after_rebuild) if after_verify.get(path) != after_rebuild.get(path)}
    assert changed <= {"contacts.json", ".contacts.json.dirty"}
    assert history_file.read_bytes().endswith(b"\n")
    assert load_catalog(plugin)["contact_count"] == 1


@pytest.mark.asyncio
async def test_catalog_rebuild_reinitializes_missing_locks_and_restores_normal_reads(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="rebuild init", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    chat_dir = history._history_chat_dir_path("business-123", 991)
    remove_history_locks(plugin)

    catalog = history.rebuild_contact_catalog()

    assert catalog["contact_count"] == 1
    assert history._root_lock_path().exists()
    assert history._chat_lock_path(chat_dir).exists()
    exit_code, output = _run_history_cli(plugin, "history", "contacts")
    assert exit_code == 0
    assert "chat=991" in output


@pytest.mark.asyncio
async def test_capture_write_reinitializes_missing_locks_and_restores_normal_reads(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="before reinit", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    chat_dir = history._history_chat_dir_path("business-123", 991)
    remove_history_locks(plugin)

    wrote = await history.observe_ptb_update(
        make_business_text_update(text="after reinit", message_id=78, update_id=2, date=base + timedelta(seconds=1)),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    assert wrote is True
    assert history._root_lock_path().exists()
    assert history._chat_lock_path(chat_dir).exists()
    assert [record["message_id"] for record in load_records(plugin)] == [77, 78]
    exit_code, output = _run_history_cli(plugin, "history", "show", "--chat", "991", "--limit", "2")
    assert exit_code == 0
    assert "after reinit" in output


@pytest.mark.asyncio
async def test_verify_repair_reinitializes_missing_locks_and_restores_normal_verify(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await history.observe_ptb_update(
        make_business_text_update(text="verify init", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    chat_dir = history._history_chat_dir_path("business-123", 991)
    remove_history_locks(plugin)

    repaired = history.verify_history(repair_tails=True)
    verified = history.verify_history()

    assert repaired.ok is True
    assert verified.ok is True
    assert history._root_lock_path().exists()
    assert history._chat_lock_path(chat_dir).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "expected_exit", "expected_output"),
    [
        (["history", "catalog"], 0, "status=stale "),
        (["history", "catalog", "--rebuild"], 1, "error: canonical history changed during catalog rebuild"),
        (
            ["history", "contacts"],
            1,
            "error: history contact catalog is stale; run 'hermes telegram-business history catalog --rebuild'",
        ),
        (
            ["history", "show", "--contact", "@customer991", "--limit", "1"],
            1,
            "error: history contact catalog is stale; run 'hermes telegram-business history catalog --rebuild'",
        ),
        (
            ["history", "search", "--contact", "@customer991", "--text", "hello", "--limit", "1"],
            1,
            "error: history contact catalog is stale; run 'hermes telegram-business history catalog --rebuild'",
        ),
        (
            ["history", "export", "--contact", "@customer991", "--format", "text", "--limit", "1"],
            1,
            "error: history contact catalog is stale; run 'hermes telegram-business history catalog --rebuild'",
        ),
        (
            ["history", "search", "--text", "hello", "--limit", "1"],
            1,
            "error: history contact catalog is stale; run 'hermes telegram-business history catalog --rebuild'",
        ),
    ],
)
async def test_catalog_rebuild_exhaustion_returns_cli_error(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_exit: int,
    expected_output: str,
):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="hello", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    plugin._history_support.rebuild_contact_catalog()

    calls = 0

    def _changing_signature(*_args, **_kwargs) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "sha256": f"{calls:064x}",
            "file_count": 1,
            "total_bytes": 1,
        }

    monkeypatch.setattr(plugin._history_support, "_history_source_signature", _changing_signature)
    monkeypatch.setattr(plugin._history_support, "_history_source_signature_locked", _changing_signature)

    exit_code, output = _run_history_cli(plugin, *argv)

    assert exit_code == expected_exit
    assert output.startswith(expected_output)


def test_catalog_concurrent_refresh_preserves_all_entries(plugin):
    history = plugin._history_support
    history.rebuild_contact_catalog()
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    record_one = history._build_event(
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=1,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        reply_to_message_id=None,
        text="one",
        chat_profile={"id": 991, "type": "private", "username": "one", "first_name": "One"},
        sender_profile={"id": 2000, "username": "one", "first_name": "One"},
    )
    record_two = history._build_event(
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(seconds=1),
        telegram_update_id=2,
        business_connection_id="business-456",
        chat_id=992,
        message_id=78,
        message_at=base + timedelta(seconds=1),
        sender_id=2001,
        direction="inbound",
        reply_to_message_id=None,
        text="two",
        chat_profile={"id": 992, "type": "private", "username": "two", "first_name": "Two"},
        sender_profile={"id": 2001, "username": "two", "first_name": "Two"},
    )

    barrier = threading.Barrier(2)

    def _refresh(record):
        barrier.wait()
        history._refresh_contact_catalog([record])

    first = threading.Thread(target=_refresh, args=(record_one,))
    second = threading.Thread(target=_refresh, args=(record_two,))
    first.start()
    second.start()
    first.join()
    second.join()

    catalog = load_catalog(plugin)
    assert {(entry["business_connection_id"], entry["chat_id"]) for entry in catalog["contacts"]} == {
        ("business-123", 991),
        ("business-456", 992),
    }


def test_capture_root_lock_serializes_canonical_append_and_catalog_publish(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    history = plugin._history_support
    history.rebuild_contact_catalog()
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    update_one = make_business_text_update(
        business_id="business-123",
        chat_id=991,
        chat_username="serial-one",
        from_user_username="serial-one",
        text="one",
        update_id=1,
        date=base,
    )
    update_two = make_business_text_update(
        business_id="business-456",
        chat_id=992,
        chat_username="serial-two",
        from_user_username="serial-two",
        text="two",
        update_id=2,
        date=base + timedelta(seconds=1),
    )
    original_refresh = history._refresh_contact_catalog_locked
    original_append = history._append_record
    first_refresh_waiting = threading.Event()
    release_first_refresh = threading.Event()
    second_append_started = threading.Event()
    cli_done = threading.Event()
    results: dict[str, Any] = {}
    blocked_first_refresh = False

    def _blocking_refresh(records, *, base_source_signature=None):
        nonlocal blocked_first_refresh
        if not blocked_first_refresh and {str(record.get("chat_id")) for record in records} == {"991"}:
            blocked_first_refresh = True
            first_refresh_waiting.set()
            assert release_first_refresh.wait(timeout=5)
        return original_refresh(records, base_source_signature=base_source_signature)

    def _tracking_append(chat_dir: Path, record: dict[str, Any]) -> bool:
        if str(record.get("chat_id")) == "992" and record.get("event_type") == "message.created":
            second_append_started.set()
        return original_append(chat_dir, record)

    monkeypatch.setattr(history, "_refresh_contact_catalog_locked", _blocking_refresh)
    monkeypatch.setattr(history, "_append_record", _tracking_append)

    def _capture(label: str, update: Any, when: datetime) -> None:
        try:
            results[label] = asyncio.run(history.observe_ptb_update(update, bot=FakeBot(), now=when))
        except Exception as exc:  # noqa: BLE001 - asserted below
            results[f"{label}_exc"] = exc

    def _contacts_cli() -> None:
        try:
            results["cli_exit"], results["cli_output"] = _run_history_cli(plugin, "history", "contacts")
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["cli_exc"] = exc
        finally:
            cli_done.set()

    first_thread = threading.Thread(target=_capture, args=("first", update_one, base))
    second_thread = threading.Thread(target=_capture, args=("second", update_two, base + timedelta(seconds=1)))
    cli_thread = threading.Thread(target=_contacts_cli)
    first_thread.start()
    assert first_refresh_waiting.wait(timeout=5)

    second_thread.start()
    cli_thread.start()

    assert second_append_started.wait(timeout=0.1) is False
    assert cli_done.wait(timeout=0.1) is False

    second_chat_dir = history._history_chat_dir_path("business-456", 992)
    assert not second_chat_dir.exists() or not list(second_chat_dir.glob("*.jsonl"))

    release_first_refresh.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    cli_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not cli_thread.is_alive()
    assert "first_exc" not in results
    assert "second_exc" not in results
    assert "cli_exc" not in results
    assert results["first"] is True
    assert results["second"] is True
    assert results["cli_exit"] == 0

    catalog = load_catalog(plugin)
    assert catalog["source_signature"] == history._history_source_signature()
    assert {(entry["business_connection_id"], entry["chat_id"]) for entry in catalog["contacts"]} == {
        ("business-123", 991),
        ("business-456", 992),
    }

    exit_code, output = _run_history_cli(plugin, "history", "contacts", "--limit", "5")
    assert exit_code == 0
    assert "chat=991" in output
    assert "chat=992" in output


@pytest.mark.asyncio
async def test_numeric_chat_lookup_without_connection_checks_canonical_duplicates_when_catalog_is_stale(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            business_id="business-123",
            chat_id=991,
            chat_username="duplicate-one",
            from_user_username="duplicate-one",
            text="first duplicate",
            update_id=1,
            date=base,
        ),
        bot=FakeBot(),
        now=base,
    )

    def _fail_refresh(_records, *, base_source_signature=None):
        raise RuntimeError("catalog broke")

    def _fail_dirty(_reason: str):
        raise OSError("dirty broke")

    monkeypatch.setattr(plugin._history_support, "_refresh_contact_catalog_locked", _fail_refresh)
    monkeypatch.setattr(plugin._history_support, "_mark_catalog_dirty_locked", _fail_dirty)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            business_id="business-456",
            chat_id=991,
            chat_username="duplicate-two",
            from_user_username="duplicate-two",
            text="second duplicate",
            update_id=2,
            date=base + timedelta(seconds=1),
        ),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    assert not catalog_dirty_path(plugin).exists()
    assert load_catalog(plugin)["contact_count"] == 1

    plugin._history_support.reset_in_memory_caches()
    reloaded = _load_plugin_module(monkeypatch, plugin._history_support.history_root().parents[2])
    parser = _build_history_parser(reloaded)

    exit_code = reloaded._history_support.handle_cli(
        parser.parse_args(["history", "show", "--chat", "991", "--limit", "1"])
    )
    ambiguous = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert "history contact catalog is stale" in ambiguous
    assert "history catalog --rebuild" in ambiguous

    exit_code, rebuilt = _run_history_cli(reloaded, "history", "catalog", "--rebuild")

    assert exit_code == 0
    assert rebuilt.startswith("status=rebuilt ")

    exit_code = reloaded._history_support.handle_cli(
        parser.parse_args(["history", "show", "--connection", "business-456", "--chat", "991", "--limit", "1"])
    )
    resolved = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert "connection=business-456" in resolved
    assert "second duplicate" in resolved


@pytest.mark.asyncio
async def test_torn_tail_is_repaired_before_next_append_without_full_read_bytes(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="one", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    history_file = load_history_file(plugin)
    with history_file.open("ab") as handle:
        handle.write(b"{\"schema_version\":1")

    original_read_bytes = Path.read_bytes

    def _guarded_read_bytes(path: Path):
        if path == history_file:
            raise AssertionError("bounded torn-tail repair should not call Path.read_bytes()")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", _guarded_read_bytes)
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="two", message_id=78, update_id=2, date=base),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)

    lines = history_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["message_id"] for line in lines] == [77, 78]
    assert history_file.read_bytes().endswith(b"\n")


@pytest.mark.asyncio
async def test_streamed_show_errors_on_torn_tail_without_mutation(enabled_history, capsys):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="one", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    history_file = load_history_file(plugin)
    with history_file.open("ab") as handle:
        handle.write(b"{\"event_id\":\"broken\"")
    torn_bytes = history_file.read_bytes()

    parser = _build_history_parser(plugin)
    exit_code = plugin._history_support.handle_cli(parser.parse_args(["history", "show", "--chat", "991", "--limit", "5"]))
    output = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert "history contact catalog is stale" in output
    assert "history catalog --rebuild" in output
    assert history_file.read_bytes() == torn_bytes


@pytest.mark.asyncio
async def test_streamed_read_waits_for_concurrent_append_and_never_exposes_partial_json(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await history.observe_ptb_update(
        make_business_text_update(text="one", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    chat_dir = history._history_chat_dir("business-123", 991)
    history_file = load_history_file(plugin)
    appended = history._build_event(
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(seconds=1),
        telegram_update_id=2,
        business_connection_id="business-123",
        chat_id=991,
        message_id=78,
        message_at=base + timedelta(seconds=1),
        sender_id=2000,
        direction="inbound",
        reply_to_message_id=None,
        text="two",
    )
    serialized = json.dumps(appended, ensure_ascii=False, separators=(",", ":")) + "\n"
    midpoint = max(1, len(serialized) // 2)
    writer_started = threading.Event()
    release_writer = threading.Event()
    reader_done = threading.Event()
    reader_result: dict[str, Any] = {}

    def _writer() -> None:
        with history._chat_writer_lock(chat_dir):
            with history_file.open("a", encoding="utf-8") as handle:
                handle.write(serialized[:midpoint])
                handle.flush()
                os.fsync(handle.fileno())
                writer_started.set()
                assert release_writer.wait(timeout=5)
                handle.write(serialized[midpoint:])
                handle.flush()
                os.fsync(handle.fileno())

    def _reader() -> None:
        try:
            with history._streamed_records_locked(chat_dir, since=None, until=None, text_query=None) as records:
                reader_result["records"] = list(records)
        except Exception as exc:  # noqa: BLE001 - assertion surfaces below
            reader_result["exc"] = exc
        finally:
            reader_done.set()

    writer_thread = threading.Thread(target=_writer)
    reader_thread = threading.Thread(target=_reader)
    writer_thread.start()
    assert writer_started.wait(timeout=5)

    reader_thread.start()
    assert reader_done.wait(timeout=0.1) is False

    release_writer.set()
    writer_thread.join(timeout=5)
    reader_thread.join(timeout=5)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert "exc" not in reader_result
    assert [record["message_id"] for record in reader_result["records"]] == [77, 78]
    assert history_file.read_bytes().endswith(b"\n")


@pytest.mark.asyncio
async def test_streamed_read_waits_for_concurrent_prune_and_keeps_selected_partition_available(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    history = plugin._history_support
    old_time = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    current_time = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await history.observe_ptb_update(
        make_business_text_update(text="old month", message_id=77, update_id=1, date=old_time),
        bot=FakeBot(),
        now=old_time,
    )

    chat_dir = history._history_chat_dir("business-123", 991)
    old_file = load_history_file(plugin, month="2026-06.jsonl")
    original_open = Path.open
    reader_selected = threading.Event()
    allow_reader_open = threading.Event()
    prune_done = threading.Event()
    reader_done = threading.Event()
    results: dict[str, Any] = {}

    def _blocking_open(path_obj: Path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path_obj == old_file and "r" in mode:
            reader_selected.set()
            assert allow_reader_open.wait(timeout=5)
        return original_open(path_obj, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _blocking_open)
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS", "10")

    def _reader() -> None:
        try:
            with history._streamed_records_locked(chat_dir, since=None, until=None, text_query=None) as records:
                results["records"] = list(records)
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["reader_exc"] = exc
        finally:
            reader_done.set()

    def _prune() -> None:
        try:
            results["maintenance"] = history.maintain_history(now=current_time)
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["prune_exc"] = exc
        finally:
            prune_done.set()

    reader_thread = threading.Thread(target=_reader)
    prune_thread = threading.Thread(target=_prune)
    reader_thread.start()
    assert reader_selected.wait(timeout=5)

    prune_thread.start()
    assert prune_done.wait(timeout=0.1) is False

    allow_reader_open.set()
    reader_thread.join(timeout=5)
    prune_thread.join(timeout=5)

    assert not reader_thread.is_alive()
    assert not prune_thread.is_alive()
    assert reader_done.is_set()
    assert prune_done.is_set()
    assert "reader_exc" not in results
    assert "prune_exc" not in results
    assert [record["message_id"] for record in results["records"]] == [77]
    assert results["maintenance"].pruned_files == 1
    assert not old_file.exists()


def test_streamed_reader_holding_chat_does_not_deadlock_capture(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    asyncio.run(
        history.observe_ptb_update(
            make_business_text_update(text="one", message_id=77, update_id=1, date=base),
            bot=FakeBot(),
            now=base,
        )
    )

    chat_dir = history._history_chat_dir("business-123", 991)
    history_file = load_history_file(plugin)
    original_open = Path.open
    reader_selected = threading.Event()
    allow_reader_open = threading.Event()
    reader_done = threading.Event()
    writer_done = threading.Event()
    results: dict[str, Any] = {}

    def _blocking_open(path_obj: Path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path_obj == history_file and "r" in mode:
            reader_selected.set()
            assert allow_reader_open.wait(timeout=5)
        return original_open(path_obj, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _blocking_open)

    def _reader() -> None:
        try:
            with history._streamed_records_locked(chat_dir, since=None, until=None, text_query=None) as records:
                results["records"] = list(records)
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["reader_exc"] = exc
        finally:
            reader_done.set()

    def _writer() -> None:
        try:
            results["writer"] = asyncio.run(
                history.observe_ptb_update(
                    make_business_text_update(
                        text="two",
                        message_id=78,
                        update_id=2,
                        date=base + timedelta(seconds=1),
                    ),
                    bot=FakeBot(),
                    now=base + timedelta(seconds=1),
                )
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["writer_exc"] = exc
        finally:
            writer_done.set()

    reader_thread = threading.Thread(target=_reader)
    writer_thread = threading.Thread(target=_writer)
    reader_thread.start()
    assert reader_selected.wait(timeout=5)

    writer_thread.start()
    assert writer_done.wait(timeout=0.1) is False

    allow_reader_open.set()
    reader_thread.join(timeout=5)
    writer_thread.join(timeout=5)

    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    assert reader_done.is_set()
    assert writer_done.is_set()
    assert "reader_exc" not in results
    assert "writer_exc" not in results
    assert results["writer"] is True
    assert [record["message_id"] for record in results["records"]] == [77]
    assert [record["message_id"] for record in load_records(plugin)] == [77, 78]


def test_reinitialized_read_lock_still_serializes_capture_writer(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    asyncio.run(
        history.observe_ptb_update(
            make_business_text_update(text="one", message_id=77, update_id=1, date=base),
            bot=FakeBot(),
            now=base,
        )
    )
    remove_history_locks(plugin)
    history.rebuild_contact_catalog()

    chat_dir = history._history_chat_dir("business-123", 991)
    history_file = load_history_file(plugin)
    original_open = Path.open
    reader_selected = threading.Event()
    allow_reader_open = threading.Event()
    reader_done = threading.Event()
    writer_done = threading.Event()
    results: dict[str, Any] = {}

    def _blocking_open(path_obj: Path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path_obj == history_file and "r" in mode:
            reader_selected.set()
            assert allow_reader_open.wait(timeout=5)
        return original_open(path_obj, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _blocking_open)

    def _reader() -> None:
        try:
            with history._streamed_records_locked(chat_dir, since=None, until=None, text_query=None) as records:
                results["records"] = list(records)
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["reader_exc"] = exc
        finally:
            reader_done.set()

    def _writer() -> None:
        try:
            results["writer"] = asyncio.run(
                history.observe_ptb_update(
                    make_business_text_update(
                        text="two",
                        message_id=78,
                        update_id=2,
                        date=base + timedelta(seconds=1),
                    ),
                    bot=FakeBot(),
                    now=base + timedelta(seconds=1),
                )
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["writer_exc"] = exc
        finally:
            writer_done.set()

    reader_thread = threading.Thread(target=_reader)
    writer_thread = threading.Thread(target=_writer)
    reader_thread.start()
    assert reader_selected.wait(timeout=5)

    writer_thread.start()
    assert writer_done.wait(timeout=0.1) is False

    allow_reader_open.set()
    reader_thread.join(timeout=5)
    writer_thread.join(timeout=5)

    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    assert reader_done.is_set()
    assert writer_done.is_set()
    assert "reader_exc" not in results
    assert "writer_exc" not in results
    assert results["writer"] is True
    assert [record["message_id"] for record in results["records"]] == [77]
    assert [record["message_id"] for record in load_records(plugin)] == [77, 78]


@pytest.mark.asyncio
async def test_second_append_uses_cached_chat_state_without_full_reload(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    original_loader = plugin._history_support._load_chat_state_from_disk
    loads = 0

    def _counting_loader(chat_dir: Path, *, repair_tails: bool):
        nonlocal loads
        loads += 1
        return original_loader(chat_dir, repair_tails=repair_tails)

    monkeypatch.setattr(plugin._history_support, "_load_chat_state_from_disk", _counting_loader)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="one", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    assert loads == 1

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="two", message_id=78, update_id=2, date=base),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )
    assert loads == 1


@pytest.mark.asyncio
async def test_external_file_signature_change_invalidates_cached_chat_state(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    original_loader = plugin._history_support._load_chat_state_from_disk
    loads = 0

    def _counting_loader(chat_dir: Path, *, repair_tails: bool):
        nonlocal loads
        loads += 1
        return original_loader(chat_dir, repair_tails=repair_tails)

    monkeypatch.setattr(plugin._history_support, "_load_chat_state_from_disk", _counting_loader)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="one", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    assert loads == 1

    history_file = load_history_file(plugin)
    external_record = plugin._history_support._build_event(
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(seconds=2),
        telegram_update_id=200,
        business_connection_id="business-123",
        chat_id=991,
        message_id=700,
        message_at=base + timedelta(seconds=2),
        sender_id=999,
        direction="incoming",
        reply_to_message_id=None,
        text="external write",
    )
    with history_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(external_record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    stat_result = history_file.stat()
    os.utime(history_file, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1))

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="three", message_id=78, update_id=2, date=base),
        bot=FakeBot(),
        now=base + timedelta(seconds=3),
    )
    assert loads == 2
    assert [record["message_id"] for record in load_records(plugin)] == [77, 700, 78]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
@pytest.mark.asyncio
async def test_history_permissions_are_private_on_posix(enabled_history):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="permissions", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    root = plugin._history_support.history_root()
    connection_dir = next(path for path in root.iterdir() if path.is_dir())
    chat_dir = next(path for path in connection_dir.iterdir() if path.is_dir())
    history_file = next(path for path in chat_dir.iterdir() if path.suffix == ".jsonl")
    lock_file = chat_dir / ".lock"

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(connection_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(chat_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(history_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_direction_resolution_accepts_sender_business_bot_and_owner_lookup(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", "*")

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="bot", message_id=77, update_id=1, date=base, sender_business_bot_id=9),
        bot=FakeBot(owner_id=1234),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="owner", message_id=78, update_id=2, date=base, from_user_id=1000),
        bot=FakeBot(owner_id=1000),
        now=base + timedelta(seconds=1),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="customer", message_id=80, update_id=4, date=base, from_user_id=2000),
        bot=FakeBot(owner_id=1000),
        now=base + timedelta(seconds=2),
    )
    unknown_bot = FakeBot(owner_id=1000)
    unknown_bot.lookup_error = RuntimeError("lookup failed")
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            text="unknown",
            message_id=79,
            update_id=3,
            date=base,
            from_user_id=1000,
            business_id="business-999",
        ),
        bot=unknown_bot,
        now=base + timedelta(seconds=3),
    )

    directions = {record["message_id"]: record["direction"] for record in load_records(plugin)}
    assert directions[77] == "outbound"
    assert directions[78] == "outbound"
    assert directions[80] == "inbound"
    unknown_records = load_records(plugin, business_id="business-999", chat_id=991)
    assert unknown_records[-1]["direction"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "edited"),
    [
        ("message.created", False),
        ("message.edited", True),
    ],
)
@pytest.mark.parametrize(
    ("legacy_direction", "canonical_direction", "from_user_id", "owner_id"),
    [
        ("incoming", "inbound", 2000, 1000),
        ("outgoing", "outbound", 1000, 1000),
    ],
)
async def test_reload_deduplicates_legacy_message_retries_after_direction_rename(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    edited: bool,
    legacy_direction: str,
    canonical_direction: str,
    from_user_id: int,
    owner_id: int,
):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    source = "edited_business_message" if edited else "business_message"
    legacy_record = make_legacy_history_record(
        plugin,
        event_type=event_type,
        source=source,
        observed_at=base,
        telegram_update_id=42,
        message_at=base,
        sender_id=from_user_id,
        direction=legacy_direction,
        text="legacy retry",
    )
    canonical_record = dict(legacy_record)
    canonical_record["direction"] = canonical_direction
    canonical_event_id = plugin._history_support._event_id(canonical_record)

    assert canonical_event_id != legacy_record["event_id"]

    append_raw_history_records(plugin, legacy_record)
    reloaded = _load_plugin_module(monkeypatch, plugin._history_support.history_root().parents[2])
    chat_dir = reloaded._history_support._history_chat_dir("business-123", 991)
    state, _ = reloaded._history_support._load_chat_state(chat_dir)

    assert legacy_record["event_id"] in state.seen_event_ids
    assert canonical_event_id in state.seen_event_ids
    assert state.messages["77"].direction == canonical_direction

    wrote = await reloaded._history_support.observe_ptb_update(
        make_business_text_update(
            text="legacy retry",
            message_id=77,
            update_id=42,
            date=base,
            from_user_id=from_user_id,
            edited=edited,
        ),
        bot=FakeBot(owner_id=owner_id),
        now=base + timedelta(seconds=30),
    )

    assert wrote is False
    records = load_records(reloaded)
    assert len(records) == 1
    assert records[0]["event_id"] == legacy_record["event_id"]
    assert records[0]["direction"] == legacy_direction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy_direction", "canonical_direction", "from_user_id", "owner_id"),
    [
        ("incoming", "inbound", 2000, 1000),
        ("outgoing", "outbound", 1000, 1000),
    ],
)
async def test_reload_deduplicates_legacy_delete_retry_and_classifies_with_canonical_direction(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
    legacy_direction: str,
    canonical_direction: str,
    from_user_id: int,
    owner_id: int,
):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    created_record = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=41,
        message_at=base,
        sender_id=from_user_id,
        direction=legacy_direction,
        text="legacy delete retry",
    )
    deleted_record = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(seconds=1),
        telegram_update_id=42,
        message_at=base,
        sender_id=from_user_id,
        direction=legacy_direction,
    )

    append_raw_history_records(plugin, created_record, deleted_record)
    reloaded = _load_plugin_module(monkeypatch, plugin._history_support.history_root().parents[2])
    chat_dir = reloaded._history_support._history_chat_dir("business-123", 991)
    state, _ = reloaded._history_support._load_chat_state(chat_dir)
    canonical_deleted = dict(deleted_record)
    canonical_deleted["direction"] = canonical_direction
    canonical_deleted_event_id = reloaded._history_support._event_id(canonical_deleted)

    assert canonical_deleted_event_id in state.seen_event_ids
    assert deleted_record["event_id"] in state.pending_deletions

    wrote = await reloaded._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=42),
        bot=FakeBot(owner_id=owner_id),
        now=base + timedelta(seconds=5),
    )

    assert wrote is False
    assert len(load_records(reloaded)) == 2

    result = reloaded._history_support.maintain_history(now=base + timedelta(seconds=20))
    records = load_records(reloaded)
    classification = records[-1]

    assert result.classified == 1
    assert len(records) == 3
    assert deleted_record["direction"] == legacy_direction
    assert classification["event_type"] == "deletion.classified"
    assert classification["classification"] == "unexplained"
    assert classification["classification_reason"] == "no_strong_match"
    assert classification["direction"] == canonical_direction
    assert classification["deleted_event_id"] == deleted_record["event_id"]

    second_result = reloaded._history_support.maintain_history(now=base + timedelta(seconds=21))
    assert second_result.classified == 0
    assert len(load_records(reloaded)) == 3


async def _delete_and_classify(
    plugin,
    monkeypatch: pytest.MonkeyPatch,
    *,
    original_text: str | None,
    replacement_text: str | None,
    expected_status: str,
    replacement_message_id: int = 88,
    base: datetime | None = None,
    reload_after_delete: bool = False,
):
    base = base or datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", "business-123")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS", "991")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW", "10")

    if original_text is not None:
        await plugin._history_support.observe_ptb_update(
            make_business_text_update(text=original_text, message_id=77, update_id=1, date=base),
            bot=FakeBot(),
            now=base,
        )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=2),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )
    if reload_after_delete:
        plugin = _load_plugin_module(monkeypatch, plugin._history_support.history_root().parents[3])
    if replacement_text is not None:
        await plugin._history_support.observe_ptb_update(
            make_business_text_update(text=replacement_text, message_id=replacement_message_id, update_id=3, date=base),
            bot=FakeBot(),
            now=base + timedelta(seconds=5),
        )
    result = plugin._history_support.maintain_history(now=base + timedelta(seconds=20))
    records = load_records(plugin, business_id="business-123", chat_id=991)
    classifications = [record for record in records if record["event_type"] == "deletion.classified"]
    assert classifications
    assert classifications[-1]["classification"] == expected_status
    return classifications[-1], result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original_text", "replacement_text", "expected_status", "expected_reason"),
    [
        ("Hello there", "hello   there", "likely_duplicate", "normalized_exact_duplicate"),
        ("Hello wrld", "Hello world", "likely_correction", "high_similarity_small_edit"),
        ("alpha beta gamma", "gamma beta alpha", "unexplained", "no_strong_match"),
        ("ok", "okay", "unexplained", "no_strong_match"),
        ("https://example.com 😄", "https://example.com 😄", "likely_duplicate", "normalized_exact_duplicate"),
    ],
)
async def test_deleted_message_classification_fixtures(
    plugin,
    monkeypatch: pytest.MonkeyPatch,
    original_text: str,
    replacement_text: str,
    expected_status: str,
    expected_reason: str,
):
    classification, _result = await _delete_and_classify(
        plugin,
        monkeypatch,
        original_text=original_text,
        replacement_text=replacement_text,
        expected_status=expected_status,
    )
    assert classification["classification_reason"] == expected_reason
    assert classification["classification_method"] == expected_reason


@pytest.mark.asyncio
async def test_deleted_message_without_original_is_unclassifiable(plugin, monkeypatch: pytest.MonkeyPatch):
    classification, result = await _delete_and_classify(
        plugin,
        monkeypatch,
        original_text=None,
        replacement_text="replacement",
        expected_status="unclassifiable",
    )
    assert classification["classification_reason"] == "missing_original"
    assert result.classified >= 1


@pytest.mark.asyncio
async def test_identical_pre_delete_message_within_nearby_before_window_is_likely_duplicate(enabled_history):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    bot = FakeBot()

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="same text", message_id=77, update_id=1, date=base),
        bot=bot,
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="same text", message_id=88, update_id=2, date=base),
        bot=bot,
        now=base + timedelta(seconds=5),
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=3),
        bot=bot,
        now=base + timedelta(seconds=10),
    )
    plugin._history_support.maintain_history(now=base + timedelta(seconds=30))

    classifications = [record for record in load_records(plugin) if record["event_type"] == "deletion.classified"]

    assert classifications[-1]["classification"] == "likely_duplicate"
    assert classifications[-1]["classification_reason"] == "normalized_exact_duplicate"
    assert classifications[-1]["replacement_message_id"] == 88


@pytest.mark.asyncio
async def test_post_delete_exact_duplicate_is_preferred_over_nearer_pre_delete_duplicate(enabled_history):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    bot = FakeBot()

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="same text", message_id=77, update_id=1, date=base),
        bot=bot,
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="same text", message_id=88, update_id=2, date=base),
        bot=bot,
        now=base + timedelta(seconds=9),
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=3),
        bot=bot,
        now=base + timedelta(seconds=10),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="same text", message_id=99, update_id=4, date=base),
        bot=bot,
        now=base + timedelta(seconds=14),
    )
    plugin._history_support.maintain_history(now=base + timedelta(seconds=30))

    classifications = [record for record in load_records(plugin) if record["event_type"] == "deletion.classified"]

    assert classifications[-1]["classification"] == "likely_duplicate"
    assert classifications[-1]["classification_reason"] == "normalized_exact_duplicate"
    assert classifications[-1]["replacement_message_id"] == 99
    assert classifications[-1]["classification_score"] == 1.0


@pytest.mark.asyncio
async def test_post_delete_correction_beats_pre_delete_duplicate_and_similar_candidates(enabled_history):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    bot = FakeBot()
    original_text = "Hello wrld"
    correction_text = "Hello world"

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text=original_text, message_id=77, update_id=1, date=base),
        bot=bot,
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text=correction_text, message_id=89, update_id=2, date=base),
        bot=bot,
        now=base + timedelta(seconds=8),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text=original_text, message_id=88, update_id=3, date=base),
        bot=bot,
        now=base + timedelta(seconds=9),
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=4),
        bot=bot,
        now=base + timedelta(seconds=10),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text=correction_text, message_id=99, update_id=5, date=base),
        bot=bot,
        now=base + timedelta(seconds=15),
    )
    plugin._history_support.maintain_history(now=base + timedelta(seconds=30))

    classifications = [record for record in load_records(plugin) if record["event_type"] == "deletion.classified"]
    expected_score = round(
        plugin._history_support._similarity_score(
            plugin._history_support._normalize_compare_text(original_text),
            plugin._history_support._normalize_compare_text(correction_text),
        ),
        4,
    )

    assert classifications[-1]["classification"] == "likely_correction"
    assert classifications[-1]["classification_reason"] == "high_similarity_small_edit"
    assert classifications[-1]["classification_method"] == "high_similarity_small_edit"
    assert classifications[-1]["replacement_message_id"] == 99
    assert classifications[-1]["classification_score"] == expected_score


@pytest.mark.asyncio
async def test_identical_pre_delete_message_outside_nearby_before_window_is_unexplained(enabled_history):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    bot = FakeBot()

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="same text", message_id=77, update_id=1, date=base),
        bot=bot,
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="same text", message_id=88, update_id=2, date=base),
        bot=bot,
        now=base + timedelta(seconds=1),
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=3),
        bot=bot,
        now=base + timedelta(seconds=17),
    )
    plugin._history_support.maintain_history(now=base + timedelta(seconds=30))

    classifications = [record for record in load_records(plugin) if record["event_type"] == "deletion.classified"]

    assert classifications[-1]["classification"] == "unexplained"
    assert classifications[-1]["classification_reason"] == "no_strong_match"
    assert classifications[-1].get("replacement_message_id") is None


@pytest.mark.asyncio
async def test_deleted_message_classification_survives_restart(plugin, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", "business-123")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS", "991")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW", "10")
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="Please call me back", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=2),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    reloaded = _load_plugin_module(monkeypatch, tmp_path)
    await reloaded._history_support.observe_ptb_update(
        make_business_text_update(text="Please call me back", message_id=88, update_id=3, date=base),
        bot=FakeBot(),
        now=base + timedelta(seconds=5),
    )
    result = reloaded._history_support.maintain_history(now=base + timedelta(seconds=20))
    records = load_records(reloaded)
    classifications = [record for record in records if record["event_type"] == "deletion.classified"]

    assert result.classified >= 1
    assert classifications[-1]["classification"] == "likely_duplicate"
    assert classifications[-1]["replacement_message_id"] == 88


@pytest.mark.asyncio
async def test_idle_deletion_is_classified_by_scheduler_without_manual_maintain(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    timers = TimerHarness()
    monkeypatch.setattr(plugin._history_support.threading, "Timer", timers.timer)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="Please call me back", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=2),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    assert len(timers.timers) == 1
    assert timers.timers[0].started is True
    assert not any(record["event_type"] == "deletion.classified" for record in load_records(plugin))

    timers.timers[0].fire()

    classifications = [record for record in load_records(plugin) if record["event_type"] == "deletion.classified"]
    assert classifications
    assert classifications[-1]["classification"] == "unexplained"


@pytest.mark.asyncio
async def test_startup_maintenance_classifies_overdue_deletions(plugin, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", "business-123")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS", "991")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW", "10")
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    timers = TimerHarness()
    monkeypatch.setattr(plugin._history_support.threading, "Timer", timers.timer)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="Please call me back", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=2),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    plugin._history_support.reset_in_memory_caches()
    reloaded = _load_plugin_module(monkeypatch, tmp_path)
    result = reloaded._history_support.run_startup_maintenance(now=base + timedelta(seconds=20))
    records = load_records(reloaded)
    classifications = [record for record in records if record["event_type"] == "deletion.classified"]

    assert result.classified == 1
    assert classifications[-1]["classification"] == "unexplained"


@pytest.mark.asyncio
async def test_startup_maintenance_recovers_pending_timer_before_window(plugin, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", "business-123")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS", "991")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW", "10")
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    first_timers = TimerHarness()
    monkeypatch.setattr(plugin._history_support.threading, "Timer", first_timers.timer)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="Please call me back", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=2),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )
    assert len(first_timers.timers) == 1

    plugin._history_support.reset_in_memory_caches()
    reloaded = _load_plugin_module(monkeypatch, tmp_path)
    second_timers = TimerHarness()
    monkeypatch.setattr(reloaded._history_support.threading, "Timer", second_timers.timer)
    result = reloaded._history_support.run_startup_maintenance(now=base + timedelta(seconds=5))

    assert result.classified == 0
    assert len(second_timers.timers) == 1
    assert second_timers.timers[0].interval == pytest.approx(6.0)

    second_timers.timers[0].fire()

    classifications = [record for record in load_records(reloaded) if record["event_type"] == "deletion.classified"]
    assert classifications
    assert classifications[-1]["classification"] == "unexplained"


@pytest.mark.asyncio
async def test_month_boundary_pending_deletion_protects_closed_partition_until_classified(
    plugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CONNECTIONS", "business-123")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CHATS", "991")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_CORRECTION_WINDOW", "10")
    june_message_at = datetime(2026, 6, 30, 23, 59, 50, tzinfo=timezone.utc)
    deleted_at = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
    replacement_at = datetime(2026, 7, 1, 0, 0, 5, tzinfo=timezone.utc)
    before_due = datetime(2026, 7, 1, 0, 0, 6, tzinfo=timezone.utc)
    after_due = datetime(2026, 7, 1, 0, 0, 20, tzinfo=timezone.utc)
    first_timers = TimerHarness()
    monkeypatch.setattr(plugin._history_support.threading, "Timer", first_timers.timer)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            text="Carry this across month boundary",
            message_id=77,
            update_id=1,
            date=june_message_at,
        ),
        bot=FakeBot(),
        now=june_message_at,
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=2),
        bot=FakeBot(),
        now=deleted_at,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            text="Carry this across month boundary",
            message_id=88,
            update_id=3,
            date=replacement_at,
        ),
        bot=FakeBot(),
        now=replacement_at,
    )

    june_file = load_history_file(plugin, month="2026-06.jsonl")
    july_file = load_history_file(plugin, month="2026-07.jsonl")
    total_before = june_file.stat().st_size + july_file.stat().st_size
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_MAX_BYTES", str(total_before - 1))

    plugin._history_support.reset_in_memory_caches()
    reloaded = _load_plugin_module(monkeypatch, tmp_path)
    second_timers = TimerHarness()
    monkeypatch.setattr(reloaded._history_support.threading, "Timer", second_timers.timer)

    before_result = reloaded._history_support.run_startup_maintenance(now=before_due)

    assert before_result.classified == 0
    assert before_result.pruned_files == 0
    assert before_result.cap_exceeded is True
    assert before_result.cap_shortfall_bytes == 1
    assert any("pending-deletion-protected closed partitions" in warning for warning in before_result.warnings)
    assert june_file.exists()
    assert july_file.exists()
    assert len(second_timers.timers) == 1
    assert second_timers.timers[0].interval == pytest.approx(3.0)
    assert not any(record["event_type"] == "deletion.classified" for record in load_records(reloaded))

    chat_dir = reloaded._history_support._history_chat_dir("business-123", 991)
    with reloaded._history_support._chat_read_lock(chat_dir):
        state, _ = reloaded._history_support._load_chat_state(chat_dir)
    assert any(pending.classification is None for pending in state.pending_deletions.values())

    monkeypatch.setattr(reloaded._history_support, "_utcnow", lambda: after_due)
    second_timers.timers[0].fire()

    records_before_prune = load_records(reloaded)
    classifications_before_prune = [
        record for record in records_before_prune if record["event_type"] == "deletion.classified"
    ]
    assert june_file.exists()
    assert classifications_before_prune[-1]["classification"] == "likely_duplicate"
    assert classifications_before_prune[-1]["classification_reason"] == "normalized_exact_duplicate"
    assert classifications_before_prune[-1]["replacement_message_id"] == 88

    after_result = reloaded._history_support.maintain_history(now=after_due)

    assert after_result.classified == 0
    assert after_result.pruned_files >= 1
    assert after_result.cap_exceeded is False
    assert not june_file.exists()
    assert july_file.exists()

    records_after_prune = load_records(reloaded)
    assert [record["event_type"] for record in records_after_prune] == [
        "message.created",
        "deletion.classified",
    ]
    assert records_after_prune[-1]["classification_reason"] == "normalized_exact_duplicate"
    assert records_after_prune[-1]["replacement_message_id"] == 88

    catalog = load_catalog(reloaded)
    assert catalog["contact_count"] == 1
    entry = catalog["contacts"][0]
    assert int(entry["chat_id"]) == 991
    assert entry["message_count"] == 1
    assert entry["deleted_count"] == 0
    assert entry["record_count"] == 2


@pytest.mark.asyncio
async def test_retention_and_size_prune_closed_partitions_but_preserve_active(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    old_time = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    current_time = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="old month", message_id=77, update_id=1, date=old_time),
        bot=FakeBot(),
        now=old_time,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="current month", message_id=78, update_id=2, date=current_time),
        bot=FakeBot(),
        now=current_time,
    )

    old_file = load_history_file(plugin, month="2026-06.jsonl")
    current_file = load_history_file(plugin, month="2026-07.jsonl")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS", "20")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_MAX_BYTES", str(current_file.stat().st_size + 1))

    result = plugin._history_support.maintain_history(now=current_time)

    assert result.pruned_files >= 1
    assert not old_file.exists()
    assert current_file.exists()
    assert json.loads(current_file.read_text(encoding="utf-8").splitlines()[-1])["message_id"] == 78


@pytest.mark.asyncio
async def test_retention_prune_rebuilds_catalog_from_remaining_canonical_history(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    june_time = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    july_time = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=991,
            chat_username="alice-june",
            chat_first_name="Alice",
            chat_last_name="June",
            from_user_username="alice-june",
            text="old alias only",
            update_id=1,
            date=june_time,
        ),
        bot=FakeBot(),
        now=june_time,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=991,
            chat_username="alice-july",
            chat_first_name="Alicia",
            chat_last_name="July",
            from_user_username="alice-july",
            text="current alias only",
            update_id=2,
            date=july_time,
            edited=True,
        ),
        bot=FakeBot(),
        now=july_time,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=992,
            chat_username="pruned-only",
            chat_first_name="Pruned",
            chat_last_name="Only",
            from_user_username="pruned-only",
            text="remove me",
            update_id=3,
            date=june_time,
        ),
        bot=FakeBot(),
        now=june_time + timedelta(seconds=1),
    )

    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS", "10")
    result = plugin._history_support.maintain_history(now=july_time)
    catalog = load_catalog(plugin)
    entries = {int(entry["chat_id"]): entry for entry in catalog["contacts"]}

    assert result.pruned_files >= 2
    assert 992 not in entries
    assert 991 in entries
    assert entries[991]["message_count"] == 0
    assert entries[991]["edit_count"] == 1
    assert entries[991]["record_count"] == 1
    assert entries[991]["first_seen_at"].startswith("2026-07-19T10:00:00")
    assert entries[991]["last_seen_at"].startswith("2026-07-19T10:00:00")
    assert entries[991]["current_profile"]["username"] == "alice-july"
    assert "@alice-june" not in entries[991]["aliases"]
    assert "Alice June" not in entries[991]["aliases"]


@pytest.mark.asyncio
async def test_post_prune_catalog_rebuild_failure_marks_dirty_until_explicit_rebuild(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    history = plugin._history_support
    june_time = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    july_time = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await history.observe_ptb_update(
        make_business_text_update(text="old month", message_id=77, update_id=1, date=june_time),
        bot=FakeBot(),
        now=june_time,
    )
    await history.observe_ptb_update(
        make_business_text_update(text="current month", message_id=78, update_id=2, date=july_time),
        bot=FakeBot(),
        now=july_time,
    )

    old_file = load_history_file(plugin, month="2026-06.jsonl")
    original_write_catalog = history._write_catalog_locked
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS", "10")
    monkeypatch.setattr(
        history,
        "_write_catalog_locked",
        lambda _catalog: (_ for _ in ()).throw(OSError("catalog write failed")),
    )

    result = history.maintain_history(now=july_time)

    assert not old_file.exists()
    assert any("post-prune contact catalog rebuild failed" in warning for warning in result.warnings)
    assert catalog_dirty_path(plugin).exists()

    before_status = snapshot_history_tree(plugin)
    monkeypatch.setattr(history, "_write_catalog_locked", original_write_catalog)

    exit_code, output = _run_history_cli(plugin, "history", "catalog")

    assert exit_code == 0
    assert output.startswith("status=dirty ")
    assert snapshot_history_tree(plugin) == before_status

    exit_code, output = _run_history_cli(plugin, "history", "catalog", "--rebuild")

    assert exit_code == 0
    assert output.startswith("status=rebuilt ")
    assert not catalog_dirty_path(plugin).exists()
    assert load_catalog(plugin)["contact_count"] == 1


@pytest.mark.asyncio
async def test_active_month_cap_failure_is_explicit(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    current_time = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="x" * 1500, message_id=77, update_id=1, date=current_time),
        bot=FakeBot(),
        now=current_time,
    )
    current_file = load_history_file(plugin, month="2026-07.jsonl")
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_MAX_BYTES", str(max(1, current_file.stat().st_size - 10)))

    result = plugin._history_support.maintain_history(now=current_time)

    assert result.cap_exceeded is True
    assert result.cap_shortfall_bytes > 0
    assert current_file.exists()
    assert plugin._history_support.verify_history().ok is True


@pytest.mark.asyncio
async def test_verify_warns_not_errors_when_retention_prunes_old_delete_tombstone(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    original_time = datetime(2026, 6, 30, 23, 59, 40, tzinfo=timezone.utc)
    deleted_time = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
    classified_time = datetime(2026, 7, 1, 0, 0, 20, tzinfo=timezone.utc)
    verify_time = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="carry forward", message_id=77, update_id=1, date=original_time),
        bot=FakeBot(),
        now=original_time,
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(77,), update_id=2),
        bot=FakeBot(),
        now=deleted_time,
    )
    plugin._history_support.maintain_history(now=classified_time)

    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS", "10")
    result = plugin._history_support.maintain_history(now=verify_time)

    june_file = load_history_file(plugin, month="2026-06.jsonl")
    july_file = load_history_file(plugin, month="2026-07.jsonl")
    assert result.pruned_files >= 1
    assert not june_file.exists()
    assert july_file.exists()

    verification = plugin._history_support.verify_history(now=verify_time)
    assert verification.ok is True
    assert any("outside the retained archive" in warning for warning in verification.warnings)


def test_collect_history_stats_streams_bounded_snapshot_without_full_load(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    records: list[dict[str, Any]] = []
    update_id = 1

    for business_id, chat_id, count in (("business-123", 991, 3000), ("business-456", 992, 1000)):
        for index in range(count):
            observed_at = base + timedelta(seconds=index)
            records.append(
                make_legacy_history_record(
                    plugin,
                    event_type="message.created",
                    source="business_message",
                    observed_at=observed_at,
                    telegram_update_id=update_id,
                    business_connection_id=business_id,
                    chat_id=chat_id,
                    message_id=10000 + index,
                    message_at=observed_at,
                    sender_id=2000,
                    direction="inbound",
                    text=f"{business_id} ordinary {index}",
                )
            )
            update_id += 1

    deleted_one = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=501,
        message_at=None,
        sender_id=None,
        direction="unknown",
    )
    update_id += 1
    deleted_two = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1, seconds=1),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=502,
        message_at=None,
        sender_id=None,
        direction="unknown",
    )
    update_id += 1
    classified_two = make_legacy_history_record(
        plugin,
        event_type="deletion.classified",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1, seconds=31),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=502,
        message_at=None,
        sender_id=None,
        direction="unknown",
        deleted_event_id=str(deleted_two["event_id"]),
        classification="unexplained",
        classification_reason="no_strong_match",
        evaluated_at=base + timedelta(hours=1, seconds=31),
        deleted_observed_at=base + timedelta(hours=1, seconds=1),
    )
    update_id += 1
    deleted_three = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1, seconds=2),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=503,
        message_at=None,
        sender_id=None,
        direction="unknown",
    )
    update_id += 1
    classified_three = make_legacy_history_record(
        plugin,
        event_type="deletion.classified",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1, seconds=32),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=503,
        message_at=None,
        sender_id=None,
        direction="unknown",
        deleted_event_id=str(deleted_three["event_id"]),
        classification="likely_duplicate",
        classification_reason="normalized_exact_duplicate",
        evaluated_at=base + timedelta(hours=1, seconds=32),
        deleted_observed_at=base + timedelta(hours=1, seconds=2),
    )

    append_raw_history_records(plugin, *records, deleted_one, deleted_two, classified_two, deleted_three, classified_three)

    total_bytes = (
        load_history_file(plugin, business_id="business-123", chat_id=991).stat().st_size
        + load_history_file(plugin, business_id="business-456", chat_id=992).stat().st_size
    )
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_MAX_BYTES", str(total_bytes - 7))

    monkeypatch.setattr(
        history,
        "_load_records",
        lambda *_args, **_kwargs: pytest.fail("_load_records must not be used by history stats"),
    )
    monkeypatch.setattr(
        history,
        "_load_chat_state",
        lambda *_args, **_kwargs: pytest.fail("_load_chat_state must not be used by history stats"),
    )
    monkeypatch.setattr(
        history,
        "_build_chat_state",
        lambda *_args, **_kwargs: pytest.fail("_build_chat_state must not be used by history stats"),
    )

    summary = history._scan_chat_live_pending_summary(history._history_chat_dir_path("business-123", 991), repair_tails=False)
    stats = history.collect_history_stats()

    assert summary.record_count == 3005
    assert summary.pending_count == 1
    assert summary.unexplained_count == 1
    assert summary.max_live_pending == 2
    assert stats.chat_count == 2
    assert stats.file_count == 2
    assert stats.record_count == 4005
    assert stats.pending_count == 1
    assert stats.unexplained_count == 1
    assert stats.total_bytes == total_bytes
    assert stats.cap_exceeded is True
    assert stats.cap_shortfall_bytes == 7


@pytest.mark.asyncio
async def test_collect_history_stats_holds_root_snapshot_against_concurrent_capture_and_prune(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    history = plugin._history_support
    june_time = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    july_time = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await history.observe_ptb_update(
        make_business_text_update(text="old month", message_id=77, update_id=1, date=june_time),
        bot=FakeBot(),
        now=june_time,
    )
    await history.observe_ptb_update(
        make_business_text_update(text="current month", message_id=78, update_id=2, date=july_time),
        bot=FakeBot(),
        now=july_time,
    )

    june_file = load_history_file(plugin, month="2026-06.jsonl")
    july_file = load_history_file(plugin, month="2026-07.jsonl")
    expected_bytes = june_file.stat().st_size + july_file.stat().st_size
    original_open = Path.open
    stats_selected = threading.Event()
    allow_stats_open = threading.Event()
    stats_done = threading.Event()
    capture_done = threading.Event()
    prune_done = threading.Event()
    results: dict[str, Any] = {}

    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_RETENTION_DAYS", "10")

    def _blocking_open(path_obj: Path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if path_obj == july_file and "r" in mode:
            stats_selected.set()
            assert allow_stats_open.wait(timeout=5)
        return original_open(path_obj, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _blocking_open)

    def _stats() -> None:
        try:
            results["stats"] = history.collect_history_stats()
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["stats_exc"] = exc
        finally:
            stats_done.set()

    def _capture() -> None:
        try:
            results["capture"] = asyncio.run(
                history.observe_ptb_update(
                    make_business_text_update(
                        text="new chat while stats snapshots",
                        business_id="business-456",
                        chat_id=992,
                        message_id=88,
                        update_id=3,
                        date=july_time + timedelta(seconds=1),
                    ),
                    bot=FakeBot(),
                    now=july_time + timedelta(seconds=1),
                )
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["capture_exc"] = exc
        finally:
            capture_done.set()

    def _prune() -> None:
        try:
            results["maintenance"] = history.maintain_history(now=july_time)
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["prune_exc"] = exc
        finally:
            prune_done.set()

    stats_thread = threading.Thread(target=_stats)
    capture_thread = threading.Thread(target=_capture)
    prune_thread = threading.Thread(target=_prune)
    stats_thread.start()
    assert stats_selected.wait(timeout=5)

    capture_thread.start()
    prune_thread.start()
    assert capture_done.wait(timeout=0.1) is False
    assert prune_done.wait(timeout=0.1) is False

    allow_stats_open.set()
    stats_thread.join(timeout=5)
    capture_thread.join(timeout=5)
    prune_thread.join(timeout=5)

    assert not stats_thread.is_alive()
    assert not capture_thread.is_alive()
    assert not prune_thread.is_alive()
    assert stats_done.is_set()
    assert capture_done.is_set()
    assert prune_done.is_set()
    assert "stats_exc" not in results
    assert "capture_exc" not in results
    assert "prune_exc" not in results
    assert results["capture"] is True
    assert results["maintenance"].pruned_files == 1

    stats = results["stats"]
    assert stats.chat_count == 1
    assert stats.file_count == 2
    assert stats.record_count == 2
    assert stats.pending_count == 0
    assert stats.unexplained_count == 0
    assert stats.total_bytes == expected_bytes


def test_verify_history_streams_bounded_archive_and_detects_duplicates(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    records: list[dict[str, Any]] = []
    update_id = 1

    for index in range(2496):
        observed_at = base + timedelta(seconds=index)
        records.append(
            make_legacy_history_record(
                plugin,
                event_type="message.created",
                source="business_message",
                observed_at=observed_at,
                telegram_update_id=update_id,
                business_connection_id="business-123",
                chat_id=991,
                message_id=20000 + index,
                message_at=observed_at,
                sender_id=2000,
                direction="inbound",
                text=f"verify ordinary {index}",
            )
        )
        update_id += 1

    deleted_good = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=500,
        message_at=None,
        sender_id=None,
        direction="unknown",
    )
    update_id += 1
    classified_good = make_legacy_history_record(
        plugin,
        event_type="deletion.classified",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1, seconds=20),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=500,
        message_at=None,
        sender_id=None,
        direction="unknown",
        deleted_event_id=str(deleted_good["event_id"]),
        classification="likely_duplicate",
        classification_reason="normalized_exact_duplicate",
        evaluated_at=base + timedelta(hours=1, seconds=20),
        deleted_observed_at=base + timedelta(hours=1),
    )
    update_id += 1
    filler = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(hours=1, seconds=21),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=6000,
        message_at=base + timedelta(hours=1, seconds=21),
        sender_id=2000,
        direction="inbound",
        text="alignment filler",
    )
    update_id += 1

    dup_within_one = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(hours=2),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7001,
        message_at=base + timedelta(hours=2),
        sender_id=2000,
        direction="inbound",
        text="duplicate within one",
    )
    update_id += 1
    dup_within_two = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(hours=2, seconds=1),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7002,
        message_at=base + timedelta(hours=2, seconds=1),
        sender_id=2000,
        direction="inbound",
        text="duplicate within two",
    )
    dup_within_two["event_id"] = dup_within_one["event_id"]
    update_id += 1
    schema_warning = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(hours=2, seconds=2),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7003,
        message_at=base + timedelta(hours=2, seconds=2),
        sender_id=2000,
        direction="inbound",
        text="unsupported schema",
    )
    schema_warning["schema_version"] = 999
    update_id += 1
    dup_across_one = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(hours=2, seconds=3),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7004,
        message_at=base + timedelta(hours=2, seconds=3),
        sender_id=2000,
        direction="inbound",
        text="duplicate across one",
    )
    update_id += 1
    outside_retained = make_legacy_history_record(
        plugin,
        event_type="deletion.classified",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=2, seconds=4),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7005,
        message_at=None,
        sender_id=None,
        direction="unknown",
        deleted_event_id="outside-retained",
        classification="unexplained",
        classification_reason="no_strong_match",
        evaluated_at=base + timedelta(hours=2, seconds=4),
        deleted_observed_at=base + timedelta(hours=1, seconds=59),
    )
    update_id += 1
    unique_filler = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(hours=2, seconds=5),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7006,
        message_at=base + timedelta(hours=2, seconds=5),
        sender_id=2000,
        direction="inbound",
        text="chunk filler",
    )
    update_id += 1
    dup_across_two = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(hours=2, seconds=6),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7007,
        message_at=base + timedelta(hours=2, seconds=6),
        sender_id=2000,
        direction="inbound",
        text="duplicate across two",
    )
    dup_across_two["event_id"] = dup_across_one["event_id"]
    update_id += 1
    missing_event_id = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base + timedelta(hours=2, seconds=7),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7008,
        message_at=base + timedelta(hours=2, seconds=7),
        sender_id=2000,
        direction="inbound",
        text="missing event id",
    )
    missing_event_id.pop("event_id")
    update_id += 1
    missing_tombstone = make_legacy_history_record(
        plugin,
        event_type="deletion.classified",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=2, seconds=8),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=7009,
        message_at=None,
        sender_id=None,
        direction="unknown",
        deleted_event_id="missing-tombstone",
        classification="unexplained",
        classification_reason="no_strong_match",
        evaluated_at=base + timedelta(hours=2, seconds=8),
    )

    records.extend(
        [
            deleted_good,
            classified_good,
            filler,
            dup_within_one,
            dup_within_two,
            schema_warning,
            dup_across_one,
            outside_retained,
            unique_filler,
            dup_across_two,
            missing_event_id,
            missing_tombstone,
        ]
    )
    append_raw_history_records(plugin, *records)

    total_bytes = load_history_file(plugin).stat().st_size
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_MAX_BYTES", str(total_bytes - 5))
    monkeypatch.setattr(history, "VERIFY_EVENT_ID_CHUNK_RECORDS", 3)
    monkeypatch.setattr(
        history,
        "_load_records",
        lambda *_args, **_kwargs: pytest.fail("_load_records must not be used by history verify"),
    )
    monkeypatch.setattr(
        history,
        "_load_chat_state",
        lambda *_args, **_kwargs: pytest.fail("_load_chat_state must not be used by history verify"),
    )
    monkeypatch.setattr(
        history,
        "_build_chat_state",
        lambda *_args, **_kwargs: pytest.fail("_build_chat_state must not be used by history verify"),
    )

    verification = history.verify_history()

    assert verification.ok is False
    assert verification.chat_count == 1
    assert verification.file_count == 1
    assert verification.record_count == len(records)
    assert verification.repaired_files == 0
    assert any("missing event_id" in error for error in verification.errors)
    assert sum("duplicate event_id" in warning for warning in verification.warnings) == 2
    assert any("unsupported schema_version 999" in warning for warning in verification.warnings)
    assert any("outside the retained archive" in warning for warning in verification.warnings)
    assert any("missing tombstone" in warning for warning in verification.warnings)
    assert any("history size cap exceeded by 5 bytes" in warning for warning in verification.warnings)
    assert not list(history.history_root().glob(f"{history.VERIFY_EVENT_ID_TEMP_DIR_PREFIX}*"))


def test_verify_history_reports_active_month_torn_tail_without_repair(plugin):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    record = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=1,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        text="tail without newline",
    )
    append_raw_history_records(plugin, record)

    history_file = load_history_file(plugin)
    with history_file.open("rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        handle.truncate()

    verification = history.verify_history()

    assert verification.ok is False
    assert verification.repaired_files == 0
    assert any(history_file.name in error and "verify --repair-tails" in error for error in verification.errors)


def test_verify_history_repairs_valid_final_record_missing_newline(plugin):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    record = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=1,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        text="repair newline only",
    )
    append_raw_history_records(plugin, record)

    history_file = load_history_file(plugin)
    with history_file.open("rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        handle.truncate()

    verification = history.verify_history(repair_tails=True)

    assert verification.ok is True
    assert verification.record_count == 1
    assert verification.repaired_files == 1
    assert history_file.read_bytes().endswith(b"\n")
    assert len(history_file.read_text(encoding="utf-8").splitlines()) == 1


def test_verify_history_repairs_partial_torn_tail_without_full_load(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    record = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=1,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        text="repair torn tail",
    )
    append_raw_history_records(plugin, record)

    history_file = load_history_file(plugin)
    with history_file.open("ab") as handle:
        handle.write(b"{\"event_id\":\"broken\"")

    monkeypatch.setattr(
        history,
        "_load_records",
        lambda *_args, **_kwargs: pytest.fail("_load_records must not be used by history verify repair"),
    )
    monkeypatch.setattr(
        history,
        "_load_chat_state",
        lambda *_args, **_kwargs: pytest.fail("_load_chat_state must not be used by history verify repair"),
    )
    monkeypatch.setattr(
        history,
        "_build_chat_state",
        lambda *_args, **_kwargs: pytest.fail("_build_chat_state must not be used by history verify repair"),
    )

    verification = history.verify_history(repair_tails=True)

    assert verification.ok is True
    assert verification.record_count == 1
    assert verification.repaired_files == 1
    assert history_file.read_bytes().endswith(b"\n")
    assert len(history_file.read_text(encoding="utf-8").splitlines()) == 1
    assert not list(history.history_root().glob(f"{history.VERIFY_EVENT_ID_TEMP_DIR_PREFIX}*"))


def test_verify_history_cleans_duplicate_temp_files_when_duplicate_audit_errors(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    record = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=1,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        text="duplicate audit cleanup",
    )
    append_raw_history_records(plugin, record)

    monkeypatch.setattr(history, "VERIFY_EVENT_ID_CHUNK_RECORDS", 1)

    def _boom(duplicate_tracker):
        duplicate_tracker._spill._flush_chunk()
        raise RuntimeError("boom")

    monkeypatch.setattr(history, "_iter_duplicate_event_id_warnings", _boom)

    verification = history.verify_history()

    assert verification.ok is False
    assert any("duplicate event_id audit failed: boom" in error for error in verification.errors)
    assert not list(history.history_root().glob(f"{history.VERIFY_EVENT_ID_TEMP_DIR_PREFIX}*"))


def test_verify_history_allows_multiple_classifications_for_one_retained_tombstone_out_of_order(plugin):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    deleted = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(seconds=2),
        telegram_update_id=2,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="unknown",
    )
    classified_before = make_legacy_history_record(
        plugin,
        event_type="deletion.classified",
        source="deleted_business_messages",
        observed_at=base + timedelta(seconds=1),
        telegram_update_id=3,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="unknown",
        deleted_event_id=str(deleted["event_id"]),
        classification="unexplained",
        classification_reason="no_strong_match",
        evaluated_at=base + timedelta(seconds=3),
        deleted_observed_at=base + timedelta(seconds=2),
    )
    classified_after = make_legacy_history_record(
        plugin,
        event_type="deletion.classified",
        source="deleted_business_messages",
        observed_at=base + timedelta(seconds=4),
        telegram_update_id=4,
        business_connection_id="business-123",
        chat_id=991,
        message_id=77,
        message_at=base,
        sender_id=2000,
        direction="unknown",
        deleted_event_id=str(deleted["event_id"]),
        classification="likely_duplicate",
        classification_reason="normalized_exact_duplicate",
        evaluated_at=base + timedelta(seconds=4),
        deleted_observed_at=base + timedelta(seconds=2),
    )

    append_raw_history_records(plugin, classified_before, deleted, classified_after)

    verification = history.verify_history()

    assert verification.ok is True
    assert verification.warning_count == 0
    assert not verification.warnings


def test_streamed_read_path_does_not_truncate_during_root_scoped_verify_pass(enabled_history, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    asyncio.run(
        history.observe_ptb_update(
            make_business_text_update(text="root scoped verify", message_id=77, update_id=1, date=base),
            bot=FakeBot(),
            now=base,
        )
    )

    chat_dir = history._history_chat_dir("business-123", 991)
    history_file = load_history_file(plugin)
    with history_file.open("rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        handle.truncate()
    torn_bytes = history_file.read_bytes()

    original_iter_chat_dirs = history._iter_chat_dirs
    verify_selected = threading.Event()
    allow_verify = threading.Event()
    verify_done = threading.Event()
    stream_done = threading.Event()
    results: dict[str, Any] = {}

    def _blocking_iter_chat_dirs():
        verify_selected.set()
        assert allow_verify.wait(timeout=5)
        yield from original_iter_chat_dirs()

    monkeypatch.setattr(history, "_iter_chat_dirs", _blocking_iter_chat_dirs)

    def _verify() -> None:
        try:
            results["verify"] = history.verify_history()
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["verify_exc"] = exc
        finally:
            verify_done.set()

    def _stream() -> None:
        try:
            with history._streamed_records_locked(chat_dir, since=None, until=None, text_query=None) as records:
                list(records)
        except Exception as exc:  # noqa: BLE001 - asserted below
            results["stream_exc"] = exc
        finally:
            stream_done.set()

    verify_thread = threading.Thread(target=_verify)
    stream_thread = threading.Thread(target=_stream)
    verify_thread.start()
    assert verify_selected.wait(timeout=5)

    stream_thread.start()
    stream_thread.join(timeout=5)

    assert stream_done.is_set()
    assert "stream_exc" in results
    assert "verify --repair-tails" in str(results["stream_exc"])
    assert history_file.read_bytes() == torn_bytes
    assert history_file.read_bytes().endswith(b"\n") is False

    allow_verify.set()
    verify_thread.join(timeout=5)

    assert not verify_thread.is_alive()
    assert verify_done.is_set()
    assert "verify_exc" not in results
    assert results["verify"].ok is False


def test_known_chat_type_fallback_does_not_repair_torn_tail(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    asyncio.run(
        history.observe_ptb_update(
            make_business_text_update(text="known type source", message_id=77, update_id=1, date=base),
            bot=FakeBot(),
            now=base,
        )
    )

    history_file = load_history_file(plugin)
    with history_file.open("rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        handle.truncate()
    torn_bytes = history_file.read_bytes()
    history._mark_catalog_dirty("force_known_type_history_fallback")

    with pytest.raises(ValueError, match="verify --repair-tails"):
        asyncio.run(
            history.observe_ptb_update(
                make_deleted_update(chat_type=None, message_ids=(77,), update_id=2),
                bot=FakeBot(),
                now=base + timedelta(seconds=1),
            )
        )

    assert history_file.read_bytes() == torn_bytes


@pytest.mark.parametrize(
    "argv",
    [
        ("history", "show", "--chat", "991"),
        ("history", "search", "--chat", "991", "--text", "hello"),
        ("history", "export", "--chat", "991"),
        ("history", "deletions", "--chat", "991", "--status", "pending"),
    ],
)
def test_history_cli_read_paths_error_on_torn_tail_without_mutation(enabled_history, argv: tuple[str, ...]):
    plugin = enabled_history
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    asyncio.run(
        history.observe_ptb_update(
            make_business_text_update(text="cli torn tail", message_id=77, update_id=1, date=base),
            bot=FakeBot(),
            now=base,
        )
    )

    history_file = load_history_file(plugin)
    with history_file.open("rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        handle.truncate()
    torn_bytes = history_file.read_bytes()

    exit_code, output = _run_history_cli(plugin, *argv)

    assert exit_code == 1
    assert "history contact catalog is stale" in output
    assert "history catalog --rebuild" in output
    assert history_file.read_bytes() == torn_bytes


def test_verify_history_caps_diagnostics_and_cli_output(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    records: list[dict[str, Any]] = []
    update_id = 1

    duplicate_seed = make_legacy_history_record(
        plugin,
        event_type="message.created",
        source="business_message",
        observed_at=base,
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=1000,
        message_at=base,
        sender_id=2000,
        direction="inbound",
        text="duplicate seed",
    )
    records.append(duplicate_seed)
    update_id += 1
    duplicate_event_id = duplicate_seed["event_id"]

    for index in range(1000):
        record = make_legacy_history_record(
            plugin,
            event_type="message.created",
            source="business_message",
            observed_at=base + timedelta(seconds=1 + index),
            telegram_update_id=update_id,
            business_connection_id="business-123",
            chat_id=991,
            message_id=2000 + index,
            message_at=base + timedelta(seconds=1 + index),
            sender_id=2000,
            direction="inbound",
            text=f"duplicate {index}",
        )
        record["event_id"] = duplicate_event_id
        records.append(record)
        update_id += 1

    for index in range(1000):
        record = make_legacy_history_record(
            plugin,
            event_type="message.created",
            source="business_message",
            observed_at=base + timedelta(hours=1, seconds=index),
            telegram_update_id=update_id,
            business_connection_id="business-123",
            chat_id=991,
            message_id=4000 + index,
            message_at=base + timedelta(hours=1, seconds=index),
            sender_id=2000,
            direction="inbound",
            text=f"schema warning {index}",
        )
        record["schema_version"] = 999
        records.append(record)
        update_id += 1

    for index in range(1000):
        record = make_legacy_history_record(
            plugin,
            event_type="message.created",
            source="business_message",
            observed_at=base + timedelta(hours=2, seconds=index),
            telegram_update_id=update_id,
            business_connection_id="business-123",
            chat_id=991,
            message_id=6000 + index,
            message_at=base + timedelta(hours=2, seconds=index),
            sender_id=2000,
            direction="inbound",
            text=f"missing event id {index}",
        )
        record.pop("event_id")
        records.append(record)
        update_id += 1

    for index in range(1000):
        record = make_legacy_history_record(
            plugin,
            event_type="deletion.classified",
            source="deleted_business_messages",
            observed_at=base + timedelta(hours=3, seconds=index),
            telegram_update_id=update_id,
            business_connection_id="business-123",
            chat_id=991,
            message_id=8000 + index,
            message_at=None,
            sender_id=None,
            direction="unknown",
            deleted_event_id=f"outside-{index}",
            classification="unexplained",
            classification_reason="no_strong_match",
            evaluated_at=base + timedelta(hours=3, seconds=index),
            deleted_observed_at=base + timedelta(hours=2, seconds=index),
        )
        records.append(record)
        update_id += 1

    append_raw_history_records(plugin, *records)
    monkeypatch.setattr(history, "VERIFY_EVENT_ID_CHUNK_RECORDS", 7)
    monkeypatch.setattr(history, "VERIFY_REFERENCE_CHUNK_RECORDS", 7)
    monkeypatch.setattr(history, "VERIFY_DIAGNOSTIC_LIMIT", 5)

    verification = history.verify_history()

    assert verification.ok is False
    assert verification.error_count == 1000
    assert verification.warning_count == 3000
    assert verification.suppressed_error_count == 995
    assert verification.suppressed_warning_count == 2995
    assert len(verification.errors) == 5
    assert len(verification.warnings) == 5
    assert all("missing event_id" in error for error in verification.errors)
    assert all("duplicate event_id" in warning for warning in verification.warnings)
    assert not list(history.history_root().glob(".verify-*"))

    exit_code, output = _run_history_cli(plugin, "history", "verify")
    output_lines = output.splitlines()

    assert exit_code == 1
    assert output_lines[0] == (
        f"ok=False chats=1 files=1 records={len(records)} repaired_files=0 "
        "errors=1000 warnings=3000"
    )
    assert len(output_lines) == 13
    assert all(line.startswith("warning: ") and "duplicate event_id" in line for line in output_lines[1:6])
    assert output_lines[6] == "warning: suppressed 2995 additional warnings"
    assert all(line.startswith("error: ") and "missing event_id" in line for line in output_lines[7:12])
    assert output_lines[12] == "error: suppressed 995 additional errors"
    assert not list(history.history_root().glob(".verify-*"))


def _build_history_parser(plugin):
    parser = argparse.ArgumentParser(prog="hermes telegram-business")
    plugin._history_support.setup_cli(parser)
    return parser


def _run_history_cli(plugin, *argv: str) -> tuple[int, str]:
    parser = _build_history_parser(plugin)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = plugin._history_support.handle_cli(parser.parse_args(list(argv)))
    return exit_code, output.getvalue().strip()


@pytest.mark.asyncio
async def test_history_cli_show_search_export_deletions_and_verify(enabled_history, capsys, monkeypatch: pytest.MonkeyPatch):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="first alpha", message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="second beta", message_id=78, update_id=2, date=base),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )
    await plugin._history_support.observe_ptb_update(
        make_deleted_update(message_ids=(78,), update_id=3),
        bot=FakeBot(),
        now=base + timedelta(seconds=2),
    )
    plugin._history_support.maintain_history(now=base + timedelta(seconds=20))

    parser = _build_history_parser(plugin)

    exit_code = plugin._history_support.handle_cli(parser.parse_args(["history", "stats"]))
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert "correction_window=10s" in output
    assert "nearby_before=15s" in output

    exit_code = plugin._history_support.handle_cli(parser.parse_args(["history", "show", "--chat", "991", "--limit", "1"]))
    output = capsys.readouterr().out.strip().splitlines()
    assert exit_code == 0
    assert len(output) == 1
    assert "deletion.classified" in output[0]
    assert "reason=no_strong_match" in output[0]

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "search", "--chat", "991", "--text", "alpha", "--limit", "5"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert "first alpha" in output
    assert "second beta" not in output

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "export", "--chat", "991", "--format", "jsonl", "--limit", "1"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert json.loads(output)["event_type"] in {"message.deleted", "deletion.classified"}

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "deletions", "--status", "unexplained", "--limit", "5"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert "status=unexplained" in output

    history_file = load_history_file(plugin)
    with history_file.open("ab") as handle:
        handle.write(b"{\"event_id\":\"broken\"")
    exit_code = plugin._history_support.handle_cli(parser.parse_args(["history", "verify", "--repair-tails"]))
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "ok=True" in output


@pytest.mark.asyncio
async def test_history_cli_contacts_catalog_and_contact_resolution(plugin, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=991,
            chat_username="alice-one",
            chat_first_name="Alice",
            chat_last_name="Smith",
            from_user_username="alice_sender_one",
            text="first alice",
            update_id=1,
            date=base,
        ),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=992,
            chat_username="alice-two",
            chat_first_name="Alice",
            chat_last_name="Smith",
            from_user_username="alice_sender_two",
            text="second alice",
            update_id=2,
            date=base,
        ),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    parser = _build_history_parser(plugin)

    exit_code = plugin._history_support.handle_cli(parser.parse_args(["history", "contacts", "--search", "alice"]))
    output = capsys.readouterr().out.strip().splitlines()
    assert exit_code == 0
    assert any("username=@alice-one" in line for line in output)
    assert any("username=@alice-two" in line for line in output)

    exit_code = plugin._history_support.handle_cli(parser.parse_args(["history", "catalog"]))
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert output.startswith("status=ok ")

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "show", "--contact", "Alice Smith", "--limit", "1"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 1
    assert "ambiguous" in output

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "show", "--contact", "@alice-two", "--limit", "1"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert "chat=992" in output
    assert "second alice" in output


@pytest.mark.asyncio
async def test_history_cli_numeric_chat_resolution_uses_fresh_catalog_for_duplicate_chat_ids(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            business_id="business-123",
            chat_id=991,
            chat_username="dup-one",
            from_user_username="dup-one",
            text="first duplicate chat",
            update_id=1,
            date=base,
        ),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            business_id="business-456",
            chat_id=991,
            chat_username="dup-two",
            from_user_username="dup-two",
            text="second duplicate chat",
            update_id=2,
            date=base + timedelta(seconds=1),
        ),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    history = plugin._history_support
    monkeypatch.setattr(
        history,
        "_load_records",
        lambda *_args, **_kwargs: pytest.fail("_load_records must not be used for numeric --chat resolution"),
    )

    parser = _build_history_parser(plugin)

    exit_code = history.handle_cli(parser.parse_args(["history", "show", "--chat", "991", "--limit", "1"]))
    output = capsys.readouterr().out.strip()
    assert exit_code == 1
    assert "pass --connection explicitly" in output

    exit_code = history.handle_cli(
        parser.parse_args(["history", "show", "--connection", "business-456", "--chat", "991", "--limit", "1"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert "connection=business-456" in output
    assert "second duplicate chat" in output

    exit_code = history.handle_cli(
        parser.parse_args(["history", "show", "--connection", "business-789", "--chat", "991", "--limit", "1"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 1
    assert "no matching history chat found for connection=business-789 chat=991" in output


@pytest.mark.asyncio
async def test_history_cli_numeric_chat_resolution_surfaces_catalog_freshness_error_without_full_load(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="catalog error", update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    history = plugin._history_support

    def _raise_catalog_error(
        *,
        rebuild_on_missing: bool,
        rebuild_on_corrupt: bool,
        rebuild_on_dirty: bool,
        rebuild_on_stale: bool,
        allow_missing_empty: bool,
        repair_tails: bool,
    ):
        raise ValueError("history contact catalog is stale; run 'hermes telegram-business history catalog --rebuild'")

    monkeypatch.setattr(history, "_load_contact_catalog", _raise_catalog_error)
    monkeypatch.setattr(
        history,
        "_load_records",
        lambda *_args, **_kwargs: pytest.fail("_load_records must not be used when catalog freshness fails"),
    )

    parser = _build_history_parser(plugin)
    exit_code = history.handle_cli(parser.parse_args(["history", "show", "--chat", "991", "--limit", "1"]))
    output = capsys.readouterr().out.strip()

    assert exit_code == 1
    assert "history contact catalog is stale" in output


@pytest.mark.asyncio
async def test_history_cli_contact_lookup_normalizes_unicode_nfkc_and_casefold(
    plugin,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    decomposed_name = "Cafe\u0301 Customer"
    composed_name = "Caf\u00e9 Customer"

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=991,
            chat_username=None,
            chat_first_name="Cafe\u0301",
            chat_last_name="Customer",
            from_user_username=None,
            from_user_first_name="Cafe\u0301",
            from_user_last_name="Customer",
            text="first unicode",
            update_id=1,
            date=base,
        ),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=991,
            chat_username=None,
            chat_first_name="Caf\u00e9",
            chat_last_name="Customer",
            from_user_username=None,
            from_user_first_name="Caf\u00e9",
            from_user_last_name="Customer",
            text="second unicode",
            update_id=2,
            date=base,
            edited=True,
        ),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    parser = _build_history_parser(plugin)

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "show", "--contact", composed_name, "--limit", "1"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert "second unicode" in output

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "show", "--contact", f"  {decomposed_name}  ", "--limit", "1"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert "second unicode" in output

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "contacts", "--search", "CAFE\u0301   customer"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert composed_name in output


@pytest.mark.asyncio
async def test_history_cli_chat_search_normalizes_unicode_nfkc_and_casefold(plugin, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    decomposed_text = "Cafe\u0301 follow-up"
    fullwidth_text = "Ｆｕｌｌｗｉｄｔｈ invoice"
    ascii_text = "plain ascii refund"

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text=decomposed_text, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text=fullwidth_text, update_id=2, message_id=78, date=base + timedelta(seconds=1)),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text=ascii_text, update_id=3, message_id=79, date=base + timedelta(seconds=2)),
        bot=FakeBot(),
        now=base + timedelta(seconds=2),
    )

    parser = _build_history_parser(plugin)

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "search", "--chat", "991", "--text", "CAF\u00c9", "--limit", "5"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert decomposed_text in output

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "search", "--chat", "991", "--text", "fullwidth invoice", "--limit", "5"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert fullwidth_text in output

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "search", "--chat", "991", "--text", "ASCII REFUND", "--limit", "5"])
    )
    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert ascii_text in output


@pytest.mark.asyncio
async def test_history_cli_global_search_normalizes_unicode_nfkc_and_casefold(
    plugin,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    decomposed_text = "Cafe\u0301 refund"
    fullwidth_text = "Ｆｕｌｌｗｉｄｔｈ receipt"
    ascii_text = "plain ascii refund"

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(chat_id=991, text=decomposed_text, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=992,
            chat_username="customer992",
            from_user_username="customer992",
            text=fullwidth_text,
            update_id=2,
            date=base + timedelta(seconds=1),
        ),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            chat_id=993,
            chat_username="customer993",
            from_user_username="customer993",
            text=ascii_text,
            update_id=3,
            date=base + timedelta(seconds=2),
        ),
        bot=FakeBot(),
        now=base + timedelta(seconds=2),
    )

    parser = _build_history_parser(plugin)

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "search", "--text", "caf\u00e9", "--limit", "5"])
    )
    output = capsys.readouterr().out.strip().splitlines()
    assert exit_code == 0
    assert any(decomposed_text in line for line in output)

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "search", "--text", "fullwidth receipt", "--limit", "5"])
    )
    output = capsys.readouterr().out.strip().splitlines()
    assert exit_code == 0
    assert any(fullwidth_text in line for line in output)

    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "search", "--text", "ASCII REFUND", "--limit", "5"])
    )
    output = capsys.readouterr().out.strip().splitlines()
    assert exit_code == 0
    assert any(ascii_text in line for line in output)


@pytest.mark.asyncio
async def test_history_cli_prunes_months_and_answers_contact_last_week_workflow(
    plugin,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    fixed_now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: fixed_now)
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            text="june recap",
            date=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc),
            update_id=1,
            chat_username="casey-weekly",
            from_user_username="casey-weekly",
        ),
        bot=FakeBot(),
        now=datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc),
    )
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(
            text="last week plan",
            date=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
            update_id=2,
            chat_username="casey-weekly",
            from_user_username="casey-weekly",
        ),
        bot=FakeBot(),
        now=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )

    selected: list[str] = []
    original_selector = plugin._history_support._iter_selected_history_files

    def _spy_selector(chat_dir: Path, *, since, until):
        paths = list(original_selector(chat_dir, since=since, until=until))
        selected.extend(path.name for path in paths)
        return iter(paths)

    monkeypatch.setattr(plugin._history_support, "_iter_selected_history_files", _spy_selector)
    parser = _build_history_parser(plugin)
    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "show", "--contact", "@casey-weekly", "--since", "7d", "--limit", "10"])
    )
    output = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert "last week plan" in output
    assert "june recap" not in output
    assert selected == ["2026-07.jsonl"]


@pytest.mark.asyncio
async def test_history_cli_global_search_is_bounded(plugin, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    for offset, chat_id, text in (
        (0, 991, "refund alpha"),
        (1, 992, "refund beta"),
        (2, 993, "refund gamma"),
    ):
        await plugin._history_support.observe_ptb_update(
            make_business_text_update(
                chat_id=chat_id,
                text=text,
                update_id=offset + 1,
                date=base + timedelta(seconds=offset),
                chat_username=f"customer-{chat_id}",
                from_user_username=f"customer-{chat_id}",
            ),
            bot=FakeBot(),
            now=base + timedelta(seconds=offset),
        )

    parser = _build_history_parser(plugin)
    exit_code = plugin._history_support.handle_cli(
        parser.parse_args(["history", "search", "--text", "refund", "--limit", "2"])
    )
    output = capsys.readouterr().out.strip().splitlines()

    assert exit_code == 0
    assert len(output) == 2
    assert "refund alpha" not in "\n".join(output)
    assert "refund beta" in output[0]
    assert "refund gamma" in output[1]


def test_streaming_query_scale_prunes_partitions_and_keeps_results_bounded(plugin, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    history = plugin._history_support
    records = []
    for month, total in ((6, 250), (7, 1250)):
        for index in range(total):
            observed_at = datetime(2026, month, 10, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=index)
            records.append(
                history._build_event(
                    event_type="message.created",
                    source="business_message",
                    observed_at=observed_at,
                    telegram_update_id=month * 10000 + index,
                    business_connection_id="business-123",
                    chat_id=991,
                    message_id=month * 10000 + index,
                    message_at=observed_at,
                    sender_id=2000,
                    direction="inbound",
                    reply_to_message_id=None,
                    text=f"needle record {month}-{index}",
                    chat_profile={"id": 991, "type": "private", "username": "scale-user", "first_name": "Scale"},
                    sender_profile={"id": 2000, "username": "scale-user", "first_name": "Scale"},
                )
            )
    append_raw_history_records(plugin, *records)
    history.rebuild_contact_catalog()

    selected: list[str] = []
    original_selector = history._iter_selected_history_files

    def _spy_selector(chat_dir: Path, *, since, until):
        paths = list(original_selector(chat_dir, since=since, until=until))
        selected.extend(path.name for path in paths)
        return iter(paths)

    monkeypatch.setattr(history, "_iter_selected_history_files", _spy_selector)
    parser = _build_history_parser(plugin)
    exit_code = history.handle_cli(
        parser.parse_args(
            [
                "history",
                "search",
                "--contact",
                "@scale-user",
                "--text",
                "needle",
                "--since",
                "2026-07-01T00:00:00Z",
                "--until",
                "2026-07-31T23:59:59Z",
                "--limit",
                "25",
            ]
        )
    )
    output = capsys.readouterr().out.strip().splitlines()

    assert exit_code == 0
    assert len(output) == 25
    assert selected == ["2026-07.jsonl"]
    assert all("needle record 7-" in line for line in output)


@pytest.mark.asyncio
async def test_history_cli_escapes_control_text_but_keeps_unicode_readable(enabled_history, capsys):
    plugin = enabled_history
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    text = "prefix\r\t\x1b[31mred\x00\x7f\x85\n🙂suffix"

    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text=text, message_id=77, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )

    parser = _build_history_parser(plugin)
    exit_code = plugin._history_support.handle_cli(parser.parse_args(["history", "show", "--chat", "991", "--limit", "1"]))
    output = capsys.readouterr().out.strip()

    assert exit_code == 0
    assert "\\r" in output
    assert "\\t" in output
    assert "\\x1b[31m" in output
    assert "\\x00" in output
    assert "\\x7f" in output
    assert "\\x85" in output
    assert "\\n" in output
    assert "🙂suffix" in output
    assert "\r" not in output
    assert "\t" not in output
    assert "\x1b" not in output
    assert "\x00" not in output
    assert "\x7f" not in output
    assert "\x85" not in output


def test_history_cli_deletions_streams_latest_classified_rows_without_full_load(plugin, monkeypatch: pytest.MonkeyPatch, capsys):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    records: list[dict[str, Any]] = []
    update_id = 1

    for business_id in ("business-123", "business-456"):
        for index in range(1800):
            observed_at = base + timedelta(seconds=index)
            records.append(
                make_legacy_history_record(
                    plugin,
                    event_type="message.created",
                    source="business_message",
                    observed_at=observed_at,
                    telegram_update_id=update_id,
                    business_connection_id=business_id,
                    chat_id=991,
                    message_id=10000 + index,
                    message_at=observed_at,
                    sender_id=2000,
                    direction="inbound",
                    text=f"ordinary {business_id} {index}",
                )
            )
            update_id += 1

    for index in range(6):
        observed_at = base + timedelta(hours=1, seconds=index)
        records.append(
            make_legacy_history_record(
                plugin,
                event_type="deletion.classified",
                source="deleted_business_messages",
                observed_at=observed_at,
                telegram_update_id=update_id,
                business_connection_id="business-123",
                chat_id=991,
                message_id=8000 + index,
                message_at=observed_at,
                sender_id=2000,
                direction="inbound",
                deleted_event_id=f"deleted-business-123-{index}",
                classification="unexplained",
                classification_reason="no_strong_match",
                evaluated_at=observed_at,
                deleted_observed_at=observed_at - timedelta(seconds=10),
            )
        )
        update_id += 1

    for index in range(6):
        observed_at = base + timedelta(hours=2, seconds=index)
        records.append(
            make_legacy_history_record(
                plugin,
                event_type="deletion.classified",
                source="deleted_business_messages",
                observed_at=observed_at,
                telegram_update_id=update_id,
                business_connection_id="business-456",
                chat_id=991,
                message_id=9000 + index,
                message_at=observed_at,
                sender_id=2000,
                direction="inbound",
                deleted_event_id=f"deleted-business-456-{index}",
                classification="unexplained",
                classification_reason="no_strong_match",
                evaluated_at=observed_at,
                deleted_observed_at=observed_at - timedelta(seconds=10),
            )
        )
        update_id += 1

    append_raw_history_records(plugin, *records)
    history.rebuild_contact_catalog()

    monkeypatch.setattr(
        history,
        "_load_records",
        lambda *_args, **_kwargs: pytest.fail("_load_records must not be used by history deletions"),
    )
    monkeypatch.setattr(
        history,
        "_build_chat_state",
        lambda *_args, **_kwargs: pytest.fail("_build_chat_state must not be used by history deletions"),
    )

    parser = _build_history_parser(plugin)

    exit_code = history.handle_cli(parser.parse_args(["history", "deletions", "--status", "unexplained", "--chat", "991"]))
    output = capsys.readouterr().out.strip()
    assert exit_code == 1
    assert "pass --connection explicitly" in output

    exit_code = history.handle_cli(
        parser.parse_args(
            ["history", "deletions", "--status", "unexplained", "--connection", "business-456", "--chat", "991", "--limit", "4"]
        )
    )
    output = capsys.readouterr().out.strip().splitlines()

    assert exit_code == 0
    assert len(output) == 4
    assert all("connection=business-456" in line for line in output)
    assert all("status=unexplained" in line for line in output)
    assert [int(line.split("message=")[1].split()[0]) for line in output] == [9002, 9003, 9004, 9005]


def test_history_cli_deletions_pending_streams_live_tombstones_with_bounded_state(
    plugin,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    history = plugin._history_support
    base = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    records: list[dict[str, Any]] = []
    update_id = 1

    for index in range(2500):
        observed_at = base + timedelta(seconds=index)
        records.append(
            make_legacy_history_record(
                plugin,
                event_type="message.created",
                source="business_message",
                observed_at=observed_at,
                telegram_update_id=update_id,
                business_connection_id="business-123",
                chat_id=991,
                message_id=20000 + index,
                message_at=observed_at,
                sender_id=2000,
                direction="inbound",
                text=f"ordinary pending {index}",
            )
        )
        update_id += 1

    deleted_one = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=501,
        message_at=None,
        sender_id=None,
        direction="unknown",
    )
    update_id += 1
    deleted_two = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1, seconds=1),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=502,
        message_at=None,
        sender_id=None,
        direction="unknown",
    )
    update_id += 1
    classified_two = make_legacy_history_record(
        plugin,
        event_type="deletion.classified",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1, seconds=31),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=502,
        message_at=None,
        sender_id=None,
        direction="unknown",
        deleted_event_id=str(deleted_two["event_id"]),
        classification="unexplained",
        classification_reason="no_strong_match",
        evaluated_at=base + timedelta(hours=1, seconds=31),
        deleted_observed_at=base + timedelta(hours=1, seconds=1),
    )
    update_id += 1
    deleted_three = make_legacy_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=base + timedelta(hours=1, seconds=2),
        telegram_update_id=update_id,
        business_connection_id="business-123",
        chat_id=991,
        message_id=503,
        message_at=None,
        sender_id=None,
        direction="unknown",
    )

    append_raw_history_records(plugin, *records, deleted_one, deleted_two, classified_two, deleted_three)
    history.rebuild_contact_catalog()

    monkeypatch.setattr(
        history,
        "_load_records",
        lambda *_args, **_kwargs: pytest.fail("_load_records must not be used by pending deletions"),
    )
    monkeypatch.setattr(
        history,
        "_build_chat_state",
        lambda *_args, **_kwargs: pytest.fail("_build_chat_state must not be used by pending deletions"),
    )

    rows, collector = history._latest_deletion_rows(
        history._history_entries_for_cli(business_connection_id="business-123", chat_id="991"),
        status="pending",
        limit=1,
    )

    assert len(rows) == 1
    assert rows[0]["message_id"] == 503
    assert collector.max_heap_size == 1
    assert collector.max_live_pending == 2

    parser = _build_history_parser(plugin)
    exit_code = history.handle_cli(
        parser.parse_args(["history", "deletions", "--status", "pending", "--connection", "business-123", "--chat", "991", "--limit", "1"])
    )
    output = capsys.readouterr().out.strip().splitlines()

    assert exit_code == 0
    assert output == [
        f"{deleted_three['observed_at']} message.deleted connection=business-123 chat=991 message=503 status=pending"
    ]


class FakeApplication:
    def __init__(self):
        self.handlers: list[tuple[int, Any]] = []

    def add_handler(self, handler, group=0):
        self.handlers.append((group, handler))
        return handler

    async def process_update(self, update, *, bot):
        seen_groups = []
        background_tasks = []
        for group in sorted({entry[0] for entry in self.handlers}):
            for handler_group, handler in [entry for entry in self.handlers if entry[0] == group]:
                checker = getattr(handler, "check_update", None)
                if callable(checker) and not checker(update):
                    continue
                seen_groups.append(handler_group)
                context = SimpleNamespace(bot=bot)
                if hasattr(handler, "handle_update"):
                    coroutine = handler.handle_update(update, self, True, context)
                else:
                    coroutine = handler.callback(update, context)
                if getattr(handler, "block", True):
                    await coroutine
                else:
                    background_tasks.append(asyncio.create_task(coroutine))
                break
        if background_tasks:
            await asyncio.gather(*background_tasks)
        return seen_groups


class FakeGroupZeroHandler:
    def __init__(self, callback):
        self.callback = callback

    def check_update(self, _update):
        return True

    async def handle_update(self, update, _application, _check_result, context):
        return await self.callback(update, context)


def _install_raw_handler_test_adapter(plugin, monkeypatch: pytest.MonkeyPatch):
    class FakeAdapter:
        def _is_user_authorized_from_message(self, _message):
            return False

        async def _handle_media_message(self, update, _context):
            return update

    telegram_adapter = types.SimpleNamespace(
        TelegramAdapter=FakeAdapter,
        TelegramMessageHandler=lambda handler_filter, callback, *args, **kwargs: (handler_filter, callback, args, kwargs),
        Application=FakeApplication,
        filters=SimpleNamespace(VIDEO_NOTE=16),
    )
    monkeypatch.setattr(plugin, "_resolve_telegram_adapter_module", lambda: telegram_adapter)
    return telegram_adapter


@pytest.mark.asyncio
async def test_raw_ptb_handler_installs_in_separate_group_and_does_not_block_group_zero(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    telegram_adapter = _install_raw_handler_test_adapter(plugin, monkeypatch)
    plugin._install_telegram_adapter_compat()

    app = telegram_adapter.Application()
    called = []

    async def group_zero(update, _context):
        called.append(update.update_id)

    app.add_handler(FakeGroupZeroHandler(group_zero), group=0)
    groups = [group for group, _handler in app.handlers]
    assert plugin._RAW_HISTORY_HANDLER_GROUP in groups
    assert 0 in groups

    seen_groups = await app.process_update(
        make_business_text_update(text="group", message_id=77, update_id=1),
        bot=FakeBot(),
    )

    assert seen_groups[0] == plugin._RAW_HISTORY_HANDLER_GROUP
    assert seen_groups[1] == 0
    assert called == [1]
    assert load_records(plugin)[0]["text"] == "group"


@pytest.mark.asyncio
async def test_raw_ptb_history_handler_completes_before_group_zero_and_group_zero_still_runs(
    enabled_history,
):
    plugin = enabled_history
    raw_handler = plugin._build_raw_business_update_handler()
    assert bool(raw_handler.block) is True
    order: list[str] = []

    class LocalApplication:
        def __init__(self):
            self.handlers: list[tuple[int, Any]] = []

        def add_handler(self, handler, group=0):
            self.handlers.append((group, handler))
            return handler

        async def process_update(self, update, *, bot):
            seen_groups = []
            background_tasks = []
            for group in sorted({entry[0] for entry in self.handlers}):
                for handler_group, handler in [entry for entry in self.handlers if entry[0] == group]:
                    checker = getattr(handler, "check_update", None)
                    if callable(checker) and not checker(update):
                        continue
                    seen_groups.append(handler_group)
                    context = SimpleNamespace(bot=bot)
                    if hasattr(handler, "handle_update"):
                        coroutine = handler.handle_update(update, self, True, context)
                    else:
                        coroutine = handler.callback(update, context)
                    if getattr(handler, "block", True):
                        await coroutine
                    else:
                        background_tasks.append(asyncio.create_task(coroutine))
                    break
            if background_tasks:
                await asyncio.gather(*background_tasks)
            return seen_groups

    class BlockingHistoryHandler:
        block = raw_handler.block

        def check_update(self, _update):
            return True

        async def callback(self, update, _context):
            order.append(f"history-start:{update.update_id}")
            await asyncio.sleep(0)
            order.append(f"history-done:{update.update_id}")

    async def group_zero(update, _context):
        order.append(f"group-zero:{update.update_id}")

    app = LocalApplication()
    app.add_handler(BlockingHistoryHandler(), group=plugin._RAW_HISTORY_HANDLER_GROUP)
    app.add_handler(FakeGroupZeroHandler(group_zero), group=0)
    seen_groups = await app.process_update(
        make_business_text_update(text="group", message_id=77, update_id=1),
        bot=FakeBot(),
    )

    assert seen_groups == [plugin._RAW_HISTORY_HANDLER_GROUP, 0]
    assert order == ["history-start:1", "history-done:1", "group-zero:1"]


def test_real_ptb_raw_handler_is_blocking_when_ptb_is_importable(plugin):
    telegram_ext = pytest.importorskip("telegram.ext")

    raw_handler = plugin._build_raw_business_update_handler()

    assert isinstance(raw_handler, telegram_ext.BaseHandler)
    assert bool(raw_handler.block) is True


@pytest.mark.asyncio
async def test_raw_ptb_handler_failure_is_contained_and_group_zero_still_runs(
    enabled_history,
    monkeypatch: pytest.MonkeyPatch,
):
    plugin = enabled_history
    telegram_adapter = _install_raw_handler_test_adapter(plugin, monkeypatch)
    plugin._install_telegram_adapter_compat()
    monkeypatch.setattr(
        plugin._history_support,
        "observe_ptb_update",
        AsyncMock(side_effect=RuntimeError("history broken")),
    )

    app = telegram_adapter.Application()
    called = []

    async def group_zero(update, _context):
        called.append(update.update_id)

    app.add_handler(FakeGroupZeroHandler(group_zero), group=0)
    await app.process_update(
        make_business_text_update(text="still routed", message_id=77, update_id=2),
        bot=FakeBot(),
    )

    assert called == [2]
