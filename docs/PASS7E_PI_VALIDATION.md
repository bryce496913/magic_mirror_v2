# Pass 7E Raspberry Pi validation

This is a physical acceptance checklist, not a claim of hardware approval. Pass 7E is stacked on Pass 7D until that PR merges. Test only after the project lead approves the prerequisite passes. Use the existing Pi 4B and normal application user. Preserve `.venv`, `.env`, `.private/`, the wake model directory, Pi-hole application password/CA, PipeWire source, MAX98357A output, and BCM22/27/24 wiring. Stop existing foreground Flask and wake processes with Ctrl+C before switching branches. Do not change Pi-hole, DNS, GPIO boot settings, systemd, or Chromium autostart.

1. **Install and inspect configuration.** Run the following without deleting `.venv` or copying `.env.example` over `.env`:

   ```bash
   cd ~/magic_mirror_v2
   git fetch origin pass-7e-tool-hardening
   git switch pass-7e-tool-hardening || git switch --track origin/pass-7e-tool-hardening
   git pull --ff-only origin pass-7e-tool-hardening
   test -f .env && test -d .private && test -x .venv/bin/python
   .venv/bin/python -m pip install -r requirements.txt -r requirements-wake.txt
   .venv/bin/python -m pip check
   git status --short
   ```

   Confirm locally, without printing or sharing secrets, that `MIRROR_HOST=127.0.0.1`, the tested PipeWire input/physical source, `MAX98357A` output, 48 kHz mono int16, gain `1.4`, wake model path, GPIO BCM22/27/24, and private Pi-hole files remain correct. Enable tools only for this planned test; enable search only when paid API calls are approved. Keep daily and per-conversation limits low. `MIRROR_SEARCH_ESTIMATED_USD_PER_REQUEST` is optional and does not enforce a budget.

2. **Run all automated tests.** From the repository directory run `.venv/bin/python -m pytest -q` and record the result. These tests mock paid APIs and hardware. If the development linter is already installed, run `.venv/bin/ruff check .`.

3. **Baseline wake and voice.** In terminal A start `./scripts/run_pi.sh`; check `curl --fail --silent http://127.0.0.1:5000/health` and open the dashboard in local Chromium. In terminal B start `.venv/bin/python -m magic_mirror.assistant.wake_cli --start`. Confirm white idle LED, local-only wake detection, green listening, blue response, clear speech from the MAX98357A, and no speaker feedback into the USB microphone. Ask one ordinary question and a follow-up. Record actual speech-end-to-audio latency; no fixed latency claim is made.

4. **Weather conversation.** With tools enabled, ask for current weather at a named city, then a forecast for tomorrow. Confirm the spoken and contextual results identify the queried city, units, timeframe, and unavailable fields honestly. The always-visible dashboard weather must retain its configured location. If the model chooses no tool, record that and stop repeated paid attempts.

5. **Search conversation.** Only with explicit search opt-in and an authorized cost ceiling, ask one current-information question. Confirm a single hosted search request, a short sourced answer, and sensible uncertainty if results are absent or conflicting. Check the wake CLI's budget-reserved search counter and the private daily count without printing queries or keys. Do not repeatedly retry an unavailable provider.

6. **Tool-result display.** Confirm loading, then the actual matching weather/search result beneath the assistant exchange. Search links must be cited public URLs and require a deliberate click. The clock, news, Pi-hole, system, and dashboard weather regions must remain readable at the installed portrait orientation, normally 600×1024. No clipping or scrolling should appear.

7. **Cancel a response.** While a tool or spoken answer is active, press `c` then Enter in the wake terminal. Confirm playback stops, stale audio/text/tool panels clear, and another question is accepted after acoustic settling. The LED must reflect the actual resulting state.

8. **Sleep and reactivate.** Say “go to sleep” or press `e` then Enter. Confirm the OpenAI session and both audio streams close, the panel clears, and white idle returns. Repeat wake/sleep several times, including a false wake with no following question. No duplicate sessions or audio owners should appear.

9. **Refresh and access control.** Refresh Chromium while a tool is loading and again after completion. The current snapshot should restore without restarting the assistant or triggering a tool. `curl --fail --silent http://127.0.0.1:5000/api/assistant` should return only the bounded current snapshot. Verify the Flask bind remains `127.0.0.1`; do not expose this transcript endpoint on the LAN. A second wake command should fail due to the owner lock before claiming audio or GPIO.

10. **Temporary network failure.** During a scheduled, non-disruptive test window, temporarily interrupt only the Pi's upstream access if that can be done without affecting Pi-hole clients. Ask a single question and verify bounded connection failure, recoverable LED/panel state, and return to local idle. Restore access and reactivate. If the Pi is serving critical DNS clients, use the automated network-failure mocks instead and record this physical check as pending; never change Pi-hole DNS settings for this test.

11. **Provider failure recovery.** If safe to do so, run a separate wake session with a process-local invalid outbound proxy, without changing `.env`: `HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 .venv/bin/python -m magic_mirror.assistant.wake_cli --start`. Ask at most one weather/search question. Confirm unavailable/timeout is surfaced and the service returns to a usable state. Stop that process, then restart normally. If the transport ignores the proxy or voice connection cannot start, use mocked provider tests as evidence and mark this live check pending.

12. **Duplicate process.** While the normal wake CLI is running, invoke the same `--start` command from a third terminal. It must reject the second owner before opening the microphone, speaker, LED, or OpenAI session. The original conversation must continue.

13. **Idle resources and cost.** After at least one minute of white idle, record the wake CLI's CPU percent, RSS MiB, established TCP count, frames, activations, session duration, tool successes/failures/latency, budget-reserved search count, reported tokens, and estimated cost if configured. Also record `ps -o pid,%cpu,rss,etime,args -C python3` and `ss -tn state established` if available. Idle should show no OpenAI/search connection. The estimate is operator supplied and excludes Realtime/audio charges; check the current provider bill for actual cost.

14. **SIGINT and SIGTERM.** Stop the wake CLI with Ctrl+C, restart it, then send `kill -TERM <wake-cli-pid>` using its PID from `ps`. Confirm both exits are prompt and clean, Flask remains healthy, and restarting wake works without reboot. Avoid broad process-killing commands.

15. **Audio and GPIO release.** After each stop, verify no microphone/speaker stream remains with the existing audio device inspection/test CLI, the LED is off, and a fresh wake start can claim the same devices. Do not run standalone audio or LED tests while the wake CLI owns them.

16. **Pi-hole regression.** Confirm the dashboard's Pi-hole status still refreshes, `systemctl is-active pihole-FTL` reports active, and the Pi's normal DNS resolution works. Do not change the Pi-hole CA, password, service, or DNS configuration. Stop Flask with Ctrl+C after testing.

Record actual observations, measured latency and resource values, failures, and any skipped paid or disruptive checks for project-lead review. Never attach `.env`, `.private/`, full transcripts, raw audio, API keys, or Pi-hole credentials to a report.
