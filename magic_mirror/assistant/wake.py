"""Local, opt-in wake detection with exclusive AudioManager microphone handoff."""

import asyncio
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np
import psutil

from ..audio import AudioError, AudioManager
from ..config import Settings, WAKE_MODEL_FILES, WAKE_MODEL_STEM
from .client import RealtimeError
from .events import ConversationEvent
from .voice import VoiceConversation, VoiceError

READ_MS = 100
MAX_RECOVERY_ATTEMPTS = 3


class WakeError(RuntimeError):
    """A safe-to-display wake detector or handoff failure."""


class WakeState(StrEnum):
    IDLE = "idle"
    WAKE_DETECTED = "wake_detected"
    LISTENING = "listening"
    PROCESSING = "processing"
    RESPONDING = "responding"
    RETURNING_IDLE = "returning_idle"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass(frozen=True)
class WakeMetrics:
    cpu_percent: float
    rss_mib: float
    established_tcp: int
    audio_frames: int
    activations: int
    false_activations: int


class DecimateTo16k:
    """Stateful anti-aliased 48->16 kHz conversion; 16 kHz passes through."""

    def __init__(self, source_rate: int):
        if source_rate not in (16000, 48000):
            raise WakeError("Wake detection requires 16 or 48 kHz mono int16 capture")
        self.source_rate = source_rate
        self._tail = np.zeros(62, dtype=np.float64)
        self._phase = 0
        n = np.arange(63, dtype=np.float64) - 31
        taps = 0.30 * np.sinc(0.30 * n) * np.hamming(63)
        self._taps = taps / taps.sum()

    def convert(self, pcm: np.ndarray) -> np.ndarray:
        frame = np.asarray(pcm)
        if frame.dtype != np.int16 or frame.ndim not in (1, 2) or frame.size == 0:
            raise WakeError("Wake capture must be nonempty mono int16 PCM")
        if frame.ndim == 2:
            if frame.shape[1] != 1:
                raise WakeError("Wake capture must be mono")
            frame = frame[:, 0]
        if len(frame) > self.source_rate:
            raise WakeError("Wake capture chunk exceeds one second")
        if self.source_rate == 16000:
            return frame.astype(np.float32) / 32768
        extended = np.concatenate((self._tail, frame.astype(np.float64)))
        filtered = np.convolve(extended, self._taps, mode="valid")
        self._tail = extended[-62:]
        output = filtered[self._phase::3]
        self._phase = (self._phase - len(frame)) % 3
        return (output / 32768).astype(np.float32)


class SherpaKeywordDetector:
    """One-thread local int8 keyword spotter; loads only after wake is enabled."""

    def __init__(self, settings: Settings):
        root = Path(settings.wake_model_dir)
        if not root.is_absolute() or any(not (root / name).is_file() for name in WAKE_MODEL_FILES):
            raise WakeError("Wake model directory is missing required English int8 model files")
        try:
            import sentencepiece
            import sherpa_onnx
        except (ImportError, OSError) as exc:
            raise WakeError("Install requirements-wake.txt to enable local wake detection") from exc
        self._temporary = tempfile.TemporaryDirectory(prefix="magic-mirror-wake-")
        try:
            tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(root / "bpe.model"))
            tokens = tokenizer.encode(settings.wake_phrase.upper(), out_type=str)
            if not tokens or any(token in {"<unk>", "⁇"} for token in tokens):
                raise WakeError("Wake phrase cannot be encoded by the installed English model")
            keyword_file = Path(self._temporary.name) / "keywords.txt"
            keyword_file.write_text(" ".join(tokens) + f" :1.0 #{settings.wake_threshold:g} @WAKE_PHRASE\n", encoding="utf-8")
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(root / "tokens.txt"),
                encoder=str(root / f"encoder-{WAKE_MODEL_STEM}"),
                decoder=str(root / f"decoder-{WAKE_MODEL_STEM}"),
                joiner=str(root / f"joiner-{WAKE_MODEL_STEM}"),
                num_threads=1, provider="cpu", keywords_file=str(keyword_file),
                keywords_score=1.0, keywords_threshold=settings.wake_threshold,
            )
            self._stream = self._spotter.create_stream()
            self._rate = DecimateTo16k(settings.audio_capture_rate)
        except Exception as exc:
            self._temporary.cleanup()
            if isinstance(exc, WakeError):
                raise
            raise WakeError("Unable to load the local wake model or phrase") from None

    def process(self, pcm: np.ndarray) -> bool:
        try:
            samples = self._rate.convert(pcm)
            self._stream.accept_waveform(16000, samples)
            for _ in range(32):
                if not self._spotter.is_ready(self._stream):
                    return False
                self._spotter.decode_stream(self._stream)
                if self._spotter.get_result(self._stream):
                    self._spotter.reset_stream(self._stream)
                    return True
            raise WakeError("Local wake decoder exceeded its per-chunk work limit")
        except WakeError:
            raise
        except Exception:
            raise WakeError("Local wake inference failed") from None

    def reset(self) -> None:
        try:
            self._spotter.reset_stream(self._stream)
            self._rate = DecimateTo16k(self._rate.source_rate)
        except Exception:
            raise WakeError("Local wake model reset failed") from None

    def close(self) -> None:
        self._stream = None
        self._spotter = None
        self._temporary.cleanup()


