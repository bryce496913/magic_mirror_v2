"""Explicit full-duplex voice conversation using AudioManager and RealtimeClient."""

import asyncio
import base64
import binascii
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from ..audio import AudioError, AudioManager
from ..config import Settings
from .client import RealtimeClient, RealtimeError
from .events import ConversationEvent, PlaybackAction, PlaybackEvent
from .resample import StreamingPCM, StreamingPeakLimiter

API_RATE = 24000
CHUNK_MS = 100
SILENCE_SECONDS = 60


class VoiceError(RuntimeError):
    """A safe-to-display voice session error."""


@dataclass
class Transcript:
    role: str
    item_id: str
    response_id: str | None
    content_index: int
    text: str
    final: bool


class VoiceConversation:
    """One manually started session. No devices or connections open at construction."""

    def __init__(self, settings: Settings, *, audio: AudioManager | None = None,
                 client: RealtimeClient | None = None,
                 on_transcript: Callable[[Transcript], None] | None = None,
                 on_latency: Callable[[float], None] | None = None,
                 on_event: Callable[[ConversationEvent], None] | None = None,
                 on_playback: Callable[[PlaybackEvent], None] | None = None,
                 before_capture: Callable[[], Awaitable[None]] | None = None,
                 on_ready: Callable[[], None] | None = None,
                 first_speech_seconds: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.audio = audio or AudioManager(settings, enabled=True)
        self.client = client or RealtimeClient(settings)
        self.on_transcript = on_transcript
        self.on_latency = on_latency
        self.on_event = on_event
        self.on_playback = on_playback
        self.before_capture = before_capture
        self.on_ready = on_ready
        self.first_speech_seconds = first_speech_seconds
        self.clock = clock
        self.transcripts: dict[tuple[str, str, int], Transcript] = {}
        self.stop_event = asyncio.Event()
        self._output = asyncio.Queue(maxsize=8)
        self._playback_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._generation = 0
        self._blocked: set[str] = set()
        self._finished_responses: set[str] = set()
        self._current_response: str | None = None
        self._audio_item: str | None = None
        self._audio_content_index = 0
        self._last_speech = self.clock()
        self._speech_end: float | None = None
        self._first_audio_reported = False
        self.heard_speech = False
        self._input_rate: StreamingPCM | None = None
        self._output_rate: StreamingPCM | None = None
        self._limiter = StreamingPeakLimiter(settings.audio_playback_gain, settings.audio_max_playback_level)

    def _check_audio_settings(self) -> None:
        if self.settings.audio_frame_format != "int16" or self.settings.audio_capture_channels != 1 or self.settings.audio_playback_channels != 1:
            raise VoiceError("Voice requires mono int16 PCM input and output")
        if self.settings.audio_capture_rate not in (24000, 48000) or self.settings.audio_playback_rate not in (24000, 48000):
            raise VoiceError("Voice requires 24 or 48 kHz hardware streams")

    async def run(self) -> None:
        self._check_audio_settings()
        self._input_rate = StreamingPCM(self.settings.audio_capture_rate, API_RATE)
        self._output_rate = StreamingPCM(API_RATE, self.settings.audio_playback_rate)
        try:
            await self.client.connect()
            await self.client.send({"type": "session.update", "session": {
                "type": "realtime", "model": self.settings.realtime_model,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": API_RATE},
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                           "prefix_padding_ms": 300, "silence_duration_ms": 600,
                                           "create_response": True, "interrupt_response": True},
                    },
                    "output": {"format": {"type": "audio/pcm", "rate": API_RATE},
                               "voice": "marin"},
                },
            }})
            deadline = asyncio.get_running_loop().time() + self.settings.realtime_connect_timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise VoiceError("Timed out waiting for OpenAI voice settings acknowledgment")
                try:
                    ack = await asyncio.wait_for(self.client.receive(), remaining)
                except TimeoutError:
                    raise VoiceError("Timed out waiting for OpenAI voice settings acknowledgment") from None
                if ack.kind == "error":
                    raise VoiceError(f"OpenAI rejected voice settings ({ack.error_code or 'server error'})")
                if ack.kind == "session.updated":
                    if ack.session_id != self.client.session_id:
                        raise VoiceError("OpenAI voice session ID changed")
                    break
            await asyncio.to_thread(self.audio.selected, "output")
            if self.before_capture is not None:
                await self.before_capture()
            if not await asyncio.to_thread(self.audio.capture.start):
                raise VoiceError("Microphone was still active before voice capture")
            if self.on_ready is not None:
                self.on_ready()
            self._last_speech = self.clock()
            self._tasks = [asyncio.create_task(self._capture_loop()),
                           asyncio.create_task(self._receive_loop()),
                           asyncio.create_task(self._playback_loop()),
                           asyncio.create_task(self._silence_loop())]
            stopper = asyncio.create_task(self.stop_event.wait())
            try:
                done, _ = await asyncio.wait([*self._tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is not stopper:
                        await task
                        if not self.stop_event.is_set():
                            raise VoiceError("Voice worker stopped unexpectedly")
            finally:
                stopper.cancel()
        finally:
            await self.close()

    async def _capture_loop(self) -> None:
        frame_count = self.settings.audio_capture_rate * CHUNK_MS // 1000
        while not self.stop_event.is_set():
            frames = await asyncio.to_thread(self.audio.capture.read_frames, frame_count)
            if self.stop_event.is_set():
                break
            converted = self._input_rate.convert(frames)
            await self.client.send({"type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(converted.astype("<i2", copy=False).tobytes()).decode("ascii")})

    async def _receive_loop(self) -> None:
        while not self.stop_event.is_set():
            event = await self.client.receive()
            await self.handle_event(event)

    async def _silence_loop(self) -> None:
        while not self.stop_event.is_set():
            silence_limit = (self.first_speech_seconds if not self.heard_speech and
                             self.first_speech_seconds is not None else SILENCE_SECONDS)
            if self.clock() - self._last_speech >= silence_limit:
                self.stop_event.set()
                return
            await asyncio.sleep(1)

    async def _playback_loop(self) -> None:
        while not self.stop_event.is_set():
            generation, response_id, chunk = await self._output.get()
            if generation != self._generation or response_id in self._blocked:
                continue
            async with self._playback_lock:
                if generation != self._generation:
                    continue
                try:
                    if chunk is None:
                        if self.audio.playback._stream is not None:
                            await asyncio.to_thread(self.audio.playback.drain, min(5, self.settings.realtime_max_response_seconds))
                            await asyncio.sleep(self.settings.audio_buffer_duration)
                            await asyncio.to_thread(self.audio.playback.stop)
                        if self._current_response == response_id:
                            self._current_response = None
                            self._audio_item = None
                        if self.on_playback:
                            self.on_playback(PlaybackEvent(PlaybackAction.COMPLETED, response_id))
                        continue
                    if self.audio.playback._stream is None:
                        await asyncio.to_thread(self.audio.playback.start, chunk)
                    else:
                        await asyncio.to_thread(self.audio.playback.write, chunk, self.settings.audio_timeout)
                    if self.on_playback:
                        self.on_playback(PlaybackEvent(PlaybackAction.STARTED, response_id))
                except AudioError:
                    if generation != self._generation or self.stop_event.is_set():
                        continue
                    raise

    async def _interrupt_output(self, *, cancel_server: bool = False) -> None:
        response_id = self._current_response
        if response_id:
            self._blocked.add(response_id)
        self._generation += 1
        while not self._output.empty():
            self._output.get_nowait()
        item_id = self._audio_item
        content_index = self._audio_content_index
        await asyncio.to_thread(self.audio.playback.stop)
        played = self.audio.playback.played_frames()
        async with self._playback_lock:
            pass  # wait for any in-flight write to finish after stop unblocks it
        self._current_response = None
        self._audio_item = None
        if response_id and self.on_playback:
            self.on_playback(PlaybackEvent(PlaybackAction.INTERRUPTED, response_id))
        self._output_rate = StreamingPCM(API_RATE, self.settings.audio_playback_rate)
        if item_id:
            await self.client.send({"type": "conversation.item.truncate", "item_id": item_id,
                                    "content_index": content_index,
                                    "audio_end_ms": played * 1000 // self.settings.audio_playback_rate})
        if cancel_server and response_id:
            try:
                await self.client.cancel_response(response_id)
            except RealtimeError:
                pass  # VAD may already have cancelled the response

    async def cancel_reply(self) -> None:
        """Cancel only the current response; microphone capture continues."""
        await self._interrupt_output(cancel_server=True)

    async def handle_event(self, event: ConversationEvent) -> None:
        if event.kind in {"invalid", "duplicate", "unknown"}:
            return
        kind = event.kind
        if kind == "input_audio_buffer.speech_started":
            self.heard_speech = True
            self._last_speech = self.clock()
            if self._current_response:
                await self._interrupt_output()
        elif kind == "input_audio_buffer.speech_stopped":
            self._last_speech = self.clock()
            self._speech_end = self._last_speech
        elif kind == "response.created" and event.response_id:
            if self._current_response and self._current_response != event.response_id:
                await self._interrupt_output()
            self._current_response = event.response_id
            self._first_audio_reported = False
            self._output_rate = StreamingPCM(API_RATE, self.settings.audio_playback_rate)
            self._limiter = StreamingPeakLimiter(self.settings.audio_playback_gain,
                                                  self.settings.audio_max_playback_level)
        elif kind == "response.output_audio.delta":
            if (not event.response_id or event.response_id != self._current_response or
                    event.response_id in self._blocked or event.response_id in self._finished_responses):
                return
            if not event.item_id or event.audio_base64 is None:
                return
            try:
                raw = base64.b64decode(event.audio_base64, validate=True)
            except (ValueError, binascii.Error):
                raise VoiceError("OpenAI sent invalid audio data") from None
            if not raw or len(raw) % 2 or len(raw) > API_RATE * 2:
                raise VoiceError("OpenAI audio chunk has invalid length")
            if self._audio_item and self._audio_item != event.item_id:
                raise VoiceError("Multiple audio items in one response are unsupported")
            self._audio_item = event.item_id
            self._audio_content_index = event.content_index or 0
            if not self._first_audio_reported and self._speech_end is not None:
                if self.on_latency:
                    self.on_latency(self.clock() - self._speech_end)
                self._first_audio_reported = True
            generation = self._generation
            for offset in range(0, len(raw), API_RATE // 10 * 2):
                if generation != self._generation or event.response_id in self._blocked:
                    return
                samples = np.frombuffer(raw[offset:offset + API_RATE // 10 * 2], dtype="<i2")
                chunk = self._limiter.process(self._output_rate.convert(samples)).reshape(-1, 1)
                await self._output.put((generation, event.response_id, chunk))
        elif kind == "response.done" and event.response_id == self._current_response:
            self._finished_responses.add(event.response_id)
            if event.status == "completed":
                await self._output.put((self._generation, event.response_id, None))
            elif event.status == "cancelled":
                await self._interrupt_output()
            else:
                await self._interrupt_output()
                if self.on_event:
                    self.on_event(event)
                raise VoiceError(f"OpenAI response {event.status or 'failed'}")
        elif kind == "error":
            if self.on_event:
                self.on_event(event)
            raise VoiceError(f"OpenAI session error ({event.error_code or 'unknown'})")
        if event.role in {"user", "assistant"} and event.text is not None and event.item_id:
            if event.response_id in self._blocked:
                return
            key = (event.role, event.item_id, event.content_index or 0)
            previous = self.transcripts.get(key)
            text = event.text if event.final else (previous.text if previous else "") + event.text
            transcript = Transcript(event.role, event.item_id, event.response_id,
                                    event.content_index or 0, text, event.final)
            self.transcripts[key] = transcript
            if event.final and self.on_transcript:
                self.on_transcript(transcript)
        if self.on_event is not None:
            self.on_event(event)

    async def close(self) -> None:
        self.stop_event.set()
        # Abort both streams first; this wakes blocked reads and writes.
        try:
            await asyncio.to_thread(self.audio.close)
        finally:
            for task in self._tasks:
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            await self.client.close()
