# Contributing

Thanks for helping. Questions and ideas go to [Discussions](https://github.com/neoromantic/hermes-telegram-business/discussions). Bugs go to [issues](https://github.com/neoromantic/hermes-telegram-business/issues/new/choose). Security problems go through a [private report](SECURITY.md).

## Set up and run the tests

You need Python 3.11 or newer. The tests use fake Telegram objects, so they need no bot token, provider key, network, or running gateway.

```bash
python -m pip install pytest pytest-asyncio pyyaml python-telegram-bot==22.8
python -m pytest -q
```

The same with `uv`:

```bash
uv run --no-project --with pytest --with pytest-asyncio --with pyyaml --with python-telegram-bot==22.8 python -m pytest -q
```

CI runs the tests on Python 3.11 to 3.14. The Ruff config is in `pyproject.toml`. Older code does not pass it yet, so lint only the files you change:

```bash
uvx ruff check path/to/changed_file.py
```

To try a change in a real gateway, install your checkout into a test Hermes profile and follow [docs/VERIFY.md](docs/VERIFY.md). Use a test Business account, not real conversations.

## Where things live

- `__init__.py`: registration, Telegram handlers, the dispatch hook, and the voice pipeline.
- `transcription_modes.py` and `owner_panel.py`: per-chat modes, `/transcribe`, and the owner panel.
- `history_support.py`: opt-in history and the `history` CLI.
- `correspondence_stats.py`: the `stats` CLI.
- `dossier_support.py` and `dossier_cli.py`: opt-in dossiers.
- `skills/`: the bundled `history` and `dossiers` skills.
- `docs/`: feature guides, the roadmap, and the verification log.

## Rules for changes

- Keep the runtime plugin ID `telegram-business-voice-transcriber` and the existing environment variable names. Existing installs, config keys, and update commands depend on them.
- Keep a change focused. Add or update tests for any behavior change.
- Update the docs in the same PR: `README.md`, `CHANGELOG.md` (under `Unreleased`), and the matching `docs/*.md`.
- No telemetry. Do not add analytics, usage reporting, or new network calls. The plugin talks only to Telegram through the Hermes gateway, the Hermes STT backend, and the host `ctx.llm`.
- No plugin-owned credentials. Model calls go through `ctx.llm`.
- Never log message text or transcripts.
- Use synthetic data in tests, fixtures, logs, and screenshots: no real tokens, IDs, messages, audio, or chats.
- Write issues, PRs, and docs in simple English.

The maintainer handles version bumps, releases, and deploys. Please leave `plugin.yaml` and `pyproject.toml` versions as they are.
