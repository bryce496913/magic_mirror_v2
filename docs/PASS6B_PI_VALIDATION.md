# Pass 6B Raspberry Pi live voice validation

This is an explicitly started, local voice test. Flask, Chromium, the dashboard assistant placeholder, GPIO and Pi-hole are unchanged. No microphone stream or OpenAI connection opens until the `--start` command. The hardware tester should keep the session short; the default Realtime session limit is 15 minutes, and the CLI ends after 60 seconds without detected speech.

## Install and check configuration

Run on the Raspberry Pi 4B as the normal application user from the existing clone:

```bash
cd magic_mirror_v2
git fetch origin
git switch --track origin/pass-6b-live-voice
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
test -f .env || cp .env.example .env
chmod 600 .env
.venv/bin/python -m magic_mirror.assistant.realtime_test
.venv/bin/python -m magic_mirror.assistant.voice_cli
```

If the branch exists locally, use `git switch pass-6b-live-voice` and `git pull --ff-only`. Keep the OpenAI API key only in the ignored local `.env`; edit it with a local editor and set `OPENAI_API_KEY` there. Never put the key in a command argument, screenshot, issue, or PR comment. The two CLI commands above are offline and must not open devices or spend API tokens.

Use the following tested hardware values in the ignored `.env`:

```dotenv
MIRROR_AUDIO_INPUT_DEVICE=pipewire
MIRROR_AUDIO_EXPECTED_INPUT_SOURCE=alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono
MIRROR_AUDIO_OUTPUT_DEVICE=MAX98357A
MIRROR_AUDIO_CAPTURE_RATE=48000
MIRROR_AUDIO_PLAYBACK_RATE=48000
MIRROR_AUDIO_CAPTURE_CHANNELS=1
MIRROR_AUDIO_PLAYBACK_CHANNELS=1
MIRROR_AUDIO_FRAME_FORMAT=int16
MIRROR_AUDIO_PLAYBACK_GAIN=1.4
```

Before a live session, verify the physical PipeWire source and PortAudio route. These commands do not contact OpenAI:

```bash
wpctl inspect @DEFAULT_AUDIO_SOURCE@
.venv/bin/python -m magic_mirror.audio.audio_test selected
```

The `node.name` from `wpctl` must exactly match `MIRROR_AUDIO_EXPECTED_INPUT_SOURCE`. If it differs, correct the Pi's input route before starting voice. The audio CLI should report the expected PipeWire input, MAX98357A output, and supported 48 kHz mono int16 format. Numeric PortAudio device indices are not stable and are intentionally absent from the configuration.

## Live conversation checklist

Start only when ready to use the API:

```bash
.venv/bin/python -m magic_mirror.assistant.voice_cli --start
```

1. Ask a simple question and confirm an audible answer from the MAX98357A speaker. Record the CLI's measured speech-end to first audio **received** time; it is not a promise about audible latency.
2. Ask a follow-up question and confirm the response uses the same session. The microphone remains active only while this command runs.
3. While an answer is speaking, begin another question. Confirm the old audio stops and the new answer is relevant. Press `c` then Enter to cancel only an answer if needed; capture continues.
4. Press `q` then Enter to end the session, or Ctrl+C for immediate stop. Confirm the CLI reports that streams were released.
5. Restart the same command without rebooting, ask one short question, and end again. Confirm there is no competing-stream error.
6. Confirm the hardware is reusable with `.venv/bin/python -m magic_mirror.audio.audio_test tone --seconds 1` and `.venv/bin/python -m magic_mirror.audio.audio_test record --seconds 3`. These commands never contact OpenAI and leave no audio files.

If speaker audio is distorted, lower `MIRROR_AUDIO_PLAYBACK_GAIN` or `MIRROR_AUDIO_MAX_PLAYBACK_LEVEL` in the local `.env` and repeat the test. A streaming peak limiter caps voice output to at most 85% of digital full scale after configured gain; the existing AudioPlayback path also saturates PCM instead of wrapping samples. The previously tested gain of `1.4` is retained as the Pi's local setting; this PR does not change its default. Test in a quiet room first: the microphone and speaker path has no acoustic echo cancellation, so speaker feedback may trigger server VAD on this hardware.

## Turn and recovery behavior

The current [OpenAI VAD guide](https://developers.openai.com/api/docs/guides/realtime-vad) documents `server_vad` with a 0.5 threshold, 300 ms speech prefix, 600 ms end-of-speech silence, automatic response creation, and interruption enabled. This CLI sends one 100 ms PCM chunk at a time and lets server VAD create responses; it does not send its own `response.create`. A local 60-second no-speech limit ends a quiet session without asking the model to respond to silence. The existing Realtime maximum session and per-response limits also apply.

The OpenAI [conversation guide](https://developers.openai.com/api/docs/guides/realtime-conversations) specifies 24 kHz mono PCM and requires WebSocket clients to truncate unplayed audio after an interruption. This implementation filters and decimates 48 kHz capture to 24 kHz, interpolates 24 kHz output to the configured 48 kHz speaker rate, and keeps at most eight 100 ms decoded output chunks before the AudioManager's bounded queue. When VAD detects a new utterance, it aborts local playback, drops old queued chunks, ignores late audio from the old response, and sends `conversation.item.truncate` using the speaker queue's played-frame count. The count approximates frames handed to the output callback, not the exact acoustic end time. The server automatically cancels the interrupted response with VAD; manual `c` also sends `response.cancel` when a response is still active.

User input transcription uses OpenAI's supported `gpt-4o-mini-transcribe` session setting, which runs asynchronously and may add transcription cost. Assistant audio transcript events come from the voice response. Final lines replace partial text by item ID; transcripts stay in process memory and terminal output, never in Flask or a file. Transcripts can be absent or arrive later than audio. A response may contain only one audio item in this first voice pass; unexpected multiple audio items fail safely rather than mixing them. Authentication, rate-limit, timeout, device, and network failures close both audio streams and the socket. There is no automatic post-disconnect reconnect loop; restart the CLI after addressing the failure. Keep tests short and verify current [model pricing](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini) before extended use.