class WakeController:
    """Owns one microphone: local idle detection, then Pass 6B voice, then idle."""

    def __init__(self, settings: Settings, *, audio: AudioManager | None = None,
                 detector_factory: Callable = SherpaKeywordDetector,
                 voice_factory: Callable = VoiceConversation,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable = asyncio.sleep,
                 process: psutil.Process | None = None,
                 on_state: Callable[[WakeState], None] | None = None,
                 on_metrics: Callable[[WakeMetrics], None] | None = None):
        self.settings = settings
        self.audio = audio or AudioManager(settings, enabled=True)
        self.detector_factory = detector_factory
        self.voice_factory = voice_factory
        self.clock = clock
        self.sleep = sleep
        self.process = process or psutil.Process()
        self.on_state = on_state
        self.on_metrics = on_metrics
        self.state = WakeState.STOPPED
        self.stop_event = asyncio.Event()
        self.detector = None
        self.voice = None
        self.audio_frames = 0
        self.activations = 0
        self.false_activations = 0
        self._next_wake = 0.0
        self._last_metrics = self.clock()

    def _state(self, value: WakeState) -> None:
        self.state = value
        if self.on_state:
            self.on_state(value)

    def metrics(self) -> WakeMetrics:
        try:
            tcp = sum(connection.status == "ESTABLISHED" for connection in
                      self.process.net_connections(kind="tcp"))
        except (OSError, psutil.Error):
            tcp = -1  # metric unavailable, never claim zero connections
        return WakeMetrics(self.process.cpu_percent(interval=None),
                           self.process.memory_info().rss / (1024 * 1024),
                           tcp,
                           self.audio_frames, self.activations, self.false_activations)

    def stop(self) -> None:
        self.stop_event.set()
        if self.voice is not None:
            self.voice.stop_event.set()
        try:
            self.audio.capture.stop()
        except AudioError:
            pass  # run() still performs full cleanup and reports persistent errors

    def end_conversation(self) -> None:
        """Return active voice to local idle without disabling the wake detector."""
        if self.voice is not None:
            self.voice.stop_event.set()

    def _voice_event(self, event: ConversationEvent) -> None:
        if event.role == "user" and event.final and event.text and (
                " ".join(event.text.strip().lower().rstrip(".!?").split()) in
                {"goodbye", "go to sleep", "stop listening"}):
            self.end_conversation()
            return
        if event.kind == "input_audio_buffer.speech_started":
            self._state(WakeState.LISTENING)
        elif event.kind == "input_audio_buffer.speech_stopped":
            self._state(WakeState.PROCESSING)
        elif event.kind in {"response.created", "response.output_audio.delta"}:
            self._state(WakeState.RESPONDING)
        elif event.kind == "response.done" and event.status == "completed":
            self._state(WakeState.LISTENING)

    async def _activate(self) -> None:
        """Keep draining local wake capture until the voice session is configured."""
        drain_stop = asyncio.Event()
        frame_count = self.settings.audio_capture_rate * READ_MS // 1000

        async def drain_local() -> None:
            while not drain_stop.is_set() and not self.stop_event.is_set():
                try:
                    await asyncio.to_thread(self.audio.capture.read_frames, frame_count)
                except AudioError:
                    if drain_stop.is_set() or self.stop_event.is_set():
                        return
                    raise

        drain_task = asyncio.create_task(drain_local())

        async def handoff() -> None:
            drain_stop.set()
            await asyncio.to_thread(self.audio.capture.stop)
            try:
                await asyncio.wait_for(drain_task, self.settings.audio_timeout + 1)
            except TimeoutError:
                raise WakeError("Wake capture did not release before voice capture") from None
            # The wake phrase and buffered post-wake audio are deliberately discarded.

        try:
            self.voice = self.voice_factory(
                self.settings, audio=self.audio, before_capture=handoff,
                first_speech_seconds=self.settings.wake_first_speech_seconds,
                on_event=self._voice_event, on_ready=lambda: self._state(WakeState.LISTENING),
            )
        except Exception:
            drain_stop.set()
            await asyncio.to_thread(self.audio.capture.stop)
            await asyncio.gather(drain_task, return_exceptions=True)
            raise WakeError("Unable to initialize voice handoff") from None
        voice_task = asyncio.create_task(self.voice.run())
        stopper = asyncio.create_task(self.stop_event.wait())
        try:
            pending = {voice_task, stopper, drain_task}
            while True:
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if drain_task in done:
                    pending.remove(drain_task)
                    if not drain_stop.is_set():
                        try:
                            await drain_task
                        finally:
                            voice_task.cancel()
                            await asyncio.gather(voice_task, return_exceptions=True)
                        raise WakeError("Wake microphone stopped during voice connection")
                if stopper in done:
                    voice_task.cancel()
                    await asyncio.gather(voice_task, return_exceptions=True)
                    return
                if voice_task in done:
                    await voice_task
                    break
            if not self.voice.heard_speech:
                self.false_activations += 1
        finally:
            drain_stop.set()
            if not drain_task.done():
                await asyncio.to_thread(self.audio.capture.stop)
            await asyncio.gather(drain_task, return_exceptions=True)
            stopper.cancel()
            self.voice = None
            self._state(WakeState.RETURNING_IDLE)

    async def run(self) -> None:
        if not self.settings.wake_enabled:
            await asyncio.to_thread(self.audio.close)
            self._state(WakeState.STOPPED)
            return
        if not self.settings.openai_api_key:
            raise WakeError("OPENAI_API_KEY is required before enabling wake conversations")
        if (self.settings.audio_frame_format != "int16" or self.settings.audio_capture_channels != 1 or
                self.settings.audio_capture_rate not in (16000, 48000)):
            raise WakeError("Wake detection requires 16 or 48 kHz mono int16 capture")
        failures = 0
        self.process.cpu_percent(interval=None)
        try:
            self.detector = self.detector_factory(self.settings)
            while not self.stop_event.is_set():
                try:
                    if not await asyncio.to_thread(self.audio.capture.start):
                        raise WakeError("Microphone was already active before idle detection")
                    self.process.cpu_percent(interval=None)
                    self._last_metrics = self.clock()
                    self._state(WakeState.IDLE)
                    frame_count = self.settings.audio_capture_rate * READ_MS // 1000
                    while not self.stop_event.is_set():
                        frames = await asyncio.to_thread(self.audio.capture.read_frames, frame_count)
                        self.audio_frames += len(frames)
                        now = self.clock()
                        if self.on_metrics and now - self._last_metrics >= 60:
                            self.on_metrics(self.metrics())
                            self._last_metrics = now
                        if now < self._next_wake:
                            continue
                        if await asyncio.to_thread(self.detector.process, frames):
                            self.activations += 1
                            self._state(WakeState.WAKE_DETECTED)
                            await self._activate()
                            self._next_wake = self.clock() + self.settings.wake_cooldown_seconds
                            self.detector.reset()
                            failures = 0
                            break
                except (AudioError, WakeError, VoiceError, RealtimeError) as exc:
                    if self.stop_event.is_set():
                        break
                    self._state(WakeState.ERROR)
                    await asyncio.to_thread(self.audio.close)
                    self.detector.reset()
                    failures += 1
                    if failures >= MAX_RECOVERY_ATTEMPTS:
                        raise WakeError(f"Wake and voice recovery stopped after {failures} failures: {exc}") from None
                    await self.sleep(min(2 ** (failures - 1), 4))
                    self._next_wake = self.clock() + self.settings.wake_cooldown_seconds
        finally:
            self.stop()
            await asyncio.to_thread(self.audio.close)
            if self.detector is not None:
                self.detector.close()
            self._state(WakeState.STOPPED)
