"""Explicit, local wake-word service; never started by Flask."""

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from ..audio import AudioError
from ..config import ConfigurationError, Settings
from .wake import WakeController, WakeError, WakeMetrics, WakeState


async def _interactive(settings: Settings) -> None:
    def state_changed(state: WakeState) -> None:
        print(f"Wake state: {state.value}", flush=True)

    def metrics(value: WakeMetrics) -> None:
        print(f"Idle process CPU: {value.cpu_percent:.1f}%; RSS: {value.rss_mib:.1f} MiB; "
              f"established TCP: {value.established_tcp}; "
              f"frames: {value.audio_frames}; activations: {value.activations}; "
              f"false activations: {value.false_activations}", flush=True)

    controller = WakeController(settings, on_state=state_changed, on_metrics=metrics)
    loop = asyncio.get_running_loop()

    def command() -> None:
        line = sys.stdin.readline().strip().lower()
        if line == "q":
            controller.stop()
        elif line == "e":
            controller.end_conversation()

    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, controller.stop)
        if sys.stdin.isatty():
            loop.add_reader(sys.stdin.fileno(), command)
        print(f"Local wake detector starting for '{settings.wake_phrase}'. "
              "After detection, wait for the listening cue before asking a question. "
              "Say 'go to sleep' or press e+Enter to end a conversation; "
              "q+Enter or Ctrl+C stops the assistant.", flush=True)
        await controller.run()
    finally:
        if sys.stdin.isatty():
            loop.remove_reader(sys.stdin.fileno())
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local wake-word detection; offline unless --start is passed")
    parser.add_argument("--start", action="store_true", help="open the local microphone for wake detection")
    args = parser.parse_args(argv)
    if not args.start:
        print("Offline. Use --start after enabling MIRROR_WAKE_ENABLED and installing the local model.")
        return 0
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    try:
        settings = Settings.from_env()
        if not settings.wake_enabled:
            raise WakeError("MIRROR_WAKE_ENABLED=false; enable it in the ignored local .env first")
        asyncio.run(_interactive(settings))
    except (ConfigurationError, WakeError, AudioError, OSError) as exc:
        print(f"Wake detector error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Wake detector stopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
