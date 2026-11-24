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

Systemd provides no interactive stdin. Wake detection, spoken questions, and the spoken “go to sleep” command still use the same local wake/voice state machine; “go to sleep” ends the conversation and returns to white wake-ready idle, rather than stopping the service. The terminal-only `c`, `e`, and `q` keyboard controls are unavailable. Use these supported service controls as `bryce`:

```bash
systemctl --user stop magic-mirror-voice.service
systemctl --user start magic-mirror-voice.service
systemctl --user restart magic-mirror-voice.service
```

`stop` or `restart` interrupts an active conversation and releases its audio/GPIO owner before another instance starts. There is no separate per-reply `c` cancellation in supervised mode; use `restart` only when ending the whole conversation is acceptable. Do not start a second wake or manual voice CLI alongside the service.

On a separate terminal, verify ordinary wake and follow-up interactions, weather and search tools as appropriate, on-screen transcripts, green listening/blue responding LED transitions, half-duplex speaker feedback protection, spoken sleep and a new wake cycle. Confirm the dashboard's weather, news, Pi-hole, system, clock, and `/health` remain functional. Idle wake listening should not open an OpenAI connection.

### Distinct shutdown and recovery checks

**Graceful systemd stop:** Run `systemctl --user stop magic-mirror-voice.service` during local idle, then during an active conversation. Its SIGTERM handler asks the controller to stop, closes the microphone, speaker, and assistant socket, and releases GPIO on normal cleanup. Confirm the journal shows a clean stop, `MainPID=0`, no wake process remains, and the LED is off. **Only while the voice service is stopped**, run `.venv/bin/python -m magic_mirror.audio.audio_test selected`, `.venv/bin/python -m magic_mirror.audio.audio_test record --seconds 3`, and `.venv/bin/python -m magic_mirror.hardware.led_test --duration 0.5` to check the configured route, microphone ownership release, and GPIO reacquisition. The audio test keeps its short recording in memory. The service must remain stopped rather than immediately restarting. `systemctl --user start magic-mirror-voice.service` should return to one white idle owner. Repeat for the dashboard and confirm `/health` stops, then becomes healthy after an explicit start.

**Unexpected process failure:** A nonzero application exit or other failure triggers `Restart=on-failure` unless the start-rate limit is reached. If one occurs naturally, inspect `Result`, `ExecMainStatus`, `NRestarts`, and the bounded journal output below. After the delayed restart, verify exactly one owner, white idle LED, correct PipeWire source and MAX98357A output, a successful wake/answer/sleep cycle, and healthy dashboard `/health`. Do not infer that cleanup was graceful from a successful restart.

**Forced SIGKILL test:** Only after basic audio/LED acceptance, send SIGKILL to the **recorded main PID** of one unit while it is idle. This is a deliberate crash test, not a clean stop:

```bash
unit=magic-mirror-voice.service
pid=$(systemctl --user show -p MainPID --value "$unit")
test "$pid" -gt 1 && kill -KILL "$pid"
systemctl --user show "$unit" -p ActiveState -p SubState -p Result -p ExecMainStatus -p NRestarts -p MainPID
journalctl --user -u "$unit" -b -n 80 --no-pager
```

The SIGKILL handler cannot run Python cleanup. Kernel process exit releases file descriptors and the ownership lock, but an LED may remain latched in its last color until the restarted process initializes it. Verify one restarted voice PID after the 15-second delay, white idle LED, no duplicate microphone owner, and a successful new interaction. Then perform a graceful `systemctl --user stop` and verify LED off and audio release. Repeat the PID-specific test for `magic-mirror-dashboard.service` and confirm `/health` is healthy after its restart. Do not kill by port or name, and do not assume GPIO cleanup occurred at the instant of SIGKILL.

