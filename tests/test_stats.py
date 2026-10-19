from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import itertools
import json
import re
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
import test_history as th
from test_history import (
    FakeBot,
    _build_history_parser,
    _run_history_cli,
    append_raw_history_records,
    catalog_dirty_path,
    make_business_media_update,
    make_business_text_update,
    make_deleted_update,
    make_history_record,
    remove_history_locks,
    snapshot_history_state,
)

plugin = th.plugin
enabled_history = th.enabled_history

ROOT = Path(__file__).resolve().parents[1]
STATS_DOC = ROOT / "docs" / "STATS.md"
HISTORY_DOC = ROOT / "docs" / "HISTORY.md"
README_DOC = ROOT / "README.md"
SKILL_DOC = ROOT / "skills" / "history" / "SKILL.md"
_FENCED_BLOCK_RE = re.compile(r"^```(\w+)\n(.*?)\n```$", flags=re.MULTILINE | re.DOTALL)

NOW = datetime(2026, 10, 19, 11, 32, tzinfo=UTC)
TIMEZONE_ENV = "TELESEC_TIMEZONE"
ALICE = {"id": 991, "type": "private", "username": "alice", "first_name": "Alice", "last_name": "Example"}
BOB = {"id": 992, "type": "private", "username": "bob", "first_name": "Bob", "last_name": "Builder"}
CAROL = {"id": 994, "type": "private", "first_name": "Carol"}
DAVE = {"id": 995, "type": "private", "username": "dave", "first_name": "Dave"}
MOSCOW_RANGE = ("--since", "2026-10-01", "--until", "2026-10-18", "--tz", "Europe/Moscow")
_UPDATE_IDS = itertools.count(10_000)
_UNSET: Any = object()

ALICE_MOSCOW = """
range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only
Alice Example (@alice) connection=business-123 chat=991
  messages 9: received 5, sent 3, unknown direction 1
  active days 4 (received 3, sent 3)
  latest 2026-10-17 21:04 received; last sent 2026-10-17 19:30
  last message: received 2026-10-17 21:04, no observed reply after it
  sessions 4 (gap 6h): started by contact 2, by owner 2; ended on received 2
  deletions 4: unexplained 1 (received 1, sent 0), likely correction 1, likely duplicate 1, pending 1
  missing history: retained from 2026-10-03 (range is partial); 1 deletion of a message not in history; 1 message known only from an edit
  deletion details: hermes telesec history deletions --connection business-123 --chat 991 --status unexplained
""".strip("\n")

OVERVIEW_MOSCOW = """
range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only
latest            last      msgs  recv  sent  unk  days  sess  del  unexpl  partial  connection    chat  contact
2026-10-18 09:15  sent         2     1     1    0     1     1    0       0  yes      business-123  992   Bob Builder (@bob)
2026-10-17 21:04  received     9     5     3    1     4     4    4       1  yes      business-123  991   Alice Example (@alice)
-                 -            0     0     0    0     0     0    1       0  no       business-456  994   Carol
shown 3 of 4 contacts; not shown: 1 with no activity in range
missing history in shown contacts: 1 deletion of a message not in history; 1 message known only from an edit; use --contact for details
deletion details: hermes telesec history deletions --status unexplained
""".strip("\n")

OVERVIEW_MOSCOW_LIMIT_1 = """
range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only
latest            last  msgs  recv  sent  unk  days  sess  del  unexpl  partial  connection    chat  contact
2026-10-18 09:15  sent     2     1     1    0     1     1    0       0  yes      business-123  992   Bob Builder (@bob)
shown 1 of 4 contacts; not shown: 2 over --limit 1, 1 with no activity in range
""".strip("\n")


