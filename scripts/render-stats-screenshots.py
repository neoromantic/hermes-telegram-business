#!/usr/bin/env python3
"""Render the synthetic stats screenshots for the README, docs, and landing page.

Builds fictional history with the test helpers: one contact (Alex Rivera, in
Russian Алекс) and the owner (Sam, in Russian Макс), about 2,000 text messages
over 12 months with a daily and weekly rhythm and a fixed seed. Then it runs
the real views: the owner panel (``stats_panel.build_stats_view``, 30 days) and
the public in-chat card (``stats_panel.build_stats_card``, all time), in
English and Russian. Each view's exact Telegram HTML goes into a Telegram-like
page in ``docs/images/src/``. With ``--png`` the pages are also rendered to
``docs/images/`` at 1x and @2x with agent-browser (Chrome).

Run from the repository root:

    uv run --python 3.13 --with pytest --with pyyaml python scripts/render-stats-screenshots.py --png

Nothing here touches real history: HERMES_HOME is a temporary directory.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "docs" / "images" / "src"
PNG_DIR = ROOT / "docs" / "images"
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
VIEWPORT_WIDTH = 430
BROWSER_SESSION = "telesec-shots"

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

# Page strings around the real views: the chat chrome only, never stats.
CHROME = {
    "en": {
        "bot": "Hermes",
        "bot_status": "bot",
        "today": "Today",
        "composer": "Message",
        "contact": "Alex Rivera",
        "initials": "AR",
        "contact_status": "last seen recently",
        "manages": "bot manages this chat",
        "pause": "Pause",
        "question": "Wait, how much do we actually text? 😄",
        "edited": "edited",
    },
    "ru": {
        "bot": "Hermes",
        "bot_status": "бот",
        "today": "Сегодня",
        "composer": "Сообщение",
        "contact": "Алекс",
        "initials": "А",
        "contact_status": "был(а) недавно",
        "manages": "бот управляет чатом",
        "pause": "Пауза",
        "question": "Слушай, а сколько мы вообще переписываемся? 😄",
        "edited": "изменено",
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
        assert card.ok, card.text
        rows = [[label for label, _ in view.buttons], [owner_panel.tr("back", language)]]
        local_now = now.astimezone(tz).strftime("%H:%M")
        history.reset_in_memory_caches()
    return {"panel": view.text, "buttons": rows, "card": card.text, "time": local_now}


# --- Telegram-like pages ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<!--
  Generated by scripts/render-stats-screenshots.py; do not edit by hand.
  Fictional people and history. The <template id="message"> holds the exact
  Telegram HTML of the real Telesec view ({what}); the script below shows it
  the way Telegram does (newlines next to a <pre> block are dropped).
  Render: {width} px wide, height of the content, deviceScaleFactor 1 and 2.
-->
<html lang="{lang}">
<head>
<meta charset="utf-8">
<title>Telesec stats: {what}</title>
<style>
  :root {{
    --accent: {accent};
    --bubble: {bubble};
    --meta: {meta};
    --hair: rgba(0, 0, 0, 0.09);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ width: {width}px; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
    color: #0b0b0c; -webkit-font-smoothing: antialiased; position: relative;
    background: linear-gradient(160deg, #d3deb0 0%, #b9d39f 45%, #a5c996 100%);
  }}
  .wall {{ position: absolute; inset: 0; overflow: hidden; }}
  .wall svg {{ width: 100%; height: 100%; }}
  .header {{
    position: relative; z-index: 2; height: 58px; display: flex; align-items: center; gap: 11px;
    padding: 0 14px 0 10px; background: rgba(250, 250, 250, 0.96); border-bottom: 1px solid var(--hair);
  }}
  .back {{ width: 22px; height: 22px; stroke: #2f8de4; fill: none; stroke-width: 2.6; stroke-linecap: round; stroke-linejoin: round; }}
  .avatar {{
    width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; flex: none;
    color: #fff; font-weight: 600; font-size: 16px;
  }}
  .avatar.bot {{ background: linear-gradient(180deg, #8f8cff, #6a5df0); }}
  .avatar.alex {{ background: linear-gradient(180deg, #ffb35a, #ff7f5c); }}
  .who {{ display: flex; flex-direction: column; gap: 1px; min-width: 0; }}
  .who .name {{ font-size: 16.5px; font-weight: 600; letter-spacing: -0.2px; }}
  .who .status {{ font-size: 13.5px; color: #8a9099; }}
  .botbar {{
    position: relative; z-index: 2; height: 44px; display: flex; align-items: center; gap: 10px;
    padding: 0 14px; background: rgba(250, 250, 250, 0.96); border-bottom: 1px solid var(--hair);
    font-size: 14px;
  }}
  .botbar .avatar {{ width: 28px; height: 28px; font-size: 13px; }}
  .botbar .name {{ font-weight: 600; }}
  .botbar .status {{ color: #8a9099; flex: 1; }}
  .botbar .pause {{ font-weight: 600; color: #2f8de4; background: rgba(51, 144, 236, 0.11); padding: 5px 13px; border-radius: 14px; }}
  .log {{ position: relative; z-index: 1; display: flex; flex-direction: column; gap: 6px; padding: 12px 8px 12px; }}
  .pill {{
    align-self: center; font-size: 13px; font-weight: 500; color: #fff;
    background: rgba(70, 100, 60, 0.32); padding: 3px 10px; border-radius: 11px; margin-bottom: 4px;
  }}
  .row {{ display: flex; flex-direction: column; }}
  .row.in {{ align-items: flex-start; padding-left: 6px; }}
  .row.out {{ align-items: flex-end; padding-right: 6px; }}
  .bubble {{
    position: relative; max-width: 368px; border-radius: 17px; padding: 7px 11px 6px 12px;
    font-size: 15.5px; line-height: 1.33; letter-spacing: -0.1px;
    box-shadow: 0 1px 1px rgba(30, 50, 20, 0.16);
  }}
  .in .bubble {{ background: #fff; border-bottom-left-radius: 5px; }}
  .out .bubble {{ background: #effedd; border-bottom-right-radius: 5px; }}
  .in .bubble::before, .out .bubble::before {{ content: ""; position: absolute; bottom: 0; width: 14px; height: 18px; }}
  .in .bubble::before {{ left: -9px; background: #fff; clip-path: path("M14 0 Q14 14.5 3 17.6 Q1.5 18 0 18 L14 18 Z"); }}
  .out .bubble::before {{ right: -9px; background: #effedd; clip-path: path("M0 0 Q0 14.5 11 17.6 Q12.5 18 14 18 L0 18 Z"); }}
  .tg {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
  .tg b {{ font-weight: 600; }}
  .tg a {{ color: var(--accent); text-decoration: none; }}
  .tg pre {{
    display: block; margin: 4px 0; padding: 6px 9px 6px 10px; border-radius: 6px;
    background: var(--bubble); border-left: 3px solid var(--accent);
    font-family: Menlo, "SF Mono", ui-monospace, monospace; font-size: 13.4px; line-height: 1.32; color: #23282e;
    letter-spacing: 0; white-space: pre; overflow: hidden; font-variant-ligatures: none;
  }}
  .meta {{
    float: right; margin: 6px -3px -2px 10px; font-size: 12px; line-height: 1;
    color: var(--meta); white-space: nowrap; display: inline-flex; gap: 4px; align-items: center;
  }}
  .meta svg {{ width: 18px; height: 11px; fill: none; stroke: #4aa83f; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }}
  .clear::after {{ content: ""; display: block; clear: both; }}
  .keyboard {{ display: flex; flex-direction: column; gap: 4px; margin-top: 4px; width: 368px; }}
  .keyboard .line {{ display: flex; gap: 4px; }}
  .keyboard .key {{
    flex: 1; height: 38px; border-radius: 10px; display: grid; place-items: center;
    background: rgba(62, 92, 52, 0.34); color: #fff; font-size: 14.5px; font-weight: 600;
    -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px);
  }}
  .composer {{
    position: relative; z-index: 2; height: 54px; display: flex; align-items: center; gap: 12px;
    padding: 0 12px; background: rgba(250, 250, 250, 0.97); border-top: 1px solid var(--hair);
  }}
  .composer svg {{ width: 26px; height: 26px; stroke: #8b939c; fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; flex: none; }}
  .field {{
    flex: 1; height: 36px; border-radius: 18px; background: #fff; border: 1px solid rgba(0, 0, 0, 0.1);
    display: flex; align-items: center; padding: 0 14px; font-size: 15.5px; color: #a3a9b0;
  }}
</style>
</head>
<body>
  <div class="wall">
    <svg xmlns="http://www.w3.org/2000/svg">
      <defs>
        <pattern id="doodles" width="220" height="220" patternUnits="userSpaceOnUse" patternTransform="rotate(-8) scale(0.8)">
          <g fill="none" stroke="#2f5a2a" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity="0.10">
            <path d="M24 52 L78 30 L58 80 L48 62 Z M48 62 L78 30"/>
            <path d="M160 46 c-8-12 -26-6 -22 8 c3 10 22 22 22 22 s19-12 22-22 c4-14 -14-20 -22-8 z"/>
            <path d="M40 150 v-34 l26-6 v34 M40 150 a8 6 0 1 1 -1 -1 M66 144 a8 6 0 1 1 -1 -1"/>
            <path d="M150 112 a10 10 0 0 1 20 0 v20 a10 10 0 0 1 -20 0 z M142 132 a18 18 0 0 0 36 0 M160 150 v12"/>
            <path d="M30 208 a14 14 0 0 1 4 -27 a18 18 0 0 1 34 4 a12 12 0 0 1 2 23 z"/>
            <path d="M126 196 v18 M117 205 h18"/>
            <circle cx="106" cy="92" r="6"/>
            <circle cx="200" cy="190" r="7"/>
          </g>
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill="url(#doodles)"/>
    </svg>
  </div>
{header}
  <main class="log">
    <div class="pill">{today}</div>
{messages}
  </main>
  <footer class="composer">
    <svg viewBox="0 0 26 26"><path d="M17.5 8.5 L10.3 15.7 a2.3 2.3 0 0 0 3.2 3.2 L20.6 11.8 a4.6 4.6 0 0 0 -6.5 -6.5 L6.9 12.5 a6.9 6.9 0 0 0 9.7 9.7 L21.4 17.4"/></svg>
    <div class="field">{composer}</div>
    <svg viewBox="0 0 26 26"><rect x="9" y="3" width="8" height="13" rx="4"/><path d="M5.5 12.5 a7.5 7.5 0 0 0 15 0 M13 20 v3"/></svg>
  </footer>
  <template id="message">{message}</template>
  <script>
    // Telegram shows <pre> as its own block, so a newline right before or after one adds no line.
    const target = document.getElementById("text");
    target.appendChild(document.getElementById("message").content.cloneNode(true));
    for (const pre of target.querySelectorAll("pre")) {{
      const before = pre.previousSibling, after = pre.nextSibling;
      if (before && before.nodeType === Node.TEXT_NODE) before.data = before.data.replace(/\\n$/, "");
      if (after && after.nodeType === Node.TEXT_NODE) after.data = after.data.replace(/^\\n/, "");
    }}
  </script>
</body>
</html>
"""

