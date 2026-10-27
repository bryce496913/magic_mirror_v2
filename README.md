# Magic Mirror V2

Magic Mirror V2 is a local Flask dashboard for a Raspberry Pi 4B running 64-bit Debian 12 Bookworm. The clock and date use Chromium's local timezone. System information, weather, RSS headlines, and Pi-hole v6 data refresh independently in the background. Unconfigured or unreachable sources show unavailable or stale states. The assistant remains a placeholder.

![Dashboard preview at 1280×800](docs/dashboard-preview.png)

Preview captured in headless Chromium during development. Check contrast and spacing on the installed mirror display before approval.

## Requirements

- Python 3.11 or newer (Debian 12 supplies 3.11)
- `python3-venv` and `python3-pip` from Debian, if they are not already installed
- Network access to PyPI for the first dependency installation
- No root access to run the application

Runtime libraries are Flask, python-dotenv, psutil, requests, and defusedxml. `pytest` is used for development. `requirements.txt` pins direct and transitive Python packages to tested versions; it does not install system packages.

## Project layout

```text
magic_mirror/
  __main__.py           manual startup and shutdown
  app.py                Flask application factory
  config.py             environment configuration and logging
  data.py               background refresh schedules and cached snapshots
  providers/            read-only system, weather, news, and Pi-hole sources
  dashboard/            routes, template, local CSS, and small JavaScript modules
tests/                  hardware-free application and configuration tests
scripts/run_pi.sh       manual Raspberry Pi launcher
.env.example            safe local configuration example
requirements.txt        pinned Python dependencies
```

Future dashboard features belong under `magic_mirror/dashboard/`. The assistant integration can be added separately when its requirements are defined.

## Raspberry Pi installation

On a Pi with the repository already cloned, run these commands as your normal user from the repository directory:

```bash
cd magic_mirror_v2
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
.venv/bin/python -m pytest -q
./scripts/run_pi.sh
```

If `python3 -m venv .venv` reports that `ensurepip` is unavailable, install Debian's `python3-venv` package using your normal package administration process, then repeat the command. No system Python packages are changed by the commands above. The `.env` command preserves an existing local configuration. The launcher changes into the project directory and uses its `.venv`. Existing installations should rerun `.venv/bin/python -m pip install -r requirements.txt` for the Pass 3 providers.

## Manual operation

With the default `.env`, open `http://127.0.0.1:5000/` in Chromium on the Pi. Check the server from a second terminal:

```bash
curl --fail --silent http://127.0.0.1:5000/health
```

The expected response is `{"status":"healthy"}`. Stop the foreground server with **Ctrl+C**. You can also start it directly with `.venv/bin/python -m magic_mirror`. The server binds to `127.0.0.1` by default. To bind elsewhere, explicitly set `MIRROR_HOST` in `.env` or the process environment. Process environment variables take precedence over `.env`. `.env` is local and ignored by Git.

| Setting | Default | Purpose |
| --- | --- | --- |
| `MIRROR_HOST` | `127.0.0.1` | Bind address; change explicitly to expose the server. |
| `MIRROR_PORT` | `5000` | HTTP port, 1–65535. |
| `MIRROR_LOG_LEVEL` | `INFO` | Application log level. |
| `MIRROR_CLOCK_FORMAT` | `24` | `12` or `24` hour clock. |
| `MIRROR_DISPLAY_SCALE` | `1.0` | Optional text scaling, from `0.75` to `1.5`. |
| `MIRROR_MODULES` | `weather,news,pihole,system,assistant` | Comma-separated regions to show; empty hides all optional regions. |
| `MIRROR_WEATHER_LATITUDE`, `MIRROR_WEATHER_LONGITUDE` | blank | Set both coordinates to enable Open-Meteo. No physical location is assumed. |
| `MIRROR_WEATHER_UNIT` | `celsius` | `celsius` or `fahrenheit` for the Open-Meteo response and existing JSON fields. The display always puts °C first and °F second. |
| `MIRROR_NEWS_FEEDS` | blank | Up to five comma-separated HTTPS or HTTP RSS/Atom URLs. |
| `MIRROR_PIHOLE_URL` | `http://127.0.0.1` | Pi-hole v6 API base address on the same Pi. |
| `MIRROR_PIHOLE_PASSWORD_FILE` | blank | Optional absolute path to a local Pi-hole v6 application password file. |
| `MIRROR_SYSTEM_INTERVAL` | `15` | System refresh seconds, minimum 5. |
| `MIRROR_WEATHER_INTERVAL` | `900` | Weather refresh seconds, minimum 300. |
| `MIRROR_NEWS_INTERVAL` | `1800` | News refresh seconds, minimum 300. |
| `MIRROR_PIHOLE_INTERVAL` | `60` | Pi-hole refresh seconds, minimum 15. |

