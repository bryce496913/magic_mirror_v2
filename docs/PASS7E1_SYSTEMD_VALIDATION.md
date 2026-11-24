# Pass 7E-1: Raspberry Pi user service validation

These units target the existing Raspberry Pi 5 installation at `/home/bryce/magic_mirror_v2` under `bryce` (UID 1000). They do not use root privileges, alter Chromium or Pi-hole, or copy secrets. The application itself reads its existing ignored `.env` via `python-dotenv` from the repository path. The dashboard and voice services are independent; stopping or failing one does not stop the other. The voice service starts after the user PipeWire and WirePlumber services have been requested and uses a bounded pre-start check of the selected PortAudio input/output formats and the verified default physical PipeWire source. The pre-start check opens **no audio streams** and makes no OpenAI connection. It waits at most 60 seconds per start attempt; systemd handles later retries.

Before installing, log in as `bryce` and confirm that the existing manual commands still work. Stop any foreground dashboard with Ctrl+C. Stop the wake or manual voice CLI with `q` then Enter or Ctrl+C, and wait for the LED to switch off and audio streams to close. The installer checks for matching foreground processes again before activation and refuses to create duplicate owners. Keep the existing `.venv`, `.env`, `.private/` wake model, PipeWire source selection, MAX98357A output, LED GPIO settings, Pi-hole files, and Chromium session. No dependency or configuration changes are required for this pass.

## Inspect and install without activation

```bash
cd /home/bryce/magic_mirror_v2
git fetch origin main pass-7e1-systemd-supervision
git switch pass-7e1-systemd-supervision || git switch --track origin/pass-7e1-systemd-supervision
git pull --ff-only origin pass-7e1-systemd-supervision
id -un; id -u
loginctl show-user bryce -p Linger
systemctl --user is-active pipewire.service wireplumber.service
stat -c '%a %U %n' .env
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
systemd-analyze verify --user systemd/user/magic-mirror-dashboard.service systemd/user/magic-mirror-voice.service
.venv/bin/python scripts/manage_user_services.py install
systemctl --user is-enabled magic-mirror-dashboard.service magic-mirror-voice.service
```

Expected: account `bryce`, UID `1000`, `Linger=yes`, active PipeWire and WirePlumber, and a private `.env` owned by `bryce` with no group/other permissions (for example mode `600`). If any prerequisite differs, stop and inspect it before activation. The installer never changes `.env` or enables/starts units. `is-enabled` should report `disabled` after a first install. It may return a nonzero status for disabled services; this is expected. If the unit files already differ from this checkout, installation stops rather than overwriting them.

## Explicit activation and inspection

Start the dashboard first, then voice. The voice service intentionally has no graphical session dependency. It fails rather than selecting another microphone or speaker when the configured route is unavailable. It is safe to leave the dashboard running while diagnosing voice startup.

```bash
cd /home/bryce/magic_mirror_v2
.venv/bin/python scripts/manage_user_services.py activate dashboard
systemctl --user status magic-mirror-dashboard.service --no-pager
curl --fail --silent http://127.0.0.1:5000/health
.venv/bin/python scripts/manage_user_services.py activate voice
systemctl --user status magic-mirror-voice.service --no-pager
systemctl --user show magic-mirror-dashboard.service magic-mirror-voice.service -p ActiveState -p SubState -p MainPID -p NRestarts
journalctl --user -u magic-mirror-dashboard.service -u magic-mirror-voice.service -n 50 --no-pager
```

The health response should be `{"status":"healthy"}`. Confirm exactly one dashboard and one wake process, the idle LED is white, the configured USB microphone remains the selected PipeWire physical source, and the MAX98357A remains the selected speaker. The existing assistant process lock rejects a second manual wake or voice owner; do not run one concurrently with the service. The dashboard reads assistant state via the existing private Unix socket, and Flask does not acquire GPIO or audio ownership. The systemd units contain only the nonsecret `MIRROR_LOG_LEVEL=INFO` override. Do not print `.env`, API keys, transcripts, or private certificate contents into logs or reports.

