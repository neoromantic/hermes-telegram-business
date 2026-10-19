# AGENTS.md

<!-- devkit:agents v1 -->
<!-- managed by devkit v1: edit in https://github.com/neoromantic/devkit/blob/main/agents/AGENTS.shared.md -->
## Shared rules (devkit)

These rules apply to people and coding agents in every repo that uses
[devkit](https://github.com/neoromantic/devkit). Project rules follow in `## Project` below.
Where they conflict, the project section wins.

### Data

- Use only made-up data in tests, fixtures, examples, docs and screenshots. No real people, chats,
  phone numbers, tokens, session files or local paths such as home directories.
- Never commit `.env` files or credentials. Tests run offline by default; live API tests are
  opt-in and read their credentials from the environment.

### Commands

Every repo exposes the same script names, so CI and agents need no per-repo knowledge:

| Script | What it does |
|---|---|
| `check` | lint and typecheck (or `lint` and `typecheck` separately) |
| `test` | offline unit tests |
| `test:e2e` | end-to-end tests, still offline |
| `build` | produce the shipped artifact, if any |

- Bun repos: `bun run <script>`. Python repos: `uv run …` (never bare `pip` or `python`).
  Go repos: `go` tooling from `go.mod`.
- Use Bun, uv or Go modules for installs. Use npm only for `npm publish` and `npm view`.

### Definition of done

- The change works end to end, tests cover it, and `check` and `test` pass locally.
- README and docs change in the same commit as the behaviour they describe.
- No new warnings, no commented-out code, no stray debug output.

### Commits and releases

- Conventional Commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`.
  A breaking change adds `!` (`feat!:`) and a `BREAKING CHANGE:` footer.
- One logical change per commit. Never rewrite published history or force-push `main`.
- Releases happen only through the repo's Release PR or release workflow. Don't bump versions,
  edit `CHANGELOG.md` or push tags by hand.

### Files

- Don't add planning or progress files (`progress.md`, `docs/plans/`). Plans live in issues.
  `ROADMAP.md`, if present, is a short "Known gaps" list that links issues.
- Don't add agent hooks that rewrite tracked files.
- `AGENTS.md` is the canonical instruction file. `CLAUDE.md` contains only `@AGENTS.md`.
- Files marked "managed by devkit" (this block, `SECURITY.md`, `CONTRIBUTING.md`, the PR
  template, `.gitleaks.toml`, `.github/zizmor.yml`, `renovate.json`, `lefthook.yml` and the
  CI stub) are synced from devkit. Change them there, not here.
<!-- /devkit:agents -->

Telesec — Telegram Business secretary for Hermes: voice transcription, correspondence history, stats, and dossiers. Runtime plugin ID `telesec`. Product direction lives in [docs/ROADMAP.md](docs/ROADMAP.md). Contributor setup and rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Every user-visible change

- **Docs in the same PR.** Update `README.md`, `CHANGELOG.md`, and the relevant `docs/*.md` (ROADMAP, VERIFY, feature guides) together with the code.
- **Website.** After a release the maintainer updates https://telesec.sergeybaus.com (version, supported Hermes version, features, screenshots). Its source is not in this repo, so mention website-relevant changes in the PR description.

## Release and deploy

- `main` is protected; merge with `gh pr merge --squash --admin --delete-branch`.
- Release: bump `plugin.yaml`, `pyproject.toml`, `uv.lock`, and the version asserts in `tests/test_plugin.py`; date the CHANGELOG section, tag `vX.Y.Z`, publish a GitHub release.
- Before tagging, run `hermes plugins validate --install-deps` on the newest Hermes release in an isolated venv, not only on a long-running gateway. A long-running gateway can have packages a clean Hermes lacks: 0.10.0 failed to import on Hermes 0.22, which no longer installs PyYAML. Plugin code may import only the standard library, PTB, and Hermes core dependencies.
- After a deploy, run the [controlled live smoke](docs/VERIFY.md#controlled-live-smoke) and record the result in `docs/VERIFY.md`. Keep that record content-free and host-neutral: no contact names, message text, raw IDs, host names, service unit names, or local paths.

## Hermes plugin catalog

- The entry is `plugin-catalog/telesec.yaml` in `NousResearch/hermes-agent` ([submission notes](docs/CATALOG_SUBMISSION.md)). Catalog users get a release only through a new upstream PR that changes the full 40-character `sha` and `version` together. Image URLs in the entry are raw GitHub URLs pinned to the same SHA.
- The catalog page renders `README.md` at the pinned SHA and drops raw HTML, so use Markdown images and links, not `<img>`.
- Open the PR from the `neoromantic` fork. Commit with the GitHub noreply email, because the contributor check rejects unmapped emails. The PR may only touch `plugin-catalog/**`. Workflows on a fork PR may wait for a maintainer to approve them.
- After each catalog change, check that the website install steps and version still match.
- Never name or compare Telesec with Nous's own Telegram Business plugin. Say it is not affiliated with Nous Research.

## Code

- Python 3.11+ with uv: `uv sync`, then `uv run pytest -q` and `uv run ruff check .` (CI runs both on 3.11–3.14). Keep the handler group and claim behaviour described in the README.
- GitHub issues, PRs, and docs are in simple English.
