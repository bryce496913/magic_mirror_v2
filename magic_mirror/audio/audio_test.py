"""Standalone, bounded PortAudio checks; no Flask or network API is used."""

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from magic_mirror.config import ConfigurationError, Settings
from .core import AudioError, AudioManager
from .devices import DeviceError
from .pcm import levels


def _tone(settings, seconds):
    count = round(settings.audio_playback_rate * seconds)
    phase = np.arange(count, dtype=np.float64) * (2 * np.pi * 440 / settings.audio_playback_rate)
    wave = 0.12 * np.sin(phase)
    if settings.audio_frame_format == "int16":
        wave = np.rint(wave * 32767).astype(np.int16)
    else:
        wave = wave.astype(np.float32)
    return np.repeat(wave[:, None], settings.audio_playback_channels, axis=1)


def run(settings, action, seconds=5.0, backend=None, sleep=time.sleep):
    # The CLI invocation is explicit authorization to exercise hardware; the
    # dashboard's MIRROR_AUDIO_ENABLED=false default remains unchanged.
    manager = AudioManager(settings, backend=backend, enabled=True)
    try:
        if action == "list":
            devices = manager.devices()
            if not devices:
                raise AudioError("No PortAudio devices found")
            for device in devices:
                print(f"{device.label()}: input={device.input_channels}, "
                      f"output={device.output_channels}, default_rate={device.default_rate:g} Hz")
            return
        if action == "selected":
            for direction in ("input", "output"):
                device = manager.selected(direction)
                rate = settings.audio_capture_rate if direction == "input" else settings.audio_playback_rate
                channels = settings.audio_capture_channels if direction == "input" else settings.audio_playback_channels
                print(f"Selected {direction}: {device.label()}; configured={rate} Hz, "
                      f"{channels} channel(s), {settings.audio_frame_format}; "
                      f"device default={device.default_rate:g} Hz")
            return
        if action not in {"tone", "record", "replay"}:
            raise ValueError("Unknown audio test action")
        if not math.isfinite(seconds) or not 0.1 <= seconds <= 30:
            raise ValueError("Audio test duration must be between 0.1 and 30 seconds")
        if action in {"record", "replay"}:
            if action == "replay" and (settings.audio_capture_rate != settings.audio_playback_rate or
                                       settings.audio_capture_channels != settings.audio_playback_channels):
                raise AudioError("Replay requires matching capture and playback rates and channels")
            manager.capture.start()
            print(f"Recording from {manager.capture.device.label()} for {seconds:g} seconds")
            recorded = manager.capture.record(seconds)
            manager.capture.stop()
            readings = levels(recorded, settings.audio_frame_format, settings.audio_capture_channels)
            print(f"Microphone peak: {readings['peak_dbfs']:.1f} dBFS; "
                  f"RMS: {readings['rms_dbfs']:.1f} dBFS")
            if action == "record":
                return
            frames = recorded
        else:
            frames = _tone(settings, seconds)
        manager.playback.start()
        print(f"Playing through {manager.playback.device.label()}")
        manager.playback.write(frames, timeout=seconds + settings.audio_timeout)
        manager.playback.drain(timeout=seconds + settings.audio_timeout)
        sleep(settings.audio_buffer_duration)  # allow the final PortAudio buffer to reach the speaker
        manager.playback.stop()
    finally:
        manager.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect and test audio devices without Flask")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="List PortAudio devices and capabilities")
    actions.add_parser("selected", help="Show the configured input and output selection")
    for name, default in (("tone", 1.0), ("record", 5.0), ("replay", 5.0)):
        command = actions.add_parser(name)
        command.add_argument("--seconds", type=float, default=default, help="Duration, 0.1–30 seconds")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    try:
        run(Settings.from_env(), args.action, seconds=getattr(args, "seconds", 5.0))
    except (ConfigurationError, AudioError, DeviceError, ValueError) as exc:
        print(f"Audio test error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Audio test interrupted; streams released", file=sys.stderr)
        return 130
    print("Audio test complete; streams released")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
