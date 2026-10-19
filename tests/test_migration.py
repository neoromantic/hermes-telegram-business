from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_plugin
import yaml

# The shared plugin fixture. An assignment, not an import, so ruff doesn't treat the
# test parameters that use it as redefinitions (F811).
plugin = test_plugin.plugin


def test_first_start_migrates_all_private_files_and_config_idempotently(plugin, tmp_path, monkeypatch):
    old_id = "telegram-business-voice-transcriber"
    contents = {
        "data/telegram-business/transcription-modes.json": b'{"version":1,"owners":{}}\n',
        "data/telegram-business/history/conn/991/2026-10.jsonl": b'{"text":"kept"}\n',
        "data/telegram-business/history/contacts.json": b'{"contacts":[]}\n',
        "data/telegram-business/dossiers/conn/991/dossier.json": b'{"facts":[]}\n',
        "data/telegram-business/dossiers/conn/991/changes.jsonl": b'{"rev":1}\n',
        f"cache/{old_id}/audio.ogg": b"audio",
        f"plugin-data/{old_id}/state.json": b'{"cursor":7}',
    }
    old_namespace = f"agent-plugin-{old_id}-{hashlib.sha256(old_id.encode()).hexdigest()[:8]}"
    new_namespace = "agent-plugin-telesec-" + hashlib.sha256(b"telesec").hexdigest()[:8]
    contents[f"plugin-data/{old_namespace}/state.json"] = b'{"cursor":8}'
    for name, content in contents.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)
    entry = {"settings": {"history_enable": True}, "llm": {"allowed_models": ["test-model"]}}
    config = {"plugins": {"enabled": [old_id, "other"], "disabled": [], "entries": {old_id: entry}},
              "unrelated": {"keep": "value"}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr(plugin._history_support, "run_startup_maintenance",
                        Mock(return_value=SimpleNamespace(cap_exceeded=False, warnings=[])))
    ctx = SimpleNamespace(llm=None, get_config={}.get, register_telegram_handler=Mock(), register_hook=Mock(),
                          register_cli_command=Mock(), register_skill=Mock())

    plugin.register(ctx)
    first_config = (tmp_path / "config.yaml").read_bytes()
    plugin.register(ctx)

    for name, content in contents.items():
        target = name.replace(old_namespace, new_namespace).replace(old_id, "telesec")
        target = target.replace("data/telegram-business/", "data/telesec/")
        path = tmp_path / target
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == 0o600
        assert not (tmp_path / name).exists()
    config["plugins"]["entries"] = {"telesec": entry}
    config["plugins"]["enabled"] = ["telesec", "other"]
    assert yaml.safe_load(first_config) == config
    assert (tmp_path / "config.yaml").read_bytes() == first_config
    assert (tmp_path / "config.yaml").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("conflict", ["data", "config"])
def test_migration_never_overwrites_conflicting_destinations(plugin, tmp_path, conflict):
    old = tmp_path / "data/telegram-business"
    old.mkdir(parents=True)
    (old / "transcription-modes.json").write_text("old")
    if conflict == "data":
        new = tmp_path / "data/telesec"
        new.mkdir()
        (new / "transcription-modes.json").write_text("new")
    else:
        (tmp_path / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {
            "telegram-business-voice-transcriber": {"settings": {"history_enable": True}},
            "telesec": {"settings": {"history_enable": False}},
        }}}))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        plugin.migrate(tmp_path)
    assert all(Path(name).read_bytes() == content for name, content in before.items())


