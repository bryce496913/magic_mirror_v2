"""Run the application manually with python -m magic_mirror."""

import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from .app import create_app
from .config import ConfigurationError, Settings, configure_logging
from .data import DashboardData


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
    app = create_app(settings, data)
    # Werkzeug handles Ctrl+C itself; make a manual SIGTERM follow the same path.
    def stop_server(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_server)
    logger.info("Starting Magic Mirror V2 on %s:%s", settings.host, settings.port)
    try:
        data.start()
        app.run(host=settings.host, port=settings.port, debug=False, use_reloader=False)
    except OSError as exc:
        logger.error("Unable to start server: %s", exc)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        data.stop()
        logger.info("Magic Mirror V2 stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
