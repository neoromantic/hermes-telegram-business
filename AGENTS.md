# AGENTS.md

Telesec — Telegram Business secretary for Hermes: voice transcription, correspondence history, stats, and dossiers. Runtime plugin ID `telesec`. Product direction lives in [docs/ROADMAP.md](docs/ROADMAP.md).

## Every user-visible change

A feature, fix, or release is not done until all of these are handled:

1. **Docs in the same PR.** Update `README.md`, `CHANGELOG.md`, and the relevant `docs/*.md` (ROADMAP, VERIFY, feature guides) together with the code.
2. **Landing page.** Keep https://telesec.sergeybaus.com in sync with the latest release: version, Hermes version, features, and screenshots. Source: `apps/hermes` in the `gooditworks/goodit` monorepo (`~/Developer/goodit`, Vercel project `hermes`, team Gooditworks). Follow that repo's `AGENTS.md`.
3. **Social posts.** Plan small posts about the change for Sergey's Telegram channel and X (`@neoromantic` in Russian, `@SergeyBaus` in English). Use the Vault skill `sergey-social-publishing` and its editorial policy, voice, and pacing rules; cards go to `~/Vault/6 - Контент/Публикации/`. Promo posts for this project have standing approval (2026-10-17), so agents queue them as `ready` without asking. Check the queue first and do not duplicate a card that is already queued.

Delegate items 2 and 3 through the `/orchestration` skill: one Orca worker in the goodit repo for the landing page and one in the Vault for posts. The coordinator reviews what they report and relays open questions.

## Release and deploy

- `main` is protected; merge with `gh pr merge --squash --admin --delete-branch`.
- Release: bump `plugin.yaml` and `pyproject.toml`, date the CHANGELOG section, tag `vX.Y.Z`, publish a GitHub release.
- Production is the `baus` gateway. Update only the plugin there (plugin directory and `hermes-gateway-baus.service`), restart it right away (no need to wait for idle), then do a live smoke test and record it in `docs/VERIFY.md`. Update `~/Vault/2 - Зоны/Server/baus/hermes-baus.md` with the live revision.

## Code

- Python 3.11+, tests with `python -m pytest -q` (see CI). Keep the handler group and claim behaviour described in the README.
- GitHub issues, PRs, and docs are in simple English.
