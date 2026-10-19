"""`hermes telesec history import`: a Telegram Desktop chat export into history. Made-up data only."""

from __future__ import annotations

import dataclasses
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_history as th
from test_history import (
    FakeBot,
    _run_history_cli,
    load_catalog,
    load_records,
    make_business_text_update,
    set_switch,
    snapshot_history_state,
)

plugin = th.plugin
enabled_history = th.enabled_history

CHAT = 991  # the contact's user id is the private chat's id
OWNER = 1000
LIVE_AT = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)


def at(*args: int) -> str:
    return str(int(datetime(*args, tzinfo=UTC).timestamp()))


def message(message_id: int, sent: str, sender: int, text: Any = "", **extra: Any) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "date": "2026-06-01T12:00:00",
        "date_unixtime": sent,
        "from": "Casey" if sender == CHAT else "Owner",
        "from_id": f"user{sender}",
        "text": text,
        "text_entities": [],
        **extra,
    }


def write_export(tmp_path: Path, messages: list[dict[str, Any]], *, chat_id: int = CHAT,
                 chat_type: str = "personal_chat") -> Path:
    path = tmp_path / "export" / "result.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"name": "Casey", "type": chat_type, "id": chat_id, "messages": messages}),
                    encoding="utf-8")
    return path


def sample_messages() -> list[dict[str, Any]]:
    return [
        message(10, at(2026, 6, 2, 9, 0), CHAT, "hello from June"),
        message(11, at(2026, 6, 2, 9, 5), OWNER, [{"type": "bold", "text": "hi"}, " there"], reply_to_message_id=10),
        message(12, at(2026, 6, 3, 8, 0), CHAT, "fixed typo", edited="x", edited_unixtime=at(2026, 6, 3, 8, 2)),
        message(13, at(2026, 6, 4, 8, 0), CHAT, "", media_type="voice_message", file="(not included)"),
        message(14, at(2026, 6, 4, 8, 1), OWNER, "a caption", photo="(not included)"),
        {"id": 15, "type": "service", "date_unixtime": at(2026, 6, 5, 8, 0), "actor_id": f"user{OWNER}",
         "action": "phone_call", "text": "", "text_entities": []},
        message(16, at(2026, 6, 6, 8, 0), OWNER, "/stats 30d"),
        message(17, at(2026, 7, 1, 8, 0), CHAT, "early July"),
        message(18, at(2026, 7, 18, 8, 0), OWNER, "day before capture"),
        message(77, at(2026, 7, 19, 10, 0), CHAT, "seen live"),
        message(78, at(2026, 7, 20, 8, 0), CHAT, "after capture started"),
    ]


async def capture_live(plugin) -> None:
    await plugin._history_support.observe_ptb_update(
        make_business_text_update(text="seen live", chat_id=CHAT, from_user_id=CHAT, message_id=77, date=LIVE_AT),
        bot=FakeBot(owner_id=OWNER),
        now=LIVE_AT,
    )


@pytest.mark.asyncio
async def test_import_fills_history_before_capture_in_order_and_is_idempotent(enabled_history, tmp_path):
    plugin = enabled_history
    await capture_live(plugin)
    path = write_export(tmp_path, sample_messages())

    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT))

    assert code == 0, output
    assert output.splitlines() == [
        "Imported 5 messages (6 records) into chat 991 (connection business-123), sent 2026-06-02 to 2026-07-18 (UTC).",
        ("Skipped: 1 already saved, 1 newer than saved history, 1 voice, round video or audio (not transcribed), "
         "1 other media, 1 service message, 1 Telesec command."),
    ]
    records = load_records(plugin, chat_id=CHAT)
    assert [(r["message_id"], r["event_type"], r["source"]) for r in records] == [
        (10, "message.created", "import"),
        (11, "message.created", "import"),
        (12, "message.created", "import"),
        (12, "message.edited", "import"),
        (17, "message.created", "import"),
        (18, "message.created", "import"),
        (77, "message.created", "business_message"),
    ]
    by_id = {(r["message_id"], r["event_type"]): r for r in records}
    assert by_id[(10, "message.created")]["direction"] == "inbound"
    assert by_id[(10, "message.created")]["sender_id"] == CHAT
    assert by_id[(11, "message.created")]["direction"] == "outbound"
    assert by_id[(11, "message.created")]["sender_id"] == OWNER
    assert by_id[(11, "message.created")]["text"] == "hi there"
    assert by_id[(11, "message.created")]["reply_to_message_id"] == 10
    assert by_id[(10, "message.created")]["observed_at"] == by_id[(10, "message.created")]["message_at"]
    # The export has only the final text of an edited message.
    assert "text" not in by_id[(12, "message.created")]
    assert by_id[(12, "message.edited")]["text"] == "fixed typo"
    assert by_id[(12, "message.edited")]["observed_at"] == "2026-06-03T08:02:00Z"
    assert all("chat_profile" not in r for r in records if r["source"] == "import")
    history = plugin._history_support
    chat_dir = history._history_chat_dir_path("business-123", CHAT)
    assert [p.name for p in history._iter_history_files(chat_dir)] == ["2026-06.jsonl", "2026-07.jsonl"]

    entry = load_catalog(plugin)["contacts"][0]
    assert entry["message_count"] == 6
    assert entry["edit_count"] == 1
    assert entry["first_seen_at"] == "2026-06-02T09:00:00Z"
    assert entry["current_profile"]["username"] == "customer991"
    assert history.verify_history().ok
    assert history._contact_catalog_status().state == "ok"

    before = snapshot_history_state(plugin)
    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT))
    assert code == 0, output
    assert output.splitlines()[0] == "Nothing to import into chat 991 (connection business-123)."
    assert output.splitlines()[1].startswith("Skipped: 6 already saved, 1 newer than saved history, ")
    assert snapshot_history_state(plugin) == before


