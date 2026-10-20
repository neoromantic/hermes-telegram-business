"""The v1 → v2 dossier migration (``dossier_migrate``, SPEC M with override O3).

Most tests load ``dossier_migrate`` in a bare package (``core_module``) and call ``migrate_root``
on a temporary root, so they do not depend on the rest of the plugin. The last tests drive the
real plugin: the first v2 pass over a migrated contact, and when the migration runs. All data is
made up.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from dossier_helpers import (
    BUSINESS_ID,
    NOW,
    FakeCtx,
    FakeLlm,
    core_module,
    cycle,
    doc,
    dossier_dir,
    read_dossier,
    seed,
    snapshot_tree,
)

CONN = "biz-demo-1"
CHAT = 700001
V1_CREATED = "2026-07-01T08:30:00Z"


@pytest.fixture
def dm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    return core_module(monkeypatch, tmp_path, "dossier_migrate")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "dossiers"


# ---------------------------------------------------------------------------
# v1 files, shaped like the ones 0.13.0 wrote
# ---------------------------------------------------------------------------


def fact(
    fid: str,
    text: str,
    *,
    section: str = "relationship",
    kind: str = "observed",
    status: str = "active",
    pinned: bool = False,
) -> dict[str, Any]:
    owner = {"action": "noted", "at": "2026-09-01T10:00:00Z"} if kind == "owner" else None
    sources = (
        []
        if kind == "owner"
        else [
            {
                "ref": "m77",
                "message_id": 77,
                "date": "2026-09-30T10:00:00Z",
                "first_seen": "2026-09-30T10:00:00Z",
                "direction": "inbound",
                "transcript": False,
                "deleted": False,
                "quote": None,
                "fp": "0123456789abcdef",
            }
        ]
    )
    return {
        "id": fid,
        "section": section,
        "kind": kind,
        "confidence": None if kind == "owner" else "high",
        "text": text,
        "status": status,
        "pinned": pinned,
        "source_gone": False,
        "unsupported_cycle": None,
        "first_revision": 1,
        "last_revision": 2,
        "owner": owner,
        "sources": sources,
    }


def corrected(fid: str, text: str, previous: str, *, section: str = "relationship") -> dict[str, Any]:
    """A model fact the owner corrected: v1 turned it into an owner fact."""
    item = fact(fid, text, section=section, kind="owner")
    item["owner"] = {"action": "corrected", "at": "2026-09-02T10:00:00Z", "previous_text": previous}
    return item


def removal(text: str, at: str, *, by: str = "remove", fid: str = "f9") -> dict[str, Any]:
    return {"fact_id": fid, "text": text, "at": at, "by": by}


def v1_contact(
    *facts: dict[str, Any],
    removed: tuple[dict[str, Any], ...] = (),
    conn: Any = CONN,
    chat: Any = CHAT,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "contact",
        "dossier_id": "a1b2c3d4e5f6",
        "created_at": V1_CREATED,
        "refreshed_at": "2026-10-01T12:00:00Z",
        "revision": 5,
        "last_revision_at": "2026-10-01T12:00:00Z",
        "next_fact_id": 10,
        "last_refresh": {"at": "2026-10-01T12:00:00Z", "trigger": "worker", "provider": "gemini", "calls": 1},
        "coverage": {
            "window_start": "2026-07-01T00:00:00Z",
            "older_history_not_sent": False,
            "messages_processed": 120,
            "first_message_at": "2026-07-01T09:00:00Z",
            "last_message_at": "2026-09-30T10:00:00Z",
            "tracked_floor": "2026-07-01T00:00:00Z",
        },
        "seen": {"77": ["0123456789abcdef", "2026-09-30T10:00:00Z"]},
        "removed": list(removed),
        "facts": list(facts),
        "business_connection_id": conn,
        "chat_id": chat,
        "contact": {"display_name": "Mira Tanaka", "aliases": ["@mira_demo"]},
    }


def v1_persona(
    *facts: dict[str, Any],
    removed: tuple[dict[str, Any], ...] = (),
    aliases: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = v1_contact(*facts, removed=removed)
    for contact_only in ("business_connection_id", "chat_id", "contact"):
        del data[contact_only]
    data.update(kind="persona", chat_aliases=aliases or {}, next_alias=len(aliases or {}) + 1)
    return data


def contact_dir(dm: ModuleType, root: Path, conn: Any = CONN, chat: Any = CHAT) -> Path:
    """``<root>/chats/<chat_key>``, the directory v1 used for this chat."""
    return root / "chats" / dm._history._path_key(conn) / dm._history._safe_part(chat)


def put(target: Path, data: dict[str, Any] | bytes, *, changes: bool = True) -> bytes:
    """Write a v1 ``dossier.json`` (and a ``changes.jsonl``) into ``target``; returns the file bytes."""
    target.mkdir(parents=True, exist_ok=True)
    raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
    (target / "dossier.json").write_bytes(raw)
    if changes:
        line = {
            "revision": 2,
            "actor": "owner",
            "changes": [{"op": "remove", "fact_id": "f9", "text": "Plans to leave."}],
        }
        (target / "changes.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    return raw


def read_doc(target: Path) -> dict[str, Any]:
    return json.loads((target / "document.json").read_text(encoding="utf-8"))


def iso(dm: ModuleType, value: Any) -> str:
    return dm._history._isoformat_utc(value)


MIRA_FACTS = (
    fact("f1", "Mira is the design lead at Northwind Demo."),
    fact("f2", "Prefers calls after 18:00,  never before.", section="preferences", kind="owner"),
    fact("f3", "Review of the Orbit mockups is due on 28 October.", section="commitments", pinned=True),
    corrected("f4", "Her work e-mail is mira@example.com.", "Her work e-mail is old@example.com."),
    fact("f5", "Likes short messages.", section="preferences", kind="inferred"),
    fact("f6", "Has a dog named Pixel.", section="shared_facts", status="unsupported"),
    fact("f7", "Asked to move the review to Wednesday.", section="decisions"),
)
MIRA_REMOVED = (
    removal("Her work e-mail is old@example.com.", "2026-09-02T10:00:00Z", by="correct", fid="f4"),
    removal("Plans to leave Northwind Demo.", "2026-09-03T10:00:00Z"),
)
MIRA_NOTE = (
    "Prefers calls after 18:00,  never before.\n"
    "Keep: Review of the Orbit mockups is due on 28 October.\n"
    "Her work e-mail is mira@example.com.\n"
    "\n"
    "Leave out:\n"
    "- Her work e-mail is old@example.com.\n"
    "- Plans to leave Northwind Demo."
)
MIRA_SEED = (
    "## Relationship\n"
    "- Mira is the design lead at Northwind Demo.\n"
    "\n"
    "## Preferences\n"
    "- Likes short messages.\n"
    "\n"
    "## Commitments\n"
    "- Review of the Orbit mockups is due on 28 October.\n"
    "\n"
    "## Decisions\n"
    "- Asked to move the review to Wednesday."
)


# ---------------------------------------------------------------------------
# A contact
# ---------------------------------------------------------------------------


def test_contact_moves_to_a_v2_document(dm: ModuleType, root: Path, no_network: None) -> None:
    target = contact_dir(dm, root)
    original = put(target, v1_contact(*MIRA_FACTS, removed=MIRA_REMOVED))

    report = dm.migrate_root(root, NOW)

    assert report == {"migrated": 1, "unreadable": 0, "backups": 1}
    doc = read_doc(target)
    assert doc == {
        "format": 2,
        "kind": "contact",
        "id": doc["id"],
        "business_connection_id": CONN,
        "chat_id": CHAT,
        "created_at": V1_CREATED,
        "updated_at": None,
        "checked_at": None,
        "revision": 0,
        "document": MIRA_SEED,  # readable at once; the first v2 pass rewrites it
        "previous": None,
        "note": MIRA_NOTE,
        "note_updated_at": iso(dm, NOW),
        "note_sha": None,
        "window_start": None,
        "floor": None,
        "older_history_not_sent": False,
        "messages": 0,
        "seen": {},
        "last_call": None,
        "note_from": "v1",
        "migrated_from": "v1",
    }
    assert len(doc["id"]) == 12 and doc["id"] != "a1b2c3d4e5f6"  # a new id, never the v1 dossier_id
    assert dm._doc.read_doc(target) == doc
    assert not (target / "dossier.json").exists()
    assert (target / "v1-dossier.json").read_bytes() == original  # the backup is the v1 file as it was
    assert not (target / "changes.jsonl").exists()
    assert sorted(path.name for path in target.iterdir()) == ["document.json", "v1-dossier.json"]


def test_owner_notes_and_corrections_go_into_the_note_verbatim(dm: ModuleType, root: Path) -> None:
    long_note = "Before any call, check the Orbit tracker and the shared calendar. " * 7
    target = contact_dir(dm, root)
    put(
        target,
        v1_contact(
            fact("f1", "Mira is the design lead at Northwind Demo."),
            fact("f2", "  Prefers calls after 18:00,  never before.  ", kind="owner"),
            corrected("f3", "Lives in Riga Demo, not Oslo Demo.", "Lives in Oslo Demo."),
            fact("f4", long_note.strip(), section="open_questions", kind="owner"),
            fact("f5", "Prefers calls after 18:00,  never before.", kind="owner"),  # the same text twice
        ),
    )

    dm.migrate_root(root, NOW)

    note = read_doc(target)["note"]
    assert note == (
        "Prefers calls after 18:00,  never before.\nLives in Riga Demo, not Oslo Demo.\n" + long_note.strip()
    )
    assert len(note) > dm._doc.NOTE_MAX_CHARS  # owner text is never clipped
    assert "Mira is the design lead" not in note  # model facts go to the document, not the note


def test_pinned_model_facts_become_keep_lines(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    put(
        target,
        v1_contact(
            fact("f1", "Pays invoices on the 5th.", pinned=True),
            fact("f2", "Never call on Sundays.", kind="owner", pinned=True),  # an owner fact stays as written
            fact("f3", "Prefers e-mail to calls.", pinned=False),
            fact("f4", "Uses a standing desk.", kind="inferred", pinned=True),
        ),
    )

    dm.migrate_root(root, NOW)

    assert (
        read_doc(target)["note"]
        == "Keep: Pays invoices on the 5th.\nNever call on Sundays.\nKeep: Uses a standing desk."
    )


def test_cyrillic_text_switches_the_labels_to_russian(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    put(
        target,
        v1_contact(
            fact("f1", "Звонить после 18:00.", kind="owner"),
            fact("f2", "Review is on Friday.", pinned=True),
            removed=(removal("Собирается уволиться.", "2026-09-03T10:00:00Z"),),
        ),
    )

    dm.migrate_root(root, NOW)

    assert read_doc(target)["note"] == (
        "Звонить после 18:00.\nСохранить: Review is on Friday.\n\nНе включать:\n- Собирается уволиться."
    )


def test_removed_texts_become_a_leave_out_paragraph(dm: ModuleType, root: Path) -> None:
    base = NOW - timedelta(days=60)
    entries = [removal(f"Wrong fact {i:02d}.", iso(dm, base + timedelta(hours=i))) for i in range(1, 26)]
    shuffled = entries[1::2] + entries[::2]  # file order does not matter, only "at"
    shuffled.insert(3, removal("Wrong fact 24.", iso(dm, base)))  # an older copy of a newer text
    shuffled.insert(5, removal("Wrong fact 25.", iso(dm, base), by="correct"))
    shuffled += [
        removal("Retired by the model.", iso(dm, NOW), by="model"),  # only owner removals count
        {"fact_id": "f8", "text": "No reason given.", "at": iso(dm, NOW)},
        removal("   ", iso(dm, NOW)),
        "not an entry",
    ]
    target = contact_dir(dm, root)
    put(target, v1_contact(removed=tuple(shuffled)))  # type: ignore[arg-type]

    dm.migrate_root(root, NOW)

    newest = [f"- Wrong fact {i:02d}." for i in range(6, 26)]  # the newest 20, oldest first
    assert read_doc(target)["note"] == "\n".join(["Leave out:", *newest])
    assert dm.LEAVE_OUT_MAX == 20


def test_seed_groups_active_model_facts_by_v1_section(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    put(
        target,
        v1_contact(
            fact("f1", "Asked for the budget by Friday.", section="open_questions"),
            fact("f2", "Mira is the design lead.", section="relationship"),
            fact("f3", "Prefers   short\nmessages.", section="preferences", kind="inferred"),
            fact("f4", "Said the launch moved to May.", section="contradictions"),
            fact("f5", "Owner-only line.", section="relationship", kind="owner"),
            fact("f6", "A fact that lost its sources.", section="decisions", status="unsupported"),
            fact("f7", "Agreed to a weekly call.", section="shared_facts"),
            fact("f8", "Plays chess on weekends.", section="hobbies"),  # not a v1 section: it goes last
            fact("f9", "Agreed on the Orbit price.", section="decisions"),
            fact("f10", "Prefers mornings.", section="preferences"),
        ),
    )

    dm.migrate_root(root, NOW)

    assert read_doc(target)["document"] == (
        "## Relationship\n- Mira is the design lead.\n\n"
        "## Preferences\n- Prefers short messages.\n- Prefers mornings.\n\n"
        "## Shared facts\n- Agreed to a weekly call.\n\n"
        "## Decisions\n- Agreed on the Orbit price.\n\n"
        "## Open questions\n- Asked for the budget by Friday.\n\n"
        "## Contradictions\n- Said the launch moved to May.\n\n"
        "## Hobbies\n- Plays chess on weekends."
    )


def test_the_converted_document_uses_russian_titles_and_is_never_cut(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    long_facts = [fact(f"f{i}", f"Факт номер {i}: " + "длинный текст " * 20, section="shared_facts")
                  for i in range(10, 40)]
    put(
        target,
        v1_contact(
            fact("f1", "Мира ведёт редизайн Orbit."),
            fact("f2", "Любит короткие сообщения.", section="preferences"),
            fact("f3", "Review is on Friday.", section="commitments"),  # one English fact keeps Russian titles
            *long_facts,
        ),
    )

    dm.migrate_root(root, NOW)

    document = read_doc(target)["document"]
    assert document.startswith(
        "## Отношения\n- Мира ведёт редизайн Orbit.\n\n## Предпочтения\n- Любит короткие сообщения.\n\n## Общие факты\n"
    )
    assert document.endswith("\n\n## Обязательства\n- Review is on Friday.")
    assert dm._doc.units(document) > dm._doc.DOC_MAX_UNITS  # over the limit, and still every fact is there
    assert all(f"- Факт номер {i}: " in document for i in range(10, 40))


def test_a_contact_without_model_facts_has_no_seed(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    put(target, v1_contact(fact("f1", "Never call on Sundays.", kind="owner")))

    dm.migrate_root(root, NOW)

    doc = read_doc(target)
    assert "seed" not in doc and doc["document"] == ""
    assert doc["note"] == "Never call on Sundays."


def test_an_empty_v1_dossier_moves_with_an_empty_note(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    put(target, v1_contact(), changes=False)

    assert dm.migrate_root(root, NOW)["migrated"] == 1

    doc = read_doc(target)
    assert doc["note"] == "" and doc["note_updated_at"] is None
    assert "seed" not in doc and doc["migrated_from"] == "v1"


def test_contact_ids_come_from_the_v1_file(dm: ModuleType, root: Path) -> None:
    conn = "biz demo/1"  # _safe_part changes it, so only the file knows the real id
    target = contact_dir(dm, root, conn, "700001")
    put(target, v1_contact(conn=conn, chat="700001"))

    dm.migrate_root(root, NOW)

    doc = read_doc(target)
    assert (doc["business_connection_id"], doc["chat_id"]) == (conn, "700001")


# ---------------------------------------------------------------------------
# The persona
# ---------------------------------------------------------------------------


def test_persona_moves_with_chats_from_its_aliases_and_no_seed(dm: ModuleType, root: Path) -> None:
    aliases = {
        "c1": {"business_connection_id": CONN, "chat_id": 700001, "chat_key": "k1"},
        "c2": {"business_connection_id": CONN, "chat_id": 700002, "chat_key": "k2"},
        "c3": {"business_connection_id": "biz-demo-2", "chat_id": 700003, "chat_key": "k3"},
        "c4": {"business_connection_id": CONN, "chat_id": 700001, "chat_key": "k1"},  # the same chat again
        "c5": "not an alias",
    }
    target = root / "persona"
    original = put(
        target,
        v1_persona(
            fact("f1", "Never uses emoji.", section="formatting", kind="owner"),
            fact("f2", "Writes short replies.", section="reply_length", pinned=True),
            fact("f3", "Greets with a first name.", section="greetings"),
            removed=(removal("Signs every message.", "2026-09-03T10:00:00Z"),),
            aliases=aliases,
        ),
    )

    report = dm.migrate_root(root, NOW)

    assert report == {"migrated": 1, "unreadable": 0, "backups": 1}
    doc = read_doc(target)
    assert doc["kind"] == "persona"
    assert doc["chats"] == [
        {"business_connection_id": CONN, "chat_id": 700001},
        {"business_connection_id": CONN, "chat_id": 700002},
        {"business_connection_id": "biz-demo-2", "chat_id": 700003},
    ]
    assert doc["note"] == "Never uses emoji.\nKeep: Writes short replies.\n\nLeave out:\n- Signs every message."
    assert "seed" not in doc  # the persona is rebuilt from scratch
    assert doc["document"] == "" and doc["newest_at"] is None and doc["messages"] == 0
    assert doc["created_at"] == V1_CREATED and doc["migrated_from"] == "v1"
    assert dm._doc.read_doc(target) == doc
    assert (target / "v1-dossier.json").read_bytes() == original
    assert not (target / "dossier.json").exists() and not (target / "changes.jsonl").exists()


def test_every_target_moves_in_one_run(dm: ModuleType, root: Path) -> None:
    put(contact_dir(dm, root, CONN, 700001), v1_contact(chat=700001))
    put(contact_dir(dm, root, CONN, 700002), v1_contact(chat=700002))
    put(contact_dir(dm, root, "biz-demo-2", 700003), v1_contact(conn="biz-demo-2", chat=700003))
    put(contact_dir(dm, root, CONN, 700004), b"{not json")
    put(root / "persona", v1_persona())
    (root / "chats" / "stray").mkdir()  # a directory without dossier.json is not touched

    report = dm.migrate_root(root, NOW)

    assert report == {"migrated": 4, "unreadable": 1, "backups": 5}
    assert list(root.glob("**/dossier.json")) == []
    assert len(list(root.glob("chats/*/*/document.json"))) == 4


def test_nothing_to_do_without_v1_files(dm: ModuleType, root: Path) -> None:
    assert dm.migrate_root(root, NOW) == {"migrated": 0, "unreadable": 0, "backups": 0}
    assert not root.exists()  # the migration never creates the root


# ---------------------------------------------------------------------------
# Crash safety and re-runs
# ---------------------------------------------------------------------------


def test_the_document_is_written_before_the_rename(dm: ModuleType, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = contact_dir(dm, root)
    original = put(target, v1_contact(*MIRA_FACTS, removed=MIRA_REMOVED))
    real_replace = os.replace

    def crash_on_the_rename(src: Any, dst: Any) -> None:
        if Path(src).name == "dossier.json":
            raise OSError("simulated crash")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crash_on_the_rename)
    crashed = dm.migrate_root(root, NOW)
    monkeypatch.setattr(os, "replace", real_replace)

    assert crashed == {"migrated": 0, "unreadable": 0, "backups": 0}
    first = read_doc(target)
    assert first["note"] == MIRA_NOTE  # the owner text is safe before the v1 file moves
    assert (target / "dossier.json").read_bytes() == original
    assert not (target / "changes.jsonl").exists()  # deleted before the rename, so it is never left behind

    rerun = dm.migrate_root(root, NOW + timedelta(minutes=5))

    assert rerun == {"migrated": 1, "unreadable": 0, "backups": 1}
    doc = read_doc(target)
    assert doc == first  # no line twice, same id, same seed, note time unchanged
    assert (target / "v1-dossier.json").read_bytes() == original


def test_a_failed_document_write_changes_nothing(dm: ModuleType, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = contact_dir(dm, root)
    put(target, v1_contact(*MIRA_FACTS, removed=MIRA_REMOVED))
    before = snapshot_tree(root)

    def disk_full(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(dm._doc, "write_doc", disk_full)

    assert dm.migrate_root(root, NOW) == {"migrated": 0, "unreadable": 0, "backups": 0}
    assert snapshot_tree(root) == before  # dossier.json and changes.jsonl stay until the note is safe


def test_a_rerun_after_success_changes_nothing(dm: ModuleType, root: Path, no_network: None) -> None:
    put(contact_dir(dm, root), v1_contact(*MIRA_FACTS, removed=MIRA_REMOVED))
    put(root / "persona", v1_persona(fact("f1", "Never uses emoji.", kind="owner")))
    put(contact_dir(dm, root, CONN, 700002), b"[]")
    dm.migrate_root(root, NOW)
    after_first = snapshot_tree(root)

    assert dm.migrate_root(root, NOW + timedelta(hours=1)) == {
        "migrated": 0,
        "unreadable": 0,
        "backups": 3,
    }
    assert snapshot_tree(root) == after_first  # contents and mtimes


def test_a_new_v1_file_merges_into_the_v2_document(dm: ModuleType, root: Path) -> None:
    """After a rollback to v1 and a new upgrade: only owner text the note lacks is added."""
    target = contact_dir(dm, root)
    put(
        target,
        v1_contact(
            fact("f1", "Never call on Sundays.", kind="owner"),
            removed=(removal("Plans to leave Northwind Demo.", "2026-09-03T10:00:00Z"),),
        ),
    )
    dm.migrate_root(root, NOW)
    doc = read_doc(target)
    doc.update(  # the first v2 pass ran and removed the backup
        window_start="2026-07-23T00:00:00Z",
        checked_at=iso(dm, NOW),
        document="**Mira is the design lead at Northwind Demo.**",
        revision=1,
        note_sha=dm._doc.note_sha(doc),
    )
    assert "seed" not in doc  # no model facts in that v1 file
    del doc["migrated_from"]
    dm._doc.write_doc(target, doc)
    (target / "v1-dossier.json").unlink()
    put(
        target,
        v1_contact(
            fact("f1", "Never call on Sundays.", kind="owner"),
            fact("f2", "Has a new office in Riga Demo.", kind="owner"),
            fact("f3", "Works on Orbit.", section="commitments"),
            removed=(
                removal("Plans to leave Northwind Demo.", "2026-09-03T10:00:00Z"),
                removal("Lives in Oslo Demo.", "2026-10-03T10:00:00Z"),
            ),
        ),
    )
    later = NOW + timedelta(days=3)

    assert dm.migrate_root(root, later)["migrated"] == 1

    merged = read_doc(target)
    assert merged["note"] == (
        "Never call on Sundays.\n\nLeave out:\n- Plans to leave Northwind Demo.\n\n"
        "Has a new office in Riga Demo.\n\nLeave out:\n- Lives in Oslo Demo."
    )
    assert merged["note_updated_at"] == iso(dm, later)
    assert dm._doc.note_sha(merged) != merged["note_sha"]  # so the next cycle rewrites with the new note
    assert "seed" not in merged  # the v2 pass already happened
    assert merged["migrated_from"] == "v1"  # the next pass deletes the new backup
    for kept in ("id", "document", "revision", "window_start", "created_at"):
        assert merged[kept] == doc[kept]


def test_the_seed_is_replaced_only_before_the_first_v2_pass(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    put(target, v1_contact(fact("f1", "Works on Orbit.")))
    dm.migrate_root(root, NOW)
    put(target, v1_contact(fact("f1", "Works on Orbit and Nova.")))

    dm.migrate_root(root, NOW)

    assert read_doc(target)["document"] == "## Relationship\n- Works on Orbit and Nova."
    put(target, v1_contact())  # a v1 file with no model facts drops the stale converted text
    dm.migrate_root(root, NOW)
    assert read_doc(target)["document"] == ""


# ---------------------------------------------------------------------------
# Files the migration cannot use
# ---------------------------------------------------------------------------

UNREADABLE = [
    b"{not json",
    b"\xff\xfe\x00 not utf-8",
    b"[]",
    json.dumps({"schema_version": 2, "kind": "contact", "facts": []}).encode(),
    json.dumps({"schema_version": 1, "kind": "contact", "facts": {}}).encode(),
    json.dumps({"schema_version": 1, "kind": "persona", "facts": []}).encode(),  # wrong kind for a contact
]


@pytest.mark.parametrize("content", UNREADABLE)
def test_unreadable_v1_is_kept_as_a_backup_next_to_a_fresh_document(
    dm: ModuleType, root: Path, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    target = contact_dir(dm, root)
    put(target, content)

    report = dm.migrate_root(root, NOW)

    assert report == {"migrated": 0, "unreadable": 1, "backups": 1}
    assert (target / "v1-dossier.json").read_bytes() == content
    assert not (target / "dossier.json").exists() and not (target / "changes.jsonl").exists()
    doc = read_doc(target)
    assert (doc["business_connection_id"], doc["chat_id"]) == (CONN, CHAT)  # read back from the directory names
    assert doc["note"] == "" and doc["document"] == "" and doc["window_start"] is None
    assert "migrated_from" not in doc  # the backup stays until forget: it may hold owner text
    assert dm._doc.valid(doc)
    assert dm.unreadable_backups(root) == [f"{target.relative_to(root).as_posix()}/v1-dossier.json"]
    chat_ref = hashlib.sha256(f"{target.parent.name}/{target.name}".encode()).hexdigest()[:12]
    assert [record.getMessage() for record in caplog.records] == [
        f"telesec: old dossier file could not be read chat_ref={chat_ref}; it is kept as v1-dossier.json"
    ]


def test_json_nested_too_deep_is_unreadable(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    content = b"[" * 100_000 + b"]" * 100_000  # json.loads raises RecursionError, not ValueError
    put(target, content)

    assert dm.migrate_root(root, NOW) == {"migrated": 0, "unreadable": 1, "backups": 1}
    assert (target / "v1-dossier.json").read_bytes() == content
    assert read_doc(target)["note"] == ""


def test_unreadable_v1_keeps_an_existing_document(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    doc = dm._doc.new_contact(CONN, CHAT, NOW)
    doc.update(note="Never call on Sundays.", document="**Mira.**", revision=2)
    dm._doc.write_doc(target, doc)
    existing = (target / "document.json").read_bytes()
    put(target, b"{not json")

    assert dm.migrate_root(root, NOW)["unreadable"] == 1
    assert (target / "document.json").read_bytes() == existing
    assert (target / "v1-dossier.json").read_bytes() == b"{not json"


def test_unreadable_persona_gets_a_fresh_persona_document(dm: ModuleType, root: Path) -> None:
    target = root / "persona"
    put(target, json.dumps({"schema_version": 1, "kind": "contact", "facts": []}).encode())

    assert dm.migrate_root(root, NOW)["unreadable"] == 1

    doc = read_doc(target)
    assert doc["kind"] == "persona" and doc["chats"] == [] and "migrated_from" not in doc
    assert dm.unreadable_backups(root) == ["persona/v1-dossier.json"]


def test_unreadable_v1_in_a_directory_with_unknown_ids_gets_no_document(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root, "biz demo/1", CHAT)  # the directory name lost characters of the id
    put(target, b"{not json")

    assert dm.migrate_root(root, NOW)["unreadable"] == 1
    assert not (target / "document.json").exists()
    assert (target / "v1-dossier.json").read_bytes() == b"{not json"


def test_migrated_backups_are_not_reported_as_unreadable(dm: ModuleType, root: Path) -> None:
    put(contact_dir(dm, root), v1_contact(*MIRA_FACTS))
    put(root / "persona", v1_persona())
    dm.migrate_root(root, NOW)

    assert dm.unreadable_backups(root) == []


def test_a_later_v1_file_never_overwrites_a_kept_backup(
    dm: ModuleType, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A rollback, or a 0.14 gateway still running, writes dossier.json again next to an unreadable backup."""
    caplog.set_level(logging.WARNING)
    target = contact_dir(dm, root)
    rel = target.relative_to(root).as_posix()
    put(target, b"{not json")
    dm.migrate_root(root, NOW)
    readable = put(target, v1_contact(fact("f1", "Never call on Sundays.", kind="owner")))

    assert dm.migrate_root(root, NOW) == {"migrated": 1, "unreadable": 0, "backups": 2}

    assert (target / "v1-dossier.json").read_bytes() == b"{not json"  # the damaged bytes stay
    assert (target / "v1-dossier.1.json").read_bytes() == readable
    assert read_doc(target)["note"] == "Never call on Sundays."
    assert dm.unreadable_backups(root) == [f"{rel}/v1-dossier.json"]  # so status still warns

    put(target, b"[]")
    assert dm.migrate_root(root, NOW) == {"migrated": 0, "unreadable": 1, "backups": 3}
    assert (target / "v1-dossier.2.json").read_bytes() == b"[]"
    assert dm.unreadable_backups(root) == [f"{rel}/v1-dossier.json", f"{rel}/v1-dossier.2.json"]
    assert [record.getMessage().rpartition(" ")[2] for record in caplog.records] == [
        "v1-dossier.json", "v1-dossier.2.json"]

    dm.drop_migrated_backups("contact", target)  # what the first v2 pass does

    assert sorted(path.name for path in target.iterdir()) == ["document.json", "v1-dossier.2.json", "v1-dossier.json"]


