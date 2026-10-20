#!/usr/bin/env python3
"""Write the rich message HTML of the dossier views over a fictional contact, for real Telegram screenshots.

Builds one fictional contact dossier (Mira Tanaka, a design lead; in Russian Ольга
Миронова, a kitchen designer) and the owner persona as real ``document.json`` files
in a temporary HERMES_HOME, with the owner note set. The documents are written by
hand in the shape the writer model returns, so they show the layout, not the quality
of a model. Then it reads them back with the real loaders and runs the real views:
the owner panel's Dossier screen (``dossier_view.render_overview``) and the
``/dossier`` post in the chat (``dossier_view.post_html``), in English and Russian.
Each view's exact Bot API rich message HTML goes to
``docs/images/src/dossier-{panel,post}-{en,ru}.html``. Send one with
sendRichMessage to take a real Telegram screenshot. This script does not make them.

Run from the repository root:

    uv run python scripts/render-dossier-screenshots.py

Nothing here touches real history or dossiers: HERMES_HOME is a temporary directory,
and no model or network is called.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "docs" / "images" / "src"
sys.path.insert(0, str(ROOT / "tests"))

import test_history as th  # needs tests/ on sys.path

CONNECTION = "business-demo"
CHAT_ID = 4242
OTHER_CHAT_ID = 4343
NOW = datetime(2026, 10, 20, 11, 32, tzinfo=UTC)
CHECKED = datetime(2026, 10, 20, 9, 40, tzinfo=UTC)
UPDATED = datetime(2026, 10, 19, 17, 5, tzinfo=UTC)

# The document as the writer returns it. Refs such as [m90] stay on disk and in the CLI; Telegram drops them.
LANGUAGES = {
    "en": {
        "tz": "Europe/Berlin",
        "name": "Mira Tanaka",
        "messages": 312,
        "document": """**Mira is your design lead at Northwind Demo, and you work together on the Orbit redesign.**

## Project
- Orbit: new onboarding and settings screens; Mira owns the visual design, you own the copy.
- Design review moved to **Wednesday, 28 October**, 10:00 [m90]
- Final files are due on **6 November**; the developers start on 9 November.

## Agreements
- You send the screen texts by **Friday, 23 October** [m101]
- Mira shares the Figma link with the whole team after the review.
- Budget for the extra illustrations: **€1,200**, approved by her manager [m77]

## How to work with her
- Prefers short voice messages to long texts; replies fastest before noon.
- Wants feedback as a list, one point per screen.

## Contact details
- New work number: +1 202 555-0111 [m112]
- Mail: mira.tanaka@example.com
""",
        "note": "We stop working on Orbit after December. Don't remind her about the old logo.",
        "persona": """## Language and tone
- Writes in English with clients and in Russian with friends; switches when the other person does.
- Friendly but brief with clients: «Hi …, thanks for the update!»

## Length and form
- One to three short sentences per message; splits longer answers into two or three messages.
- Lists with dashes for plans and options.

## Habits
- Confirms with «Sounds good» or «Done»; declines politely and offers a new time.
- Rarely uses emoji, never with new clients.
""",
        "summary": """## Project
- Orbit: new onboarding and settings screens.
- Design review on **Wednesday, 28 October**, 10:00.
- Final files are due on **6 November**.

## Agreements
- The owner sends the screen texts by **Friday, 23 October**.
- Mira shares the Figma link with the team after the review.
- Extra illustrations: **€1,200**, approved.

## Open questions
- Who presents the review to the developers.
""",
    },
    "ru": {
        "tz": "Europe/Moscow",
        "name": "Ольга Миронова",
        "messages": 148,
        "document": """**Ольга — дизайнер кухонь в студии «Кедр Демо», она ведёт ваш заказ на кухню.**

## Заказ
- Кухня 12 м², фасады из дуба, столешница из камня.
- Замер прошёл; эскиз готов, ждёт вашего ответа [m41]
- Монтаж перенесён на **14 ноября**, с 9:00 [m58]

## Договорённости
- Предоплата **60 000 ₽** до **25 октября**, остаток после монтажа [m47]
- Вы присылаете фото стены с розетками до пятницы.
- Ольга бесплатно меняет ручки на чёрные [m52]

## Как с ней общаться
- Отвечает быстрее голосом; после 19:00 не пишет.
- Просит согласовывать изменения одним сообщением, списком.

## Контакты
- Рабочий телефон: +1 202 555-0112
- Почта для документов: olga.mironova@example.com
""",
        "note": "Договор подписан на меня, а не на жену. Скидку не обсуждаем.",
        "persona": """## Язык и тон
- С клиентами пишет на «вы», коротко и вежливо: «Добрый день, …! Спасибо».
- С друзьями переходит на «ты» и пишет короче.

## Длина и форма
- Одно-три коротких предложения; длинный ответ делит на несколько сообщений.
- Планы и варианты пишет списком через дефис.