The clock uses the Pi's configured local timezone through Chromium. Change the Pi's system timezone if the clock shows the wrong local time, then refresh Chromium. The clock checks the current time at minute boundaries and refreshes when the page becomes visible again. The browser requests one cached `/api/dashboard` JSON snapshot every 15 seconds while visible. Up to four background workers, one per enabled data provider, run on independent schedules; they never run in page routes or overlap their own refreshes. Successful snapshots stay in memory until the process exits; failed refreshes retain last-known-good values with a stale label and a UTC last-success timestamp. The endpoint returns data only, never feed URLs, coordinates, passwords, or session IDs. No external HTTP requests occur during application initialization or page rendering.

On shutdown, all workers receive a stop signal and no new refresh starts. The application waits up to seven seconds in total for active work. If a bounded network call lasts longer, the coordinator reports that workers are still alive and retains their handles; another `stop()` can wait again, and `start()` refuses to create replacements until those workers finish. When the foreground Flask process exits, any remaining daemon worker ends with that process. This does not alter Pi-hole or other services.

Open-Meteo supplies current temperature, condition, high/low, feels-like when available, and tomorrow's short forecast. Each weather reading shows Celsius in larger white type followed by Fahrenheit in smaller gray type. Both values are calculated from the same response; no second API request is made. Existing JSON fields (`temperature`, `high`, `low`, `feels_like`, and `unit`) keep their configured source units; additional `_c` and `_f` fields supply the dual display. RSS/Atom feeds supply at most three deduplicated titles with publisher and publication time when available. News text is inserted as text, and article scripts or images are never loaded. The Pi-hole provider implements the v6 `/api/stats/summary`, `/api/dns/blocking`, and session authentication contract documented by Pi-hole; it only reads statistics and blocking status after authentication. The system provider reads psutil and Linux thermal data without privilege. CPU temperature shows an unavailable marker if no sensor is present.

For the hardware validation steps, including a private Pi-hole credential setup procedure and version check, see [Pass 3 Raspberry Pi validation](docs/PASS3_PI_VALIDATION.md).

This is Flask's manual server, intended for local development and hardware acceptance. There is no systemd unit or kiosk autostart configuration in Pass 3.

## Raspberry Pi display check

After starting Flask in one terminal, open `http://127.0.0.1:5000/` in Chromium on the Pi. For an optional temporary full-screen kiosk test, launch Chromium manually from the Pi's graphical session:

```bash
chromium --kiosk --app=http://127.0.0.1:5000/ --no-first-run --disable-session-crashed-bubble
```

Verify the live clock advances across a minute boundary without refresh, the date and 12/24-hour preference match `.env`, the black background fills the screen, every enabled region is readable from several feet away, and no scrollbars appear. Refresh once to confirm the layout and clock recover. Close the Chromium window, then press Ctrl+C in the Flask terminal and check that `/health` is no longer reachable. This command does not change Chromium startup settings.

## Troubleshooting

- A `Configuration error` names an invalid `MIRROR_HOST`, `MIRROR_PORT`, or `MIRROR_LOG_LEVEL`. Check `.env` and any exported environment overrides. Port must be an integer from 1 to 65535.
- `Address already in use` means the selected port is occupied. Choose a free port in `.env`; do not stop an unrelated service.
- `ModuleNotFoundError` usually means dependencies were not installed into `.venv` or the wrong Python interpreter was used. Repeat the installation command and launch through `scripts/run_pi.sh`.
- If Chromium cannot reach the page, verify `/health` locally first and make sure Chromium runs on the same Pi. Localhost binding intentionally does not expose the server to other devices.
- If dependency installation fails, verify Python is at least 3.11, PyPI is reachable, and `.venv/bin/python -m pip` is being used.

## Raspberry Pi acceptance checklist

- [ ] On 64-bit Debian 12, create `.venv` and install `requirements.txt` without changing system Python packages.
- [ ] Run all tests successfully on the Pi.
- [ ] Start `./scripts/run_pi.sh`; Chromium on the Pi displays the clock, date, and enabled data regions.
- [ ] Confirm the clock advances without refresh and uses the Pi's timezone; check both clock formats if desired.
- [ ] Confirm the layout is readable and free of scrollbars at the installed display resolution.
- [ ] `curl http://127.0.0.1:5000/health` returns `{"status":"healthy"}`.
- [ ] Press Ctrl+C; the server stops cleanly and releases its port.
- [ ] Confirm the application's default bind address is localhost and existing Pi-hole behavior is unaffected.
