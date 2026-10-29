"""Local wake control with fake detector, microphone, voice and process metrics."""

import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from magic_mirror.assistant.client import RealtimeError
from magic_mirror.assistant.events import ConversationEvent, ConversationState
from magic_mirror.assistant.wake import (DecimateTo16k, WakeController, WakeError,
                                        WakeState)
from magic_mirror.assistant.wake_cli import main as cli_main
from magic_mirror.audio import AudioError
from magic_mirror.config import ConfigurationError, Settings, WAKE_MODEL_FILES


class FakeCapture:
    def __init__(self):
        self.active = False
        self.owner = None
        self.starts = 0
        self.stops = 0
        self.reads = 0
        self.fail_reads = 0

    def start(self):
        if self.active:
            return False
        self.active = True
        self.owner = "wake" if self.starts % 2 == 0 else "voice"
        self.starts += 1
        return True

    def read_frames(self, count):
        time.sleep(0.002)
        if not self.active:
            raise AudioError("microphone stopped")
        self.reads += 1
        if self.fail_reads:
            self.fail_reads -= 1
            raise AudioError("microphone disconnected")
        return np.zeros((count, 1), dtype=np.int16)

    def stop(self):
        was_active = self.active
        self.active = False
        self.owner = None
        if was_active:
            self.stops += 1
        return was_active


class FakeAudio:
    def __init__(self):
        self.capture = FakeCapture()
        self.closes = 0

    def close(self):
        self.capture.stop()
        self.closes += 1


class FakeDetector:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = 0
        self.resets = 0
        self.closed = False

    def process(self, frames):
        self.calls += 1
        return self.results.pop(0) if self.results else False

    def reset(self):
        self.resets += 1

    def close(self):
        self.closed = True


class FakeProcess:
    def cpu_percent(self, interval=None):
        return 2.5

    def memory_info(self):
        return SimpleNamespace(rss=48 * 1024 * 1024)

    def net_connections(self, kind="tcp"):
        assert kind == "tcp"
        return []


class FakeVoice:
    def __init__(self, settings, *, audio, before_capture, first_speech_seconds,
                 on_event, on_ready, gate=None, failure=None, heard_speech=True):
        self.audio = audio
        self.before_capture = before_capture
        self.on_event = on_event
        self.on_ready = on_ready
        self.gate = gate
        self.failure = failure
        self.heard_speech = heard_speech
        self.stop_event = asyncio.Event()
        self.first_speech_seconds = first_speech_seconds
        self.started = False

    async def run(self):
        assert self.audio.capture.active  # wake capture remains open during handshake
        await asyncio.sleep(0)
        if self.failure:
            raise self.failure
        await self.before_capture()
        assert not self.audio.capture.active
        assert self.audio.capture.start()
        self.started = True
        self.on_ready()
        if self.heard_speech:
            self.on_event(ConversationEvent("input_audio_buffer.speech_started", ConversationState.LISTENING))
            self.on_event(ConversationEvent("input_audio_buffer.speech_stopped", ConversationState.PROCESSING))
            self.on_event(ConversationEvent("response.created", ConversationState.RESPONDING))
        if self.gate is not None:
            waits = [asyncio.create_task(self.gate.wait()), asyncio.create_task(self.stop_event.wait())]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits:
                    task.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
        self.audio.close()


def settings(**changes):
    values = dict(wake_enabled=True, wake_model_dir="/tmp/model",
                  openai_api_key="fake-key", audio_capture_rate=48000,
                  audio_capture_channels=1, audio_frame_format="int16",
                  wake_cooldown_seconds=0.01)
    values.update(changes)
    return Settings(**values)


async def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition did not become true")


def test_disabled_mode_opens_no_audio_or_voice():
    async def scenario():
        audio = FakeAudio()
        called = []
        controller = WakeController(Settings(wake_enabled=False), audio=audio,
                                    detector_factory=lambda _: called.append("detector"),
                                    voice_factory=lambda **kwargs: called.append("voice"))
        await controller.run()
        assert controller.state == WakeState.STOPPED
        assert audio.capture.starts == 0 and not called
    asyncio.run(scenario())


def test_non_wake_audio_stays_local_and_shutdown_releases_mic():
    async def scenario():
        audio, detector = FakeAudio(), FakeDetector()
        voices = []
        now, reported = [0.0], []
        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=lambda *a, **k: voices.append(1),
                                    process=FakeProcess(), clock=lambda: now[0],
                                    on_metrics=reported.append)
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: detector.calls >= 4)
        assert controller.state == WakeState.IDLE
        assert not voices and audio.capture.active
        sample = controller.metrics()
        assert sample.cpu_percent == 2.5 and sample.rss_mib == 48 and sample.established_tcp == 0
        now[0] = 60.0
        await wait_until(lambda: len(reported) == 1)
        assert reported[0].established_tcp == 0
        controller.stop()
        await task
        assert detector.closed and not audio.capture.active and controller.state == WakeState.STOPPED
    asyncio.run(scenario())


