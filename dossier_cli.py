"""``hermes telesec dossier ...``: read contact dossiers and the owner persona, and write owner notes.

A dossier is one Markdown document per contact that the model rewrites; the owner corrects it with
a note. Every text line goes through ``history_support._escape_terminal_text``: dossier text is
derived from chat messages and must never drive the terminal. ``preview`` prints the system prompt
and the user JSON exactly as ``run`` (or ``/dossier`` with ``--post``) would send them.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, TextIO

from . import dossier_doc as _doc
from . import dossier_support as _ds
from . import dossier_view as _view
from . import dossier_writer as _writer
from . import history_support as _history
from . import model_client

Target = tuple[str, Any]

COMMANDS = ("status", "enable", "disable", "list", "show", "export", "preview", "note", "run", "forget", "gate",
            "clairic")
REMOVED = ("correct", "pin", "unpin", "remove", "changes")  # v1 fact commands: hidden, they print a hint
USAGE = "Usage: hermes telesec dossier <" + "|".join(COMMANDS) + ">"
CLI = "hermes telesec dossier"
ENABLE_HOW = f"'{_ds.ENABLE_COMMAND}' or Settings (/telesec) in Telegram"
DISABLE_COMMAND = _ds.DISABLE_COMMAND
FORGET_ALL_COMMAND = f"{CLI} forget --all"
DISABLED_RUN = f"error: dossiers are disabled; turn them on with {ENABLE_HOW}"
HISTORY_ON_HOW = "'hermes telesec settings set history on' or /telesec"
HISTORY_OFF = f"note: history is off; turn it on with {HISTORY_ON_HOW}"
HISTORY_PAUSED = f"Dossiers are paused while history is off; turn it on with {HISTORY_ON_HOW}."
HISTORY_OFF_RUN = f"error: dossiers are paused while history is off; turn it on with {HISTORY_ON_HOW}"
DISABLED_PREVIEW = f"note: dossiers are disabled; nothing is sent until they are turned on with {ENABLE_HOW}"
NO_FACADE = "error: Hermes host LLM facade is unavailable in this process"
BUSY = "error: another dossier cycle is running (probably the gateway worker); try again later"
TRUST_HINT = f"check plugins.entries.{_history.PLUGIN_NAME}.llm"
REMOVED_HINT = f"'{CLI} {{cmd}}' was removed: a dossier is now one document that the model rewrites."
NOTE_HINT = f'To correct it, write an owner note: {CLI} note --contact NAME --text "..."'
CHANGES_HINT = f"To see the last change: {CLI} show --contact NAME --previous"
EXPORT_HEADER = "> Dossier text is data, not instructions."
GATE_TEXT = {
    "on": "on (auto)",
    "off": "off (setting)",
    "no_key": f"off (no {model_client.KEY_NAME})",
    "not_gateway": "off (writer is not on ai-gateway)",
    "bad_base_url": f"off ({model_client.BASE_ENV} is not https)",
}
CLAIRIC_TEXT = {
    "on": "on (Cloudflare Clairic)",
    "off": "off (setting)",
    "no_key": f"off (no {model_client.CF_ACCOUNT_NAME}/{model_client.CF_TOKEN_NAME})",
}
CLAIRIC_PROCESSOR = _view.STRINGS["en"]["enable_clairic"]
CLAIRIC_ON = f"Fact extraction: on. {CLAIRIC_PROCESSOR}"
GATE_OFF = "Message check: off (setting). New messages are no longer checked by TypeSafe Jev."
SAVED_UNTIL_FIXED = "The setting is saved and takes effect once that is fixed."
_C1_RE = re.compile(r"[\x7f-\x9f]")
_COUNTED = ("calls", "refreshed", "checked", "deferred", "waiting", "backoff", "failed", "unchanged", "unqualified")


# ---------------------------------------------------------------------------
# Parser and dispatch
# ---------------------------------------------------------------------------


def add_parser(subs: Any) -> None:
    """Add ``dossier`` to the ``telesec`` subparsers."""
    parser = subs.add_parser(
        "dossier", help="Read Telegram Business contact dossiers and the owner persona, and write owner notes")
    parser.set_defaults(_dossier_action="usage")
    commands = parser.add_subparsers(dest="telegram_business_dossier_command",
                                     metavar="<" + "|".join(COMMANDS) + ">")

    def leaf(name: str, help_text: str | None, *, target: bool = True, all_: bool = False) -> argparse.ArgumentParser:
        # A leaf without help is left out of the command list.
        command = commands.add_parser(name, help=help_text) if help_text else commands.add_parser(name)
        command.set_defaults(_dossier_action=name)
        if target:
            command.add_argument("--connection")
            command.add_argument("--chat")
            command.add_argument("--contact")
            command.add_argument("--persona", action="store_true")
            if all_:
                command.add_argument("--all", action="store_true")
        return command

    leaf("status", "Show the dossier settings, model, usage, failures and the last cycle", target=False)
    leaf("enable", "Turn dossiers on for all private chats (sends their text to the model)", target=False)
    leaf("disable", "Turn dossiers off; stored dossiers are kept", target=False)
    leaf("list", "List stored dossiers", target=False)
    show = leaf("show", "Show a dossier with its owner note")
    show.add_argument("--previous", action="store_true", help="Show the version before the last rewrite")
    show.add_argument("--format", choices=["text", "json"], default="text")
    show.add_argument("--fact", help=argparse.SUPPRESS)  # removed in v2: prints the hint
    export = leaf("export", "Export a dossier as Markdown or JSON")
    export.add_argument("--format", choices=["markdown", "json"], default="markdown")
    preview = leaf("preview", "Print the exact input of the next model call (no call, no writes)")
    preview.add_argument("--post", action="store_true", help="Print what /dossier would send instead")
    note = leaf("note", "Show, write or clear the owner note the model follows")
    text = note.add_mutually_exclusive_group()
    text.add_argument("--text")
    text.add_argument("--clear", action="store_true")
    leaf("run", "Run one dossier cycle now (calls the model)")
    leaf("forget", "Delete a dossier and its owner note (history is untouched)", all_=True)
    gate = leaf("gate", "Show or set the message check (TypeSafe Jev)", target=False)
    gate.add_argument("mode", nargs="?", choices=_ds.GATE_MODES)
    clairic = leaf("clairic", "Show or set fact extraction (Cloudflare Clairic)", target=False)
    clairic.add_argument("mode", nargs="?", choices=_ds.CLAIRIC_MODES)
    for name in REMOVED:
        removed = leaf(name, None)
        # The v1 flags are accepted too: REMAINDER alone rejects a first argument that looks like an option.
        for flag in ("--fact", "--text", "--section", "--limit", "--format"):
            removed.add_argument(flag, help=argparse.SUPPRESS)
        removed.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)


def handle_cli(args: argparse.Namespace) -> int:
    handler = _ACTIONS.get(getattr(args, "_dossier_action", None))
    if handler is None:
        print(USAGE)
        return 1
    try:
        _ds.migrate_v1()
    except Exception as exc:  # noqa: BLE001 - the next cycle retries; the command still runs
        print(f"warning: old v1 dossier files could not be moved ({type(exc).__name__}); the next cycle tries again",
              file=sys.stderr)
    try:
        return handler(args)
    except (OSError, ValueError) as exc:
        print(f"error: {_esc(str(exc))}")
        return 1


# ---------------------------------------------------------------------------
# Output and formatting helpers
# ---------------------------------------------------------------------------


def _esc(text: Any) -> str:
    return _history._escape_terminal_text(str(text))


def _emit(lines: Iterable[str], stream: TextIO | None = None) -> None:
    for line in lines:
        print(_esc(line), file=stream or sys.stdout)


def _text_lines(text: str) -> list[str]:
    return text.splitlines() or [""]


def _c1_escaped(text: str) -> str:
    """JSON text with DEL and C1 controls written as JSON escapes: the same JSON value, safe for a terminal."""
    return _C1_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def _json_text(obj: Any) -> str:
    # json already escapes C0 controls; C1 controls would reach the terminal raw.
    return _c1_escaped(json.dumps(obj, ensure_ascii=False, indent=2))


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count:,} {word if count == 1 else plural or word + 's'}"


def _num(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usd(value: Any) -> str:
    amount = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    if amount == 0 or amount >= 0.01:
        return f"${amount:.2f}"
    return f"${amount:.4f}" if amount >= 0.0001 else f"${amount:.6f}"


def _seconds(duration_ms: int) -> str:
    seconds = duration_ms / 1000
    return f"{seconds:.1f}s" if seconds < 10 else f"{round(seconds)}s"


def _when(value: Any, *, utc: bool = True) -> str:
    at = _history._parse_datetime(value)
    if at is None:
        return "?"
    return f"{at.astimezone(UTC):%Y-%m-%d %H:%M}" + (" UTC" if utc else "")


# ---------------------------------------------------------------------------
# Targets, names and stored files
# ---------------------------------------------------------------------------


def _resolve_target(args: argparse.Namespace, *, allow_all: bool = False) -> Target:
    persona = bool(getattr(args, "persona", False))
    everything = allow_all and bool(getattr(args, "all", False))
    contact = getattr(args, "contact", None)
    chat = getattr(args, "chat", None)
    connection = getattr(args, "connection", None)
    has_contact = contact is not None or chat is not None
    if everything:
        if persona or has_contact or connection is not None:
            raise ValueError("--all cannot be combined with --contact, --chat, --connection or --persona")
        return ("all", None)
    if persona:
        if has_contact or connection is not None:
            raise ValueError("--persona cannot be combined with --contact or --chat")
        return ("persona", None)
    if not has_contact:
        raise ValueError("pass --contact, --chat, --persona or --all" if allow_all
                         else "pass --contact, --chat or --persona")
    try:
        entry = _history._resolve_history_entry(
            business_connection_id=connection, chat_id=None if chat is None else str(chat), contact=contact)
    except ValueError:
        if allow_all and connection is not None and chat is not None and contact is None:
            # forget only: an orphan dossier (chat gone from the catalog) is addressed by conn + chat.
            return ("contact", {"business_connection_id": str(connection), "chat_id": _chat_id(chat)})
        raise
    if entry.get("chat_type") != "private":
        raise ValueError("dossiers are built only for private chats")
    return ("contact", entry)


def _has_target(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "persona", False)) or any(
        getattr(args, name, None) is not None for name in ("contact", "chat", "connection"))


def _chat_id(raw: Any) -> Any:
    text = str(raw).strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else text


def _label(target: Target) -> str:
    if target[0] == "persona":
        return "the owner persona"
    entry = target[1]
    if "chat_type" in entry:
        return _history._catalog_entry_display_name(entry)
    return f"chat:{entry.get('chat_id')}"


def _ident(entry: dict[str, Any]) -> str:
    return f"{entry.get('business_connection_id')}:{entry.get('chat_id')}"


def _load(target: Target) -> dict[str, Any] | None:
    """The target's document or None; raises dossier_doc.DocError when it is corrupt."""
    if target[0] == "persona":
        return _ds.load_persona()
    return _ds.load_contact(target[1]["business_connection_id"], target[1]["chat_id"])


