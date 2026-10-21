"""Pure Telegram views of v2 dossiers: the Markdown converter, the panel screens, callback args, the
enable confirmation and the ``/dossier`` post with its refusals (SPEC J, K.3, K.4, H; overrides O3, O4, O7).

``dossier_view`` is loaded in a bare package (``core_module``), so these tests depend only on
``dossier_doc`` and ``dossier_writer``. No files, network or Telegram. All data is made up.
"""

from __future__ import annotations

import ast
import itertools
import json
import re
import sys
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from dossier_helpers import core_module

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CHECKED = datetime(2026, 10, 20, 9, 10, tzinfo=UTC)
CONN = "biz-demo-1"
CHAT = 700001
NAME = "Mira Tanaka"
DOC_MD = "**Mira is your design lead at Northwind Demo.**\n\n## Work\n- Owns the Orbit redesign [m90]\n"
DOC_HTML = (
    "<p><b>Mira is your design lead at Northwind Demo.</b></p><h4>Work</h4><ul><li>Owns the Orbit redesign</li></ul>"
)
CONVERTER_TAGS = {"h4", "p", "b", "i", "br", "ul", "li", "blockquote"}
SCREEN_TAGS = {**{tag: set() for tag in CONVERTER_TAGS}, "h3": set(), "tg-time": {"unix", "format"}}
VOID = {"br"}
BIDI_AND_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


@pytest.fixture(scope="module")
def dv(tmp_path_factory: pytest.TempPathFactory) -> Any:
    with pytest.MonkeyPatch.context() as mp:
        yield core_module(mp, tmp_path_factory.mktemp("dossier-view"), "dossier_view")


# ---------------------------------------------------------------------------
# Builders and checks
# ---------------------------------------------------------------------------


def contact_doc(dv: ModuleType, **changes: Any) -> dict[str, Any]:
    """A valid contact ``document.json``: built and checked by default."""
    doc = dv._doc.new_contact(CONN, CHAT, NOW - timedelta(days=30))
    doc.update(document=DOC_MD, revision=3, updated_at=iso(CHECKED), checked_at=iso(CHECKED), messages=312)
    doc.update(window_start=iso(NOW - timedelta(days=90)))
    doc.update(changes)
    assert dv._doc.valid(doc)
    return doc


def persona_doc(dv: ModuleType, **changes: Any) -> dict[str, Any]:
    doc = dv._doc.new_persona(NOW - timedelta(days=30))
    doc.update(
        document="## Tone\n- Short, friendly replies [m4]\n",
        revision=2,
        updated_at=iso(CHECKED),
        checked_at=iso(NOW),
        messages=40,
        chats=[{"business_connection_id": CONN, "chat_id": CHAT}, {"business_connection_id": CONN, "chat_id": 700002}],
        newest_at=iso(CHECKED - timedelta(hours=1)),
    )
    doc.update(changes)
    assert dv._doc.valid(doc)
    return doc


def snap(dv: ModuleType, target: str = "contact", **changes: Any) -> Any:
    base: dict[str, Any] = {"target": target, "name": NAME if target == "contact" else ""}
    base.update(chat_id=CHAT, connection=CONN if target == "contact" else None,
                doc=contact_doc(dv) if target == "contact" else persona_doc(dv))
    base.update(changes)
    return dv.DossierSnapshot(**base)


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def tg_time(at: datetime, body: str) -> str:
    return f'<tg-time unix="{int(at.timestamp())}" format="r">{body}</tg-time>'


def s(dv: ModuleType, lang: str, key: str) -> Any:
    return dv.STRINGS[lang][key]


class _Checker(HTMLParser):
    """Well-formed HTML with only the allowed tags and attributes."""

    def __init__(self, allowed: dict[str, set[str]]) -> None:
        super().__init__(convert_charrefs=True)
        self.allowed = allowed
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        if tag not in self.allowed:
            self.errors.append(f"tag {tag}")
        for name, _value in attrs:
            if name not in self.allowed.get(tag, set()):
                self.errors.append(f"attr {tag}[{name}]")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.errors.append(f"self-closed {tag}")

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID or not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> in {self.stack}")
        else:
            self.stack.pop()


def check_html(dv: ModuleType, html: str, allowed: dict[str, set[str]] | None = None) -> set[str]:
    checker = _Checker(SCREEN_TAGS if allowed is None else allowed)
    checker.feed(html)
    checker.close()
    assert not checker.errors, checker.errors[:5]
    assert not checker.stack, f"unclosed {checker.stack}"
    assert re.search(r"&(?!(lt|gt|amp|quot|#x27);)", html) is None, "unsupported entity"
    assert not BIDI_AND_CONTROL.search(html)
    assert dv.fits(html)
    return checker.tags


def check_view(dv: ModuleType, view: Any) -> None:
    check_html(dv, view.html)
    assert view.text and dv._units(view.text) <= dv.TEXT_MAX_CHARS
    for row in view.buttons:
        assert 1 <= len(row) <= 2
        for label, args in row:
            assert label and not BIDI_AND_CONTROL.search(label)
            assert args.isascii() and len(args.encode()) <= dv.ARGS_MAX_BYTES
            assert dv.parse_args(args) is not None, args
    assert view.notice is None or len(view.notice) <= 200


def args_of(view: Any) -> list[str]:
    return [args for row in view.buttons for _, args in row]


# ---------------------------------------------------------------------------
# Markdown -> rich HTML
# ---------------------------------------------------------------------------

GOLDEN = [
    ("plain", "Mira leads the Orbit redesign.", "<p>Mira leads the Orbit redesign.</p>"),
    ("lines and paragraphs", "One\ntwo\n\nThree", "<p>One<br>two</p><p>Three</p>"),
    ("headings", "# Mira\n### Work\n###### Small", "<h4>Mira</h4><h4>Work</h4><h4>Small</h4>"),
    ("closing hashes", "## Work ##", "<h4>Work</h4>"),
    ("not headings", "#hashtag and\n####### seven", "<p>#hashtag and<br>####### seven</p>"),
    ("bullets", "- one\n* two\n+ three\n• four", "<ul><li>one</li><li>two</li><li>three</li><li>four</li></ul>"),
    (
        "nested bullets",
        "- Orbit\n  - review on Friday\n    - room 4\n- Nova",
        "<ul><li>Orbit</li><li>– review on Friday</li><li>– room 4</li><li>Nova</li></ul>",
    ),
    ("continuation", "- Orbit review\n  moved to Friday", "<ul><li>Orbit review<br>moved to Friday</li></ul>"),
    (
        "around a list",
        "Plans:\n- a\n- b\nThat is all.",
        "<p>Plans:</p><ul><li>a</li><li>b</li></ul><p>That is all.</p>",
    ),
    ("numbered", "1. Send the draft\n2) Book the room", "<p>1. Send the draft<br>2) Book the room</p>"),
    ("quote", "> We stop after December.\n>Final.", "<blockquote>We stop after December.<br>Final.</blockquote>"),
    ("table", "| Item | Price |\n|:---|---:|\n| Logo | 120 |", "<p>Item · Price</p><p>Logo · 120</p>"),
    ("fences end blocks", '```json\n{"a": 1}\n```\n~~~\nb\n~~~', '<p>{"a": 1}</p><p>b</p>'),
    ("one-line code", "```npm i``` then go", "<p>npm i then go</p>"),
    ("rules", "Before\n\n---\n***\n_ _ _\n\nAfter", "<p>Before</p><p>After</p>"),
    ("bold", "**Mira** and __Orbit__", "<p><b>Mira</b> and <b>Orbit</b></p>"),
    ("italic", "*soon* and _maybe_", "<p><i>soon</i> and <i>maybe</i></p>"),
    ("bold italic", "***now***", "<p><b><i>now</i></b></p>"),
    ("nesting", "**a *b* c** and *d **e** f*", "<p><b>a <i>b</i> c</b> and <i>d <b>e</b> f</i></p>"),
    ("code, strike, link", "`npm i` ~~old~~ [site](https://example.com/a?b=1&c=2)", "<p>npm i old site</p>"),
    (
        "refs",
        "## Work [m1]\n- Owns Orbit [m90, m91]\n- Review on **Friday** [m7]",
        "<h4>Work</h4><ul><li>Owns Orbit</li><li>Review on <b>Friday</b></li></ul>",
    ),
    ("end marker", "Done.\n<<END>>", "<p>Done.</p>"),
    ("markers inside words", "snake_case_name and 2*3*4", "<p>snake_case_name and 2*3*4</p>"),
    ("unbalanced", "**open\nhalf* and _x", "<p>**open<br>half* and _x</p>"),
    ("crossed", "**a *b** c*", "<p><b>a *b</b> c*</p>"),
    ("escaping", "a < b & c > d", "<p>a &lt; b &amp; c &gt; d</p>"),
    ("windows newlines and tabs", "one\r\ntwo\tthree", "<p>one<br>two  three</p>"),
    ("empty", "", ""),
]