@pytest.fixture(autouse=True)
def _fixed_clock(plugin, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(TIMEZONE_ENV, raising=False)
    monkeypatch.setattr(plugin._history_support, "_utcnow", lambda: NOW)


@pytest.fixture
def cs(plugin):
    return importlib.import_module(f"{plugin.__name__}.correspondence_stats")


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def _msg(
    plugin,
    mid: int,
    at: datetime | None,
    direction: str,
    *,
    edited: bool = False,
    observed: datetime | None = None,
    message_at: Any = _UNSET,
    conn: str = "business-123",
    chat: int = 991,
    profile: dict[str, Any] | None = ALICE,
) -> dict[str, Any]:
    assert at is not None or observed is not None
    record = make_history_record(
        plugin,
        event_type="message.edited" if edited else "message.created",
        source="edited_business_message" if edited else "business_message",
        observed_at=observed or at + timedelta(seconds=1),
        telegram_update_id=next(_UPDATE_IDS),
        business_connection_id=conn,
        chat_id=chat,
        message_id=mid,
        message_at=at if message_at is _UNSET else message_at,
        direction=direction,
        text=f"SECRET-{mid}",
    )
    if profile is not None:
        # Profiles sit outside the event_id signature, so they can be attached afterwards.
        record["chat_profile"] = dict(profile, id=chat)
    return record


def _tomb(
    plugin,
    mid: int,
    observed: datetime,
    direction: str = "unknown",
    *,
    message_at: datetime | None = None,
    conn: str = "business-123",
    chat: int = 991,
) -> dict[str, Any]:
    return make_history_record(
        plugin,
        event_type="message.deleted",
        source="deleted_business_messages",
        observed_at=observed,
        telegram_update_id=next(_UPDATE_IDS),
        business_connection_id=conn,
        chat_id=chat,
        message_id=mid,
        message_at=message_at,
        sender_id=None if message_at is None else 2000,
        direction=direction,
    )


def _cls(
    plugin,
    tomb: dict[str, Any],
    classification: str,
    observed: datetime,
    reason: str,
    *,
    replacement: int | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    history = plugin._history_support
    return make_history_record(
        plugin,
        event_type="deletion.classified",
        source=tomb["source"],
        observed_at=observed,
        telegram_update_id=tomb["telegram_update_id"],
        business_connection_id=tomb["business_connection_id"],
        chat_id=tomb["chat_id"],
        message_id=tomb["message_id"],
        message_at=history._parse_datetime(tomb["message_at"]),
        sender_id=tomb["sender_id"],
        direction=tomb["direction"],
        deleted_event_id=tomb["event_id"],
        classification=classification,
        replacement_message_id=replacement,
        classification_reason=reason,
        classification_score=score,
        evaluated_at=observed,
        deleted_observed_at=history._parse_datetime(tomb["observed_at"]),
    )


def _profile(chat: int, username: str) -> dict[str, Any]:
    return {"id": chat, "type": "private", "username": username, "first_name": username.title()}


def _seed(plugin, *records: dict[str, Any]) -> None:
    append_raw_history_records(plugin, *records)
    plugin._history_support.rebuild_contact_catalog()


def _stats(plugin, *argv: str) -> tuple[int, str]:
    return _run_history_cli(plugin, "stats", *argv)


def _stats_json(plugin, *argv: str) -> dict[str, Any]:
    exit_code, output = _stats(plugin, *argv, "--format", "json")
    assert exit_code == 0, output
    return json.loads(output)


def _contact_json(plugin, *argv: str) -> dict[str, Any]:
    document = _stats_json(plugin, *argv)
    assert document["mode"] == "contact"
    return document["contacts"][0]


def _entry(
    *,
    conn: str = "business-123",
    chat: int = 991,
    profile: dict[str, Any] = ALICE,
    first_seen_at: str | None = None,
) -> dict[str, Any]:
    return {
        "business_connection_id": conn,
        "chat_id": chat,
        "chat_type": "private",
        "current_profile": dict(profile, id=chat),
        "first_seen_at": first_seen_at,
    }


def _window(cs, *, since: str | None = None, until: str | None = None, tz: str | None = None, gap: str | None = None):
    args = argparse.Namespace(since=since, until=until, tz=tz, session_gap=gap)
    return cs.build_window(args, now=NOW, environ={})


def _alice_records(plugin) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def add(record: dict[str, Any]) -> dict[str, Any]:
        records.append(record)
        return record

    add(_msg(plugin, 101, _utc(2026, 10, 3, 8, 0), "inbound", observed=_utc(2026, 10, 3, 8, 0, 5)))
    add(_msg(plugin, 102, _utc(2026, 10, 3, 8, 30), "outbound", observed=_utc(2026, 10, 3, 8, 30, 1)))
    add(_msg(plugin, 103, _utc(2026, 10, 3, 20, 0), "inbound", observed=_utc(2026, 10, 3, 20, 0, 1)))
    add(_msg(plugin, 104, _utc(2026, 10, 3, 21, 30), "unknown", observed=_utc(2026, 10, 3, 21, 30, 1)))
    add(_msg(plugin, 104, _utc(2026, 10, 3, 21, 30), "unknown", edited=True, observed=_utc(2026, 10, 3, 21, 40)))
    add(_msg(plugin, 100, _utc(2026, 10, 2, 10, 0), "inbound", edited=True, observed=_utc(2026, 10, 4, 7, 0)))
    add(_msg(plugin, 105, _utc(2026, 10, 10, 9, 0), "unknown", observed=_utc(2026, 10, 10, 9, 0, 2)))
    add(_msg(plugin, 105, _utc(2026, 10, 10, 9, 0), "outbound", edited=True, observed=_utc(2026, 10, 10, 9, 5)))
    add(_msg(plugin, 106, _utc(2026, 10, 10, 9, 10), "inbound", observed=_utc(2026, 10, 10, 9, 10, 1)))
    t106 = add(_tomb(plugin, 106, _utc(2026, 10, 10, 9, 11), "inbound", message_at=_utc(2026, 10, 10, 9, 10)))
    add(_msg(plugin, 107, _utc(2026, 10, 10, 9, 12), "inbound", observed=_utc(2026, 10, 10, 9, 12, 1)))
    t107 = add(_tomb(plugin, 107, _utc(2026, 10, 10, 9, 12, 30), "inbound", message_at=_utc(2026, 10, 10, 9, 12)))
    add(_cls(plugin, t106, "unexplained", _utc(2026, 10, 10, 9, 13), "no_strong_match"))
    add(_msg(plugin, 108, _utc(2026, 10, 10, 9, 13), "inbound", observed=_utc(2026, 10, 10, 9, 13, 1)))
    add(
        _cls(
            plugin,
            t107,
            "likely_duplicate",
            _utc(2026, 10, 10, 9, 14),
            "normalized_exact_duplicate",
            replacement=108,
        )
    )
    t108 = add(_tomb(plugin, 108, _utc(2026, 10, 10, 9, 14), "inbound", message_at=_utc(2026, 10, 10, 9, 13)))
    add(_msg(plugin, 109, _utc(2026, 10, 10, 9, 14, 30), "inbound", observed=_utc(2026, 10, 10, 9, 14, 31)))
    add(
        _cls(
            plugin,
            t108,
            "likely_correction",
            _utc(2026, 10, 10, 9, 16),
            "high_similarity_small_edit",
            replacement=109,
            score=0.93,
        )
    )
    t200 = add(_tomb(plugin, 200, _utc(2026, 10, 12, 10, 0), "unknown"))
    add(_cls(plugin, t200, "unclassifiable", _utc(2026, 10, 12, 10, 2), "missing_original"))
    add(_msg(plugin, 110, _utc(2026, 10, 17, 16, 30), "outbound", observed=_utc(2026, 10, 17, 16, 30, 1)))
    add(_msg(plugin, 111, _utc(2026, 10, 17, 18, 4), "inbound", observed=_utc(2026, 10, 17, 18, 4, 2)))
    add(_tomb(plugin, 102, _utc(2026, 10, 17, 19, 0), "outbound", message_at=_utc(2026, 10, 3, 8, 30)))
    add(_msg(plugin, 112, _utc(2026, 10, 18, 21, 0), "inbound", observed=_utc(2026, 10, 18, 21, 0, 2)))
    return records


def _overview_extra_records(plugin) -> list[dict[str, Any]]:
    carol_sent = _utc(2026, 9, 20, 10, 0)
    five = timedelta(seconds=5)
    return [
        _msg(
            plugin,
            601,
            _utc(2026, 10, 18, 6, 0),
            "inbound",
            observed=_utc(2026, 10, 18, 6, 0, 5),
            chat=992,
            profile=BOB,
        ),
        _msg(
            plugin,
            602,
            _utc(2026, 10, 18, 6, 15),
            "outbound",
            observed=_utc(2026, 10, 18, 6, 15, 5),
            chat=992,
            profile=BOB,
        ),
        _msg(
            plugin, 501, carol_sent, "inbound", observed=carol_sent + five, conn="business-456", chat=994, profile=CAROL
        ),
        _tomb(plugin, 501, _utc(2026, 10, 5, 10, 0), "inbound", message_at=carol_sent, conn="business-456", chat=994),
        _msg(
            plugin, 701, _utc(2026, 9, 10, 8, 0), "inbound", observed=_utc(2026, 9, 10, 8, 0, 5), chat=995, profile=DAVE
        ),
    ]


def _alice_fixture(plugin) -> None:
    _seed(plugin, *_alice_records(plugin))


def _overview_fixture(plugin) -> None:
    _seed(plugin, *_alice_records(plugin), *_overview_extra_records(plugin))


def _keys_everywhere(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys_everywhere(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys_everywhere(item)}
    return set()


def _doc_section(text: str, heading: str) -> str:
    marker = f"\n## {heading}\n"
    assert marker in text, f"missing section: ## {heading}"
    return re.split(r"^## ", text.split(marker, 1)[1], maxsplit=1, flags=re.MULTILINE)[0]


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    return [(match[1], match[2]) for match in _FENCED_BLOCK_RE.finditer(text)]


def _documented_stats_commands(text: str) -> list[list[str]]:
    """Every documented `hermes telesec stats ...` line, as CLI argv after the program name."""
    commands = []
    for language, body in _fenced_blocks(text):
        if language != "bash":
            continue
        for line in body.splitlines():
            argv = shlex.split(line, comments=True)
            if argv[:3] == ["hermes", "telesec", "stats"]:
                commands.append(argv[2:])
    return commands


def _assert_json_subset(documented: Any, actual: Any, path: str = "$") -> None:
    """A trimmed doc example may drop keys but must keep real keys, real order, and real values."""
    if isinstance(documented, dict):
        assert isinstance(actual, dict), path
        assert list(documented) == [key for key in actual if key in documented], f"{path}: unknown or reordered keys"
        for key, value in documented.items():
            _assert_json_subset(value, actual[key], f"{path}.{key}")
    elif isinstance(documented, list):
        assert isinstance(actual, list) and len(documented) == len(actual), path
        for index, (item, actual_item) in enumerate(zip(documented, actual, strict=True)):
            _assert_json_subset(item, actual_item, f"{path}[{index}]")
    else:
        assert documented == actual, path


# --- Golden output and docs ------------------------------------------------------


def test_worked_example_text_moscow(plugin):
    _alice_fixture(plugin)

    exit_code, output = _stats(plugin, "--contact", "@alice", *MOSCOW_RANGE)

    assert exit_code == 0
    assert output == ALICE_MOSCOW


def test_worked_example_utc(plugin):
    _alice_fixture(plugin)

    exit_code, output = _stats(
        plugin, "--contact", "@alice", "--since", "2026-10-01", "--until", "2026-10-18", "--tz", "UTC"
    )

    expected = ALICE_MOSCOW.splitlines()
    expected[0] = "range 2026-10-01..2026-10-18 UTC, by Telegram send time; text messages only"
    expected[2] = "  messages 10: received 6, sent 3, unknown direction 1"
    expected[3] = "  active days 4 (received 4, sent 3)"
    expected[4] = "  latest 2026-10-18 21:00 received; last sent 2026-10-17 16:30"
    expected[5] = "  last message: received 2026-10-18 21:00, no observed reply after it"
    expected[6] = "  sessions 5 (gap 6h): started by contact 3, by owner 2; ended on received 3"
    assert exit_code == 0
    assert output.splitlines() == expected


def test_worked_example_json(plugin):
    _alice_fixture(plugin)

    document = _stats_json(plugin, "--contact", "@alice", *MOSCOW_RANGE)

    assert document["schema_version"] == 1
    assert document["report"] == "correspondence_stats"
    assert document["mode"] == "contact"
    assert document["generated_at"] == "2026-10-19T14:32:00+03:00"
    assert document["range"] == {
        "since": "2026-10-01T00:00:00+03:00",
        "until": "2026-10-18T23:59:59.999999+03:00",
        "timezone": "Europe/Moscow",
        "time_basis": "telegram_send_time",
        "deletion_time_basis": "deletion_observed_time",
        "content": "text_messages_only",
        "session_gap_seconds": 21600,
    }
    assert document["overview"] is None
    [alice] = document["contacts"]
    assert alice["business_connection_id"] == "business-123"
    assert alice["chat_id"] == 991
    assert alice["chat_type"] == "private"
    assert alice["display_name"] == "Alice Example (@alice)"
    assert alice["name"] == "Alice Example"
    assert alice["username"] == "alice"
    assert alice["messages"] == {"total": 9, "received": 5, "sent": 3, "unknown_direction": 1}
    assert alice["active_days"] == {"total": 4, "received": 3, "sent": 3}
    assert alice["latest"] == {
        "at": "2026-10-17T21:04:00+03:00",
        "side": "received",
        "received_at": "2026-10-17T21:04:00+03:00",
        "sent_at": "2026-10-17T19:30:00+03:00",
        "observed_reply": "none",
    }
    assert alice["sessions"] == {
        "total": 4,
        "started_by_contact": 2,
        "started_by_owner": 2,
        "started_by_unknown": 0,
        "ended_on_received": 2,
    }
    assert alice["deletions"] == {
        "total": 4,
        "unexplained": {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0},
        "likely_correction": 1,
        "likely_duplicate": 1,
        "unclassifiable": 0,
        "pending": 1,
        "details_command": (
            "hermes telesec history deletions --connection business-123 --chat 991 --status unexplained"
        ),
    }
    assert alice["deletions"]["details_command"] in ALICE_MOSCOW
    assert alice["missing_history"] == {
        "retained_from": "2026-10-03T11:00:05+03:00",
        "range_partial": True,
        "deletions_of_messages_not_in_history": 1,
        "messages_known_only_from_edit": 1,
    }


def test_overview_golden(plugin):
    _overview_fixture(plugin)

    exit_code, output = _stats(plugin, *MOSCOW_RANGE)

    assert exit_code == 0
    assert output == OVERVIEW_MOSCOW
    assert all(line == line.rstrip() for line in output.splitlines())

    document = _stats_json(plugin, *MOSCOW_RANGE)
    assert document["mode"] == "overview"
    assert [contact["display_name"] for contact in document["contacts"]] == [
        "Bob Builder (@bob)",
        "Alice Example (@alice)",
        "Carol",
    ]
    assert document["overview"] == {
        "contacts_in_scope": 4,
        "active": 3,
        "shown": 3,
        "limit": 20,
        "not_shown_over_limit": 0,
        "not_shown_no_activity": 1,
        "not_shown_no_activity_partial": 0,
        "not_shown_no_activity_missing_history": {
            "deletions_of_messages_not_in_history": 0,
            "messages_known_only_from_edit": 0,
        },
    }


def test_overview_limit_footer(plugin):
    _overview_fixture(plugin)

    exit_code, output = _stats(plugin, *MOSCOW_RANGE, "--limit", "1")

    assert exit_code == 0
    assert output == OVERVIEW_MOSCOW_LIMIT_1
    document = _stats_json(plugin, *MOSCOW_RANGE, "--limit", "1")
    assert [contact["chat_id"] for contact in document["contacts"]] == [992]
    assert document["overview"]["not_shown_over_limit"] == 2
    # --limit is clamped to 1..5000 and ignored in contact mode.
    assert _stats(plugin, *MOSCOW_RANGE, "--limit", "0") == (0, OVERVIEW_MOSCOW_LIMIT_1)
    assert _stats(plugin, "--contact", "@alice", *MOSCOW_RANGE, "--limit", "1") == (0, ALICE_MOSCOW)


def test_docs_examples_match_output(plugin):
    _overview_fixture(plugin)
    blocks = _fenced_blocks(_doc_section(STATS_DOC.read_text(encoding="utf-8"), "Example (synthetic)"))

    # Each documented command is followed by its output: text verbatim, JSON as a trimmed subset.
    checked = []
    for (language, command), (output_language, documented) in zip(blocks[::2], blocks[1::2], strict=True):
        assert language == "bash"
        argv = shlex.split(command)
        assert argv[:3] == ["hermes", "telesec", "stats"]
        exit_code, output = _run_history_cli(plugin, *argv[2:])
        assert exit_code == 0, output
        if output_language == "text":
            assert documented == output
        else:
            assert output_language == "json"
            _assert_json_subset(json.loads(documented), json.loads(output))
        checked.append((tuple(argv[3:]), output_language))

    assert checked == [
        (("--contact", "@alice", *MOSCOW_RANGE), "text"),
        (MOSCOW_RANGE, "text"),
        (("--contact", "@alice", *MOSCOW_RANGE, "--format", "json"), "json"),
    ]
    assert [body for _, body in blocks[1:4:2]] == [ALICE_MOSCOW, OVERVIEW_MOSCOW]
    assert "(docs/STATS.md)" in README_DOC.read_text(encoding="utf-8")
    assert "(STATS.md)" in HISTORY_DOC.read_text(encoding="utf-8")


def test_docs_cover_options_keys_and_commands(plugin):
    _overview_fixture(plugin)
    stats_doc = STATS_DOC.read_text(encoding="utf-8")
    options = vars(_build_history_parser(plugin).parse_args(["stats"]))

    for dest in options:
        if dest.startswith(("_", "telesec_")):
            continue
        # Every real option has a row in the options table.
        assert f"\n| `--{dest.replace('_', '-')}` |" in stats_doc, dest
    keys = _keys_everywhere(_stats_json(plugin, "--contact", "@alice")) | _keys_everywhere(_stats_json(plugin))
    assert sorted(key for key in keys if f"`{key}`" not in stats_doc) == []
    commands = _documented_stats_commands(stats_doc) + _documented_stats_commands(
        README_DOC.read_text(encoding="utf-8")
    )
    assert len(commands) >= 7
    for argv in commands:
        exit_code, output = _run_history_cli(plugin, *argv)
        assert exit_code == 0, (argv, output)
        assert "SECRET-" not in output


def test_json_key_order(plugin):
    _overview_fixture(plugin)
    top = ["schema_version", "report", "mode", "generated_at", "range", "contacts", "overview"]
    range_keys = ["since", "until", "timezone", "time_basis", "deletion_time_basis", "content", "session_gap_seconds"]
    contact_keys = [
        "business_connection_id",
        "chat_id",
        "chat_type",
        "display_name",
        "name",
        "username",
        "messages",
        "active_days",
        "latest",
        "sessions",
        "deletions",
        "missing_history",
    ]
    nested = {
        "messages": ["total", "received", "sent", "unknown_direction"],
        "active_days": ["total", "received", "sent"],
        "latest": ["at", "side", "received_at", "sent_at", "observed_reply"],
        "sessions": ["total", "started_by_contact", "started_by_owner", "started_by_unknown", "ended_on_received"],
        "deletions": [
            "total",
            "unexplained",
            "likely_correction",
            "likely_duplicate",
            "unclassifiable",
            "pending",
            "details_command",
        ],
        "missing_history": [
            "retained_from",
            "range_partial",
            "deletions_of_messages_not_in_history",
            "messages_known_only_from_edit",
        ],
    }
    overview_keys = [
        "contacts_in_scope",
        "active",
        "shown",
        "limit",
        "not_shown_over_limit",
        "not_shown_no_activity",
        "not_shown_no_activity_partial",
        "not_shown_no_activity_missing_history",
    ]

    for document in (_stats_json(plugin, "--contact", "@alice", *MOSCOW_RANGE), _stats_json(plugin, *MOSCOW_RANGE)):
        assert list(document) == top
        assert list(document["range"]) == range_keys
        assert document["contacts"]
        for contact in document["contacts"]:
            assert list(contact) == contact_keys
            for key, expected in nested.items():
                assert list(contact[key]) == expected
            assert list(contact["deletions"]["unexplained"]) == ["total", "received", "sent", "unknown_direction"]
        if document["mode"] == "overview":
            assert list(document["overview"]) == overview_keys
            assert list(document["overview"]["not_shown_no_activity_missing_history"]) == [
                "deletions_of_messages_not_in_history",
                "messages_known_only_from_edit",
            ]


# --- Counting definitions --------------------------------------------------------


def test_message_counted_once(plugin):
    at = _utc(2026, 10, 10, 9, 0)
    created = _msg(plugin, 1, at, "inbound")
    redelivered = _msg(plugin, 1, at, "inbound", observed=at + timedelta(seconds=30))
    _seed(
        plugin,
        created,
        _msg(plugin, 1, at, "inbound", edited=True, observed=at + timedelta(minutes=1)),
        _msg(plugin, 1, at, "inbound", edited=True, observed=at + timedelta(minutes=2)),
        dict(created),
        redelivered,
    )

    contact = _contact_json(plugin, "--contact", "@alice")

    assert redelivered["event_id"] != created["event_id"]
    assert contact["messages"] == {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0}
    assert contact["sessions"]["total"] == 1


def test_latest_known_direction_wins(plugin, cs):
    at = _utc(2026, 10, 10, 9, 0)
    later = at + timedelta(minutes=5)
    window = _window(cs)

    def side_of(*records: dict[str, Any]) -> str:
        counts = cs.compute_stats(_entry(), records, window).messages
        assert counts.total == 1
        by_side = {"received": counts.received, "sent": counts.sent, "unknown": counts.unknown}
        return next(side for side, count in by_side.items() if count)

    assert (
        side_of(_msg(plugin, 1, at, "inbound"), _msg(plugin, 1, at, "unknown", edited=True, observed=later))
        == "received"
    )
    assert (
        side_of(_msg(plugin, 2, at, "unknown"), _msg(plugin, 2, at, "outbound", edited=True, observed=later)) == "sent"
    )
    assert (
        side_of(_msg(plugin, 3, at, "outbound"), _msg(plugin, 3, at, "inbound", edited=True, observed=later))
        == "received"
    )
    assert (
        side_of(_msg(plugin, 4, at, "unknown"), _msg(plugin, 4, at, "unknown", edited=True, observed=later))
        == "unknown"
    )
    same_second = at + timedelta(seconds=1)
    assert (
        side_of(
            _msg(plugin, 5, at, "inbound", observed=same_second),
            _msg(plugin, 5, at, "outbound", edited=True, observed=same_second),
        )
        == "sent"
    )


def test_unknown_direction_separate(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 11, 9, 0), "unknown"),
        _msg(plugin, 3, _utc(2026, 10, 10, 10, 0), "outbound"),
        _msg(plugin, 4, _utc(2026, 10, 10, 9, 0), "inbound", chat=992, profile=BOB),
    )

    alice = _contact_json(plugin, "--contact", "@alice")
    messages = alice["messages"]
    assert messages["total"] == messages["received"] + messages["sent"] + messages["unknown_direction"] == 3
    assert messages["unknown_direction"] == 1
    assert alice["active_days"] == {"total": 2, "received": 1, "sent": 1}

    exit_code, output = _stats(plugin, "--contact", "@bob")
    assert exit_code == 0
    assert "  messages 1: received 1, sent 0, unknown direction 0" in output.splitlines()


def test_send_time_placement(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound", observed=_utc(2026, 10, 12, 9, 0)),
        _msg(plugin, 2, _utc(2026, 9, 29, 9, 0), "inbound", observed=_utc(2026, 10, 2, 9, 0)),
        _msg(plugin, 3, None, "outbound", observed=_utc(2026, 10, 5, 9, 0), message_at=None),
        _msg(plugin, 3, None, "outbound", edited=True, observed=_utc(2026, 10, 15, 9, 0), message_at=None),
    )

    contact = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-01", "--until", "2026-10-11")

    assert contact["messages"] == {"total": 2, "received": 1, "sent": 1, "unknown_direction": 0}
    assert contact["latest"]["at"] == "2026-10-10T09:00:00+00:00"
    assert contact["latest"]["sent_at"] == "2026-10-05T09:00:00+00:00"


def test_foreign_records_ignored(plugin, cs):
    at = _utc(2026, 10, 10, 9, 0)
    records = [
        _msg(plugin, 1, at, "inbound"),
        _msg(plugin, 2, at, "inbound", chat=992),
        _msg(plugin, 3, at, "inbound", conn="business-456"),
        _tomb(plugin, 1, at + timedelta(minutes=1), "inbound", message_at=at, chat=992),
    ]

    stats = cs.compute_stats(_entry(), records, _window(cs))

    assert stats.messages.total == 1
    assert stats.deletions.total == 0


@pytest.mark.asyncio
async def test_text_only_capture(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = _utc(2026, 10, 18, 10, 0)
    await history.observe_ptb_update(
        make_business_media_update(message_id=1, update_id=1, date=base),
        bot=FakeBot(),
        now=base,
    )
    await history.observe_ptb_update(
        make_business_text_update(text="hello", message_id=2, update_id=2, date=base),
        bot=FakeBot(),
        now=base + timedelta(seconds=1),
    )

    exit_code, output = _stats(plugin, "--contact", "@customer991")

    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0].endswith("text messages only")
    assert "  messages 1: received 1, sent 0, unknown direction 0" in lines


# --- Deletions -------------------------------------------------------------------


def test_deleted_still_counts_unless_replaced(plugin):
    base = _utc(2026, 10, 10, 9, 0)

    def at(minutes: int) -> datetime:
        return base + timedelta(minutes=minutes)

    records = [
        _msg(plugin, 1, at(0), "inbound"),
        _msg(plugin, 2, at(1), "outbound"),
        _msg(plugin, 3, at(2), "inbound"),
        _msg(plugin, 4, at(3), "inbound"),
        _msg(plugin, 5, at(4), "outbound"),
        _msg(plugin, 6, at(5), "inbound"),
        _msg(plugin, 7, at(6), "outbound"),
    ]
    tombs = {
        mid: _tomb(plugin, mid, at(10 + mid), direction, message_at=at(mid - 1))
        for mid, direction in (
            (1, "inbound"),
            (2, "outbound"),
            (3, "inbound"),
            (4, "inbound"),
            (5, "outbound"),
        )
    }
    records.extend(tombs.values())
    records.extend(
        [
            _cls(plugin, tombs[1], "unexplained", at(30), "no_strong_match"),
            _cls(plugin, tombs[2], "unclassifiable", at(30), "missing_text"),
            _cls(plugin, tombs[4], "likely_duplicate", at(30), "normalized_exact_duplicate", replacement=6),
            _cls(
                plugin, tombs[5], "likely_correction", at(30), "high_similarity_small_edit", replacement=7, score=0.95
            ),
        ]
    )
    _seed(plugin, *records)

    exit_code, output = _stats(plugin, "--contact", "@alice")

    assert exit_code == 0
    lines = output.splitlines()
    assert "  messages 5: received 3, sent 2, unknown direction 0" in lines
    assert (
        "  deletions 5: unexplained 1 (received 1, sent 0), likely correction 1, likely duplicate 1, "
        "unclassifiable 1, pending 1"
    ) in lines


def test_latest_classification_in_file_order_wins(plugin):
    # history_support's own state rebuild keeps the last classification in file
    # (append) order; stats must agree even when observed_at stepped backwards.
    at = _utc(2026, 10, 10, 9, 0)
    tomb_one = _tomb(plugin, 1, at + timedelta(minutes=1), "inbound", message_at=at)
    tomb_two = _tomb(plugin, 2, at + timedelta(minutes=1), "inbound", message_at=at + timedelta(seconds=1))
    late = _cls(plugin, tomb_two, "unexplained", at + timedelta(minutes=10), "no_strong_match")
    early = _cls(plugin, tomb_two, "likely_duplicate", at + timedelta(minutes=5), "normalized_exact_duplicate")
    tomb_three = _tomb(plugin, 3, at + timedelta(minutes=2), "outbound", message_at=at + timedelta(seconds=2))
    _seed(
        plugin,
        _msg(plugin, 1, at, "inbound"),
        _cls(plugin, tomb_one, "unexplained", at + timedelta(minutes=3), "no_strong_match"),
        tomb_one,
        _msg(plugin, 2, at + timedelta(seconds=1), "inbound"),
        tomb_two,
        late,
        early,
        _msg(plugin, 3, at + timedelta(seconds=2), "outbound"),
        tomb_three,
        dict(tomb_three),
    )

    contact = _contact_json(plugin, "--contact", "@alice")

    # Message 2's last classification in file order is likely_duplicate, so it is not counted.
    assert contact["messages"]["total"] == 2
    assert contact["deletions"]["total"] == 3
    assert contact["deletions"]["unexplained"] == {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0}
    assert contact["deletions"]["likely_duplicate"] == 1
    assert contact["deletions"]["pending"] == 1


def test_latest_known_direction_follows_file_order(plugin, cs):
    # The capture clock stepped back: the edit was appended last but observed earlier.
    at = _utc(2026, 10, 10, 9, 0)
    records = [
        _msg(plugin, 1, at, "inbound", observed=at + timedelta(minutes=5)),
        _msg(plugin, 1, at, "outbound", edited=True, observed=at + timedelta(minutes=1)),
        _msg(plugin, 1, at, "unknown", edited=True, observed=at + timedelta(minutes=9)),
    ]

    counts = cs.compute_stats(_entry(), records, _window(cs)).messages

    assert (counts.received, counts.sent, counts.unknown) == (0, 1, 0)


_CLASSIFICATION_REASONS = {
    "likely_duplicate": "normalized_exact_duplicate",
    "likely_correction": "high_similarity_small_edit",
    "unexplained": "no_strong_match",
    "unclassifiable": "missing_text",
}
_DETAILS_991 = "hermes telesec history deletions --connection business-123 --chat 991 --status unexplained"


# Every adjacent pair of likely_duplicate > likely_correction > unexplained >
# unclassifiable > pending, with the winner on the first and on the second tombstone.
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("likely_duplicate", "likely_correction", "likely_duplicate"),
        ("likely_correction", "likely_duplicate", "likely_duplicate"),
        ("likely_correction", "unexplained", "likely_correction"),
        ("unexplained", "likely_correction", "likely_correction"),
        ("unexplained", "unclassifiable", "unexplained"),
        ("unclassifiable", "unexplained", "unexplained"),
        ("unclassifiable", "pending", "unclassifiable"),
        ("pending", "unclassifiable", "unclassifiable"),
        ("unexplained", "pending", "unexplained"),
        ("unexplained", "likely_duplicate", "likely_duplicate"),
    ],
)
def test_deleted_twice_precedence(plugin, first: str, second: str, expected: str):
    sent = _utc(2026, 10, 10, 9, 0)
    first_tomb = _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent)
    second_tomb = _tomb(plugin, 1, sent + timedelta(minutes=2), "inbound", message_at=sent)
    records = [_msg(plugin, 1, sent, "inbound"), first_tomb, second_tomb]
    for tomb, status in ((first_tomb, first), (second_tomb, second)):
        if status != "pending":
            records.append(_cls(plugin, tomb, status, sent + timedelta(minutes=5), _CLASSIFICATION_REASONS[status]))
    _seed(plugin, *records)

    contact = _contact_json(plugin, "--contact", "@alice")

    replaced = expected in {"likely_duplicate", "likely_correction"}
    assert contact["messages"]["total"] == (0 if replaced else 1)
    unexplained = int(expected == "unexplained")
    assert contact["deletions"] == {
        "total": 1,
        "unexplained": {"total": unexplained, "received": unexplained, "sent": 0, "unknown_direction": 0},
        "likely_correction": int(expected == "likely_correction"),
        "likely_duplicate": int(expected == "likely_duplicate"),
        "unclassifiable": int(expected == "unclassifiable"),
        "pending": int(expected == "pending"),
        "details_command": _DETAILS_991 if unexplained else None,
    }
    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    details = [line for line in output.splitlines() if line.startswith("  deletion details:")]
    assert details == ([f"  deletion details: {_DETAILS_991}"] if unexplained else [])


