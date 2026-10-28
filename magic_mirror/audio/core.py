"""Explicit PortAudio stream owners with bounded in-memory frame buffers."""

from collections import deque
from queue import Empty, Full, Queue
from threading import Condition, Event, Lock, RLock
from time import monotonic

import numpy as np

from .devices import DeviceError, check_format, list_devices, select_device
from .pcm import PCMError, playback_gain


class AudioError(RuntimeError):
    """An audio device or operation is unavailable or unsafe."""


class AudioManager:
    """One in-process capture and playback owner; construction opens no audio device."""

    def __init__(self, settings, backend=None, *, enabled=None):
        self.settings = settings
        self.enabled = settings.audio_enabled if enabled is None else enabled
        self._sd = backend
        self.capture = AudioCapture(self)
        self.playback = AudioPlayback(self)

    @property
    def backend(self):
        if self._sd is None:
            try:
                import sounddevice
            except (ImportError, OSError) as exc:
                raise AudioError("PortAudio is unavailable; install sounddevice and libportaudio2") from exc
            self._sd = sounddevice
        return self._sd

    def devices(self):
        return list_devices(self.backend)

    def selected(self, direction):
        if direction not in {"input", "output"}:
            raise ValueError("Audio direction must be input or output")
        settings = self.settings
        selector = settings.audio_input_device if direction == "input" else settings.audio_output_device
        channels = settings.audio_capture_channels if direction == "input" else settings.audio_playback_channels
        rate = settings.audio_capture_rate if direction == "input" else settings.audio_playback_rate
        device = select_device(self.devices(), selector, direction, channels)
        check_format(self.backend, device, direction, channels, rate, settings.audio_frame_format)
        return device

    def close(self):
        """Release both directions even if one cleanup fails."""
        first_error = None
        for owner in (self.capture, self.playback):
            try:
                owner.stop()
            except AudioError as exc:
                first_error = first_error or exc
        if first_error:
            raise first_error


class AudioCapture:
    _claim_lock = Lock()
    _claim_owner = None

    def __init__(self, manager):
        self.manager = manager
        self._lock = RLock()
        self._read_lock = Lock()
        self._stop = Event()
        self._frames = Queue(maxsize=8)
        self._remainder = None
        self._stream = None
        self._error = None
        self.device = None

    def _claim(self):
        with AudioCapture._claim_lock:
            if AudioCapture._claim_owner is not None:
                raise AudioError("Microphone is already owned by this process")
            AudioCapture._claim_owner = self

    def _release(self):
        with AudioCapture._claim_lock:
            if AudioCapture._claim_owner is self:
                AudioCapture._claim_owner = None

    def start(self):
        with self._lock:
            if self._stream is not None:
                return False
            if not self.manager.enabled:
                raise AudioError("Audio is disabled")
            self._claim()
            stream = None
            try:
                settings = self.manager.settings
                self.device = self.manager.selected("input")
                self._frames = Queue(maxsize=8)
                self._remainder = None
                self._error = None
                self._stop.clear()

                def callback(indata, _frames, _time, status):
                    if status:
                        self._error = "Microphone stream reported a device or buffer error"
                    if not self._stop.is_set():
                        try:
                            self._frames.put_nowait(indata.copy())
                        except Full:
                            self._error = "Microphone buffer overrun"

                stream = self.manager.backend.InputStream(
                    device=self.device.index, channels=settings.audio_capture_channels,
                    samplerate=settings.audio_capture_rate, dtype=settings.audio_frame_format,
                    blocksize=max(1, round(settings.audio_capture_rate * settings.audio_buffer_duration)),
                    callback=callback)
                self._stream = stream
                stream.start()
                return True
            except BaseException as exc:
                self._stop.set()
                self._stream = None
                if stream is not None:
                    try:
                        stream.abort()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
                self._release()
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(exc, (DeviceError, AudioError)):
                    raise AudioError(str(exc)) from exc
                raise AudioError("Unable to open microphone") from exc

    def read_frames(self, frame_count, timeout=None):
        settings = self.manager.settings
        if not isinstance(frame_count, int) or not 1 <= frame_count <= settings.audio_capture_rate * 30:
            raise ValueError("Capture request must contain 1 to 30 seconds of frames")
        if not self._read_lock.acquire(blocking=False):
            raise AudioError("Another microphone read is in progress")
        try:
            if self._stream is None:
                raise AudioError("Microphone is not started")
            deadline = monotonic() + (frame_count / settings.audio_capture_rate + settings.audio_timeout
                                      if timeout is None else timeout)
            chunks = []
            remaining = frame_count
            while remaining:
                if self._stop.is_set():
                    raise AudioError("Microphone capture was cancelled")
                if self._error:
                    raise AudioError(self._error)
                chunk = self._remainder
                self._remainder = None
                if chunk is None:
                    wait = min(0.05, deadline - monotonic())
                    if wait <= 0:
                        raise AudioError("Microphone capture timed out or disconnected")
                    try:
                        chunk = self._frames.get(timeout=wait)
                    except Empty:
                        continue
                take = min(remaining, len(chunk))
                chunks.append(chunk[:take])
                if take < len(chunk):
                    self._remainder = chunk[take:]
                remaining -= take
            return np.concatenate(chunks, axis=0)
        finally:
            self._read_lock.release()

    def record(self, seconds):
        if not isinstance(seconds, (int, float)) or not 0 < seconds <= 30:
            raise ValueError("Recording duration must be greater than 0 and at most 30 seconds")
        frame_count = round(seconds * self.manager.settings.audio_capture_rate)
        return self.read_frames(frame_count, timeout=seconds + self.manager.settings.audio_timeout)

    def stop(self):
        with self._lock:
            stream = self._stream
            self._stop.set()
            self._stream = None
            self.device = None
            self._remainder = None
            if stream is None:
                self._release()
                return False
            failed = False
            try:
                stream.abort()
            except Exception:
                failed = True
            try:
                stream.close()
            except Exception:
                failed = True
            self._release()
            if failed:
                raise AudioError("Microphone cleanup failed")
            return True


