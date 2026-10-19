"""Pure Telegram views of dossiers (issue #17): rich HTML, plain fallback, buttons and callback args.

The contact and persona fixtures are built by the real dossier_support pipeline on in-memory history
(Msg/ChatView, plan_*, make_resolver, merge_batch, owner edits, apply_maintenance, compute_stale and
sources_now), so their shape is exactly what the worker stores. No files, network or Telegram.
"""

from __future__ import annotations

import html as html_lib
import importlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import test_history

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
MERGED_AT = NOW - timedelta(hours=2)
EDITED_AT = NOW - timedelta(hours=1)
CONN = "business-123"
CHAT = 991
OTHER_CHAT = 992
PREFIX_RE = re.compile(r"[0-9a-f]{6}")
ALLOWED = {
    "h3": set(), "h4": set(), "p": set(), "b": set(), "i": set(), "pre": set(), "blockquote": set(),
    "cite": set(), "details": {"open"}, "summary": set(), "ul": set(), "li": set(), "br": set(),
    "footer": set(), "tg-time": {"unix", "format"},
}
VOID = {"br", "hr"}
BIDI_AND_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


# ---------------------------------------------------------------------------
# Module loading and fixture builders
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mods(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    with pytest.MonkeyPatch.context() as mp:
        for name in ("TELESEC_DOSSIER_BACKFILL_DAYS", "TELESEC_DOSSIER_BACKFILL_MESSAGES"):
            mp.delenv(name, raising=False)
        plugin = test_history._load_plugin_module(mp, tmp_path_factory.mktemp("dossier-view"))
        ds = importlib.import_module(f"{plugin.__name__}.dossier_support")
        dv = importlib.import_module(f"{plugin.__name__}.dossier_view")
        cfg = replace(ds.load_config(), backfill_days=90, backfill_messages=500)
    return SimpleNamespace(ds=ds, dv=dv, cfg=cfg)


@pytest.fixture
def dv(mods: SimpleNamespace) -> ModuleType:
    return mods.dv


def msg(ds: ModuleType, mid: int, label: str, text: str, at: datetime, *, chat: int = CHAT,
        deleted: bool = False) -> Any:
    direction = {"contact": "inbound", "owner": "outbound", "transcript": "outbound", "unknown": "unknown"}[label]
    return ds.Msg(key=str(mid), chat_key=ds.chat_key(CONN, chat), business_connection_id=CONN, chat_id=chat,
                  message_id=mid, direction=direction, label=label, text=text, message_at=at, order_at=at,
                  first_seen=at, deleted=deleted, fp=ds.fingerprint(label, text, at))


def chat_view(ds: ModuleType, msgs: list[Any], *, chat: int = CHAT, since: datetime,
              months: set[str] | None = None) -> Any:
    return ds.ChatView(entry={"business_connection_id": CONN, "chat_id": chat}, chat_key=ds.chat_key(CONN, chat),
                       messages={str(m.message_id): m for m in msgs}, replaced=set(),
                       deleted_seen={str(m.message_id) for m in msgs if m.deleted},
                       months=months or {f"{m.order_at:%Y-%m}" for m in msgs}, fold_since=since,
                       last_activity_at=None, signature=[])


def add(section: str, kind: str, text: str, *sources: tuple[str, str], conf: str | None = None) -> dict[str, Any]:
    return {"op": "add", "fact_id": "", "section": section, "kind": kind, "confidence": conf or "high", "text": text,
            "sources": [{"ref": ref, "quote": quote} for ref, quote in sources], "reason": "fixture"}


def merge(mods: SimpleNamespace, kind: str, d: dict[str, Any], views: dict[str, Any], plan: Any,
          items: list[dict[str, Any]], *, chat_key: str | None = None) -> dict[str, Any]:
    ds = mods.ds
    resolver = ds.make_resolver(kind, [], plan.pending, views, chat_key=chat_key, alias_map=plan.alias_map)
    merged, line = ds.merge_batch(d, {"changes": items}, kind=kind, resolver=resolver, batch=plan.pending, plan=plan,
                                  views=views, now=MERGED_AT, cycle_id="c1", cfg=mods.cfg, model="gemini-3.8-flash",
                                  provider="gemini", trigger="worker")
    assert line is not None and not line.get("dropped"), line and line.get("dropped")
    return merged


def owner_edit(ds: ModuleType, d: dict[str, Any], fn: Any, *args: Any) -> None:
    rev = d["revision"] + 1
    fn(d, *args, now=EDITED_AT, rev=rev)
    d["revision"], d["last_revision_at"] = rev, EDITED_AT.isoformat()


CONTACT_MESSAGES = [
    (10, "contact", "Hi! I prefer calls after 6pm on weekdays, never on Sunday.", timedelta(days=10)),
    (11, "owner", "Sure. I will send you the signed contract by Friday.", timedelta(days=10, minutes=-5)),
    (12, "contact", "Did you get my invoice #42 from last week?", timedelta(days=9)),
    (13, "owner", "Agreed: we go with the blue design for the landing page.", timedelta(days=9, minutes=-5)),
    (14, "transcript", "\U0001F399 We discussed the trip to Lisbon in May.", timedelta(days=8)),
    (15, "contact", "Say hi to Maria from me.", timedelta(days=8, minutes=-5)),
    (16, "unknown", "Meeting moved to Tuesday 10:00.", timedelta(days=7)),
    (17, "contact", "My budget is 5k.", timedelta(days=7, minutes=-5)),
    (18, "contact", "Actually my budget is 3k.", timedelta(days=6)),
    (19, "contact", "Let's keep it between us.", timedelta(days=5)),
    (20, "owner", "I'll call you on Monday.", timedelta(days=5, minutes=-5)),
    (21, "contact", "I moved to Berlin last year.", timedelta(days=60)),
]


def build_contact(mods: SimpleNamespace) -> SimpleNamespace:
    """f1..f11 from the model, f12 an owner note, f5 corrected, f10 pinned and then source gone,
    f9 unsupported; now f4 is edited, f2 deleted, f8 gone and f11 older than the fold."""
    ds = mods.ds
    msgs = {mid: msg(ds, mid, label, text, NOW - delta, deleted=mid == 15)
            for mid, label, text, delta in CONTACT_MESSAGES}
    view1 = chat_view(ds, list(msgs.values()), since=NOW - timedelta(days=92))
    ck = view1.chat_key
    plan = ds.plan_contact(None, view1, now=MERGED_AT, cfg=mods.cfg)
    assert plan.qualified and len(plan.pending) == len(msgs)
    entry = {"business_connection_id": CONN, "chat_id": CHAT}
    d = merge(mods, "contact", ds.new_contact_dossier(entry, MERGED_AT), {ck: view1}, plan, [
        add("commitments", "observed", "The owner promised to send the signed contract by Friday.",
            ("m11", "I will send you the signed contract by Friday.")),
        add("open_questions", "inferred", "The contact may be waiting for an answer about invoice #42.",
            ("m12", ""), conf="medium"),
        add("decisions", "observed", "They chose the blue design for the landing page.",
            ("m13", "we go with the blue design")),
        add("contradictions", "observed", "The contact named two different budgets.",
            ("m17", "My budget is 5k."), ("m18", "Actually my budget is 3k.")),
        add("preferences", "observed", "The contact prefers calls after 6pm on weekdays.",
            ("m10", "I prefer calls after 6pm on weekdays")),
        add("shared_facts", "inferred", "A trip to Lisbon in May was discussed.", ("m14", ""), conf="low"),
        add("relationship", "observed", "The contact knows Maria.", ("m15", "Say hi to Maria from me.")),
        add("shared_facts", "observed", "The meeting moved to Tuesday 10:00.",
            ("m16", "Meeting moved to Tuesday 10:00.")),
        add("preferences", "observed", "The contact wants to keep things private.",
            ("m19", "Let's keep it between us.")),
        add("commitments", "observed", "The owner will call on Monday.", ("m20", "I'll call you on Monday.")),
        add("relationship", "observed", "The contact moved to Berlin last year.",
            ("m21", "I moved to Berlin last year.")),
    ], chat_key=ck)
    owner_edit(ds, d, ds.owner_note, "relationship", "Call him Sasha, never Alexander.")
    owner_edit(ds, d, ds.owner_correct, "f5", "The contact prefers calls after 7pm on weekdays.")
    owner_edit(ds, d, ds.owner_pin, "f10")
    view2 = chat_view(ds, [m for mid, m in msgs.items() if mid not in (19, 20)], since=NOW - timedelta(days=92))
    ops = ds.apply_maintenance(d, views={ck: view2}, cycle_id="c2")
    ds._mark_revision(d, ops, d["revision"] + 1)
    d["revision"] += 1
    current = dict(msgs)
    current[17] = msg(ds, 17, "contact", "My budget is 7k now.", msgs[17].message_at)
    current[12] = replace(msgs[12], deleted=True)
    for mid in (16, 19, 20, 21):
        del current[mid]
    view3 = chat_view(ds, list(current.values()), since=NOW - timedelta(days=30),
                      months={"2026-08", "2026-09", "2026-10"})
    views = {ck: view3}
    snap = mods.dv.DossierSnapshot(
        kind="contact", label="Ivan Petrov", dossier=d, now=NOW, provider="gemini", model="gemini-3.8-flash",
        stale=ds.compute_stale(d, views), sources_now={f["id"]: ds.sources_now(d, f, views) for f in d["facts"]},
        business_connection_id=CONN, chat_id=CHAT)
    return SimpleNamespace(d=d, snap=snap, facts={f["id"]: f for f in d["facts"]})


def build_persona(mods: SimpleNamespace) -> SimpleNamespace:
    ds = mods.ds
    one = [msg(ds, 11, "owner", "Sure. I will send you the signed contract by Friday.", NOW - timedelta(days=10)),
           msg(ds, 10, "contact", "Hi!", NOW - timedelta(days=10, minutes=5))]
    two = [msg(ds, 30, "owner", "Привет! Созвонимся завтра?", NOW - timedelta(days=1), chat=OTHER_CHAT),
           msg(ds, 31, "owner", "Спасибо 🙏", NOW - timedelta(hours=20), chat=OTHER_CHAT)]
    since = NOW - timedelta(days=92)
    views = {v.chat_key: v for v in (chat_view(ds, one, since=since), chat_view(ds, two, chat=OTHER_CHAT, since=since))}
    plan = ds.plan_persona(None, views, now=MERGED_AT, cfg=mods.cfg)
    alias = plan.alias_map
    c1, c2 = alias[ds.chat_key(CONN, CHAT)], alias[ds.chat_key(CONN, OTHER_CHAT)]
    d = merge(mods, "persona", ds.new_persona_dossier(MERGED_AT), views, plan, [
        add("tone", "inferred", "The owner writes short, friendly messages.", (f"{c2}:m31", ""), conf="medium"),
        add("greetings", "observed", "The owner greets with a short hello.", (f"{c2}:m30", "Привет!")),
        add("formatting", "observed", "The owner writes full sentences.",
            (f"{c1}:m11", "I will send you the signed contract by Friday.")),
    ])
    owner_edit(ds, d, ds.owner_note, "boundaries", "Never use emoji with clients.")
    snap = mods.dv.DossierSnapshot(
        kind="persona", label="", dossier=d, now=NOW, provider="gemini", model="gemini-3.8-flash",
        stale=ds.compute_stale(d, views), sources_now={f["id"]: ds.sources_now(d, f, views) for f in d["facts"]},
        chat_names={c1: "Ivan Petrov", c2: "Olga & <Co>"})
    return SimpleNamespace(d=d, snap=snap, c1=c1, c2=c2)


@pytest.fixture
def contact(mods: SimpleNamespace) -> SimpleNamespace:
    return build_contact(mods)


@pytest.fixture
def persona(mods: SimpleNamespace) -> SimpleNamespace:
    return build_persona(mods)


def fact(fid: int, section: str, text: str, *, sources: list[dict[str, Any]] | None = None, kind: str = "observed",
         rev: int = 1, **extra: Any) -> dict[str, Any]:
    """A stored fact with exactly the keys merge_batch writes (checked in test_fact_builder_matches_merge_batch)."""
    return {"id": f"f{fid}", "section": section, "kind": kind, "confidence": "low" if kind == "inferred" else None,
            "text": text, "status": "active", "pinned": False, "source_gone": False, "unsupported_cycle": None,
            "first_revision": 1, "last_revision": rev, "owner": None, "sources": sources or [], **extra}


def source(mid: int, quote: str | None, *, day: int = 1) -> dict[str, Any]:
    at = (NOW - timedelta(days=day)).isoformat().replace("+00:00", "Z")
    return {"ref": f"m{mid}", "message_id": mid, "date": at, "first_seen": at, "direction": "inbound",
            "transcript": False, "deleted": False, "quote": quote, "fp": f"{mid:016x}"}


def dossier(mods: SimpleNamespace, facts: list[dict[str, Any]]) -> dict[str, Any]:
    d = mods.ds.new_contact_dossier({"business_connection_id": CONN, "chat_id": CHAT}, MERGED_AT)
    d.update(facts=facts, revision=3, refreshed_at=MERGED_AT.isoformat(), next_fact_id=len(facts) + 1,
             last_refresh={"at": MERGED_AT.isoformat(), "trigger": "worker", "provider": "gemini",
                           "model": "gemini-3.8-flash", "calls": 1},
             coverage={"window_start": (NOW - timedelta(days=90)).isoformat(), "older_history_not_sent": True,
                       "messages_processed": 500, "first_message_at": (NOW - timedelta(days=90)).isoformat(),
                       "last_message_at": NOW.isoformat()})
    return d


def snap_of(mods: SimpleNamespace, d: dict[str, Any] | None, **kw: Any) -> Any:
    kw.setdefault("provider", "gemini")
    kw.setdefault("model", "gemini-3.8-flash")
    kw.setdefault("business_connection_id", CONN)
    kw.setdefault("chat_id", CHAT)
    return mods.dv.DossierSnapshot(kind=kw.pop("kind", "contact"), label=kw.pop("label", "Ivan Petrov"), dossier=d,
                                   now=NOW, **kw)


def huge(mods: SimpleNamespace, n: int = 60, *, sources: int = 5, text_chars: int = 300) -> Any:
    """n facts x ``sources`` sources with 200-char quotes and long current history texts."""
    sections = mods.ds.CONTACT_SECTIONS
    facts, now_src = [], {}
    for i in range(1, n + 1):
        srcs = [source(i * 10 + k, (f"quote {i}.{k} " + "q" * 200)[:200], day=k + 1) for k in range(sources)]
        facts.append(fact(i, sections[i % len(sections)], (f"fact {i} " + "x" * text_chars)[:text_chars],
                          sources=srcs, rev=i))
        now_src[f"f{i}"] = [{**s, "state": "edited" if k % 2 else "present", "text": "history " + "h" * 1000}
                            for k, s in enumerate(srcs)]
    return snap_of(mods, dossier(mods, facts), sources_now=now_src)


# ---------------------------------------------------------------------------
# HTML checks
# ---------------------------------------------------------------------------


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.depth = 0
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED:
            self.errors.append(f"tag {tag}")
            return
        for name, value in attrs:
            if name not in ALLOWED[tag]:
                self.errors.append(f"attr {tag}[{name}]")
            elif tag == "details" and value is not None:
                self.errors.append("details open has a value")
            elif tag == "tg-time" and name == "unix" and not re.fullmatch(r"\d{9,11}", value or ""):
                self.errors.append(f"tg-time unix {value!r}")
            elif tag == "tg-time" and name == "format" and not re.fullmatch(r"r|w?[dD]?[tT]?", value or ""):
                self.errors.append(f"tg-time format {value!r}")
        if tag not in VOID:
            self.stack.append(tag)
            self.depth = max(self.depth, len(self.stack))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in VOID:
            self.errors.append(f"self-closed {tag}")

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID or not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> in {self.stack}")
        else:
            self.stack.pop()


RICH_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "hr", "footer", "ul", "ol", "li", "blockquote",
               "details"}