@pytest.mark.parametrize(("md", "html"), [case[1:] for case in GOLDEN], ids=[case[0] for case in GOLDEN])
def test_converter_golden_table(dv: ModuleType, md: str, html: str) -> None:
    assert dv.markdown_html(md) == html
    check_html(dv, html, {tag: set() for tag in CONVERTER_TAGS})


def test_converter_escapes_every_block_kind(dv: ModuleType) -> None:
    evil = '<script>alert("x")</script> & <b>co</b>'
    md = f"# {evil}\n\n- {evil}\n\n1. {evil}\n\n> {evil}\n\n| {evil} | x |\n\n{evil}"
    escaped = '&lt;script&gt;alert("x")&lt;/script&gt; &amp; &lt;b&gt;co&lt;/b&gt;'

    html = dv.markdown_html(md)

    assert "<script" not in html and "<b>co" not in html
    assert html == (
        f"<h4>{escaped}</h4><ul><li>{escaped}</li></ul><p>1. {escaped}</p>"
        f"<blockquote>{escaped}</blockquote><p>{escaped} · x</p><p>{escaped}</p>"
    )


def test_converter_removes_control_and_bidi_characters(dv: ModuleType) -> None:
    md = "\u202eevil\u202c \u2066Mira\u2069\u200f\x07\x1b[0m\x00 note\u061c\x85"

    html = dv.markdown_html(md)

    assert html == "<p>evil Mira[0m note</p>"
    assert not BIDI_AND_CONTROL.search(html)


def test_converter_strips_refs_everywhere(dv: ModuleType) -> None:
    md = (
        "# Mira [m1]\n- Owns Orbit [m2,m3]\n| Logo [m4] | 120 [m5–m9] |\n> Quote [m6-7]\n**Bold [m7, m8–m9]**\n"
        "Text [m123456789012]"
    )

    html = dv.markdown_html(md)

    assert not re.search(r"\[m\d", html)
    assert html == (
        "<h4>Mira</h4><ul><li>Owns Orbit</li></ul><p>Logo · 120</p><blockquote>Quote</blockquote>"
        "<p><b>Bold</b><br>Text</p>"
    )


@pytest.mark.parametrize(
    "md",
    ["**not closed", "closed**", "*one", "two*", "__x", "x__", "_y", "y_", "~~gone", "`tick", "[text](no end"],
)
def test_unbalanced_markers_stay_literal(dv: ModuleType, md: str) -> None:
    html = dv.markdown_html(md)

    assert html == f"<p>{md}</p>"
    assert "<b>" not in html and "<i>" not in html


def test_converter_output_is_always_well_formed_and_in_the_subset(dv: ModuleType) -> None:
    fragments = [
        "**",
        "*",
        "__",
        "_",
        "***",
        "`",
        "~~",
        "[a](b)",
        "<b>",
        "</i>",
        "&amp;",
        "<<END>>",
        "[m1]",
        "x",
        "# h",
        "- li",
        "  - nested",
        "1. n",
        "> q",
        "| a | b |",
        "|---|",
        "```",
        "\u202e",
        "**b *i* b**",
        "*i **b** i*",
    ]
    allowed = {tag: set() for tag in CONVERTER_TAGS}
    seen: set[str] = set()
    for first, second in itertools.product(fragments, repeat=2):
        for md in (first + second, f"{first} {second}", f"{first}\n{second}", f"{first}word{second}"):
            seen |= check_html(dv, dv.markdown_html(md), allowed)
    assert seen == CONVERTER_TAGS  # every tag of the subset is produced, and nothing else


def test_the_fits_guard_falls_back_to_plain_escaped_lines(dv: ModuleType) -> None:
    md = "\n".join(f"- **item {i}** & more [m{i}]" for i in range(300))  # 600 blocks as a list
    assert not dv.fits(dv._blocks(dv._lines(md)))

    html = dv.markdown_html(md)

    assert html == "<p>" + "<br>".join(f"- **item {i}** &amp; more" for i in range(300)) + "</p>"
    assert dv.fits(html) and dv.count_blocks(html) == 1


def test_the_whole_screen_falls_back_when_the_document_fills_the_block_budget(dv: ModuleType) -> None:
    document = "\n".join(f"- b{i}" for i in range(224))  # 448 blocks: fits alone, not with the screen around it
    assert dv.fits(dv.markdown_html(document))

    view = dv.render_overview(snap(dv, doc=contact_doc(dv, document=document, note="Call after six.")), "en")

    check_view(dv, view)
    assert "<p>- b0<br>- b1<br>" in view.html and "<li>" not in view.html
    assert view.html.endswith("<blockquote><b>Your note (not posted)</b><br>Call after six.</blockquote>")


# ---------------------------------------------------------------------------
# The contact screen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_contact_screen(dv: ModuleType, lang: str) -> None:
    doc = contact_doc(dv, note="Call after six.\nNo calls on Sundays.")

    view = dv.render_overview(snap(dv, doc=doc), lang)

    check_view(dv, view)
    if lang == "en":
        head = f"Updated {tg_time(CHECKED, '20 October 2026, 09:10 UTC')} · from 312 messages"
        label = "Your note (not posted)"
    else:
        head = f"Обновлено {tg_time(CHECKED, '20 октября 2026, 09:10 UTC')} · сообщений: 312"
        label = "Ваша заметка (не публикуется)"
    assert view.html == (
        f"<h3>{NAME}</h3><p><i>{head}</i></p>{DOC_HTML}"
        f"<blockquote><b>{label}</b><br>Call after six.<br>No calls on Sundays.</blockquote>"
    )
    assert view.buttons == (((s(dv, lang, "b_refresh"), "rf.c"),),)
    assert view.text.splitlines()[0] == NAME and "• Owns the Orbit redesign" in view.text


@pytest.mark.parametrize(
    ("lang", "messages", "expected"),
    [
        ("en", 0, "from 0 messages"),
        ("en", 1, "from 1 message"),
        ("en", 2, "from 2 messages"),
        ("en", 21, "from 21 messages"),
        ("ru", 1, "сообщений: 1"),
        ("ru", 3, "сообщений: 3"),
        ("ru", 25, "сообщений: 25"),
    ],
)
def test_contact_header_plurals(dv: ModuleType, lang: str, messages: int, expected: str) -> None:
    view = dv.render_overview(snap(dv, doc=contact_doc(dv, messages=messages)), lang)

    assert re.search(r"<p><i>[^<]*<tg-time[^>]*>[^<]*</tg-time> · ([^<]*)</i></p>", view.html)[1] == expected