## Привычки
- Соглашается словами «Отлично» и «Договорились»; отказывает мягко и предлагает другое время.
- Эмодзи почти не ставит.
""",
        "summary": """## Заказ
- Кухня 12 м², фасады из дуба.
- Монтаж **14 ноября**, с 9:00.

## Договорённости
- Предоплата **60 000 ₽** до **25 октября**, остаток после монтажа.
- Фото стены с розетками — до пятницы.
- Ручки меняются на чёрные без доплаты.

## Открытые вопросы
- Нужен ли второй замер перед монтажом.
""",
    },
}


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_documents(ds, doc_module, spec: dict) -> None:
    """The contact and the persona as stored after a few refreshes, written atomically like the worker does."""
    contact = doc_module.new_contact(CONNECTION, CHAT_ID, datetime(2026, 9, 1, 8, tzinfo=UTC))
    contact.update({
        "updated_at": _iso(UPDATED), "checked_at": _iso(CHECKED), "revision": 9, "document": spec["document"],
        "note": spec["note"], "note_updated_at": _iso(UPDATED), "messages": spec["messages"],
        "window_start": "2026-07-22T00:00:00Z", "floor": "2026-07-22T00:00:00Z",
        "last_call": {"provider": "ai-gateway", "model": "google/gemini-3.8-flash", "at": _iso(CHECKED)},
    })
    contact["note_sha"] = doc_module.note_sha(contact)
    persona = doc_module.new_persona(datetime(2026, 9, 1, 8, tzinfo=UTC))
    persona.update({
        "updated_at": _iso(UPDATED), "checked_at": _iso(CHECKED), "revision": 3, "document": spec["persona"],
        "messages": 280, "newest_at": _iso(CHECKED),
        "chats": [{"business_connection_id": CONNECTION, "chat_id": CHAT_ID},
                  {"business_connection_id": CONNECTION, "chat_id": OTHER_CHAT_ID}],
        "last_call": {"provider": "ai-gateway", "model": "google/gemini-3.8-flash", "at": _iso(UPDATED)},
    })
    with ds._data_lock():
        doc_module.write_doc(ds.contact_dir(CONNECTION, CHAT_ID), contact)
        doc_module.write_doc(ds.persona_dir(), persona)


def render_views(language: str) -> dict[str, object]:
    spec = LANGUAGES[language]
    with tempfile.TemporaryDirectory() as home, pytest.MonkeyPatch.context() as mp:
        plugin = th._load_plugin_module(mp, Path(home))
        mp.setitem(sys.modules, "hermes_time", SimpleNamespace(get_timezone_name=lambda: spec["tz"]))
        for name in list(os.environ):
            # The shell's own Telesec settings must not change the pictures.
            if name.startswith(("TELESEC_", "HERMES_TELEGRAM_BUSINESS_")):
                mp.delenv(name)
        ds = plugin._dossier_support
        doc_module = sys.modules[f"{plugin.__name__}.dossier_doc"]
        view_module = sys.modules[f"{plugin.__name__}.dossier_view"]
        owner_panel = sys.modules[f"{plugin.__name__}.owner_panel"]
        write_documents(ds, doc_module, spec)

        # The panel state of a working setup: dossiers and history on, the model connected, the owner verified.
        snapshot = view_module.DossierSnapshot(
            target="contact", name=spec["name"], chat_id=CHAT_ID, doc=ds.load_contact(CONNECTION, CHAT_ID),
            manage_global=True,
        )
        panel = view_module.render_overview(snapshot, language)
        persona = view_module.render_overview(
            view_module.DossierSnapshot(target="persona", doc=ds.load_persona(), manage_global=True),
            language,
        )
        post = view_module.post_html(spec["summary"], NOW.astimezone(ZoneInfo(spec["tz"])))  # the owner's date
        assert view_module.fits(panel.html) and view_module.fits(post), language
    return {
        "panel": panel.html, "panel_text": panel.text, "post": post, "post_text": view_module.html_to_text(post),
        "persona": persona.html,
        "buttons": [*([label for label, _ in row] for row in panel.buttons), [owner_panel.tr("back", language)]],
    }


# --- Rich message sources ------------------------------------------------------------------------

HEADER = """<!--
  Generated by scripts/render-dossier-screenshots.py; do not edit by hand.
  The exact Bot API rich message HTML of the real Telesec view ({what}), over a
  fictional contact and dossier. Send it with sendRichMessage to take a real
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
            for name in ("panel", "post"):
                print(f"--- {language} {name}\n{views[name]}\n--- {language} {name} (plain fallback)\n"
                      f"{views[name + '_text']}")
            print(f"--- {language} persona\n{views['persona']}")
            print(f"--- {language} buttons\n{views['buttons']}")
        for name, what in (("panel", "owner panel, Dossier screen"), ("post", "/dossier summary in the chat")):
            page = SRC_DIR / f"dossier-{name}-{language}.html"
            page.write_text(HEADER.format(what=what) + views[name] + "\n", encoding="utf-8")
            print(page.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
