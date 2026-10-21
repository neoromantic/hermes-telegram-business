# Contributing

Thanks for helping. Questions and ideas go to [Discussions](https://github.com/neoromantic/telesec/discussions). Bugs go to [issues](https://github.com/neoromantic/telesec/issues/new/choose). Security problems go through a [private report](SECURITY.md).

## Set up and run the tests

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The tests use fake Telegram objects, so they need no bot token, provider key, network, or running gateway.

```bash
uv sync               # dev tools from uv.lock: pytest, PTB 22.8, PyYAML, ruamel.yaml, Ruff
uv run pytest -q
uv run ruff check .
```

CI runs Ruff and the tests on Python 3.11 to 3.14. The Ruff config is in `pyproject.toml`.

To try a change in a real gateway, install your checkout into a test Hermes profile and follow [docs/VERIFY.md](docs/VERIFY.md). Use a test Business account, not real conversations.

## Where things live

- `__init__.py`: registration, Telegram handlers, the dispatch hook, and the voice pipeline.
- `transcription_modes.py` and `owner_panel.py`: per-chat modes and the owner panel.
- `history_support.py`: opt-in history and the `history` CLI.
- `correspondence_stats.py`: the `stats` CLI.
- `dossier_support.py`: the opt-in dossier worker, the stored switches, the model route and its warnings, the refresh of contacts and the persona, the owner note, and forget.
- `dossier_doc.py`: the dossier file, `document.json`: reading, checking, and atomic writes.
- `dossier_writer.py`: the dossier prompts, the model inputs, the answer checks, and the writer call; also the optional message check and fact extraction requests.
- `model_client.py`: the only direct model calls, for the optional message check (Vercel AI Gateway) and fact extraction (Cloudflare Workers AI). Standard library only.
- `dossier_post.py`: what `/dossier` may post: the stored dossier once it has seen the chat, after one refresh when it has not.
- `dossier_migrate.py`: the one-time move of fact-based dossiers to documents. It goes away in a later release.
- `dossier_cli.py`: the `dossier` CLI.
- `dossier_view.py`: pure Telegram views of dossiers and the `/dossier` post (rich HTML, plain fallback, buttons, Russian and English).
- `dossier_panel.py`: the owner panel's Dossier section, the switch and persona gating, and the owner's in-chat `/dossier` command.
- `tests/dossier_eval/`: the opt-in live dossier eval ([below](#dossier-eval)).
- `scripts/render-dossier-screenshots.py`: writes the rich message HTML of the dossier views over a fictional contact to `docs/images/src/`, for real Telegram screenshots.
- `skills/`: the bundled `history` and `dossiers` skills.
- `docs/`: feature guides, the roadmap, and the verification log.

## Rules for changes

- Keep the runtime plugin ID `telesec` and the existing environment variable names. Existing installs, config keys, and update commands depend on them.
- Keep a change focused. Add or update tests for any behavior change.
- Update the docs in the same PR: `README.md`, `CHANGELOG.md` (under `Unreleased`), and the matching `docs/*.md`.
- No telemetry. Do not add analytics, usage reporting, or new network calls. The plugin talks only to Telegram through the Hermes gateway, the Hermes STT backend, the host `ctx.llm`, and, for the opt-in dossier message check and fact extraction, `model_client.py`.
- No plugin-owned credentials. Model calls go through `ctx.llm`. The only exception is `model_client.py`: it reads keys through the Hermes secret scope, never from `os.environ`, never stores or logs them, uses `https` only, and follows no redirects.
- Plugin code imports only the standard library, python-telegram-bot, and Hermes core modules (lazily, inside `try`).
- Never log message text, transcripts, dossier or note text, names, raw ids, or keys. Log a type name or code instead of an error's text, and never use `exc_info`.
- Use synthetic data in tests, fixtures, logs, and screenshots: no real tokens, IDs, messages, audio, or chats.
- Write issues, PRs, and docs in simple English.

## Dossier eval

`tests/dossier_eval/` replays made-up chats through the real dossier code and scores the result: key facts current, stale, or missing, invented claims, canary texts that must never appear (deleted messages, the owner note in a `/dossier` post) or are only counted (the transcript of a deleted voice message, which stays in the chat), calls, tokens, cost, and time. It is opt-in, live, and paid; the offline tests in `tests/test_dossier_eval.py` check its plumbing.

The `v1` arm needs a worktree of `v0.13.0`. The eval runs the code in it, so keep it in a directory only you can write to, not in a shared temp directory. Check the plumbing first, offline and free:

```bash
git worktree add ../telesec-v1 v0.13.0
uv run python tests/dossier_eval/run.py --out ../telesec-eval --v1-worktree ../telesec-v1 --dry-run
```

The offline test of the `v1` arm is opt-in too: `uv run pytest` skips it unless `TELESEC_EVAL_V1_WORKTREE` names that worktree.

```bash
TELESEC_EVAL_V1_WORKTREE=../telesec-v1 uv run pytest -q tests/test_dossier_eval.py
```

A live run needs `AI_GATEWAY_API_KEY` in the environment (and `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` for the `v2-clairic` arm):

```bash
uv run python tests/dossier_eval/run.py --out ../telesec-eval \
    --arms v1,v2,v2-gate,v2-think,v2-clairic --v1-worktree ../telesec-v1
```

- The arms are `v1` (the old fact-based dossiers), `v2`, `v2-gate` (with the message check), `v2-think` (reasoning `high`), and `v2-clairic` (with fact extraction; skipped without Cloudflare keys).
- `--scenarios` and `--runs` pick what to run. `--budget-usd` (default 6) stops before any call that could go over it. `--judge` picks the judge model, which must not be a Google model.
- Results go only to `--out/<UTC timestamp>/`: one JSON file per arm and a `summary.md` with numbers and pass/fail only, no message text. Keep them out of the repository; measured numbers go into [docs/DOSSIERS.md](docs/DOSSIERS.md#cost-and-quality).

The maintainer handles version bumps, releases, and deploys. Please leave `plugin.yaml` and `pyproject.toml` versions as they are.
