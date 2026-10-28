"""Run the application manually with python -m magic_mirror."""

import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from .app import create_app
from .config import ConfigurationError, Settings, configure_logging
from .data import DashboardData
from .hardware.led import LedError, RGBLedController


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)
    logger = logging.getLogger("magic_mirror")
    data = DashboardData(settings)
    led = RGBLedController(settings) if settings.led_enabled else None
    app = create_app(settings, data, led=led)
    # Werkzeug handles Ctrl+C itself; make a manual SIGTERM follow the same path.
    def stop_server(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_server)
    logger.info("Starting Magic Mirror V2 on %s:%s", settings.host, settings.port)
    exit_code = 0
    try:
        if led is not None:
            led.open()
        data.start()
        app.run(host=settings.host, port=settings.port, debug=False, use_reloader=False)
    except LedError as exc:
        logger.error("LED startup failed: %s", exc)
        exit_code = 1
    except OSError as exc:
        logger.error("Unable to start server: %s", exc)
        exit_code = 1
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if not data.stop():
                logger.warning("Dashboard data workers still finishing bounded requests at process exit")
        finally:
            if led is not None:
                try:
                    led.close()
                except LedError as exc:
                    logger.error("LED cleanup failed: %s", exc)
                    exit_code = 1
        logger.info("Magic Mirror V2 stopped")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
