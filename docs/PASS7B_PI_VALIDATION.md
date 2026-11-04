# Pass 7B Raspberry Pi validation

Use the existing Raspberry Pi installation and private `.env`. Preserve `.venv`, the local wake model, Pi-hole password/CA files, PipeWire settings, LED wiring, and dashboard configuration. Pass 7A is merged into `main`; this branch includes its Realtime, transcript, and watchdog fixes. Stop the existing mirror and wake processes cleanly before switching branches.

```bash
cd ~/magic_mirror_v2
git fetch origin main pass-7b-live-weather
git switch pass-7b-live-weather || git switch --track origin/pass-7b-live-weather
git pull --ff-only origin pass-7b-live-weather
test -x .venv/bin/python || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-wake.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

Edit the existing ignored `.env` locally: keep the previously verified `OPENAI_API_KEY`, `MIRROR_AUDIO_INPUT_DEVICE=pipewire`, physical source check, `MIRROR_AUDIO_OUTPUT_DEVICE=MAX98357A`, 48 kHz mono int16, local gain, wake model path, Pi-hole CA/password paths, and LED BCM22/27/24 mapping. Set `MIRROR_HOST=127.0.0.1`, `MIRROR_TOOLS_ENABLED=true`, both `MIRROR_WEATHER_LATITUDE` and `MIRROR_WEATHER_LONGITUDE` to your own location, and optionally `MIRROR_WEATHER_LOCATION_NAME` to a spoken label. Keep the tested `MIRROR_WEATHER_UNIT` preference for the dashboard; voice tools provide both Fahrenheit and Celsius from that single source response. Defaults for `MIRROR_TOOL_TIMEOUT_SECONDS=15`, `MIRROR_TOOL_MAX_OUTPUT_BYTES=2048`, and `MIRROR_VOICE_WEATHER_CACHE_SECONDS=600` are suitable initially. Do not display or attach `.env` in reports.

The weather HTTP client bounds each request to five seconds and the tool dispatcher bounds the whole weather operation, including optional city geocoding, to 15 seconds by default. A timeout becomes a status-only tool result and the assistant requests one continuation. The Pass 7A Realtime idle deadline is 60 seconds by default, so the tool has time to finish or time out before the idle watchdog expires; its response deadline tracks active server responses. Keep the configured Realtime idle limit comfortably above the tool timeout if changing either value. The Pass 7A receive-deadline and Python 3.11 watchdog fixes remain active while weather runs.

Start the dashboard in one terminal with `./scripts/run_pi.sh`, then verify `curl --fail --silent http://127.0.0.1:5000/health`. Start the wake listener in another terminal with `.venv/bin/python -m magic_mirror.assistant.wake_cli --start`. While the listener is idle, verify no OpenAI connection or weather request is created by that process, for example with `ss -tnp` and the wake process ID. The dashboard's independent weather refresh may still contact Open-Meteo; distinguish its PID from the wake listener. The idle LED should be white.

Say the configured wake phrase once, then ask a small set of questions: “What's the weather outside?”, “Will it rain this afternoon?”, “What's the forecast for tomorrow?”, “How cold will it get tonight?”, “What's the weather in Auckland?”, and “Will it rain this weekend?” For every temperature the assistant mentions, check that it says Fahrenheit first, then Celsius, including current, feels-like, high, low, and hourly-window readings. Repeat one question with each local `MIRROR_WEATHER_UNIT` setting if practical; the dashboard preference should remain as configured while speech still includes both units. Check that replies identify the location and local timeframe, do not describe model estimates as sensor observations, and do not invent missing numbers. Check that repeated questions within ten minutes reuse a fresh voice cache and avoid extra weather calls. Try an ambiguous city such as “Springfield” and confirm the assistant asks for clarification rather than silently selecting one. If the model does not elect a function call, record that outcome; do not keep repeating paid voice queries merely to force one.

Check a follow-up in the same conversation, manual cancellation (`c` then Enter), “go to sleep” or `e` then Enter, and a second wake/conversation cycle. Confirm the dashboard, LED, audio devices, and half-duplex speaker feedback prevention still work. Stop the wake listener with `q` then Enter or Ctrl+C, and stop Flask with Ctrl+C. Verify no audio streams or OpenAI connection remain. Do not modify DNS, Pi-hole, GPIO boot configuration, autostart, or systemd services.

Record actual device latency, idle CPU/RSS, tool-call counts, any ambiguous-location or provider-failure behavior, and the exact test result. The automated suite uses fake HTTP responses and fake audio; it does not prove physical microphone, speaker, wake, LED, Chromium, or provider availability on the Pi.