@pytest.mark.parametrize(
    ("lang", "n", "chats", "expected"),
    [
        ("en", 1, 1, "from 1 of your messages in 1 chat"),
        ("en", 40, 2, "from 40 of your messages in 2 chats"),
        ("ru", 1, 1, "ваших сообщений: 1, чатов: 1"),
        ("ru", 40, 5, "ваших сообщений: 40, чатов: 5"),
    ],
)
def test_persona_header_plurals(dv: ModuleType, lang: str, n: int, chats: int, expected: str) -> None:
    doc = persona_doc(dv, messages=n, chats=[{"business_connection_id": CONN, "chat_id": i} for i in range(chats)])

    view = dv.render_overview(snap(dv, "persona", doc=doc), lang)

    assert re.search(r"</tg-time> · ([^<]*)</i></p>", view.html)[1] == expected


def test_the_header_needs_a_check_time(dv: ModuleType) -> None:
    for checked_at in (None, "not a time", "0001-01-01T00:00:00+05:00"):  # the last is out of range in UTC
        view = dv.render_overview(snap(dv, doc=contact_doc(dv, checked_at=checked_at)), "en")

        check_view(dv, view)
        assert view.html == f"<h3>{NAME}</h3>{DOC_HTML}"


def test_the_title_is_the_escaped_name_clipped_to_64(dv: ModuleType) -> None:
    long_name = "Mira " + "Tanaka-" * 20

    titles = {
        name: re.match(r"<h3>(.*?)</h3>", dv.render_overview(snap(dv, name=name), "en").html)[1]
        for name in ("", " \u202e ", "<b>Mira & Co</b>", long_name)
    }

    assert titles[""] == titles[" \u202e "] == "?"
    assert titles["<b>Mira & Co</b>"] == "&lt;b&gt;Mira &amp; Co&lt;/b&gt;"
    assert titles[long_name].endswith("…") and dv._units(titles[long_name]) == 64


def test_only_the_document_and_the_note_are_shown(dv: ModuleType) -> None:
    doc = contact_doc(
        dv,
        previous="CANARY-PREVIOUS",
        seed="CANARY-SEED",
        last_call={"provider": "CANARY-PROVIDER", "model": "CANARY-MODEL", "at": iso(CHECKED)},
        business_connection_id="biz-canary",
        seen={"77": ["CANARY-FP", iso(CHECKED)]},
        leave_out=["CANARY-LEAVE-OUT"],  # no such field in v2 (O3); an unknown key is never shown
        note_sha="CANARY-SHA",
    )

    view = dv.render_overview(snap(dv, doc=doc, chat_id="CANARY-CHAT"), "en")

    assert "CANARY" not in view.html + view.text and "biz-canary" not in view.html


# ---------------------------------------------------------------------------
# The note
# ---------------------------------------------------------------------------


def note_of(view: Any) -> str:
    return re.search(r"<blockquote><b>[^<]*</b><br>(.*)</blockquote>", view.html)[1]


def test_the_note_is_clipped_at_500_units_with_an_ellipsis(dv: ModuleType) -> None:
    note = "Before any call, check the Orbit tracker. " * 30  # 1260 characters, as a migrated note may be

    shown = note_of(dv.render_overview(snap(dv, doc=contact_doc(dv, note=note.strip())), "en"))

    assert shown.endswith("…") and dv._units(shown) <= dv.NOTE_DISPLAY_UNITS == 500
    assert note.startswith(shown[:-1])


def test_a_short_note_shows_whole_and_escaped(dv: ModuleType) -> None:
    note = "Line <one> & two\n\n   spaced    out  \n\u202eLast\u202c"

    shown = note_of(dv.render_overview(snap(dv, doc=contact_doc(dv, note=note)), "en"))

    assert shown == "Line &lt;one&gt; &amp; two<br>spaced out<br>Last"


def test_a_note_keeps_at_most_ten_lines(dv: ModuleType) -> None:
    note = "\n".join(f"line {i}" for i in range(1, 16))

    shown = note_of(dv.render_overview(snap(dv, doc=contact_doc(dv, note=note)), "en")).split("<br>")

    assert shown == [*(f"line {i}" for i in range(1, 10)), "line 10 line 11 line 12 line 13 line 14 line 15"]


def test_no_note_no_quote(dv: ModuleType) -> None:
    for note in ("", "  \n "):
        assert "<blockquote>" not in dv.render_overview(snap(dv, doc=contact_doc(dv, note=note)), "en").html


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_a_migrated_contact_shows_missing_and_its_note(dv: ModuleType, lang: str) -> None:
    doc = contact_doc(
        dv,
        document="",
        revision=0,
        updated_at=None,
        checked_at=None,
        messages=0,
        note="Never call on Sundays.\n\nLeave out:\n- Plans to leave Northwind Demo.",
        seed="## Relationship\n- CANARY-SEED",
        migrated_from="v1",
    )

    view = dv.render_overview(snap(dv, doc=doc), lang)

    check_view(dv, view)
    label = "Your note (not posted)" if lang == "en" else "Ваша заметка (не публикуется)"
    assert view.html == (
        f"<h3>{NAME}</h3><p>{s(dv, lang, 'st_missing')}</p><blockquote><b>{label}</b><br>Never call on Sundays."
        "<br>Leave out:<br>- Plans to leave Northwind Demo.</blockquote>"
    )


# ---------------------------------------------------------------------------
# States and status lines
# ---------------------------------------------------------------------------

NO_DOCUMENT_STATES = [
    ("off", {"enabled": False, "manage_global": True}, "st_off"),
    ("history off", {"history_enabled": False}, "st_history_off"),
    ("chat off", {"chat_off": True}, "note_chat_off"),
    ("no model", {"llm_ready": False}, "st_no_model"),
    ("refreshing", {"refreshing": True}, "st_refreshing"),
    ("failed", {"failure": "no_end_marker"}, "st_failed_none"),
    ("missing", {}, "st_missing"),
    ("unqualified", {"unqualified": True}, "st_unqualified"),
    ("not in history", {"in_history": False}, "st_not_in_history"),
]


@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize(
    ("changes", "key"), [case[1:] for case in NO_DOCUMENT_STATES], ids=[c[0] for c in NO_DOCUMENT_STATES]
)
def test_contact_states_without_a_document(dv: ModuleType, lang: str, changes: dict[str, Any], key: str) -> None:
    view = dv.render_overview(snap(dv, doc=None, **changes), lang)

    check_view(dv, view)
    body = s(dv, lang, key).format(error="no_end_marker")
    assert view.html == f"<h3>{NAME}</h3><p>{body}</p>"


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_off_tells_a_non_manager_who_can_turn_dossiers_on(dv: ModuleType, lang: str) -> None:
    view = dv.render_overview(snap(dv, doc=None, enabled=False), lang)

    check_view(dv, view)
    lines = (s(dv, lang, "st_off"), s(dv, lang, "notice")["operator_only"], s(dv, lang, "off_cli"))
    assert view.html == f"<h3>{NAME}</h3><p>{'<br>'.join(lines)}</p>"
    assert "hermes telesec dossier enable" in view.text


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_off_under_a_document_tells_a_non_manager_who_can_turn_dossiers_on(dv: ModuleType, lang: str) -> None:
    view = dv.render_overview(snap(dv, enabled=False), lang)  # Settings is read-only for a non-manager

    check_view(dv, view)
    lines = (s(dv, lang, "st_off"), s(dv, lang, "notice")["operator_only"], s(dv, lang, "off_cli"))
    assert view.html.endswith(f"{DOC_HTML}<p><i>{'<br>'.join(lines)}</i></p>")
    assert "hermes telesec dossier enable" in view.text
    managed = dv.render_overview(snap(dv, enabled=False, manage_global=True), lang)
    assert managed.html.endswith(f"<p><i>{s(dv, lang, 'st_off')}</i></p>")


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_a_failed_first_persona_names_the_persona(dv: ModuleType, lang: str) -> None:
    body = s(dv, lang, "st_failed_none_persona").format(error="no_end_marker")

    missing = dv.render_overview(snap(dv, "persona", doc=None, failure="no_end_marker", manage_global=True), lang)
    empty = dv.render_overview(snap(dv, "persona", doc=persona_doc(dv, document=""), failure="no_end_marker"), lang)

    check_view(dv, missing)
    assert missing.html == f"<h3>{s(dv, lang, 'title_persona')}</h3><p>{body}</p>"
    assert empty.html.endswith(f"<p><i>{body}</i></p>")
    assert s(dv, lang, "st_failed_none").format(error="no_end_marker") not in missing.html + empty.html


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_the_screen_never_names_a_hidden_refresh_button(dv: ModuleType, lang: str) -> None:
    label = re.compile(rf"(?<!\w){s(dv, lang, 'b_refresh')}(?!\w)")
    flags = ("enabled", "history_enabled", "chat_off", "llm_ready", "in_history", "refreshing", "corrupt",
             "unqualified", "manage_global")
    for values in itertools.product((False, True), repeat=len(flags)):
        for failure in (None, "TimeoutError"):
            view = dv.render_overview(snap(dv, doc=None, failure=failure, **dict(zip(flags, values))), lang)
            if "rf.c" not in args_of(view):
                assert not label.search(view.text), (values, failure, view.text)


