"""Manually started local voice conversation; no Flask route or background service."""

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from ..audio import AudioError
from ..config import ConfigurationError, Settings
from .client import RealtimeError
from .voice import Transcript, VoiceConversation, VoiceError


async def _interactive(settings: Settings) -> None:
    def transcript(item: Transcript) -> None:
        print(f"{'You' if item.role == 'user' else 'Assistant'}: {item.text}", flush=True)

    def latency(seconds: float) -> None:
        print(f"Speech-end to first audio received: {seconds:.2f}s", flush=True)

    voice = VoiceConversation(settings, on_transcript=transcript, on_latency=latency)
    loop = asyncio.get_running_loop()
    queued_commands: set[asyncio.Task] = set()

    def command() -> None:
        line = sys.stdin.readline().strip().lower()
        if line == "c":
            task = asyncio.create_task(voice.cancel_reply())
            queued_commands.add(task)
            def finished(done: asyncio.Task) -> None:
                queued_commands.discard(done)
                try:
                    done.result()
                except (AudioError, RealtimeError, VoiceError) as exc:
                    print(f"Reply cancellation failed: {exc}", file=sys.stderr)
                    voice.stop_event.set()
                except asyncio.CancelledError:
                    pass
            task.add_done_callback(finished)
        elif line == "q" or not line and not sys.stdin.isatty():
            voice.stop_event.set()

    try:
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, voice.stop_event.set)
        if sys.stdin.isatty():
            loop.add_reader(sys.stdin.fileno(), command)
        print("Voice session starting. Speak naturally; press c+Enter to cancel a reply, q+Enter to end, or Ctrl+C to stop.", flush=True)
        await voice.run()
    finally:
        if sys.stdin.isatty():
            loop.remove_reader(sys.stdin.fileno())
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(name)
        for task in queued_commands:
            task.cancel()
        if queued_commands:
            await asyncio.gather(*queued_commands, return_exceptions=True)
        print("Voice session ended; audio streams released", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local voice assistant; offline unless --start is specified")
    parser.add_argument("--start", action="store_true", help="open microphone, speaker and OpenAI Realtime session")
    args = parser.parse_args(argv)
    if not args.start:
        print("Offline. Use --start to open a live voice session.")
        return 0
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    try:
        asyncio.run(_interactive(Settings.from_env()))
    except (ConfigurationError, RealtimeError, VoiceError, AudioError, OSError) as exc:
        print(f"Voice session error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Voice session cancelled", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
