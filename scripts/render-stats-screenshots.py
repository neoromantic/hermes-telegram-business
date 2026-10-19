#!/usr/bin/env python3
"""Write the rich message HTML of the stats views over fictional history, for real Telegram screenshots.

Builds fictional history with the test helpers: one contact (Alex Rivera, in
Russian Алекс) and the owner (Sam, in Russian Макс), about 2,000 text messages
over 12 months with a daily and weekly rhythm and a fixed seed. Then it runs
the real views: the owner panel (``stats_panel.build_stats_view``, 30 days) and
the public in-chat card (``stats_panel.build_stats_card``, all time), in
English and Russian. Each view's exact Bot API rich message HTML goes to
``docs/images/src/stats-{panel,card}-{en,ru}.html``. Send one with
sendRichMessage to take a real Telegram screenshot; the PNGs in
``docs/images/`` are such screenshots, not made here.

Run from the repository root:

    uv run --python 3.13 --with pytest --with pyyaml --with python-telegram-bot==22.8 \\
        python scripts/render-stats-screenshots.py

Nothing here touches real history: HERMES_HOME is a temporary directory.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "docs" / "images" / "src"
sys.path.insert(0, str(ROOT / "tests"))

import test_history as th  # needs tests/ on sys.path

SEED = 24
CONNECTION = "business-demo"
CHAT_ID = 4242
OWNER_ID = 1000
CONTACT_ID = 4242
FIRST_DAY = date(2025, 10, 21)
LAST_DAY = date(2026, 10, 19)
NOW_LOCAL = time(14, 32)

LANGUAGES = {
    "en": {
        "tz": "Europe/Berlin",
        "contact": {"first_name": "Alex", "last_name": "Rivera", "username": "alex_rivera"},
        "owner": "Sam",
    },
    "ru": {
        "tz": "Europe/Moscow",
        "contact": {"first_name": "Алекс", "username": "alex_rivera"},
        "owner": "Макс",
    },
}

# --- Fictional history -----------------------------------------------------------------------------

# Relative chance that a conversation starts in a local hour: mornings, lunch, and a late evening peak.
# fmt: off
HOUR_WEIGHTS = [
    0.2, 0.1, 0, 0, 0, 0, 0.1, 0.5, 1.6, 2.4, 2.0, 1.6,  # 0:00 to 11:00
    1.5, 1.9, 1.3, 1.1, 1.2, 1.4, 1.9, 2.4, 2.8, 3.4, 2.6, 1.0,  # 12:00 to 23:00
]
# fmt: on
# Chance of any conversation on a weekday, Monday first.
WEEKDAY_CHANCE = [0.82, 0.93, 0.74, 0.86, 0.7, 0.42, 0.36]
# Days with no messages at all: a holiday trip, and a quiet day before the current streak.
QUIET = {date(2026, 8, 3) + timedelta(days=offset) for offset in range(5)} | {LAST_DAY - timedelta(days=9)}


def _trend(day: date) -> float:
    # Slow growth over the year and a busy December.
    progress = (day - FIRST_DAY).days / (LAST_DAY - FIRST_DAY).days
    level = 0.75 + 0.35 * progress
    if day.month == 12:
        level += 0.2
    return level


def _length(rng: random.Random, median: float) -> int:
    return max(1, min(600, round(rng.lognormvariate(math.log(median), 0.75))))


def _delay(rng: random.Random, median_minutes: float) -> timedelta:
    minutes = rng.lognormvariate(math.log(median_minutes), 1.0)
    return timedelta(minutes=min(minutes, 5.5 * 60))


def _turn_size(rng: random.Random, mean: float) -> int:
    # 1 + geometric, so the mean is ``mean``.
    size, extra = 1, 1 - 1 / mean
    while rng.random() < extra and size < 8:
        size += 1
    return size


def _session(rng: random.Random, start: datetime) -> list[tuple[datetime, str]]:
    """(local send time, direction) for one conversation."""
    side = "inbound" if rng.random() < 0.57 else "outbound"
    turns = 2 + min(7, int(rng.expovariate(1 / 1.9)))
    if rng.random() < 0.12:
        turns = 1
    at, messages = start, []
    for turn in range(turns):
        if turn:
            at += _delay(rng, 9 if side == "inbound" else 24)
        for index in range(_turn_size(rng, 1.8 if side == "inbound" else 1.25)):
            if index:
                at += timedelta(seconds=rng.randint(12, 150))
            messages.append((at, side))
        side = "outbound" if side == "inbound" else "inbound"
    return messages


def _timeline(tz: ZoneInfo) -> list[tuple[datetime, str]]:
    rng = random.Random(SEED)
    now = datetime.combine(LAST_DAY, NOW_LOCAL, tzinfo=tz)
    timeline: list[tuple[datetime, str]] = []
    day = FIRST_DAY
    while day <= LAST_DAY:
        chance = min(0.97, WEEKDAY_CHANCE[day.weekday()] * _trend(day))
        if day == FIRST_DAY or (LAST_DAY - day).days < 9:
            chance = 1.0  # the first day, and a streak that is still running
        if day not in QUIET and rng.random() < chance:
            sessions = 1 + (rng.random() < 0.6 * _trend(day)) + (rng.random() < 0.3)
            for _ in range(sessions):
                hour = rng.choices(range(24), weights=HOUR_WEIGHTS)[0]
                start = datetime.combine(day, time(hour, rng.randint(0, 59), rng.randint(0, 59)), tzinfo=tz)
                timeline.extend(_session(rng, start))
        day += timedelta(days=1)
    # Today: a morning exchange, then Alex's two messages at noon wait for an answer.
    today = datetime.combine(LAST_DAY, time(0), tzinfo=tz)
    timeline = [item for item in timeline if item[0] < today + timedelta(hours=11)]
    for hh, mm, ss, side in ((12, 4, 0, "inbound"), (12, 5, 10, "inbound")):
        timeline.append((today.replace(hour=hh, minute=mm, second=ss), side))
    return sorted(item for item in timeline if item[0] < now)


def _text(rng: random.Random, length: int) -> str:
    # Placeholder text of the right length; stats only count characters, never show text.
    words, size = [], 0
    while size < length:
        word = "".join(rng.choice("etaoinshrdlu") for _ in range(rng.randint(2, 8)))
        words.append(word)
        size += len(word) + 1
    return " ".join(words)[:length].strip() or "ok"


def build_records(plugin, language: str) -> list[dict]:
    spec = LANGUAGES[language]
    tz = ZoneInfo(spec["tz"])
    rng = random.Random(SEED + 1)
    profile = {"id": CHAT_ID, "type": "private", **spec["contact"]}
    update_ids = iter(range(100_000, 10_000_000))
    records: list[dict] = []
    recent = datetime.combine(LAST_DAY - timedelta(days=29), time(0), tzinfo=tz)

    def message(mid: int, at: datetime, direction: str, text: str, *, edited_at: datetime | None = None) -> dict:
        record = th.make_history_record(
            plugin,
            event_type="message.created" if edited_at is None else "message.edited",
            source="business_message" if edited_at is None else "edited_business_message",
            observed_at=(edited_at or at + timedelta(seconds=1)).astimezone(UTC),
            telegram_update_id=next(update_ids),
            business_connection_id=CONNECTION,
            chat_id=CHAT_ID,
            message_id=mid,
            message_at=at.astimezone(UTC),
            sender_id=CONTACT_ID if direction == "inbound" else OWNER_ID,
            direction=direction,
            text=text,
        )
        record["chat_profile"] = dict(profile)
        records.append(record)
        return record

    def deletion(created: dict, observed: datetime, classification: str, replacement: int | None) -> None:
        history = plugin._history_support
        tomb = th.make_history_record(
            plugin,
            event_type="message.deleted",
            source="deleted_business_messages",
            observed_at=observed.astimezone(UTC),
            telegram_update_id=next(update_ids),
            business_connection_id=CONNECTION,
            chat_id=CHAT_ID,
            message_id=created["message_id"],
            message_at=history._parse_datetime(created["message_at"]),
            sender_id=created["sender_id"],
            direction=created["direction"],
        )
        verdict = th.make_history_record(
            plugin,
            event_type="deletion.classified",
            source=tomb["source"],
            observed_at=(observed + timedelta(seconds=30)).astimezone(UTC),
            telegram_update_id=tomb["telegram_update_id"],
            business_connection_id=CONNECTION,
            chat_id=CHAT_ID,
            message_id=tomb["message_id"],
            message_at=history._parse_datetime(tomb["message_at"]),
            sender_id=tomb["sender_id"],
            direction=tomb["direction"],
            deleted_event_id=tomb["event_id"],
            classification=classification,
            replacement_message_id=replacement,
            classification_reason="no_strong_match" if replacement is None else "high_similarity_small_edit",
            classification_score=None if replacement is None else 0.91,
            evaluated_at=(observed + timedelta(seconds=30)).astimezone(UTC),
            deleted_observed_at=observed.astimezone(UTC),
        )
        records.extend((tomb, verdict))

    corrections, unexplained = 2, 1
    mid = 1
    for at, direction in _timeline(tz):
        length = _length(rng, 42 if direction == "inbound" else 18)
        created = message(mid, at, direction, _text(rng, length))
        if rng.random() < 0.035:
            message(mid, at, direction, _text(rng, length + rng.randint(-3, 6)), edited_at=at + timedelta(minutes=2))
        if at >= recent and rng.random() < 0.02:
            if corrections:
                # Deleted and sent again with a fix: history keeps both, stats count only the new one.
                corrections -= 1
                mid += 1
                fixed = message(mid, at + timedelta(seconds=40), direction, _text(rng, length))
                deletion(created, at + timedelta(seconds=45), "likely_correction", fixed["message_id"])
            elif unexplained:
                unexplained -= 1
                deletion(created, at + timedelta(hours=3), "unexplained", None)
        mid += 1
    return records


# --- Real views ------------------------------------------------------------------------------------


def render_views(language: str) -> dict[str, object]:
    spec = LANGUAGES[language]
    tz = ZoneInfo(spec["tz"])
    now = datetime.combine(LAST_DAY, NOW_LOCAL, tzinfo=tz).astimezone(UTC)
    with tempfile.TemporaryDirectory() as home, pytest.MonkeyPatch.context() as mp:
        plugin = th._load_plugin_module(mp, Path(home))
        mp.setitem(sys.modules, "hermes_time", SimpleNamespace(get_timezone_name=lambda: spec["tz"]))
        for name in list(os.environ):
            # The shell's own Telesec settings must not change the pictures.
            if name.startswith(("TELESEC_", "HERMES_TELEGRAM_BUSINESS_")):
                mp.delenv(name)
        th.set_switch(plugin, "history", True)
        history = plugin._history_support
        history.reset_in_memory_caches()
        mp.setattr(history, "_utcnow", lambda: now)
        th.append_raw_history_records(plugin, *build_records(plugin, language))
        history.rebuild_contact_catalog()
        stats_panel = sys.modules[f"{plugin.__name__}.stats_panel"]
        owner_panel = sys.modules[f"{plugin.__name__}.owner_panel"]
        view = stats_panel.build_stats_view(
            chat_id=CHAT_ID,
            business_connection_id=CONNECTION,
            period="30d",
            now=now,
            language_code=language,
        )
        card = stats_panel.build_stats_card(
            chat_id=CHAT_ID,
            business_connection_id=CONNECTION,
            period="all",
            owner_name=spec["owner"],
            contact_name=spec["contact"]["first_name"],
            now=now,
            language_code=language,
        )
        assert card.ok and card.html and view.html, card.text
        rows = [[label for label, _ in view.buttons], [owner_panel.tr("back", language)]]
        history.reset_in_memory_caches()
    return {"panel": view.html, "panel_text": view.text, "card": card.html, "card_text": card.text, "buttons": rows}


# --- Rich message sources ------------------------------------------------------------------------

HEADER = """<!--
  Generated by scripts/render-stats-screenshots.py; do not edit by hand.
  The exact Bot API rich message HTML of the real Telesec view ({what}), over
  fictional people and history. Send it with sendRichMessage to take a real
  Telegram screenshot.
-->
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--print", action="store_true", help="print the views' rich HTML and plain fallbacks")
    args = parser.parse_args()

    SRC_DIR.mkdir(parents=True, exist_ok=True)
    for language in LANGUAGES:
        views = render_views(language)
        if args.print:
            for name in ("panel", "card"):
                print(f"--- {language} {name}\n{views[name]}\n--- {language} {name} (plain fallback)\n"
                      f"{views[name + '_text']}")
            print(f"--- {language} buttons\n{views['buttons']}")
        for name, what in (("panel", "owner panel, 30 days"), ("card", "in-chat /stats card, all time")):
            page = SRC_DIR / f"stats-{name}-{language}.html"
            page.write_text(HEADER.format(what=what) + views[name] + "\n", encoding="utf-8")
            print(page.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
