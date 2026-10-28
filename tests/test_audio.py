"""Audio tests drive fake PortAudio streams; no microphone or speaker is needed."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from magic_mirror import create_app
from magic_mirror.audio import AudioError, AudioManager
from magic_mirror.audio.audio_test import run
from magic_mirror.audio.core import AudioPlayback
from magic_mirror.audio.devices import DeviceError
from magic_mirror.audio.pcm import PCMError, levels, playback_gain
from magic_mirror.config import ConfigurationError, Settings


class FakeStream:
    def __init__(self, *, callback, channels, dtype, blocksize, **_kwargs):
        self.callback = callback
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        self.started = False
        self.aborted = False
        self.closed = False
        self.fail_start = False

    def start(self):
        if self.fail_start:
            raise OSError("disconnected")
        self.started = True

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True

    def input(self, frames, status=None):
        data = np.asarray(frames, dtype=self.dtype).reshape(-1, self.channels)
        self.callback(data, len(data), None, status)

    def output(self, count=None, status=None):
        data = np.empty((count or self.blocksize, self.channels), dtype=self.dtype)
        self.callback(data, len(data), None, status)
        return data


class FakeBackend:
    def __init__(self):
        self.device_rows = [
            {"name": "USB PnP Sound Device", "hostapi": 0, "max_input_channels": 1,
             "max_output_channels": 0, "default_samplerate": 48000},
            {"name": "HDMI", "hostapi": 0, "max_input_channels": 0,
             "max_output_channels": 2, "default_samplerate": 48000},
            {"name": "MAX98357A", "hostapi": 0, "max_input_channels": 0,
             "max_output_channels": 1, "default_samplerate": 48000},
        ]
        self.inputs = []
        self.outputs = []
        self.fail_start = False

    def query_hostapis(self):
        return [{"name": "ALSA"}]

    def query_devices(self):
        return self.device_rows

    def check_input_settings(self, *, device, channels, samplerate, dtype):
        if device != 0 or channels != 1 or samplerate != 48000 or dtype not in {"int16", "float32"}:
            raise ValueError("unsupported")

    def check_output_settings(self, *, device, channels, samplerate, dtype):
        if device != 2 or channels != 1 or samplerate != 48000 or dtype not in {"int16", "float32"}:
            raise ValueError("unsupported")

    def InputStream(self, **kwargs):
        stream = FakeStream(**kwargs)
        stream.fail_start = self.fail_start
        self.inputs.append(stream)
        return stream

    def OutputStream(self, **kwargs):
        stream = FakeStream(**kwargs)
        stream.fail_start = self.fail_start
        self.outputs.append(stream)
        return stream


def manager(backend=None, **kwargs):
    return AudioManager(Settings(audio_enabled=True, audio_buffer_duration=0.02,
                                 audio_timeout=0.1, **kwargs), backend or FakeBackend())


def test_device_selection_rejects_hdmi_and_reports_properties():
    audio = manager()
    assert audio.selected("input").name == "USB PnP Sound Device"
    assert audio.selected("output").name == "MAX98357A"
    assert "ALSA" in audio.selected("output").label()
    assert len(audio.devices()) == 3
    with pytest.raises(ValueError):
        audio.selected("other")


def test_missing_ambiguous_and_incapable_devices():
    backend = FakeBackend()
    with pytest.raises(DeviceError, match="not found"):
        manager(backend, audio_output_device="missing").selected("output")
    backend.device_rows.append({**backend.device_rows[2], "name": "MAX98357A secondary"})
    assert manager(backend).selected("output").index == 2  # exact name wins
    with pytest.raises(DeviceError, match="ambiguous"):
        manager(backend, audio_output_device="MAX98357").selected("output")
    backend.device_rows[2]["max_output_channels"] = 0
    with pytest.raises(DeviceError, match="lacks"):
        manager(backend).selected("output")


def test_hostapi_disambiguation_and_unsupported_rate():
    backend = FakeBackend()
    backend.device_rows.append({**backend.device_rows[2], "hostapi": 1})
    backend.query_hostapis = lambda: [{"name": "ALSA"}, {"name": "PulseAudio"}]
    assert manager(backend, audio_output_device="ALSA:MAX98357A").selected("output").hostapi == "ALSA"
    with pytest.raises(DeviceError, match="does not support"):
        manager(backend, audio_capture_rate=16000).selected("input")
    with pytest.raises(DeviceError, match="lacks"):
        manager(backend, audio_capture_channels=2).selected("input")


def test_capture_frames_record_cancel_and_repeated_start_stop():
    backend = FakeBackend()
    audio = manager(backend)
    assert audio.capture.start()
    assert audio.capture.start() is False
    stream = backend.inputs[-1]
    stream.input(np.arange(12, dtype=np.int16))
    assert audio.capture.read_frames(5).ravel().tolist() == list(range(5))
    assert audio.capture.read_frames(7).ravel().tolist() == list(range(5, 12))
    stream.input(np.ones((480, 1), dtype=np.int16))
    assert audio.capture.record(0.01).shape == (480, 1)
    assert audio.capture.stop()
    assert stream.aborted and stream.closed
    assert audio.capture.stop() is False
    assert audio.capture.start()
    audio.close()
    assert backend.inputs[-1].closed


def test_capture_cancellation_disconnect_and_buffer_overflow():
    backend = FakeBackend()
    audio = manager(backend)
    audio.capture.start()
    errors = []
    waiter = threading.Thread(target=lambda: _capture_error(audio, errors))
    waiter.start()
    time.sleep(0.01)
    audio.capture.stop()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert errors and "cancelled" in errors[0]
    audio.capture.start()
    with pytest.raises(AudioError, match="timed out or disconnected"):
        audio.capture.read_frames(1, timeout=0.02)
    for _ in range(9):
        backend.inputs[-1].input(np.zeros((2, 1), dtype=np.int16))
    with pytest.raises(AudioError, match="buffer overrun"):
        audio.capture.read_frames(1)
    audio.close()


def test_capture_reports_device_status():
    backend = FakeBackend()
    audio = manager(backend)
    audio.capture.start()
    backend.inputs[-1].input(np.zeros((2, 1), dtype=np.int16), status="device lost")
    with pytest.raises(AudioError, match="stream reported"):
        audio.capture.read_frames(1)
    audio.close()


def _capture_error(audio, errors):
    try:
        audio.capture.read_frames(1, timeout=1)
    except AudioError as exc:
        errors.append(str(exc))


def test_competing_capture_and_playback_owners_and_failed_open_recovery():
    backend = FakeBackend()
    first, second = manager(backend), manager(backend)
    first.capture.start()
    first.playback.start()
    with pytest.raises(AudioError, match="already owned"):
        second.capture.start()
    with pytest.raises(AudioError, match="already owned"):
        second.playback.start()
    first.close()
    backend.fail_start = True
    with pytest.raises(AudioError, match="Unable to open"):
        second.capture.start()
    assert backend.inputs[-1].closed
    backend.fail_start = False
    second.capture.start()
    second.playback.start()
    second.close()


def test_playback_queue_drain_cancel_and_silence():
    backend = FakeBackend()
    audio = manager(backend)
    audio.playback.start()
    assert audio.playback.start() is False
    stream = backend.outputs[-1]
    silence = stream.output(4)
    assert not silence.any()
    frames = np.array([[1000], [-1000], [0]], dtype=np.int16)
    assert audio.playback.write(frames) == 3
    assert stream.output(3).ravel().tolist() == [1000, -1000, 0]
    audio.playback.drain()
    assert audio.playback.stop()
    assert stream.aborted and stream.closed
    assert audio.playback.stop() is False
    audio.playback.start()
    audio.close()


def test_playback_buffer_timeout_and_cancel_waiter():
    backend = FakeBackend()
    audio = manager(backend)
    audio.playback.start()
    big = np.zeros((backend.outputs[-1].blocksize * 10, 1), dtype=np.int16)
    with pytest.raises(AudioError, match="timed out"):
        audio.playback.write(big, timeout=0.02)
    assert backend.outputs[-1].closed
    audio.playback.start()
    errors = []
    def write_waiter():
        try:
            audio.playback.write(big, timeout=1)
        except AudioError as exc:
            errors.append(str(exc))
    waiter = threading.Thread(target=write_waiter)
    waiter.start()
    time.sleep(0.01)
    audio.playback.stop()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert errors and "cancelled" in errors[0]
    audio.close()


def test_overlapping_writes_are_rejected():
    backend = FakeBackend()
    audio = manager(backend)
    audio.playback.start()
    big = np.zeros((backend.outputs[-1].blocksize * 10, 1), dtype=np.int16)
    errors = []
    def writer():
        try:
            audio.playback.write(big, timeout=1)
        except AudioError as exc:
            errors.append(str(exc))
    first = threading.Thread(target=writer)
    first.start()
    for _ in range(100):
        if audio.playback._queued_frames == backend.outputs[-1].blocksize * 8:
            break
        time.sleep(0.001)
    with pytest.raises(AudioError, match="Another speaker write"):
        audio.playback.write(np.zeros((1, 1), dtype=np.int16))
    audio.playback.stop()
    first.join(timeout=1)
    assert not first.is_alive() and errors


def test_playback_stream_error_and_drain_timeout():
    backend = FakeBackend()
    audio = manager(backend)
    audio.playback.start()
    audio.playback.write(np.ones((100, 1), dtype=np.int16))
    with pytest.raises(AudioError, match="drain timed out"):
        audio.playback.drain(timeout=0.01)
    backend.outputs[-1].output(100, status="device lost")
    with pytest.raises(AudioError, match="stream reported"):
        audio.playback.write(np.ones((1, 1), dtype=np.int16))
    assert backend.outputs[-1].closed


@pytest.mark.parametrize("gain,level,source,expected", [
    (1, 1, [1000, -1000, 0], [1000, -1000, 0]),
    (2, 1, [20000, -20000], [32767, -32768]),
    (4, 0.5, [30000, -30000], [16383, -16384]),
    (2, 1, [0, 0], [0, 0]),
])
def test_int16_playback_gain_and_clipping(gain, level, source, expected):
    data = np.asarray(source, dtype=np.int16)
    assert playback_gain(data, "int16", 1, gain, level).ravel().tolist() == expected


def test_float_pcm_levels_and_invalid_input():
    assert playback_gain(np.array([0.3, -0.8], dtype=np.float32), "float32", 1, 2, 0.9).ravel().tolist() == pytest.approx([0.6, -0.9])
    assert levels(np.zeros(3, dtype=np.int16), "int16", 1) == {"peak_dbfs": float("-inf"), "rms_dbfs": float("-inf")}
    assert levels(np.array([16384, 0], dtype=np.int16), "int16", 1)["peak_dbfs"] == pytest.approx(-6.02, abs=0.01)
    for value in (b"\x01", np.array([np.nan], dtype=np.float32),
                  np.array([1.5], dtype=np.float32), np.array([1.0], dtype=np.float64)):
        with pytest.raises(PCMError):
            playback_gain(value, "float32" if not isinstance(value, bytes) else "int16", 1)


def test_cli_lists_and_selects_without_opening_streams(capsys):
    backend = FakeBackend()
    settings = Settings()
    run(settings, "list", backend=backend)
    run(settings, "selected", backend=backend)
    output = capsys.readouterr().out
    assert "USB PnP Sound Device" in output and "MAX98357A" in output
    assert not backend.inputs and not backend.outputs


def test_cli_record_replay_tone_and_interruption(monkeypatch, capsys):
    backend = FakeBackend()
    original_input = backend.InputStream
    def input_stream(**kwargs):
        stream = original_input(**kwargs)
        original_start = stream.start
        def start():
            original_start()
            stream.input(np.full((4800, 1), 1000, dtype=np.int16))
        stream.start = start
        return stream
    backend.InputStream = input_stream

    def drain(self, timeout=None):
        while self._queued_frames:
            self._stream.output()
    monkeypatch.setattr(AudioPlayback, "drain", drain)
    settings = Settings(audio_buffer_duration=0.02)
    run(settings, "record", seconds=0.1, backend=backend)
    run(settings, "replay", seconds=0.1, backend=backend, sleep=lambda _seconds: None)
    run(settings, "tone", seconds=0.1, backend=backend, sleep=lambda _seconds: None)
    def interrupt(_seconds):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(settings, "tone", seconds=0.1, backend=backend, sleep=interrupt)
    assert all(stream.closed for stream in backend.inputs + backend.outputs)
    output = capsys.readouterr().out
    assert "Microphone peak:" in output and "RMS:" in output
    assert "MAX98357A" in output and "USB PnP Sound Device" in output


def test_disabled_audio_and_dashboard_independence():
    backend = FakeBackend()
    audio = AudioManager(Settings(), backend)
    with pytest.raises(AudioError, match="disabled"):
        audio.capture.start()
    with pytest.raises(AudioError, match="disabled"):
        audio.playback.start()
    app = create_app(Settings(), audio=audio)
    assert app.test_client().get("/").status_code == 200
    assert app.test_client().get("/health").json == {"status": "healthy"}
    assert app.extensions["magic_mirror_audio"] is audio
    assert not backend.inputs and not backend.outputs


def test_flask_keyboard_interrupt_closes_audio_without_opening_it(monkeypatch):
    from magic_mirror import __main__ as entry
    events = []
    settings = Settings(audio_enabled=True, led_enabled=False)

    class FakeAudio:
        def __init__(self, _settings):
            events.append("audio-created")
        def close(self):
            events.append("audio-close")

    class FakeData:
        def start(self):
            events.append("data-start")
        def stop(self):
            events.append("data-stop")
            return True

    class FakeApp:
        def run(self, **_kwargs):
            events.append("flask-run")
            raise KeyboardInterrupt

    monkeypatch.setattr(entry, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entry, "Settings", SimpleNamespace(from_env=lambda: settings))
    monkeypatch.setattr(entry, "configure_logging", lambda _level: None)
    monkeypatch.setattr(entry, "AudioManager", FakeAudio)
    monkeypatch.setattr(entry, "DashboardData", lambda _settings: FakeData())
    monkeypatch.setattr(entry, "create_app", lambda _settings, _data, led, audio: FakeApp())
    monkeypatch.setattr(entry.signal, "signal", lambda *_args: None)
    assert entry.main() == 0
    assert events == ["audio-created", "data-start", "flask-run", "data-stop", "audio-close"]


@pytest.mark.parametrize("values", [
    {"MIRROR_AUDIO_ENABLED": "yes"},
    {"MIRROR_AUDIO_INPUT_DEVICE": ""},
    {"MIRROR_AUDIO_OUTPUT_DEVICE": "\n"},
    {"MIRROR_AUDIO_CAPTURE_RATE": "7999"},
    {"MIRROR_AUDIO_PLAYBACK_RATE": "96001"},
    {"MIRROR_AUDIO_CAPTURE_CHANNELS": "0"},
    {"MIRROR_AUDIO_PLAYBACK_CHANNELS": "3"},
    {"MIRROR_AUDIO_BUFFER_DURATION": "nan"},
    {"MIRROR_AUDIO_FRAME_FORMAT": "mp3"},
    {"MIRROR_AUDIO_PLAYBACK_GAIN": "5"},
    {"MIRROR_AUDIO_MAX_PLAYBACK_LEVEL": "0"},
    {"MIRROR_AUDIO_TIMEOUT": "0"},
])
def test_invalid_audio_config(values):
    with pytest.raises(ConfigurationError):
        Settings.from_env(values)


def test_audio_config_defaults_and_overrides():
    assert Settings.from_env({}) == Settings()
    settings = Settings.from_env({"MIRROR_AUDIO_ENABLED": "true", "MIRROR_AUDIO_INPUT_DEVICE": "ALSA:USB PnP",
                                  "MIRROR_AUDIO_OUTPUT_DEVICE": "ALSA:MAX98357A", "MIRROR_AUDIO_CAPTURE_RATE": "44100",
                                  "MIRROR_AUDIO_PLAYBACK_RATE": "44100", "MIRROR_AUDIO_FRAME_FORMAT": "float32",
                                  "MIRROR_AUDIO_PLAYBACK_GAIN": "2", "MIRROR_AUDIO_MAX_PLAYBACK_LEVEL": "0.9"})
    assert settings.audio_enabled and settings.audio_capture_rate == 44100
    assert settings.audio_playback_rate == 44100 and settings.audio_frame_format == "float32"
    assert settings.audio_playback_gain == 2 and settings.audio_max_playback_level == 0.9
