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

The hardware tester found MAX98357A as an ALSA output with two available channels, USB PnP Sound Device as a one-channel ALSA input, and separate PipeWire and Pulse compatibility interfaces. Their PortAudio numeric indices may change, so configure names rather than indices. Direct ALSA MAX98357A playback produced a clear 440 Hz tone. Direct ALSA USB capture opened but reported three input overflows in about 24 callbacks over three seconds. The tested PipeWire input completed a five-second application recording without warnings. Both working routes support 48 kHz mono int16 PCM.

For this Pi, set the following in the ignored local `.env` after confirming the device list. `wpctl inspect` is read-only; compare its `node.name` with the expected source before recording:

```bash
wpctl inspect @DEFAULT_AUDIO_SOURCE@
```

```dotenv
MIRROR_AUDIO_INPUT_DEVICE=pipewire
MIRROR_AUDIO_EXPECTED_INPUT_SOURCE=alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono
MIRROR_AUDIO_OUTPUT_DEVICE=MAX98357A
MIRROR_AUDIO_PLAYBACK_GAIN=1.4
```

The `pipewire` PortAudio input follows the current PipeWire default source; its name alone does **not** prove that the USB microphone is still selected. The application compares the current default `node.name` with `MIRROR_AUDIO_EXPECTED_INPUT_SOURCE` before each capture start and fails clearly if `wpctl` is unavailable, the expected source is blank, or the names differ. The selected-device CLI prints the verified source. This check is a snapshot at stream start; a later routing change during a recording is not continuously monitored. Do not change global PipeWire defaults automatically, and do not select HDMI or another microphone merely to make a test pass.

```bash
.venv/bin/python -m magic_mirror.audio.audio_test selected
```

This checks the configured capture/playback rates, channel counts, PCM format, and expected physical PipeWire source. If unsupported, adjust the settings in `.env` to values the actual device accepts and rerun `selected`. For replay, capture and playback rates and channel counts must match; there is no implicit resampling. The default is 48 kHz, mono, signed 16-bit PCM.

## Test each path and cleanup

Start at a comfortable hardware volume. The tone is a 440 Hz sine wave at 12% full scale for one second. The microphone sample lasts five seconds and remains only in memory.

```bash
.venv/bin/python -m magic_mirror.audio.audio_test tone --seconds 1
.venv/bin/python -m magic_mirror.audio.audio_test record --seconds 5
.venv/bin/python -m magic_mirror.audio.audio_test replay --seconds 5
.venv/bin/python -m magic_mirror.audio.audio_test tone --seconds 1
```

Confirm the tone is audible through the MAX98357A speaker, the microphone peak and RMS are reported in dBFS without printing speech, and replay is intelligible. The tested Pi sounded clear but quiet at gain `1.0`, clear and louder at `1.4`, and distorted/crackling at `2.0`; keep `1.4` as a local hardware-tested choice, not a universal default. The CLI reports the input, requested, and output digital peaks plus the count of limited samples. A −10 dBFS recording peak would reach roughly −4 dBFS at 2× gain, so simple digital clipping is not established by the reported peak alone. If the limited-sample count is zero but the speaker crackles, investigate downstream routing or amplifier behavior separately. The software output still saturates at `MIRROR_AUDIO_MAX_PLAYBACK_LEVEL`; it does not alter PipeWire or microphone gain. Compression and loudness normalization remain outside this pass.

The CLI also prints capture callbacks, PortAudio input-overflow warnings, application queue drops, and other stream-status errors. One isolated input overflow permits capture to complete with a warning that audio may contain a gap. Capture fails if at least three overflows exceed 5% of callbacks, or ten occur at any rate. Application queue drops and other status errors fail immediately. On the direct ALSA result of three overflows in 24 callbacks, the new policy reports excessive dropouts instead of calling it a device disconnection. PipeWire's 30 clean callbacks in the three-second diagnostic and successful five-second application recording are the preferred starting point for this Pi. If the physical source changes, update the expected node only after verifying the intended microphone.

Press Ctrl+C during `record` or `tone`, then repeat the same command to confirm that streams release. Disconnecting a device should produce a recoverable error; reconnect it and rerun the test. PortAudio stream open and close calls are delegated to its driver; `MIRROR_AUDIO_TIMEOUT` bounds frame reads, playback queue waits, and drain waits, not a driver call that hangs inside PortAudio. The capture queue holds at most eight callback buffers, playback holds at most eight, and each command is limited to 30 seconds. Abrupt power loss or a forced process kill cannot run Python cleanup.

Finally, start `./scripts/run_pi.sh` and verify `/health`, the dashboard, Pi-hole, and the RGB LED behavior from Pass 4. Ordinary Flask startup must not activate the microphone or speaker. Stop Flask with Ctrl+C. The audio CLI has no Flask, systemd, GPIO, or network dependency.