On a separate terminal, verify ordinary wake and follow-up interactions, weather and search tools as appropriate, on-screen transcripts, green listening/blue responding LED transitions, half-duplex speaker feedback protection, spoken sleep and a new wake cycle. Confirm the dashboard's weather, news, Pi-hole, system, clock, and `/health` remain functional. Idle wake listening should not open an OpenAI connection. Stop the voice service with `systemctl --user stop magic-mirror-voice.service`: the LED should turn off and audio resources release. A clean manual stop must stay stopped. Start it again explicitly with `systemctl --user start magic-mirror-voice.service`.

For a controlled crash-recovery test **only after hardware readiness is confirmed**, record `systemctl --user show -p MainPID --value magic-mirror-voice.service`, send `SIGKILL` to that exact voice PID, and verify one restart after the configured 15-second delay. Repeat separately for the dashboard, then confirm `/health` is healthy again. Do not kill by port or name. Check `journalctl --user -u magic-mirror-voice.service` and `journalctl --user -u magic-mirror-dashboard.service` for the exit and restart reasons. Stop both services normally and confirm neither immediately restarts. Re-enable/start them with `systemctl --user enable --now ...` only after the test. A controlled reboot test is optional after project-lead approval; enabled user units should start through the already enabled lingering user manager without a graphical login. This PR does not reboot or activate either service.

## Recovery limits and rollback

Both services use `Restart=on-failure`, a 15-second retry delay, and a five-start limit in 300 seconds. Repeated fast failures eventually leave the affected service failed; inspect its journal and configuration before `systemctl --user reset-failed <unit>` and an explicit `start`. A slow or unavailable PipeWire route causes the voice preflight to wait up to 60 seconds, then exit; the next systemd attempt may succeed once routing is ready. `TimeoutStopSec=45s` allows normal SIGTERM cleanup of microphone, speaker, GPIO, and dashboard workers. If that grace period is exceeded, systemd terminates the remaining service process group; inspect the cause before restarting. No custom infinite restart loop is used.

To return to the tested manual launch method, stop and disable the services, then remove only the recognized Magic Mirror user units:

```bash
cd /home/bryce/magic_mirror_v2
.venv/bin/python scripts/manage_user_services.py rollback
systemctl --user is-active magic-mirror-dashboard.service magic-mirror-voice.service
./scripts/run_pi.sh
```

In another terminal, start the wake assistant manually with `MIRROR_LOG_LEVEL=INFO .venv/bin/python -m magic_mirror.assistant.wake_cli --start`. Confirm `/health`, one audio owner, and expected LED behavior. Stop the CLI with `q` or Ctrl+C and Flask with Ctrl+C. Rollback leaves `.env`, `.venv`, `.private/`, PipeWire/WirePlumber, Pi-hole, Chromium, and GPIO configuration untouched. It does not disable user lingering or remove any unrelated user service.

Physical validation remains necessary: cloud tests cannot prove post-boot PipeWire routing, the selected physical microphone/speaker, GPIO release, Chromium readability, Realtime API behavior, or real Pi crash timing.

## Hardware acceptance checklist

- [ ] Manual processes are stopped before explicit activation; one dashboard and one voice owner start.
- [ ] `/health` is healthy, and dashboard data plus Chromium remain functional independently of voice.
- [ ] PipeWire resolves the verified USB source; audio uses MAX98357A and the existing settings.
- [ ] Wake, follow-up, spoken sleep, cancellation, transcripts, tools, and LED states work.
- [ ] Clean `systemctl --user stop` stays stopped and releases microphone, speaker, and GPIO.
- [ ] Controlled crashes restart after the delay, with no duplicate owner and healthy recovery.
- [ ] Reboot with lingering restores both enabled services without a graphical login, if approved.
- [ ] Rollback restores the manual commands without changing `.env`, Pi-hole, or Chromium.