BLOCK_HOLDERS = {"", "li", "blockquote", "details"}  # RichBlock lists: loose inline runs become paragraphs


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, list[Any]]] = [("", [])]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node: tuple[str, list[Any]] = (tag, [])
        self.stack[-1][1].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1][1].append(("#text", []))


def rich_blocks(html: str) -> int:
    """Independent block count from the Bot API types: every block (list items, quotes and details too)
    plus one paragraph per loose inline run in a block holder; summary is the details' title, not a block."""
    tree = _Tree()
    tree.feed(html)
    tree.close()

    def count(node: tuple[str, list[Any]]) -> int:
        tag, kids = node
        total, run = (1 if tag in RICH_BLOCKS else 0), False
        for kid in kids:
            if kid[0] in RICH_BLOCKS or kid[0] == "summary":
                total, run = total + count(kid), False
            elif tag in BLOCK_HOLDERS and not run:
                total, run = total + 1, True
        return total

    return count(tree.stack[0])


def check_html(dv: ModuleType, html: str) -> None:
    checker = _Checker()
    checker.feed(html)
    checker.close()
    assert not checker.errors, checker.errors[:5]
    assert not checker.stack, f"unclosed {checker.stack}"
    assert checker.depth <= 16
    assert html.count("<summary>") == html.count("<details")
    assert re.search(r"&(?!(lt|gt|amp|quot|#x27|#\d+);)", html) is None, "unsupported entity"
    assert not BIDI_AND_CONTROL.search(html)
    assert len(html.encode("utf-16-le")) // 2 <= dv.BUDGET_CHARS < dv.RICH_MAX_CHARS
    assert dv.count_blocks(html) == rich_blocks(html) <= dv.BUDGET_BLOCKS < dv.RICH_MAX_BLOCKS