TARGET_HTML = "--connection 'biz &lt;demo&gt;' --chat '7 00;rm'"


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_a_corrupt_file_shows_the_reset_command(dv: ModuleType, lang: str) -> None:
    contact = dv.render_overview(snap(dv, doc=None, corrupt=True, chat_id="7 00;rm", connection="biz <demo>"), lang)
    persona = dv.render_overview(snap(dv, "persona", doc=None, corrupt=True, manage_global=True), lang)

    check_view(dv, contact)
    check_view(dv, persona)
    assert (
        contact.html
        == f"<h3>{NAME}</h3><p>{s(dv, lang, 'st_corrupt').format(target=TARGET_HTML)}</p>"
    )
    assert "hermes telesec dossier forget --connection 'biz <demo>' --chat '7 00;rm'" in contact.text
    assert "hermes telesec dossier forget --persona" in persona.text
    assert "rf.c" not in args_of(contact)


def test_a_corrupt_flag_wins_over_a_document(dv: ModuleType) -> None:
    view = dv.render_overview(snap(dv, corrupt=True), "en")

    assert "Mira is your design lead" not in view.html and "can't be read" in view.html


DOCUMENT_STATUSES = [
    ("none", {}, {}, None),
    ("off", {"enabled": False, "manage_global": True}, {}, "st_off"),
    ("history off", {"history_enabled": False}, {}, "st_history_off"),
    ("chat off", {"chat_off": True}, {}, "note_chat_off"),
    ("no model", {"llm_ready": False}, {}, "st_no_model"),
    ("refreshing", {"refreshing": True}, {}, "st_refreshing"),
    ("failed", {"failure": "no_end_marker"}, {}, "st_failed"),
    ("empty", {}, {"document": ""}, "st_empty"),
    ("failed and empty", {"failure": "TimeoutError"}, {"document": ""}, "st_failed_none"),
]


@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize(
    ("changes", "doc_changes", "key"), [case[1:] for case in DOCUMENT_STATUSES], ids=[c[0] for c in DOCUMENT_STATUSES]
)
def test_status_line_under_a_document(
    dv: ModuleType, lang: str, changes: dict[str, Any], doc_changes: dict[str, Any], key: str | None
) -> None:
    view = dv.render_overview(snap(dv, doc=contact_doc(dv, **doc_changes), **changes), lang)

    check_view(dv, view)
    status = re.findall(r"<p><i>([^<]*)</i></p>$", view.html)
    if key is None:
        assert status == [] and view.html.endswith(DOC_HTML)
    else:
        error = changes.get("failure", "")
        assert status == [s(dv, lang, key).format(error=error)]
        assert view.html.count("<h3>") == 1 and "<tg-time" in view.html  # the header stays


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"history_enabled": False, "chat_off": True, "refreshing": True, "failure": "x"}, "st_history_off"),
        ({"chat_off": True, "refreshing": True, "failure": "x"}, "note_chat_off"),
        ({"refreshing": True, "failure": "x"}, "st_refreshing"),
        ({"failure": "x"}, "st_failed"),
    ],
)
def test_status_priority(dv: ModuleType, changes: dict[str, Any], key: str) -> None:
    view = dv.render_overview(snap(dv, **changes), "en")

    assert view.html.endswith(f"<p><i>{s(dv, 'en', key).format(error='x')}</i></p>")
    assert view.html.count("<p><i>") == 2  # the header and one status line


def test_the_failure_code_is_escaped_and_clipped(dv: ModuleType) -> None:
    view = dv.render_overview(snap(dv, failure="<Bad&Error>" + "x" * 100), "en")

    check_view(dv, view)
    error = re.search(r"failed \((.*)\)\.", view.html)[1]
    assert error.startswith("&lt;Bad&amp;Error&gt;") and error.endswith("…")


# ---------------------------------------------------------------------------
# The persona screen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_persona_screen(dv: ModuleType, lang: str) -> None:
    doc = persona_doc(dv, note="Keep it short.")

    view = dv.render_overview(snap(dv, "persona", doc=doc, manage_global=True, chat_off=True), lang)

    check_view(dv, view)
    if lang == "en":
        title, label = "My persona", "Your note"
        head = f"Updated {tg_time(CHECKED, '20 October 2026, 09:10 UTC')} · from 40 of your messages in 2 chats"
    else:
        title, label = "Моя персона", "Ваша заметка"
        head = f"Обновлено {tg_time(CHECKED, '20 октября 2026, 09:10 UTC')} · ваших сообщений: 40, чатов: 2"
    assert view.html == (  # chat_off belongs to a contact: the persona ignores it
        f"<h3>{title}</h3><p><i>{head}</i></p><h4>Tone</h4><ul><li>Short, friendly replies</li></ul>"
        f"<blockquote><b>{label}</b><br>Keep it short.</blockquote>"
    )
    assert view.buttons == (((s(dv, lang, "b_back_dossier"), "o.c"),),)


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_persona_missing(dv: ModuleType, lang: str) -> None:
    empty = dv._doc.new_persona(NOW)  # never built: no header, the state is the body
    for doc in (None, empty, {**empty, "note": "Keep it short."}):
        view = dv.render_overview(snap(dv, "persona", doc=doc, manage_global=True), lang)

        check_view(dv, view)
        assert view.html.startswith(
            f"<h3>{s(dv, lang, 'title_persona')}</h3><p>{s(dv, lang, 'st_persona_missing')}</p>"
        )
        assert view.buttons == (((s(dv, lang, "b_back_dossier"), "o.c"),),)


PERSONA_DUE = {"en": "No persona yet. It is written at the next dossier cycle.",
               "ru": "Персоны пока нет. Она появится после следующего цикла обработки."}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_a_persona_due_at_the_next_cycle_never_asks_for_10_messages(dv: ModuleType, lang: str) -> None:
    empty = dv._doc.new_persona(NOW)
    for doc in (None, empty, {**empty, "note": "Keep it short."}):
        view = dv.render_overview(snap(dv, "persona", doc=doc, manage_global=True, persona_due=True), lang)

        check_view(dv, view)
        assert view.html.startswith(f"<h3>{s(dv, lang, 'title_persona')}</h3><p>{PERSONA_DUE[lang]}</p>")
        assert "10" not in view.text
    built = dv.render_overview(snap(dv, "persona", doc=persona_doc(dv), persona_due=True), lang)
    assert PERSONA_DUE[lang] not in built.text  # a written persona shows itself


def test_an_empty_persona_build_shows_its_header_and_empty(dv: ModuleType) -> None:
    view = dv.render_overview(snap(dv, "persona", doc=persona_doc(dv, document="")), "en")

    assert "<tg-time" in view.html and view.html.endswith("<p><i>Nothing worth keeping yet.</i></p>")


def test_an_unknown_target_is_an_error(dv: ModuleType) -> None:
    with pytest.raises(ValueError):
        dv.render_overview(snap(dv, target="group"), "en")
    with pytest.raises(ValueError):
        dv.render_state("busy", "en", "group")


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


