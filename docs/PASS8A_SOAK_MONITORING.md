# Pass 8A: 72-hour Raspberry Pi reliability soak

The monitor reads local service metadata, process counters, system resources, and the dashboard's loopback `/health` endpoint every five minutes. It does not read `.env`, call OpenAI, read transcripts, open audio devices, access GPIO, or control any production service. It uses its own optional **user** systemd unit. The unit is never enabled by repository installation.

## Prepare and install (no activation)

As `bryce` on the Pi, with the existing checkout and `.venv`:

```bash
cd /home/bryce/magic_mirror_v2
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q tests/test_soak.py
systemd-analyze verify --user systemd/user/magic-mirror-soak.service
install -d -m 0700 "$HOME/.config/systemd/user"
install -m 0644 systemd/user/magic-mirror-soak.service "$HOME/.config/systemd/user/magic-mirror-soak.service"
systemctl --user daemon-reload
systemctl --user is-enabled magic-mirror-soak.service
```

The last command should report `disabled` (and may exit nonzero). Review the installed unit before activation. It assumes the existing `/home/bryce/magic_mirror_v2/.venv` and user lingering. It does not change the dashboard or voice units.

## Activate explicitly

```bash
systemctl --user enable --now magic-mirror-soak.service
systemctl --user status magic-mirror-soak.service
cd /home/bryce/magic_mirror_v2
.venv/bin/python -m magic_mirror.soak status
journalctl --user -u magic-mirror-soak.service -n 30 --no-pager
```

The service samples immediately, sleeps between samples, and ends at the original 72-hour deadline. After reboot, it resumes from its private state file; downtime appears as missing samples. `Restart=on-failure` retries monitor failures, with a 60-second delay and a systemd rate limit. A normal stop or completed duration is not automatically restarted. The monitor never repairs a failed application service.

The service command uses `--hours 72 --interval-seconds 300`. To choose another duration (1–720 hours) or interval (30–3600 seconds), edit **only** the monitor unit before activation:

```bash
systemctl --user edit magic-mirror-soak.service
```

For example, enter:

```ini
[Service]
ExecStart=
ExecStart=/home/bryce/magic_mirror_v2/.venv/bin/python -m magic_mirror.soak run --hours 72 --interval-seconds 300 --max-log-mib 4
```

Then run `systemctl --user daemon-reload`. The log limit is 1–32 MiB per JSONL file. Do not change duration or interval after the first sample: the private state rejects incompatible restarts so results are not silently mixed. Archive and remove `.private/soak` before a separate new run.

## Observe and report

```bash
cd /home/bryce/magic_mirror_v2
systemctl --user status magic-mirror-soak.service
.venv/bin/python -m magic_mirror.soak status
.venv/bin/python -m magic_mirror.soak report
journalctl --user -u magic-mirror-soak.service --since today --no-pager
```

Samples are JSONL in `.private/soak/samples.jsonl`, rotated once to `samples.jsonl.1`. Each file is at most 4 MiB by default, and the directory/files are mode 0700/0600. The state and owner lock are in the same ignored private directory. The report reads this data only. Do not publish raw diagnostic files without reviewing them; even numeric process metrics can reveal usage patterns.

An illustrative sample, **synthetic values**:

```json
{"timestamp":"2026-01-01T00:00:00+00:00","boot_time":1000.0,"services":{"dashboard":{"state":"active","pid":1234,"restarts":0,"rss_bytes":45000000,"cpu_seconds":21.0,"cpu_percent":1.2,"uptime_seconds":3600.0},"voice":{"state":"active","pid":1235,"restarts":0,"rss_bytes":95000000,"cpu_seconds":32.0,"cpu_percent":2.4,"uptime_seconds":3600.0}},"dependencies":{"pihole":"active","pipewire":"active","wireplumber":"active"},"health":{"healthy":true,"response_ms":11.0,"error":null},"system":{"load_1m":0.4,"load_5m":0.3,"available_bytes":2000000000,"swap_used_bytes":0}}
```

An illustrative report excerpt, also **synthetic**:

```text
Soak duration: 72.0 / 72.0 hours
Samples: 864 collected, 864 expected, 0 missing, 0 malformed
dashboard: 0 automatic restarts, 0 PID changes, 0 inactive, 0 unknown; CPU typical/max 1.2/4.0%, 0 unavailable intervals; RSS baseline/peak/trend 45000000/48000000/1000000 bytes, 0 unknown; potential leak=False
voice: 0 automatic restarts, 0 PID changes, 0 inactive, 0 unknown; CPU typical/max 2.4/9.0%, 0 unavailable intervals; RSS baseline/peak/trend 95000000/99000000/2000000 bytes, 0 unknown; potential leak=False
Dashboard health: 0 failures, 0 unknown; response typical/max 11.0/70.0 ms, 0 unavailable timings
pihole: 0 outages, 0 unknown
pipewire: 0 outages, 0 unknown
wireplumber: 0 outages, 0 unknown
System one-minute load typical/max: 0.4/1.2
Available memory typical/max: 2000000000.0/2100000000.0 bytes; swap used typical/max: 0.0/0.0 bytes; 0 unavailable resource measurements
Assessment: PASS (unexplained PID changes and potential leaks need review)
```

`PASS` requires the complete window with no missing/malformed samples, outages, health failures, restart evidence, or resource uncertainty. `FAIL` means a sampled service/dependency outage, an automatic restart counter increase, or a dashboard health failure was observed. `INCONCLUSIVE` covers incomplete duration, missing/unknown data, unexplained PID changes, or a possible memory leak. The leak flag compares early and late RSS quartile medians; it is a review signal, not proof of a leak. A five-minute sampler can miss short incidents. A reboot resets systemd restart counters, so PID changes are compared only within one boot. CPU percentages are process CPU-time deltas; the first sample after a new PID has no CPU percent. If the chosen log limit causes rotation to discard samples, the report counts the resulting missing slots and remains inconclusive.

## Hardware acceptance and removal

During the soak, use voice normally. Check the dashboard, voice, Pi-hole, PipeWire and WirePlumber remain active; compare journal entries with any report failures. Confirm the monitor did not open a microphone/speaker or change LED behavior. After 72 hours, generate the report and inspect the private JSONL for only the fixed fields shown above, with no credentials or conversation content. Investigate every missing or malformed sample, restart, outage, health failure, and potential memory rise before acceptance.

To stop and remove **only** this monitor:

```bash
systemctl --user disable --now magic-mirror-soak.service
rm "$HOME/.config/systemd/user/magic-mirror-soak.service"
systemctl --user daemon-reload
```

The private report remains under `.private/soak` for review. Archive it securely before deleting it for a future run. Removing the monitor does not affect the dashboard, voice, Pi-hole, PipeWire, WirePlumber, audio, or GPIO.