def _catalog() -> dict[str, dict[str, Any]]:
    """Private catalog entries by chat key; {} when the catalog cannot be read."""
    try:
        entries = _ds.private_entries()
    except (OSError, ValueError):
        return {}
    return {_ds.chat_key(e.get("business_connection_id"), e.get("chat_id")): e for e in entries}


@dataclass(frozen=True)
class _Stored:
    kind: str  # contact | persona
    key: str  # chat key (the directory under chats/), or "persona"
    doc: dict[str, Any] | None
    corrupt: bool
    backup: bool  # a v1-dossier.json is still kept
    entry: dict[str, Any] | None  # the catalog entry of a contact

    @property
    def built(self) -> bool:
        if self.doc is None:
            return False
        if self.kind == "persona":
            return self.doc.get("checked_at") is not None
        return self.doc.get("window_start") is not None

    @property
    def label(self) -> str:
        if self.kind == "persona":
            return "Owner persona"
        if self.entry is not None:
            return _history._catalog_entry_display_name(self.entry)
        if self.doc is not None:
            return f"chat:{self.doc.get('chat_id')}"
        return f"chat:{self.key.rpartition('/')[2]}"

    @property
    def ids(self) -> tuple[Any, Any] | None:
        source = self.entry or self.doc
        if self.kind == "persona" or source is None:
            return None
        return source.get("business_connection_id"), source.get("chat_id")


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(p.is_file() for p in path.iterdir())


