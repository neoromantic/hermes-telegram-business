"""One-release migration from the old plugin identity; also used before reinstall."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from pathlib import Path

OLD_ID = "telegram-business-voice-transcriber"
NEW_ID = "telesec"


def _yaml_io():
    """PyYAML where the host has it (Hermes 0.21); Hermes 0.22+ ships ruamel.yaml instead."""
    try:
        import yaml
    except ImportError:
        from ruamel.yaml import YAML

        round_trip = YAML()
        return round_trip.load, round_trip.dump
    return yaml.safe_load, lambda data, stream: yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)


def migrate(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    # Same cross-process lock as Hermes's plugin settings writer.
    with (home / ".config.yaml.lock").open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        config_path = home / "config.yaml"
        config = {}
        text = config_path.read_text() if config_path.exists() else ""
        # A config that never named the old plugin needs no YAML parser at all.
        if OLD_ID in text:
            load, dump = _yaml_io()
            config = load(text) or {}
        plugins = config.get("plugins", {})
        entries = plugins.get("entries", {})
        changed = False
        if OLD_ID in entries:
            if NEW_ID in entries and entries[NEW_ID] != entries[OLD_ID]:
                raise FileExistsError("Both plugin config entries exist; merge them before restarting")
            entries[NEW_ID] = entries.pop(OLD_ID)
            changed = True
        for key in ("enabled", "disabled"):
            names = plugins.get(key, [])
            if OLD_ID in names:
                plugins[key] = [NEW_ID if name == OLD_ID else name for name in names]
                changed = True

        paths = [
            (home / "data" / "telegram-business", home / "data" / NEW_ID),
            (home / "cache" / OLD_ID, home / "cache" / NEW_ID),
            (home / "plugin-data" / OLD_ID, home / "plugin-data" / NEW_ID),
        ]
        old_namespace = f"agent-plugin-{OLD_ID}-{hashlib.sha256(OLD_ID.encode()).hexdigest()[:8]}"
        new_namespace = f"agent-plugin-{NEW_ID}-{hashlib.sha256(NEW_ID.encode()).hexdigest()[:8]}"
        paths.append((home / "plugin-data" / old_namespace, home / "plugin-data" / new_namespace))
        for source, target in paths:
            if source.exists() and target.exists():
                raise FileExistsError(f"Migration preserves both {source} and {target}; merge them before restarting")
        for source, target in paths:
            if source.exists():
                source.rename(target)
                target.chmod(0o700)
                for path in target.rglob("*"):
                    if path.is_dir():
                        path.chmod(0o700)
                    else:
                        path.chmod(0o600)
        if changed:
            descriptor, temporary = tempfile.mkstemp(dir=home, prefix=".telesec-config-")
            with os.fdopen(descriptor, "w") as output:
                os.fchmod(output.fileno(), 0o600)
                dump(config, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, config_path)
