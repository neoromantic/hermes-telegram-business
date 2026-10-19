# Verification

## Automated checks

```bash
uv run --no-project --python 3.14 --with pytest --with pytest-asyncio --with pyyaml --with python-telegram-bot==22.8 python -m pytest -q
```

CI runs the suite on Python 3.11–3.14. Telegram, STT, and LLM network calls are simulated. Real PTB objects and request serialization are exercised separately from the lightweight unit fixtures.

History checks must cover one-time opt-in, private-chat defaults, explicit scope overrides, create/edit/delete capture, correction classification, restart/retry behavior, partial final writes, contact renames and ambiguity, bounded date/text queries, retention, catalog rebuilds, and CLI failure exits.

Native-handler checks must demonstrate that Business voice/audio is consumed before the ordinary media handler, unrelated messages continue normally, and text history observation does not spend an LLM call. The transcript surfaces must retain the intended `business_connection_id`, complete text, and UTF-16 entity positions.

## Live gateway smoke: 0.9.0, 2026-10-19

Plugin `0.9.0` ran on the owner's production gateway (Hermes `0.21.6`) with a real Business connection. In a real private chat, `/transcribe off` was confirmed in place and the next voice note was skipped silently (`decision=skip reason=mode_off`). `/transcribe both` restored transcription, and the next voice note got its transcript caption. `/transcribe`, `/transcribe reset` and an unknown argument returned the expected status lines. The modes file was written `0600` and listed by `hermes telegram-business mode list`. The live run showed that the contact could see command messages, so 0.9.0 deletes the command and its status about 5 seconds after confirming.

## Current-core smoke: 2026-10-18

Plugin `0.8.0` at [`0e758ffc`](https://github.com/neoromantic/hermes-telegram-business/commit/0e758ffc1bbbc759b6b81e8f6bd557927a15ec7a) was installed and enabled through the actual CLI in an isolated profile using tagged Hermes `v0.22.2`, Python `3.14.5`, and PTB `22.8`. [CI passed 243 tests on every Python version from 3.11 through 3.14](https://github.com/neoromantic/hermes-telegram-business/actions/runs/39147890386).

- Real `PluginManager`/`PluginContext` discovery registered the hook, CLI, history skill, and one Telegram handler factory. Unload and forced rediscovery removed and restored the registrations without duplication.
- Real `TelegramAdapter` and `TelegramApplication` processed an in-memory Telegram transport. History opt-out, create/edit/retry/delete capture, and delayed classification produced eight canonical events, including `likely_duplicate` and `unexplained` deletions. Text capture made no model calls; handled voice updates did not reach the ordinary handler.
- Separate CLI processes resolved the synthetic contact, showed and searched its history, listed unexplained deletions, and returned `ok=True ... records=8 ... errors=0 warnings=0` from `history verify`.
- Synthetic incoming and outgoing voice notes passed through Hermes's real OpenAI STT backend and host-owned `PluginLlm` cleanup (`gpt-4.1-mini-2025-04-14`, temporary profile overrides). The simulated Telegram transport received one reply and one caption edit; amounts, time, and negation survived, and transient audio was removed.
- The default `gemini` / `gemini-3.8-flash` passed the real trust gate, provider resolver, native Gemini adapter, and structured-output parsing with simulated Google HTTP. The generated request targeted `/v1beta/models/gemini-3.8-flash:generateContent`, requested JSON schema output, and omitted sampling parameters. This used a synthetic credential placeholder and sent no request to Google.

No real Telegram conversation was contacted and no production gateway was restarted. Actual Business delivery/client rendering and live Gemini 3.8 quality, latency, and credential availability remain to be checked with the controlled smoke below.

## Controlled live smoke

Use an explicitly selected test Business conversation and the intended Hermes profile. These steps contact Telegram; the automated suite does not execute them.

1. Install the branch into the selected profile, enable the plugin, and restart its gateway. Check registration and errors in that profile's logs.
2. With history disabled, send a text message and verify that no history record is created.
3. Enable history. Send and edit text from each side; confirm `history show --contact ...` reports the events and contact names.
4. Delete an original message and separately delete/resend a small correction. After the configured window, inspect `history deletions` for distinct classifications.
5. Restart the gateway and verify existing records remain readable without duplicate events. Check `history verify` and catalog recovery.
6. Send short incoming/outgoing voice notes and a round video note. Verify one logical transcript per source and outgoing caption behavior in Telegram clients.
7. Send an attached speech recording and a tagged music file. Check that the speech is transcribed and music is skipped without an agent turn.
8. Exercise cleanup with the configured Gemini model and trust allowlist. Compare Russian, English, and mixed-language examples containing names, dates, amounts, negations, and long paragraphs against raw STT. Record fidelity and time to visible text.
9. Disable/remove the plugin and restart. Confirm ordinary Hermes Telegram behavior still works.

Keep evidence free of real contact details and message content. Record Hermes/PTB/Python/model versions and distinguish simulated checks from live Telegram/model results.

## Documentation images

The transcript illustration depicts implemented delivery shapes. The history image contains actual CLI output captured from synthetic Business updates. The assistant illustration is explicitly a future-design concept. All use fictional content and have editable HTML sources in `docs/images/src/`.

Render with `agent-browser --engine chrome`, inspect the image, and check for clipped text before replacing PNGs. A design illustration is not evidence of Telegram client rendering or successful delivery.
