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

## Argument reconciliation fix and focused Pi retest

The second Pi trace confirmed a completed function call was rejected as `conflicting_arguments` before the local dispatcher ran. A deterministic test reproduced the exact result: `response.output_item.added` carried an empty, provisional argument buffer, while `response.function_call_arguments.done` and `response.done` carried the completed `{}`. The old controller stored the provisional empty string as a final value and compared it with `{}`, producing `invalid_arguments`. The controller now keeps added-item arguments provisional, compares only completed values, and still rejects mismatched completed arguments, item IDs, malformed JSON, oversized input, and duplicate calls. A mocked full Realtime client and fake audio devices complete the output submission, spoken continuation, playback drain, and return to listening without a transport error. The original Pi transport error after the invalid output remains unexplained; it may have been a separate connection or timeout event. No audio architecture or credential handling changed.

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
| `Tool audio drain pending/completed` | An audio prelude was drained before the function continuation; no such lines are expected if the function response had no audio. |
| `Recoverable Realtime error` or `Realtime transport error (reason=...)` | The server or transport failed; the current voice session closes and wake recovery returns to local idle. Reasons include `send_failed`, `connection_closed`, `idle_timeout`, `response_timeout`, `session_limit`, or `unknown`, without raw provider details. |

The expected successful path is `Function-call requested` → `Function-call completion detected` → `validation accepted` → `execution started` → `execution result (success)` → `Function output submitted (status=success)` → `Response continuation requested` → a completed response with zero function calls → audible reply → green listening after playback drain. If the stage stops after output submission, continuation send failed. If it stops after continuation requested, record the next sanitized response status or transport reason and the elapsed time. Share only the stage lines, their order, sanitized codes, and whether the LED returned to white idle. The diagnostics deliberately omit call IDs, arguments, function output, raw audio, transcripts, headers, keys and full provider payloads. Press `e` then Enter to sleep, `q` then Enter or Ctrl+C to stop, and verify the microphone, speaker and GPIO are released. Do not repeatedly make paid calls solely to obtain a trace.

## Continuation closure retest

On commit `29223c6`, one Pi run completed local tool execution and sent `response.create`, then logged `connection_closed` about 16 seconds later without a continuation `response.done`. Another run had completed the continuation. The exact Pi-side cause is unconfirmed. The client had a reproducible local timing defect: a receive already waiting before the tool output kept its old idle deadline after the output and continuation extended activity. That timeout can now resume its wait using the updated deadline. The watchdog also rechecks activity before closing. The new transport log distinguishes `local_timeout`, `local_shutdown`, `remote_clean`, `remote_error`, `abnormal_no_frame`, and `socket_error`; it prints a numeric close code when a close frame provides one, never the provider's close reason.

After updating to the new PR head with the installation commands above, confirm the short commit ID changed. With both foreground processes running and Chromium on the Pi's `http://127.0.0.1:5000/`, ask the capabilities question once, listen for the complete answer, and confirm the answer appears on the dashboard. If it fails, record the elapsed time from `Response continuation requested` to the next log line, the sanitized `reason`, `close_category`, and `close_code`, whether `Response continuation started` appeared, and whether the LED returned to white idle. A `local_timeout` points to an application deadline; a `remote_*` category means a close frame was received, but its cause still requires evidence. `abnormal_no_frame` means no close frame was received. An absent `Response continuation started` means the client did not observe a server `response.created` event, even though it submitted the request. None of these categories alone attributes fault to OpenAI or the network.

To report the active watchdog limits without showing `.env` or credentials, use:

```bash
cd ~/magic_mirror_v2
.venv/bin/python - <<'PY'
from dotenv import load_dotenv
from magic_mirror.config import Settings
load_dotenv('.env', override=False)
s = Settings.from_env()
print('idle_seconds:', s.realtime_idle_seconds,
      'response_seconds:', s.realtime_max_response_seconds,
      'session_seconds:', s.realtime_max_session_seconds)
PY
```

Stop the wake CLI with Ctrl+C after the single test and confirm the LED is off and no audio stream remains. Restart it once to check recovery without rebooting. Keep `.env`, `.private/`, wake model files, Pi-hole configuration, and the existing `.venv` in place.

## Dashboard transcript retest

The subsequent Pi run confirmed a successful capabilities call, function output, spoken continuation, audible answer, and LED transition, but the dashboard showed **Assistant unavailable**. That label means the browser could not obtain a usable `/api/assistant` snapshot; it does not establish that the Realtime transcript was missing. Two code gaps were reproduced with hardware-free tests and fixed here: a completed audio transcript present only in `response.done` was discarded by the parser, and a fully bounded Unicode exchange could exceed the state socket's old 2 KiB read limit. The socket now permits at most 8 KiB and still rejects oversized snapshots. The exact Pi-side read failure remains to be identified at hardware retest.

Keep Flask and the wake CLI running in separate foreground terminals, and open Chromium at `http://127.0.0.1:5000/` on the Pi. Ask the capabilities question once. During the audible answer and again just after the LED returns to green, run this in a third terminal. It reports state and text length without printing the private transcript:

```bash
cd ~/magic_mirror_v2
.venv/bin/python - <<'PY'
import json
from urllib.request import urlopen
with urlopen('http://127.0.0.1:5000/api/assistant', timeout=3) as response:
    value = json.load(response)
    print('HTTP:', response.status, 'state:', value.get('state'),
          'assistant characters:', len(value.get('assistant_text', '')),
          'read status:', response.headers.get('X-Mirror-Assistant-Read-Status', 'ok'))
PY
```

A positive assistant character count with `read status: ok` means the state socket supplied an answer; refresh Chromium and check the local browser origin and rendering. `state: unavailable` comes with a safe read-status header: `socket_missing` suggests the wake CLI exited or is using a different checkout, `socket_refused` suggests a stale socket, `socket_denied` indicates local permissions, `socket_timeout` indicates a delayed reader/server, and `invalid_snapshot` indicates an invalid or oversized payload. Check that the wake CLI is still running and that `.private/assistant-state.sock` exists; do not remove or replace private files as part of this test. If the endpoint is forbidden, verify Chromium is using the Pi's loopback URL, not a LAN hostname. Share only the HTTP code, state, character count, read-status value, and whether the LED was green or white. A completed answer should remain visible while the active conversation is listening and clear when it returns to idle or is stopped.

## Limits and privacy

- Only `get_mirror_capabilities` is registered in this pass. It returns a static list, not live status. Weather and web search are not tools yet.
- Tool arguments and outputs remain in the current OpenAI Realtime conversation but are not persisted locally or exposed through Flask. OpenAI API usage after wake activation may incur charges; idle local wake audio is not uploaded.
- The maximum call count, execution deadline, concurrency, and output size are configurable, but each tool has stricter local caps. A cancelled or retired turn cannot publish a late result. If cancellation races a continuation already requested from the server, the session closes rather than accepting a possibly stale response.
- The Pi must verify spoken tool accuracy, audio drain, wake-to-voice handoff, LED transitions, and idle CPU/RSS/TCP measurements before this PR is merged.
