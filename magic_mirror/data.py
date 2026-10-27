"""One background coordinator with independent refresh schedules and safe snapshots."""

import logging
import threading
import time
from datetime import datetime, timezone

from .providers import ProviderUnavailable
from .providers.news import read_news
from .providers.pihole import PiHoleProvider
from .providers.system import read_system
from .providers.weather import read_weather

LOGGER = logging.getLogger(__name__)


class DashboardData:
    def __init__(self, settings, providers=None, clock=time.monotonic):
        self.settings = settings
        pihole = PiHoleProvider()
        self.providers = providers or {
            "system": (read_system, settings.system_interval),
            "weather": (lambda: read_weather(settings), settings.weather_interval),
            "news": (lambda: read_news(settings), settings.news_interval),
            "pihole": (lambda: pihole.read(settings), settings.pihole_interval),
        }
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._next = {name: 0.0 for name in self.providers if name in settings.enabled_modules}
        self._snapshots = {name: {"status": "unavailable", "updated_at": None,
                                  "data": None, "message": "Waiting for first refresh"}
                           for name in self._next}

    def snapshot(self):
        with self._lock:
            return {name: {**entry, "data": entry["data"].copy() if entry["data"] else None}
                    for name, entry in self._snapshots.items()}

    def refresh_one(self, name):
        provider, interval = self.providers[name]
        try:
            data = provider()
            if not isinstance(data, dict):
                raise ValueError("Provider returned invalid data")
        except Exception as exc:
            message = str(exc) if isinstance(exc, ProviderUnavailable) else "Source temporarily unavailable"
            LOGGER.warning("%s refresh failed (%s)", name, type(exc).__name__)
            with self._lock:
                previous = self._snapshots[name]
                self._snapshots[name] = {**previous, "status": "stale" if previous["data"] is not None else "unavailable",
                                         "message": message}
        else:
            with self._lock:
                self._snapshots[name] = {"status": "ok", "updated_at": datetime.now(timezone.utc).isoformat(),
                                         "data": data, "message": ""}
        self._next[name] = self._clock() + interval

    def _run(self):
        while not self._stop.is_set():
            now = self._clock()
            due = [name for name, deadline in self._next.items() if deadline <= now]
            if due:
                for name in due:
                    if self._stop.is_set():
                        break
                    self.refresh_one(name)
            elif self._next:
                self._stop.wait(max(0.0, min(self._next.values()) - now))
            else:
                self._stop.wait()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mirror-data", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=7)
            self._thread = None
