# Magic Mirror V2

Pass 1 is a small Flask foundation for a Raspberry Pi 4B running 64-bit Debian 12 Bookworm. It serves a placeholder dashboard and a health response. Hardware, Pi-hole, Chromium kiosk integration, assistant, and other mirror features are outside this pass.

## Requirements

- Python 3.11 or newer (Debian 12 supplies 3.11)
- `python3-venv` and `python3-pip` from Debian, if they are not already installed
- Network access to PyPI for the first dependency installation
- No root access to run the application

The only runtime libraries are Flask and python-dotenv. `pytest` is used for development. `requirements.txt` pins direct and transitive Python packages to tested versions; it does not install system packages.

## Project layout

```text
magic_mirror/
  __main__.py           manual startup and shutdown
  app.py                Flask application factory
  config.py             environment configuration and logging
  dashboard/            routes and placeholder template
tests/                  hardware-free application and configuration tests
scripts/run_pi.sh       manual Raspberry Pi launcher
.env.example            safe local configuration example
requirements.txt        pinned Python dependencies
```

Future dashboard features belong under `magic_mirror/dashboard/`. A future assistant integration can be added as a separate package when its requirements are defined.

## Raspberry Pi installation

On a Pi with the repository already cloned, run these commands as your normal user from the repository directory:

```bash
cd magic_mirror_v2
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m pytest -q
./scripts/run_pi.sh
```

If `python3 -m venv .venv` reports that `ensurepip` is unavailable, install Debian's `python3-venv` package using your normal package administration process, then repeat the command. No system Python packages are changed by the commands above. `cp` is needed only once; edit `.env` locally for future runs. The launcher changes into the project directory and uses its `.venv`.

## Manual operation

With the default `.env`, open `http://127.0.0.1:5000/` in Chromium on the Pi. Check the server from a second terminal:

```bash
curl --fail --silent http://127.0.0.1:5000/health
```

The expected response is `{"status":"healthy"}`. Stop the foreground server with **Ctrl+C**. You can also start it directly with `.venv/bin/python -m magic_mirror`. The server binds to `127.0.0.1` by default. To bind elsewhere, explicitly set `MIRROR_HOST` in `.env` or the process environment. `MIRROR_PORT` defaults to `5000`; `MIRROR_LOG_LEVEL` defaults to `INFO`. Process environment variables take precedence over `.env`. `.env` is local and ignored by Git.

This is Flask's manual server, intended for local development and initial hardware acceptance. There is no systemd unit or kiosk autostart configuration in Pass 1.

## Troubleshooting

- A `Configuration error` names an invalid `MIRROR_HOST`, `MIRROR_PORT`, or `MIRROR_LOG_LEVEL`. Check `.env` and any exported environment overrides. Port must be an integer from 1 to 65535.
- `Address already in use` means the selected port is occupied. Choose a free port in `.env`; do not stop an unrelated service.
- `ModuleNotFoundError` usually means dependencies were not installed into `.venv` or the wrong Python interpreter was used. Repeat the installation command and launch through `scripts/run_pi.sh`.
- If Chromium cannot reach the page, verify `/health` locally first and make sure Chromium runs on the same Pi. Localhost binding intentionally does not expose the server to other devices.
- If dependency installation fails, verify Python is at least 3.11, PyPI is reachable, and `.venv/bin/python -m pip` is being used.

## Raspberry Pi acceptance checklist

- [ ] On 64-bit Debian 12, create `.venv` and install `requirements.txt` without changing system Python packages.
- [ ] Run all tests successfully on the Pi.
- [ ] Start `./scripts/run_pi.sh`; Chromium on the Pi displays the title and running status.
- [ ] `curl http://127.0.0.1:5000/health` returns `{"status":"healthy"}`.
- [ ] Press Ctrl+C; the server stops cleanly and releases its port.
- [ ] Confirm the application's default bind address is localhost and existing Pi-hole behavior is unaffected.
