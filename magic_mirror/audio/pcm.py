"""Bounded PCM validation and peak-safe output gain."""

import math

import numpy as np


class PCMError(ValueError):
    """PCM frames have an unsupported or corrupt shape or representation."""


def decode_pcm(pcm, frame_format, channels):
    if frame_format not in {"int16", "float32"} or channels not in {1, 2}:
        raise PCMError("Unsupported PCM format or channel count")
    dtype = np.dtype("<i2" if frame_format == "int16" else "<f4")
    if isinstance(pcm, (bytes, bytearray, memoryview)):
        if not pcm or len(pcm) % (dtype.itemsize * channels):
            raise PCMError("PCM byte length must contain complete, nonempty frames")
        frames = np.frombuffer(pcm, dtype=dtype).copy().reshape(-1, channels)
    elif isinstance(pcm, np.ndarray):
        if pcm.dtype != dtype or pcm.size == 0:
            raise PCMError(f"PCM must be nonempty {frame_format} data")
        frames = pcm.reshape(-1, 1) if pcm.ndim == 1 and channels == 1 else pcm
        if frames.ndim != 2 or frames.shape[1] != channels:
            raise PCMError("PCM channel count does not match playback configuration")
        frames = frames.copy()
    else:
        raise PCMError("PCM must be bytes or a NumPy array")
    if frame_format == "float32" and not np.isfinite(frames).all():
        raise PCMError("PCM contains non-finite samples")
    if frame_format == "float32" and np.max(np.abs(frames.astype(np.float64))) > 1:
        raise PCMError("Float PCM must be within -1 to 1")
    return frames


def playback_gain(pcm, frame_format, channels, gain=1.0, max_level=1.0):
    """Apply gain in float64, then saturate before converting back to PCM."""
    frames = decode_pcm(pcm, frame_format, channels)
    if not math.isfinite(gain) or not 0 <= gain <= 4 or not math.isfinite(max_level) or not 0.1 <= max_level <= 1:
        raise PCMError("Playback gain or maximum level is invalid")
    amplified = frames.astype(np.float64) * gain
    if frame_format == "int16":
        lower = math.ceil(-32768 * max_level)
        upper = math.floor(32767 * max_level)
        return np.clip(np.rint(amplified), lower, upper).astype(np.int16)
    return np.clip(amplified, -max_level, max_level).astype(np.float32)


def levels(pcm, frame_format, channels):
    frames = decode_pcm(pcm, frame_format, channels).astype(np.float64)
    normalized = frames / 32768 if frame_format == "int16" else frames
    peak = float(np.max(np.abs(normalized)))
    rms = float(np.sqrt(np.mean(np.square(normalized))))
    return {"peak_dbfs": 20 * math.log10(peak) if peak else float("-inf"),
            "rms_dbfs": 20 * math.log10(rms) if rms else float("-inf")}