class AudioPlayback:
    _claim_lock = Lock()
    _claim_owner = None

    def __init__(self, manager):
        self.manager = manager
        self._lock = RLock()
        self._write_lock = Lock()
        self._condition = Condition(Lock())
        self._stop = Event()
        self._queue = deque()
        self._queued_frames = 0
        self._stream = None
        self._error = None
        self.device = None

    def _claim(self):
        with AudioPlayback._claim_lock:
            if AudioPlayback._claim_owner is not None:
                raise AudioError("Speaker is already owned by this process")
            AudioPlayback._claim_owner = self

    def _release(self):
        with AudioPlayback._claim_lock:
            if AudioPlayback._claim_owner is self:
                AudioPlayback._claim_owner = None

    def start(self):
        with self._lock:
            if self._stream is not None:
                return False
            if not self.manager.enabled:
                raise AudioError("Audio is disabled")
            self._claim()
            stream = None
            try:
                settings = self.manager.settings
                self.device = self.manager.selected("output")
                self._stop.clear()
                self._error = None
                with self._condition:
                    self._queue.clear()
                    self._queued_frames = 0

                def callback(outdata, frames, _time, status):
                    if status:
                        self._error = "Speaker stream reported a device or buffer error"
                    outdata.fill(0)
                    if self._stop.is_set():
                        return
                    with self._condition:
                        offset = 0
                        while offset < frames and self._queue:
                            chunk = self._queue[0]
                            count = min(frames - offset, len(chunk))
                            outdata[offset:offset + count] = chunk[:count]
                            if count == len(chunk):
                                self._queue.popleft()
                            else:
                                self._queue[0] = chunk[count:]
                            self._queued_frames -= count
                            offset += count
                        self._condition.notify_all()

                stream = self.manager.backend.OutputStream(
                    device=self.device.index, channels=settings.audio_playback_channels,
                    samplerate=settings.audio_playback_rate, dtype=settings.audio_frame_format,
                    blocksize=max(1, round(settings.audio_playback_rate * settings.audio_buffer_duration)),
                    callback=callback)
                self._stream = stream
                stream.start()
                return True
            except BaseException as exc:
                self._stop.set()
                self._stream = None
                if stream is not None:
                    try:
                        stream.abort()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
                self._release()
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(exc, (DeviceError, AudioError)):
                    raise AudioError(str(exc)) from exc
                raise AudioError("Unable to open speaker") from exc

    def write(self, pcm, timeout=None):
        settings = self.manager.settings
        frames = playback_gain(pcm, settings.audio_frame_format, settings.audio_playback_channels,
                               settings.audio_playback_gain, settings.audio_max_playback_level)
        if len(frames) > settings.audio_playback_rate * 30:
            raise ValueError("Playback request must be at most 30 seconds")
        if not self._write_lock.acquire(blocking=False):
            raise AudioError("Another speaker write is in progress")
        try:
            deadline = monotonic() + (len(frames) / settings.audio_playback_rate + settings.audio_timeout
                                      if timeout is None else timeout)
            block = max(1, round(settings.audio_playback_rate * settings.audio_buffer_duration))
            capacity = block * 8
            for offset in range(0, len(frames), block):
                chunk = frames[offset:offset + block]
                with self._condition:
                    while self._queued_frames + len(chunk) > capacity:
                        if self._stop.is_set() or self._stream is None:
                            raise AudioError("Speaker playback was cancelled")
                        if self._error:
                            raise AudioError(self._error)
                        wait = deadline - monotonic()
                        if wait <= 0:
                            raise AudioError("Speaker playback timed out or disconnected")
                        self._condition.wait(timeout=wait)
                    if self._stop.is_set() or self._stream is None:
                        raise AudioError("Speaker is not started or playback was cancelled")
                    if self._error:
                        raise AudioError(self._error)
                    self._queue.append(chunk)
                    self._queued_frames += len(chunk)
                    self._condition.notify_all()
            return len(frames)
        except AudioError:
            self.stop()
            raise
        finally:
            self._write_lock.release()

    def drain(self, timeout=None):
        deadline = monotonic() + (self.manager.settings.audio_timeout if timeout is None else timeout)
        with self._condition:
            if self._stream is None:
                raise AudioError("Speaker is not started")
            if self._error:
                raise AudioError(self._error)
            while self._queued_frames:
                if self._stop.is_set():
                    raise AudioError("Speaker playback was cancelled")
                if self._error:
                    raise AudioError(self._error)
                wait = deadline - monotonic()
                if wait <= 0:
                    raise AudioError("Speaker drain timed out or disconnected")
                self._condition.wait(timeout=wait)
            if self._error:
                raise AudioError(self._error)

    def stop(self):
        with self._lock:
            stream = self._stream
            self._stop.set()
            self._stream = None
            self.device = None
            with self._condition:
                self._queue.clear()
                self._queued_frames = 0
                self._condition.notify_all()
            if stream is None:
                self._release()
                return False
            failed = False
            try:
                stream.abort()
            except Exception:
                failed = True
            try:
                stream.close()
            except Exception:
                failed = True
            self._release()
            if failed:
                raise AudioError("Speaker cleanup failed")
            return True
