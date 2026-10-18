"""Reproduce the stats CLI using a disposable, entirely fictional #2 archive.

Run from the repository: python docs/examples/correspondence_stats.py stats ...
Only fixture setup writes files; the report runs the registered read-only CLI.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import history_support as history  # noqa: E402 -- import this checkout, never an installed plugin


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="synthetic-stats-") as directory:
        history.get_hermes_home = lambda: Path(directory)
        chat_dir = history._history_chat_dir_path("business-123", 991)
        history._ensure_history_chat_dir(chat_dir)
        history._ensure_private_file(history._root_lock_path())
        history._ensure_private_file(history._chat_lock_path(chat_dir))
        base = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
        update_id = 0

        def append(mid, hours, *, event="message.created", direction="inbound", sent=None, **extra):
            nonlocal update_id
            update_id += 1
            at = base + timedelta(hours=hours)
            row = history._build_event(
                event_type=event,
                source={
                    "message.created": "business_message",
                    "message.edited": "edited_business_message",
                    "message.deleted": "deleted_business_messages",
                    "deletion.classified": "deleted_business_messages",
                }[event],
                observed_at=at,
                telegram_update_id=update_id,
                business_connection_id="business-123",
                chat_id=991,
                message_id=mid,
                message_at=sent or at,
                sender_id=2000 if direction == "inbound" else 1000,
                direction=direction,
                reply_to_message_id=None,
                text="Fictional fixture text, never printed by stats.",
                chat_profile={
                    "id": 991,
                    "type": "private",
                    "first_name": "Alice",
                    "last_name": "Example",
                    "username": "alice_example",
                },
                **extra,
            )
            history._append_record(chat_dir, row)
            return row

        append(1, 0)
        append(2, 1, direction="outbound")
        append(3, 7, direction="unknown")
        append(4, 15)
        append(5, 16)
        append(6, 17, event="message.edited", sent=base + timedelta(hours=16))
        append(1, 31 * 24, event="message.edited", direction="inbound", sent=base)
        tomb = append(4, 18, event="message.deleted")
        append(
            4,
            31 * 24 + 1,
            event="deletion.classified",
            deleted_event_id=tomb["event_id"],
            classification="likely_correction",
            deleted_observed_at=base + timedelta(hours=18),
        )
        tomb = append(5, 19, event="message.deleted")
        append(
            5,
            31 * 24 + 2,
            event="deletion.classified",
            deleted_event_id=tomb["event_id"],
            classification="unexplained",
            deleted_observed_at=base + timedelta(hours=19),
        )
        tomb = append(99, 20, event="message.deleted")
        append(
            99,
            31 * 24 + 3,
            event="deletion.classified",
            deleted_event_id=tomb["event_id"],
            classification="unclassifiable",
            classification_reason="missing_original",
            deleted_observed_at=base + timedelta(hours=20),
        )
        history.rebuild_contact_catalog()  # Explicit fixture preparation, never part of stats.
        parser = argparse.ArgumentParser(prog="hermes telegram-business")
        history.setup_cli(parser)
        return history.handle_cli(parser.parse_args(argv or ["stats"]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
