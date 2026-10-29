"""Explicit, local wake-word service; never started by Flask."""

import argparse
import asyncio
import signal
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from ..audio import AudioError
from ..config import ConfigurationError, Settings
from ..hardware.led import LedError, RGBLedController
from .display import AssistantStateServer, AssistantStateStore
from .wake import WakeController, WakeError, WakeMetrics, WakeState


def led_state_for(state: WakeState) -> str:
    return {WakeState.IDLE: "idle", WakeState.WAKE_DETECTED: "idle",
            WakeState.LISTENING: "listening", WakeState.PROCESSING: "responding",
            WakeState.RESPONDING: "responding"}.get(state, "off")


async def _interactive(settings: Settings) -> None:
    store = AssistantStateStore()
    server = AssistantStateServer(store)
    led = RGBLedController(replace(settings, led_initial_state="off")) if settings.led_enabled else None

    def state_changed(state: WakeState) -> None:
        store.on_state(state)
        if led is not None:
            led.set_state(led_state_for(state))
        print(f"Wake state: {state.value}", flush=True)

    def metrics(value: WakeMetrics) -> None:
        print(f"Idle process CPU: {value.cpu_percent:.1f}%; RSS: {value.rss_mib:.1f} MiB; "
              f"established TCP: {value.established_tcp}; "
              f"frames: {value.audio_frames}; activations: {value.activations}; "
              f"false activations: {value.false_activations}", flush=True)

    controller = WakeController(settings, on_state=state_changed, on_metrics=metrics,
                                on_voice_event=store.on_event, on_playback=store.on_playback)
    loop = asyncio.get_running_loop()
    registered_signals = []
    stdin_registered = False

    def command() -> None:
        line = sys.stdin.readline().strip().lower()
        if line == "q":
            controller.stop()
        elif line == "e":
            controller.end_conversation()

    try:
        await server.start()  # also rejects a second wake owner before GPIO or audio opens
        if led is not None:
            led.open()
        for name in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(name, controller.stop)
            registered_signals.append(name)
        if sys.stdin.isatty():
            loop.add_reader(sys.stdin.fileno(), command)
            stdin_registered = True
        print(f"Local wake detector starting for '{settings.wake_phrase}'. "
              "After detection, wait for the listening cue before asking a question. "
              "Say 'go to sleep' or press e+Enter to end a conversation; "
              "q+Enter or Ctrl+C stops the assistant.", flush=True)
        await controller.run()
    finally:
        if stdin_registered:
            loop.remove_reader(sys.stdin.fileno())
        for name in registered_signals:
            loop.remove_signal_handler(name)
        store.on_state(WakeState.STOPPED)
        try:
            if led is not None:
                led.close()
        finally:
            await server.close()


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
    except (ConfigurationError, WakeError, AudioError, LedError, OSError, RuntimeError) as exc:
        print(f"Wake detector error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Wake detector stopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