CHECKS = '<svg viewBox="0 0 18 11"><path d="M1 6 L4.2 9.2 L10.2 2 M7.4 8.2 L8.4 9.2 L14.4 2"/></svg>'


def _panel_page(language: str, views: dict[str, object]) -> str:
    chrome = CHROME[language]
    keyboard = "\n".join(
        '        <div class="line">'
        + "".join(f'<div class="key">{html.escape(label)}</div>' for label in row)
        + "</div>"
        for row in views["buttons"]
    )
    header = f"""  <header class="header">
    <svg class="back" viewBox="0 0 22 22"><path d="M14 4 L7 11 L14 18"/></svg>
    <div class="avatar bot">H</div>
    <div class="who"><div class="name">{chrome["bot"]}</div><div class="status">{chrome["bot_status"]}</div></div>
  </header>"""
    messages = f"""    <div class="row in">
      <div class="bubble clear"><div class="tg" id="text"></div><span class="meta">{views["time"]}</span></div>
      <div class="keyboard">
{keyboard}
      </div>
    </div>"""
    return PAGE.format(
        what="owner panel, 30 days",
        lang=language,
        width=VIEWPORT_WIDTH,
        accent="#3a95e8",
        bubble="rgba(58, 149, 232, 0.08)",
        meta="#9aa3ad",
        header=header,
        today=chrome["today"],
        messages=messages,
        composer=chrome["composer"],
        message=views["panel"],
    )


