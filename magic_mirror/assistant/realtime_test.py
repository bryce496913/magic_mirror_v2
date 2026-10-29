"""Hardware-free, opt-in Realtime connection validation CLI."""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from ..config import ConfigurationError, Settings
from .client import RealtimeClient, RealtimeError


async def _connect(settings: Settings) -> None:
    async with RealtimeClient(settings) as client:
        print(f"Session acknowledged: {client.session_id}")
        print(f"Model requested: {settings.realtime_model}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Realtime settings; offline unless --connect is given")
    parser.add_argument("--connect", action="store_true", help="explicitly open and close one live OpenAI session")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    print(f"Model: {settings.realtime_model}")
    print(f"API key configured: {'yes' if settings.openai_api_key else 'no'}")
    print(f"Limits: session={settings.realtime_max_session_seconds}s idle={settings.realtime_idle_seconds}s response={settings.realtime_max_response_seconds}s")
    print(f"Connection: timeout={settings.realtime_connect_timeout}s retries={settings.realtime_retry_limit} backoff={settings.realtime_backoff_seconds}s")
    print("Session settings: valid")
    if not args.connect:
        print("Offline check complete; no connection opened")
        return 0
    try:
        asyncio.run(_connect(settings))
    except (RealtimeError, KeyboardInterrupt) as exc:
        print(f"Realtime validation failed: {exc or 'interrupted'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