def test_deleted_twice_first_observation_and_latest_known_side(plugin):
    # Message 1: first deleted before --since, redelivered inside the range -> not on the line.
    early_sent = _utc(2026, 10, 1, 9, 0)
    early_first = _tomb(plugin, 1, _utc(2026, 10, 2, 9, 0), "inbound", message_at=early_sent)
    early_again = _tomb(plugin, 1, _utc(2026, 10, 12, 9, 0), "inbound", message_at=early_sent)
    # Message 2: first deleted inside the range, redelivered after --until -> on the line.
    in_sent = _utc(2026, 10, 11, 9, 0)
    in_first = _tomb(plugin, 2, _utc(2026, 10, 12, 9, 0), "inbound", message_at=in_sent)
    in_again = _tomb(plugin, 2, _utc(2026, 10, 16, 9, 0), "inbound", message_at=in_sent)
    # Message 3: unknown created direction; the latest tombstone with a known side decides.
    side_sent = _utc(2026, 10, 11, 10, 0)
    side_out = _tomb(plugin, 3, _utc(2026, 10, 12, 9, 0), "outbound", message_at=side_sent)
    side_in = _tomb(plugin, 3, _utc(2026, 10, 13, 9, 0), "inbound", message_at=side_sent)
    side_unknown = _tomb(plugin, 3, _utc(2026, 10, 13, 10, 0), "unknown", message_at=side_sent)
    _seed(
        plugin,
        _msg(plugin, 1, early_sent, "inbound"),
        early_first,
        early_again,
        _msg(plugin, 2, in_sent, "inbound"),
        in_first,
        _msg(plugin, 3, side_sent, "unknown"),
        side_out,
        side_in,
        side_unknown,
        _cls(plugin, side_in, "unexplained", _utc(2026, 10, 13, 9, 5), "no_strong_match"),
        in_again,
        _cls(plugin, in_again, "unexplained", _utc(2026, 10, 16, 9, 5), "no_strong_match"),
    )
    window = ("--contact", "@alice", "--since", "2026-10-10", "--until", "2026-10-14")

    contact = _contact_json(plugin, *window)

    assert contact["messages"] == {"total": 2, "received": 1, "sent": 0, "unknown_direction": 1}
    assert contact["deletions"]["total"] == 2
    assert contact["deletions"]["pending"] == 0
    assert contact["deletions"]["unexplained"] == {"total": 2, "received": 2, "sent": 0, "unknown_direction": 0}
    exit_code, output = _stats(plugin, *window)
    assert exit_code == 0
    assert "  deletions 2: unexplained 2 (received 2, sent 0)" in output.splitlines()


