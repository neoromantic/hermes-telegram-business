"""The v2 dossier file: one ``document.json`` per contact and one for the owner persona.

Stdlib and ``history_support`` only. A document holds the Markdown the model wrote, the version
before it, the owner note, the refresh cursor and the counters, so one atomic replace commits all
of it. Writers hold the dossier data lock; readers take no lock and rely on the atomic replace.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import history_support as _history

DOC_FILE = "document.json"
V1_FILE = "dossier.json"  # read only by the v1 migration
V1_BACKUP = "v1-dossier.json"  # the migrated v1 file, kept until the first v2 pass
# Every backup: a later v1 file is kept as v1-dossier.<n>.json, never over one. Any other name, such as an
# owner's own "v1-dossier copy.json", is not a backup: it is never listed, counted or deleted.
V1_BACKUP_RE = re.compile(r"v1-dossier(?:\.([0-9]+))?\.json")
V1_CHANGES = "changes.jsonl"  # the v1 revision log, deleted by the migration
DOC_FORMAT = 2
DOC_MAX_UNITS = 3200  # UTF-16 units, every kind: the Telegram screen needs room around it
NOTE_MAX_CHARS = 500  # limit for a note typed by the owner; a migrated note may be longer and is never clipped
KINDS = ("contact", "persona")
DOC_ERROR = "dossier file is corrupt or unsupported; reset it with 'hermes telesec dossier forget …'"

# C0 controls except tab and newline, DEL and C1 controls, and the bidi embedding, override and isolate controls.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


class DocError(ValueError):
    """A ``document.json`` that v2 cannot use. The message is fixed text: never a path, id or content."""

    def __init__(self, message: str = DOC_ERROR) -> None:
        super().__init__(message)


def units(text: str) -> int:
    """Length in UTF-16 code units, the way Telegram counts it."""
    return len(text.encode("utf-16-le")) // 2


def strip_controls(text: str) -> str:
    """``text`` without control and bidi characters; newlines stay and a tab becomes a space."""
    return _CONTROL_RE.sub("", text.replace("\r\n", "\n").replace("\t", " "))


def clean_note(text: str) -> str:
    """An owner note as stored: controls removed (newlines kept), trimmed. It is never clipped here."""
    return strip_controls(text).strip()


def note_sha(doc: dict[str, Any]) -> str | None:
    """Hash of the doc's note, or None when there is no note.

    The stored ``note_sha`` is this value for the note the current document was written with, so a
    different value means the note changed and the target needs a refresh.
    """
    note = doc.get("note") or ""
    return hashlib.sha256(note.encode("utf-8")).hexdigest()[:16] if note else None


def _base(kind: str, now: datetime) -> dict[str, Any]:
    return {
        "format": DOC_FORMAT,
        "kind": kind,
        "id": secrets.token_hex(6),  # never changes; a forget deletes the file, so a rebuild gets a new id
        "created_at": _history._isoformat_utc(now.replace(microsecond=0)),
        "updated_at": None,  # last content change
        "checked_at": None,  # last time messages were processed
        "revision": 0,
        "document": "",
        "previous": None,
        "note": "",
        "note_updated_at": None,
        "note_sha": None,
        "messages": 0,
        "last_call": None,
    }


def new_contact(business_connection_id: Any, chat_id: Any, now: datetime) -> dict[str, Any]:
    """An empty contact document. ``window_start`` is None, so the next pass is a first pass."""
    return {
        **_base("contact", now),
        "business_connection_id": str(business_connection_id),
        "chat_id": chat_id,
        "window_start": None,
        "floor": None,
        "older_history_not_sent": False,
        "seen": {},
    }


def new_persona(now: datetime) -> dict[str, Any]:
    """An empty persona document. ``chats`` lists the chats (raw ids) whose messages fed the document."""
    return {**_base("persona", now), "chats": [], "newest_at": None}


def _text(value: Any) -> bool:
    return isinstance(value, str)


def _optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _flag(value: Any) -> bool:
    return isinstance(value, bool)


def _chat_id(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


def _ident(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _call(value: Any) -> bool:
    return value is None or isinstance(value, dict)


def _seen(value: Any) -> bool:
    # {str(message_id): [fp, order_at]}
    return isinstance(value, dict) and all(
        isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and _optional_text(item[1])
        for item in value.values()
    )


def _chats(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


_COMMON_FIELDS: dict[str, Callable[[Any], bool]] = {
    "id": _ident,
    "created_at": _optional_text,
    "updated_at": _optional_text,
    "checked_at": _optional_text,
    "revision": _count,
    "document": _text,
    "previous": _optional_text,
    "note": _text,
    "note_updated_at": _optional_text,
    "note_sha": _optional_text,
    "messages": _count,
    "last_call": _call,
}
_KIND_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "contact": {
        "business_connection_id": _text,
        "chat_id": _chat_id,
        "window_start": _optional_text,
        "floor": _optional_text,
        "older_history_not_sent": _flag,
        "seen": _seen,
    },
    "persona": {"chats": _chats, "newest_at": _optional_text},
}
# Present only on migrated files: "migrated_from" until the first v2 pass, and "note_from" while the
# note holds converted v1 text (until the owner replaces or clears it). "seed" is only in files an
# earlier build of this version wrote: the v1 facts, which read_doc now shows as the document. A
# persona whose text moved to "previous" also has "previous_chats", the chats that text was built from.
_OPTIONAL_FIELDS: dict[str, Callable[[Any], bool]] = {
    "migrated_from": _optional_text,
    "note_from": _optional_text,
    "seed": _optional_text,
    "previous_chats": _chats,
}


def valid(doc: Any) -> bool:
    """True when ``doc`` has format 2, a known kind and every field with its type. Unknown keys are allowed."""
    if not isinstance(doc, dict) or type(doc.get("format")) is not int or doc["format"] != DOC_FORMAT:
        return False
    kind = doc.get("kind")
    if kind not in KINDS:
        return False
    required = {**_COMMON_FIELDS, **_KIND_FIELDS[kind]}
    if any(name not in doc or not check(doc[name]) for name, check in required.items()):
        return False
    return all(check(doc[name]) for name, check in _OPTIONAL_FIELDS.items() if name in doc)


def read_doc(target_dir: Path) -> dict[str, Any] | None:
    """The document in ``target_dir``, or None when there is none. Raises DocError when it is unusable.

    v2 never reads ``dossier.json`` here: only the migration does.
    """
    try:
        doc = json.loads((target_dir / DOC_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:  # bad JSON or bad UTF-8
        raise DocError from None
    if not valid(doc):
        raise DocError
    seed = doc.pop("seed", None)
    if seed and not doc["document"] and doc.get("window_start") is None:
        doc["document"] = seed  # an earlier build kept the v1 facts beside an empty document
    return doc


def converted(doc: dict[str, Any]) -> bool:
    """A contact document the migration wrote from v1 facts (revision 0) that no v2 pass has rewritten yet.

    It can be longer than DOC_MAX_UNITS: the migration never cuts a fact.
    """
    return doc.get("kind") == "contact" and doc.get("window_start") is None and bool(doc.get("document"))


def backups(target_dir: Path) -> list[Path]:
    """The v1 backups in ``target_dir``: ``v1-dossier.json``, then any ``v1-dossier.<n>.json``, by number."""
    found = [path for path in target_dir.glob("v1-dossier*.json") if V1_BACKUP_RE.fullmatch(path.name)]
    return sorted(found, key=lambda path: (len(path.name), path.name))


def write_doc(target_dir: Path, doc: dict[str, Any]) -> None:
    """Replace ``target_dir/document.json`` in one atomic step: file 0600, directory 0700.

    The caller holds the dossier data lock, because the temporary file name is fixed. A document
    that ``read_doc`` would reject is refused before anything is written.
    """
    if not valid(doc):
        raise ValueError("refusing to write an invalid dossier document")
    _history._ensure_private_dir(target_dir)
    _history._write_json_atomic(target_dir / DOC_FILE, doc)
