"""Bounded streaming mono int16 PCM conversion for 24/48 kHz speech."""

import numpy as np


class PCMRateError(ValueError):
    """The Realtime PCM format cannot be converted safely."""


class StreamingPCM:
    def __init__(self, source_rate: int, target_rate: int):
        if source_rate not in (24000, 48000) or target_rate not in (24000, 48000):
            raise PCMRateError("Voice PCM rates must be 24000 or 48000 Hz")
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._tail = np.zeros(62, dtype=np.float64)
        self._phase = 0
        self._previous = None
        # 63-tap windowed-sinc low-pass rejects content above the 24 kHz Nyquist
        # before 48->24 decimation. Speech passband ends near 10.8 kHz.
        n = np.arange(63, dtype=np.float64) - 31
        taps = 0.45 * np.sinc(0.45 * n) * np.hamming(63)
        self._taps = taps / taps.sum()

    def convert(self, samples) -> np.ndarray:
        frame = np.asarray(samples)
        if frame.dtype != np.int16 or frame.size == 0 or frame.ndim not in (1, 2):
            raise PCMRateError("Voice PCM must be nonempty int16 mono")
        if frame.ndim == 2:
            if frame.shape[1] != 1:
                raise PCMRateError("Voice PCM must be mono")
            frame = frame[:, 0]
        if len(frame) > self.source_rate:
            raise PCMRateError("Voice PCM chunk exceeds one second")
        if self.source_rate == self.target_rate:
            return frame.copy()
        if self.source_rate == 48000:
            extended = np.concatenate((self._tail, frame.astype(np.float64)))
            filtered = np.convolve(extended, self._taps, mode="valid")
            self._tail = extended[-62:]
            result = filtered[self._phase::2]
            self._phase = (self._phase + len(frame)) % 2
        else:
            values = frame.astype(np.float64)
            previous = values[0] if self._previous is None else self._previous
            prior = np.concatenate(([previous], values[:-1]))
            result = np.empty(len(values) * 2, dtype=np.float64)
            result[0::2] = prior
            result[1::2] = (prior + values) / 2
            self._previous = values[-1]
        return np.clip(np.rint(result), -32768, 32767).astype(np.int16)


class StreamingPeakLimiter:
    """Keep voice peaks below a conservative ceiling after AudioPlayback gain."""

    def __init__(self, gain: float, max_level: float):
        self.gain = gain
        self.ceiling = min(max_level, 0.85)
        self._scale = 1.0

    def process(self, frames: np.ndarray) -> np.ndarray:
        peak = float(np.max(np.abs(frames.astype(np.int32)))) / 32768
        desired = min(1.0, self.ceiling / (peak * self.gain)) if peak and self.gain else 1.0
        self._scale = desired if desired < self._scale else min(desired, self._scale + 0.02)
        return np.clip(np.rint(frames.astype(np.float64) * self._scale), -32768, 32767).astype(np.int16)