def test_unexplained_split_by_side(plugin):
    at = _utc(2026, 10, 10, 9, 0)
    out_tomb = _tomb(plugin, 2, at + timedelta(minutes=10), "outbound", message_at=at)
    # The tombstone copied the unknown direction of the latest edit.
    edited_tomb = _tomb(plugin, 3, at + timedelta(minutes=10), "unknown", message_at=at)
    unknown_tomb = _tomb(plugin, 4, at + timedelta(minutes=10), "unknown", message_at=at)
    # The original was pruned, but the tombstone still carries its send time and side.
    pruned_tomb = _tomb(plugin, 5, at + timedelta(minutes=10), "inbound", message_at=at - timedelta(days=60))
    _seed(
        plugin,
        _msg(plugin, 2, at, "outbound"),
        _msg(plugin, 3, at, "outbound"),
        _msg(plugin, 3, at, "unknown", edited=True, observed=at + timedelta(minutes=1)),
        _msg(plugin, 4, at, "unknown"),
        out_tomb,
        edited_tomb,
        unknown_tomb,
        pruned_tomb,
        *(
            _cls(plugin, tomb, "unexplained", at + timedelta(minutes=20), "no_strong_match")
            for tomb in (out_tomb, edited_tomb, unknown_tomb, pruned_tomb)
        ),
    )

    exit_code, output = _stats(plugin, "--contact", "@alice")

    assert exit_code == 0
    assert "  deletions 4: unexplained 4 (received 1, sent 2, unknown direction 1)" in output.splitlines()
    assert "deleted by" not in output


def test_deletion_range_by_observed_time(plugin):
    early = _utc(2026, 10, 1, 9, 0)
    in_range = _utc(2026, 10, 7, 9, 0)
    corrected = _utc(2026, 10, 8, 9, 0)
    correction_tomb = _tomb(plugin, 3, _utc(2026, 10, 9, 9, 0), "inbound", message_at=corrected)
    _seed(
        plugin,
        _msg(plugin, 1, early, "inbound"),
        _tomb(plugin, 1, _utc(2026, 10, 6, 9, 0), "inbound", message_at=early),
        _msg(plugin, 2, in_range, "outbound"),
        _tomb(plugin, 2, _utc(2026, 10, 12, 9, 0), "outbound", message_at=in_range),
        _msg(plugin, 3, corrected, "inbound"),
        correction_tomb,
        _cls(plugin, correction_tomb, "likely_correction", _utc(2026, 10, 12, 9, 0), "high_similarity_small_edit"),
    )

    contact = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-05", "--until", "2026-10-10")

    assert contact["messages"] == {"total": 1, "received": 0, "sent": 1, "unknown_direction": 0}
    assert contact["deletions"]["total"] == 2
    assert contact["deletions"]["pending"] == 1
    assert contact["deletions"]["likely_correction"] == 1


def test_month_floor_resolves_side(plugin):
    sent = _utc(2026, 9, 2, 9, 0)
    stale_tomb = _tomb(plugin, 1, _utc(2026, 10, 5, 9, 0), "unknown", message_at=sent)
    _seed(
        plugin,
        _msg(plugin, 1, sent, "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 5, 8, 0), "outbound"),
        stale_tomb,
        _cls(plugin, stale_tomb, "unexplained", _utc(2026, 10, 5, 9, 5), "no_strong_match"),
    )

    contact = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-01")

    assert contact["messages"]["total"] == 1
    assert contact["deletions"]["unexplained"] == {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0}


def test_pending_not_classified(plugin):
    sent = _utc(2026, 10, 10, 9, 0)
    _seed(
        plugin,
        _msg(plugin, 1, sent, "inbound"),
        _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent),
    )
    before = snapshot_history_state(plugin)

    exit_code, output = _stats(plugin, "--contact", "@alice")

    assert exit_code == 0
    assert "  deletions 1: pending 1" in output.splitlines()
    assert "deletion details:" not in output
    assert snapshot_history_state(plugin) == before


# --- Missing history -------------------------------------------------------------


@pytest.mark.parametrize("classified", [True, False])
def test_deletion_of_uncaptured_message(plugin, classified: bool):
    uncaptured = _tomb(plugin, 200, _utc(2026, 10, 12, 10, 0), "unknown")
    records = [_msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound"), uncaptured]
    if classified:
        records.append(_cls(plugin, uncaptured, "unclassifiable", _utc(2026, 10, 12, 10, 2), "missing_original"))
    _seed(plugin, *records)

    contact = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-10")
    assert contact["deletions"]["total"] == 0
    assert contact["missing_history"]["deletions_of_messages_not_in_history"] == 1
    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2026-10-10")
    assert exit_code == 0
    assert "  missing history: none" not in output
    assert "1 deletion of a message not in history" in output

    exit_code, output = _stats(plugin, "--since", "2026-10-10")
    assert exit_code == 0
    # The contact is hidden (no counted message or deletion), but its missing history is still reported.
    assert output.splitlines()[1:] == [
        "No contacts with activity in range.",
        "shown 0 of 1 contacts; not shown: 1 with no activity in range",
        "missing history in contacts with no activity in range: 1 deletion of a message not in history; "
        + "use --contact for details",
    ]
    overview = _stats_json(plugin, "--since", "2026-10-10")["overview"]
    assert overview["not_shown_no_activity"] == 1
    assert overview["not_shown_no_activity_partial"] == 0
    assert overview["not_shown_no_activity_missing_history"] == {
        "deletions_of_messages_not_in_history": 1,
        "messages_known_only_from_edit": 0,
    }

    _seed(plugin, _msg(plugin, 200, _utc(2026, 10, 12, 9, 0), "inbound", observed=_utc(2026, 10, 13, 9, 0)))
    contact = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-10")
    assert contact["deletions"]["total"] == 1
    assert contact["deletions"]["unclassifiable" if classified else "pending"] == 1
    assert contact["missing_history"]["deletions_of_messages_not_in_history"] == 0


def test_edit_only_message(plugin):
    replaced_tomb = _tomb(plugin, 301, _utc(2026, 10, 13, 10, 0), "inbound", message_at=_utc(2026, 10, 13, 9, 0))
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 11, 9, 0), "outbound"),
        _msg(plugin, 300, _utc(2026, 10, 12, 9, 0), "inbound", edited=True, observed=_utc(2026, 10, 12, 10, 0)),
        _msg(plugin, 299, _utc(2026, 10, 1, 9, 0), "inbound", edited=True, observed=_utc(2026, 10, 12, 10, 0)),
        _msg(plugin, 301, _utc(2026, 10, 13, 9, 0), "inbound", edited=True, observed=_utc(2026, 10, 13, 9, 30)),
        replaced_tomb,
        _cls(plugin, replaced_tomb, "likely_duplicate", _utc(2026, 10, 13, 10, 5), "normalized_exact_duplicate"),
    )

    contact = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-10")

    assert contact["messages"]["total"] == 1
    assert contact["missing_history"]["messages_known_only_from_edit"] == 2
    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2026-10-10")
    assert exit_code == 0
    assert "; 2 messages known only from an edit" in output


def test_partial_range(plugin):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 3, 9, 0), "inbound"))

    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2026-10-01")
    assert exit_code == 0
    assert "  missing history: retained from 2026-10-03 (range is partial)" in output.splitlines()

    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2026-10-04")
    assert exit_code == 0
    assert "  missing history: none (retained from 2026-10-03)" in output.splitlines()

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert "  missing history: none (retained from 2026-10-03)" in output.splitlines()
    assert _contact_json(plugin, "--contact", "@alice")["missing_history"]["range_partial"] is False

    # first_seen_at is the first observation, 2026-10-03T09:00:01Z: --since exactly
    # there covers all retained history, one second earlier does not.
    at_first = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-03T09:00:01Z")
    assert at_first["missing_history"]["range_partial"] is False
    before_first = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-03T09:00:00Z")
    assert before_first["missing_history"]["range_partial"] is True


def test_overview_range_before_retained_history(plugin):
    _seed(
        plugin,
        # Alice is retained from 2026-10-03, Bob from 2026-10-12, Dave from 2026-08-20.
        _msg(plugin, 1, _utc(2026, 10, 3, 9, 0), "inbound"),
        _msg(plugin, 601, _utc(2026, 10, 12, 9, 0), "outbound", chat=992, profile=BOB),
        # Bob edited a message sent in September, before his history was captured.
        _msg(
            plugin,
            600,
            _utc(2026, 9, 15, 9, 0),
            "inbound",
            edited=True,
            observed=_utc(2026, 10, 12, 10, 0),
            chat=992,
            profile=BOB,
        ),
        _msg(plugin, 701, _utc(2026, 8, 20, 9, 0), "inbound", chat=995, profile=DAVE),
    )
    september = ("--since", "2026-09-01", "--until", "2026-09-30")

    # The whole range is before Alice's and Bob's retained history: their activity there
    # is unknown, not zero, and the overview says so instead of only "no activity".
    exit_code, output = _stats(plugin, *september)
    assert exit_code == 0
    assert output.splitlines() == [
        "range 2026-09-01..2026-09-30 UTC, by Telegram send time; text messages only",
        "No contacts with activity in range.",
        "shown 0 of 3 contacts; not shown: 3 with no activity in range "
        + "(range is partial for 2: retained history starts after --since)",
        "missing history in contacts with no activity in range: 1 message known only from an edit; "
        + "use --contact for details",
    ]
    contacts_table = _doc_section(STATS_DOC.read_text(encoding="utf-8"), "Contacts table")
    assert [body for language, body in _fenced_blocks(contacts_table) if language == "text"] == [output]
    document = _stats_json(plugin, *september)
    assert document["contacts"] == []
    assert document["overview"] == {
        "contacts_in_scope": 3,
        "active": 0,
        "shown": 0,
        "limit": 20,
        "not_shown_over_limit": 0,
        "not_shown_no_activity": 3,
        "not_shown_no_activity_partial": 2,
        "not_shown_no_activity_missing_history": {
            "deletions_of_messages_not_in_history": 0,
            "messages_known_only_from_edit": 1,
        },
    }
    # The contact report agrees, and missing history is never mixed into the counts.
    bob = _contact_json(plugin, "--contact", "@bob", *september)
    assert bob["messages"]["total"] == 0
    assert bob["missing_history"]["range_partial"] is True
    assert bob["missing_history"]["messages_known_only_from_edit"] == 1

    # A shown row keeps its own partial column; the footer covers only hidden contacts.
    exit_code, output = _stats(plugin, "--since", "2026-10-01", "--until", "2026-10-05")
    assert exit_code == 0
    lines = output.splitlines()
    assert lines[2].split()[-6:] == ["yes", "business-123", "991", "Alice", "Example", "(@alice)"]
    assert lines[3:] == [
        "shown 1 of 3 contacts; not shown: 2 with no activity in range "
        + "(range is partial for 1: retained history starts after --since)",
    ]
    overview = _stats_json(plugin, "--since", "2026-10-01", "--until", "2026-10-05")["overview"]
    assert (overview["not_shown_no_activity"], overview["not_shown_no_activity_partial"]) == (2, 1)

    # Without --since the range covers all retained history, so nothing is partial.
    exit_code, output = _stats(plugin, "--until", "2026-08-31")
    assert exit_code == 0
    assert output.splitlines()[-1] == "shown 1 of 3 contacts; not shown: 2 with no activity in range"
    assert _stats_json(plugin, "--until", "2026-08-31")["overview"]["not_shown_no_activity_partial"] == 0
    assert "SECRET-" not in output