def test_refresh_and_persona_buttons(dv: ModuleType) -> None:
    assert args_of(dv.render_overview(snap(dv), "en")) == ["rf.c"]
    assert args_of(dv.render_overview(snap(dv, manage_global=True), "en")) == ["rf.c", "o.p"]
    assert dv.render_overview(snap(dv, manage_global=True), "ru").buttons == (
        (("Обновить", "rf.c"), ("Моя персона", "o.p")),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"history_enabled": False},
        {"chat_off": True},
        {"llm_ready": False},
        {"in_history": False},
        {"refreshing": True},
        {"corrupt": True},
        {"doc": None, "unqualified": True},
    ],
    ids=lambda changes: ",".join(f"{key}={value}" for key, value in changes.items()),
)
def test_refresh_is_hidden_when_it_cannot_run(dv: ModuleType, changes: dict[str, Any]) -> None:
    view = dv.render_overview(snap(dv, manage_global=True, **changes), "en")

    assert args_of(view) == ["o.p"]


def test_refresh_stays_for_a_built_dossier_and_for_a_missing_one(dv: ModuleType) -> None:
    assert args_of(dv.render_overview(snap(dv, unqualified=True), "en")) == ["rf.c"]  # a document is shown
    assert args_of(dv.render_overview(snap(dv, doc=None), "en")) == ["rf.c"]
    assert args_of(dv.render_overview(snap(dv, failure="TimeoutError"), "en")) == ["rf.c"]


# ---------------------------------------------------------------------------
# A document converted from v1 (revision 0, no v2 pass yet)
# ---------------------------------------------------------------------------

CONVERTED_MD = "## Relationship\n- Mira is the design lead at Northwind Demo.\n\n## Commitments\n- Review on Friday."


def converted_doc(dv: ModuleType, **changes: Any) -> dict[str, Any]:
    doc = dv._doc.new_contact(CONN, CHAT, NOW - timedelta(days=1))
    doc.update(document=CONVERTED_MD, note="Never call on Sundays.", migrated_from="v1", note_from="v1")
    doc.update(changes)
    assert dv._doc.valid(doc) and dv._doc.converted(doc)
    return doc


@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize(
    ("changes", "key", "buttons"),
    [
        ({}, "st_converted", ["rf.c"]),
        ({"unqualified": True}, "st_converted_waiting", []),  # inactive: nothing in the backfill window
        ({"in_history": False}, "st_converted_waiting", []),  # pruned, or the chat left history
        ({"chat_off": True}, "note_chat_off", []),  # paused for this chat: frozen, still readable
        ({"llm_ready": False}, "st_no_model", []),
    ],
    ids=["ready", "unqualified", "not in history", "chat off", "no model"],
)
def test_a_converted_document_is_shown_in_every_chat_state(
    dv: ModuleType, lang: str, changes: dict[str, Any], key: str, buttons: list[str]
) -> None:
    view = dv.render_overview(snap(dv, doc=converted_doc(dv), **changes), lang)

    check_view(dv, view)
    assert "<h4>Relationship</h4>" in view.html and "Mira is the design lead at Northwind Demo." in view.html
    assert "Never call on Sundays." in view.html  # the converted note
    assert view.html.endswith(f"<p><i>{s(dv, lang, key)}</i></p>")
    for missing in ("st_missing", "st_unqualified", "st_not_in_history"):
        assert s(dv, lang, missing) not in view.html
    assert "<tg-time" not in view.html  # never checked: no header time
    assert args_of(view) == buttons


# ---------------------------------------------------------------------------
# Notices and one-line screens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["busy", "refresh_started", "unavailable", "up_to_date", "failed", "operator_only"])
def test_states_and_notices(dv: ModuleType, key: str) -> None:
    for lang in ("en", "ru"):
        view = dv.render_state(key, lang, "persona", error="BadRequest")

        check_view(dv, view)
        assert view.notice == dv.notice_text(key, lang, error="BadRequest")
        assert view.html == f"<p>{view.notice}</p>"
        assert view.buttons == ((("Open dossier" if lang == "en" else "Открыть досье", "o.p"),),)
    assert dv.notice_text("up_to_date", "en") == "Already up to date"
    assert dv.notice_text("up_to_date", "ru") == "Уже актуально"
    assert dv.notice_text("failed", "en") == "That did not work (?)."
    assert dv.notice_text("failed", "en", error="x" * 100).endswith("…).")


def test_no_connection_has_no_retry_button(dv: ModuleType) -> None:
    view = dv.render_state("no_connection", "en")

    check_view(dv, view)
    assert view.buttons == ()


def test_v1_notices_are_gone(dv: ModuleType) -> None:
    for key in ("changed", "pinned", "unpinned", "removed", "nope"):
        with pytest.raises(ValueError):
            dv.notice_text(key, "en")


def test_with_notice_shows_a_notice_once(dv: ModuleType) -> None:
    for key in dv.NOTICE_KEYS:
        for lang in ("en", "ru"):
            state = dv.render_state(key, lang, error="X")
            assert dv.with_notice(state) == state  # the screen already is the notice
    plain = dv.render_overview(snap(dv), "en")
    assert dv.with_notice(plain) == plain
    noted = replace(plain, notice="Refresh started.")

    shown = dv.with_notice(noted)

    check_view(dv, shown)
    assert shown.html == f"<p><b>Refresh started.</b></p>{plain.html}"
    assert shown.text.startswith(f"Refresh started.\n\n{NAME}")
    assert shown.buttons == plain.buttons and shown.notice == "Refresh started."


# ---------------------------------------------------------------------------
# Callback args
# ---------------------------------------------------------------------------


def test_the_three_args_round_trip(dv: ModuleType) -> None:
    actions = {
        "o.c": dv.Action("overview", "contact"),
        "o.p": dv.Action("overview", "persona"),
        "rf.c": dv.Action("refresh", "contact"),
    }
    for args, action in actions.items():
        assert dv.parse_args(args) == action
        assert dv.build_args(action.name, action.target) == args
    assert dv.parse_args("") == dv.Action("overview", "contact")
    assert dv.build_args("overview") == "o.c" and dv.build_args("refresh") == "rf.c"


V1_ARGS = [
    "m.c.0",
    "m.p.3",
    "f.c.abcdef.12",
    "pn.c.abcdef.1",
    "up.p.012345.9",
    "ra.c.abcdef.1",
    "rd.c.abcdef.1",
    "ea",
    "ed",
    "ds",
    "m",
    "f",
    "pn",
    "up",
    "ra",
    "rd",
]


@pytest.mark.parametrize("args", V1_ARGS)
def test_every_v1_code_gives_none(dv: ModuleType, args: str) -> None:
    assert dv.parse_args(args) is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        1,
        b"o.c",
        ["o.c"],
        "o",
        "o.",
        ".c",
        "o.x",
        "rf.p",
        "rf",
        "O.C",
        "o.c ",
        " o.c",
        "o.c.0",
        "o..c",
        "о.с",
        "o.c\n",
        "o-c",
        "x" * 31,
        "o.c" + "." * 28,
    ],
)
def test_malformed_args_give_none(dv: ModuleType, bad: Any) -> None:
    assert dv.parse_args(bad) is None


@pytest.mark.parametrize(("name", "target"), [("refresh", "persona"), ("pin", "contact"), ("overview", "group")])
def test_build_args_rejects_unknown_actions(dv: ModuleType, name: str, target: str) -> None:
    with pytest.raises(ValueError):
        dv.build_args(name, target)


def test_every_callback_fits_telegram(dv: ModuleType) -> None:
    import importlib

    panel = importlib.import_module(f"{dv.__package__}.owner_panel")
    for args in ("o.c", "o.p", "rf.c"):
        assert dv.ARGS_RE.fullmatch(args) and len(args.encode()) <= dv.ARGS_MAX_BYTES
        for chat in (0, 991, -1001234567890, -99999999999999999999):
            data = panel.section_callback(chat, "dossier", args)  # raises over 64 bytes
            assert len(data.encode()) <= 64
            assert panel.parse_callback(data) is not None


