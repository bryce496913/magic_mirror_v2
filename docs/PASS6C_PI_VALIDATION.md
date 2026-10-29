# Pass 6C local wake-word validation on Raspberry Pi

This pass adds a **local**, explicitly started keyword spotter. The default phrase is **“magic mirror”**; the configured phrase is encoded by the installed English model at startup. No custom model must be trained or purchased. The phrase's accuracy has not yet been measured on the physical Pi and must be validated before approval. The wake CLI is separate from Flask and does not add a web activation route, kiosk startup, or systemd service.

## Engine and model decision

We evaluated current packages and official project documentation:

| Engine | Pi/Python and license finding | Decision |
| --- | --- | --- |
| [Sherpa-ONNX keyword spotting](https://k2-fsa.github.io/sherpa/onnx/kws/index.html) | Maintained Apache-2.0 code, with Python 3.11 ARM64 wheels in the pinned 2.0.1 release. The [English GigaSpeech 3.3M model archive](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html#sherpa-onnx-kws-zipformer-gigaspeech-3-3m-2024-01-01-english) includes int8 files and its README declares Apache License 2.0. BPE tokens permit an English phrase without training. | Selected: local inference, no access key or idle network connection, one inference thread. |
| [Picovoice Porcupine](https://github.com/Picovoice/porcupine) | Supports Pi 4 and is maintained; its repository declares Apache-2.0. SDK use requires a Picovoice Console AccessKey, and a non-built-in “Magic Mirror” model requires a separate `.ppn` generated under Picovoice terms. | Not selected: cannot assume the desired custom phrase is available free of charge or without account terms. |
| [openWakeWord](https://github.com/dscripka/openWakeWord) | Apache-2.0 code and local inference on ARM64; its included pretrained models are CC BY-NC-SA 4.0. Python release 0.6.0 has a larger ONNX/TFLite dependency stack. | Not selected for this small first pass because the bundled models have noncommercial terms and do not supply our phrase directly. |

The selected model is trained on GigaSpeech English data and its included README declares Apache-2.0; review that file before redistribution or commercial use. This PR does **not** commit model weights. It uses the int8 encoder/decoder/joiner from the official release. The detector is optional and loads only when explicitly enabled and started. The model may still false-trigger or miss speech; adjust `MIRROR_WAKE_THRESHOLD` upward to reduce false triggers or downward to reduce misses, based on physical measurements. The default threshold is 0.35, with one CPU thread. No idle microphone samples leave the Pi or are saved.

## Install and configure

Run on the 64-bit Bookworm Pi as the normal application user:

```bash
cd magic_mirror_v2
git fetch origin
git switch --track origin/pass-6c-local-wake
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-wake.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
test -f .env || cp .env.example .env
chmod 600 .env
mkdir -p .private/models
curl --fail --location --output /tmp/mirror-wake-model.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
printf '%s\n' 'f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a  /tmp/mirror-wake-model.tar.bz2' | sha256sum --check
tar -xjf /tmp/mirror-wake-model.tar.bz2 -C .private/models
rm /tmp/mirror-wake-model.tar.bz2
realpath .private/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01
```

If the branch exists locally, use `git switch pass-6c-local-wake` and `git pull --ff-only`. The archive hash above was verified during development; stop if it differs. The extracted model files stay under ignored `.private/`. Read the archive's `README.md` for its stated license. The optional wake dependencies are not needed for the ordinary dashboard or manually activated voice CLI.

In the ignored local `.env`, keep the Pass 6B microphone, physical PipeWire source, 48 kHz mono int16, MAX98357A and tested gain 1.4 settings. Set `MIRROR_WAKE_ENABLED=true`, `MIRROR_WAKE_PHRASE=magic mirror`, and set `MIRROR_WAKE_MODEL_DIR` to the **absolute directory printed by `realpath`**. Keep `OPENAI_API_KEY` only in `.env`; never pass it on a command line. For a different English phrase, edit only `MIRROR_WAKE_PHRASE` and restart the CLI. The model must still recognize it reliably on this hardware; changing words does not guarantee accuracy.

Check the physical input and offline CLI before activation:

```bash
wpctl inspect @DEFAULT_AUDIO_SOURCE@
.venv/bin/python -m magic_mirror.audio.audio_test selected
.venv/bin/python -m magic_mirror.assistant.wake_cli
```

The PipeWire `node.name` must match the configured physical USB microphone. The offline CLI must report that it did not start.

## Hardware acceptance

Start explicitly in a foreground terminal:

```bash
.venv/bin/python -m magic_mirror.assistant.wake_cli --start
```

1. In a quiet room at normal speaking distance, say “magic mirror.” Confirm `wake_detected`, then wait for the `listening` cue before asking a short question. Hear the reply. The wake phrase itself is discarded; it is never sent as a question.
2. Stay quiet for at least eight seconds after a wake. Confirm a false activation returns to idle and the count increases, without a lingering OpenAI connection.
3. Say “go to sleep” or press `e` then Enter during a conversation. Confirm it returns to idle without stopping the wake CLI. Run 10–20 quiet-room wake/conversation/sleep cycles. Note misses, false triggers, device-busy errors, and time from detection to the listening cue. A five-second cooldown suppresses repeated wake events after each session.
4. Repeat with normal background noise and the speaker at the tested local gain. Note changes in misses and false activations. Test ordinary non-wake speech; it should leave the controller idle.
5. Leave the detector idle for at least two minutes. The CLI prints idle process CPU%, RSS MiB, and established TCP connection count every 60 seconds. Record the Pi measurements rather than assuming cloud or desktop timings. CPU% is per logical core and may exceed 100 for multithreaded code; this spotter requests one inference thread. Established TCP should be **0** while idle. `-1` means the metric was unavailable and needs a separate check.
6. Press `q` then Enter or Ctrl+C to stop the whole assistant. Confirm `stopped` and that the microphone is reusable with `.venv/bin/python -m magic_mirror.audio.audio_test record --seconds 3`. No reboot is needed.

The controller keeps one local microphone stream in idle. On wake, it drains local samples while the OpenAI session is configured, then stops that stream and starts Pass 6B capture using the same AudioManager. It intentionally discards the wake phrase and any audio spoken before the listening cue; there is no stored pre-roll or idle upload. After conversation end, timeout, false activation, or a recoverable error it closes the OpenAI session and both streams, observes cooldown, and reopens only local detection. Device failures retry at most three times with short backoff before stopping. Stop the CLI if the measured idle CPU or memory is too high for this Pi; physical validation determines whether the model is acceptable.

A development smoke test on an **x86 cloud host**, feeding three seconds of synthetic silence at real-time spacing after model warm-up, measured 13.0% of one CPU core, 93.1 MiB process RSS, and zero established TCP connections. These are not Raspberry Pi measurements or accuracy claims. The Pi tester should record the CLI's own 60-second idle samples and accept or reject this model based on that hardware.