def test_real_prune(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    july = _utc(2026, 7, 10, 9, 0)
    august = _utc(2026, 8, 10, 9, 0)
    _seed(
        plugin,
        _msg(plugin, 1, july, "inbound"),
        _msg(plugin, 2, august, "outbound"),
        _msg(plugin, 3, _utc(2026, 10, 5, 9, 0), "inbound", observed=_utc(2026, 10, 5, 9, 0, 3)),
        _msg(plugin, 1, july, "inbound", edited=True, observed=_utc(2026, 10, 6, 9, 0)),
        _tomb(plugin, 2, _utc(2026, 10, 7, 9, 0), "outbound", message_at=august),
        _tomb(plugin, 99, _utc(2026, 10, 8, 9, 0), "unknown"),
    )
    monkeypatch.setenv("TELESEC_HISTORY_RETENTION_DAYS", "30")

    result = history.maintain_history(now=NOW)

    assert result.pruned_files == 2
    contact = _contact_json(plugin, "--contact", "@alice")
    assert contact["messages"] == {"total": 1, "received": 1, "sent": 0, "unknown_direction": 0}
    assert contact["missing_history"]["retained_from"] == "2026-10-05T09:00:03+00:00"
    assert contact["missing_history"]["range_partial"] is False
    assert contact["missing_history"]["messages_known_only_from_edit"] == 1
    assert contact["missing_history"]["deletions_of_messages_not_in_history"] == 1
    assert contact["deletions"]["total"] == 1
    assert contact["deletions"]["unexplained"] == {"total": 1, "received": 0, "sent": 1, "unknown_direction": 0}

    partial = _contact_json(plugin, "--contact", "@alice", "--since", "2026-07-01")
    assert partial["missing_history"]["range_partial"] is True
    assert partial["messages"]["total"] == 1


def test_orphan_classification_ignored(plugin):
    sent = _utc(2026, 10, 10, 9, 0)
    absent_tomb = _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent)
    _seed(
        plugin,
        _msg(plugin, 1, sent, "inbound"),
        _cls(plugin, absent_tomb, "likely_duplicate", sent + timedelta(minutes=5), "normalized_exact_duplicate"),
    )

    contact = _contact_json(plugin, "--contact", "@alice")

    assert contact["messages"]["total"] == 1
    assert contact["deletions"]["total"] == 0
    assert contact["missing_history"]["deletions_of_messages_not_in_history"] == 0


# --- Range and time zone ---------------------------------------------------------


def test_moscow_plain_dates_inclusive(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 9, 30, 20, 59, 59), "inbound"),
        _msg(plugin, 2, _utc(2026, 9, 30, 21, 0, 0), "inbound"),
        _msg(plugin, 3, _utc(2026, 10, 4, 21, 0, 0), "inbound"),
        _msg(plugin, 4, _utc(2026, 10, 5, 20, 59, 59), "inbound"),
        _msg(plugin, 5, _utc(2026, 10, 18, 20, 59, 59), "inbound"),
        _msg(plugin, 6, _utc(2026, 10, 18, 21, 0, 0), "inbound"),
    )

    document = _stats_json(plugin, "--contact", "@alice", *MOSCOW_RANGE)
    assert document["contacts"][0]["messages"]["total"] == 4
    assert document["range"]["since"] == "2026-10-01T00:00:00+03:00"
    assert document["range"]["until"] == "2026-10-18T23:59:59.999999+03:00"

    one_day = _contact_json(
        plugin, "--contact", "@alice", "--since", "2026-10-05", "--until", "2026-10-05", "--tz", "Europe/Moscow"
    )
    assert one_day["messages"]["total"] == 2


def test_exact_timestamp_bounds_inclusive(plugin):
    since = _utc(2026, 10, 5, 9, 0)
    until = _utc(2026, 10, 18, 18, 30)
    second = timedelta(seconds=1)
    early = _utc(2026, 10, 2, 9, 0)
    records = [
        _msg(plugin, 1, since, "inbound"),
        _msg(plugin, 2, since - second, "inbound"),
        _msg(plugin, 3, until, "outbound", observed=until + timedelta(minutes=5)),
        _msg(plugin, 4, until + second, "outbound"),
    ]
    # Deletions of messages sent before the range fall into it by observation time only.
    for mid, observed in ((10, since), (11, since - second), (12, until), (13, until + second)):
        sent = early + timedelta(minutes=mid)
        records += [_msg(plugin, mid, sent, "inbound"), _tomb(plugin, mid, observed, "inbound", message_at=sent)]
    _seed(plugin, *records)
    window = ("--contact", "@alice", "--since", "2026-10-05T09:00:00Z", "--until", "2026-10-18T18:30Z")

    contact = _contact_json(plugin, *window)

    assert contact["messages"] == {"total": 2, "received": 1, "sent": 1, "unknown_direction": 0}
    assert contact["latest"]["at"] == "2026-10-18T18:30:00+00:00"
    assert contact["deletions"]["total"] == 2
    assert contact["deletions"]["pending"] == 2
    exit_code, output = _stats(plugin, *window)
    assert exit_code == 0
    assert output.splitlines()[0] == (
        "range 2026-10-05 09:00..2026-10-18 18:30 UTC, by Telegram send time; text messages only"
    )
    assert "  messages 2: received 1, sent 1, unknown direction 0" in output.splitlines()
    assert "  deletions 2: pending 2" in output.splitlines()


def test_dst_days(plugin, cs):
    try:
        berlin = ZoneInfo("Europe/Berlin")
        santiago = ZoneInfo("America/Santiago")
    except ZoneInfoNotFoundError:
        pytest.skip("tz database unavailable")

    def bounds(day: str, tz) -> tuple[datetime, datetime]:
        return (
            cs.parse_bound(day, option_name="--since", tz=tz, now=NOW, end_of_day=False),
            cs.parse_bound(day, option_name="--until", tz=tz, now=NOW, end_of_day=True),
        )

    assert bounds("2026-10-25", berlin) == (_utc(2026, 10, 24, 22, 0), datetime(2026, 10, 25, 22, 59, 59, 999999, UTC))
    assert bounds("2026-03-29", berlin) == (_utc(2026, 3, 28, 23, 0), datetime(2026, 3, 29, 21, 59, 59, 999999, UTC))
    assert bounds("2026-09-06", santiago)[0] == _utc(2026, 9, 6, 4, 0)

    _seed(plugin, _msg(plugin, 1, _utc(2026, 9, 6, 12, 0), "inbound"))
    exit_code, output = _stats(
        plugin, "--contact", "@alice", "--since", "2026-09-06", "--until", "2026-09-06", "--tz", "America/Santiago"
    )
    assert exit_code == 0
    assert output.splitlines()[0].startswith("range 2026-09-06..2026-09-06 America/Santiago,")
    assert "  messages 1: received 1, sent 0, unknown direction 0" in output.splitlines()


def test_naive_and_offset_timestamps(plugin, cs):
    moscow = ZoneInfo("Europe/Moscow")

    def since(value: str) -> datetime:
        return cs.parse_bound(value, option_name="--since", tz=moscow, now=NOW, end_of_day=False)

    assert since("2026-10-01T09:00") == _utc(2026, 10, 1, 6, 0)
    assert since("2026-10-01 09:00") == _utc(2026, 10, 1, 6, 0)
    assert since("2026-10-01T09:00Z") == _utc(2026, 10, 1, 9, 0)
    assert since("2026-10-01T09:00+00:00") == _utc(2026, 10, 1, 9, 0)
    assert since("20261001") == since("2026-W40-4") == since("2026-10-01") == _utc(2026, 9, 30, 21, 0)
    assert cs.parse_bound("20261001", option_name="--until", tz=moscow, now=NOW, end_of_day=True) == datetime(
        2026, 10, 1, 20, 59, 59, 999999, UTC
    )

    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 3, 9, 0), "inbound"))
    exit_code, output = _stats(
        plugin,
        "--contact",
        "@alice",
        "--since",
        "2026-10-01T09:00",
        "--until",
        "2026-10-18T18:30Z",
        "--tz",
        "Europe/Moscow",
    )
    assert exit_code == 0
    assert output.splitlines()[0] == (
        "range 2026-10-01 09:00..2026-10-18 21:30 Europe/Moscow, by Telegram send time; text messages only"
    )
    document = _stats_json(plugin, "--contact", "@alice", "--since", "2026-10-01T09:00Z", "--tz", "Europe/Moscow")
    assert document["range"]["since"] == "2026-10-01T12:00:00+03:00"
    assert document["range"]["until"] is None


def test_relative_bounds(plugin):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 18, 9, 0), "inbound"))

    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2d", "--until", "0d", "--tz", "Europe/Moscow")
    assert exit_code == 0
    assert output.splitlines()[0] == (
        "range 2026-10-17 14:32..2026-10-19 14:32 Europe/Moscow, by Telegram send time; text messages only"
    )

    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2d", "--tz", "Europe/Moscow")
    assert exit_code == 0
    assert output.splitlines()[0].startswith("range 2026-10-17 14:32..now Europe/Moscow,")

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert output.splitlines()[0] == "range start of history..now UTC, by Telegram send time; text messages only"


def test_tz_default_and_override(plugin, cs, monkeypatch: pytest.MonkeyPatch):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 18, 9, 0), "inbound"))

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert " UTC, by Telegram send time" in output.splitlines()[0]

    monkeypatch.setenv(TIMEZONE_ENV, "Europe/Moscow")
    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert " Europe/Moscow, by Telegram send time" in output.splitlines()[0]
    assert "  latest 2026-10-18 12:00 received; no sent message" in output.splitlines()
    assert _stats_json(plugin, "--contact", "@alice")["range"]["timezone"] == "Europe/Moscow"

    exit_code, output = _stats(plugin, "--contact", "@alice", "--tz", "Asia/Tokyo")
    assert exit_code == 0
    assert " Asia/Tokyo, by Telegram send time" in output.splitlines()[0]

    monkeypatch.setenv(TIMEZONE_ENV, "  ")
    assert _stats_json(plugin, "--contact", "@alice")["range"]["timezone"] == "UTC"

    def _no_tz_database(name: str):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(cs, "ZoneInfo", _no_tz_database)
    exit_code, output = _stats(plugin, "--contact", "@alice", "--tz", "UTC")
    assert exit_code == 0
    assert " UTC, by Telegram send time" in output.splitlines()[0]


def test_env_tz_applies_to_plain_and_naive_bounds(plugin, monkeypatch: pytest.MonkeyPatch):
    _seed(
        plugin,
        # 2026-10-01 00:30 in Moscow, still 2026-09-30 in UTC.
        _msg(plugin, 1, _utc(2026, 9, 30, 21, 30), "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 5, 9, 0), "outbound"),
        # 2026-10-18 in UTC, already 2026-10-19 01:00 in Moscow.
        _msg(plugin, 3, _utc(2026, 10, 18, 22, 0), "inbound"),
    )
    monkeypatch.setenv(TIMEZONE_ENV, "Europe/Moscow")
    plain = ("--contact", "@alice", "--since", "2026-10-01", "--until", "2026-10-18")

    exit_code, output = _stats(plugin, *plain)
    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0] == "range 2026-10-01..2026-10-18 Europe/Moscow, by Telegram send time; text messages only"
    assert "  messages 2: received 1, sent 1, unknown direction 0" in lines
    assert "  latest 2026-10-05 12:00 sent; last received 2026-10-01 00:30" in lines
    document = _stats_json(plugin, *plain)
    assert document["range"]["since"] == "2026-10-01T00:00:00+03:00"
    assert document["range"]["until"] == "2026-10-18T23:59:59.999999+03:00"
    assert document["range"]["timezone"] == "Europe/Moscow"
    assert document["contacts"][0]["latest"]["received_at"] == "2026-10-01T00:30:00+03:00"

    # Naive timestamps are read in the env zone too.
    naive = ("--contact", "@alice", "--since", "2026-10-01T00:00", "--until", "2026-10-18T23:59:59")
    exit_code, output = _stats(plugin, *naive)
    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0] == "range 2026-10-01..2026-10-18 23:59 Europe/Moscow, by Telegram send time; text messages only"
    assert "  messages 2: received 1, sent 1, unknown direction 0" in lines
    document = _stats_json(plugin, *naive)
    assert document["range"]["since"] == "2026-10-01T00:00:00+03:00"
    assert document["range"]["until"] == "2026-10-18T23:59:59+03:00"
    assert document["contacts"][0]["latest"]["received_at"] == "2026-10-01T00:30:00+03:00"

    # An explicit --tz overrides the env for the bounds, not only for the label.
    exit_code, output = _stats(plugin, *plain, "--tz", "UTC")
    assert exit_code == 0
    lines = output.splitlines()
    assert lines[0] == "range 2026-10-01..2026-10-18 UTC, by Telegram send time; text messages only"
    assert "  latest 2026-10-18 22:00 received; last sent 2026-10-05 09:00" in lines
    document = _stats_json(plugin, *plain, "--tz", "UTC")
    assert document["range"]["since"] == "2026-10-01T00:00:00+00:00"
    assert document["range"]["until"] == "2026-10-18T23:59:59.999999+00:00"
    assert document["contacts"][0]["latest"]["received_at"] == "2026-10-18T22:00:00+00:00"