# ---------------------------------------------------------------------------
# Turning dossiers on (Settings)
# ---------------------------------------------------------------------------

ROUTE_EN = (
    "Dossiers use the Telesec dossiers model task; change it with 'hermes model'. If that model fails, Hermes may "
    "retry with your main model."
)
GATE_EN = ("For short checks, the current dossier and the chat's new messages go to TypeSafe Jev through the same "
           "AI Gateway, with zero data retention.")
ROUTE_RU = (
    "Досье пишет модель задачи Telesec dossiers; её можно сменить через 'hermes model'. Если эта модель не ответит, "
    "Hermes может повторить запрос на вашей основной модели."
)
GATE_RU = ("Для коротких проверок текущее досье и новые сообщения чата уходят в TypeSafe Jev через тот же AI Gateway, "
           "без хранения данных.")
CLAIRIC_LINES = {
    "en": ("New messages also go to Cloudflare Workers AI (Clairic, a preview model) to pick out dates, amounts and "
           "contact details for the writer."),
    "ru": ("Новые сообщения также уходят в Cloudflare Workers AI (Clairic, тестовая модель): она выделяет для досье "
           "даты, суммы и контакты."),
}


def cfg(**changes: Any) -> SimpleNamespace:
    """The ``DossierConfig`` fields the enable lines read: the default task route, both extra processors off."""
    base = {"provider": "ai-gateway", "model": "google/gemini-3.8-flash", "task_registered": True,
            "legacy_env": False, "gate": "off", "clairic": "off"}
    return SimpleNamespace(**{**base, **changes})


def test_the_legacy_auto_route_names_the_model_it_sends(dv: ModuleType) -> None:
    lines = dv.enable_lines(cfg(provider="auto", legacy_env=True), "en")

    assert "auto / google/gemini-3.8-flash" in lines[0] and "your main model" not in lines[0]
    assert "your main model" in dv.enable_lines(cfg(provider="auto", model=None), "en")[0]


def test_enable_confirm_in_english(dv: ModuleType) -> None:
    text, yes, cancel = dv.enable_confirm(cfg(), "en")

    assert (yes, cancel) == ("Enable", "Cancel")
    assert text == (
        "Turn on dossiers?\n\nThe text of ALL private chats will be sent to ai-gateway / google/gemini-3.8-flash to "
        f"build a dossier for each contact and your persona.\n\n{ROUTE_EN}\n\nDossiers are stored locally. You can "
        "turn them off at any time; stored dossiers are kept."
    )


def test_enable_confirm_in_russian_with_both_extra_processors(dv: ModuleType) -> None:
    text, yes, cancel = dv.enable_confirm(cfg(gate="on", clairic="on"), "ru")

    assert (yes, cancel) == ("Включить", "Отмена")
    assert text.startswith("Включить досье?\n\nТекст ВСЕХ личных чатов будет отправляться в ai-gateway / ")
    assert f"\n\n{ROUTE_RU}\n\n{GATE_RU}\n\n{CLAIRIC_LINES['ru']}\n\n" in text


@pytest.mark.parametrize("gate", ["on", "off", "no_key", "not_gateway", "bad_base_url"])
@pytest.mark.parametrize("clairic", ["on", "off", "no_key"])
def test_enable_lines_name_an_extra_processor_only_when_it_runs(dv: ModuleType, gate: str, clairic: str) -> None:
    for lang, gate_line in (("en", GATE_EN), ("ru", GATE_RU)):
        lines = dv.enable_lines(cfg(gate=gate, clairic=clairic), lang)

        assert (gate_line in lines) is (gate == "on")
        assert (CLAIRIC_LINES[lang] in lines) is (clairic == "on")


def test_the_message_check_line_says_the_dossier_goes_to_typesafe(dv: ModuleType) -> None:
    assert "current dossier" in GATE_EN and "текущее досье" in GATE_RU  # not only the new messages
    assert dv.enable_lines(cfg(gate="on"), "en")[-1] == GATE_EN


@pytest.mark.parametrize(("changes", "route"), [
    ({}, True),
    ({"provider": "auto", "model": "main model"}, True),
    ({"legacy_env": True, "provider": "gemini", "model": "gemini-3.8-flash"}, False),  # TELESEC_DOSSIER_PROVIDER
    ({"task_registered": False}, False),  # an older Hermes: the explicit fallback route
])
def test_the_task_route_sentence_only_when_the_task_is_in_use(dv: ModuleType, changes: dict[str, Any],
                                                              route: bool) -> None:
    for lang, sentence in (("en", ROUTE_EN), ("ru", ROUTE_RU)):
        lines = dv.enable_lines(cfg(**changes), lang)
        assert (sentence in lines) is route
        assert (sentence in dv.enable_confirm(cfg(**changes), lang)[0]) is route


@pytest.mark.parametrize(("lang", "shown"), [("en", "sent to your main model to build"),
                                              ("ru", "будет отправляться в вашу основную модель, чтобы")])
def test_the_auto_route_has_a_localized_name(dv: ModuleType, lang: str, shown: str) -> None:
    text = dv.enable_confirm(cfg(provider="auto", model="main model"), lang)[0]

    assert shown in text and "auto" not in text and "main model /" not in text


def test_enable_lines_name_the_model_safely(dv: ModuleType) -> None:
    for provider, model, shown in [("", "provider default", "provider default"), ("ai-gateway", "", "ai-gateway"),
                                   ("", "", "?")]:
        assert f"will be sent to {shown} to build" in dv.enable_lines(cfg(provider=provider, model=model), "en")[0]
    line = dv.enable_lines(cfg(provider="ai-gateway\u202e", model="x" * 300), "en")[0]
    assert "\u202e" not in line and "…" in line
    assert "TELESEC_DOSSIER_PROVIDER" not in dv.enable_confirm(cfg(legacy_env=True), "en")[0]  # no blocked state (O4)


# ---------------------------------------------------------------------------
# The /dossier post
# ---------------------------------------------------------------------------


def test_post_in_english(dv: ModuleType) -> None:
    html = dv.post_html("**Mira, your design lead.**\n\n## Agreements\n- The **Orbit** review is on Friday [m12, m14].\n"
                        "- Mira sends the mockups [m15–m17]", NOW)

    assert html == (
        "<h3>Dossier</h3><p><b>Mira, your design lead.</b></p><h4>Agreements</h4>"
        "<ul><li>The <b>Orbit</b> review is on Friday.</li><li>Mira sends the mockups</li></ul>"
        "<p><i>Prepared automatically from this chat on 20 October 2026; it may contain mistakes.</i></p>"
    )
    check_html(dv, html)


def test_post_in_russian(dv: ModuleType) -> None:
    html = dv.post_html("**Мира, ваш дизайнер.**\n\n## Планы\n- Встреча в пятницу [m12].", NOW)

    assert html == (
        "<h3>Досье</h3><p><b>Мира, ваш дизайнер.</b></p><h4>Планы</h4><ul><li>Встреча в пятницу.</li></ul>"
        "<p><i>Составлено автоматически по этой переписке 20 октября 2026; возможны неточности.</i></p>"
    )


@pytest.mark.parametrize(
    ("document", "lang"),
    [
        ("абв defghij", "ru"),  # 3 of 10 letters are Cyrillic: exactly 30 %
        ("аб cdefghij", "en"),  # 20 %
        ("абв defghij [m1] [m22, m333]", "ru"),  # refs are not letters of the document
        ("Orbit: встреча в пятницу, 18:00", "ru"),
        ("Orbit review on Friday, встреча", "en"),
        ("12:00 / 5 000 ₽", "en"),  # no letters at all
        ("", "en"),
    ],
)
def test_post_language_follows_the_cyrillic_share(dv: ModuleType, document: str, lang: str) -> None:
    title = re.match(r"<h3>([^<]*)</h3>", dv.post_html(document, NOW))[1]

    assert title == s(dv, lang, "post_title")