def _card_page(language: str, views: dict[str, object]) -> str:
    chrome = CHROME[language]
    asked = (datetime.strptime(views["time"], "%H:%M") - timedelta(minutes=2)).strftime("%H:%M")
    header = f"""  <header class="header">
    <svg class="back" viewBox="0 0 22 22"><path d="M14 4 L7 11 L14 18"/></svg>
    <div class="avatar alex">{chrome["initials"]}</div>
    <div class="who"><div class="name">{chrome["contact"]}</div><div class="status">{chrome["contact_status"]}</div></div>
  </header>
  <div class="botbar">
    <div class="avatar bot">H</div><span class="name">{chrome["bot"]}</span>
    <span class="status">{chrome["manages"]}</span><span class="pause">{chrome["pause"]}</span>
  </div>"""
    messages = f"""    <div class="row in">
      <div class="bubble clear">{html.escape(chrome["question"])}<span class="meta">{asked}</span></div>
    </div>
    <div class="row out">
      <div class="bubble clear"><div class="tg" id="text"></div><span class="meta">{chrome["edited"]} {views["time"]} {CHECKS}</span></div>
    </div>"""
    return PAGE.format(
        what="in-chat /stats card, all time",
        lang=language,
        width=VIEWPORT_WIDTH,
        accent="#4aa83f",
        bubble="rgba(84, 176, 70, 0.10)",
        meta="#5aae4f",
        header=header,
        today=chrome["today"],
        messages=messages,
        composer=chrome["composer"],
        message=views["card"],
    )


