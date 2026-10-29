# Pass 6A Realtime foundation

This pass adds an explicit, server-side OpenAI Realtime WebSocket client and a hardware-independent validation CLI. It does not connect during Flask startup, enable audio streams, alter the dashboard, or send any conversation content in its validation command. A live connection is opened only with `--connect`.

## Raspberry Pi installation and validation

On the Pi, as the normal application user, from the repository root:

```bash
cd magic_mirror_v2
git fetch origin
git switch --track origin/pass-6a-realtime-foundation
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
test -f .env || cp .env.example .env
.venv/bin/python -m magic_mirror.assistant.realtime_test
```

If the branch exists locally, run `git switch pass-6a-realtime-foundation` and `git pull --ff-only` instead of `git switch --track`. The offline CLI prints the chosen model, whether a key is configured, validated limits, and that no connection was opened. It never prints the key. To test a live session, put `OPENAI_API_KEY` in the ignored local `.env` with file permissions limited to the application user, then explicitly run:

```bash
.venv/bin/python -m magic_mirror.assistant.realtime_test --connect
```

The command connects over certificate-verified WebSocket, waits for `session.created`, sends a text-only `session.update`, waits for `session.updated`, prints the session ID and requested model, then closes. This is a live API connection and may be billable under your account's current terms. It sends no `response.create`, audio, or text input. A missing key, authentication failure, unavailable model, timeout, or invalid session configuration exits nonzero with a sanitized error. No key is passed as a command-line argument. There is no automatic substitution of another model.

Run the existing dashboard separately with `./scripts/run_pi.sh`; inspect `http://127.0.0.1:5000/health` and stop with Ctrl+C. This does not exercise the Realtime client. No service or kiosk configuration changes are needed.

## Verified protocol and implementation

The OpenAI documentation checked for this pass describes the GA server-to-server WebSocket URL `wss://api.openai.com/v1/realtime?model=<id>` with an `Authorization: Bearer` header. The server emits `session.created` first; the client sends `session.update` with `session.type="realtime"`; `session.updated` acknowledges the configuration. The client requests `output_modalities=["text"]`, sets `audio.input.format` and `audio.output.format` to 24 kHz `audio/pcm`, and disables automatic turn detection. The audio format fields define future compatibility; this pass sends no audio. Voice output, resampling from the existing 48 kHz devices, playback truncation, and turn control are reserved for a later pass.

`EventTracker` turns server JSON into typed `ConversationEvent` records with state, session ID, response ID, item ID, event ID, role, partial text or audio, final flag, status, error code, and usage when available. States are `disconnected`, `connecting`, `ready`, `listening`, `processing`, `responding`, `stopping`, and `error`. `response.created` starts a response timer; `response.done` ends it and must be checked for `completed`, `cancelled`, `failed`, or `incomplete`. `response.cancel` is explicit. Server `error` events carry codes without their possibly sensitive free-form messages. Unexpected and malformed events yield `unknown` or `invalid` records without crashing. A future display should replace a partial transcript keyed by role, item ID and content index with its `.done`/`.completed` text; that final event is not an extra utterance. Duplicate finals and late deltas are suppressed. Assistant audio chunks remain base64 in typed events and are never played here.

Connections have a bounded handshake and acknowledgment timeout. Transient connect errors retry at most `MIRROR_REALTIME_RETRY_LIMIT` times with exponential backoff; authentication, configuration, and other protocol failures do not retry. An unexpected post-connection disconnect is reported to the caller; there is no automatic reconnect loop. A per-session watchdog enforces session lifetime, idle time, and active response time even if the caller stops receiving events. `receive()` checks the same deadlines and reports the expiry reason. Callers should still explicitly `close()` when finished; the CLI always closes in a context manager. A socket close is given a three-second grace period. Usage aggregates only nonnegative token counts from `response.done`; it records no conversation content. A separate transcription service's usage is not aggregated in this pass.

Official sources consulted:

- [Realtime WebSocket guide](https://developers.openai.com/api/docs/guides/voice-websockets?api=realtime)
- [Realtime conversation guide](https://developers.openai.com/api/docs/guides/realtime-conversations)
- [Realtime client events](https://developers.openai.com/api/reference/resources/realtime/client-events)
- [Realtime server events](https://developers.openai.com/api/reference/resources/realtime/server-events)
- [GPT-Realtime-2.1 Mini model](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini)

The model page currently lists `gpt-realtime-2.1-mini` as supporting WebSocket, text/audio input, text/audio output and tool use. Listed token prices are $0.60 per 1M text input, $0.06 cached text input, $2.40 text output; $10 per 1M audio input, $0.30 cached audio input, and $20 per 1M audio output. Pricing, rate limits and availability can change; verify them in the [current pricing page](https://developers.openai.com/api/docs/pricing) before sustained use. The model is a lower-cost distilled reasoning model; its speed, recognition quality, and output should be validated on the Pi before enabling voice. The CLI does not create responses, but its live connection is still an explicit API operation.
