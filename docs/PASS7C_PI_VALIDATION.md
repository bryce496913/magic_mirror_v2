# Pass 7C Raspberry Pi validation

This PR is stacked on Pass 7B. Wait for project-lead acceptance of the earlier passes before physical installation. Preserve the Pi's existing `.venv`, private `.env`, wake model, PipeWire USB microphone, MAX98357A output, Pi-hole CA/password, and LED BCM22/27/24 configuration. Stop the running wake and Flask processes cleanly before switching branches.

```bash
cd ~/magic_mirror_v2
git fetch origin main pass-7c-web-search
git switch pass-7c-web-search || git switch --track origin/pass-7c-web-search
git pull --ff-only origin pass-7c-web-search
test -x .venv/bin/python || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-wake.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

In the ignored local `.env`, retain the verified audio/wake/OpenAI settings and `MIRROR_HOST=127.0.0.1`. Set `MIRROR_TOOLS_ENABLED=true` and **explicitly** set `MIRROR_SEARCH_ENABLED=true` for this test. Keep the default search limits initially: two per conversation, three sources, 12-second request deadline, 300-second cache, and ten per UTC day. The global `MIRROR_TOOL_TIMEOUT_SECONDS` should remain at least 12 seconds. Do not display or attach `.env` or the key in a report.

Start Flask with `./scripts/run_pi.sh` in terminal A and check `curl --fail --silent http://127.0.0.1:5000/health`. Open `http://127.0.0.1:5000/` in local Chromium. Start the wake listener with `.venv/bin/python -m magic_mirror.assistant.wake_cli --start` in terminal B. Before saying the wake phrase, confirm its idle CPU/RSS remains appropriate and it has no OpenAI connection (`ss -tnp` with the wake process ID); normal dashboard weather/news refreshes are separate processes.

Say the wake phrase, then ask one current question such as “What's the latest Raspberry Pi OS release?” or “What's the latest technology news?” Record whether the Realtime model chose `search_web`; do not force repeated paid calls if it did not. If it searched, verify the LED/dashboard remain in processing until the actual spoken answer, the answer names important sources, and up to three cited source titles appear as clickable links on the local dashboard. Check the links correspond to the current answer; no link should open automatically. Ask a simple stable fact or arithmetic question and confirm it does not trigger a search. If useful, repeat the same query within five minutes and confirm the tool reports a cache hit without a second hosted request. Check a follow-up, manual cancellation (`c` then Enter), spoken sleep or `e` then Enter, and a second wake cycle. Confirm source links clear on a new question, interruption, and return to idle, while the dashboard weather/news/Pi-hole/system modules keep working.

The tool log should show allowlisted completion status only, never queries, keys, raw provider responses, or hidden instructions. Search usage counts may appear in the tool result but are not sent to Flask as history. Stop the wake listener with `q` then Enter or Ctrl+C, stop Flask with Ctrl+C, and confirm no microphone/speaker stream or OpenAI connection remains. Record actual search/answer latency, count of provider calls, source relevance and dates, any unavailable state, and the full test result. Do not alter DNS, Pi-hole, GPIO boot setup, systemd, or Chromium autostart. Automated tests are mocked and do not prove physical audio, Chromium layout, network access, or provider billing behavior.