def check_view(dv: ModuleType, view: Any) -> None:
    check_html(dv, view.html)
    assert view.text and dv._units(view.text) <= dv.TEXT_MAX_CHARS
    for row in view.buttons:
        assert 1 <= len(row) <= dv.PAGE_COLUMNS
        for label, args in row:
            assert label and not BIDI_AND_CONTROL.search(label)
            assert args.isascii() and len(args.encode()) <= dv.ARGS_MAX_BYTES
            assert dv.parse_args(args) is not None, args
    assert view.notice is None or len(view.notice) <= 200


def summaries(html: str) -> list[tuple[bool, str]]:
    """Section blocks (open, "Title · N"); the nested per-fact "Sources" blocks are left out."""
    found = [(bool(m[1]), html_lib.unescape(m[2]))
             for m in re.finditer(r"<details( open)?><summary>([^<]*)</summary>", html)]
    return [item for item in found if not item[1].startswith(("Sources · ", "Источники · "))]


def fact_ids(html: str) -> list[str]:
    return re.findall(r"<b>(f\d+)</b>", html)


def section_html(html: str, title: str) -> str:
    start = html.index(f"<summary>{title} · ")
    return html[start:html.index("</ul></details>", start)]


def args_of(view: Any) -> list[str]:
    return [args for row in view.buttons for _, args in row]


def labels_of(view: Any) -> list[str]:
    return [label for row in view.buttons for label, _ in row]


# ---------------------------------------------------------------------------
# Fixture shape
# ---------------------------------------------------------------------------


def test_fixture_built_by_real_pipeline(mods: SimpleNamespace, contact: SimpleNamespace) -> None:
    f = contact.facts
    assert [fid for fid in f] == [f"f{i}" for i in range(1, 13)]
    assert f["f5"]["kind"] == "owner" and f["f5"]["owner"]["previous_text"].startswith("The contact prefers calls after 6pm")
    assert mods.ds._is_note(f["f12"]) and f["f12"]["sources"] == []
    assert f["f9"]["status"] == "unsupported" and f["f10"]["source_gone"] and f["f10"]["pinned"]
    assert f["f6"]["sources"][0]["transcript"] and f["f7"]["sources"][0]["deleted"]
    assert f["f8"]["sources"][0]["direction"] == "unknown"
    assert contact.snap.stale == {"f2": [{"ref": "m12", "change": "deleted"}],
                                  "f4": [{"ref": "m17", "change": "edited"}],
                                  "f8": [{"ref": "m16", "change": "gone"}]}
    states = {s["ref"]: s["state"] for items in contact.snap.sources_now.values() for s in items}
    assert states == {"m11": "present", "m12": "present", "m13": "present", "m17": "edited", "m18": "present",
                      "m10": "present", "m14": "present", "m15": "present", "m16": "gone", "m21": "old"}


def test_fact_builder_matches_merge_batch(contact: SimpleNamespace) -> None:
    assert set(fact(1, "decisions", "x")) == set(contact.facts["f1"])
    assert set(source(1, "q")) == set(contact.facts["f1"]["sources"][0])


def test_contact_sections_cover_dossier_sections(mods: SimpleNamespace, dv: ModuleType) -> None:
    assert sorted(dv.CONTACT_ORDER) == sorted(mods.ds.CONTACT_SECTIONS)
    for lang in ("en", "ru"):
        assert set(dv.STRINGS[lang]["sections"]) == set(mods.ds.CONTACT_SECTIONS) | set(mods.ds.PERSONA_SECTIONS)


def test_strings_tables_match_and_are_html_safe(dv: ModuleType) -> None:
    def walk(value: Any, path: str = "") -> dict[str, Any]:
        if isinstance(value, dict):
            return {k: v for key, item in value.items() for k, v in walk(item, f"{path}.{key}").items()}
        return {path: value}

    en, ru = walk(dv.STRINGS["en"]), walk(dv.STRINGS["ru"])
    assert set(en) == set(ru)
    for key, value in en.items():
        if isinstance(value, tuple):  # months, or plural forms: (one, many) in English, (one, few, many) in Russian
            assert len(value) == len(ru[key]) == 12 if key == ".months" else (len(value), len(ru[key])) == (2, 3)
    strings = [v for table in (en, ru) for value in table.values() for v in (value if isinstance(value, tuple)
                                                                             else (value,))]
    for value in strings:
        assert isinstance(value, str) and value
        assert not re.search(r"[<>&\"]", value) and "'" not in value.replace("can't", ""), value
    assert all(re.search(r"[а-яё]", v) for v in walk(dv.STRINGS["ru"]["sections"]).values())


@pytest.mark.parametrize(("code", "lang"), [("ru", "ru"), ("ru-RU", "ru"), ("RU", "ru"), ("en", "en"),
                                            ("uk", "en"), ("", "en"), (None, "en"), ("de-ru", "en")])
def test_lang_for(dv: ModuleType, code: str | None, lang: str) -> None:
    assert dv.lang_for(code) == lang


# ---------------------------------------------------------------------------
# Owner overview
# ---------------------------------------------------------------------------


def test_owner_overview_en(dv: ModuleType, contact: SimpleNamespace) -> None:
    view = dv.render_overview(contact.snap, "en")
    check_view(dv, view)
    html = view.html
    assert html.startswith("<h3>Ivan Petrov</h3><p>Dossier updated "
                           f'<tg-time unix="{int(MERGED_AT.timestamp())}" format="r">2 h ago</tg-time>'
                           " · 19 Oct 2026 · revision 5</p>")
    assert "<p>Coverage: 12 messages, 20 Aug 2026 – 14 Oct 2026<br>Model: gemini / gemini-3.8-flash</p>" in html
    assert "<p>Facts: 11 · stale: 3 · unsupported: 1 · contradictions: 1</p>" in html
    assert summaries(html) == [
        (True, "Commitments · 2"), (True, "Open questions · 1"), (True, "Decisions · 1"),
        (True, "Contradictions · 1"), (False, "Preferences · 1"), (False, "Relationship · 3"),
        (False, "Shared facts · 2"), (False, "Unsupported · 1")]
    assert "<footer>✎ owner edit · 📌 pinned · ~ model guess · ⚠ source changed<br>" \
           "Corrections, notes, history and export: hermes telesec dossier</footer>" in html
    assert "Processing is off" not in html and "failed" not in html
    assert view.buttons == (
        (("Manage facts", "m.c.0"), ("Refresh", "rf.c")), (("My persona", "o.p"), ("Disable", "ds")))
    assert view.text.startswith("Ivan Petrov\nDossier updated 2 h ago · 19 Oct 2026 · revision 5\n")
    assert "\nCommitments · 2\n• 📌 ~low source gone f10 The owner will call on Monday.\n• f1 The owner promised" \
           in view.text
    assert "• ⚠ f4 The contact named two different budgets. (m17, m18)" in view.text


def test_owner_overview_ru(dv: ModuleType, contact: SimpleNamespace) -> None:
    view = dv.render_overview(contact.snap, "ru")
    check_view(dv, view)
    html = view.html
    assert "<p>Досье обновлено <tg-time" in html and ">2 ч назад</tg-time> · 19 окт 2026 · ревизия 5</p>" in html
    assert "Охват: 12 сообщений, 20 авг 2026 – 14 окт 2026<br>Модель: gemini / gemini-3.8-flash" in html
    assert "Фактов: 11 · устарели: 3 · без подтверждения: 1 · противоречий: 1" in html
    assert [title for _, title in summaries(html)] == [
        "Обязательства · 2", "Открытые вопросы · 1", "Решения · 1", "Противоречия · 1", "Предпочтения · 1",
        "Отношения · 3", "Общие факты · 2", "Без подтверждения · 1"]
    assert "<summary>Источники · 1</summary>" in html and "собеседник" in html and "расшифровка" in html
    assert "<i>~средняя</i>" in html and "<i>нет источника</i>" in html
    assert "Изменено, сейчас:</i> My budget is 7k now." in html
    assert "Больше нет в истории" in html and "Старше окна отслеживания" in html
    assert labels_of(view) == ["Управлять фактами", "Обновить", "Моя персона", "Выключить"]
    assert "The contact knows Maria." in html  # fact texts stay as stored


