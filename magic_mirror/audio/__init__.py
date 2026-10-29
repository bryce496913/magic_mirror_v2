"""Opt-in, in-process audio components; importing this package opens no devices."""

from .core import AudioError, AudioManager

__all__ = ("AudioError", "AudioManager")