@pytest.mark.parametrize("value", ["Mars/Base", "../etc", ""])
def test_invalid_tz(plugin, monkeypatch: pytest.MonkeyPatch, value: str):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 18, 9, 0), "inbound"))
    hint = "use an IANA name such as Europe/Moscow"

    exit_code, output = _stats(plugin, "--contact", "@alice", "--tz", value)
    assert exit_code == 1
    assert output == f"error: unknown timezone {value!r} in --tz; {hint}"

    exit_code, output = _stats(plugin, "--tz", value, "--format", "json")
    assert exit_code == 1
    assert output == f"error: unknown timezone {value!r} in --tz; {hint}"

    if value:
        monkeypatch.setenv(TIMEZONE_ENV, value)
        exit_code, output = _stats(plugin, "--contact", "@alice")
        assert exit_code == 1
        assert output == f"error: unknown timezone {value!r} in {TIMEZONE_ENV}; {hint}"


def test_invalid_bounds_and_gaps(plugin):
    _seed(plugin, *(_msg(plugin, mid, _utc(2026, 10, 18, mid, 0), "inbound") for mid in (1, 3, 6)))

    for value in ("yesterday", "2026-10", "2026-02-30", "99999999999d"):
        assert _stats(plugin, "--contact", "@alice", "--since", value) == (
            1,
            f"error: unsupported --since value: {value}",
        )
    assert _stats(plugin, "--contact", "@alice", "--until", "9999-12-31") == (
        1,
        "error: unsupported --until value: 9999-12-31",
    )
    # A relative bound that lands on 0001-01-01T01:00Z cannot be shown west of UTC.
    assert _stats(plugin, "--contact", "@alice", "--since", "63928002720s", "--tz", "America/New_York") == (
        1,
        "error: unsupported --since value: 63928002720s",
    )
    assert _stats(plugin, "--since", "2026-10-10", "--until", "2026-10-01") == (
        1,
        "error: --until must be greater than or equal to --since",
    )
    for value in ("0h", "6", "1.5h", "-1h", "6H", ""):
        assert _stats(plugin, "--contact", "@alice", f"--session-gap={value}") == (
            1,
            f"error: unsupported --session-gap value: {value}; use a positive duration such as 90m, 6h, or 1d",
        )
    for value, label, sessions in (("90m", "90m", 3), ("1d", "1d", 1), ("1w", "7d", 1), ("150s", "150s", 3)):
        exit_code, output = _stats(plugin, "--contact", "@alice", "--session-gap", value)
        assert exit_code == 0
        assert any(line.startswith(f"  sessions {sessions} (gap {label})") for line in output.splitlines()), output
    assert _stats_json(plugin, "--contact", "@alice", "--session-gap", "90m")["range"]["session_gap_seconds"] == 5400


def test_active_days_follow_tz(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 5, 22, 30), "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 6, 1, 0), "outbound"),
        # Same-side pairs that straddle Moscow midnight pin the per-side day counts.
        _msg(plugin, 11, _utc(2026, 10, 5, 22, 30), "inbound", chat=992, profile=BOB),
        _msg(plugin, 12, _utc(2026, 10, 6, 1, 0), "inbound", chat=992, profile=BOB),
        _msg(plugin, 13, _utc(2026, 10, 7, 22, 30), "outbound", chat=992, profile=BOB),
        _msg(plugin, 14, _utc(2026, 10, 8, 1, 0), "outbound", chat=992, profile=BOB),
    )

    assert _contact_json(plugin, "--contact", "@alice")["active_days"] == {"total": 2, "received": 1, "sent": 1}
    assert _contact_json(plugin, "--contact", "@alice", "--tz", "Europe/Moscow")["active_days"] == {
        "total": 1,
        "received": 1,
        "sent": 1,
    }

    assert _contact_json(plugin, "--contact", "@bob")["active_days"] == {"total": 4, "received": 2, "sent": 2}
    assert _contact_json(plugin, "--contact", "@bob", "--tz", "Europe/Moscow")["active_days"] == {
        "total": 2,
        "received": 1,
        "sent": 1,
    }
    exit_code, output = _stats(plugin, "--contact", "@bob", "--tz", "Europe/Moscow")
    assert exit_code == 0
    assert "  active days 2 (received 1, sent 1)" in output.splitlines()


# --- Sessions --------------------------------------------------------------------


def test_session_gap_strict(plugin):
    start = _utc(2026, 10, 10, 0, 0)
    _seed(
        plugin,
        _msg(plugin, 1, start, "inbound"),
        _msg(plugin, 2, start + timedelta(hours=6), "inbound"),
        _msg(plugin, 3, start + timedelta(hours=12, seconds=1), "inbound"),
    )

    assert _contact_json(plugin, "--contact", "@alice")["sessions"]["total"] == 2
    assert _contact_json(plugin, "--contact", "@alice", "--session-gap", "90m")["sessions"]["total"] == 3
    assert _contact_json(plugin, "--contact", "@alice", "--session-gap", "1d")["sessions"]["total"] == 1


def test_session_starters_and_endings(plugin):
    day = timedelta(days=1)
    start = _utc(2026, 10, 10, 9, 0)
    _seed(
        plugin,
        _msg(plugin, 1, start, "inbound"),
        _msg(plugin, 2, start + day, "outbound"),
        _msg(plugin, 3, start + day + timedelta(minutes=5), "inbound"),
        _msg(plugin, 4, start + 2 * day, "unknown"),
    )

    exit_code, output = _stats(plugin, "--contact", "@alice")

    assert exit_code == 0
    assert (
        "  sessions 3 (gap 6h): started by contact 1, by owner 1, unknown 1; ended on received 2" in output.splitlines()
    )
    assert _contact_json(plugin, "--contact", "@alice")["sessions"] == {
        "total": 3,
        "started_by_contact": 1,
        "started_by_owner": 1,
        "started_by_unknown": 1,
        "ended_on_received": 2,
    }


@pytest.mark.parametrize(("with_unknown", "sessions"), [(True, 1), (False, 2)])
def test_unknown_bridges_sessions(plugin, with_unknown: bool, sessions: int):
    start = _utc(2026, 10, 10, 0, 0)
    records = [_msg(plugin, 1, start, "inbound"), _msg(plugin, 3, start + timedelta(hours=10), "outbound")]
    if with_unknown:
        records.append(_msg(plugin, 2, start + timedelta(hours=5), "unknown"))
    _seed(plugin, *records)

    assert _contact_json(plugin, "--contact", "@alice")["sessions"]["total"] == sessions


def test_session_cut_at_range_start(plugin):
    since = _utc(2026, 10, 10, 0, 0)
    _seed(
        plugin,
        _msg(plugin, 1, since - timedelta(hours=1), "outbound"),
        _msg(plugin, 2, since + timedelta(hours=1), "inbound"),
    )

    cut = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-10")["sessions"]
    assert (cut["total"], cut["started_by_contact"], cut["started_by_owner"]) == (1, 1, 0)
    whole = _contact_json(plugin, "--contact", "@alice")["sessions"]
    assert (whole["total"], whole["started_by_contact"], whole["started_by_owner"]) == (1, 0, 1)


def test_replaced_excluded_from_sessions_and_latest(plugin):
    start = _utc(2026, 10, 10, 0, 0)
    later = start + timedelta(hours=10)
    duplicate = _tomb(plugin, 2, later + timedelta(minutes=1), "outbound", message_at=later)
    _seed(
        plugin,
        _msg(plugin, 1, start, "inbound"),
        _msg(plugin, 2, later, "outbound"),
        duplicate,
        _cls(plugin, duplicate, "likely_duplicate", later + timedelta(minutes=5), "normalized_exact_duplicate"),
    )

    contact = _contact_json(plugin, "--contact", "@alice")

    assert contact["messages"]["total"] == 1
    assert contact["sessions"]["total"] == 1
    assert contact["sessions"]["ended_on_received"] == 1
    assert contact["latest"]["at"] == "2026-10-10T00:00:00+00:00"
    assert contact["latest"]["observed_reply"] == "none"


# --- Latest and last message -----------------------------------------------------


def test_reply_states(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 12, 9, 0), "outbound"),
        _msg(plugin, 2, _utc(2026, 10, 18, 9, 0), "inbound"),
    )

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert "  last message: received 2026-10-18 09:00, no observed reply after it" in output.splitlines()
    assert _contact_json(plugin, "--contact", "@alice")["latest"]["observed_reply"] == "none"

    # An unknown-direction message after the range is not an observed reply.
    _seed(plugin, _msg(plugin, 4, _utc(2026, 10, 19, 6, 0), "unknown"))
    exit_code, output = _stats(plugin, "--contact", "@alice", "--until", "2026-10-18")
    assert exit_code == 0
    assert "  last message: received 2026-10-18 09:00, no observed reply after it" in output.splitlines()
    assert _contact_json(plugin, "--contact", "@alice", "--until", "2026-10-18")["latest"]["observed_reply"] == "none"
    # Inside the range it is the latest message, so there is no reply state at all.
    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert (
        "  latest 2026-10-19 06:00 unknown direction; last received 2026-10-18 09:00; last sent 2026-10-12 09:00"
        in output.splitlines()
    )
    assert "last message:" not in output
    assert _contact_json(plugin, "--contact", "@alice")["latest"]["observed_reply"] is None

    _seed(plugin, _msg(plugin, 3, _utc(2026, 10, 19, 7, 0), "outbound"))
    exit_code, output = _stats(plugin, "--contact", "@alice", "--until", "2026-10-18")
    assert exit_code == 0
    assert "  last message: received 2026-10-18 09:00, reply observed after the range" in output.splitlines()
    assert (
        _contact_json(plugin, "--contact", "@alice", "--until", "2026-10-18")["latest"]["observed_reply"]
        == "after_range"
    )

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert "  latest 2026-10-19 07:00 sent; last received 2026-10-18 09:00" in output.splitlines()
    assert "last message:" not in output
    assert _contact_json(plugin, "--contact", "@alice")["latest"]["observed_reply"] is None

    exit_code, output = _stats(plugin, "--contact", "@alice", "--since", "2026-10-19T08:00Z")
    assert exit_code == 0
    lines = output.splitlines()
    assert "  latest none" in lines
    assert "  sessions 0 (gap 6h)" in lines
    assert "  messages 0: received 0, sent 0, unknown direction 0" in lines
    assert _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-19T08:00Z")["latest"] == {
        "at": None,
        "side": None,
        "received_at": None,
        "sent_at": None,
        "observed_reply": None,
    }


def test_reply_tie_broken_by_message_id(plugin):
    same = _utc(2026, 10, 18, 9, 0)
    first, later = same + timedelta(seconds=1), same + timedelta(seconds=2)
    # The higher id is written and observed first, so neither file order, observation
    # order, nor string order of ids gives the expected (send time, numeric id) order.
    _seed(
        plugin,
        _msg(plugin, 10, same, "outbound", observed=first),
        _msg(plugin, 9, same, "inbound", observed=later),
        _msg(plugin, 100, same, "inbound", observed=first, chat=992, profile=BOB),
        _msg(plugin, 21, same, "outbound", observed=later, chat=992, profile=BOB),
    )

    alice = _contact_json(plugin, "--contact", "@alice")["latest"]
    assert alice["side"] == "sent"
    assert alice["observed_reply"] is None
    bob = _contact_json(plugin, "--contact", "@bob")["latest"]
    assert bob["side"] == "received"
    assert bob["observed_reply"] == "none"


def test_unknown_latest_line(plugin):
    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 18, 9, 0), "unknown"))

    exit_code, output = _stats(plugin, "--contact", "@alice")

    assert exit_code == 0
    assert "  latest 2026-10-18 09:00 unknown direction; no received message; no sent message" in output.splitlines()
    assert (
        "  sessions 1 (gap 6h): started by contact 0, by owner 0, unknown 1; ended on received 0" in output.splitlines()
    )
    # Machine-readable values stay the bare "unknown", not the text label.
    assert _contact_json(plugin, "--contact", "@alice")["latest"]["side"] == "unknown"
    document = _stats_json(plugin)
    assert [contact["latest"]["side"] for contact in document["contacts"]] == ["unknown"]

    exit_code, output = _stats(plugin)
    assert exit_code == 0
    header, row = output.splitlines()[1:3]
    assert header == (
        "latest            last     msgs  recv  sent  unk  days  sess  del  unexpl  partial  connection    chat  contact"
    )
    assert row == (
        "2026-10-18 09:00  unknown     1     0     0    1     1     1    0       0  no       business-123  991   "
        "Alice Example (@alice)"
    )


# --- Overview --------------------------------------------------------------------