def test_fact_order_owner_pinned_then_stale_then_newest(mods: SimpleNamespace, dv: ModuleType) -> None:
    facts = [fact(1, "decisions", "oldest plain", sources=[source(1, "a")], rev=1),
             fact(2, "decisions", "newest plain", sources=[source(2, "b")], rev=5),
             fact(3, "decisions", "stale one", sources=[source(3, "c")], rev=2),
             fact(4, "decisions", "pinned old", sources=[source(4, "d")], rev=1, pinned=True),
             fact(5, "decisions", "owner note", kind="owner", rev=3, owner={"action": "noted", "at": "x"}),
             fact(6, "decisions", "plain tie", sources=[source(6, "e")], rev=5)]
    snap = snap_of(mods, dossier(mods, facts), stale={"f3": [{"ref": "m3", "change": "edited"}]})
    view = dv.render_overview(snap, "en")
    check_view(dv, view)
    assert fact_ids(section_html(view.html, "Decisions")) == ["f5", "f4", "f3", "f2", "f6", "f1"]
    picker = dv.render_fact_picker(snap, "en")
    assert [a.split(".")[-1] for a in args_of(picker) if a.startswith("f.")] == ["5", "4", "3", "2", "6", "1"]


def test_markers_and_sources(dv: ModuleType, contact: SimpleNamespace) -> None:
    html = dv.render_overview(contact.snap, "en").html
    li = {m[1]: m[0] for m in re.finditer(r"<li>(?:(?!<li>).)*?<b>(f\d+)</b>.*?</li>", html)}
    assert li["f5"].startswith("<li>✎ <b>f5</b> The contact prefers calls after 7pm")
    # Maintenance downgraded f10 (observed, its only quoted source gone) to a low-confidence guess.
    assert li["f10"].startswith("<li>📌 <i>~low</i> <i>source gone</i> <b>f10</b>")
    assert li["f2"].startswith("<li><i>~medium</i> ⚠ <b>f2</b>")
    assert li["f6"].startswith("<li><i>~low</i> <b>f6</b>")
    assert li["f4"].startswith("<li>⚠ <b>f4</b>") and li["f8"].startswith("<li>⚠ <b>f8</b>")
    assert li["f12"].startswith("<li>✎ <b>f12</b> Call him Sasha, never Alexander.</li>")
    assert li["f1"].startswith("<li><b>f1</b> ")
    assert "<blockquote>My budget is 5k.<cite>m17 · 12 Oct 2026 · contact · ⚠ edited</cite></blockquote>" \
           "<p><i>Edited, now:</i> My budget is 7k now.</p>" in html
    assert "<p>m12 · 10 Oct 2026 · contact · deleted · ⚠ deleted</p>" \
           "<p><i>Now in history:</i> Did you get my invoice #42 from last week?</p>" in html
    assert "<blockquote>Say hi to Maria from me.<cite>m15 · 11 Oct 2026 · contact · deleted</cite></blockquote>" in html
    assert "<p>m14 · 11 Oct 2026 · transcript</p>" in html
    assert "<cite>m16 · 12 Oct 2026 · unknown · ⚠ gone</cite></blockquote><p><i>No longer in history</i></p>" in html
    assert "<cite>m21 · 20 Aug 2026 · contact</cite></blockquote>" \
           "<p><i>Older than the tracking window, not re-read</i></p>" in html
    # A present message whose text equals the quote is not repeated.
    assert "Now in history:</i> Say hi to Maria" not in html


def test_stale_error_failure_and_busy_notes(dv: ModuleType, contact: SimpleNamespace) -> None:
    snap = replace(contact.snap, failure_error="TimeoutError", stale_error="OSError", cycle_busy=True)
    html = dv.render_overview(snap, "en").html
    assert ("<p><i>A processing cycle is running; the dossier may change shortly.<br>The last refresh failed: "
            "TimeoutError.<br>Could not check sources against history: OSError.</i></p>") in html
    refreshing = dv.render_overview(replace(contact.snap, refreshing=True), "ru")
    assert "Идёт обновление…" in refreshing.html and "rf.c" not in args_of(refreshing)


def test_disabled_with_stored_dossier(dv: ModuleType, contact: SimpleNamespace) -> None:
    view = dv.render_overview(replace(contact.snap, enabled=False), "en")
    check_view(dv, view)
    assert "<p><i>Processing is off: this dossier is not updated.</i></p>" in view.html
    assert "<b>f1</b>" in view.html
    assert view.buttons == ((("Manage facts", "m.c.0"),), (("My persona", "o.p"), ("Enable", "ea")))


def test_enabled_without_llm_has_no_refresh(dv: ModuleType, contact: SimpleNamespace) -> None:
    view = dv.render_overview(replace(contact.snap, llm_ready=False), "en")
    assert "The model is not connected in this process" in view.html
    assert "rf.c" not in args_of(view)


# ---------------------------------------------------------------------------
# Contact audience (/dossier) and persona
# ---------------------------------------------------------------------------


def test_contact_audience_excludes_notes_buttons_and_owner_lines(dv: ModuleType, contact: SimpleNamespace) -> None:
    snap = replace(contact.snap, failure_error="TimeoutError", refreshing=True)
    view = dv.render_overview(snap, "en", audience="contact")
    check_view(dv, view)
    assert view.buttons == () and view.notice is None
    assert "Call him Sasha" not in view.html and "f12" not in view.html and "Call him Sasha" not in view.text
    assert "The contact prefers calls after 7pm on weekdays." in view.html  # corrected facts stay
    for owner_only in ("TimeoutError", "Refreshing", "hermes telesec", "Processing is off"):
        assert owner_only not in view.html and owner_only not in view.text
    assert "<footer>✎ owner edit · 📌 pinned · ~ model guess</footer>" in view.html
    # f12 note, f2 and f7 deleted-only, f8 gone from history, f9 unsupported, f10 without a source left
    assert summaries(view.html) == [(True, "Commitments · 1"), (True, "Decisions · 1"), (True, "Contradictions · 1"),
                                    (False, "Preferences · 1"), (False, "Relationship · 1"),
                                    (False, "Shared facts · 1")]
    assert "<p>Facts: 6 · contradictions: 1</p>" in view.html
    unchecked = replace(snap, stale_error="OSError")  # history could not be read: nothing is posted
    assert dv.post_blocker(unchecked) == "unchecked"
    with pytest.raises(ValueError):
        dv.render_overview(unchecked, "en", audience="contact")
    reason = dv.render_post_reason(unchecked, "en")
    assert "could not be checked against the chat history (OSError)" in reason.text
    assert "7pm" not in reason.html
    assert "(OSError)" in dv.render_post_reason(unchecked, "ru").text


def test_contact_audience_hides_deleted_and_edited_source_text(dv: ModuleType, contact: SimpleNamespace) -> None:
    owner = dv.render_overview(contact.snap, "en")
    for gone in ("Say hi to Maria from me.", "Did you get my invoice #42", "My budget is 5k.", "Now in history",
                 "Edited, now", "· deleted"):
        assert gone in owner.html  # the owner still sees the archive
    assert "⚠ <b>f4</b>" in owner.html and "stale: 3" in owner.html and "source gone" in owner.html
    for lang, labels, day, side in (
            ("en", ("Now in history", "Edited, now", "deleted", "edited", "gone", "stale", "source gone",
                    "source changed", "Unsupported"), "12 Oct 2026", "contact"),
            ("ru", ("Сейчас в истории", "Изменено, сейчас", "удалено", "изменено", "пропало", "устарели",
                    "нет источника", "источник изменился"), "12 окт 2026", "собеседник")):
        view = dv.render_overview(contact.snap, lang, audience="contact")
        check_view(dv, view)
        for gone in ("Say hi to Maria", "The contact knows Maria.", "invoice #42", "My budget is 5k.",
                     "My budget is 7k now.", "Meeting moved to Tuesday 10:00.", "keep things private",
                     "call on Monday", "The meeting moved", "<b>f2</b>", "<b>f7</b>", "<b>f8</b>", "⚠", *labels):
            assert gone not in view.html and gone not in view.text, (lang, gone)
        assert f"<p>m17 · {day} · {side}</p>" in view.html  # edited: the reference without its old wording
        assert "<blockquote>we go with the blue design<cite>m13 · " in view.html
        assert "The contact named two different budgets." in view.html
    assert dv.contact_fact_count(contact.snap) == 6
    gone = replace(contact.snap, sources_now={k: [{**n, "state": "gone"} for n in v]
                                              for k, v in contact.snap.sources_now.items()})
    assert dv.post_blocker(gone) == "empty"  # e.g. the chat's history was removed


