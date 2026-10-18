"""Correspondence reports use synthetic canonical #2 fixtures exclusively."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from test_history import (
    _run_history_cli,
    FakeBot,
    make_business_text_update,
    append_raw_history_records,
    make_history_record,
    _load_plugin_module,
    snapshot_history_state,
)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    loaded = _load_plugin_module(monkeypatch, tmp_path)
    yield loaded
    loaded._history_support.reset_in_memory_caches()


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def record(plugin, mid, *, at=BASE, sent=BASE, event="message.created", direction="inbound", **extra):
    return make_history_record(
        plugin,
        event_type=event,
        source={
            "message.created": "business_message",
            "message.edited": "edited_business_message",
            "message.deleted": "deleted_business_messages",
            "deletion.classified": "deleted_business_messages",
        }[event],
        observed_at=at,
        message_at=sent,
        message_id=mid,
        direction=direction,
        telegram_update_id=int(at.timestamp()) + mid,
        text="PRIVATE ARCHIVED TEXT",
        **extra,
    )


def seed(plugin, *records):
    append_raw_history_records(plugin, *records)
    plugin._history_support.rebuild_contact_catalog()


def report(plugin, *args):
    code, output = _run_history_cli(plugin, "stats", "--format", "json", *args)
    assert code == 0, output
    assert "PRIVATE" not in output
    return json.loads(output)


def chat(plugin, *args):
    return report(plugin, "--chat", "991", *args)["contacts"][0]


def deletion(plugin, mid, *, status="unexplained", at=BASE, classified_at=None, **extra):
    tombstone = record(plugin, mid, event="message.deleted", at=at)
    classification = record(
        plugin,
        mid,
        event="deletion.classified",
        at=classified_at or at + timedelta(seconds=10),
        deleted_event_id=tombstone["event_id"],
        classification=status,
        deleted_observed_at=at,
        **extra,
    )
    return tombstone, classification


def test_dedup_latest_edit_original_send_time_and_cross_partition(plugin):
    created = record(plugin, 1, sent=BASE - timedelta(days=1))
    edit = record(
        plugin,
        1,
        at=BASE + timedelta(days=40),
        sent=BASE + timedelta(days=40),
        event="message.edited",
        direction="outbound",
    )
    fallback = record(plugin, 2, at=BASE + timedelta(hours=1), sent=None)
    fallback_edit = record(plugin, 2, at=BASE + timedelta(days=40), sent=None, event="message.edited")
    edit_only = record(plugin, 3, at=BASE + timedelta(days=40), sent=BASE, event="message.edited")
    seed(plugin, created, created, edit, fallback, fallback_edit, edit_only)
    before = snapshot_history_state(plugin)
    result = chat(plugin, "--since", "2026-09-30", "--until", "2026-10-01")
    assert result["messages"] == {"total": 3, "received": 2, "sent": 1, "unknown": 0}
    assert result["missing_history"]["edit_only_messages"] == 1
    assert result["missing_history"]["send_time_fallback_messages"] == 1
    assert result["latest_activity"]["any"] == "2026-10-01T01:00:00Z"
    assert result["missing_history"]["range_starts_before_retained"] is True
    assert snapshot_history_state(plugin) == before
    assert chat(plugin, "--since", "2026-11-01")["messages"]["total"] == 0


def test_deletion_exclusions_latest_status_and_observation_range(plugin):
    rows = [record(plugin, mid) for mid in range(1, 7)]
    for mid, status in enumerate(("likely_duplicate", "likely_correction", "unexplained", "unclassifiable"), 1):
        rows.extend(
            deletion(plugin, mid, status=status, at=BASE + timedelta(hours=1), classified_at=BASE + timedelta(days=40))
        )
    rows.extend(
        deletion(
            plugin, 99, status="unclassifiable", at=BASE + timedelta(hours=2), classification_reason="missing_original"
        )
    )
    rows.append(record(plugin, 5, event="message.deleted", at=BASE + timedelta(hours=2)))
    # Later classification overrides an earlier classification, without a second deletion.
    tomb, old = deletion(plugin, 6, status="likely_duplicate", at=BASE + timedelta(hours=2))
    rows.extend(
        [
            tomb,
            old,
            record(
                plugin,
                6,
                event="deletion.classified",
                at=BASE + timedelta(days=41),
                deleted_event_id=tomb["event_id"],
                classification="unexplained",
            ),
        ]
    )
    seed(plugin, *rows)
    result = chat(plugin, "--since", "2026-10-01", "--until", "2026-10-01")
    assert result["messages"]["total"] == 4
    assert result["deletions"] == {
        "total": 7,
        "unexplained": 2,
        "likely_correction": 1,
        "likely_duplicate": 1,
        "unclassifiable": 2,
        "pending": 1,
        "unexplained_by_actor": {"contact": 0, "owner": 0, "unknown": 2},
    }
    assert result["missing_history"]["deletions_without_original"] == 1
    assert chat(plugin, "--since", "2026-11-01")["deletions"]["total"] == 0


def test_classification_without_tombstone_never_uses_classification_time(plugin):
    tomb, classified = deletion(plugin, 1, at=BASE, classified_at=BASE + timedelta(days=40))
    no_time = record(
        plugin,
        2,
        event="deletion.classified",
        at=BASE + timedelta(days=40),
        classification="unclassifiable",
        deleted_event_id="pruned-tombstone",
    )
    seed(plugin, classified, no_time)
    result = chat(plugin, "--since", "2026-10-01", "--until", "2026-10-01")
    assert result["deletions"]["total"] == 1
    assert result["missing_history"]["deletions_without_original"] == 1
    assert result["missing_history"]["deletions_without_observation_time"] == 1
    assert chat(plugin, "--since", "2026-11-01")["deletions"]["total"] == 0


def test_edit_only_evidence_is_counted_separately_from_missing_original_deletions(plugin):
    seed(
        plugin,
        record(plugin, 1, event="message.edited"),
        *deletion(
            plugin, 1, at=BASE + timedelta(hours=1), status="unclassifiable", classification_reason="missing_original"
        ),
    )
    result = chat(plugin)
    assert result["messages"]["total"] == 1
    assert result["missing_history"]["edit_only_messages"] == 1
    assert result["missing_history"]["deletions_without_original"] == 1


def test_sessions_strict_gap_unknown_days_and_last_message(plugin):
    times = [0, 6, 12, 18, 25, 32]
    directions = ["inbound", "unknown", "outbound", "inbound", "outbound", "inbound"]
    seed(
        plugin,
        *(
            record(plugin, i, at=BASE + timedelta(hours=h), sent=BASE + timedelta(hours=h), direction=d)
            for i, (h, d) in enumerate(zip(times, directions), 1)
        ),
    )
    result = chat(plugin)
    assert result["messages"] == {"total": 6, "received": 3, "sent": 2, "unknown": 1}
    assert result["active_days"] == {"total": 2, "received": 2, "sent": 2, "unknown": 1}
    assert result["sessions"] == {
        "total": 3,
        "started_by": {"contact": 2, "owner": 1, "unknown": 0},
        "ended_without_observed_reply": 2,
    }
    assert result["last_message_no_observed_reply"] is True
    clipped = chat(plugin, "--since", "2026-10-01T05:00:00Z", "--until", "2026-10-01T13:00:00Z")
    assert clipped["sessions"]["started_by"]["unknown"] == 1
    assert clipped["sessions"]["total"] == 1
    assert clipped["last_message_no_observed_reply"] is False
    assert chat(plugin, "--session-gap", "90m")["sessions"]["total"] == 6


@pytest.mark.parametrize(("first_id", "last_id"), [(9, 10), (99, 100)])
@pytest.mark.parametrize("first_direction", ["inbound", "outbound"])
def test_same_second_messages_follow_numeric_id_order(plugin, first_id, last_id, first_direction):
    last_direction = "outbound" if first_direction == "inbound" else "inbound"
    # Reverse observation/file order to require the message ID tie-break.
    seed(
        plugin,
        record(plugin, last_id, direction=last_direction),
        record(plugin, first_id, direction=first_direction),
    )
    before = snapshot_history_state(plugin)
    result = chat(plugin, "--since", "2026-10-01", "--until", "2026-10-01")
    ends_received = last_direction == "inbound"
    assert result["latest_activity"] == {
        "any": "2026-10-01T00:00:00Z",
        "received": "2026-10-01T00:00:00Z",
        "sent": "2026-10-01T00:00:00Z",
        "unknown": None,
        "direction": "received" if ends_received else "sent",
    }
    assert result["last_message_no_observed_reply"] is ends_received
    assert result["sessions"] == {
        "total": 1,
        "started_by": {
            "contact": int(first_direction == "inbound"),
            "owner": int(first_direction == "outbound"),
            "unknown": 0,
        },
        "ended_without_observed_reply": int(ends_received),
    }
    assert snapshot_history_state(plugin) == before


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        ("2026-03-29", "2026-03-28T23:00:00Z", "2026-03-29T21:59:59.999999Z"),
        ("2026-10-25", "2026-10-24T22:00:00Z", "2026-10-25T22:59:59.999999Z"),
    ],
)
def test_local_date_inclusive_dst_and_end_microsecond(plugin, day, start, end):
    parse = plugin._history_support._parse_datetime
    left, right = parse(start), parse(end)
    times = [left - timedelta(microseconds=1), left, right, right + timedelta(microseconds=1)]
    seed(plugin, *(record(plugin, i, at=at, sent=at) for i, at in enumerate(times, 1)))
    result = report(plugin, "--since", day, "--until", day, "--tz", "Europe/Berlin")
    assert result["since"] == start
    assert result["until"] == end
    assert result["contacts"][0]["messages"]["total"] == 2
    assert result["contacts"][0]["active_days"]["total"] == 1


def test_relative_history_formats_share_one_clock_and_timezone_env(plugin, monkeypatch):
    history = plugin._history_support
    monkeypatch.setattr(history, "_utcnow", lambda: BASE + timedelta(days=7))
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_TIMEZONE", "Europe/Moscow")
    seed(plugin, record(plugin, 1))
    for duration in ["7d", "1w", "168h", "10080m", "604800s"]:
        result = report(plugin, "--since", duration, "--until", "0s")
        assert result["timezone"] == "Europe/Moscow"
        assert result["contacts"][0]["messages"]["total"] == 1
    assert report(plugin, "--tz", "UTC")["timezone"] == "UTC"


def test_all_contacts_limit_sort_after_filter_and_identity(plugin):
    rows = []
    for mid in range(25):
        # Same message ID in separate chats/connections must count separately.
        rows.append(
            record(
                plugin,
                1,
                at=BASE + timedelta(days=mid),
                sent=BASE + timedelta(hours=mid),
                chat_id=100 + mid,
                business_connection_id="one",
            )
        )
    rows.append(record(plugin, 1, at=BASE + timedelta(days=50), sent=BASE, chat_id=124, business_connection_id="two"))
    seed(plugin, *rows)
    result = report(plugin, "--until", "2026-10-01T05:00:00Z")
    assert result["contacts_returned"] == 20
    assert result["contacts"][0]["chat_id"] == 105
    assert sum(c["messages"]["total"] for c in result["contacts"]) == 7
    assert len(report(plugin, "--limit", "3")["contacts"]) == 3
    assert report(plugin, "--connection", "two")["contacts_total"] == 1
    code, output = _run_history_cli(plugin, "stats", "--chat", "124")
    assert code == 1 and "pass --connection explicitly" in output
    assert report(plugin, "--chat", "124", "--connection", "two")["contacts"][0]["messages"]["total"] == 1


def test_contact_resolver_ambiguity_and_text_privacy(plugin):
    first = record(plugin, 1)
    first["chat_profile"] = {"id": 991, "type": "private", "first_name": "Alice", "username": "alice_example"}
    second = record(plugin, 2, chat_id=992)
    second["chat_profile"] = {"id": 992, "type": "private", "first_name": "Alice", "username": "other"}
    seed(plugin, first, second)
    code, output = _run_history_cli(plugin, "stats", "--contact", "Alice")
    assert code == 1 and "ambiguous" in output
    assert report(plugin, "--contact", "@alice_example")["contacts"][0]["chat_id"] == 991
    code, output = _run_history_cli(plugin, "stats", "--contact", "@alice_example")
    assert code == 0 and "text messages only" in output and "PRIVATE" not in output
    assert "no observed reply after it" in output
    assert "actor unknown" in output


@pytest.mark.parametrize(
    "args",
    [
        ("--tz", "Fake/Timezone"),
        ("--since", "PRIVATE BAD DATE"),
        ("--until", "2026-02-30"),
        ("--session-gap", "0h"),
        ("--session-gap", "PRIVATE"),
        ("--since", "2026-10-02", "--until", "2026-10-01"),
        ("--chat", "991", "--contact", "alice"),
    ],
)
def test_invalid_options_are_actionable_without_echoing_arbitrary_values(plugin, args):
    code, output = _run_history_cli(plugin, "stats", *args)
    assert code == 1 and "error:" in output
    assert "PRIVATE" not in output


def test_missing_profile_and_read_locks_do_not_create_state(plugin):
    assert report(plugin)["contacts"] == []
    assert not plugin._history_support.history_root().exists()
    seed(plugin, record(plugin, 1))
    plugin._history_support._chat_lock_path(
        plugin._history_support._history_chat_dir_path("business-123", 991)
    ).unlink()
    before = snapshot_history_state(plugin)
    code, output = _run_history_cli(plugin, "stats")
    assert code == 1 and "lock is uninitialized" in output
    assert snapshot_history_state(plugin) == before


@pytest.mark.parametrize("damage", ["stale", "dirty", "torn", "json", "metadata", "utf8"])
def test_error_paths_do_not_mutate_or_leak_archive(plugin, damage, monkeypatch):
    seed(plugin, record(plugin, 1))
    history = plugin._history_support
    path = next(history.history_root().glob("*/*/*.jsonl"))
    if damage == "stale":
        path.write_bytes(path.read_bytes() + b"\n")
    elif damage == "dirty":
        history._catalog_dirty_path().write_text("PRIVATE")
    else:
        # Keep the catalog signature fresh to exercise the canonical scanner too.
        if damage == "torn":
            path.write_bytes(path.read_bytes()[:-1])
        elif damage == "json":
            path.write_bytes(path.read_bytes() + b'{"PRIVATE invalid json\n')
        elif damage == "utf8":
            path.write_bytes(path.read_bytes() + b"\xffPRIVATE\n")
        else:
            bad = record(plugin, 2)
            bad["direction"] = "PRIVATE"
            path.write_text(path.read_text() + json.dumps(bad) + "\n")
        monkeypatch.setattr(history, "_catalog_is_fresh_locked", lambda *_: True)
        original_status = history._contact_catalog_status_locked

        def force_fresh(**kwargs):
            result = original_status(**kwargs)
            return history.CatalogStatus(state="ok", catalog=result.catalog)

        monkeypatch.setattr(history, "_contact_catalog_status_locked", force_fresh)
    before = snapshot_history_state(plugin)
    code, output = _run_history_cli(plugin, "stats", "--format", "json")
    assert code == 1 and "PRIVATE" not in output
    assert "history catalog --rebuild" in output or "history verify" in output
    assert snapshot_history_state(plugin) == before


def test_storage_stats_remains_available_and_help_distinguishes_commands(plugin):
    code, output = _run_history_cli(plugin, "history", "stats")
    assert code == 0 and "record_count=" in output and "total_bytes=" in output
    from test_history import _build_history_parser

    parser = _build_history_parser(plugin)
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(["history", "stats", "--help"])
    assert exited.value.code == 0


def test_latest_edit_direction_wins_over_delayed_create_observation(plugin):
    seed(
        plugin,
        record(plugin, 1, at=BASE + timedelta(hours=3), direction="inbound"),
        record(plugin, 1, at=BASE + timedelta(hours=1), event="message.edited", direction="outbound"),
        record(plugin, 1, at=BASE + timedelta(hours=2), event="message.edited", direction="unknown"),
    )
    assert chat(plugin)["messages"] == {"total": 1, "received": 0, "sent": 0, "unknown": 1}
    assert chat(plugin)["sessions"]["started_by"]["unknown"] == 1
    assert chat(plugin)["last_message_no_observed_reply"] is False


def test_activity_sort_compares_instants_including_fractional_seconds(plugin):
    seed(plugin, record(plugin, 1, chat_id=101), record(plugin, 1, chat_id=102, sent=BASE + timedelta(microseconds=1)))
    assert [c["chat_id"] for c in report(plugin)["contacts"]] == [102, 101]


def test_replacement_deleted_outside_range_is_still_excluded(plugin):
    seed(plugin, record(plugin, 1), *deletion(plugin, 1, status="likely_correction", at=BASE + timedelta(days=40)))
    result = chat(plugin, "--since", "2026-10-01", "--until", "2026-10-01")
    assert result["messages"]["total"] == 0
    assert result["deletions"]["total"] == 0


def test_pruned_partitions_and_original_evidence_are_not_cached(plugin):
    previous = BASE - timedelta(days=1)
    seed(
        plugin,
        record(plugin, 1, at=previous, sent=previous),
        record(plugin, 2),
        record(plugin, 1, event="message.edited", sent=previous),
    )
    assert chat(plugin)["messages"]["total"] == 2
    history = plugin._history_support
    chat_dir = history._history_chat_dir_path("business-123", 991)
    (chat_dir / "2026-09.jsonl").unlink()  # Simulate explicit retention outside the read path.
    history.rebuild_contact_catalog()
    before = snapshot_history_state(plugin)
    result = chat(plugin, "--since", "2026-09-01")
    assert result["messages"]["total"] == 2  # The retained edit is still a legitimate message.
    assert result["missing_history"]["edit_only_messages"] == 1
    assert result["missing_history"]["retained_from"] == "2026-10-01T00:00:00Z"
    assert result["missing_history"]["range_starts_before_retained"] is True
    assert result["missing_history"]["completeness"] == "unknown"
    assert snapshot_history_state(plugin) == before


def test_local_active_days_and_iso_offset_equivalence(plugin):
    left = BASE - timedelta(hours=2)
    right = BASE + timedelta(hours=1)
    seed(plugin, record(plugin, 1, at=left, sent=left), record(plugin, 2, at=right, sent=right))
    assert chat(plugin, "--tz", "UTC")["active_days"]["total"] == 2
    assert chat(plugin, "--tz", "Europe/Moscow")["active_days"]["total"] == 1
    assert chat(plugin, "--since", "2026-10-01T00:00:00+03:00")["messages"]["total"] == 2
    assert chat(plugin, "--since", "2026-10-01T00:00:00")["messages"]["total"] == 1


def test_root_lock_missing_does_not_initialize_it(plugin):
    seed(plugin, record(plugin, 1))
    history = plugin._history_support
    history._root_lock_path().unlink()
    before = snapshot_history_state(plugin)
    code, output = _run_history_cli(plugin, "stats")
    assert code == 1 and "root lock is uninitialized" in output
    assert snapshot_history_state(plugin) == before


def test_synthetic_example_runs_registered_cli_and_cleans_fixture(tmp_path):
    import os
    import subprocess
    import sys
    from test_history import ROOT

    examples = ROOT / "docs" / "examples"
    before = set(examples.iterdir())
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, pathlib, runpy, sys\n"
                "def check_scratch(event, args):\n"
                "    if event == 'tempfile.mkdtemp':\n"
                "        assert pathlib.Path(args[0]).parent == pathlib.Path(os.environ['TMPDIR'])\n"
                "sys.addaudithook(check_scratch)\n"
                "sys.argv = sys.argv[1:]\n"
                "runpy.run_path(sys.argv[0], run_name='__main__')\n"
            ),
            str(examples / "correspondence_stats.py"),
            "stats",
            "--contact",
            "@alice_example",
            "--since",
            "2026-09-19",
            "--until",
            "2026-10-19",
            "--tz",
            "Europe/Moscow",
        ],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "messages 5: received 3, sent 1, unknown direction 1" in result.stdout
    assert "deletions 3: unexplained 1 (actor unknown)" in result.stdout
    assert "edit-only messages counted 1" in result.stdout
    assert "Fictional fixture text" not in result.stdout
    assert set(examples.iterdir()) == before
    assert list(tmp_path.iterdir()) == []


def test_equal_observation_edits_and_classifications_use_canonical_file_order(plugin):
    first_edit = record(plugin, 1, event="message.edited", direction="outbound")
    last_edit = record(plugin, 1, event="message.edited", direction="unknown")
    last_edit["event_id"] = "0000000000000000000000000000000000000000000000000000000000000000"
    tomb, first_classification = deletion(plugin, 1, status="likely_correction")
    last_classification = {**first_classification, "classification": "unexplained", "event_id": "latest-in-file"}
    seed(plugin, record(plugin, 1), first_edit, last_edit, tomb, first_classification, last_classification)
    result = chat(plugin)
    assert result["messages"] == {"total": 1, "received": 0, "sent": 0, "unknown": 1}
    assert result["deletions"]["unexplained"] == 1


def test_out_of_order_classification_and_deleted_observation_join(plugin):
    tomb, classified = deletion(plugin, 1, at=BASE + timedelta(hours=1), classified_at=BASE)
    seed(plugin, classified, record(plugin, 1), tomb)
    assert chat(plugin)["deletions"]["unexplained"] == 1
    assert chat(plugin)["deletions"]["pending"] == 0


def test_regressed_clock_latest_edit_matches_canonical_state(plugin):
    seed(
        plugin,
        record(plugin, 1),
        record(plugin, 1, at=BASE + timedelta(hours=3), event="message.edited", direction="outbound"),
        record(plugin, 1, at=BASE + timedelta(hours=2), event="message.edited", direction="unknown"),
    )
    history = plugin._history_support
    state, _ = history._load_chat_state_from_disk(
        history._history_chat_dir_path("business-123", 991), repair_tails=False
    )
    assert state.messages["1"].direction == "unknown"
    before = snapshot_history_state(plugin)
    result = chat(plugin)
    assert result["messages"] == {"total": 1, "received": 0, "sent": 0, "unknown": 1}
    assert result["latest_activity"]["direction"] == "unknown"
    assert result["sessions"]["started_by"]["unknown"] == 1
    assert snapshot_history_state(plugin) == before


@pytest.mark.asyncio
async def test_fake_capture_regressed_clock_latest_edit(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE", "1")
    history = plugin._history_support
    bot = FakeBot()
    for update_id, (hour, sender) in enumerate([(0, 2000), (3, 1000), (2, None)], 1):
        assert await history.observe_ptb_update(
            make_business_text_update(
                message_id=1,
                update_id=update_id,
                date=BASE,
                edited=update_id > 1,
                from_user_id=sender,
            ),
            bot=bot,
            now=BASE + timedelta(hours=hour),
        )
    state, _ = history._load_chat_state_from_disk(
        history._history_chat_dir_path("business-123", 991), repair_tails=False
    )
    assert state.messages["1"].direction == "unknown"
    before = snapshot_history_state(plugin)
    assert chat(plugin)["messages"] == {"total": 1, "received": 0, "sent": 0, "unknown": 1}
    assert snapshot_history_state(plugin) == before


@pytest.mark.parametrize("first_event", ["message.created", "message.edited"])
def test_regressed_clock_fallback_uses_first_source_observation(plugin, first_event):
    seed(
        plugin,
        record(plugin, 1, at=BASE + timedelta(hours=3), sent=None, event=first_event),
        record(plugin, 1, at=BASE + timedelta(hours=2), sent=None, event="message.edited", direction="outbound"),
    )
    result = chat(plugin, "--since", "2026-10-01T02:30:00Z")
    assert result["messages"] == {"total": 1, "received": 0, "sent": 1, "unknown": 0}
    assert result["latest_activity"]["any"] == "2026-10-01T03:00:00Z"
    assert result["missing_history"]["send_time_fallback_messages"] == 1
    assert result["missing_history"]["retained_from"] == "2026-10-01T02:00:00Z"
    assert result["missing_history"]["completeness"] == "unknown"
    assert chat(plugin, "--until", "2026-10-01T02:30:00Z")["messages"]["total"] == 0


@pytest.mark.parametrize("classification_first", [False, True])
def test_regressed_clock_last_superseding_classification(plugin, classification_first):
    tomb, first = deletion(
        plugin, 1, at=BASE + timedelta(hours=1),
        classified_at=BASE + timedelta(hours=3), status="likely_correction",
    )
    last = record(
        plugin,
        1,
        event="deletion.classified",
        at=BASE + timedelta(hours=2),
        deleted_event_id=tomb["event_id"],
        deleted_observed_at=BASE + timedelta(hours=1),
        classification="unexplained",
    )
    rows = [first, last, tomb] if classification_first else [tomb, first, last]
    seed(plugin, record(plugin, 1), *rows)
    result = chat(plugin, "--until", "2026-10-01T01:30:00Z")
    assert result["messages"]["total"] == 1
    assert result["deletions"]["total"] == 1
    assert result["deletions"]["unexplained"] == 1
    assert result["deletions"]["likely_correction"] == 0
    assert result["deletions"]["pending"] == 0
