# Pass 7D Raspberry Pi display validation

This PR is stacked on Pass 7C and should be installed for physical acceptance only after the project lead approves its earlier dependencies. Preserve the existing `.venv`, ignored `.env`, wake model, Pi-hole CA/password, PipeWire USB microphone, MAX98357A speaker, and GPIO BCM22/27/24 wiring. Stop the current Flask and wake processes cleanly before switching branches.

```bash
cd ~/magic_mirror_v2
git fetch origin main pass-7d-tool-dashboard
git switch pass-7d-tool-dashboard || git switch --track origin/pass-7d-tool-dashboard
git pull --ff-only origin pass-7d-tool-dashboard
test -x .venv/bin/python || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-wake.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

Keep the tested private `OPENAI_API_KEY`, audio gain, wake phrase/model, and exact PipeWire source configuration. Set `MIRROR_HOST=127.0.0.1`, `MIRROR_WAKE_ENABLED=true`, and `MIRROR_TOOLS_ENABLED=true`; set `MIRROR_SEARCH_ENABLED=true` only if explicitly testing search costs. Keep your existing dashboard weather coordinates and `MIRROR_WEATHER_UNIT`. No new `.env` setting is needed for the contextual panel. Do not print or attach `.env`.

Start Flask in terminal A with `./scripts/run_pi.sh`; check `curl --fail --silent http://127.0.0.1:5000/health`, then open `http://127.0.0.1:5000/` in local Chromium. Start the wake listener in terminal B with `.venv/bin/python -m magic_mirror.assistant.wake_cli --start`. Confirm local white idle, no OpenAI connection, and that refreshing Chromium does not start a session, tool call, microphone stream, or GPIO owner.

Wake the assistant and ask “What's the weather in Auckland?” while the always-visible weather module remains set to your own location. During the tool request, look for a compact loading state; after completion, confirm the assistant panel shows Auckland, its current conditions, temperature, precipitation probability where supplied, and freshness. The always-visible weather module must keep its own location and refresh schedule. Ask “Will it rain tomorrow?” and check the forecast period, high/low, and available precipitation probability. Missing data should be omitted or show a concise unavailable state, never invented. If the model does not choose a tool, record that instead of repeatedly making paid voice calls.

If search is explicitly enabled, ask one current question and verify a short topic/summary with up to three readable source labels. Publication dates should appear only when supplied, and links should open only after a deliberate click. Confirm no raw URL, HTML markup, API key, or hidden prompt appears on screen. Check that the displayed result belongs to the current spoken question and response. Press `c` then Enter during an answer or tool request, ask a new question, and confirm the old loading/result panel and links do not reappear. Say “go to sleep” or press `e` then Enter; confirm idle clears the contextual result. Refresh Chromium during loading and after completion to confirm the current snapshot is restored without interrupting audio. Stop the wake CLI with `q` then Enter or Ctrl+C and Flask with Ctrl+C; confirm stopped/unavailable status and no active audio stream.

Check portrait readability at the installed orientation and resolution (normally 600×1024 after rotating a 1024×600 panel), no page scrollbars or clipping, legible source labels several feet away, and the clock/weather/news/Pi-hole/system regions. Record actual latency and resource measurements; automated mocks do not prove hardware behavior. Do not change DNS, Pi-hole configuration, GPIO boot settings, systemd, or Chromium autostart.

Optional development visual check, with Chromium already installed, uses an additional **development-only** package in the project virtual environment:

```bash
.venv/bin/python -m pip install 'playwright==1.62.0'
.venv/bin/python scripts/visual_check_pass7d.py
```

The script serves a local fixture, verifies browser reload, safe DOM insertion, source-link filtering, all existing dashboard regions, and no page overflow at 600×1024 and 1024×600. It writes [mocked weather](pass7d-weather-portrait.png) and [mocked search](pass7d-search-portrait.png) screenshots. These are layout previews, not real weather or live search evidence.