def test_overview_sort_and_activity(plugin):
    records = [
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound", chat=991, profile=_profile(991, "aaa")),
        _msg(plugin, 1, _utc(2026, 10, 12, 9, 0), "inbound", chat=992, profile=_profile(992, "bbb")),
        _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound", chat=993, profile=_profile(993, "ccc")),
        _tomb(plugin, 1, _utc(2026, 10, 14, 9, 0), "inbound", message_at=_utc(2026, 10, 1, 9, 0), chat=993),
        _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound", chat=994, profile=_profile(994, "ddd")),
        _tomb(plugin, 2, _utc(2026, 10, 14, 9, 0), "unknown", chat=994),
        _msg(
            plugin, 1, _utc(2026, 10, 11, 9, 0), "inbound", conn="business-456", chat=990, profile=_profile(990, "eee")
        ),
        _msg(plugin, 1, _utc(2026, 10, 11, 9, 0), "inbound", chat=996, profile=_profile(996, "fff")),
        # More deletion-only rows. ggg's latest deletion (10-15) is newer than ccc's (10-14),
        # but its earliest (10-06) is older than hhh's (10-13), so max vs min matters.
        _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound", chat=997, profile=_profile(997, "ggg")),
        _msg(plugin, 2, _utc(2026, 10, 1, 9, 1), "inbound", chat=997, profile=_profile(997, "ggg")),
        _tomb(plugin, 1, _utc(2026, 10, 6, 9, 0), "inbound", message_at=_utc(2026, 10, 1, 9, 0), chat=997),
        _tomb(plugin, 2, _utc(2026, 10, 15, 9, 0), "inbound", message_at=_utc(2026, 10, 1, 9, 1), chat=997),
        _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound", chat=998, profile=_profile(998, "hhh")),
        _tomb(plugin, 1, _utc(2026, 10, 13, 9, 0), "inbound", message_at=_utc(2026, 10, 1, 9, 0), chat=998),
    ]
    _seed(plugin, *records)
    expected = [
        ("business-123", 992, "Bbb (@bbb)"),
        ("business-123", 996, "Fff (@fff)"),
        ("business-456", 990, "Eee (@eee)"),
        ("business-123", 991, "Aaa (@aaa)"),
        ("business-123", 997, "Ggg (@ggg)"),
        ("business-123", 993, "Ccc (@ccc)"),
        ("business-123", 998, "Hhh (@hhh)"),
    ]

    document = _stats_json(plugin, "--since", "2026-10-05")
    assert [
        (contact["business_connection_id"], contact["chat_id"], contact["display_name"])
        for contact in document["contacts"]
    ] == expected
    assert [contact["deletions"]["total"] for contact in document["contacts"]] == [0, 0, 0, 0, 2, 1, 1]
    assert document["overview"]["not_shown_no_activity"] == 1

    exit_code, output = _stats(plugin, "--since", "2026-10-05")
    assert exit_code == 0
    lines = output.splitlines()
    assert len(lines) == 1 + 1 + 7 + 2
    assert [line.rsplit("  ", 1)[-1] for line in lines[2:9]] == [name for _, _, name in expected]
    assert "ddd" not in output
    assert lines[-2:] == [
        "shown 7 of 8 contacts; not shown: 1 with no activity in range",
        # ddd's only deletion is of a message not in history: not a row, but still reported.
        "missing history in contacts with no activity in range: 1 deletion of a message not in history; "
        + "use --contact for details",
    ]

    # --limit cuts the oldest deletion-only row, not the most recently active one.
    limited = _stats_json(plugin, "--since", "2026-10-05", "--limit", "6")
    assert [contact["chat_id"] for contact in limited["contacts"]] == [chat for _, chat, _ in expected[:6]]
    assert limited["overview"]["not_shown_over_limit"] == 1


def test_overview_default_limit_20(plugin):
    _seed(
        plugin,
        *(
            _msg(plugin, 1, _utc(2026, 10, 10, 9, minute), "inbound", chat=chat, profile=_profile(chat, f"user{chat}"))
            for minute, chat in enumerate(range(2001, 2022))
        ),
    )

    exit_code, output = _stats(plugin)

    assert exit_code == 0
    lines = output.splitlines()
    assert len(lines) == 1 + 1 + 20 + 1
    assert lines[-1] == "shown 20 of 21 contacts; not shown: 1 over --limit 20"
    assert "user2001" not in output
    assert "user2021" in lines[2]


def test_same_chat_two_connections(plugin):
    alice_two = dict(ALICE, username="alice2")
    second_tomb = _tomb(
        plugin, 2, _utc(2026, 10, 10, 9, 5), "inbound", message_at=_utc(2026, 10, 10, 9, 0), conn="business-456"
    )
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 10, 9, 0), "inbound", conn="business-456", profile=alice_two),
        second_tomb,
        _cls(plugin, second_tomb, "unexplained", _utc(2026, 10, 10, 9, 10), "no_strong_match"),
    )

    document = _stats_json(plugin)
    assert sorted(contact["business_connection_id"] for contact in document["contacts"]) == [
        "business-123",
        "business-456",
    ]
    assert _stats(plugin, "--chat", "991") == (
        1,
        "error: multiple history chats match chat=991; pass --connection explicitly",
    )
    exit_code, output = _stats(plugin, "--connection", "business-456", "--chat", "991")
    assert exit_code == 0
    assert output.splitlines()[1] == "Alice Example (@alice2) connection=business-456 chat=991"

    exit_code, output = _stats(plugin, "--connection", "business-456")
    assert exit_code == 0
    lines = output.splitlines()
    assert len(lines) == 5
    assert lines[2].endswith("Alice Example (@alice2)")
    assert lines[3] == "shown 1 of 1 contacts"
    assert (
        lines[4]
        == "deletion details: hermes telesec history deletions --connection business-456 --status unexplained"
    )


def test_group_chat_named_by_title(plugin):
    team = {"type": "group", "title": "Team"}
    ops = {"type": "supergroup", "title": "Ops Room", "username": "opsroom"}
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound", chat=-1001234, profile=team),
        _msg(plugin, 1, _utc(2026, 10, 9, 9, 0), "inbound", chat=-1005678, profile=ops),
    )

    contact = _contact_json(plugin, "--connection", "business-123", "--chat=-1001234")
    assert (contact["chat_id"], contact["chat_type"]) == (-1001234, "group")
    assert (contact["display_name"], contact["name"], contact["username"]) == ("Team", "Team", None)
    exit_code, output = _stats(plugin, "--contact", "Team")
    assert exit_code == 0
    assert output.splitlines()[1] == "Team connection=business-123 chat=-1001234"

    contact = _contact_json(plugin, "--contact", "@opsroom")
    assert (contact["chat_type"], contact["display_name"], contact["name"], contact["username"]) == (
        "supergroup",
        "Ops Room (@opsroom)",
        "Ops Room",
        "opsroom",
    )

    document = _stats_json(plugin)
    assert [contact["display_name"] for contact in document["contacts"]] == ["Team", "Ops Room (@opsroom)"]
    exit_code, output = _stats(plugin)
    assert exit_code == 0
    rows = output.splitlines()[2:4]
    assert [row.rsplit("  ", 1)[-1] for row in rows] == ["Team", "Ops Room (@opsroom)"]
    assert rows[0].endswith("business-123  -1001234  Team")
    assert rows[1].endswith("business-123  -1005678  Ops Room (@opsroom)")
    assert "chat:" not in output


def test_details_command_runs_for_dash_leading_connection(plugin, cs):
    conn = "-Qx3_Ab-9"
    sent = _utc(2026, 10, 10, 9, 0)
    tomb = _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent, conn=conn, chat=992)
    _seed(
        plugin,
        _msg(plugin, 1, sent, "inbound", conn=conn, chat=992, profile=BOB),
        tomb,
        _cls(plugin, tomb, "unexplained", sent + timedelta(minutes=5), "no_strong_match"),
    )
    contact_command = f"hermes telesec history deletions --connection={conn} --chat 992 --status unexplained"
    overview_command = f"hermes telesec history deletions --connection={conn} --status unexplained"

    exit_code, output = _stats(plugin, "--contact", "@bob")
    assert exit_code == 0
    assert f"  deletion details: {contact_command}" in output.splitlines()
    assert _contact_json(plugin, "--contact", "@bob")["deletions"]["details_command"] == contact_command
    exit_code, output = _stats(plugin, f"--connection={conn}")
    assert exit_code == 0
    assert output.splitlines()[-1] == f"deletion details: {overview_command}"

    for command in (contact_command, overview_command):
        argv = shlex.split(command)
        assert argv[:2] == ["hermes", "telesec"]
        exit_code, listed = _run_history_cli(plugin, *argv[2:])
        assert exit_code == 0, listed
        assert listed != "No matching deletions found."
        assert len(listed.splitlines()) == 1

    # Quoting still applies, and a negative chat id is attached the same way.
    odd = cs._details_command("-a b", -100)
    assert odd == "hermes telesec history deletions --connection='-a b' --chat=-100 --status unexplained"
    parsed = _build_history_parser(plugin).parse_args(shlex.split(odd)[2:])
    assert (parsed.connection, parsed.chat, parsed.status) == ("-a b", "-100", "unexplained")


def test_overview_no_activity_and_empty(plugin):
    history = plugin._history_support

    exit_code, output = _stats(plugin)
    assert (exit_code, output) == (0, "No Telegram Business history found.")
    assert not history.history_root().exists()
    document = _stats_json(plugin)
    assert document["contacts"] == []
    assert document["overview"] == {
        "contacts_in_scope": 0,
        "active": 0,
        "shown": 0,
        "limit": 20,
        "not_shown_over_limit": 0,
        "not_shown_no_activity": 0,
        "not_shown_no_activity_partial": 0,
        "not_shown_no_activity_missing_history": {
            "deletions_of_messages_not_in_history": 0,
            "messages_known_only_from_edit": 0,
        },
    }
    assert not history.history_root().exists()

    _seed(plugin, _msg(plugin, 1, _utc(2026, 10, 1, 9, 0), "inbound"))
    exit_code, output = _stats(plugin, "--since", "2026-10-10")
    assert exit_code == 0
    assert output.splitlines() == [
        "range 2026-10-10..now UTC, by Telegram send time; text messages only",
        "No contacts with activity in range.",
        "shown 0 of 1 contacts; not shown: 1 with no activity in range",
    ]


# --- Resolver, safety, read-only, fail-closed ------------------------------------


def test_resolver(plugin):
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 10, 9, 0), "inbound"),
        _msg(plugin, 2, _utc(2026, 10, 10, 9, 0), "inbound", chat=992, profile=dict(ALICE, username="alice-two")),
    )

    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 0
    assert output.splitlines()[1] == "Alice Example (@alice) connection=business-123 chat=991"
    assert _stats(plugin, "--contact", "@x") == (1, "error: no matching contact found for '@x'")
    exit_code, output = _stats(plugin, "--contact", "Alice Example")
    assert exit_code == 1
    assert output.startswith("error: contact 'Alice Example' is ambiguous; choose one of: ")
    assert _stats(plugin, "--chat", "991", "--contact", "@alice") == (
        1,
        "error: pass either --chat or --contact, not both",
    )
    assert _stats(plugin, "--contact", "") == (1, "error: pass --chat or --contact")


def test_message_text_never_printed(plugin):
    _overview_fixture(plugin)

    for argv in (("--contact", "@alice"), ()):
        exit_code, output = _stats(plugin, *argv, *MOSCOW_RANGE)
        assert exit_code == 0
        assert "SECRET-" not in output
        exit_code, output = _stats(plugin, *argv, *MOSCOW_RANGE, "--format", "json")
        assert exit_code == 0
        assert "SECRET-" not in output
        assert "text" not in _keys_everywhere(json.loads(output))


def test_untrusted_names_escaped(plugin):
    hostile = "Ev\x1b[31mil\u202e"
    profile = {"id": 991, "type": "private", "first_name": hostile, "last_name": "Twin\x9b"}
    raw_name = f"{hostile} Twin\x9b"
    escaped = "Ev\\x1b[31mil\\u202e Twin\\x9b"
    sent = _utc(2026, 10, 10, 9, 0)
    hostile_tomb = _tomb(plugin, 1, sent + timedelta(minutes=1), "inbound", message_at=sent)
    _seed(
        plugin,
        _msg(plugin, 1, sent, "inbound", profile=profile),
        hostile_tomb,
        _cls(plugin, hostile_tomb, "unexplained", sent + timedelta(minutes=5), "no_strong_match"),
        _msg(plugin, 2, sent, "inbound", chat=992, profile=profile),
    )

    outputs = [
        _stats(plugin, "--connection", "business-123", "--chat", "991"),
        _stats(plugin),
        _stats(plugin, "--contact", raw_name),
    ]

    assert outputs[0][0] == 0
    assert outputs[0][1].splitlines()[1] == f"{escaped} connection=business-123 chat=991"
    assert outputs[1][0] == 0
    assert any(line.endswith(f"  {escaped}") for line in outputs[1][1].splitlines())
    assert outputs[2][0] == 1
    assert "is ambiguous" in outputs[2][1]
    assert f"business-123:991={escaped}" in outputs[2][1]
    for _, output in outputs:
        assert "\x1b" not in output
        assert "\u202e" not in output
        assert "\x9b" not in output

    exit_code, raw_json = _stats(plugin, "--chat", "991", "--connection", "business-123", "--format", "json")
    assert exit_code == 0
    assert raw_json.isascii()
    assert json.loads(raw_json)["contacts"][0]["display_name"] == raw_name