@pytest.mark.asyncio
async def test_imported_messages_count_in_stats(enabled_history, tmp_path):
    plugin = enabled_history
    await capture_live(plugin)
    path = write_export(tmp_path, sample_messages())
    assert _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT))[0] == 0

    code, output = _run_history_cli(plugin, "stats", "--chat", str(CHAT), "--format", "json", "--tz", "UTC")

    assert code == 0, output
    report = json.loads(output)
    contact = report["contacts"][0] if "contacts" in report else report
    assert contact["messages"]["received"] == 4
    assert contact["messages"]["sent"] == 2
    assert contact["edited"]["total"] == 1
    assert contact["missing_history"]["retained_from"].startswith("2026-06-02")


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(enabled_history, tmp_path):
    plugin = enabled_history
    await capture_live(plugin)
    path = write_export(tmp_path, sample_messages())
    before = snapshot_history_state(plugin)

    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT), "--dry-run")

    assert code == 0, output
    lines = output.splitlines()
    assert lines[0].startswith("Would import 5 messages (6 records) into chat 991")
    assert lines[-1] == "Dry run: nothing was written."
    assert snapshot_history_state(plugin) == before


@pytest.mark.asyncio
async def test_import_writes_only_the_chat_history_files(enabled_history, tmp_path):
    plugin = enabled_history
    await capture_live(plugin)
    path = write_export(tmp_path, sample_messages())

    assert _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT))[0] == 0

    history = plugin._history_support
    root = history.history_root()
    chat_dir = history._history_chat_dir_path("business-123", CHAT).relative_to(root).as_posix()
    files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert files == {".lock", "contacts.json", f"{chat_dir}/.lock", f"{chat_dir}/2026-06.jsonl",
                     f"{chat_dir}/2026-07.jsonl"}


def test_import_without_saved_history_needs_a_known_connection(enabled_history, tmp_path):
    plugin = enabled_history
    path = write_export(tmp_path, sample_messages()[:3])

    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT))
    assert (code, output) == (1, "error: chat 991 has no saved history; pass --connection")
    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT),
                                    "--connection", "business-123")
    assert (code, output) == (1, "error: unknown connection business-123: it has no saved history and no known owner")
    assert not plugin._history_support.history_root().exists()

    plugin._transcription_modes.default_store().bind_connection("business-123", OWNER)
    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT),
                                    "--connection", "business-123")

    assert code == 0, output
    entry = load_catalog(plugin)["contacts"][0]
    assert (entry["chat_id"], entry["chat_type"], entry["message_count"]) == (CHAT, "private", 3)
    assert {r["chat_profile"]["type"] for r in load_records(plugin, chat_id=CHAT)} == {"private"}


@pytest.mark.asyncio
async def test_import_refuses_when_history_is_not_saved(enabled_history, tmp_path):
    plugin = enabled_history
    await capture_live(plugin)
    path = write_export(tmp_path, sample_messages())
    before = snapshot_history_state(plugin)

    plugin._chat_settings.default_store().set_flag(OWNER, CHAT, "history", False, via="cli")
    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT))
    assert (code, output) == (1, "error: chat 991 is set to Don't save; turn Save history on for it first")

    set_switch(plugin, "history", False)
    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT), "--dry-run")
    assert (code, output) == (1, "error: history is off in Settings; turn on Save history first")
    assert snapshot_history_state(plugin) == before


