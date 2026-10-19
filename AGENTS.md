# AGENTS.md

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

- Python 3.11+, tests with `python -m pytest -q` (see CI). Keep the handler group and claim behaviour described in the README.
- GitHub issues, PRs, and docs are in simple English.
