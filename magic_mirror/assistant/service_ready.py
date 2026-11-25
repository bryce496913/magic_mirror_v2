"""Bounded, read-only audio readiness check for the user-level voice service."""

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from ..audio import AudioManager
from ..audio.devices import DeviceError
from ..audio.core import AudioError
from ..config import ConfigurationError, Settings


def audio_ready(settings: Settings) -> bool:
    """Check selected PortAudio formats and verified PipeWire routing, without streams."""
    manager = AudioManager(settings)
    try:
        manager.selected("input")
        manager.selected("output")
    except (AudioError, DeviceError):
        return False
    return True


def wait_for_audio(settings: Settings, seconds: float, *, check=audio_ready,
                   clock=time.monotonic, sleep=time.sleep) -> bool:
    """Bound startup retries; systemd remains responsible for later recovery."""
    deadline = clock() + seconds
    while True:
        if check(settings):
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(2.0, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local voice audio readiness without opening streams")
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 120:
        parser.error("--wait-seconds must be between 0 and 120")
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    try:
        settings = Settings.from_env()
    except ConfigurationError:
        print("Voice service configuration is invalid; inspect the local .env", file=sys.stderr)
        return 1
    if not settings.wake_enabled or not settings.openai_api_key:
        print("Voice service requires enabled wake detection and a local API key", file=sys.stderr)
        return 1
    if wait_for_audio(settings, args.wait_seconds):
        print("Voice audio route ready", flush=True)
        return 0
    print("Voice audio route unavailable; check PipeWire, WirePlumber and configured devices", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
