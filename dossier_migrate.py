"""One-time move of v1 dossiers (``dossier.json``) to the v2 ``document.json``.

Stdlib, ``dossier_doc`` and ``history_support`` only. ``dossier_support.migrate_v1`` calls
``migrate_root`` under the dossier data lock; nothing here takes a lock or calls a model.

The owner's own words go verbatim into the note: owner notes and corrections, pinned facts as
"Keep: …", and the texts the owner corrected or removed as a "Leave out:" paragraph. Active model
facts become the contact's document at revision 0, readable at once in every chat; the first v2 pass
rewrites it. The v1 file stays as ``v1-dossier.json`` until that pass; a later v1 file in the same
directory (a rollback, or a 0.14 gateway still running) goes to ``v1-dossier.<n>.json``, so no backup
is overwritten. ``document.json`` is written before the v1 file is renamed, so a crash at any point
re-runs cleanly and never repeats a note line. This module goes away in a later release, together
with the backups.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import dossier_doc as _doc
from . import history_support as _history

logger = logging.getLogger(__name__)

V1_SCHEMA_VERSION = 1
CONTACT_SECTIONS = (  # v1 section order; the converted document keeps it
    "relationship",
    "preferences",
    "shared_facts",
    "commitments",
    "decisions",
    "open_questions",
    "contradictions",
)
SECTION_TITLES = {  # the titles v1's panel showed
    "en": {
        "relationship": "Relationship",
        "preferences": "Preferences",
        "shared_facts": "Shared facts",
        "commitments": "Commitments",
        "decisions": "Decisions",
        "open_questions": "Open questions",
        "contradictions": "Contradictions",
        "other": "Other",
    },
    "ru": {
        "relationship": "Отношения",
        "preferences": "Предпочтения",
        "shared_facts": "Общие факты",
        "commitments": "Обязательства",
        "decisions": "Решения",
        "open_questions": "Открытые вопросы",
        "contradictions": "Противоречия",
        "other": "Другое",
    },
}
SEED_KINDS = ("observed", "inferred")  # model-written facts; "owner" facts go to the note
REMOVED_BY = ("correct", "remove")  # texts the owner replaced or removed
LEAVE_OUT_MAX = 20  # newest owner removals carried into the note
KEEP_LABEL = {"en": "Keep:", "ru": "Сохранить:"}
LEAVE_OUT_LABEL = {"en": "Leave out:", "ru": "Не включать:"}

_BACKUP_RE = re.compile(r"v1-dossier(?:\.([0-9]+))?\.json")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_INT_RE = re.compile(r"-?[1-9][0-9]*|0")
_OLDEST = datetime.min.replace(tzinfo=UTC)


def migrate_root(root: Path, now: datetime) -> dict[str, int]:
    """Move every v1 dossier under ``root`` to v2. The caller holds the dossier data lock.

    Visits ``chats/*/*/`` and ``persona/`` when they hold ``dossier.json``. Returns
    ``{"migrated", "unreadable", "backups"}``: files moved, files that could not be read (kept as a
    backup next to a fresh document), and the v1 backups now under ``root``. A directory that fails
    (an I/O error, a corrupt ``document.json`` or anything else) is logged by error type and left as
    it was, so the next call tries it again and its owner text stays in ``dossier.json``.
    """
    counts = {"migrated": 0, "unreadable": 0, "failed": 0}
    for kind, target in _targets(root, _doc.V1_FILE):
        backup = ""
        try:
            outcome, backup = _migrate_dir(kind, target, now)
        except Exception as exc:  # noqa: BLE001 - one bad directory never stops the others or the cycle
            outcome = "failed"
            logger.warning(
                "%s: dossier migration failed chat_ref=%s error=%s",
                _history.PLUGIN_NAME,
                _ref(kind, target),
                type(exc).__name__,
            )
        if outcome == "unreadable":
            logger.warning(
                "%s: old dossier file could not be read chat_ref=%s; it is kept as %s",
                _history.PLUGIN_NAME,
                _ref(kind, target),
                backup,
            )
        counts[outcome] += 1
    report = {"migrated": counts["migrated"], "unreadable": counts["unreadable"]}
    report["backups"] = len(_backup_files(root))
    if any(counts.values()):
        logger.info(
            "%s: dossier migration migrated=%d unreadable=%d failed=%d backups=%d",
            _history.PLUGIN_NAME,
            counts["migrated"],
            counts["unreadable"],
            counts["failed"],
            report["backups"],
        )
    return report


def unreadable_backups(root: Path) -> list[str]:
    """v1 backups (paths relative to ``root``) the migration could not read.

    For the status warning: these backups hold owner text that did not reach the note.
    """
    return [
        path.relative_to(root).as_posix()
        for kind, path in _backup_files(root)
        if _usable_v1(kind, path.parent, path.name) is None
    ]


def drop_migrated_backups(kind: str, target: Path) -> None:
    """Delete the backups in ``target`` whose owner text reached the note, at the first v2 pass.

    An unreadable one stays until a forget. The caller holds the dossier data lock.
    """
    for path in _doc.backups(target):
        if _usable_v1(kind, target, path.name) is not None:
            path.unlink(missing_ok=True)


def _targets(root: Path, name: str) -> list[tuple[str, Path]]:
    """``(kind, directory)`` for every contact directory and the persona directory holding ``name``."""
    found = [("contact", path.parent) for path in sorted(root.glob(f"chats/*/*/{name}"))]
    if (root / "persona" / name).exists():
        found.append(("persona", root / "persona"))
    return found


def _backup_files(root: Path) -> list[tuple[str, Path]]:
    """``(kind, path)`` for every v1 backup under ``root``: contacts first, then the persona."""
    found = [("contact", path) for target in sorted(root.glob("chats/*/*")) for path in _doc.backups(target)]
    return found + [("persona", path) for path in _doc.backups(root / "persona")]


def _ref(kind: str, target: Path) -> str:
    """The log reference of a directory: ``dossier_support._log_ref`` of its chat, never a raw id."""
    if kind == "persona":
        return "persona"
    return hashlib.sha256(f"{target.parent.name}/{target.name}".encode()).hexdigest()[:12]


def _migrate_dir(kind: str, target: Path, now: datetime) -> tuple[str, str]:
    """``(outcome, backup name)`` for one directory holding ``dossier.json``."""
    data = _usable_v1(kind, target, _doc.V1_FILE)
    if data is None:
        fresh = None if (target / _doc.DOC_FILE).exists() else _fresh(kind, target, None, now)
        if fresh is not None:
            _doc.write_doc(target, fresh)
        return "unreadable", _retire_v1(target)
    doc = _doc.read_doc(target)  # a crash re-run or a rollback left one: merge into it
    if doc is not None and doc["kind"] != kind:
        raise _doc.DocError
    if doc is None:
        doc = _fresh(kind, target, data, now)  # never None: _usable_v1 checked the contact ids
        doc["created_at"] = _created_at(data) or doc["created_at"]
    note = _merged_note(doc["note"], data)
    if note != doc["note"]:
        doc["note"] = note
        doc["note_updated_at"] = _history._isoformat_utc(now.replace(microsecond=0))
        doc["note_from"] = "v1"  # so 'note --text' asks for --replace before it drops this text
    if kind == "contact" and doc["window_start"] is None:  # no v2 pass yet: the newest v1 facts are the document
        doc["document"] = _seed(data)
    if kind == "persona":
        doc["chats"] = _merged_chats(doc["chats"], data)
    doc["migrated_from"] = "v1"  # the next v2 pass deletes the backup
    _doc.write_doc(target, doc)
    return "migrated", _retire_v1(target)


def _retire_v1(target: Path) -> str:
    """Delete ``changes.jsonl``, then keep ``dossier.json`` as a backup; returns the backup's name.

    The log goes first because a directory without ``dossier.json`` is never visited again. A backup
    already there is never overwritten (it may be the only copy of owner text that never reached the
    note): the newer file gets the number after the highest one kept, so the highest number is always
    the newest file, also once a lower one is deleted. The caller holds the data lock.
    """
    (target / _doc.V1_CHANGES).unlink(missing_ok=True)
    numbers = [int(m[1] or 0) for path in _doc.backups(target) if (m := _BACKUP_RE.fullmatch(path.name))]
    n = max(numbers, default=-1) + 1
    name = _doc.V1_BACKUP if n == 0 else f"v1-dossier.{n}.json"
    os.replace(target / _doc.V1_FILE, target / name)
    _history._fsync_parent_directory(target / name)
    return name


def _usable_v1(kind: str, target: Path, name: str) -> dict[str, Any] | None:
    """The v1 dossier ``target/name`` when it can be migrated, else None.

    It must be a v1 file of ``kind`` with a facts list, and a contact must have ids that belong to
    its directory (the file's own, or ones read back from the directory names).
    """
    try:
        data = json.loads((target / name).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):  # bad JSON, bad UTF-8 or JSON nested too deep
        return None
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != V1_SCHEMA_VERSION
        or data.get("kind") != kind
        or not isinstance(data.get("facts"), list)
    ):
        return None
    if kind == "contact" and _contact_ids(target, data) is None:
        return None
    return data


def _contact_ids(target: Path, data: dict[str, Any] | None) -> tuple[str, Any] | None:
    """``(business_connection_id, chat_id)`` of a contact directory, or None when they are unknown.

    The v1 file's ids win when they map to this directory. Without them the directory names are
    read back: ``<_path_key(connection)>/<_safe_part(chat)>`` keeps the ids unless ``_safe_part``
    had to replace characters, and the hash in ``_path_key`` tells when it did.
    """
    key = f"{target.parent.name}/{target.name}"
    chat: Any = int(target.name) if _INT_RE.fullmatch(target.name) else target.name  # Telegram ids are numbers
    candidates = [(target.parent.name.rpartition("--")[0], chat)]
    if data is not None:
        candidates.insert(0, (data.get("business_connection_id"), data.get("chat_id")))
    for connection, chat_id in candidates:
        if (
            _id(connection)
            and _id(chat_id)
            and f"{_history._path_key(connection)}/{_history._safe_part(chat_id)}" == key
        ):
            return str(connection), chat_id
    return None


def _id(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool) and value != ""


def _fresh(kind: str, target: Path, data: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """An empty document for ``target``, or None for a contact whose ids are unknown."""
    if kind == "persona":
        return _doc.new_persona(now)
    ids = _contact_ids(target, data)
    return None if ids is None else _doc.new_contact(ids[0], ids[1], now)


def _when(value: Any) -> datetime | None:
    """A v1 timestamp as an aware datetime, or None when it is missing or out of range."""
    try:
        return _history._parse_datetime(value)
    except OverflowError:  # e.g. year 1 with a positive offset
        return None


def _created_at(data: dict[str, Any]) -> str | None:
    created = _when(data.get("created_at"))
    return None if created is None else _history._isoformat_utc(created.replace(microsecond=0))


def _facts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Active v1 facts in file order."""
    return [fact for fact in data["facts"] if isinstance(fact, dict) and fact.get("status") == "active"]


def _text(value: Any) -> str:
    """A stored v1 text as it was written: controls removed and trimmed, never clipped."""
    return _doc.clean_note(value) if isinstance(value, str) else ""


def _owner_text(data: dict[str, Any]) -> tuple[list[tuple[str, bool]], list[str]]:
    """The owner's own words in a v1 dossier: ``(items, leave_out)``.

    ``items`` are ``(text, pinned)`` in fact order: owner notes and corrections as written, and
    pinned model facts. ``leave_out`` holds the newest ``LEAVE_OUT_MAX`` distinct texts the owner
    corrected or removed, oldest first.
    """
    items: list[tuple[str, bool]] = []
    for fact in _facts(data):
        text = _text(fact.get("text"))
        if text and fact.get("kind") == "owner":
            items.append((text, False))
        elif text and fact.get("pinned") is True:
            items.append((text, True))
    removed = data.get("removed")
    entries = [
        entry
        for entry in (removed if isinstance(removed, list) else [])
        if isinstance(entry, dict) and entry.get("by") in REMOVED_BY and _text(entry.get("text"))
    ]
    entries.sort(key=lambda entry: _when(entry.get("at")) or _OLDEST)
    leave_out: list[str] = []
    for entry in reversed(entries):  # newest first, so a repeated text keeps its newest place
        text = _text(entry["text"])
        if text not in leave_out:
            leave_out.append(text)
        if len(leave_out) == LEAVE_OUT_MAX:
            break
    leave_out.reverse()
    return items, leave_out


def _merged_note(existing: str, data: dict[str, Any]) -> str:
    """``existing`` plus the v1 owner text it does not hold yet.

    New owner lines form one paragraph and new leave-out texts another, under "Leave out:". The
    labels are Russian when any collected text has Cyrillic. A line already in ``existing`` is
    never added again, so a re-run after a crash changes nothing.
    """
    items, leave_out = _owner_text(data)
    lang = "ru" if any(_CYRILLIC_RE.search(text) for text in [*(text for text, _ in items), *leave_out]) else "en"
    lines = set(existing.split("\n"))

    def missing(line: str) -> bool:
        return not set(line.split("\n")) <= lines

    owner: list[str] = []
    for text, pinned in items:
        line = f"{KEEP_LABEL[lang]} {text}" if pinned else text
        if missing(line) and line not in owner:
            owner.append(line)
    removed = [f"- {text}" for text in leave_out if missing(f"- {text}")]
    paragraphs = [existing] if existing else []
    if owner:
        paragraphs.append("\n".join(owner))
    if removed:
        paragraphs.append("\n".join([LEAVE_OUT_LABEL[lang], *removed]))
    return "\n\n".join(paragraphs)


def _seed(data: dict[str, Any]) -> str:
    """Active v1 model facts as the converted document, or "" when there are none.

    One ``## Section`` per v1 section in v1 order (unknown sections after them), one bullet per
    fact in file order, under v1's own section titles: Russian when most facts have Cyrillic. No refs
    and no title line: the first v2 pass rewrites it. It is never clipped, so it can be longer than
    the document limit; the panel then shows its start and names ``show``.
    """
    groups: dict[str, list[str]] = {}
    for fact in _facts(data):
        text = " ".join(_text(fact.get("text")).split())
        if not text or fact.get("kind") not in SEED_KINDS:
            continue
        section = fact.get("section")
        name = " ".join(section.replace("_", " ").split()) if isinstance(section, str) else ""
        groups.setdefault(name.lower() or "other", []).append(text)
    texts = [text for group in groups.values() for text in group]
    russian = 2 * sum(1 for text in texts if _CYRILLIC_RE.search(text)) > len(texts)
    titles = SECTION_TITLES["ru" if russian else "en"]
    order = {name.replace("_", " "): index for index, name in enumerate(CONTACT_SECTIONS)}
    names = sorted(groups, key=lambda name: order.get(name, len(order)))
    return "\n\n".join(
        "\n".join(
            [f"## {titles.get(name.replace(' ', '_')) or name.capitalize()}", *(f"- {text}" for text in groups[name])]
        )
        for name in names
    )


def _merged_chats(chats: list[dict[str, Any]], data: dict[str, Any]) -> list[dict[str, Any]]:
    """The persona's ``chats`` plus the chats of the v1 ``chat_aliases``, each chat once.

    Raw ids, because owner lookups and the persona erase need them.
    """
    merged = list(chats)
    known = {(str(chat.get("business_connection_id")), str(chat.get("chat_id"))) for chat in chats}
    aliases = data.get("chat_aliases")
    for info in aliases.values() if isinstance(aliases, dict) else ():
        if not isinstance(info, dict):
            continue
        connection, chat_id = info.get("business_connection_id"), info.get("chat_id")
        if _id(connection) and _id(chat_id) and (str(connection), str(chat_id)) not in known:
            known.add((str(connection), str(chat_id)))
            merged.append({"business_connection_id": str(connection), "chat_id": chat_id})
    return merged
