# Pass 7D weather location validation

This change recognizes `New York City` and `NYC` as aliases for **New York, New York, United States**. The alias is pinned to both state and country, and any explicit qualifier must still match the geocoder result. City, country, and state names otherwise use exact case-insensitive matching with normalized whitespace; arbitrary similar names are not substituted. `USA`, `U.S.A.`, and `NY` are recognized only in their specific United States context. Ambiguous city results still request clarification. No new weather provider or API request is used for alias resolution.

On the Raspberry Pi, stop the wake listener and Flask cleanly, then update the feature branch while preserving the ignored `.venv`, `.env`, wake model, and Pi-hole private files:

```bash
cd ~/magic_mirror_v2
git fetch origin main pass-7d-weather-location
git switch pass-7d-weather-location || git switch --track origin/pass-7d-weather-location
git pull --ff-only origin pass-7d-weather-location
test -x .venv/bin/python || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-wake.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

Keep the existing local `.env` and device settings. Start Flask with `./scripts/run_pi.sh` and verify `curl --fail --silent http://127.0.0.1:5000/health`. In another terminal, start the wake listener with `.venv/bin/python -m magic_mirror.assistant.wake_cli --start`.

Ask for current weather in “New York City”, “New York”, and “New York City, USA”. Confirm the spoken location is New York in the United States, with current model conditions when available. Ask for weather in “Springfield” and confirm genuine ambiguity leads to a clarification request. Ask for current Tokyo weather **and** tomorrow's Tokyo forecast in one question. Confirm the assistant issues both tool calls, receives separate current and tomorrow results, and speaks tomorrow's forecast only when the forecast tool reports it available. A successful direct tomorrow forecast does not prove why an earlier combined voice request omitted it; capture sanitized tool stages and availability statuses if it recurs. Avoid recording transcripts, location arguments, coordinates, or credentials in shared logs.

Stop the wake listener with `q` then Enter or Ctrl+C and stop Flask with Ctrl+C. Confirm the microphone, speaker, LED, and dashboard return to their normal idle state. Automated mocks verify location selection and tool continuation; physical Pi testing is still required for Open-Meteo results and the model's choice to call both tools.
