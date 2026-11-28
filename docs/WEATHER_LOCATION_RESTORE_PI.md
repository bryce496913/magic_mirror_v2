# Raspberry Pi validation: restored weather locations

PR #17 added conservative weather-city aliases; PR #18 reverted them. This PR restores the `New York City` and `NYC` mappings to Open-Meteo's `New York` result, pinned to New York state and the United States. Other city names, including Manhattan and Tokyo, still require exact geocoder names and use the existing qualifier and ambiguity rules. Explicitly supplied blank, null, unsupported, or mismatched locations return an unavailable result rather than the dashboard's configured default. Only an omitted `location` uses that default. No extra weather API request is made solely to convert temperatures.

Test on the Raspberry Pi only after project-lead review. Preserve the current ignored `.env`, `.venv`, wake model, Pi-hole files, audio routes, GPIO configuration, Chromium session, and any separately installed user services. Stop the dashboard and wake services or foreground processes cleanly before switching branches. This PR does not install, enable, or modify systemd units.

```bash
cd /home/bryce/magic_mirror_v2
git fetch origin main
git fetch origin refs/heads/restore-weather-location-resolution:refs/remotes/origin/restore-weather-location-resolution
git switch restore-weather-location-resolution || git switch --track origin/restore-weather-location-resolution
git pull --ff-only origin restore-weather-location-resolution
.venv/bin/python -m pytest -q tests/test_weather_tools.py tests/test_tool_calls.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check magic_mirror/assistant/tools/weather.py magic_mirror/assistant/tools/registry.py tests/test_weather_tools.py
.venv/bin/python -m pip check
```

The following direct check uses the existing local configuration and bounded Open-Meteo calls. It does not open the microphone or OpenAI connection and prints only public city labels and availability, not `.env` or API credentials:

```bash
.venv/bin/python - <<'PY'
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from magic_mirror.assistant.tools.weather import WeatherTools
from magic_mirror.config import Settings

load_dotenv(Path('.env'), override=False)
weather = WeatherTools(Settings.from_env())

async def check():
    for city in ('New York City', 'NYC', 'Manhattan', 'Manhattan, Kansas, USA',
                 'Tokyo, Japan', 'Paris, France', 'Springfield', 'No Such City Qzxq'):
        result = await weather.current({'location': city})
        print(city, result['availability'], result.get('location') or result.get('reason'))
    tomorrow = await weather.forecast({'location': 'Tokyo, Japan', 'period': 'tomorrow'})
    print('Tokyo tomorrow', tomorrow['availability'], len(tomorrow.get('days', [])))
    invalid = await weather.current({'location': '  '})
    print('Explicit blank', invalid['availability'], invalid.get('reason'))

asyncio.run(check())
PY
```

Expected when Open-Meteo is available: `New York City` and `NYC` report New York, New York, United States; Manhattan resolves to New York, while the Kansas-qualified request resolves to Kansas. Tokyo and Paris retain their own country labels. Springfield should ask for clarification if the returned cities remain genuinely ambiguous. The unsupported and blank requests must never return the dashboard default. Tokyo tomorrow should contain one forecast day when provider data is available. Provider outages or changed geocoder records should be reported as such; do not substitute an unrelated city.

After the direct checks, the hardware tester may start the existing dashboard and wake assistant using the already approved manual or user-service procedure. Ask once each for current weather in New York City, Manhattan, and Tokyo, plus tomorrow in Tokyo. Check that tool results, spoken answers, and dashboard text agree on location, availability, timeframe, and Fahrenheit/Celsius units. Check `/health` and unrelated dashboard modules. Keep paid voice tests bounded. Stop the assistant and dashboard cleanly; confirm audio and LED ownership returns to the established idle/stopped state. Record actual Pi results and any provider mismatch without sharing private configuration or transcripts.
