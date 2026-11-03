# Pass 7A Raspberry Pi validation

This pass adds one deterministic, read-only test function to the existing Realtime voice session. Tools are disabled by default. The local Sherpa-ONNX wake detector remains offline with respect to OpenAI until activation; Flask never executes a tool. Use the normal application account on the tested 64-bit Debian 12 Raspberry Pi 4B. Physical audio, wake, LED, and dashboard behavior still require acceptance.

The protocol follows the [current official Realtime conversation guide](https://developers.openai.com/api/docs/guides/realtime-conversations#function-calling): declare functions in `session.update`, collect `response.function_call_arguments.delta` and `.done`, execute only completed function-call items from `response.done`, send `conversation.item.create` with a `function_call_output` and the same `call_id`, then send `response.create`. The [Realtime response-create reference](https://platform.openai.com/docs/api-reference/realtime-client-events/response/create) confirms response-level `tools` and `tool_choice` overrides; the continuation disables tools when the configured call limit is exhausted. No tool runs on a cancelled or incomplete model response. The [configured model page](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini) lists function calling as supported.

## Install without replacing local configuration

Stop the existing wake CLI and Flask with Ctrl+C before changing branches. Check `git status --short` and preserve any tracked Pi edits separately. Do not remove `.venv`, `.env`, `.private/`, the wake model, or Pi-hole files.

```bash
cd magic_mirror_v2
git fetch origin main pass-7a-tool-foundation
git switch pass-7a-tool-foundation || git switch --track origin/pass-7a-tool-foundation
git pull --ff-only origin pass-7a-tool-foundation
test -x .venv/bin/python || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-wake.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
test -f .env || cp .env.example .env
chmod 600 .env
```

`requirements.txt` now includes pinned `jsonschema` and its dependencies for complete JSON Schema validation. Python 3.11 ARM64 wheels for the new packages were checked during development. The existing Sherpa model directory and Pi-hole CA/password remain untouched.

Edit the ignored `.env` locally and set `MIRROR_TOOLS_ENABLED=true`. Keep the already tested `MIRROR_WAKE_ENABLED=true`, model directory, `MIRROR_AUDIO_INPUT_DEVICE=pipewire`, exact `MIRROR_AUDIO_EXPECTED_INPUT_SOURCE`, `MIRROR_AUDIO_OUTPUT_DEVICE=MAX98357A`, 48 kHz mono int16 capture, `MIRROR_AUDIO_PLAYBACK_GAIN=1.4`, LED pin mapping, and private `OPENAI_API_KEY`. Keep `MIRROR_HOST=127.0.0.1`. Do not print or attach `.env` to a report.

## Live check

In terminal A, start the dashboard:

```bash
./scripts/run_pi.sh
```

In terminal B, explicitly start wake listening:

```bash
.venv/bin/python -m magic_mirror.assistant.wake_cli --start
```

In terminal C, check the local dashboard and audio selection:

```bash
curl --fail --silent http://127.0.0.1:5000/health
.venv/bin/python -m magic_mirror.audio.audio_test selected
wpctl inspect @DEFAULT_AUDIO_SOURCE@
```

Open `http://127.0.0.1:5000/` in Pi Chromium. Before saying the wake phrase, confirm the CLI reports local idle, the LED is white, and no OpenAI TCP connection has opened. Say the configured wake phrase, wait for green Listening, then ask “Use your capabilities function to tell me what you can do.” The model chooses whether to call a tool; if it does, terminal B logs `Tool get_mirror_capabilities execution result (success)` without arguments or result data. Confirm the dashboard remains in the processing/responding state while the call runs, then hear one answer, see its matching transcript, and wait for the speaker drain and acoustic settling before green Listening returns. A spoken request may not force the model to choose a tool; record that outcome rather than repeatedly spending API calls.

Ask a short follow-up to confirm the same voice session continues and no answer is triggered by speaker echo. A spoken question during playback is intentionally suppressed; press `c` then Enter to cancel manually and wait for green before speaking. Say “go to sleep” or press `e` then Enter; confirm the Realtime session closes and local white idle resumes. Press `q` then Enter or Ctrl+C to stop the wake CLI; confirm LED off and both audio streams closed. Stop Flask separately with Ctrl+C. Repeat one start/stop cycle without rebooting and confirm that `/health`, clock, weather, news, system, and Pi-hole remain functional. Do not alter Pi-hole, DNS, GPIO boot configuration, or Chromium autostart.

Automated tests use fake Realtime WebSockets and audio devices for malformed JSON, unknown functions, schema failures, duplicate IDs, multiple calls, timeouts, exceptions, cancellation, late results, disconnects, and microphone suppression. Do not try to force malformed tool events through a live model. The local result log gives the only intentional tool-execution cue; neither tool arguments nor result contents are logged.

## Focused invocation diagnostic after the first Pi failure

The initial Pi attempt reported a spoken command error but no successful dispatcher log. The server event and `response.done` status from that attempt were not captured, so its root cause is **not yet reproducible**. The official sequence and model support are verified above; the session advertises the read-only capability function with `tool_choice=auto`, so the model may still choose not to call it. This update adds safe stage logs and mocked tests for a successful wire-event sequence, failed/incomplete function responses before dispatch, a server error before dispatch, and a failed continuation after local execution. No audio architecture or API credential handling changes were needed.

For one bounded retest, preserve `.env`, `.venv`, `.private/`, the wake model and Pi-hole files. Run the install/test commands above, then in terminal B use a process-local log-level override:

```bash
cd ~/magic_mirror_v2
MIRROR_LOG_LEVEL=INFO .venv/bin/python -m magic_mirror.assistant.wake_cli --start
```

Wake once and ask the same capabilities question. Read only these diagnostic lines from terminal B; do not copy full console output or transcripts:

| Observed stage | Meaning |
| --- | --- |
| `Realtime response completed (status=completed, function_calls=0)` | The model finished without requesting any local function. A spoken claim that a command failed is not evidence of local execution. |
| `Realtime response completed (status=failed/incomplete, function_calls=...)` | The model response stopped before a completed call was eligible for dispatch. Record the sanitized status/code. |
| `Function-call requested` then `Function-call completion detected` | A completed function-call response reached the voice controller. |
| `Function-call rejected` or `Tool validation rejected` | Local turn, call-ID, JSON, schema, or limit checks prevented execution; the reason label is safe to share. |
| `Tool get_mirror_capabilities execution started/result` | The local static function actually ran and returned a status. |
| `Function output submitted` then `Response continuation requested` | The result was sent under the original call ID and the spoken continuation was requested. |
| `Recoverable Realtime error` or `Realtime transport error` | The server or transport failed; the current voice session closes and wake recovery returns to local idle. |

If the stage stops after `Function output submitted`, the continuation send failed. If it stops after `Response continuation requested`, inspect the next sanitized response status or Realtime error. Share the stage lines, their order, sanitized codes, and whether the LED returned to white idle. The diagnostics deliberately omit call IDs, arguments, function output, raw audio, transcripts, headers, keys and full provider payloads. Press `e` then Enter to sleep, `q` then Enter or Ctrl+C to stop, and verify the microphone, speaker and GPIO are released. Do not repeatedly make paid calls solely to obtain a trace.

## Limits and privacy

- Only `get_mirror_capabilities` is registered in this pass. It returns a static list, not live status. Weather and web search are not tools yet.
- Tool arguments and outputs remain in the current OpenAI Realtime conversation but are not persisted locally or exposed through Flask. OpenAI API usage after wake activation may incur charges; idle local wake audio is not uploaded.
- The maximum call count, execution deadline, concurrency, and output size are configurable, but each tool has stricter local caps. A cancelled or retired turn cannot publish a late result. If cancellation races a continuation already requested from the server, the session closes rather than accepting a possibly stale response.
- The Pi must verify spoken tool accuracy, audio drain, wake-to-voice handoff, LED transitions, and idle CPU/RSS/TCP measurements before this PR is merged.