def test_the_highest_backup_number_is_always_the_newest_file(dm: ModuleType, root: Path) -> None:
    """The rollback docs say to rename the backup with the highest number: a freed lower name is never reused."""
    target = contact_dir(dm, root)
    put(target, v1_contact(fact("f1", "Never call on Sundays.", kind="owner")))
    dm.migrate_root(root, NOW)
    put(target, b"{not json")  # e.g. a 0.14 gateway that was still running
    dm.migrate_root(root, NOW)
    dm.drop_migrated_backups("contact", target)  # the first v2 pass: only the unreadable .1 stays
    assert sorted(path.name for path in target.iterdir()) == ["document.json", "v1-dossier.1.json"]
    newest = put(target, v1_contact(fact("f2", "Pays on the 5th.", kind="owner")))

    assert dm.migrate_root(root, NOW) == {"migrated": 1, "unreadable": 0, "backups": 2}

    assert (target / "v1-dossier.2.json").read_bytes() == newest
    assert not (target / "v1-dossier.json").exists()
    assert read_doc(target)["note"] == "Never call on Sundays.\n\nPays on the 5th."


def test_a_corrupt_document_leaves_the_directory_untouched(
    dm: ModuleType, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    target = contact_dir(dm, root)
    put(target, v1_contact(*MIRA_FACTS, removed=MIRA_REMOVED))
    (target / "document.json").write_text('{"format": 3}', encoding="utf-8")
    before = snapshot_tree(root)

    assert dm.migrate_root(root, NOW) == {"migrated": 0, "unreadable": 0, "backups": 0}
    assert snapshot_tree(root) == before  # the v1 file stays, so no owner text is lost
    assert [record.getMessage().split("chat_ref=")[0] for record in caplog.records] == [
        "telesec: dossier migration failed "
    ]
    assert caplog.records[0].getMessage().endswith("error=DocError")


def test_a_persona_document_in_a_contact_directory_is_not_merged(dm: ModuleType, root: Path) -> None:
    target = contact_dir(dm, root)
    dm._doc.write_doc(target, dm._doc.new_persona(NOW))
    put(target, v1_contact(*MIRA_FACTS))

    assert dm.migrate_root(root, NOW) == {"migrated": 0, "unreadable": 0, "backups": 0}
    assert (target / "dossier.json").exists()


def test_an_unexpected_error_in_one_directory_does_not_stop_the_others(
    dm: ModuleType, root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    broken = contact_dir(dm, root, CONN, 700001)
    put(broken, v1_contact(*MIRA_FACTS, chat=700001))
    put(contact_dir(dm, root, CONN, 700002), v1_contact(*MIRA_FACTS, chat=700002))
    before = snapshot_tree(broken)
    real_seed = dm._seed

    def seed_fails_for_one_chat(data: dict[str, Any]) -> str:
        if data["chat_id"] == 700001:
            raise RuntimeError("simulated bug")
        return real_seed(data)

    monkeypatch.setattr(dm, "_seed", seed_fails_for_one_chat)

    assert dm.migrate_root(root, NOW) == {"migrated": 1, "unreadable": 0, "backups": 1}
    assert snapshot_tree(broken) == before  # its owner text stays in dossier.json for the next call
    (record,) = caplog.records
    assert record.getMessage().endswith("error=RuntimeError") and "simulated" not in record.getMessage()


def test_out_of_range_v1_dates_do_not_stop_the_migration(dm: ModuleType, root: Path) -> None:
    data = v1_contact(
        fact("f1", "Never call on Sundays.", kind="owner"),
        removed=(
            removal("Lives in Oslo Demo.", "0001-01-01T00:00:00+05:00"),  # before year 1 in UTC
            removal("Plans to leave Northwind Demo.", "2026-09-03T10:00:00Z"),
        ),
    )
    data["created_at"] = "0001-01-01T00:00:00+05:00"
    target = contact_dir(dm, root)
    put(target, data)

    assert dm.migrate_root(root, NOW)["migrated"] == 1

    doc = read_doc(target)
    assert doc["created_at"] == iso(dm, NOW.replace(microsecond=0))
    assert doc["note"] == (
        "Never call on Sundays.\n\nLeave out:\n- Lives in Oslo Demo.\n- Plans to leave Northwind Demo."
    )


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


def test_migration_logs_carry_no_text_names_or_ids(
    dm: ModuleType, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    conn, chat = "biz-canary-77", 700777
    put(
        contact_dir(dm, root, conn, chat),
        v1_contact(
            fact("f1", "CANARY-FACT about the Orbit budget."),
            fact("f2", "CANARY-NOTE call after six.", kind="owner"),
            removed=(removal("CANARY-REMOVED plans to leave.", "2026-09-03T10:00:00Z"),),
            conn=conn,
            chat=chat,
        ),
    )
    put(contact_dir(dm, root, conn, 700778), b'{"text": "CANARY-BROKEN"')
    broken = contact_dir(dm, root, conn, 700779)
    put(broken, v1_contact(fact("f1", "CANARY-FAILED", kind="owner"), conn=conn, chat=700779))
    (broken / "document.json").write_text("CANARY-CORRUPT", encoding="utf-8")
    put(root / "persona", v1_persona(fact("f1", "CANARY-PERSONA", kind="owner")))

    report = dm.migrate_root(root, NOW)

    assert report == {"migrated": 2, "unreadable": 1, "backups": 3}
    assert caplog.records
    canaries = ("CANARY", "Mira", "Tanaka", "mira_demo", conn, str(chat), "700778", "700779", str(root))
    for record in caplog.records:
        logged = record.getMessage() + repr(record.args)
        assert not any(canary in logged for canary in canaries), logged
        assert record.exc_info is None


# ---------------------------------------------------------------------------
# With the plugin: the first v2 pass and when the migration runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_v2_pass_sends_the_seed_and_deletes_the_backup(
    dossier_history: ModuleType, dossiers: ModuleType
) -> None:
    plugin, ds = dossier_history, dossiers
    chat = 6101
    await seed(
        plugin,
        chat,
        "mira_demo",
        [
            (1, "in", "Can we move the Orbit review to Wednesday?", 60),
            (2, "out", "Yes, Wednesday works for me.", 55),
        ],
    )
    target = dossier_dir(ds, chat)
    put(target, v1_contact(*MIRA_FACTS, removed=MIRA_REMOVED, conn=BUSINESS_ID, chat=chat))
    fake = FakeLlm()

    cycle(ds, fake)

    (call,) = fake.by_purpose("telesec_dossier")
    assert call["user"]["mode"] == "first"
    assert call["user"]["current_dossier"] == MIRA_SEED
    assert call["user"]["owner_note"] == MIRA_NOTE
    doc = read_dossier(ds, chat)
    assert doc is not None
    assert doc["revision"] == 1 and "Orbit review" in doc["document"]
    assert doc["note"] == MIRA_NOTE
    assert "seed" not in doc and "migrated_from" not in doc
    assert sorted(path.name for path in target.iterdir()) == ["document.json"]  # the backup is gone


@pytest.mark.asyncio
async def test_a_first_v2_pass_that_answers_no_change_keeps_the_seed(
    dossier_history: ModuleType, dossiers: ModuleType
) -> None:
    plugin, ds = dossier_history, dossiers
    chat = 6106
    await seed(plugin, chat, "mira_demo", [(1, "in", "Thanks, see you then.", 60), (2, "out", "See you!", 55)])
    target = dossier_dir(ds, chat)
    put(target, v1_contact(*MIRA_FACTS, conn=BUSINESS_ID, chat=chat))

    report = cycle(ds, FakeLlm(doc("NO_CHANGE")))

    assert (report.refreshed, report.failed) == (1, 0)
    kept = read_dossier(ds, chat)
    assert kept is not None and (kept["document"], kept["revision"]) == (MIRA_SEED, 1)
    assert "seed" not in kept and "migrated_from" not in kept
    assert sorted(path.name for path in target.iterdir()) == ["document.json"]


@pytest.mark.asyncio
async def test_an_unreadable_contact_backup_stays_after_the_first_v2_pass(
    dossier_history: ModuleType, dossiers: ModuleType
) -> None:
    plugin, ds = dossier_history, dossiers
    chat = 6104
    await seed(plugin, chat, "mira_demo", [(1, "in", "Can we meet on Wednesday?", 60), (2, "out", "Yes.", 55)])
    target = dossier_dir(ds, chat)
    content = put(target, b'{"schema_version": 1, "facts": [')  # may hold owner text that never reached the note

    cycle(ds, FakeLlm())

    doc = read_dossier(ds, chat)
    assert doc is not None and doc["revision"] == 1 and "migrated_from" not in doc
    assert (target / "v1-dossier.json").read_bytes() == content  # kept until a forget
    ds.forget(("contact", {"business_connection_id": BUSINESS_ID, "chat_id": chat}))
    assert not target.exists()


@pytest.mark.asyncio
async def test_the_persona_backup_goes_only_after_a_readable_migration(
    dossier_history: ModuleType, dossiers: ModuleType
) -> None:
    plugin, ds = dossier_history, dossiers
    await seed(plugin, 6105, "mira_demo", [(1, "in", "Can we meet on Wednesday?", 60), (2, "out", "Yes, at ten.", 55)])
    persona = ds.persona_dir()

    put(persona, v1_persona(fact("f1", "Never uses emoji.", kind="owner")))
    cycle(ds, FakeLlm(), target=("persona", None))
    doc = ds.load_persona()
    assert doc is not None and doc["revision"] == 1 and doc["note"] == "Never uses emoji."
    assert "migrated_from" not in doc
    assert not (persona / "v1-dossier.json").exists()  # its owner text is in the note

    ds.forget(("persona", None))
    content = put(persona, b'{"schema_version": 1, "kind": "persona", "facts": [')
    cycle(ds, FakeLlm(), target=("persona", None))
    doc = ds.load_persona()
    assert doc is not None and doc["revision"] == 1 and "migrated_from" not in doc
    assert (persona / "v1-dossier.json").read_bytes() == content  # unreadable: kept, it may hold owner text

    result = ds.forget(("contact", {"business_connection_id": BUSINESS_ID, "chat_id": 6105}))
    assert result["persona_reset"] is True  # a kept v1 backup may hold this chat's text
    assert not (persona / "v1-dossier.json").exists()


@pytest.mark.asyncio
async def test_every_backup_is_counted_and_each_goes_by_its_own_rule(
    dossier_history: ModuleType, dossiers: ModuleType
) -> None:
    plugin, ds = dossier_history, dossiers
    chat = 6108
    await seed(plugin, chat, "mira_demo", [(1, "in", "Can we meet on Wednesday?", 60), (2, "out", "Yes.", 55)])
    target, persona = dossier_dir(ds, chat), ds.persona_dir()
    for path, readable in ((target, v1_contact(fact("f1", "Never call on Sundays.", kind="owner"), conn=BUSINESS_ID,
                                               chat=chat)),
                           (persona, v1_persona(fact("f1", "Never uses emoji.", kind="owner")))):
        put(path, b'{"schema_version": 1, "facts": [')
        ds.migrate_v1(now=NOW)
        put(path, readable)  # written by a 0.14 gateway that was still running
        ds.migrate_v1(now=NOW)
    rel = target.relative_to(ds.dossier_root()).as_posix()

    assert ds.v1_backups() == 4
    assert ds.unreadable_v1_warnings() == [
        f"warning: 1 old dossier file could not be read; it is kept as v1-dossier.json in {rel}.",
        "warning: 1 old dossier file could not be read; it is kept as v1-dossier.json in persona."]

    cycle(ds, FakeLlm())

    for path in (target, persona):  # the readable backup went, as its owner text is in the note
        assert sorted(file.name for file in path.iterdir()) == ["document.json", "v1-dossier.json"]
    assert ds.v1_backups() == 2 and len(ds.unreadable_v1_warnings()) == 2
    result = ds.forget(("contact", {"business_connection_id": BUSINESS_ID, "chat_id": chat}))
    assert result["persona_reset"] is True and not target.exists()
    assert ds._doc.backups(persona) == [] and ds.v1_backups() == 0


@pytest.mark.asyncio
async def test_the_migration_runs_at_cycle_start_and_not_in_register(
    dossier_history: ModuleType, dossiers: ModuleType
) -> None:
    plugin, ds = dossier_history, dossiers
    chat = 6102
    target = dossier_dir(ds, chat)
    put(target, v1_contact(fact("f1", "Never call on Sundays.", kind="owner"), conn=BUSINESS_ID, chat=chat))
    fake = FakeLlm()

    plugin.register(FakeCtx(fake))

    assert (target / "dossier.json").exists() and not (target / "document.json").exists()

    ds.set_enabled(False, via="cli")  # the migration runs before the enabled check
    cycle(ds, fake)

    assert not (target / "dossier.json").exists() and (target / "v1-dossier.json").exists()
    doc = read_dossier(ds, chat)
    assert doc is not None and doc["note"] == "Never call on Sundays." and doc["migrated_from"] == "v1"
    assert fake.calls == []


def test_only_the_status_warning_scans_the_v1_backups(
    dossiers: ModuleType, monkeypatch: pytest.MonkeyPatch, no_network: None
) -> None:
    ds = dossiers
    target = dossier_dir(ds, 6107)
    put(target, b'{"schema_version": 1, "facts": [')
    ds.migrate_v1(now=NOW)
    rel = target.relative_to(ds.dossier_root()).as_posix()

    assert ds.unreadable_v1_warnings() == [
        f"warning: 1 old dossier file could not be read; it is kept as v1-dossier.json in {rel}."]
    scans: list[Path] = []
    monkeypatch.setattr(ds.dossier_migrate, "unreadable_backups", lambda root: scans.append(root) or [])
    assert not any("could not be read" in line for line in ds.load_config().warnings)
    assert scans == []  # load_config runs on every panel screen and cycle, so it never parses the backups


def test_migrate_v1_moves_every_file_once_without_a_model_call(dossiers: ModuleType, no_network: None) -> None:
    ds = dossiers
    root = ds.dossier_root()
    put(root / "chats" / ds.chat_key(BUSINESS_ID, 6103), v1_contact(*MIRA_FACTS, conn=BUSINESS_ID, chat=6103))
    put(root / "persona", v1_persona(fact("f1", "Never uses emoji.", kind="owner")))
    fake = FakeLlm()
    ds.bind_llm(fake)

    first = ds.migrate_v1(now=NOW)

    assert (first["migrated"], first["unreadable"], first["backups"]) == (2, 0, 2)

    def files() -> dict[str, Any]:
        return {path: entry for path, entry in snapshot_tree(root).items() if not Path(path).name.startswith(".")}

    after_first = files()
    ds.migrate_v1(now=NOW + timedelta(hours=1))
    assert files() == after_first
    assert fake.calls == []