def _read_stored(kind: str, key: str, path: Path, entry: dict[str, Any] | None) -> _Stored:
    try:
        doc, corrupt = _doc.read_doc(path), False
    except (OSError, ValueError):
        doc, corrupt = None, True
    return _Stored(kind, key, doc, corrupt, (path / _doc.V1_BACKUP).exists(), entry)


def _stored(catalog: dict[str, dict[str, Any]]) -> list[_Stored]:
    """Every stored contact dossier (sorted by name), then the persona. Reads only."""
    chats = _ds.dossier_root() / "chats"
    items = []
    if chats.is_dir():
        for path in chats.glob("*/*"):
            if _has_files(path):
                key = path.relative_to(chats).as_posix()
                items.append(_read_stored("contact", key, path, catalog.get(key)))
    items.sort(key=lambda item: (item.label.casefold(), item.key))
    if _has_files(_ds.persona_dir()):
        items.append(_read_stored("persona", "persona", _ds.persona_dir(), None))
    return items


def _failure_text(record: dict[str, Any]) -> str:
    return f"{record.get('error')} ×{_num(record.get('count')) or 1}"


# ---------------------------------------------------------------------------
# status / enable / disable / gate / clairic
# ---------------------------------------------------------------------------


def _since(settings: dict[str, Any]) -> str:
    if not settings.get("updated_at"):
        return ""
    return f" (since {_when(settings['updated_at'])} via {settings.get('updated_via') or 'unknown'})"


def _switch_line(settings: dict[str, Any]) -> str:
    if settings["enabled"]:
        return f"Dossiers: enabled{_since(settings)}"
    return f"Dossiers: disabled{_since(settings)}. Turn on with {ENABLE_HOW}."


