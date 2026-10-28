"""Explicit, standalone Raspberry Pi LED exercise; Flask is never started."""

import argparse
import math
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from magic_mirror.config import ConfigurationError, Settings
from .led import LedError, RGBLedController


def run(settings, duration=1.0, channel=None, sleep=time.sleep, controller_factory=RGBLedController):
    controller = controller_factory(settings)
    print(f"BCM GPIO pins: red={controller.pins[0]}, green={controller.pins[1]}, "
          f"blue={controller.pins[2]} (active_high={controller.active_high})")
    with controller:
        if channel:
            controller.show_channel(channel)
            print(f"Testing {channel}")
            sleep(duration)
        else:
            for state in ("idle", "listening", "responding", "off"):
                controller.set_state(state)
                print(f"Testing {state}")
                sleep(duration)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Exercise Magic Mirror RGB LED without Flask")
    parser.add_argument("--channel", choices=("red", "green", "blue"), help="Test one channel instead of the state cycle")
    parser.add_argument("--duration", type=float, default=1.0, help="Seconds per state or channel (0.1–60)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or not 0.1 <= args.duration <= 60:
        parser.error("--duration must be between 0.1 and 60 seconds")
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    try:
        run(Settings.from_env(), duration=args.duration, channel=args.channel)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except LedError as exc:
        print(f"LED error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("LED test interrupted; GPIO released", file=sys.stderr)
        return 130
    print("LED test complete; GPIO released")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