**Restart-rate-limit exhaustion:** The units allow five starts in 300 seconds. Do not repeatedly crash the physical audio process merely to force this state during routine acceptance. If repeated fast failures exhaust the limit, `systemctl --user status <unit>` and its journal should show a start-limit failure; the service stays failed rather than cycling indefinitely. Fix the underlying problem, confirm audio routing and private files, then use `systemctl --user reset-failed <unit>` followed by `systemctl --user start <unit>`. Check the resulting PID, LED, audio ownership, and `/health` as appropriate. Avoid copying raw transcripts or private values from journals into reports.

A controlled reboot test is optional after project-lead approval; enabled user units should start through the already enabled lingering user manager without a graphical login. This PR does not reboot or activate either service.

## Recovery limits and rollback

Both services use `Restart=on-failure`, a 15-second retry delay, and a five-start limit in 300 seconds. Repeated fast failures eventually leave the affected service failed; inspect its journal and configuration before `systemctl --user reset-failed <unit>` and an explicit `start`. A slow or unavailable PipeWire route causes the voice preflight to wait up to 60 seconds, then exit; the next systemd attempt may succeed once routing is ready. `TimeoutStopSec=45s` allows normal SIGTERM cleanup of microphone, speaker, GPIO, and dashboard workers. If that grace period is exceeded, systemd terminates the remaining service process group; inspect the cause before restarting. No custom infinite restart loop is used.

Activation starts each selected unit before enabling it for boot. If start fails, that unit's enablement is unchanged; if enabling fails, the installer attempts to stop a unit it just started, clear any newly created boot enablement, and preserve any previously active or enabled state. With `activate both`, an earlier service may remain active when the later one fails. Read the command's diagnostic, then check `systemctl --user status <unit>` and `journalctl --user -u <unit> -n 50`; inspect `is-active` and `is-enabled` for both units before retrying or rolling back. During rollback, a stop/disable failure leaves all unit files in place for a safe retry, though an earlier unit may already be stopped. A file-removal failure reports the remaining unit; rerunning rollback handles already removed files. An unrelated or changed unit with the same filename is never overwritten or removed by the installer.

To return to the tested manual launch method, stop and disable the services, then remove only the recognized Magic Mirror user units:

```bash
cd /home/bryce/magic_mirror_v2
.venv/bin/python scripts/manage_user_services.py rollback
systemctl --user is-active magic-mirror-dashboard.service magic-mirror-voice.service
./scripts/run_pi.sh
```

In another terminal, start the wake assistant manually with `MIRROR_LOG_LEVEL=INFO .venv/bin/python -m magic_mirror.assistant.wake_cli --start`. Confirm `/health`, one audio owner, and expected LED behavior. Stop this **foreground** CLI with `q` or Ctrl+C and Flask with Ctrl+C. Rollback leaves `.env`, `.venv`, `.private/`, PipeWire/WirePlumber, Pi-hole, Chromium, and GPIO configuration untouched. It does not disable user lingering or remove any unrelated user service.

Physical validation remains necessary: cloud tests cannot prove post-boot PipeWire routing, the selected physical microphone/speaker, GPIO release, Chromium readability, Realtime API behavior, or real Pi crash timing.

## Hardware acceptance checklist

- [ ] Manual processes are stopped before explicit activation; one dashboard and one voice owner start.
- [ ] `/health` is healthy, and dashboard data plus Chromium remain functional independently of voice.
- [ ] PipeWire resolves the verified USB source; audio uses MAX98357A and the existing settings.
- [ ] Wake, follow-up, spoken sleep, transcripts, tools, and LED states work without interactive stdin; `c`, `e`, and `q` are not tested in service mode.
- [ ] `systemctl --user stop`, `start`, and `restart` control one assistant owner without a second process.
- [ ] Clean `systemctl --user stop` stays stopped and releases microphone, speaker, and GPIO.
- [ ] A PID-specific SIGKILL restarts after the delay, with no duplicate owner and healthy recovery; no graceful cleanup is assumed during the forced exit.
- [ ] Start-rate-limit settings and failure diagnostics are verified without repeatedly crashing the physical assistant.
- [ ] Reboot with lingering restores both enabled services without a graphical login, if approved.
- [ ] Rollback restores the manual commands without changing `.env`, Pi-hole, or Chromium.