def test_contact_audience_keeps_a_fact_but_not_its_deleted_or_replaced_sources(mods: SimpleNamespace,
                                                                               dv: ModuleType) -> None:
    deleted = {**source(5, "secret deleted words"), "deleted": True}
    d = dossier(mods, [fact(1, "relationship", "The contact has a dog.", sources=[deleted, source(6, "my dog Rex")]),
                       fact(2, "relationship", "The contact lives on Oak Street.", sources=[source(7, "12 Oak St")]),
                       fact(3, "relationship", "Not checked yet.", sources=[source(8, "unchecked words")])])
    now = {"f1": [{"ref": "m5", "state": "present", "deleted": True, "text": "secret deleted words"},
                  {"ref": "m6", "state": "present", "deleted": False, "text": "my dog Rex"}],
           "f2": [{"ref": "m7", "state": "replaced", "deleted": False}]}  # deleted and resent: a correction
    snap = snap_of(mods, d, sources_now=now)
    owner = dv.render_overview(snap, "en").html
    assert "secret deleted words" in owner and "· deleted" in owner and "Oak Street" in owner
    for lang in ("en", "ru"):
        view = dv.render_overview(snap, lang, audience="contact")
        check_view(dv, view)
        assert "The contact has a dog." in view.html and "my dog Rex" in view.html
        assert "<p>m5 · " in view.html and "<p>m8 · " in view.html  # f3 has no sources_now: no quote
        for gone in ("secret deleted words", "deleted", "удалено", "Oak", "unchecked words"):
            assert gone not in view.html and gone not in view.text, (lang, gone)
    assert dv.contact_fact_count(snap) == 2


def test_contact_audience_excludes_corrected_note(mods: SimpleNamespace, dv: ModuleType) -> None:
    c = build_contact(mods)
    owner_edit(mods.ds, c.d, mods.ds.owner_correct, "f12", "Never mention the 20% discount.")
    note = next(f for f in c.d["facts"] if f["id"] == "f12")
    assert note["owner"]["action"] == "corrected" and note["owner"]["noted"] is True
    snap = replace(c.snap, dossier=c.d)
    view = dv.render_overview(snap, "en", audience="contact")
    check_view(dv, view)
    for hidden in ("20% discount", "Call him Sasha", "<b>f12</b>"):
        assert hidden not in view.html and hidden not in view.text
    owner = dv.render_overview(snap, "en")
    assert "Never mention the 20% discount." in owner.html and "Owner note" in dv.render_fact_card(snap, "en", "f12").html


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_off_overview_tells_a_non_operator_who_can_turn_dossiers_on(dv: ModuleType, contact: SimpleNamespace,
                                                                    lang: str) -> None:
    snap = replace(contact.snap, enabled=False, manage_global=False)
    view = dv.render_overview(snap, lang)
    check_view(dv, view)
    notice = dv.STRINGS[lang]["notice"]["operator_only"]
    assert notice in view.html and dv.STRINGS[lang]["off_cli"] in view.html
    assert "ds" not in args_of(view) and "ea" not in args_of(view)
    assert notice not in dv.render_overview(replace(snap, manage_global=True), lang).html
    assert notice not in dv.render_overview(replace(snap, enabled=True), lang).html


def test_contact_audience_refuses_blocked_and_persona(dv: ModuleType, contact: SimpleNamespace,
                                                      persona: SimpleNamespace) -> None:
    for snap in (persona.snap, replace(contact.snap, enabled=False), replace(contact.snap, dossier=None, status="missing"),
                 replace(contact.snap, dossier=None, status="corrupt")):
        with pytest.raises(ValueError):
            dv.render_overview(snap, "en", audience="contact")
    with pytest.raises(ValueError):
        dv.render_overview(contact.snap, "en", audience="everyone")


def test_post_blocker_and_reasons(mods: SimpleNamespace, dv: ModuleType, contact: SimpleNamespace,
                                  persona: SimpleNamespace) -> None:
    notes_only = dossier(mods, [fact(1, "relationship", "secret", kind="owner", owner={"action": "noted"})])
    corrected_note = dossier(mods, [fact(1, "relationship", "secret corrected", kind="owner",
                                         owner={"action": "corrected", "noted": True, "previous_text": "secret"})])
    deleted_only = dossier(mods, [fact(1, "relationship", "from a deleted message",
                                       sources=[{**source(5, "deleted words"), "deleted": True}])])
    cases = {
        None: contact.snap,
        "persona": persona.snap,
        "disabled": replace(contact.snap, enabled=False),
        "corrupt": replace(contact.snap, dossier=None, status="corrupt"),
        "missing": replace(contact.snap, dossier=None, status="missing", missing_reason="unqualified"),
        "empty": snap_of(mods, notes_only),
    }
    for reason, snap in cases.items():
        assert dv.post_blocker(snap) == reason
    for d in (corrected_note, deleted_only):
        assert dv.post_blocker(snap_of(mods, d)) == "empty" and dv.contact_fact_count(snap_of(mods, d)) == 0
        reason_html = dv.render_post_reason(snap_of(mods, d), "en").html
        assert "secret" not in reason_html and "deleted" not in reason_html
    assert dv.render_post_reason(cases["disabled"], "en").text == \
        "Nothing was posted to the chat with Ivan Petrov.\nDossiers are off."
    assert dv.render_post_reason(cases["missing"], "ru").text == (
        "В чат «Ivan Petrov» ничего не отправлено.\nДосье пока нет.\nНужно хотя бы одно полученное и одно "
        "отправленное текстовое сообщение за последние 90 дней.")
    corrupt = dv.render_post_reason(cases["corrupt"], "en")
    assert "hermes telesec dossier forget --connection business-123 --chat 991" in corrupt.text
    assert "secret" not in dv.render_post_reason(cases["empty"], "en").html
    failed = dv.render_post_reason(contact.snap, "en", "send_failed", error="BadRequest")
    assert failed.text.endswith("Telegram did not accept the dossier message (BadRequest). A bot can post in a "
                                "Business chat only if the contact wrote there in the last 24 hours.")
    assert "за последние 24 часа" in dv.render_post_reason(contact.snap, "ru", "send_failed", error="BadRequest").text
    with pytest.raises(ValueError):
        dv.render_post_reason(contact.snap, "en")
    for snap in cases.values():
        if dv.post_blocker(snap):
            check_view(dv, dv.render_post_reason(snap, "ru"))


def test_persona_overview(dv: ModuleType, persona: SimpleNamespace) -> None:
    view = dv.render_overview(persona.snap, "en")
    check_view(dv, view)
    html = view.html
    assert html.startswith("<h3>My persona</h3><p>Persona updated <tg-time")
    assert "Coverage: 3 messages in 2 chats, 9 Oct 2026 – 18 Oct 2026" in html
    assert "contradictions" not in html.split("<details")[0]
    assert summaries(html) == [(False, "Tone · 1"), (False, "Formatting · 1"), (False, "Greetings · 1"),
                               (False, "Boundaries · 1")]
    assert f"<blockquote>Привет!<cite>{persona.c2}:m30 · Olga &amp; &lt;Co&gt; · 18 Oct 2026</cite></blockquote>" in html
    assert f"<cite>{persona.c1}:m11 · Ivan Petrov · 9 Oct 2026</cite>" in html
    assert view.buttons == ((("Manage facts", "m.p.0"),), (("‹ Dossier", "o.c"),))
    ru = dv.render_overview(persona.snap, "ru")
    assert ru.html.startswith("<h3>Моя персона</h3><p>Персона обновлена ")
    assert "Охват: 3 сообщения в 2 чатах" in ru.html
    assert [t for _, t in summaries(ru.html)] == ["Тон · 1", "Оформление · 1", "Приветствия · 1", "Границы · 1"]


def test_persona_fact_card_and_actions(dv: ModuleType, persona: SimpleNamespace) -> None:
    prefix = dv.dossier_prefix(persona.d)
    card = dv.render_fact_card(persona.snap, "en", "f2", prefix=prefix)
    check_view(dv, card)
    assert f"<b>{persona.c2}:m30</b> · Olga &amp; &lt;Co&gt; · <tg-time" in card.html
    assert args_of(card) == [f"pn.p.{prefix}.2", f"ra.p.{prefix}.2", "m.p.0"]
    picker = dv.render_fact_picker(persona.snap, "en")
    assert all(a.startswith(("f.p.", "o.p")) for a in args_of(picker))
    assert picker.buttons[-1] == (("‹ My persona", "o.p"),)  # back to the persona, not the contact dossier
    assert dv.render_fact_picker(persona.snap, "ru").buttons[-1] == (("‹ Моя персона", "o.p"),)


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