@pytest.mark.parametrize(
    ("at", "en", "ru"),
    [
        (datetime(2026, 1, 1, 0, 5), "1 January 2026", "1 января 2026"),
        (datetime(2026, 3, 8, 12, 0, tzinfo=UTC), "8 March 2026", "8 марта 2026"),
        (datetime(2026, 5, 9, 9, 0, tzinfo=UTC), "9 May 2026", "9 мая 2026"),
        (datetime(2026, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=3))), "31 December 2026", "31 декабря 2026"),
    ],
)
def test_post_date_is_the_local_date_with_month_names(dv: ModuleType, at: datetime, en: str, ru: str) -> None:
    assert f"from this chat on {en};" in dv.post_html("Plain English document.", at)
    assert f"по этой переписке {ru};" in dv.post_html("Краткое русское резюме.", at)


def test_month_tables(dv: ModuleType) -> None:
    assert len(s(dv, "en", "months")) == len(s(dv, "ru", "months")) == 12
    assert s(dv, "ru", "months")[2] == "марта" and s(dv, "ru", "months")[7] == "августа"  # genitive


def test_the_post_adds_only_its_title_and_a_dated_footer(dv: ModuleType) -> None:
    # Fixed text plus a date: no owner-only metadata (model, cost, update time, counts or the note label).
    for document, lang in (("- Plain English point.", "en"), ("- Пункт по-русски.", "ru")):
        fixed = dv.post_html(document, NOW).replace(dv.markdown_html(document), "")
        footer = s(dv, lang, "post_footer").format(date=dv._date(NOW, lang))
        assert fixed == f"<h3>{s(dv, lang, 'post_title')}</h3><p><i>{footer}</i></p>"
    assert (s(dv, "en", "post_title"), s(dv, "ru", "post_title")) == ("Dossier", "Досье")


def test_a_full_size_post_fits(dv: ModuleType) -> None:
    line = "- **Встреча по Orbit** перенесена на пятницу, 18:00 [m123]\n"
    document = (line * 80)[: dv._doc.DOC_MAX_UNITS]

    html = dv.post_html(document, NOW)

    check_html(dv, html)
    assert html.startswith("<h3>Досье</h3><ul><li><b>Встреча по Orbit</b>")


# ---------------------------------------------------------------------------
# The owner's message when nothing was posted
# ---------------------------------------------------------------------------

REFUSALS = {
    "en": {
        "disabled": "Dossiers are off. Turn them on in Settings (/telesec).",
        "no_model": "The dossier needs a refresh first, and the model is not connected.",
        "history_off": "History is off, so deleted messages can't be tracked. Turn history on in Settings (/telesec).",
        "chat_off": "Dossier is off for this chat.",
        "window": "The contact hasn't written in the last 24 hours, and Telegram lets a bot post only within 24 "
        "hours of their last message.",
        "unqualified": "No dossier yet: it needs messages from both you and the contact.",
        "empty": "The dossier has nothing worth keeping yet.",
        "busy": "The dossier for this chat is already being posted.",
        "changed": "Messages changed while the dossier was being refreshed. Send /dossier again.",
        "failed": "The dossier could not be prepared (TimeoutError).",
        # A refusal while the window was not known open: the 24 hours are only a possible cause.
        "send_refused": "Telegram did not accept the dossier (TimeoutError). One possible cause: a bot can post in a "
        "Business chat only if the contact wrote there in the last 24 hours.",
        "send_failed": "Telegram did not accept the dossier (TimeoutError).",
        "send_unsure": "Telegram did not confirm it (TimeoutError). Check the chat before you send /dossier again.",
    },
    "ru": {
        "disabled": "Досье выключены. Включите их в Настройках (/telesec).",
        "no_model": "Досье нужно сначала обновить, а модель не подключена.",
        "history_off": "История выключена, поэтому удалённые сообщения не отслеживаются. Включите историю в "
        "Настройках (/telesec).",
        "chat_off": "Досье для этого чата выключено.",
        "window": "Собеседник не писал последние 24 часа, а Telegram разрешает боту писать только в течение 24 "
        "часов после его последнего сообщения.",
        "unqualified": "Досье пока нет: нужны сообщения и от вас, и от собеседника.",
        "empty": "В досье пока нечего сохранить.",
        "busy": "Досье для этого чата уже отправляется.",
        "changed": "Пока обновлялось досье, сообщения изменились. Отправьте /dossier ещё раз.",
        "failed": "Не удалось подготовить досье (TimeoutError).",
        "send_refused": "Telegram не принял досье (TimeoutError). Возможная причина: бот может писать в бизнес-чат, "
        "только если собеседник писал туда за последние 24 часа.",
        "send_failed": "Telegram не принял досье (TimeoutError).",
        "send_unsure": "Telegram не подтвердил отправку (TimeoutError). Проверьте чат, прежде чем отправлять "
        "/dossier ещё раз.",
    },
}
HEADS = {"en": "Nothing was posted to the chat with Mira Tanaka.", "ru": "В чат «Mira Tanaka» ничего не отправлено."}
# A send that timed out may be in the chat, so its head never says that nothing was posted.
UNSURE_HEADS = {"en": "The dossier may have been posted to the chat with Mira Tanaka.",
                "ru": "Досье, возможно, отправлено в чат «Mira Tanaka»."}


def test_post_reasons_are_exactly_the_v2_set(dv: ModuleType) -> None:
    assert dv.POST_REASONS == (
        "disabled",
        "no_model",
        "history_off",
        "chat_off",
        "window",
        "unqualified",
        "empty",
        "busy",
        "changed",
        "failed",
        "send_refused",
        "send_failed",
        "send_unsure",
    )
    assert set(REFUSALS["en"]) == set(REFUSALS["ru"]) == set(dv.POST_REASONS)


@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize("reason", list(REFUSALS["en"]))
def test_post_refusal_for_every_reason(dv: ModuleType, lang: str, reason: str) -> None:
    text = dv.post_refusal(reason, lang, name=NAME, error="TimeoutError")

    head = UNSURE_HEADS[lang] if reason == "send_unsure" else HEADS[lang]
    assert text == f"{head}\n{REFUSALS[lang][reason]}"
    assert "<" not in text and "\u202e" not in text


def test_english_chat_off_keeps_its_sentence(dv: ModuleType) -> None:
    assert "Dossier is off for this chat." in dv.post_refusal("chat_off", "en", name=NAME)


def test_post_refusal_cleans_the_name_and_the_error(dv: ModuleType) -> None:
    text = dv.post_refusal("failed", "en", name="\u202eMira\n" + "x" * 100, error="Bad\nError" + "y" * 100)

    head, line = text.split("\n")
    assert head.startswith("Nothing was posted to the chat with Mira x") and head.endswith("….")
    assert line.startswith("The dossier could not be prepared (Bad Error") and line.endswith("…).")
    assert dv.post_refusal("failed", "en", name="", error=None) == (
        "Nothing was posted to the chat with ?.\nThe dossier could not be prepared (?)."
    )


@pytest.mark.parametrize("reason", ["blocked", "persona", "corrupt", "missing", "unchecked", "nope"])
def test_removed_and_unknown_reasons_are_errors(dv: ModuleType, reason: str) -> None:
    with pytest.raises(ValueError):
        dv.post_refusal(reason, "en", name=NAME)


# ---------------------------------------------------------------------------
# Strings
# ---------------------------------------------------------------------------


