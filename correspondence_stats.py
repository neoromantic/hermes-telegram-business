"""Deterministic correspondence stats over retained Telegram Business history.

The report is computed live from canonical history JSONL under the same chat read
locks as the other ``history`` read commands. It is strictly read-only and stdlib-only.
Message text is read only to measure its length; it is never kept or printed.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Attribute access only (``_history._x(...)``): a from-import would create an import
# cycle and bypass monkeypatches of history_support helpers.
from . import history_support as _history

CLI_ACTION = "correspondence_stats"
REPORT_SCHEMA_VERSION = 2
DEFAULT_OVERVIEW_LIMIT = 20
DEFAULT_SESSION_GAP = "6h"
READ_SLACK = timedelta(days=2)
_DURATION_RE = re.compile(r"(\d+)([smhdw])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
CLASSIFICATIONS = frozenset({"unexplained", "likely_correction", "likely_duplicate", "unclassifiable"})
REPLACED = frozenset({"likely_duplicate", "likely_correction"})
STATUS_PRECEDENCE = ("likely_duplicate", "likely_correction", "unexplained", "unclassifiable", "pending")
SIDE = {"inbound": "received", "outbound": "sent"}
_SIDE_LABEL = {"received": "received", "sent": "sent", "unknown": "unknown direction"}
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Activity buckets, finest first: the first one that fits the window in MAX_ACTIVITY_BUCKETS wins.
ACTIVITY_BUCKETS = ("day", "week", "month", "quarter", "year")
MAX_ACTIVITY_BUCKETS = 31
_ONE_DAY = timedelta(days=1)
_MICROSECOND = timedelta(microseconds=1)
# Bidi controls and line/paragraph separators can reorder or split terminal output
# even after C0/C1 escaping, so untrusted names render them as \uXXXX literals.
_UNSAFE_CODEPOINTS = frozenset({0x061C, 0x200E, 0x200F, 0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A)})
_OVERVIEW_COLUMNS = (
    "latest",
    "last",
    "msgs",
    "recv",
    "sent",
    "unk",
    "days",
    "sess",
    "del",
    "unexpl",
    "partial",
    "connection",
    "chat",
    "contact",
)
_LEFT_ALIGNED_COLUMNS = frozenset({"latest", "last", "partial", "connection", "chat"})


@dataclass(frozen=True, slots=True)
class StatsWindow:
    since: datetime | None
    until: datetime | None
    tz: tzinfo
    tz_label: str
    session_gap: timedelta
    now: datetime

    def contains(self, at: datetime) -> bool:
        return (self.since is None or at >= self.since) and (self.until is None or at <= self.until)

    @property
    def read_since(self) -> datetime | None:
        # Partitions are keyed by the UTC month of observed_at. Reading whole months
        # from since - slack keeps every record of an in-range message (edits,
        # tombstones, and classifications are observed after the message) while
        # tolerating small capture clock skew.
        if self.since is None:
            return None
        try:
            floor = (self.since - READ_SLACK).astimezone(UTC)
        except OverflowError:
            return None
        return floor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(slots=True)
class _Message:
    message_id: Any
    first_observed_at: datetime
    created: bool = False
    message_at: datetime | None = None
    side: str = "unknown"
    edited: bool = False
    # Only the length of the latest text is kept, never the text itself.
    text_length: int | None = None

    @property
    def at(self) -> datetime:
        return self.message_at or self.first_observed_at

    @property
    def sort_key(self) -> tuple[datetime, tuple[int, Any]]:
        return (self.at, _message_id_sort_key(self.message_id))


@dataclass(frozen=True, slots=True)
class _Tombstone:
    message_key: str
    observed_at: datetime
    ordinal: int
    side: str
    message_at_missing: bool


@dataclass(slots=True)
class ChatScan:
    business_connection_id: str
    chat_key: str
    messages: dict[str, _Message] = field(default_factory=dict)
    tombstones: dict[str, _Tombstone] = field(default_factory=dict)
    classifications: dict[str, str] = field(default_factory=dict)

    def consume(self, record: dict[str, Any], ordinal: int) -> None:
        # ``ordinal`` is canonical file order (monthly partitions, then lines), i.e. append
        # order. "Latest" follows it, as history_support's own state rebuild does, so a
        # capture clock that stepped backwards cannot reorder events.
        # Defensive: a chat directory only ever holds its own chat's records.
        if (
            str(record.get("business_connection_id")) != self.business_connection_id
            or str(record.get("chat_id")) != self.chat_key
        ):
            return
        observed = _history._parse_datetime(record.get("observed_at"))
        message_id = record.get("message_id")
        if observed is None or message_id is None:
            return
        key = str(message_id)
        side = SIDE.get(str(record.get("direction")), "unknown")
        event_type = str(record.get("event_type") or "")
        if event_type in {"message.created", "message.edited"}:
            message = self.messages.get(key)
            if message is None:
                message = self.messages[key] = _Message(message_id=message_id, first_observed_at=observed)
            message.created = message.created or event_type == "message.created"
            message.first_observed_at = min(message.first_observed_at, observed)
            message_at = _history._parse_datetime(record.get("message_at"))
            if message_at is not None and (message.message_at is None or message_at < message.message_at):
                message.message_at = message_at
            # The latest known direction wins; an unknown edit never erases it.
            if side != "unknown":
                message.side = side
            message.edited = message.edited or event_type == "message.edited"
            text = record.get("text")
            if isinstance(text, str):
                # Like the direction, the latest text in file order wins.
                message.text_length = len(text.strip())
            return
        if event_type == "message.deleted":
            # A repeated event_id is the same event, so the first copy wins.
            event_id = str(record.get("event_id") or "") or f"line:{ordinal}"
            missing_message_at = _history._parse_datetime(record.get("message_at")) is None
            self.tombstones.setdefault(event_id, _Tombstone(key, observed, ordinal, side, missing_message_at))
            return
        if event_type == "deletion.classified":
            classification = record.get("classification")
            deleted_event_id = record.get("deleted_event_id")
            if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
                return
            if not isinstance(deleted_event_id, str) or not deleted_event_id:
                return
            # The latest classification in file order wins.
            self.classifications[deleted_event_id] = classification


@dataclass(frozen=True, slots=True)
class SideCounts:
    received: int = 0
    sent: int = 0
    unknown: int = 0

    @property
    def total(self) -> int:
        return self.received + self.sent + self.unknown


@dataclass(frozen=True, slots=True)
class ActiveDays:
    total: int = 0
    received: int = 0
    sent: int = 0


@dataclass(frozen=True, slots=True)
class Latest:
    at: datetime | None = None
    side: str | None = None
    received_at: datetime | None = None
    sent_at: datetime | None = None
    observed_reply: str | None = None


@dataclass(frozen=True, slots=True)
class Sessions:
    total: int = 0
    started_by_contact: int = 0
    started_by_owner: int = 0
    started_by_unknown: int = 0
    ended_on_received: int = 0


@dataclass(frozen=True, slots=True)
class Deletions:
    total: int = 0
    unexplained: SideCounts = field(default_factory=SideCounts)
    likely_correction: int = 0
    likely_duplicate: int = 0
    unclassifiable: int = 0
    pending: int = 0
    latest_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MissingHistory:
    retained_from: datetime | None = None
    range_partial: bool = False
    deletions_not_in_history: int = 0
    edit_only_messages: int = 0


@dataclass(frozen=True, slots=True)
class Share:
    # Percent of known-side messages; None without any.
    received: int | None = None
    sent: int | None = None


@dataclass(frozen=True, slots=True)
class Turns:
    received: int = 0
    sent: int = 0
    # Mean messages per turn, one decimal; None without turns on that side.
    per_turn_received: float | None = None
    per_turn_sent: float | None = None

    @property
    def total(self) -> int:
        return self.received + self.sent


@dataclass(frozen=True, slots=True)
class ReplyTimes:
    count: int = 0
    median_seconds: int | None = None
    p90_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class Replies:
    # received: replies by the contact; sent: replies by the owner.
    received: ReplyTimes = field(default_factory=ReplyTimes)
    sent: ReplyTimes = field(default_factory=ReplyTimes)


@dataclass(frozen=True, slots=True)
class Waiting:
    # received: the contact's turn has no reply yet; sent: the owner's turn has none.
    side: str
    since: datetime
    seconds: int


@dataclass(frozen=True, slots=True)
class TextLength:
    # Median characters per message.
    received: int | None = None
    sent: int | None = None


@dataclass(frozen=True, slots=True)
class Activity:
    bucket: str
    start: date  # start of the first bucket
    counts: tuple[int, ...]
    # The window's first and last local dates, which the buckets cover.
    first_day: date
    last_day: date


@dataclass(frozen=True, slots=True)
class Streaks:
    current_days: int = 0
    longest_days: int = 0
    longest_start: date | None = None
    longest_end: date | None = None


@dataclass(frozen=True, slots=True)
class Silence:
    seconds: int
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class PreviousPeriod:
    since: datetime
    until: datetime
    messages: SideCounts
    active_days: ActiveDays
    replies: Replies


@dataclass(frozen=True, slots=True)
class ContactStats:
    business_connection_id: str
    chat_id: Any
    chat_type: str | None
    name: str | None
    username: str | None
    display_name: str
    messages: SideCounts
    active_days: ActiveDays
    latest: Latest
    sessions: Sessions
    deletions: Deletions
    missing: MissingHistory
    share: Share = field(default_factory=Share)
    turns: Turns = field(default_factory=Turns)
    replies: Replies = field(default_factory=Replies)
    waiting: Waiting | None = None
    text_length: TextLength = field(default_factory=TextLength)
    edited: SideCounts = field(default_factory=SideCounts)
    hours: tuple[int, ...] = (0,) * 24  # local hour 0..23
    weekdays: tuple[int, ...] = (0,) * 7  # Monday..Sunday
    activity: Activity | None = None
    streaks: Streaks = field(default_factory=Streaks)
    silence: Silence | None = None
    previous: PreviousPeriod | None = None

    @property
    def has_activity(self) -> bool:
        return self.messages.total > 0 or self.deletions.total > 0

    @property
    def peak_hour(self) -> int | None:
        return _peak(self.hours)

    @property
    def peak_weekday(self) -> int | None:
        return _peak(self.weekdays)


@dataclass(frozen=True, slots=True)
class Overview:
    rows: tuple[ContactStats, ...]
    # Contacts with no counted message or deletion in range. They are not rows,
    # but their missing history is still reported: their activity may be unknown, not zero.
    idle: tuple[ContactStats, ...]
    limit: int

    @property
    def in_scope(self) -> int:
        return len(self.rows) + len(self.idle)

    @property
    def shown(self) -> tuple[ContactStats, ...]:
        return self.rows[: self.limit]

    @property
    def over_limit(self) -> int:
        return max(0, len(self.rows) - self.limit)

    @property
    def no_activity(self) -> int:
        return len(self.idle)

    @property
    def no_activity_partial(self) -> int:
        return sum(1 for stats in self.idle if stats.missing.range_partial)

    @property
    def no_activity_deletions_not_in_history(self) -> int:
        return sum(stats.missing.deletions_not_in_history for stats in self.idle)

    @property
    def no_activity_edit_only_messages(self) -> int:
        return sum(stats.missing.edit_only_messages for stats in self.idle)


def _message_id_sort_key(message_id: Any) -> tuple[int, Any]:
    if isinstance(message_id, int) and not isinstance(message_id, bool):
        return (0, message_id)
    if isinstance(message_id, str) and message_id.isascii() and message_id.isdigit():
        return (0, int(message_id))
    return (1, str(message_id))


def _hermes_timezone_name() -> str:
    # Hermes resolves HERMES_TIMEZONE, then config.yaml ``timezone``. Outside Hermes
    # (or on any failure there) there is simply no Hermes time zone.
    try:
        import hermes_time  # type: ignore[import-not-found]

        name = hermes_time.get_timezone_name()
    except Exception:  # noqa: BLE001 - an optional host default must never break stats
        return ""
    return name.strip() if isinstance(name, str) else ""


def resolve_timezone(value: str | None = None) -> tuple[tzinfo, str]:
    """--tz, else Hermes's time zone, else UTC; every stats view resolves its zone here.

    Telesec has no time zone setting of its own.
    """
    if value is None:
        # A bad Hermes value is the host's problem: fall back to UTC instead of failing.
        hermes = _hermes_timezone_name()
        try:
            zone = UTC if hermes in ("", "UTC") else ZoneInfo(hermes)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            zone = UTC
        return zone, getattr(zone, "key", "UTC")
    name = value.strip()
    if name == "UTC":
        return UTC, "UTC"
    try:
        if not name:
            raise ValueError(name)
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"unknown timezone {name!r} in --tz; use an IANA name such as Europe/Moscow") from exc
    return zone, zone.key


def _local_day_start(day: date, tz: tzinfo) -> datetime:
    # fold=0: a local midnight inside a DST gap maps to the first instant of the day.
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def _local_day_end(day: date, tz: tzinfo) -> datetime:
    return _local_day_start(day + timedelta(days=1), tz) - timedelta(microseconds=1)


def _is_local_day_start(at: datetime, tz: tzinfo) -> bool:
    try:
        return _local_day_start(at.astimezone(tz).date(), tz) == at
    except OverflowError:
        return False


def parse_bound(
    raw: str | None,
    *,
    option_name: str,
    tz: tzinfo,
    now: datetime,
    end_of_day: bool,
) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    try:
        if match := _DURATION_RE.fullmatch(value):
            # Relative to the single captured now, like the other history commands.
            bound = (now - timedelta(seconds=int(match[1]) * _UNIT_SECONDS[match[2]])).astimezone(UTC)
        else:
            try:
                day = date.fromisoformat(value)
            except ValueError:
                day = None
            if day is not None:
                bound = _local_day_end(day, tz) if end_of_day else _local_day_start(day, tz)
            else:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=tz)
                bound = parsed.astimezone(UTC)
        bound.astimezone(tz)  # the report must be able to print the bound in --tz
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"unsupported {option_name} value: {raw}") from exc
    return bound


def parse_duration(raw: str | None, *, option_name: str) -> timedelta:
    match = _DURATION_RE.fullmatch((raw or "").strip())
    error = f"unsupported {option_name} value: {raw}; use a positive duration such as 90m, 6h, or 1d"
    if not match or int(match[1]) == 0:
        raise ValueError(error)
    try:
        return timedelta(seconds=int(match[1]) * _UNIT_SECONDS[match[2]])
    except OverflowError as exc:
        raise ValueError(error) from exc


def build_window(args: argparse.Namespace, *, now: datetime) -> StatsWindow:
    tz, tz_label = resolve_timezone(getattr(args, "tz", None))
    since = parse_bound(getattr(args, "since", None), option_name="--since", tz=tz, now=now, end_of_day=False)
    until = parse_bound(getattr(args, "until", None), option_name="--until", tz=tz, now=now, end_of_day=True)
    if since is not None and until is not None and until < since:
        raise ValueError("--until must be greater than or equal to --since")
    raw_gap = getattr(args, "session_gap", None)
    session_gap = parse_duration(DEFAULT_SESSION_GAP if raw_gap is None else raw_gap, option_name="--session-gap")
    return StatsWindow(since=since, until=until, tz=tz, tz_label=tz_label, session_gap=session_gap, now=now)


def previous_window(window: StatsWindow, retained_from: datetime | None) -> StatsWindow | None:
    """The window of the same length that ends right before ``window.since``.

    A range of whole local days (it starts at a local midnight and runs to now or
    to the end of a local day, such as a date --since or a panel period) compares
    with as many whole local days, so 13..19 October (today) compares with 6..12.

    None without --since, or when retained history does not cover all of it: a
    comparison with a partly missing period would be wrong, not just rough.
    """
    if window.since is None or retained_from is None:
        return None
    tz = window.tz
    try:
        end = window.now if window.until is None else window.until + _MICROSECOND
        span = end - window.since
        if span <= timedelta(0):
            return None
        if _is_local_day_start(window.since, tz) and (window.until is None or _is_local_day_start(end, tz)):
            first = window.since.astimezone(tz).date()
            days = (_window_last_day(window) - first).days + 1
            since = _local_day_start(first - timedelta(days=days), tz)
        else:
            since = window.since - span
        since.astimezone(tz)
    except OverflowError:
        return None
    if retained_from > since:
        return None
    return StatsWindow(
        since=since,
        until=window.since - _MICROSECOND,
        tz=window.tz,
        tz_label=window.tz_label,
        session_gap=window.session_gap,
        now=window.now,
    )


def _scan(entry: dict[str, Any], records: Iterable[dict[str, Any]]) -> ChatScan:
    scan = ChatScan(str(entry.get("business_connection_id")), str(entry.get("chat_id")))
    for ordinal, record in enumerate(records):
        scan.consume(record, ordinal)
    return scan


def compute_stats(entry: dict[str, Any], records: Iterable[dict[str, Any]], window: StatsWindow) -> ContactStats:
    return summarize_scan(entry, _scan(entry, records), window)


def collect_contact_stats(entry: dict[str, Any], window: StatsWindow) -> ContactStats:
    # One read covers the previous period too, so the comparison needs no second pass.
    previous = previous_window(window, _history._parse_datetime(entry.get("first_seen_at")))
    # The stream is consumed fully inside the chat read lock so torn tails and
    # invalid JSON fail closed; the summary is computed after the lock is released.
    with _history._streamed_records_locked(
        _history._entry_chat_dir(entry),
        since=(window if previous is None else previous).read_since,
        until=None,
    ) as records:
        scan = _scan(entry, records)
    return summarize_scan(entry, scan, window)


def collect_overview(entries: Iterable[dict[str, Any]], window: StatsWindow, *, limit: int) -> Overview:
    active: list[ContactStats] = []
    idle: list[ContactStats] = []
    for entry in entries:
        stats = collect_contact_stats(entry, window)
        (active if stats.has_activity else idle).append(stats)
    # Stable sorts: (connection, chat) breaks ties, newest activity first, and rows
    # with counted messages before deletion-only rows.
    active.sort(key=lambda stats: (stats.business_connection_id, str(stats.chat_id)))
    active.sort(key=_activity_at, reverse=True)
    active.sort(key=lambda stats: stats.messages.total == 0)
    return Overview(rows=tuple(active), idle=tuple(idle), limit=limit)


def _activity_at(stats: ContactStats) -> datetime:
    at = stats.latest.at if stats.messages.total else stats.deletions.latest_at
    return at or datetime.min.replace(tzinfo=UTC)


def _sessions(counted: list[_Message], gap: timedelta) -> Sessions:
    total = contact = owner = unknown = ended = 0
    previous_at: datetime | None = None
    last_side: str | None = None
    for message in counted:
        # A gap exactly equal to the threshold stays in the same session.
        if previous_at is None or message.at - previous_at > gap:
            if last_side == "received":
                ended += 1
            total += 1
            if message.side == "received":
                contact += 1
            elif message.side == "sent":
                owner += 1
            else:
                unknown += 1
        previous_at, last_side = message.at, message.side
    if last_side == "received":
        ended += 1
    return Sessions(
        total=total,
        started_by_contact=contact,
        started_by_owner=owner,
        started_by_unknown=unknown,
        ended_on_received=ended,
    )


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _peak(counts: tuple[int, ...]) -> int | None:
    # max() keeps the first maximum, so ties go to the earliest index.
    return max(range(len(counts)), key=counts.__getitem__) if any(counts) else None


def change_percent(current: int, previous: int) -> int | None:
    """Signed change from ``previous`` to ``current`` in whole percent; None when previous is 0."""
    return None if previous == 0 else _round_half_up((current - previous) * 100 / previous)


@dataclass(slots=True)
class _Turn:
    side: str
    first: _Message
    last: _Message
    # True when the turn directly follows a turn of the other side, with no
    # unknown-direction message between them.
    follows: bool = False


def _turns(counted: list[_Message]) -> list[_Turn]:
    turns: list[_Turn] = []
    current: _Turn | None = None
    for message in counted:
        if message.side == "unknown":
            # An unknown-direction message ends the turn and starts none.
            current = None
        elif current is not None and current.side == message.side:
            current.last = message
        else:
            current = _Turn(message.side, message, message, follows=current is not None)
            turns.append(current)
    return turns


def _per_turn(messages: int, turns: int) -> float | None:
    # Tenths rounded half up in integers, so 2.25 is 2.3 on every platform.
    return None if not turns else (20 * messages + turns) // (2 * turns) / 10


def _reply_times(delays: list[float]) -> ReplyTimes:
    if not delays:
        return ReplyTimes()
    ordered = sorted(delays)
    rank = (9 * len(ordered) + 9) // 10  # nearest rank: ceil(0.9 * n), in integers
    return ReplyTimes(
        count=len(ordered),
        median_seconds=_round_half_up(statistics.median(ordered)),
        p90_seconds=_round_half_up(ordered[rank - 1]),
    )


def _replies(turns: list[_Turn], gap: timedelta) -> Replies:
    delays: dict[str, list[float]] = {"received": [], "sent": []}
    for previous, turn in pairwise(turns):
        # A longer silence than the session gap starts a new conversation, not a reply.
        if turn.follows and (delay := turn.first.at - previous.last.at) <= gap:
            delays[turn.side].append(delay.total_seconds())
    return Replies(received=_reply_times(delays["received"]), sent=_reply_times(delays["sent"]))


def _median_length(messages: list[_Message]) -> int | None:
    lengths = [message.text_length for message in messages if message.text_length is not None]
    return _round_half_up(statistics.median(lengths)) if lengths else None


def _bucket_index(day: date, bucket: str) -> int:
    if bucket == "day":
        return day.toordinal()
    if bucket == "week":
        return (day.toordinal() - 1) // 7  # 0001-01-01 is a Monday
    if bucket == "month":
        return day.year * 12 + day.month - 1
    if bucket == "quarter":
        return day.year * 4 + (day.month - 1) // 3
    return day.year


def _bucket_start(index: int, bucket: str) -> date:
    if bucket == "day":
        return date.fromordinal(index)
    if bucket == "week":
        return date.fromordinal(index * 7 + 1)
    if bucket == "month":
        return date(index // 12, index % 12 + 1, 1)
    if bucket == "quarter":
        return date(index // 4, index % 4 * 3 + 1, 1)
    return date(index, 1, 1)


def _activity(days: list[date], first_day: date | None, last_day: date) -> Activity | None:
    if first_day is None:
        return None
    if days:
        # Defensive: a send time after now (capture clock skew) still lands in a bucket.
        first_day, last_day = min(first_day, days[0]), max(last_day, days[-1])
    last_day = max(first_day, last_day)
    for bucket in ACTIVITY_BUCKETS:
        start, end = _bucket_index(first_day, bucket), _bucket_index(last_day, bucket)
        if end - start < MAX_ACTIVITY_BUCKETS:
            break
    counts = [0] * (end - start + 1)
    for day in days:
        counts[_bucket_index(day, bucket) - start] += 1
    return Activity(
        bucket=bucket,
        start=_bucket_start(start, bucket),
        counts=tuple(counts),
        first_day=first_day,
        last_day=last_day,
    )


def _streaks(days: list[date], last_day: date) -> Streaks:
    if not days:
        return Streaks()
    ordered = sorted(set(days))
    run_start = best_start = best_end = ordered[0]
    for previous, day in pairwise(ordered):
        if day - previous != _ONE_DAY:
            run_start = day
        # Strictly longer only, so ties keep the earliest run.
        if day - run_start > best_end - best_start:
            best_start, best_end = run_start, day
    active = set(ordered)
    # The current streak is still alive when it ends on the last day or the day before.
    day: date | None = last_day if last_day in active or last_day == date.min else last_day - _ONE_DAY
    current = 0
    while day is not None and day in active:
        current += 1
        day = None if day == date.min else day - _ONE_DAY
    return Streaks(
        current_days=current,
        longest_days=(best_end - best_start).days + 1,
        longest_start=best_start,
        longest_end=best_end,
    )


def _silence(counted: list[_Message]) -> Silence | None:
    best: tuple[timedelta, _Message, _Message] | None = None
    for previous, message in pairwise(counted):
        gap = message.at - previous.at
        if best is None or gap > best[0]:  # ties keep the earliest gap
            best = (gap, previous, message)
    if best is None:
        return None
    return Silence(seconds=best[0] // timedelta(seconds=1), start=best[1].at, end=best[2].at)


def _waiting(turns: list[_Turn], window: StatsWindow) -> Waiting | None:
    # Only a window that runs to now can say that a turn is still unanswered.
    # Unknown-direction messages after the last known turn do not end the wait.
    if window.until is not None or not turns:
        return None
    since = turns[-1].first.at
    return Waiting(side=turns[-1].side, since=since, seconds=max(0, (window.now - since) // timedelta(seconds=1)))


def _window_last_day(window: StatsWindow) -> date:
    return (window.now if window.until is None else window.until).astimezone(window.tz).date()


def _active_days(tz: tzinfo, counted: list[_Message], received: list[_Message], sent: list[_Message]) -> ActiveDays:
    return ActiveDays(
        total=len({message.at.astimezone(tz).date() for message in counted}),
        received=len({message.at.astimezone(tz).date() for message in received}),
        sent=len({message.at.astimezone(tz).date() for message in sent}),
    )


def _previous_period(timeline: list[_Message], window: StatsWindow) -> PreviousPeriod:
    assert window.since is not None and window.until is not None
    counted = [message for message in timeline if window.contains(message.at)]
    received = [message for message in counted if message.side == "received"]
    sent = [message for message in counted if message.side == "sent"]
    return PreviousPeriod(
        since=window.since,
        until=window.until,
        messages=SideCounts(received=len(received), sent=len(sent), unknown=len(counted) - len(received) - len(sent)),
        active_days=_active_days(window.tz, counted, received, sent),
        replies=_replies(_turns(counted), window.session_gap),
    )


def _deleted_side(message: _Message | None, tombstones: list[_Tombstone]) -> str:
    side = "unknown" if message is None else message.side
    if side != "unknown":
        return side
    known = [tombstone for tombstone in tombstones if tombstone.side != "unknown"]
    if not known:
        return "unknown"
    return max(known, key=lambda tombstone: tombstone.ordinal).side


def _contact_identity(entry: dict[str, Any]) -> tuple[str | None, str | None, str]:
    profile = entry.get("current_profile")
    if not isinstance(profile, dict):
        profile = {}
    name = _history._clean_optional_text(profile.get("title"))
    if name is None:
        parts = [
            part
            for part in (
                _history._clean_optional_text(profile.get("first_name")),
                _history._clean_optional_text(profile.get("last_name")),
            )
            if part
        ]
        name = " ".join(parts) or None
    username = _history._clean_optional_text(profile.get("username"))
    if username is not None:
        username = username.lstrip("@") or None
    if name and username:
        display_name = f"{name} (@{username})"
    elif name:
        display_name = name
    elif username:
        display_name = f"@{username}"
    else:
        display_name = f"chat:{entry.get('chat_id')}"
    return name, username, display_name


def summarize_scan(entry: dict[str, Any], scan: ChatScan, window: StatsWindow) -> ContactStats:
    """Pure: the stats for ``window`` from an already-scanned chat.

    The previous period is computed from the same scan, so the scan must reach back
    to ``previous_window(window, ...).read_since`` for it to be complete.
    """
    tz = window.tz
    by_key: dict[str, list[tuple[_Tombstone, str]]] = {}
    for event_id, tombstone in scan.tombstones.items():
        classified = scan.classifications.get(event_id)
        status = "pending" if classified is None else classified
        by_key.setdefault(tombstone.message_key, []).append((tombstone, status))
    key_status = {
        key: min((status for _, status in items), key=STATUS_PRECEDENCE.index) for key, items in by_key.items()
    }
    replaced = {key for key, status in key_status.items() if status in REPLACED}
    # The timeline keeps messages sent after --until so a later reply is still observed.
    timeline = sorted(
        (message for key, message in scan.messages.items() if message.created and key not in replaced),
        key=lambda message: message.sort_key,
    )
    counted = [message for message in timeline if window.contains(message.at)]
    received = [message for message in counted if message.side == "received"]
    sent = [message for message in counted if message.side == "sent"]

    if counted:
        last = counted[-1]
        observed_reply = None
        if last.side == "received":
            replied = any(message.side == "sent" and message.sort_key > last.sort_key for message in timeline)
            observed_reply = "after_range" if replied else "none"
        latest = Latest(
            at=last.at,
            side=last.side,
            received_at=received[-1].at if received else None,
            sent_at=sent[-1].at if sent else None,
            observed_reply=observed_reply,
        )
    else:
        latest = Latest()

    status_counts = dict.fromkeys(STATUS_PRECEDENCE, 0)
    unexplained_sides = {"received": 0, "sent": 0, "unknown": 0}
    not_in_history = 0
    latest_deletion: datetime | None = None
    for key, items in by_key.items():
        tombstones = [tombstone for tombstone, _ in items]
        # The deletion time is the first tombstone's observation, in file order.
        deleted_at = min(tombstones, key=lambda tombstone: tombstone.ordinal).observed_at
        if not window.contains(deleted_at):
            continue
        message = scan.messages.get(key)
        if message is None and all(tombstone.message_at_missing for tombstone in tombstones):
            not_in_history += 1
            continue
        status = key_status[key]
        status_counts[status] += 1
        if status == "unexplained":
            unexplained_sides[_deleted_side(message, tombstones)] += 1
        latest_deletion = deleted_at if latest_deletion is None else max(latest_deletion, deleted_at)

    retained_from = _history._parse_datetime(entry.get("first_seen_at"))
    name, username, display_name = _contact_identity(entry)
    previous = previous_window(window, retained_from)
    known = len(received) + len(sent)
    received_share = None if not known else (200 * len(received) + known) // (2 * known)
    turns = _turns(counted)
    turns_received = sum(1 for turn in turns if turn.side == "received")
    local = [message.at.astimezone(tz) for message in counted]
    days = [at.date() for at in local]
    hours, weekdays = [0] * 24, [0] * 7
    for at in local:
        hours[at.hour] += 1
        weekdays[at.weekday()] += 1
    edited = [message for message in counted if message.edited]
    return ContactStats(
        business_connection_id=str(entry.get("business_connection_id")),
        chat_id=entry.get("chat_id"),
        chat_type=entry.get("chat_type"),
        name=name,
        username=username,
        display_name=display_name,
        messages=SideCounts(
            received=len(received),
            sent=len(sent),
            unknown=len(counted) - len(received) - len(sent),
        ),
        active_days=_active_days(tz, counted, received, sent),
        latest=latest,
        sessions=_sessions(counted, window.session_gap),
        deletions=Deletions(
            total=sum(status_counts.values()),
            unexplained=SideCounts(**unexplained_sides),
            likely_correction=status_counts["likely_correction"],
            likely_duplicate=status_counts["likely_duplicate"],
            unclassifiable=status_counts["unclassifiable"],
            pending=status_counts["pending"],
            latest_at=latest_deletion,
        ),
        missing=MissingHistory(
            retained_from=retained_from,
            range_partial=window.since is not None and retained_from is not None and window.since < retained_from,
            deletions_not_in_history=not_in_history,
            edit_only_messages=sum(
                1 for message in scan.messages.values() if not message.created and window.contains(message.at)
            ),
        ),
        share=Share(
            received=received_share,
            sent=None if received_share is None else 100 - received_share,
        ),
        turns=Turns(
            received=turns_received,
            sent=len(turns) - turns_received,
            per_turn_received=_per_turn(len(received), turns_received),
            per_turn_sent=_per_turn(len(sent), len(turns) - turns_received),
        ),
        replies=_replies(turns, window.session_gap),
        waiting=_waiting(turns, window),
        text_length=TextLength(received=_median_length(received), sent=_median_length(sent)),
        edited=SideCounts(
            received=sum(1 for message in edited if message.side == "received"),
            sent=sum(1 for message in edited if message.side == "sent"),
            unknown=sum(1 for message in edited if message.side == "unknown"),
        ),
        hours=tuple(hours),
        weekdays=tuple(weekdays),
        activity=_activity(
            days,
            window.since.astimezone(tz).date() if window.since is not None else (days[0] if days else None),
            _window_last_day(window),
        ),
        streaks=_streaks(days, _window_last_day(window)),
        silence=_silence(counted),
        previous=None if previous is None else _previous_period(timeline, previous),
    )


def _safe(text: str) -> str:
    escaped = _history._escape_terminal_text(text)
    return "".join(f"\\u{ord(char):04x}" if ord(char) in _UNSAFE_CODEPOINTS else char for char in escaped)


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _iso(at: datetime | None, tz: tzinfo) -> str | None:
    return None if at is None else at.astimezone(tz).isoformat()


def _local_text(at: datetime, tz: tzinfo) -> str:
    return at.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _bound_text(at: datetime | None, tz: tzinfo, *, end_of_day: bool, missing: str) -> str:
    if at is None:
        return missing
    boundary: datetime | None = at
    if end_of_day:
        try:
            boundary = at + timedelta(microseconds=1)
        except OverflowError:
            boundary = None
    if boundary is not None and _is_local_day_start(boundary, tz):
        return at.astimezone(tz).date().isoformat()
    return _local_text(at, tz)


def _format_duration(value: timedelta) -> str:
    seconds = value // timedelta(seconds=1)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _format_span(seconds: int | None) -> str:
    # Compact and rounded down: 40s, 12m, 1h40m, 3h05m, 4d02h.
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02d}h"


def _value(value: Any) -> str:
    return "-" if value is None else str(value)


def _cli_option(name: str, value: Any) -> str:
    text = str(value)
    # Connection ids are opaque base64url-like strings and may start with "-";
    # argparse would read "--connection -AbC" as a new option, so attach those with "=".
    separator = "=" if text.startswith("-") else " "
    return f"{name}{separator}{shlex.quote(text)}"


def _details_command(business_connection_id: str | None, chat_id: Any = None) -> str:
    parts = ["hermes telesec history deletions"]
    if business_connection_id is not None:
        parts.append(_cli_option("--connection", business_connection_id))
    if chat_id is not None:
        parts.append(_cli_option("--chat", chat_id))
    parts.append("--status unexplained")
    return " ".join(parts)


def _range_line(window: StatsWindow) -> str:
    since_text = _bound_text(window.since, window.tz, end_of_day=False, missing="start of history")
    until_text = _bound_text(window.until, window.tz, end_of_day=True, missing="now")
    return f"range {since_text}..{until_text} {window.tz_label}, by Telegram send time; text messages only"


def _latest_text(latest: Latest, tz: tzinfo) -> str:
    if latest.at is None or latest.side is None:
        return "latest none"
    parts = [f"latest {_local_text(latest.at, tz)} {_SIDE_LABEL[latest.side]}"]
    if latest.side != "received":
        parts.append(
            "no received message"
            if latest.received_at is None
            else f"last received {_local_text(latest.received_at, tz)}"
        )
    if latest.side != "sent":
        parts.append("no sent message" if latest.sent_at is None else f"last sent {_local_text(latest.sent_at, tz)}")
    return "; ".join(parts)


def _sessions_text(sessions: Sessions, gap: timedelta) -> str:
    head = f"sessions {sessions.total} (gap {_format_duration(gap)})"
    if not sessions.total:
        return head
    starters = f"started by contact {sessions.started_by_contact}, by owner {sessions.started_by_owner}"
    if sessions.started_by_unknown:
        starters += f", unknown {sessions.started_by_unknown}"
    return f"{head}: {starters}; ended on received {sessions.ended_on_received}"


def _split_text(counts: SideCounts) -> str:
    split = f"received {counts.received}, sent {counts.sent}"
    return split + (f", unknown direction {counts.unknown}" if counts.unknown else "")


def _replies_text(replies: Replies) -> str:
    parts = []
    for side, times in (("received", replies.received), ("sent", replies.sent)):
        part = f"{side} {times.count}"
        if times.count:
            part += f": median {_format_span(times.median_seconds)}, p90 {_format_span(times.p90_seconds)}"
        parts.append(part)
    return "replies " + "; ".join(parts)


def _rhythm_text(stats: ContactStats) -> str:
    peak_hour, peak_weekday = stats.peak_hour, stats.peak_weekday
    peak = "none" if peak_hour is None or peak_weekday is None else f"{WEEKDAY_NAMES[peak_weekday]} {peak_hour:02d}:00"
    hours = " ".join(str(count) for count in stats.hours)
    weekdays = " ".join(str(count) for count in stats.weekdays)
    return f"peak {peak}; hours 0..23: {hours}; weekdays Mon..Sun: {weekdays}"


def _streak_text(stats: ContactStats, tz: tzinfo) -> str:
    streaks = stats.streaks
    text = f"streak current {streaks.current_days}d, longest {streaks.longest_days}d"
    if streaks.longest_start is not None and streaks.longest_end is not None:
        text += f" ({streaks.longest_start.isoformat()}..{streaks.longest_end.isoformat()})"
    if stats.silence is not None:
        silence = stats.silence
        span = f"{_local_text(silence.start, tz)}..{_local_text(silence.end, tz)}"
        text += f"; longest silence {_format_span(silence.seconds)} ({span})"
    return text


def _previous_text(stats: ContactStats, tz: tzinfo) -> str:
    previous = stats.previous
    assert previous is not None
    since = _bound_text(previous.since, tz, end_of_day=False, missing="start of history")
    until = _bound_text(previous.until, tz, end_of_day=True, missing="now")
    messages = f"messages {previous.messages.total} -> {stats.messages.total}"
    change = change_percent(stats.messages.total, previous.messages.total)
    if change is not None:
        messages += f" ({change:+d}%)"
    replies = ", ".join(
        f"{side} -"
        if before.median_seconds is None and now.median_seconds is None
        else f"{side} {_format_span(before.median_seconds)} -> {_format_span(now.median_seconds)}"
        for side, before, now in (
            ("received", previous.replies.received, stats.replies.received),
            ("sent", previous.replies.sent, stats.replies.sent),
        )
    )
    days = f"active days {previous.active_days.total} -> {stats.active_days.total}"
    return f"previous {since}..{until}: {messages}; {days}; reply median {replies}"


def _metric_lines(stats: ContactStats, window: StatsWindow) -> list[str]:
    """The conversation metrics; only the previous-period line shows without messages."""
    tz = window.tz
    lines: list[str] = []
    if stats.messages.total:
        share, turns, length = stats.share, stats.turns, stats.text_length
        if share.received is not None:
            lines.append(f"share received {share.received}%, sent {share.sent}%")
        if turns.total:
            lines.append(
                f"turns {turns.total} (received {turns.received}, sent {turns.sent}); "
                + f"messages per turn received {_value(turns.per_turn_received)}, sent {_value(turns.per_turn_sent)}"
            )
            lines.append(_replies_text(stats.replies))
        if stats.waiting is not None:
            waiting = stats.waiting
            lines.append(
                f"waiting {waiting.side} since {_local_text(waiting.since, tz)} ({_format_span(waiting.seconds)})"
            )
        edited = f"edited {stats.edited.total}"
        if stats.edited.total:
            edited += f" ({_split_text(stats.edited)})"
        lines.append(f"text length median received {_value(length.received)}, sent {_value(length.sent)}; {edited}")
        lines.append(_rhythm_text(stats))
        if stats.activity is not None:
            activity = stats.activity
            counts = " ".join(str(count) for count in activity.counts)
            lines.append(f"activity by {activity.bucket} from {activity.start.isoformat()}: {counts}")
        lines.append(_streak_text(stats, tz))
    if stats.previous is not None:
        lines.append(_previous_text(stats, tz))
    return lines


def _deletions_text(deletions: Deletions) -> str:
    parts: list[str] = []
    unexplained = deletions.unexplained
    if unexplained.total:
        parts.append(f"unexplained {unexplained.total} ({_split_text(unexplained)})")
    for label, count in (
        ("likely correction", deletions.likely_correction),
        ("likely duplicate", deletions.likely_duplicate),
        ("unclassifiable", deletions.unclassifiable),
        ("pending", deletions.pending),
    ):
        if count:
            parts.append(f"{label} {count}")
    if not parts:
        return f"deletions {deletions.total}"
    return f"deletions {deletions.total}: {', '.join(parts)}"


def _missing_phrases(deletions_not_in_history: int, edit_only_messages: int) -> list[str]:
    phrases: list[str] = []
    if deletions_not_in_history:
        phrases.append(
            _plural(
                deletions_not_in_history,
                "deletion of a message not in history",
                "deletions of messages not in history",
            )
        )
    if edit_only_messages:
        phrases.append(
            _plural(edit_only_messages, "message known only from an edit", "messages known only from an edit")
        )
    return phrases


def _missing_text(missing: MissingHistory, tz: tzinfo) -> str:
    retained = "unknown" if missing.retained_from is None else missing.retained_from.astimezone(tz).date().isoformat()
    phrases = _missing_phrases(missing.deletions_not_in_history, missing.edit_only_messages)
    if not missing.range_partial and not phrases:
        return f"missing history: none (retained from {retained})"
    text = f"missing history: retained from {retained}"
    if missing.range_partial:
        text += " (range is partial)"
    return text + "".join(f"; {phrase}" for phrase in phrases)


def render_contact_text(stats: ContactStats, window: StatsWindow) -> list[str]:
    tz = window.tz
    messages = stats.messages
    days = stats.active_days
    lines = [
        _range_line(window),
        f"{_safe(stats.display_name)} connection={_safe(stats.business_connection_id)} chat={_safe(str(stats.chat_id))}",
        f"  messages {messages.total}: received {messages.received}, sent {messages.sent}, "
        + f"unknown direction {messages.unknown}",
        f"  active days {days.total} (received {days.received}, sent {days.sent})",
        f"  {_latest_text(stats.latest, tz)}",
    ]
    if stats.latest.side == "received" and stats.latest.at is not None:
        reply = (
            "no observed reply after it" if stats.latest.observed_reply == "none" else "reply observed after the range"
        )
        lines.append(f"  last message: received {_local_text(stats.latest.at, tz)}, {reply}")
    lines.append(f"  {_sessions_text(stats.sessions, window.session_gap)}")
    lines.extend(f"  {line}" for line in _metric_lines(stats, window))
    lines.append(f"  {_deletions_text(stats.deletions)}")
    lines.append(f"  {_missing_text(stats.missing, tz)}")
    if stats.deletions.unexplained.total:
        lines.append(f"  deletion details: {_safe(_details_command(stats.business_connection_id, stats.chat_id))}")
    return lines


def _overview_cells(stats: ContactStats, tz: tzinfo) -> tuple[str, ...]:
    return (
        "-" if stats.latest.at is None else _local_text(stats.latest.at, tz),
        stats.latest.side or "-",
        str(stats.messages.total),
        str(stats.messages.received),
        str(stats.messages.sent),
        str(stats.messages.unknown),
        str(stats.active_days.total),
        str(stats.sessions.total),
        str(stats.deletions.total),
        str(stats.deletions.unexplained.total),
        "yes" if stats.missing.range_partial else "no",
        _safe(stats.business_connection_id),
        _safe(str(stats.chat_id)),
        _safe(stats.display_name),
    )


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    table = [_OVERVIEW_COLUMNS, *rows]
    padded = _OVERVIEW_COLUMNS[:-1]
    widths = [max(len(row[index]) for row in table) for index in range(len(padded))]
    lines = []
    for row in table:
        cells = [
            cell.ljust(width) if column in _LEFT_ALIGNED_COLUMNS else cell.rjust(width)
            for column, cell, width in zip(padded, row, widths, strict=False)
        ]
        # The contact column is last and unpadded, so no line has trailing spaces.
        lines.append("  ".join([*cells, row[-1]]))
    return lines


def render_overview_text(overview: Overview, window: StatsWindow, *, connection: str | None) -> list[str]:
    shown = overview.shown
    lines = [_range_line(window)]
    if shown:
        lines.extend(_table([_overview_cells(stats, window.tz) for stats in shown]))
    else:
        lines.append("No contacts with activity in range.")
    not_shown: list[str] = []
    if overview.over_limit:
        not_shown.append(f"{overview.over_limit} over --limit {overview.limit}")
    if overview.no_activity:
        idle = f"{overview.no_activity} with no activity in range"
        if overview.no_activity_partial:
            # Their activity in range is unknown, not zero, so say so instead of hiding it.
            idle += f" (range is partial for {overview.no_activity_partial}: retained history starts after --since)"
        not_shown.append(idle)
    footer = f"shown {len(shown)} of {overview.in_scope} contacts"
    if not_shown:
        footer += f"; not shown: {', '.join(not_shown)}"
    lines.append(footer)
    for label, phrases in (
        (
            "shown contacts",
            _missing_phrases(
                sum(stats.missing.deletions_not_in_history for stats in shown),
                sum(stats.missing.edit_only_messages for stats in shown),
            ),
        ),
        (
            "contacts with no activity in range",
            _missing_phrases(
                overview.no_activity_deletions_not_in_history,
                overview.no_activity_edit_only_messages,
            ),
        ),
    ):
        if phrases:
            lines.append(f"missing history in {label}: {'; '.join(phrases)}; use --contact for details")
    if any(stats.deletions.unexplained.total for stats in shown):
        lines.append(f"deletion details: {_safe(_details_command(connection))}")
    return lines


def _date(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _reply_document(times: ReplyTimes) -> dict[str, Any]:
    return {"count": times.count, "median_seconds": times.median_seconds, "p90_seconds": times.p90_seconds}


def _previous_document(previous: PreviousPeriod | None, tz: tzinfo) -> dict[str, Any] | None:
    if previous is None:
        return None
    # The same shapes as the contact's own keys, trimmed to what is compared.
    return {
        "since": _iso(previous.since, tz),
        "until": _iso(previous.until, tz),
        "messages": {"total": previous.messages.total},
        "active_days": {"total": previous.active_days.total},
        "replies": {
            "received": {"median_seconds": previous.replies.received.median_seconds},
            "sent": {"median_seconds": previous.replies.sent.median_seconds},
        },
    }


def _contact_document(stats: ContactStats, tz: tzinfo) -> dict[str, Any]:
    unexplained = stats.deletions.unexplained
    waiting, activity, silence = stats.waiting, stats.activity, stats.silence
    return {
        "business_connection_id": stats.business_connection_id,
        "chat_id": stats.chat_id,
        "chat_type": stats.chat_type,
        "display_name": stats.display_name,
        "name": stats.name,
        "username": stats.username,
        "messages": {
            "total": stats.messages.total,
            "received": stats.messages.received,
            "sent": stats.messages.sent,
            "unknown_direction": stats.messages.unknown,
        },
        "active_days": {
            "total": stats.active_days.total,
            "received": stats.active_days.received,
            "sent": stats.active_days.sent,
        },
        "latest": {
            "at": _iso(stats.latest.at, tz),
            "side": stats.latest.side,
            "received_at": _iso(stats.latest.received_at, tz),
            "sent_at": _iso(stats.latest.sent_at, tz),
            "observed_reply": stats.latest.observed_reply,
        },
        "sessions": {
            "total": stats.sessions.total,
            "started_by_contact": stats.sessions.started_by_contact,
            "started_by_owner": stats.sessions.started_by_owner,
            "started_by_unknown": stats.sessions.started_by_unknown,
            "ended_on_received": stats.sessions.ended_on_received,
        },
        "deletions": {
            "total": stats.deletions.total,
            "unexplained": {
                "total": unexplained.total,
                "received": unexplained.received,
                "sent": unexplained.sent,
                "unknown_direction": unexplained.unknown,
            },
            "likely_correction": stats.deletions.likely_correction,
            "likely_duplicate": stats.deletions.likely_duplicate,
            "unclassifiable": stats.deletions.unclassifiable,
            "pending": stats.deletions.pending,
            "details_command": (
                _details_command(stats.business_connection_id, stats.chat_id) if unexplained.total else None
            ),
        },
        "missing_history": {
            "retained_from": _iso(stats.missing.retained_from, tz),
            "range_partial": stats.missing.range_partial,
            "deletions_of_messages_not_in_history": stats.missing.deletions_not_in_history,
            "messages_known_only_from_edit": stats.missing.edit_only_messages,
        },
        "share": {"received": stats.share.received, "sent": stats.share.sent},
        "turns": {
            "total": stats.turns.total,
            "received": stats.turns.received,
            "sent": stats.turns.sent,
            "messages_per_turn": {"received": stats.turns.per_turn_received, "sent": stats.turns.per_turn_sent},
        },
        "replies": {"received": _reply_document(stats.replies.received), "sent": _reply_document(stats.replies.sent)},
        "waiting": None
        if waiting is None
        else {"side": waiting.side, "since": _iso(waiting.since, tz), "seconds": waiting.seconds},
        "text_length_median": {"received": stats.text_length.received, "sent": stats.text_length.sent},
        "edited": {"total": stats.edited.total, "received": stats.edited.received, "sent": stats.edited.sent},
        "hours": list(stats.hours),
        "weekdays": list(stats.weekdays),
        "peak_hour": stats.peak_hour,
        "peak_weekday": stats.peak_weekday,
        "activity": None
        if activity is None
        else {"bucket": activity.bucket, "start": _date(activity.start), "counts": list(activity.counts)},
        "streaks": {
            "current_days": stats.streaks.current_days,
            "longest_days": stats.streaks.longest_days,
            "longest_start": _date(stats.streaks.longest_start),
            "longest_end": _date(stats.streaks.longest_end),
        },
        "silence": None
        if silence is None
        else {"seconds": silence.seconds, "from": _iso(silence.start, tz), "to": _iso(silence.end, tz)},
        "previous": _previous_document(stats.previous, tz),
    }


def report_document(
    *,
    mode: str,
    contacts: list[ContactStats],
    window: StatsWindow,
    overview: Overview | None,
) -> dict[str, Any]:
    tz = window.tz
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report": "correspondence_stats",
        "mode": mode,
        "generated_at": _iso(window.now, tz),
        "range": {
            "since": _iso(window.since, tz),
            "until": _iso(window.until, tz),
            "timezone": window.tz_label,
            "time_basis": "telegram_send_time",
            "deletion_time_basis": "deletion_observed_time",
            "content": "text_messages_only",
            "session_gap_seconds": window.session_gap // timedelta(seconds=1),
        },
        "contacts": [_contact_document(stats, tz) for stats in contacts],
        "overview": None
        if overview is None
        else {
            "contacts_in_scope": overview.in_scope,
            "active": len(overview.rows),
            "shown": len(overview.shown),
            "limit": overview.limit,
            "not_shown_over_limit": overview.over_limit,
            "not_shown_no_activity": overview.no_activity,
            "not_shown_no_activity_partial": overview.no_activity_partial,
            "not_shown_no_activity_missing_history": {
                "deletions_of_messages_not_in_history": overview.no_activity_deletions_not_in_history,
                "messages_known_only_from_edit": overview.no_activity_edit_only_messages,
            },
        },
    }


def _dump(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=True, indent=2)


def add_cli_parser(subs: Any) -> None:
    parser = subs.add_parser(
        "stats",
        help="Show correspondence stats per contact (text messages only)",
        description=(
            "Deterministic report over retained Telegram Business history, by Telegram send time. "
            "Text messages only. Never prints message text and never changes history."
        ),
        epilog=(
            "examples:\n"
            "  hermes telesec stats\n"
            "  hermes telesec stats --contact @alice --since 30d\n"
            "  hermes telesec stats --chat 991 --connection business-123 --since 2026-10-01 "
            "--until 2026-10-18 --tz Europe/Moscow\n"
            "  hermes telesec stats --contact @alice --format json\n"
            "storage stats (records, bytes, settings): hermes telesec history stats"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--contact", help="Contact name, @username, or alias from the history catalog")
    parser.add_argument("--chat", help="Numeric Telegram chat id")
    parser.add_argument(
        "--connection",
        help="Business connection id; filters the overview and disambiguates --chat/--contact",
    )
    parser.add_argument(
        "--since",
        help=(
            "Inclusive start: 7d, 2026-10-01 (start of that day in --tz), or ISO timestamp; "
            "default: all retained history"
        ),
    )
    parser.add_argument(
        "--until",
        help="Inclusive end: 1d, 2026-10-18 (end of that day in --tz), or ISO timestamp; default: now",
    )
    parser.add_argument(
        "--tz",
        help="IANA time zone for dates, days, and hours (default: Hermes's time zone, else UTC)",
    )
    parser.add_argument(
        "--session-gap",
        default=DEFAULT_SESSION_GAP,
        help="Silence that starts a new session: 90m, 6h, 1d (default: 6h)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_OVERVIEW_LIMIT,
        help="Contacts shown in the overview (default: 20)",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.set_defaults(_history_action=CLI_ACTION)


def _run(args: argparse.Namespace) -> list[str]:
    window = build_window(args, now=_history._utcnow())
    as_json = (getattr(args, "format", None) or "text") == "json"
    connection = getattr(args, "connection", None)
    contact = getattr(args, "contact", None)
    chat = None if getattr(args, "chat", None) is None else str(args.chat)
    if contact is not None or chat is not None:
        entry = _history._resolve_history_entry(business_connection_id=connection, chat_id=chat, contact=contact)
        stats = collect_contact_stats(entry, window)
        if as_json:
            return [_dump(report_document(mode="contact", contacts=[stats], window=window, overview=None))]
        return render_contact_text(stats, window)
    entries = _history._filter_catalog_entries(business_connection_id=connection)
    limit = _history._bounded_limit(getattr(args, "limit", None), DEFAULT_OVERVIEW_LIMIT)
    overview = collect_overview(entries, window, limit=limit)
    if as_json:
        return [
            _dump(report_document(mode="overview", contacts=list(overview.shown), window=window, overview=overview))
        ]
    if not entries:
        return ["No Telegram Business history found."]
    return render_overview_text(overview, window, connection=connection)


def handle_cli(args: argparse.Namespace) -> int:
    try:
        # Everything is computed before printing, so a failure never leaves partial output.
        lines = _run(args)
    except (OSError, ValueError, OverflowError) as exc:
        # Resolver errors can quote untrusted contact names.
        print(f"error: {_safe(str(exc))}")
        return 1
    print("\n".join(lines))
    return 0