def test_wake_handoff_states_cooldown_and_return_to_idle():
    async def scenario():
        audio, detector, states = FakeAudio(), FakeDetector([False, True, True]), []
        voices = []

        def make_voice(*args, **kwargs):
            voice = FakeVoice(*args, **kwargs)
            voices.append(voice)
            return voice

        controller = WakeController(settings(wake_cooldown_seconds=1.0), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=make_voice, on_state=states.append,
                                    process=FakeProcess())
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: len(voices) == 1 and audio.capture.starts >= 3)
        assert audio.capture.active and controller.activations == 1
        assert WakeState.WAKE_DETECTED in states
        assert WakeState.PROCESSING in states and WakeState.RESPONDING in states
        before = detector.calls
        await asyncio.sleep(0.02)
        assert detector.calls == before  # cooldown suppresses duplicate inference
        controller.stop()
        await task
        assert audio.capture.stops >= 3 and not audio.capture.active
        assert detector.resets == 1 and detector.closed
    asyncio.run(scenario())


def test_activation_while_busy_does_not_start_second_session():
    async def scenario():
        audio, detector, gate = FakeAudio(), FakeDetector([True, True, True]), asyncio.Event()
        voices = []

        def make_voice(*args, **kwargs):
            voice = FakeVoice(*args, gate=gate, **kwargs)
            voices.append(voice)
            return voice

        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=make_voice, process=FakeProcess())
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: voices and voices[0].started)
        calls = detector.calls
        await asyncio.sleep(0.02)
        assert len(voices) == 1 and detector.calls == calls
        controller.stop()
        await task
        assert not audio.capture.active
    asyncio.run(scenario())


def test_spoken_end_returns_to_idle_without_stopping_detector():
    async def scenario():
        audio, detector, gate = FakeAudio(), FakeDetector([True]), asyncio.Event()
        voices = []

        def make_voice(*args, **kwargs):
            voice = FakeVoice(*args, gate=gate, **kwargs)
            voices.append(voice)
            return voice

        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=make_voice, process=FakeProcess())
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: voices and voices[0].started)
        controller._voice_event(ConversationEvent(
            "conversation.item.input_audio_transcription.completed", ConversationState.LISTENING,
            role="user", item_id="user", text="Go to sleep.", final=True))
        await wait_until(lambda: controller.state == WakeState.IDLE and audio.capture.starts >= 3)
        assert not controller.stop_event.is_set() and controller.activations == 1
        controller.stop()
        await task
        assert not audio.capture.active
    asyncio.run(scenario())


def test_false_activation_counts_and_returns_idle():
    async def scenario():
        audio, detector = FakeAudio(), FakeDetector([True])
        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=lambda *a, **k: FakeVoice(*a, heard_speech=False, **k),
                                    process=FakeProcess())
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: controller.false_activations == 1 and controller.state == WakeState.IDLE)
        controller.stop()
        await task
        assert controller.activations == 1 and detector.resets == 1
    asyncio.run(scenario())


def test_repeated_wake_sleep_cycles_reuse_one_audio_manager():
    async def scenario():
        audio, detector = FakeAudio(), FakeDetector([True, True])
        voices = []

        def make_voice(*args, **kwargs):
            voice = FakeVoice(*args, **kwargs)
            voices.append(voice)
            return voice

        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=make_voice, process=FakeProcess())
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: controller.activations == 2 and controller.state == WakeState.IDLE)
        assert len(voices) == 2 and audio.capture.starts == 5
        controller.stop()
        await task
        assert not audio.capture.active and detector.resets == 2
    asyncio.run(scenario())


def test_device_disconnect_retries_bounded_and_recovers():
    async def scenario():
        audio, detector, states = FakeAudio(), FakeDetector(), []
        audio.capture.fail_reads = 1
        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    sleep=lambda _: asyncio.sleep(0),
                                    process=FakeProcess(), on_state=states.append)
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: WakeState.ERROR in states and audio.capture.starts >= 2)
        controller.stop()
        await task
        assert audio.capture.stops >= 2 and not audio.capture.active

        audio = FakeAudio()
        audio.capture.fail_reads = 4
        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: FakeDetector(),
                                    sleep=lambda _: asyncio.sleep(0), process=FakeProcess())
        with pytest.raises(WakeError, match="3 failures"):
            await controller.run()
        assert not audio.capture.active and controller.state == WakeState.STOPPED
    asyncio.run(scenario())