def walk(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        return {key: item for name, sub in value.items() for key, item in walk(sub, f"{path}.{name}").items()}
    return {path: value}


def test_english_and_russian_have_the_same_keys(dv: ModuleType) -> None:
    en, ru = walk(dv.STRINGS["en"]), walk(dv.STRINGS["ru"])

    assert set(en) == set(ru)
    for key, value in en.items():
        if key.startswith(".forms."):  # (one, many) in English, (one, few, many) in Russian
            assert (len(value), len(ru[key])) == (2, 3)
        elif isinstance(value, tuple):
            assert len(value) == len(ru[key])


def test_strings_are_html_safe_and_named_for_v2(dv: ModuleType) -> None:
    for lang in ("en", "ru"):
        for key, value in walk(dv.STRINGS[lang]).items():
            for text in value if isinstance(value, tuple) else (value,):
                assert isinstance(text, str) and text, key
                assert not re.search(r'[<>&"]', text), (key, text)
    assert "st_blocked" not in dv.STRINGS["en"] and "blocked" not in dv.STRINGS["en"]["post"]  # O4
    assert not any("TELESEC_DOSSIER_PROVIDER" in str(value) for value in walk(dv.STRINGS["en"]).values())
    assert dv.STRINGS["en"]["note_label"] == "Your note (not posted)"


@pytest.mark.parametrize(
    ("code", "lang"),
    [
        ("ru", "ru"),
        ("ru-RU", "ru"),
        ("RU", "ru"),
        ("en", "en"),
        ("uk", "en"),
        ("", "en"),
        (None, "en"),
        ("de-ru", "en"),
    ],
)
def test_lang_for(dv: ModuleType, code: str | None, lang: str) -> None:
    assert dv.lang_for(code) == lang


def test_snapshot_has_no_blocked_state(dv: ModuleType) -> None:
    names = {field.name for field in fields(dv.DossierSnapshot)}

    assert "blocked" not in names
    assert {
        "target",
        "name",
        "chat_id",
        "connection",
        "doc",
        "corrupt",
        "enabled",
        "history_enabled",
        "chat_off",
        "llm_ready",
        "refreshing",
        "failure",
        "unqualified",
        "manage_global",
        "persona_due",
        "in_history",
    } == names


# ---------------------------------------------------------------------------
# Size budget (J.6): the plain fallback fits without being clipped
# ---------------------------------------------------------------------------


def dense_document(dv: ModuleType, line: str) -> str:
    text = line * (dv._doc.DOC_MAX_UNITS // len(line) + 1)
    return text[: dv._doc.DOC_MAX_UNITS]


WORST_DOCUMENTS = {
    "bold bullets with refs": "- **Фаза 2 проекта Orbit** сдвинута на 28 октября, ревью в среду [m123456]\n",
    "plain bullets": "- " + "щ" * 70 + "\n",
    "plain lines": "Northwind Demo pays the Orbit invoices on the fifth day of each month.\n",
}


@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize("target", ["contact", "persona"])
@pytest.mark.parametrize("kind", list(WORST_DOCUMENTS))
def test_worst_case_screen_fits(dv: ModuleType, lang: str, target: str, kind: str) -> None:
    document = dense_document(dv, WORST_DOCUMENTS[kind])
    assert dv._doc.units(document) == dv._doc.DOC_MAX_UNITS
    note = "\n".join(["Звонить только после шести, по будням. Не упоминать Nova."] * 22)[:1200]
    builder = contact_doc if target == "contact" else persona_doc
    doc = builder(dv, document=document, note=note, messages=10**9)
    if target == "persona":
        doc["chats"] = [{"business_connection_id": CONN, "chat_id": i} for i in range(10**3)]
    name = "Ж" * dv.NAME_CHARS

    view = dv.render_overview(snap(dv, target, doc=doc, name=name, failure="E" * 64, manage_global=True), lang)

    check_view(dv, view)
    assert "<tg-time" in view.html and "<blockquote>" in view.html and view.html.endswith("</i></p>")
    unclipped = dv.html_to_text(view.html, limit=10**6)
    assert dv._units(unclipped) <= dv.TEXT_MAX_CHARS and view.text == unclipped


@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize("kind", list(WORST_DOCUMENTS))
def test_a_converted_document_over_the_limit_shows_its_start_and_names_show(
    dv: ModuleType, lang: str, kind: str
) -> None:
    line = WORST_DOCUMENTS[kind]
    document = line * (3 * dv._doc.DOC_MAX_UNITS // len(line))  # the migration never cuts a v1 fact
    note = "\n".join(["Звонить только после шести, по будням. Не упоминать Nova."] * 22)[:1200]
    doc = converted_doc(dv, document=document, note=note)
    name = "Ж" * dv.NAME_CHARS

    connection = "bc-" + "c" * 61  # the longest id the screen shows whole
    view = dv.render_overview(snap(dv, doc=doc, name=name, failure="E" * 64, manage_global=True,
                                   connection=connection), lang)

    check_view(dv, view)
    unclipped = dv.html_to_text(view.html, limit=10**6)
    assert dv._units(unclipped) <= dv.TEXT_MAX_CHARS and view.text == unclipped
    # The connection picks this owner's chat: the contact's chat id is the same under every owner.
    target = f"--connection {connection} --chat {CHAT}"
    assert s(dv, lang, "st_clipped").format(target=target) in view.html
    assert doc["document"] == document  # the stored text stays whole; 'show' prints all of it


# ---------------------------------------------------------------------------
# Block counting, plain text and the rich payload (kept from v1)
# ---------------------------------------------------------------------------


def test_block_counter(dv: ModuleType) -> None:
    assert dv.count_blocks("<p>a</p><ul><li>x</li><li>y</li></ul>") == 6  # each item's text is a paragraph
    assert dv.count_blocks("<li>text<details><summary>S</summary><p>x</p></details></li>") == 4
    assert dv.count_blocks("<blockquote>q<cite>c</cite></blockquote>") == 2
    assert dv.count_blocks("<blockquote><b>Note</b><br>a<br>b</blockquote>") == 2
    assert dv.count_blocks("loose <b>root</b> text<hr/>") == 2


def test_html_to_text(dv: ModuleType) -> None:
    html = "<h3>A &amp; B</h3><p><i>head</i></p><ul><li>x</li><li>y</li></ul><blockquote><b>N</b><br>q</blockquote>"
    assert dv.html_to_text(html) == "A & B\nhead\n• x\n• y\n> N\n> q"
    assert dv.html_to_text("<p>" + "w" * 5000 + "</p>") == "w" * 4095 + "…"
    clipped = dv.html_to_text("<p>" + "😀" * 3000 + "</p>")  # 2 UTF-16 units each
    assert clipped == "😀" * 2047 + "…" and dv._units(clipped) == 4095


def test_rich_message_payload(dv: ModuleType) -> None:
    from telegram.request import RequestData
    from telegram.request._requestparameter import RequestParameter

    view = dv.render_overview(snap(dv), "ru")
    assert dv.rich_message(view) == {"html": view.html, "skip_entity_detection": True}
    # bot.do_api_request(..., api_kwargs={"rich_message": ...}) JSON-encodes the nested object as one field.
    data = RequestData(
        [
            RequestParameter.from_input("rich_message", dv.rich_message(view)),
            RequestParameter.from_input("chat_id", CHAT),
        ]
    )
    assert json.loads(data.json_parameters["rich_message"]) == {"html": view.html, "skip_entity_detection": True}


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_module_is_pure() -> None:
    """Module-level imports are the stdlib, dossier_doc and dossier_writer, and no function imports anything."""
    source = (Path(__file__).resolve().parent.parent / "dossier_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    relative, absolute = set(), set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            absolute |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level:
            relative |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            absolute.add(str(node.module).split(".")[0])
    assert relative == {"dossier_doc", "dossier_writer"}
    assert absolute <= set(sys.stdlib_module_names) | {"__future__"}, absolute - set(sys.stdlib_module_names)

    lazy = [
        (function.name, alias.name)
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert lazy == []
    for forbidden in ("open(", "environ", ".now(", ".today(", "utcnow", "time.time", "print(", "logger", "logging"):
        assert forbidden not in source, forbidden
