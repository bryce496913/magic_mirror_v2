# Pass 5 Raspberry Pi audio validation

The physical USB PnP microphone and MAX98357A amplifier have been tested through PipeWire and ALSA. This pass tests what **PortAudio** sees. PortAudio names and indices can differ from PipeWire sink names and ALSA card names, and an ALSA device with a familiar card name may bypass PipeWire routing or mixing. Select an explicit matching PortAudio path and verify sound at the physical speaker. No audio is sent to a network API or written to disk by these commands.

## Install and inspect

From the Raspberry Pi 4B as the normal user:

```bash
cd magic_mirror_v2
git fetch origin
git switch --track origin/pass-5-audio
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
dpkg-query -W libportaudio2
test -f .env || cp .env.example .env
.venv/bin/python -m magic_mirror.audio.audio_test list
```

If the branch already exists locally, use `git switch pass-5-audio` and `git pull --ff-only`. The pinned Python packages have Python 3.11 ARM64 wheels. If `dpkg-query` reports `libportaudio2` missing, ask the Pi's package administrator to install the Debian `libportaudio2` package, then confirm that `.venv/bin/python -c 'import sounddevice'` succeeds. This PR does not install or change system packages.

Identify the USB microphone and speaker path from the PortAudio list. In the ignored local `.env`, set `MIRROR_AUDIO_INPUT_DEVICE` and `MIRROR_AUDIO_OUTPUT_DEVICE` to unique case-insensitive names or substrings from that list; use `HostAPI:name` if names repeat. The defaults prefer `USB PnP Sound Device` and `MAX98357A`, but the actual amplifier may appear under another logical name. Do not select HDMI just to make a test pass. Keep the preferred physical path explicit.

```bash
.venv/bin/python -m magic_mirror.audio.audio_test selected
```

This checks the configured capture/playback rates, channel counts, and PCM format against both selected PortAudio devices. If unsupported, adjust the settings in `.env` to values the actual device accepts and rerun `selected`. For replay, capture and playback rates and channel counts must match; there is no implicit resampling. The default is 48 kHz, mono, signed 16-bit PCM.

## Test each path and cleanup

Start at a comfortable hardware volume. The tone is a 440 Hz sine wave at 12% full scale for one second. The microphone sample lasts five seconds and remains only in memory.

```bash
.venv/bin/python -m magic_mirror.audio.audio_test tone --seconds 1
.venv/bin/python -m magic_mirror.audio.audio_test record --seconds 5
.venv/bin/python -m magic_mirror.audio.audio_test replay --seconds 5
.venv/bin/python -m magic_mirror.audio.audio_test tone --seconds 1
```

Confirm the tone is audible through the MAX98357A speaker, the microphone peak and RMS are reported in dBFS without printing speech, and replay is intelligible. If replay is too quiet, optionally set `MIRROR_AUDIO_PLAYBACK_GAIN=2.0` (about +6 dB) in local `.env` and repeat the replay at a safe hardware volume. The software output saturates at `MIRROR_AUDIO_MAX_PLAYBACK_LEVEL`; it does not alter PipeWire or microphone gain. Return gain to `1.0` after the comparison if preferred. Compression and loudness normalization are outside this pass.

Press Ctrl+C during `record` or `tone`, then repeat the same command to confirm that streams release. Disconnecting a device should produce a recoverable error; reconnect it and rerun the test. PortAudio stream open and close calls are delegated to its driver; `MIRROR_AUDIO_TIMEOUT` bounds frame reads, playback queue waits, and drain waits, not a driver call that hangs inside PortAudio. The capture queue holds at most eight callback buffers, playback holds at most eight, and each command is limited to 30 seconds. Abrupt power loss or a forced process kill cannot run Python cleanup.

Finally, start `./scripts/run_pi.sh` and verify `/health`, the dashboard, Pi-hole, and the RGB LED behavior from Pass 4. Ordinary Flask startup must not activate the microphone or speaker. Stop Flask with Ctrl+C. The audio CLI has no Flask, systemd, GPIO, or network dependency.