def test_disabled_and_nothing_stored(dv: ModuleType, mods: SimpleNamespace) -> None:
    snap = snap_of(mods, None, status="missing", enabled=False)
    view = dv.render_overview(snap, "en")
    check_view(dv, view)
    assert "<p><b>Dossiers are off.</b></p>" in view.html
    assert "sends the text of all private chats to <b>gemini / gemini-3.8-flash</b>." in view.html
    assert view.buttons == ((("My persona", "o.p"), ("Enable", "ea")),)
    ru = dv.render_overview(snap, "ru")
    assert "Досье выключены." in ru.html and "текст всех личных чатов отправляется в <b>gemini" in ru.html
    assert "Only the owner" not in view.html

    other = replace(snap, manage_global=False)  # no Enable button: say who can turn them on, and how
    view = dv.render_overview(other, "en")
    check_view(dv, view)
    assert view.buttons == ()
    assert ("<p>Only the owner of every Business connection the bot knows about can open the persona or turn "
            "dossiers on and off.</p><p>From the terminal: hermes telesec dossier enable</p>") in view.html
    assert "Из терминала: hermes telesec dossier enable" in dv.render_overview(other, "ru").html


@pytest.mark.parametrize(("reason", "error", "en", "ru", "refresh"), [
    ("unqualified", None, "It needs at least one received and one sent text message in the last 90 days.",
     "за последние 90 дней.", False),
    ("pending", None, "It will be built in the next processing cycle.", "Оно появится в следующем цикле", True),
    ("failed", "TimeoutError", "The last attempt failed: TimeoutError.", "Последняя попытка не удалась: TimeoutError.",
     True),
])
def test_missing_reasons(dv: ModuleType, mods: SimpleNamespace, reason: str, error: str | None, en: str, ru: str,
                         refresh: bool) -> None:
    snap = snap_of(mods, None, status="missing", missing_reason=reason, failure_error=error)
    view = dv.render_overview(snap, "en")
    check_view(dv, view)
    assert "<p><b>No dossier yet.</b></p>" in view.html and en in view.html
    assert view.html.count("TimeoutError") == (1 if error else 0)
    assert ("rf.c" in args_of(view)) is refresh and "m.c.0" not in args_of(view)
    assert ru in dv.render_overview(snap, "ru").html
    assert "rf.c" not in args_of(dv.render_overview(replace(snap, in_history=False), "en"))  # nothing to refresh from


def test_corrupt_state(dv: ModuleType, mods: SimpleNamespace) -> None:
    snap = snap_of(mods, None, status="corrupt", business_connection_id="conn with 'quote' <x>", chat_id=-100)
    view = dv.render_overview(snap, "en")
    check_view(dv, view)
    assert "<b>The stored dossier file can't be read.</b>" in view.html
    assert "<pre>hermes telesec dossier forget --connection " \
           "&#x27;conn with &#x27;&quot;&#x27;&quot;&#x27;quote&#x27;&quot;&#x27;&quot;&#x27; &lt;x&gt;&#x27; --chat -100</pre>" \
           in view.html
    assert "forget --connection 'conn with '\"'\"'quote'\"'\"' <x>' --chat -100" in view.text
    assert "rf.c" not in args_of(view)
    persona = dv.render_overview(snap_of(mods, None, kind="persona", status="corrupt"), "ru")
    assert "Файл персоны не читается." in persona.html and "forget --persona" in persona.html


def test_persona_missing(dv: ModuleType, mods: SimpleNamespace) -> None:
    view = dv.render_overview(snap_of(mods, None, kind="persona", status="missing", missing_reason="pending"), "en")
    assert "<p><b>No persona yet.</b></p>" in view.html and "sent messages across private chats" in view.html
    assert view.buttons == ((("‹ Dossier", "o.c"),),)


@pytest.mark.parametrize("key", ["changed", "busy", "refresh_started", "unavailable", "enabled", "disabled",
                                 "pinned", "unpinned", "removed", "failed"])
def test_states_and_notices(dv: ModuleType, key: str) -> None:
    for lang in ("en", "ru"):
        view = dv.render_state(key, lang, "persona", fact_id="f3", error="BadRequest")
        check_view(dv, view)
        assert view.notice == dv.notice_text(key, lang, fact_id="f3", error="BadRequest")
        assert view.buttons == ((("Open dossier" if lang == "en" else "Открыть досье", "o.p"),),)
    assert dv.notice_text("pinned", "ru", fact_id="f3") == "f3 закреплён."
    with pytest.raises(ValueError):
        dv.notice_text("nope", "en")


def test_with_notice_shows_a_notice_once(dv: ModuleType, contact: SimpleNamespace) -> None:
    for key in dv.NOTICE_KEYS:
        for lang in ("en", "ru"):
            state = dv.render_state(key, lang, fact_id="f3", error="X")
            assert dv.with_notice(state) == state  # the screen already is the notice
    plain = dv.render_overview(contact.snap, "en")
    assert dv.with_notice(plain) == plain
    noted = replace(plain, notice="Pinned f3.")
    shown = dv.with_notice(noted)
    check_view(dv, shown)
    assert shown.html == f"<p><b>Pinned f3.</b></p>{plain.html}" and shown.text.startswith("Pinned f3.\n\nIvan Petrov")
    assert shown.buttons == plain.buttons and shown.notice == "Pinned f3."


# ---------------------------------------------------------------------------
# Picker, card, confirmations
# ---------------------------------------------------------------------------


def test_fact_picker_paging(mods: SimpleNamespace, dv: ModuleType) -> None:
    snap = huge(mods, 60, sources=1)
    prefix = dv.dossier_prefix(snap.dossier)
    first = dv.render_fact_picker(snap, "en", 0)
    check_view(dv, first)
    assert first.html.count("<li>") == 24 and "<p>Facts, page 1 of 3</p>" in first.html
    assert [len(row) for row in first.buttons] == [4, 4, 4, 4, 4, 4, 1, 1]
    assert first.buttons[-2] == (("2 ›", "m.c.1"),) and first.buttons[-1] == (("‹ Dossier", "o.c"),)
    action = dv.parse_args(first.buttons[0][0][1])
    assert action.name == "fact" and action.prefix == prefix and action.fact_id == first.buttons[0][0][0]
    middle = dv.render_fact_picker(snap, "ru", 1)
    assert middle.buttons[-2] == (("‹ 1", "m.c.0"), ("3 ›", "m.c.2")) and "страница 2 из 3" in middle.html
    last = dv.render_fact_picker(snap, "en", 99)
    assert "page 3 of 3" in last.html and last.html.count("<li>") == 12
    assert [len(row) for row in last.buttons] == [4, 4, 4, 1, 1] and last.buttons[-2] == (("‹ 2", "m.c.1"),)
    assert "page 1 of 3" in dv.render_fact_picker(snap, "en", -5).html
    empty = dv.render_fact_picker(snap_of(mods, dossier(mods, [])), "en")
    assert "No facts yet." in empty.html and empty.buttons == ((("‹ Dossier", "o.c"),),)


def test_fact_card(dv: ModuleType, contact: SimpleNamespace) -> None:
    prefix = dv.dossier_prefix(contact.d)
    card = dv.render_fact_card(contact.snap, "en", "f4", prefix=prefix)
    check_view(dv, card)
    html = card.html
    assert html.startswith("<h3>Ivan Petrov · f4</h3><p>⚠ Contradictions · Observed in messages</p>"
                           "<p><b>The contact named two different budgets.</b></p>"
                           "<p>Sources changed since the last refresh: m17 edited.<br>Revisions 1–1</p><h4>Sources · 2</h4>")
    unix = int(datetime(2026, 10, 12, 12, 5, tzinfo=UTC).timestamp())
    assert (f'<p><b>m17</b> · <tg-time unix="{unix}" format="dt">12 Oct 2026, 12:05 UTC</tg-time> · contact · ⚠ edited'
            "</p><blockquote>My budget is 5k.</blockquote><p><i>Edited, now:</i> My budget is 7k now.</p>") in html
    assert card.buttons == ((("Pin", f"pn.c.{prefix}.4"), ("Remove", f"ra.c.{prefix}.4")), (("‹ Facts", "m.c.0"),))
    corrected = dv.render_fact_card(contact.snap, "ru", "f5")
    assert "Исправлено владельцем" in corrected.html
    assert "<p><i>До исправления:</i> The contact prefers calls after 6pm on weekdays.</p>" in corrected.html
    pinned = dv.render_fact_card(contact.snap, "en", "10")
    assert "Pinned: the model never changes it.<br>Its sources are gone from history." in pinned.html
    assert "<p>No sources.</p>" in pinned.html and pinned.buttons[0][0] == ("Unpin", f"up.c.{prefix}.10")
    old = dv.render_fact_card(contact.snap, "en", "f11").html
    assert "Older than the tracking window, not re-read" in old
    gone = dv.render_fact_card(contact.snap, "en", "f8").html
    assert "⚠ gone" in gone and "No longer in history" in gone
    unsupported = dv.render_fact_card(contact.snap, "en", "f9").html
    assert "Unsupported: removed on the next refresh" in unsupported
    note = dv.render_fact_card(contact.snap, "en", "f12")
    assert "Owner note" in note.html and "Revisions 2–2" in note.html
    inferred = dv.render_fact_card(contact.snap, "ru", "f2").html
    assert "Догадка модели, уверенность средняя" in inferred and "удалено · ⚠ удалено" in inferred
    for fid in contact.facts:
        check_view(dv, dv.render_fact_card(contact.snap, "en", fid))
        check_view(dv, dv.render_fact_card(contact.snap, "ru", fid))