def test_voice_failure_closes_mic_before_recovery():
    async def scenario():
        audio, detector = FakeAudio(), FakeDetector([True])
        count = [0]

        def make_voice(*args, **kwargs):
            count[0] += 1
            return FakeVoice(*args, failure=RealtimeError("network interrupted"), **kwargs)

        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=make_voice,
                                    sleep=lambda _: asyncio.sleep(0), process=FakeProcess())
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: count[0] == 1 and audio.capture.starts >= 2)
        controller.stop()
        await task
        assert not audio.capture.active and audio.closes >= 2
    asyncio.run(scenario())


def test_mic_disconnect_during_handshake_cancels_pending_voice():
    class SlowVoice(FakeVoice):
        cancelled = False

        async def run(self):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def scenario():
        audio, detector, voices = FakeAudio(), FakeDetector([True]), []

        def make_voice(*args, **kwargs):
            audio.capture.fail_reads = 1
            voice = SlowVoice(*args, **kwargs)
            voices.append(voice)
            return voice

        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=make_voice,
                                    sleep=lambda _: asyncio.sleep(0), process=FakeProcess())
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: voices and voices[0].cancelled and audio.capture.starts >= 2)
        controller.stop()
        await task
        assert not audio.capture.active and controller.state == WakeState.STOPPED
    asyncio.run(scenario())


def test_voice_factory_failure_releases_wake_capture():
    async def scenario():
        audio, detector = FakeAudio(), FakeDetector([True])

        def failed_voice(*args, **kwargs):
            raise RuntimeError("factory failure")

        controller = WakeController(settings(), audio=audio,
                                    detector_factory=lambda _: detector,
                                    voice_factory=failed_voice,
                                    sleep=lambda _: asyncio.sleep(0), process=FakeProcess())
        task = asyncio.create_task(controller.run())
        await wait_until(lambda: audio.capture.starts >= 2 and controller.state == WakeState.IDLE)
        controller.stop()
        await task
        assert not audio.capture.active and audio.capture.stops >= 2
    asyncio.run(scenario())


def test_decimator_48k_to_16k_and_invalid_frames():
    decimator = DecimateTo16k(48000)
    samples = np.rint(10000 * np.sin(2 * np.pi * 1000 * np.arange(48000) / 48000)).astype(np.int16)
    result = np.concatenate([decimator.convert(samples[i:i + 4800]) for i in range(0, len(samples), 4800)])
    assert len(result) == 16000 and result.dtype == np.float32
    assert np.max(np.abs(result)) < 1
    high = np.rint(10000 * np.sin(2 * np.pi * 12000 * np.arange(48000) / 48000)).astype(np.int16)
    rejected = DecimateTo16k(48000).convert(high)
    assert np.sqrt(np.mean(rejected[1000:].astype(float) ** 2)) < 0.01
    with pytest.raises(WakeError):
        DecimateTo16k(44100)
    with pytest.raises(WakeError):
        decimator.convert(np.ones((100, 2), dtype=np.int16))


def test_wake_config_and_cli_default(tmp_path, capsys):
    model = tmp_path / "model"
    model.mkdir()
    for name in WAKE_MODEL_FILES:
        (model / name).touch()
    loaded = Settings.from_env({"MIRROR_WAKE_ENABLED": "true", "MIRROR_WAKE_MODEL_DIR": str(model),
                                "MIRROR_WAKE_PHRASE": "Hey Magic Mirror"})
    assert loaded.wake_phrase == "hey magic mirror" and loaded.wake_enabled
    (model / WAKE_MODEL_FILES[0]).unlink()
    with pytest.raises(ConfigurationError, match="missing required"):
        Settings.from_env({"MIRROR_WAKE_ENABLED": "true", "MIRROR_WAKE_MODEL_DIR": str(model)})
    for values in ({"MIRROR_WAKE_ENABLED": "true"},
                   {"MIRROR_WAKE_PHRASE": "hello!"},
                   {"MIRROR_WAKE_COOLDOWN_SECONDS": "-1"},
                   {"MIRROR_WAKE_THRESHOLD": "1.0"},
                   {"MIRROR_WAKE_FIRST_SPEECH_SECONDS": "100"}):
        with pytest.raises(ConfigurationError):
            Settings.from_env(values)
    assert cli_main([]) == 0
    assert "Offline" in capsys.readouterr().out