def _model_line(cfg: _ds.DossierConfig) -> str:
    route = f"Model: {cfg.provider} / {cfg.model}"
    trust = f"needs plugins.entries.{_history.PLUGIN_NAME}.llm trust config"
    if cfg.legacy_env:
        return f"{route} (from {_ds.PROVIDER_ENV}/{_ds.MODEL_ENV}; {trust})"
    if not cfg.task_registered:
        return f"{route} (explicit route: the Telesec dossiers task is not registered; {trust})"
    if cfg.provider == "auto":
        return "Model: auto (main model)"
    details = ["Telesec dossiers task", f"reasoning {cfg.reasoning or 'provider default'}",
               f"max_tokens {cfg.max_tokens}" if cfg.max_tokens else "max_tokens not capped"]
    if cfg.provider.lower() in _writer.GATEWAY_ALIASES:
        details.append(f"zero data retention {'on' if cfg.zdr else 'off'}")
    return f"{route} ({'; '.join(details)})"


def _same_provider(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return a == b or (a in _writer.GATEWAY_ALIASES and b in _writer.GATEWAY_ALIASES)


def _last_answer_line(cfg: _ds.DossierConfig, items: list[_Stored]) -> str:
    calls = [item.doc["last_call"] for item in items
             if item.doc is not None and isinstance(item.doc.get("last_call"), dict)]
    calls = [call for call in calls if isinstance(call.get("at"), str)]
    if not calls:
        return "Last answer: none yet"
    last = max(calls, key=lambda call: call["at"])
    provider, model = (_ds.without_credentials(str(last.get(name) or "?")) for name in ("provider", "model"))
    line = f"Last answer: {provider} / {model}"
    if cfg.provider != "auto" and not (_same_provider(provider, cfg.provider)
                                       and model.strip().lower() == cfg.model.strip().lower()):
        line += " (host fallback: differs from the configured route)"
    return line


def _gate_line(cfg: _ds.DossierConfig) -> str:
    if cfg.gate == "on":
        return f"Message check: TypeSafe {model_client.JEV_MODEL}, {GATE_TEXT['on']}"
    return f"Message check: {GATE_TEXT.get(cfg.gate, cfg.gate)}"


def _stored_line(items: list[_Stored]) -> str:
    contacts = [item for item in items if item.kind == "contact"]
    details = [(sum(1 for i in contacts if i.doc is not None and not i.built), "waiting for first build"),
               (sum(1 for i in contacts if i.built and not i.doc["document"]), "empty"),
               (sum(1 for i in contacts if i.corrupt), "unreadable")]
    shown = ", ".join(f"{count} {name}" for count, name in details if count)
    persona = "yes" if any(item.kind == "persona" for item in items) else "no"
    notes = sum(1 for item in items if item.doc is not None and item.doc.get("note"))
    return (f"Stored: {_plural(len(contacts), 'contact dossier')}{f' ({shown})' if shown else ''}, "
            f"owner persona {persona}, {_plural(notes, 'owner note')}, {_plural(_ds.v1_backups(), 'v1 backup')}")


def _usage_lines(usage: dict[str, Any]) -> list[str]:
    lines = []
    for month in sorted(usage, reverse=True):
        parts, shown = usage[month], []
        writer, check, clairic = parts.get("writer"), parts.get("check"), parts.get("clairic")
        if writer:
            shown.append(f"writer {_plural(_num(writer.get('calls')), 'call')}, "
                         f"{_num(writer.get('input_tokens')):,} in / {_num(writer.get('output_tokens')):,} out tokens, "
                         f"{_usd(writer.get('cost_usd'))} ({_num(writer.get('unpriced'))} unpriced)")
        if check:
            shown.append(f"check {_plural(_num(check.get('calls')), 'call')}, {_usd(check.get('cost_usd'))}, "
                         f"{_num(check.get('deferred'))} deferred, {_plural(_num(check.get('errors')), 'error')}")
        if clairic:
            shown.append(f"fact extraction {_plural(_num(clairic.get('calls')), 'call')}, "
                         f"{_usd(clairic.get('cost_usd'))}, {_plural(_num(clairic.get('errors')), 'error')}")
        if shown:
            lines.append(f"Usage {month}: {'; '.join(shown)}")
    return lines or ["Usage: none yet"]


def _failures_line(failures: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> str:
    if not failures:
        return "Failures: none"
    shown = []
    for key, record in sorted(failures.items())[:5]:
        if key == "persona":
            name = "owner persona"
        elif key in catalog:
            name = _history._catalog_entry_display_name(catalog[key])
        else:
            name = f"chat:{key.rpartition('/')[2]}"
        shown.append(f"{name}: {_failure_text(record)}, last {_when(record.get('at'))}")
    more = ", …" if len(failures) > 5 else ""
    return f"Failures: {len(failures)} ({', '.join(shown)}{more})"


def _counts(report: Any) -> str:
    get = report.get if isinstance(report, dict) else lambda name: getattr(report, name)
    text = " ".join(f"{name}={_num(get(name))}" for name in _COUNTED)
    return f"{text} persona={get('persona')} cost={_usd(get('cost_usd'))}"


def _last_cycle_lines(last: Any) -> list[str]:
    if not isinstance(last, dict):
        return ["Last cycle: none yet"]
    trigger = last.get("trigger") or "unknown"
    lines = [(f"Last cycle: {last.get('outcome')} {_when(last.get('started_at'))} ({trigger}) in "
              f"{_seconds(_num(last.get('duration_ms')))}: {_counts(last)}")]
    if last.get("outcome") == "aborted_trust":
        lines.append(f"Last cycle aborted: model call denied by the Hermes trust gate; {TRUST_HINT}")
    return lines


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = _ds.load_config()
    settings = _ds.load_settings()
    catalog = _catalog()
    items = _stored(catalog)
    state = _ds.load_state()
    lines = [*cfg.warnings, *_ds.unreadable_v1_warnings()]
    lines.append(_switch_line(settings))
    history_on = _history.capture_enabled()
    lines.append("History: on" if history_on else f"History: off{'. ' + HISTORY_PAUSED if cfg.enabled else ''}")
    lines.append(_model_line(cfg))
    lines.append(_last_answer_line(cfg, items))
    lines.append(_gate_line(cfg))
    lines.append(f"Fact extraction: {CLAIRIC_TEXT.get(cfg.clairic, cfg.clairic)}")
    lines.append(f"Worker: every {cfg.interval_minutes} min, {cfg.quiet_minutes} min after the last message, "
                 f"up to {cfg.max_calls_per_minute} model calls a minute")
    lines.append(f"First build: up to {cfg.backfill_messages} messages from the last {cfg.backfill_days} days")
    lines.append(f"Storage: {_ds.dossier_root()}")
    lines.append(_stored_line(items))
    lines.extend(_usage_lines(state["usage"]))
    lines.append(_failures_line(state["failures"], catalog))
    lines.extend(_last_cycle_lines(state.get("last_cycle")))
    lines.append(f"Cycle running now: {'yes' if _ds.cycle_running() else 'no'}")
    _emit(lines)
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    result = _ds.set_enabled(True, via="cli")
    cfg = _ds.load_config()  # after the write: a corrupt switch file no longer warns
    lines = list(cfg.warnings)
    lines.append(f"Dossiers: enabled ({_when(result['updated_at'])} via cli)." if result["changed"]
                 else f"Dossiers are already enabled{_since(result)}.")
    lines.append("The gateway worker starts its first cycle within a few minutes; no gateway restart is needed.")
    lines.extend(_view.enable_lines(cfg, "en"))
    if not _history.capture_enabled():
        lines.append(HISTORY_OFF)
    lines.append(f"Turn off with '{DISABLE_COMMAND}'.")
    _emit(lines)
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    result = _ds.set_enabled(False, via="cli")
    lines = list(_ds.load_config().warnings)
    if result["changed"]:
        lines.append(f"Dossiers: disabled ({_when(result['updated_at'])} via cli). No more chat text is sent to the "
                     "model; a cycle already running stops before its next model call.")
    else:
        lines.append(f"Dossiers are already disabled{_since(result)}.")
    lines.append(f"Stored dossiers are kept; delete them with '{FORGET_ALL_COMMAND}'.")
    _emit(lines)
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    if args.mode is not None:
        _ds.set_gate(args.mode, via="cli")
    state = _ds.load_config().gate
    if args.mode == "off":
        line = GATE_OFF
    elif args.mode == "auto" and state == "on":
        line = f"Message check: {GATE_TEXT['on']}. {_view.STRINGS['en']['enable_gate']}"
    else:
        line = f"Message check: {GATE_TEXT.get(state, state)}."
        if args.mode == "auto":
            line = f"{line} {SAVED_UNTIL_FIXED}"
    _emit([line])
    return 0


def _cmd_clairic(args: argparse.Namespace) -> int:
    if args.mode is not None:
        _ds.set_clairic(args.mode, via="cli")
    state = _ds.load_config().clairic
    line = CLAIRIC_ON if args.mode == "on" and state == "on" else f"Fact extraction: {CLAIRIC_TEXT.get(state, state)}."
    if args.mode == "on" and state != "on":
        line = f"{line} {SAVED_UNTIL_FIXED}"
    _emit([line])
    return 0


# ---------------------------------------------------------------------------
# list / show / export
# ---------------------------------------------------------------------------


def _list_row(item: _Stored, state: dict[str, Any]) -> tuple[str, str, str, str]:
    doc, tags = item.doc, []
    ident = "-" if item.ids is None else f"{item.ids[0]}:{item.ids[1]}"
    if item.corrupt:
        info = "-  unreadable"
    elif not item.built:
        info = "-  waiting for first build"
    elif item.kind == "persona":
        info = (f"rev {doc['revision']}  {_doc.units(doc['document']):,} chars  from "
                f"{_plural(doc['messages'], 'message')} in {_plural(len(doc['chats']), 'chat')}  "
                f"checked {_when(doc['checked_at'], utc=False)}")
    else:
        info = (f"rev {doc['revision']}  {_doc.units(doc['document']):,} chars  {_plural(doc['messages'], 'message')}"
                f"  checked {_when(doc['checked_at'], utc=False)}")
    if doc is not None and doc.get("note"):
        tags.append("[note]")
    failure = state["failures"].get(item.key)
    if failure:
        tags.append(f"[failed: {_failure_text(failure)}]")
    gate = state["chats"].get(item.key) if item.kind == "contact" else None
    if isinstance(gate, dict) and gate.get("recheck_at"):
        tags.append("[deferred]")
    if (doc is not None and not item.built and not failure and isinstance(gate, dict)
            and gate.get("dossier_id") == doc["id"]):
        tags.append("[unqualified]")  # the last pass found no first build to make
    if item.built and not doc["document"]:
        tags.append("[empty]")
    if item.corrupt:
        tags.append("[corrupt]")
    if item.ids is not None and _ds.chat_paused(*item.ids):
        tags.append("[off for this chat]")
    if item.backup:
        tags.append("[v1 backup]")
    return item.label, ident, info, " ".join(tags)


def _cmd_list(args: argparse.Namespace) -> int:
    items = _stored(_catalog())
    if not items:
        print("No dossiers yet.")
        return 0
    state = _ds.load_state()
    rows = [tuple(_esc(cell) for cell in _list_row(item, state)) for item in items]
    name_width = max(len(row[0]) for row in rows)
    ident_width = max(len(row[1]) for row in rows)
    for name, ident, info, tags in rows:
        print("  ".join(part for part in (name.ljust(name_width), ident.ljust(ident_width), info, tags) if part))
    return 0


def _header_lines(target: Target, doc: dict[str, Any]) -> list[str]:
    if target[0] == "persona":
        head = "Owner persona"
        source = (f"from {_plural(doc['messages'], 'message')} in {_plural(len(doc['chats']), 'chat')}"
                  if doc.get("checked_at") else None)
    else:
        head = f"Dossier: {_label(target)} ({_ident(target[1])})"
        source = f"from {_plural(doc['messages'], 'message')}" if doc.get("window_start") is not None else None
    parts = [f"Revision {doc['revision']}"]
    if doc.get("checked_at"):
        parts.append(f"checked {_when(doc['checked_at'])}")
    if doc.get("updated_at"):
        parts.append(f"updated {_when(doc['updated_at'])}")
    if source:
        parts.append(source)
    call = doc.get("last_call")
    if isinstance(call, dict) and call.get("provider"):
        parts.append(" / ".join(_ds.without_credentials(str(call.get(name))) for name in ("provider", "model")))
    return [head, " · ".join(parts)]


def _status_lines(target: Target, doc: dict[str, Any]) -> list[str]:
    lines = []
    key = "persona" if target[0] == "persona" else _ds.chat_key(target[1]["business_connection_id"],
                                                                 target[1]["chat_id"])
    failure = _ds.load_state()["failures"].get(key)
    if failure:
        lines.append(f"The last refresh failed ({_failure_text(failure)}, {_when(failure.get('at'))}); "
                     "this is the earlier version.")
    if target[0] == "contact" and _ds._entry_paused(target[1]):
        lines.append(f"{_ds.CHAT_OFF} This version stays as it is.")
    if doc.get("older_history_not_sent"):
        cfg = _ds.load_config()
        lines.append(f"Older history was not sent: the first build reads up to {cfg.backfill_messages} messages "
                     f"from the last {cfg.backfill_days} days.")
    return lines


def _body_lines(target: Target, doc: dict[str, Any], *, previous: bool) -> list[str]:
    built = doc.get("checked_at") if target[0] == "persona" else doc.get("window_start") is not None
    if previous:
        lines = ["--- previous version ---"]
        lines.extend(_text_lines(doc["previous"]) if doc.get("previous") else ["No previous version."])
    elif doc["document"]:
        lines = _text_lines(doc["document"])
    else:
        lines = ["Nothing worth keeping yet." if built else "Waiting for the first build."]
    if doc.get("note"):
        lines.append("")
        lines.append("--- owner note ---" if target[0] == "persona" else "--- owner note (not posted) ---")
        lines.extend(_text_lines(doc["note"]))
    return lines


def _json_view(target: Target, doc: dict[str, Any]) -> dict[str, Any]:
    view = {key: value for key, value in doc.items() if key != "seen"}
    view["label"] = "Owner persona" if target[0] == "persona" else _label(target)
    return view


def _cmd_show(args: argparse.Namespace) -> int:
    if args.fact is not None:
        return _removed_hint("show --fact")
    target = _resolve_target(args)
    doc = _load(target)
    if doc is None:
        _emit([f"error: no dossier for {_label(target)} yet"])
        return 1
    if args.format == "json":
        print(_json_text(_json_view(target, doc)))
        return 0
    lines = _header_lines(target, doc)
    lines.extend(_status_lines(target, doc))
    lines.append("")
    lines.extend(_body_lines(target, doc, previous=args.previous))
    _emit(lines)
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    doc = _load(target)
    if doc is None:
        _emit([f"error: no dossier for {_label(target)} yet"])
        return 1
    if args.format == "json":
        print(_json_text(_json_view(target, doc)))
        return 0
    title = "# Owner persona" if target[0] == "persona" else f"# Dossier: {_label(target)}"
    lines = [title, EXPORT_HEADER, ""]
    lines.extend(_text_lines(doc["document"]))
    if doc.get("note"):
        lines.extend(["", "## Owner note"])
        lines.extend(_text_lines(doc["note"]))
    _emit(lines)
    return 0


# ---------------------------------------------------------------------------
# preview / run
# ---------------------------------------------------------------------------


def _print_input(system: str, user: str) -> None:
    # The exact text; JSON escapes C0 controls itself and _raw_json the C1 ones.
    sys.stdout.write(f"--- system ---\n{system}\n--- user ---\n{_c1_escaped(user)}\n")


def _cmd_preview(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    if target[0] == "persona" and args.post:
        print("error: --post works for one contact; pass --contact or --chat")
        return 1
    if target[0] == "contact" and _ds._entry_paused(target[1]):
        print(f"error: {_ds.CHAT_OFF}")
        return 1
    now = _history._utcnow()
    notes = [] if _ds.load_config().enabled else [DISABLED_PREVIEW]
    if not _history.capture_enabled():
        notes.append(HISTORY_OFF)
    if target[0] == "persona":
        system, user, due = _ds.preview_persona(now=now)
        if not user:
            notes.append(f"# nothing due: no sent text messages in the last {_ds.load_config().backfill_days} days")
        else:
            _print_input(system, user)
            sent = json.loads(user)["messages"]
            chats = len({m["chat"] for m in sent})
            source = f"{_plural(len(sent), 'message')} in {_plural(chats, 'chat')}"
            notes.append(f"# would run: persona from {source}" if due else
                         f"# not due yet; 'run --persona' would rebuild it from {source}")
        _emit(notes, sys.stderr)
        return 0
    entry = target[1]
    if args.post:
        from . import dossier_post

        result = dossier_post.post_payload(entry, now=now)
        if isinstance(result, str):
            notes.append(f"# /dossier would send nothing; refusal: {result}")
        else:
            system, user = result
            _print_input(system, user)
            sent = json.loads(user).get("messages") or []
            notes.append(f"# /dossier would send {_plural(len(sent), 'message')}; refusal: none")
        _emit(notes, sys.stderr)
        return 0
    system, user, info, gate = _ds.preview_contact(entry, now=now)
    if info.first and not info.qualified:
        cfg = _ds.load_config()
        notes.append(f"# nothing due: {_label(target)} does not qualify yet; the first build needs a received and a "
                     f"sent text message in the last {cfg.backfill_days} days")
    elif not user:
        notes.append("# nothing due")
    else:
        _print_input(system, user)
        notes.append(f"# would run: {'first' if info.first else 'update'}, {info.new} new, {info.edited} edited, "
                     f"{info.deleted} deleted, note {'changed' if info.note_changed else 'unchanged'}"
                     f"{'; message check would apply' if gate else ''}")
    _emit(notes, sys.stderr)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    # Check the opt-in first: a disabled run takes no lock and reads no history.
    if not _ds.load_config().enabled:
        print(DISABLED_RUN)
        return 1
    if not _history.capture_enabled():
        print(HISTORY_OFF_RUN)
        return 1
    llm = _ds._LLM
    if llm is None or not callable(getattr(llm, "complete", None)):
        print(NO_FACADE)
        return 1
    target = _resolve_target(args) if _has_target(args) else None
    if target is not None and target[0] == "contact" and _ds._entry_paused(target[1]):
        print(f"error: {_ds.CHAT_OFF}")
        return 1
    report = _ds.run_cycle(llm=llm, trigger="cli", target=target)
    refusals = {"chat_off": f"error: {_ds.CHAT_OFF}", "disabled": DISABLED_RUN, "history_off": HISTORY_OFF_RUN,
                "busy": BUSY,
                "catalog_unavailable": "error: the contact catalog is unavailable; check it with "
                                       "'hermes telesec history catalog'"}
    if report.outcome in refusals:  # switched or locked since the checks above
        print(refusals[report.outcome])
        return 1
    if report.outcome == "aborted_trust":
        print(f"error: model call denied by the Hermes trust gate: {_esc(report.error or 'denied')}")
        print(_esc(TRUST_HINT))
        return 1
    if report.outcome == "no_history":
        print("No Telegram Business history found; nothing to do.")
        return 0
    lines = [f"Dossier cycle {report.outcome} in {_seconds(report.duration_ms)}: {_counts(report)}"]
    lines.extend(f"failed {label} {error}" for label, error in report.failures)
    _emit(lines)
    return 1 if report.failed or report.failures or report.persona == "failed" else 0


# ---------------------------------------------------------------------------
# note / forget / removed commands
# ---------------------------------------------------------------------------


def _run_hint(args: argparse.Namespace, target: Target) -> str:
    if target[0] == "persona":
        return f"{CLI} run --persona"
    if args.contact is not None:
        escaped = re.sub(r'(["\\$`])', r"\\\1", args.contact)
        return f'{CLI} run --contact "{escaped}"'
    entry = target[1]
    return (f"{CLI} run --connection {shlex.quote(str(entry['business_connection_id']))} "
            f"--chat {shlex.quote(str(entry['chat_id']))}")


def _cmd_note(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    label = _label(target)
    text = None if args.text is None else _doc.clean_note(args.text)
    if args.text is None and not args.clear:
        doc = _load(target)
        _emit(_text_lines(doc["note"]) if doc and doc.get("note") else [f"No owner note for {label}."])
        return 0
    if not text:
        doc = _load(target)
        if not doc or not doc.get("note"):
            _emit([f"No owner note for {label}."])
            return 0
        _ds.set_note(target, None)
        _emit([f"Cleared the owner note for {label}."])
        return 0
    if len(text) > _doc.NOTE_MAX_CHARS:
        _emit([f"error: the note is {len(text)} characters; the limit is {_doc.NOTE_MAX_CHARS}"])
        return 2
    result = _ds.set_note(target, text)
    saved = f"Saved the owner note for {label} ({_plural(result['chars'], 'character')})."
    # The same checks as run, so the hint never names a command that refuses.
    if not _ds.load_config().enabled:
        then = f"Dossiers are disabled; the note is used once they are turned on with {ENABLE_HOW}."
    elif not _history.capture_enabled():
        then = f"Dossiers are paused while history is off; the note is used once it is turned on with {HISTORY_ON_HOW}."
    elif target[0] == "contact" and _ds._entry_paused(target[1]):
        then = f"{_ds.CHAT_OFF} The note is used once it is back on."
    else:
        what = "persona" if target[0] == "persona" else "dossier"
        then = f"The next cycle rewrites the {what} with it; run '{_run_hint(args, target)}' to do it now."
    _emit([f"{saved} {then}"])
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    target = _resolve_target(args, allow_all=True)
    enabled = _ds.load_config().enabled
    result = _ds.forget(target)
    if target[0] == "all":
        if not result["existed"]:
            lines = ["No dossiers stored."]
        else:
            persona = "the owner persona" if result["persona"] else "no owner persona"
            lines = [f"Forgot {_plural(result['contacts'], 'contact dossier')} and {persona}. History was not touched."]
        lines.append("Dossiers are still enabled, so the worker rebuilds them from history on its next cycle. "
                     f"To stop processing: run '{DISABLE_COMMAND}', then '{FORGET_ALL_COMMAND}'." if enabled
                     else "Dossiers are disabled; nothing will be rebuilt.")
        _emit(lines)
        return 0
    label = _label(target)
    what = "the owner persona" if target[0] == "persona" else f"the dossier for {label}"
    if result["existed"]:
        revision = result["revision"] if result["revision"] is not None else "unknown"
        line = f"Forgot {what} (revision {revision}{', with owner note' if result['note'] else ''}). "
        line += "History was not touched."
    else:
        line = f"No dossier stored for {label}."
    if result.get("persona_reset"):
        line += " The owner persona was erased and will be rebuilt without this chat."
    lines = [line]
    if result["existed"] or result.get("persona_reset"):
        if not enabled:
            lines.append("Dossiers are disabled; nothing will be rebuilt.")
        elif target[0] == "contact" and "chat_type" not in target[1]:
            lines.append("Its chat is no longer in the contact catalog, so nothing will be rebuilt.")
        else:
            lines.append("Dossiers are enabled, so the worker rebuilds it from history on its next cycle.")
    _emit(lines)
    return 0


def _removed_hint(command: str) -> int:
    _emit([REMOVED_HINT.format(cmd=command), CHANGES_HINT if command == "changes" else NOTE_HINT])
    return 1


def _removed(command: str) -> Callable[[argparse.Namespace], int]:
    return lambda args: _removed_hint(command)


_ACTIONS: dict[str, Callable[[argparse.Namespace], int]] = {
    "status": _cmd_status,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "list": _cmd_list,
    "show": _cmd_show,
    "export": _cmd_export,
    "preview": _cmd_preview,
    "note": _cmd_note,
    "run": _cmd_run,
    "forget": _cmd_forget,
    "gate": _cmd_gate,
    "clairic": _cmd_clairic,
    **{name: _removed(name) for name in REMOVED},
}