def test_fact_card_back_returns_to_its_page(mods: SimpleNamespace, dv: ModuleType) -> None:
    snap = huge(mods, 60, sources=1)
    picker = dv.render_fact_picker(snap, "en", 2)
    fid = picker.buttons[0][0][0]
    card = dv.render_fact_card(snap, "en", fid)
    assert card.buttons[-1] == (("‹ Facts", "m.c.2"),)


def test_changed_dossier_is_never_acted_on(dv: ModuleType, contact: SimpleNamespace) -> None:
    for render in (dv.render_fact_card, dv.render_remove_confirm):
        view = render(contact.snap, "en", "f4", prefix="000000")
        assert view.notice == "This dossier changed. Open it again." and view.buttons[0][0][1] == "o.c"
        assert all(not a.startswith(("pn.", "up.", "rd.", "ra.")) for a in args_of(view))
        assert render(contact.snap, "en", "f99").notice == "This dossier changed. Open it again."


def test_actions_on_missing_dossier_show_the_state(dv: ModuleType, mods: SimpleNamespace) -> None:
    snap = snap_of(mods, None, status="missing", missing_reason="pending")
    for view in (dv.render_fact_card(snap, "en", "f1", prefix="abcdef"), dv.render_fact_picker(snap, "en"),
                 dv.render_remove_confirm(snap, "en", "f1")):
        assert "No dossier yet." in view.html


def test_remove_confirm(dv: ModuleType, contact: SimpleNamespace) -> None:
    prefix = dv.dossier_prefix(contact.d)
    view = dv.render_remove_confirm(contact.snap, "en", "f3", prefix=prefix)
    check_view(dv, view)
    assert view.html == ("<h3>Remove f3?</h3><p><b>f3</b> They chose the blue design for the landing page.</p>"
                         "<p>The model will not add this fact again. This can't be undone in Telegram.</p>")
    assert view.buttons == ((("Remove", f"rd.c.{prefix}.3"), ("Cancel", f"f.c.{prefix}.3")),)
    ru = dv.render_remove_confirm(contact.snap, "ru", "f3")
    assert "Удалить f3?" in ru.html and labels_of(ru) == ["Да, удалить", "Отмена"]


def test_enable_confirm(dv: ModuleType) -> None:
    view = dv.render_enable_confirm("en", "gemini", "gemini-3.8-flash")
    check_view(dv, view)
    assert view.html == (
        "<h3>Turn on dossiers?</h3><p>The text of ALL private chats will be sent to <b>gemini / gemini-3.8-flash</b>"
        " to build a dossier for each contact and your persona.</p><p>Dossiers are stored locally. You can turn "
        "them off at any time; stored dossiers are kept.</p>")
    assert view.buttons == ((("Enable", "ed"), ("Cancel", "o.c")),)
    ru = dv.render_enable_confirm("ru", "openrouter", "<evil> & \u202emodel")
    check_view(dv, ru)
    assert "Текст ВСЕХ личных чатов будет отправляться в <b>openrouter / &lt;evil&gt; &amp; model</b>" in ru.html
    assert labels_of(ru) == ["Включить", "Отмена"]


# ---------------------------------------------------------------------------
# Size budget
# ---------------------------------------------------------------------------


def test_budget_first_drops_only_current_history_text(mods: SimpleNamespace, dv: ModuleType) -> None:
    snap = huge(mods, 16, sources=4, text_chars=100)
    view = dv.render_overview(snap, "en")
    check_view(dv, view)
    assert "history hhh" not in view.html and "Now in history" not in view.html and "Edited, now" not in view.html
    assert len(re.findall(r"<summary>Sources · 4</summary>", view.html)) == 16  # every fact keeps its sources
    small = huge(mods, 3, sources=2, text_chars=100)
    assert "<p><i>Edited, now:</i> history hhh" in dv.render_overview(small, "en").html  # under budget: all kept


def test_budget_drops_history_text_then_old_source_details(mods: SimpleNamespace, dv: ModuleType) -> None:
    snap = huge(mods, 60)
    full = "".join(dv._fact_li(dv._cx(snap, "en"), f, set(), 0) for f in snap.dossier["facts"])
    assert dv.count_blocks(full) > dv.RICH_MAX_BLOCKS and len(full) > dv.RICH_MAX_CHARS  # it really is too big
    for lang in ("en", "ru"):
        view = dv.render_overview(snap, lang)
        check_view(dv, view)
        assert "history hhh" not in view.html  # 1. current history text went first
        details = re.findall(r"<b>(f\d+)</b>[^<]*<details>", view.html)
        assert details, "newer facts keep their source details"
        kept = {int(fid[1:]) for fid in details}
        dropped = {int(fid[1:]) for fid in fact_ids(view.html)} - kept
        assert dropped and max(dropped) < min(kept)  # 2. the oldest facts lost their details first
        assert "more fact" not in view.html and "ещё" not in view.html
        assert len(fact_ids(view.html)) == 60
        assert dv._units(view.text) <= dv.TEXT_MAX_CHARS and view.text.endswith("…")


def test_budget_truncates_with_more_line(mods: SimpleNamespace, dv: ModuleType) -> None:
    facts = [fact(i, "shared_facts" if i > 5 else "commitments", f"{i} " + "y" * 498, kind="owner",
                  owner={"action": "noted", "at": "x"}, rev=i) for i in range(1, 401)]
    snap = snap_of(mods, dossier(mods, facts))
    view = dv.render_overview(snap, "en")
    check_view(dv, view)
    shown = len(fact_ids(view.html))
    assert 0 < shown < 400
    assert f"<p><i>… {400 - shown} more facts, see hermes telesec dossier show</i></p><footer>" in view.html
    assert summaries(view.html)[0] == (True, "Commitments · 5")
    assert "Shared facts · 395" in view.html  # totals stay true, the line says what is missing
    ru = dv.render_overview(snap, "ru")
    check_view(dv, ru)
    hidden = 400 - len(fact_ids(ru.html))
    word = dv._word("ru", hidden, ("факт", "факта", "фактов"))
    assert f"<p><i>… ещё {hidden} {word} — полностью: hermes telesec dossier show</i></p>" in ru.html


def test_budget_contact_audience_more_line_has_no_cli(mods: SimpleNamespace, dv: ModuleType) -> None:
    facts = [fact(i, "decisions", f"{i} " + "z" * 298, sources=[source(i, "z" * 200)], rev=i) for i in range(1, 400)]
    view = dv.render_overview(snap_of(mods, dossier(mods, facts)), "en", audience="contact")
    check_view(dv, view)
    assert re.search(r"<p><i>… \d+ more facts</i></p><footer>", view.html)
    assert "hermes" not in view.html


def test_block_counter(dv: ModuleType) -> None:
    assert dv.count_blocks("<p>a</p><ul><li>x</li><li>y</li></ul>") == 6  # each item's text is a paragraph
    assert dv.count_blocks("<li>text<details><summary>S</summary><p>x</p></details></li>") == 4
    assert dv.count_blocks("<blockquote>q<cite>c</cite></blockquote>") == 2
    assert dv.count_blocks("<details><summary>S</summary>loose</details>") == 2
    assert dv.count_blocks("loose <b>root</b> text<hr/>") == 2
    assert dv.count_blocks("<footer>a<br>b</footer>") == 1


def test_html_to_text(dv: ModuleType) -> None:
    html = ("<h3>A &amp; B</h3><details open><summary>S · 1</summary><ul><li>x<details><summary>Src</summary>"
            "<blockquote>q<cite>m1</cite></blockquote></details></li></ul></details><footer>f</footer>")
    assert dv.html_to_text(html) == "A & B\n\nS · 1\n• x\nSrc\n> q\n> — m1\nf"
    assert dv.html_to_text("<p>" + "w" * 5000 + "</p>") == "w" * 4095 + "…"
    clipped = dv.html_to_text("<p>" + "😀" * 3000 + "</p>")  # 2 UTF-16 units each
    assert clipped == "😀" * 2047 + "…" and dv._units(clipped) == 4095