def test_read_only(plugin, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    _overview_fixture(plugin)
    before = snapshot_history_state(plugin)
    for name in (
        "_root_writer_lock",
        "_chat_writer_lock",
        "_append_record",
        "_write_json_atomic",
        "_rebuild_catalog_locked",
        "rebuild_contact_catalog",
        "_repair_torn_tail",
        "maintain_history",
        "_classify_due_for_chat",
        "_load_records",
        "_load_chat_state",
        "_build_chat_state",
    ):
        monkeypatch.setattr(history, name, lambda *_args, _name=name, **_kwargs: pytest.fail(f"stats called {_name}"))

    for argv in (("--contact", "@alice"), ()):
        for output_format in ("text", "json"):
            exit_code, output = _stats(plugin, *argv, *MOSCOW_RANGE, "--format", output_format)
            assert exit_code == 0, output

    assert snapshot_history_state(plugin) == before


def _break_history(plugin, case: str) -> str:
    history = plugin._history_support
    stale = "error: history contact catalog is stale; run 'hermes telesec history catalog --rebuild'"
    if case == "stale":
        append_raw_history_records(plugin, _msg(plugin, 999, _utc(2026, 10, 18, 22, 0), "inbound"))
        return stale
    if case == "dirty":
        catalog_dirty_path(plugin).write_text("{}", encoding="utf-8")
        return "error: history contact catalog is dirty; run 'hermes telesec history catalog --rebuild'"
    if case == "root_lock":
        history._root_lock_path().unlink()
        return "error: history root lock is uninitialized; "
    if case == "chat_lock":
        history._chat_lock_path(history._history_chat_dir_path("business-123", 991)).unlink()
        return "error: history chat lock is uninitialized for "
    if case == "all_locks":
        remove_history_locks(plugin)
        return "error: history root lock is uninitialized; "
    assert case == "torn_tail"
    with th.load_history_file(plugin, month="2026-10.jsonl").open("ab") as handle:
        handle.write(b'{"event_id":"broken"')
    return stale


@pytest.mark.parametrize("mode", ["contact", "overview"])
@pytest.mark.parametrize("case", ["stale", "dirty", "root_lock", "chat_lock", "all_locks", "torn_tail"])
def test_fail_closed(plugin, mode: str, case: str):
    _alice_fixture(plugin)
    expected = _break_history(plugin, case)
    before = snapshot_history_state(plugin)
    argv = ("--contact", "@alice") if mode == "contact" else ()

    for output_format in ("text", "json"):
        exit_code, output = _stats(plugin, *argv, "--format", output_format)
        assert exit_code == 1
        assert output.startswith(expected), output
        assert len(output.splitlines()) == 1

    assert snapshot_history_state(plugin) == before


def test_collect_contact_stats_rejects_torn_tail(plugin, cs):
    history = plugin._history_support
    _alice_fixture(plugin)
    entry = history._resolve_history_entry(business_connection_id=None, chat_id=None, contact="@alice")
    with th.load_history_file(plugin, month="2026-10.jsonl").open("ab") as handle:
        handle.write(b'{"event_id":"broken"')

    with pytest.raises(ValueError, match="torn or invalid tail"):
        cs.collect_contact_stats(entry, _window(cs))


def test_invalid_json_line_fails_closed(plugin, cs):
    history = plugin._history_support
    _alice_fixture(plugin)
    entry = history._resolve_history_entry(business_connection_id=None, chat_id=None, contact="@alice")
    with th.load_history_file(plugin, month="2026-10.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not json\n")

    with pytest.raises(ValueError, match="invalid JSON"):
        cs.collect_contact_stats(entry, _window(cs))


def test_invalid_utf8_line_fails_closed(plugin, cs, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    _alice_fixture(plugin)
    entry = history._resolve_history_entry(business_connection_id=None, chat_id=None, contact="@alice")
    with th.load_history_file(plugin, month="2026-10.jsonl").open("ab") as handle:
        handle.write(b"\xff\xfe not utf-8\n")
    before = snapshot_history_state(plugin)

    with pytest.raises(ValueError):
        cs.collect_contact_stats(entry, _window(cs))
    # Past the (now stale) catalog check, the CLI still prints one error line and changes nothing.
    monkeypatch.setattr(history, "_resolve_history_entry", lambda **_kwargs: entry)
    exit_code, output = _stats(plugin, "--contact", "@alice")
    assert exit_code == 1
    assert output.startswith("error: ") and len(output.splitlines()) == 1, output
    assert snapshot_history_state(plugin) == before


# --- Partitions ------------------------------------------------------------------


def test_partition_selection_slack(plugin, cs, monkeypatch: pytest.MonkeyPatch):
    history = plugin._history_support
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 7, 10, 9, 0), "inbound"),
        _msg(plugin, 2, _utc(2026, 8, 10, 9, 0), "inbound"),
        _msg(plugin, 3, _utc(2026, 9, 10, 9, 0), "inbound"),
        _msg(plugin, 4, _utc(2026, 10, 1, 0, 30), "inbound", observed=_utc(2026, 9, 30, 23, 50)),
        _msg(plugin, 5, _utc(2026, 10, 10, 9, 0), "inbound"),
    )
    calls: list[tuple[datetime | None, datetime | None, list[str]]] = []
    original = history._iter_selected_history_files

    def _spy(chat_dir: Path, *, since: datetime | None, until: datetime | None):
        paths = list(original(chat_dir, since=since, until=until))
        calls.append((since, until, [path.stem for path in paths]))
        return iter(paths)

    monkeypatch.setattr(history, "_iter_selected_history_files", _spy)

    _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-01", "--tz", "Europe/Moscow")
    assert calls[-1] == (_utc(2026, 9, 1), None, ["2026-09", "2026-10"])

    _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-04", "--tz", "Europe/Moscow")
    assert calls[-1] == (_utc(2026, 10, 1), None, ["2026-10"])

    skewed = _contact_json(plugin, "--contact", "@alice", "--since", "2026-10-01", "--tz", "UTC")
    assert skewed["messages"]["total"] == 2

    _contact_json(plugin, "--contact", "@alice")
    assert calls[-1] == (None, None, ["2026-07", "2026-08", "2026-09", "2026-10"])

    # The floor is the UTC month start even when a caller passes a non-UTC bound.
    window = cs.StatsWindow(
        since=datetime(2026, 10, 3, 22, 0, tzinfo=ZoneInfo("America/New_York")),
        until=None,
        tz=UTC,
        tz_label="UTC",
        session_gap=timedelta(hours=6),
        now=NOW,
    )
    assert window.read_since == _utc(2026, 10, 1)


def test_late_records_apply(plugin):
    second_tomb = _tomb(plugin, 2, _utc(2026, 10, 12, 9, 0), "inbound", message_at=_utc(2026, 10, 6, 9, 0))
    third_tomb = _tomb(plugin, 3, _utc(2026, 10, 12, 9, 0), "inbound", message_at=_utc(2026, 10, 7, 9, 0))
    _seed(
        plugin,
        _msg(plugin, 1, _utc(2026, 10, 5, 9, 0), "unknown"),
        _msg(plugin, 1, _utc(2026, 10, 5, 9, 0), "outbound", edited=True, observed=_utc(2026, 10, 15, 9, 0)),
        _msg(plugin, 2, _utc(2026, 10, 6, 9, 0), "inbound"),
        second_tomb,
        _cls(plugin, second_tomb, "likely_duplicate", _utc(2026, 10, 13, 9, 0), "normalized_exact_duplicate"),
        _msg(plugin, 3, _utc(2026, 10, 7, 9, 0), "inbound"),
        third_tomb,
        _cls(plugin, third_tomb, "unexplained", _utc(2026, 10, 13, 9, 0), "no_strong_match"),
    )

    contact = _contact_json(plugin, "--contact", "@alice", "--until", "2026-10-10")

    assert contact["messages"] == {"total": 2, "received": 1, "sent": 1, "unknown_direction": 0}
    assert contact["deletions"]["total"] == 0


# --- Wiring ----------------------------------------------------------------------


def test_cli_wiring(plugin):
    history = plugin._history_support
    parser = _build_history_parser(plugin)

    assert parser.parse_args(["stats"])._history_action == "correspondence_stats"
    assert parser.parse_args(["history", "stats"])._history_action == "stats"
    exit_code, output = _run_history_cli(plugin, "history", "stats")
    assert exit_code == 0
    assert output.startswith("enabled=")

    def _help(*argv: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), pytest.raises(SystemExit):
            _build_history_parser(plugin).parse_args([*argv, "--help"])
        return buffer.getvalue()

    assert "storage stats" in _help("history")
    # Parallel branches (#7 mode, #17 dossiers) register their own top-level
    # subcommands, so only the relative order of history and stats is pinned.
    subcommands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    names = list(subcommands.choices)
    assert {"history", "stats"} <= set(names)
    assert names.index("history") < names.index("stats")
    assert "Show correspondence stats per contact" in parser.format_help()
    stats_help = _help("stats")
    for needle in ("--tz", "--session-gap", "examples:", "--format", "storage stats"):
        assert needle in stats_help
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        parser.parse_args(["stats", "--format", "xml"])

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert history.handle_cli(argparse.Namespace()) == 1
    assert buffer.getvalue().startswith("Usage: hermes telesec history <")


@pytest.mark.asyncio
async def test_capture_end_to_end(enabled_history):
    plugin = enabled_history
    history = plugin._history_support
    base = _utc(2026, 10, 18, 10, 0)
    second = timedelta(seconds=1)
    # Captured before any FakeBot owner lookup for business-123: once an owner is
    # known for the connection (in memory or persisted), direction may be resolved
    # without a bot, and this message would no longer be unknown.
    await history.observe_ptb_update(
        make_business_text_update(text="no owner lookup", message_id=3, update_id=3, date=base),
        bot=None,
        now=base,
    )
    await history.observe_ptb_update(
        make_business_text_update(text="hello there", message_id=1, update_id=1, date=base + second),
        bot=FakeBot(),
        now=base + second,
    )
    await history.observe_ptb_update(
        make_business_text_update(
            text="owner reply", message_id=2, update_id=2, date=base + 2 * second, from_user_id=1000
        ),
        bot=FakeBot(),
        now=base + 2 * second,
    )
    await history.observe_ptb_update(
        make_deleted_update(message_ids=(1,), update_id=4),
        bot=FakeBot(),
        now=base + 3 * second,
    )
    history.maintain_history(now=base + timedelta(seconds=20))
    history.rebuild_contact_catalog()

    exit_code, output = _stats(plugin, "--contact", "@customer991")

    assert exit_code == 0
    lines = output.splitlines()
    assert "  messages 3: received 1, sent 1, unknown direction 1" in lines
    assert "  deletions 1: unexplained 1 (received 1, sent 0)" in lines
    assert (
        "  deletion details: hermes telesec history deletions --connection business-123 --chat 991 "
        "--status unexplained"
    ) in lines
    for text in ("hello there", "owner reply", "no owner lookup"):
        assert text not in output


def test_skill_has_stats_recipe(plugin):
    _overview_fixture(plugin)
    skill = SKILL_DOC.read_text(encoding="utf-8")
    section = _doc_section(skill, "Stats Recipes")

    assert "hermes telesec stats --contact" in section
    assert "TELESEC_TIMEZONE" in skill
    assert "history stats   # storage stats" in skill
    assert "Do not over-interpret" in section and "no observed reply after it" in section

    # Every recipe runs, and every JSON field the recipe names exists in real output.
    documents = []
    commands = _documented_stats_commands(section)
    assert len(commands) >= 4
    for argv in commands:
        exit_code, output = _run_history_cli(plugin, *argv)
        assert exit_code == 0, (argv, output)
        assert "SECRET-" not in output
        if argv[-2:] == ["--format", "json"]:
            documents.append(json.loads(output))
    by_mode = {document["mode"]: document for document in documents}
    assert set(by_mode) == {"contact", "overview"}
    references = set(re.findall(r"`((?:messages|latest|deletions|missing_history|overview)\.[a-z_]+)", section))
    assert {
        "messages.total",
        "latest.observed_reply",
        "deletions.details_command",
        "missing_history.range_partial",
        "overview.not_shown_over_limit",
        "overview.not_shown_no_activity_partial",
    } <= references
    for reference in references:
        block, key = reference.split(".")
        container = by_mode["overview"]["overview"] if block == "overview" else by_mode["contact"]["contacts"][0][block]
        assert key in container, reference
    assert "`unknown_direction`" in section and "unknown_direction" in by_mode["contact"]["contacts"][0]["messages"]
