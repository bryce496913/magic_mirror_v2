# Pass 6D Raspberry Pi validation

Pass 6D keeps wake detection, voice, and LED ownership in the explicitly started wake CLI. Flask serves the existing dashboard and a read-only, current-state route on loopback. Neither Flask startup nor a browser refresh starts the assistant. This guide is for the project lead and physical Pi tester; cloud tests cannot verify GPIO, PortAudio routing, microphone ownership, speaker quality, Chromium layout, or Pi CPU and memory usage.

## Install and prepare

On the Pi, after Pass 6A–6C have been merged and the model from the [Pass 6C guide](PASS6C_PI_VALIDATION.md) is installed, use the normal application account:

```bash
cd magic_mirror_v2
git fetch origin
git switch --track origin/pass-6d-assistant-sync
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-wake.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
test -f .env || cp .env.example .env
chmod 600 .env
mkdir -p .private
chmod 700 .private
```

If the branch already exists locally, use `git switch pass-6d-assistant-sync` and `git pull --ff-only`. No new Python dependency was added in this pass. Keep the existing private Pi-hole credentials and trusted CA under `.private/` or another protected directory. `chmod 700 .private` is required for the assistant socket; its socket and lock file are mode `0600` and are ignored by Git.

In `.env`, set `MIRROR_WAKE_ENABLED=true`, `MIRROR_LED_ENABLED=true`, `MIRROR_WAKE_MODEL_DIR` to the existing absolute model directory, and `OPENAI_API_KEY` to the existing private key. Preserve the tested `MIRROR_AUDIO_INPUT_DEVICE=pipewire`, exact `MIRROR_AUDIO_EXPECTED_INPUT_SOURCE`, `MIRROR_AUDIO_OUTPUT_DEVICE=MAX98357A`, 48 kHz mono int16 and `MIRROR_AUDIO_PLAYBACK_GAIN=1.4`. Keep `MIRROR_HOST=127.0.0.1`; the assistant route rejects non-loopback requests even if the main dashboard is deliberately exposed. Restart Flask after changing `.env` so both processes agree about GPIO ownership. Do not start the standalone LED test while either process owns the pins.

## Start and observe

In terminal A, start Flask:

```bash
./scripts/run_pi.sh
```

In terminal B, explicitly start the assistant:

```bash
.venv/bin/python -m magic_mirror.assistant.wake_cli --start
```

In terminal C, check the local routes and devices:

```bash
curl --fail --silent http://127.0.0.1:5000/health
curl --fail --silent http://127.0.0.1:5000/api/assistant
.venv/bin/python -m magic_mirror.audio.audio_test selected
wpctl inspect @DEFAULT_AUDIO_SOURCE@
```

The health response should be `{"status":"healthy"}`. The assistant snapshot should say `idle` when the wake detector is ready. Do not share a snapshot captured during speech; it contains the current question and answer. Open `http://127.0.0.1:5000/` in Pi Chromium, optionally with `chromium --kiosk --app=http://127.0.0.1:5000/ --no-first-run --disable-session-crashed-bubble`. The assistant text updates at most about one second after a local state change while the tab is visible. The clock, weather, news, Pi-hole, and system modules continue their existing independent refresh schedules.

## Hardware checks

1. Confirm the idle LED is white and the assistant says **Ready for wake word**. Say the configured wake phrase, then wait for **Listening** and a green LED before asking a short question. Confirm **Processing** and then **Responding** show blue. Compare the displayed user transcript with what was said and the displayed answer with the spoken reply. Partial transcripts have an ellipsis; final ones do not. The wake phrase itself should not appear as the question.
2. Ask a follow-up. Confirm the previous exchange clears when new speech starts, no unrelated transcript appears, and one response plays at a time. Interrupt a spoken reply with another question. Confirm stale text and audio from the interrupted answer disappear and the new answer belongs to the new turn.
3. Test silence after wake, a failed or interrupted network connection, and temporary microphone or speaker unavailability. Confirm a recoverable error appears, the LED turns off during recovery, then white returns only after local wake capture resumes. The CLI limits recovery attempts and must release streams if recovery stops. Never expose the key or transcript in a bug report.
4. Refresh Chromium during a response and while idle. Confirm the page rehydrates from the current snapshot, with no second microphone stream, OpenAI session, or GPIO owner. Close and reopen Chromium; the assistant should continue independently. Try `curl http://127.0.0.1:5000/api/assistant` only on the Pi; a request made to the Pi's network address should be forbidden.
5. In terminal B, say “go to sleep” or press `e` then Enter. Confirm the wake CLI returns to white idle and closes the Realtime session. Press Ctrl+C in terminal B. Confirm the LED is off, `/api/assistant` becomes `unavailable` while Flask remains up, and microphone and speaker streams are closed. Restart the same wake CLI without rebooting. Confirm only one process can own its state socket, microphone, and LED at a time.
   While wake is running, a second `wake_cli --start` or `voice_cli --start` under the same application account should stop immediately with an owner error. Do this check only if additional API use is acceptable; the rejected process makes no connection.
6. Repeat with a SIGTERM to the wake CLI PID shown by `pgrep -af magic_mirror.assistant.wake_cli` (`kill -TERM <PID>`), then check LED off and GPIO release. Stop Flask with Ctrl+C in terminal A and check `/health` is no longer reachable. Flask shutdown does not stop a separately started wake CLI; stop terminal B independently before leaving the Pi.
7. During two minutes of quiet idle, record CPU%, RSS MiB, and established TCP count printed by the wake CLI. Expect no idle OpenAI TCP connection; `-1` means the metric was unavailable, not zero. Use `pgrep -af magic_mirror.assistant.wake_cli` to confirm one wake process. Record real Pi numbers rather than assuming cloud timings. Check that no unexpected stream owner remains using the existing audio test after stopping the assistant.

For display acceptance, check the installed 1024×600 portrait presentation from normal viewing distance: both transcript rows should be readable, no module should clip or create scrollbars, and weather/news/Pi-hole/system/clock should remain functional. Photograph the display only if the tester's privacy policy permits it; avoid exposing private transcripts.

## Known limits

- The endpoint is read-only and serves only the current exchange to a same-origin browser on the Pi's loopback address. The Unix socket is private to the application account. No history is saved. Polling may show a state up to roughly one second late and pauses when Chromium is hidden.
- Each transcript is bounded to 240 characters and clamped to two visible lines to protect the portrait layout. A longer spoken answer can exceed what the mirror displays. Text from the server may arrive before the corresponding sound reaches the physical speaker; the playback flag only turns on after a successful local playback write.
- The wake CLI and Flask are separate foreground processes. They require separate shutdown and have no systemd or kiosk autostart wiring. If the wake CLI exits while Flask remains up, the assistant area shows unavailable. If Flask exits, wake detection continues until the CLI is stopped.
- Pi audio quality, wake accuracy, LED electrical behavior, and idle resource use require physical validation before merge. No new tools, smart-home integration, briefing, or boot service is included.