def test_rich_message_payload(dv: ModuleType, contact: SimpleNamespace) -> None:
    from telegram.request import RequestData
    from telegram.request._requestparameter import RequestParameter

    view = dv.render_overview(contact.snap, "ru")
    assert dv.rich_message(view) == {"html": view.html, "skip_entity_detection": True}
    # bot.do_api_request(..., api_kwargs={"rich_message": ...}) JSON-encodes the nested object as one field.
    data = RequestData([RequestParameter.from_input("rich_message", dv.rich_message(view)),
                        RequestParameter.from_input("chat_id", OTHER_CHAT)])
    assert json.loads(data.json_parameters["rich_message"]) == {"html": view.html, "skip_entity_detection": True}
    assert data.json_parameters["chat_id"] == str(OTHER_CHAT)


# ---------------------------------------------------------------------------
# Callback args
# ---------------------------------------------------------------------------


def all_actions(dv: ModuleType) -> list[Any]:
    out = []
    for name in dv.ACTION_NAMES:
        for target in dv.TARGETS:
            if name in ("enable_ask", "enable_do", "disable"):
                if target == "contact":
                    out.append(dv.Action(name))
            elif name == "manage":
                out += [dv.Action(name, target, page=p) for p in (0, 9, 10, 99)]
            elif name in ("fact", "pin", "unpin", "remove_ask", "remove_do"):
                out += [dv.Action(name, target, prefix=p, fact_id=f) for p in ("abcdef", "012345")
                        for f in ("f1", "f9999", "f999999")]
            elif name != "refresh" or target == "contact":
                out.append(dv.Action(name, target))
    return out


def args_for(dv: ModuleType, a: Any) -> str:
    return dv.build_args(a.name, a.target, prefix=a.prefix, fact_id=a.fact_id, page=a.page)


def test_args_round_trip_and_size(dv: ModuleType) -> None:
    actions = all_actions(dv)
    assert {a.name for a in actions} == set(dv.ACTION_NAMES)
    seen = set()
    for action in actions:
        args = args_for(dv, action)
        assert args.isascii() and dv.ARGS_RE.fullmatch(args) and len(args.encode()) <= dv.ARGS_MAX_BYTES
        assert dv.parse_args(args) == action
        seen.add(args)
    assert len(seen) == len(actions)
    assert max(len(args_for(dv, a)) for a in actions) == len("rd.p.abcdef.999999")


@pytest.mark.parametrize("bad", [
    None, 123, b"o.c", "", "o", "o.x", "o.c.", ".o.c", "o..c", "o:c", "O.C", "о.c", " o.c", "o.c ", "o.c.extra",
    "m.c", "m.c.100", "m.c.01", "m.c.-1", "m.p.1.2", "f.c.abcdef", "f.c.abc.1", "f.c.ABCDEF.1", "f.c.abcdeg.1",
    "f.c.abcdef.0", "f.c.abcdef.01", "f.c.abcdef.1234567", "f.c.abcdef.f1", "f.x.abcdef.1", "pn.c.abcdef.1.2",
    "rf.p", "rf.c.1", "ea.c", "bk", "bk.c", "zz", "x" * 31, "rd.c.abcdef.1" + "\n", "o.c\x00",
])
def test_malformed_args_rejected(dv: ModuleType, bad: Any) -> None:
    assert dv.parse_args(bad) is None


@pytest.mark.parametrize("kwargs", [
    {"name": "nope"}, {"name": "overview", "target": "group"}, {"name": "refresh", "target": "persona"},
    {"name": "manage", "page": 100}, {"name": "manage", "page": -1}, {"name": "fact", "fact_id": "f1"},
    {"name": "fact", "prefix": "abcdef"}, {"name": "pin", "prefix": "ABCDEF", "fact_id": "f1"},
    {"name": "pin", "prefix": "abcdef", "fact_id": "f0"}, {"name": "pin", "prefix": "abcdef", "fact_id": "f1234567"},
    {"name": "pin", "prefix": "abcdefa", "fact_id": "f1"},
])
def test_build_args_rejects_invalid(dv: ModuleType, kwargs: dict[str, Any]) -> None:
    name = kwargs.pop("name")
    with pytest.raises(ValueError):
        dv.build_args(name, **kwargs)


def test_dossier_prefix(dv: ModuleType, contact: SimpleNamespace) -> None:
    assert PREFIX_RE.fullmatch(dv.dossier_prefix(contact.d)) and contact.d["dossier_id"].startswith(
        dv.dossier_prefix(contact.d))
    assert dv.dossier_prefix({"dossier_id": "ZZ<>12345"}) == "" and dv.dossier_prefix({}) == ""
    snap = replace(contact.snap, dossier={**contact.d, "dossier_id": "not-hex"})
    assert all(not a.startswith("f.") for a in args_of(dv.render_fact_picker(snap, "en")))
    assert all(not a.startswith(("pn.", "ra.")) for a in args_of(dv.render_fact_card(snap, "en", "f1")))


# ---------------------------------------------------------------------------
# Escaping and purity
# ---------------------------------------------------------------------------


EVIL = "<script>alert(1)</script> & \"double\" 'single' \u202eevil\u2066 line\nbreak </blockquote></details>"


def test_escaping_of_every_dynamic_string(mods: SimpleNamespace, dv: ModuleType) -> None:
    src = {**source(1, EVIL), "ref": "m1<x>"}
    facts = [fact(1, "decisions", EVIL, sources=[src], kind="inferred", confidence='"><b>'),
             fact(2, "evil<section>&", EVIL, kind="owner", owner={"action": "corrected", "previous_text": EVIL})]
    d = dossier(mods, facts)
    d["last_refresh"]["model"] = EVIL
    snap = snap_of(mods, d, label=EVIL, failure_error=EVIL, stale_error=EVIL,
                   stale={"f1": [{"ref": "m1<x>", "change": EVIL}]},
                   sources_now={"f1": [{**src, "state": "edited", "text": EVIL}]})
    persona = snap_of(mods, {**d, "kind": "persona"}, kind="persona",
                      chat_names={"m1<x>": EVIL})
    views = [dv.render_overview(snap, "en"), dv.render_overview(replace(snap, stale_error=None), "ru", audience="contact"),
             dv.render_fact_card(snap, "en", "f1"), dv.render_fact_card(snap, "ru", "f2"),
             dv.render_fact_picker(snap, "en"), dv.render_remove_confirm(snap, "en", "f1"),
             dv.render_overview(persona, "en"), dv.render_fact_card(persona, "en", "f1"),
             dv.render_overview(replace(snap, dossier=None, status="missing", missing_reason="failed"), "en"),
             dv.render_post_reason(replace(snap, enabled=False), "en"),
             dv.render_overview(replace(snap, dossier=None, status="missing", enabled=False, model=EVIL), "en")]
    for view in views:
        check_view(dv, view)
        assert "<script" not in view.html and "</blockquote></details> " not in view.html and "\n" not in view.html
        for escaped in ("&lt;script&gt;alert(1)&lt;/script&gt; &amp; ", "&quot;double&quot;", "&#x27;single&#x27;"):
            assert escaped in view.html, (escaped, view.html[:300])
        assert not BIDI_AND_CONTROL.search(view.text.replace("\n", ""))
        assert "line break" in view.text or "line brea" in view.html  # newlines collapse to spaces
    assert "evil&lt;section&gt;&amp;" in views[0].html and "<script>alert(1)</script>" in views[0].text


def test_module_is_pure() -> None:
    """Only stdlib text helpers and pure dossier_support constants/functions: no files, env, clock or logs."""
    source_text = (Path(__file__).resolve().parent.parent / "dossier_view.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from \S+ )?import .+$", source_text, re.MULTILINE))
    assert imports == {"import html as _html", "import re", "import shlex", "from collections.abc import Callable",
                       "from dataclasses import dataclass, field", "from datetime import UTC, datetime",
                       "from html.parser import HTMLParser", "from typing import Any",
                       "from . import dossier_support as _ds", "from __future__ import annotations"}
    assert set(re.findall(r"_ds\.(\w+)", source_text)) <= {
        "LABELS", "MAX_OWNER_TEXT_CHARS", "MAX_QUOTE_CHARS", "PERSONA_SECTIONS", "_is_note", "clip",
        "normalize_fact_id", "protected", "ws"}
    for forbidden in ("open(", "environ", ".now(", ".today(", "utcnow", "time.time", "print(", "logger"):
        assert forbidden not in source_text, forbidden