# --- PNG rendering ---------------------------------------------------------------------------------


def _browser(*argv: str) -> str:
    env = dict(os.environ, AGENT_BROWSER_ENGINE="chrome")
    command = ["agent-browser", "--engine", "chrome", "--session", BROWSER_SESSION, *argv]
    return subprocess.run(command, env=env, check=True, capture_output=True, text=True).stdout.strip()


def render_png(page: Path) -> list[Path]:
    _browser("open", page.resolve().as_uri())
    _browser("set", "viewport", str(VIEWPORT_WIDTH), "400", "1")
    _browser("wait", "300")
    # The body's own height: the wallpaper is absolute, so it never stretches the page.
    measured = json.loads(_browser("--json", "eval", "Math.ceil(document.body.getBoundingClientRect().height)"))
    height = int(measured["data"]["result"] if isinstance(measured, dict) else measured)
    written = []
    for scale, suffix in ((1, ""), (2, "@2x")):
        _browser("set", "viewport", str(VIEWPORT_WIDTH), str(height), str(scale))
        _browser("wait", "300")
        target = PNG_DIR / f"{page.stem}{suffix}.png"
        _browser("screenshot", str(target))
        written.append(target)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--png", action="store_true", help="also render PNGs with agent-browser")
    parser.add_argument("--print", action="store_true", help="print the views' Telegram HTML")
    args = parser.parse_args()

    pages = []
    for language in LANGUAGES:
        views = render_views(language)
        if args.print:
            print(f"--- {language} panel\n{views['panel']}\n{views['buttons']}\n--- {language} card\n{views['card']}")
        for name, build in (("panel", _panel_page), ("card", _card_page)):
            page = SRC_DIR / f"stats-{name}-{language}.html"
            page.write_text(build(language, views), encoding="utf-8")
            pages.append(page)
    if args.png:
        try:
            for page in pages:
                for png in render_png(page):
                    print(png.relative_to(ROOT))
        finally:
            _browser("close")
    else:
        for page in pages:
            print(page.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