def test_settings_form_and_environment_overrides(plugin, monkeypatch):
    settings = {"history_enable": True, "cleanup_style": "enriched", "timezone": "Asia/Tbilisi", "agent_enable": True}
    schema = yaml.safe_load((Path(__file__).parents[1] / "plugin.yaml").read_text())["config_schema"]
    assert set(schema) == set(settings)
    legacy = {**settings, "dossier_enable": True}  # removed in 0.11.0; moved into the stored switch once
    ctx = SimpleNamespace(llm=None, get_config=legacy.get, register_telegram_handler=Mock(), register_hook=Mock(),
                          register_cli_command=Mock(), register_skill=Mock())
    plugin.register(ctx)
    assert plugin._history_support.history_config_from_env().enabled
    assert plugin._dossier_support.load_config().enabled
    assert plugin._cleanup_style() == "enriched"
    assert plugin._truthy_env("TELESEC_AGENT_ENABLE", settings["agent_enable"])
    from importlib import import_module
    stats = import_module(f"{plugin.__name__}.correspondence_stats")
    assert stats.resolve_timezone(None, {})[1] == "Asia/Tbilisi"
    for key in ("HISTORY_ENABLE", "DOSSIER_ENABLE", "AGENT_ENABLE"):
        monkeypatch.setenv(f"TELESEC_{key}", "0")
    monkeypatch.setenv("TELESEC_CLEANUP_STYLE", "conservative")
    assert not plugin._history_support.history_config_from_env().enabled
    assert plugin._dossier_support.load_config().enabled  # the stored switch, not the variable
    assert not plugin._truthy_env("TELESEC_AGENT_ENABLE", settings["agent_enable"])
    assert plugin._cleanup_style() == "conservative"
    assert stats.resolve_timezone(None, {"TELESEC_TIMEZONE": "UTC"})[1] == "UTC"
    legacy["history_enable"] = False
    monkeypatch.delenv("TELESEC_HISTORY_ENABLE")
    assert not plugin._history_support.history_config_from_env().enabled


def test_old_environment_and_cli_aliases_warn_and_new_environment_wins(plugin, monkeypatch, caplog):
    monkeypatch.setattr(os, "environ", {
        "HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE": "1",
        "HERMES_TELEGRAM_BUSINESS_DOSSIER_ENABLE": "1",
        "TG_BUSINESS_VOICE_CLEANUP_STYLE": "enriched",
        "TG_BUSINESS_VOICE_TRANSCRIBER_DISABLE": "1",
        "TG_BUSINESS_AUDIO_FILE_MIN_WORDS": "5",
        "TELESEC_CLEANUP_STYLE": "conservative",
    })
    ctx = SimpleNamespace(llm=None, get_config={}.get, register_telegram_handler=Mock(), register_hook=Mock(),
                          register_cli_command=Mock(), register_skill=Mock())
    plugin.register(ctx)
    assert plugin._history_support.history_config_from_env().enabled
    assert plugin._dossier_support.load_config().enabled
    assert plugin._disabled()
    assert plugin._audio_file_limits()[-1] == 5
    assert plugin._cleanup_style() == "conservative"
    plugin.register(ctx)
    assert caplog.text.count("Deprecated Telesec environment aliases") == 1
    assert "HERMES_TELEGRAM_BUSINESS_HISTORY_ENABLE -> TELESEC_HISTORY_ENABLE" in caplog.text
    commands = {call.kwargs["name"]: call.kwargs for call in ctx.register_cli_command.call_args_list}
    parser = argparse.ArgumentParser(prog="hermes telegram-business")
    commands["telegram-business"]["setup_fn"](parser)
    assert commands["telegram-business"]["handler_fn"](parser.parse_args(["mode", "list"])) == 0
    assert "hermes telegram-business is deprecated; use hermes telesec" in caplog.text


def test_migration_works_without_pyyaml(plugin, tmp_path, monkeypatch):
    # Hermes 0.22+ depends on ruamel.yaml and no longer installs PyYAML.
    from importlib import import_module

    migration = import_module(f"{plugin.__name__}.migration")
    monkeypatch.setitem(sys.modules, "yaml", None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("plugins:\n  enabled: [telesec]\n")

    migration.migrate(tmp_path)

    assert config_path.read_text() == "plugins:\n  enabled: [telesec]\n"
    pytest.importorskip("ruamel.yaml")
    old_id = "telegram-business-voice-transcriber"
    config_path.write_text(f"# kept\nplugins:\n  enabled: [{old_id}, other]\n  entries:\n    {old_id}: {{settings: {{a: 1}}}}\n")

    migration.migrate(tmp_path)

    text = config_path.read_text()
    assert text.startswith("# kept\n") and old_id not in text
    assert yaml.safe_load(text)["plugins"] == {"enabled": ["telesec", "other"], "entries": {"telesec": {"settings": {"a": 1}}}}