@pytest.mark.asyncio
async def test_import_skips_messages_older_than_retention(enabled_history, tmp_path, monkeypatch):
    plugin = enabled_history
    await capture_live(plugin)
    history = plugin._history_support
    config = history.history_config_from_env()
    monkeypatch.setattr(history, "history_config_from_env", lambda: dataclasses.replace(config, retention_days=30))
    path = write_export(tmp_path, sample_messages())

    history_import = importlib.import_module(f"{plugin.__name__}.history_import")
    result = history_import.run_import(path, chat=str(CHAT), now=datetime(2026, 7, 25, tzinfo=UTC))

    assert [r["message_id"] for r in result.records] == [17, 18]
    assert result.skipped["retention"] == 3


@pytest.mark.parametrize(
    ("export", "error"),
    [
        ({"chat_id": 992}, "error: this export is chat 992, not 991"),
        ({"chat_type": "private_group"}, "error: only a one-to-one chat can be imported; this export is a private_group"),
    ],
)
@pytest.mark.asyncio
async def test_import_refuses_an_export_of_another_chat(enabled_history, tmp_path, export, error):
    plugin = enabled_history
    await capture_live(plugin)
    path = write_export(tmp_path, sample_messages(), chat_id=export.get("chat_id", CHAT),
                       chat_type=export.get("chat_type", "personal_chat"))

    assert _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT)) == (1, error)


@pytest.mark.asyncio
async def test_import_refuses_an_export_made_by_another_account(enabled_history, tmp_path):
    plugin = enabled_history
    await capture_live(plugin)
    messages = [message(10, at(2026, 6, 2, 9, 0), CHAT, "hello"), message(11, at(2026, 6, 2, 9, 5), 3000, "hi")]
    path = write_export(tmp_path, messages)

    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT))

    assert (code, output) == (1, "error: the export was made by another account, not the owner of this connection")


@pytest.mark.asyncio
async def test_import_skips_a_bot_posting_in_the_chat(enabled_history, tmp_path):
    plugin = enabled_history
    await capture_live(plugin)
    bot_post = message(19, at(2026, 6, 7, 8, 0), 5000, "a long reply from a bot", reply_to_message_id=10)
    path = write_export(tmp_path, [*sample_messages(), bot_post])

    code, output = _run_history_cli(plugin, "history", "import", str(path), "--chat", str(CHAT), "--dry-run")

    assert code == 0, output
    assert output.splitlines()[0].startswith("Would import 5 messages (6 records)")
    assert "1 from a bot or a third account" in output.splitlines()[1]


def test_import_refuses_a_file_that_is_not_a_chat_export(enabled_history, tmp_path):
    plugin = enabled_history
    full = tmp_path / "full.json"
    full.write_text(json.dumps({"about": "x", "chats": {"list": []}}), encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")

    code, output = _run_history_cli(plugin, "history", "import", str(full), "--chat", str(CHAT))
    assert (code, output) == (1, "error: this is a full account export; export the one chat with Export chat history")
    code, output = _run_history_cli(plugin, "history", "import", str(broken), "--chat", str(CHAT))
    assert code == 1 and output.startswith("error: broken.json is not a JSON export")


def test_a_fuller_export_adds_only_new_messages_in_send_order(enabled_history, tmp_path):
    plugin = enabled_history
    plugin._transcription_modes.default_store().bind_connection("business-123", OWNER)
    first, second, third = sample_messages()[:3]
    argv = ("history", "import", "--chat", str(CHAT), "--connection", "business-123")
    assert _run_history_cli(plugin, *argv[:2], str(write_export(tmp_path, [first, third])), *argv[2:])[0] == 0

    code, output = _run_history_cli(plugin, *argv[:2], str(write_export(tmp_path, [first, second, third])), *argv[2:])

    assert code == 0, output
    assert output.splitlines() == [
        "Imported 1 message (1 record) into chat 991 (connection business-123), sent 2026-06-02 to 2026-06-02 (UTC).",
        "Skipped: 2 already saved.",
    ]
    records = load_records(plugin, chat_id=CHAT)
    assert [(r["message_id"], r["event_type"]) for r in records] == [
        (10, "message.created"), (11, "message.created"), (12, "message.created"), (12, "message.edited"),
    ]
    history = plugin._history_support
    assert history.verify_history().ok
    assert load_catalog(plugin)["contacts"][0]["message_count"] == 3
    assert not list(history.history_root().rglob("*.import-tmp"))
