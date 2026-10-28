# Pass 4 Raspberry Pi RGB LED validation

Run these commands on the Raspberry Pi 4B as the normal user. Stop the Flask app before running the standalone LED test so only one process owns the pins. No sudo, systemd change, reboot, or Pi-hole change is needed.

## Install and run automated tests

```bash
cd magic_mirror_v2
git fetch origin
git switch --track origin/pass-4-rgb-led
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

If the branch already exists locally, use `git switch pass-4-rgb-led` and `git pull --ff-only`. The pinned `lgpio` package has a Python 3.11 ARM64 wheel for Debian 12. Confirm the test suite passes before touching the LED.

## Exercise the physical LED

The verified active-high BCM mapping is red 22 (physical pin 15), green 27 (pin 13), and blue 24 (pin 18). The standalone command uses these defaults from `.env.example` and claims GPIO only for the duration of the test. It runs even when `MIRROR_LED_ENABLED=false`, because invoking it is the explicit hardware test action.

```bash
test -f .env || cp .env.example .env
.venv/bin/python -m magic_mirror.hardware.led_test --duration 1
.venv/bin/python -m magic_mirror.hardware.led_test --channel red --duration 1
.venv/bin/python -m magic_mirror.hardware.led_test --channel green --duration 1
.venv/bin/python -m magic_mirror.hardware.led_test --channel blue --duration 1
.venv/bin/python -m magic_mirror.hardware.led_test --duration 1
```

The cycle should show **white → green → blue → off**. Each single-channel command should light only its named color and leave the LED off when it exits. The command prints the BCM pins it used. Repeat the cycle without a reboot to confirm pins were released. Press Ctrl+C during a cycle and confirm the LED goes off; then repeat the cycle. If pins are unavailable, the command prints an LED error and exits without starting Flask.

## Optional Flask ownership check

With Flask stopped, set `MIRROR_LED_ENABLED=true` and `MIRROR_LED_INITIAL_STATE=idle` in the ignored local `.env`, then start it:

```bash
./scripts/run_pi.sh
```

Confirm the LED lights white. In another terminal, verify `curl --fail --silent http://127.0.0.1:5000/health` returns `{"status":"healthy"}` and the dashboard still renders. Press Ctrl+C in the Flask terminal; confirm the LED goes off, then run the standalone cycle again to confirm the pins were released. Restore `MIRROR_LED_ENABLED=false` and `MIRROR_LED_INITIAL_STATE=off` in `.env` when finished. The Pi-hole integration and dashboard do not use the GPIO controller.

If the LED is dark or the app reports a GPIO error, confirm the active-high wiring and BCM pin assignments, check that another process is not using the pins, and verify `lgpio` installed in `.venv`. Do not run a second LED test while the app owns GPIO.
