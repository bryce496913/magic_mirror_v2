"""Bounded per-provider workers with independent schedules and safe snapshots."""

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
DATA_MODULES = frozenset({"system", "weather", "news", "pihole"})


class DashboardData:
    def __init__(self, settings, providers=None):
        self.settings = settings
        pihole = PiHoleProvider()
        self.providers = providers if providers is not None else {
            "system": (read_system, settings.system_interval),
            "weather": (lambda: read_weather(settings), settings.weather_interval),
            "news": (lambda: read_news(settings), settings.news_interval),
            "pihole": (lambda: pihole.read(settings), settings.pihole_interval),
        }
        self._lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stop = threading.Event()
        self._stopping = False
        self._workers = {}
        self._provider_locks = {name: threading.Lock() for name in self.providers
                                if name in settings.enabled_modules and name in DATA_MODULES}
        self._next = {name: 0.0 for name in self._provider_locks}
        self._snapshots = {name: {"status": "unavailable", "updated_at": None,
                                  "data": None, "message": "Waiting for first refresh"}
                           for name in self._next}

    def snapshot(self):
        with self._lock:
            return {name: {**entry, "data": entry["data"].copy() if entry["data"] else None}
                    for name, entry in self._snapshots.items()}

    def refresh_one(self, name):
        provider, interval = self.providers[name]
        with self._provider_locks[name]:
            # The launch decision is serialized with stop(). A call accepted here
            # was in flight before shutdown, even if its I/O finishes afterward.
            with self._lifecycle:
                if self._stop.is_set():
                    return False
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
            self._next[name] = time.monotonic() + interval
            return True

    def _run_provider(self, name):
        while not self._stop.is_set():
            if not self.refresh_one(name):
                return
            self._stop.wait(max(0.0, self._next[name] - time.monotonic()))

    def start(self):
        with self._lifecycle:
            if self._stopping:
                raise RuntimeError("Data workers are still shutting down")
            if any(worker.is_alive() for worker in self._workers.values()):
                if self._stop.is_set():
                    raise RuntimeError("Data workers are still shutting down")
                return False
            self._workers.clear()
            self._stop.clear()
            for name in self._provider_locks:
                worker = threading.Thread(target=self._run_provider, args=(name,),
                                          name=f"mirror-data-{name}", daemon=True)
                self._workers[name] = worker
                worker.start()
            return True

    def stop(self, grace=7.0):
        """Signal all workers; return False and retain handles if any outlive grace."""
        with self._stop_lock:
            with self._lifecycle:
                self._stopping = True
                self._stop.set()
                workers = tuple(self._workers.values())
            deadline = time.monotonic() + max(0.0, grace)
            for worker in workers:
                if worker is not threading.current_thread():
                    worker.join(timeout=max(0.0, deadline - time.monotonic()))
            with self._lifecycle:
                self._workers = {name: worker for name, worker in self._workers.items()
                                 if worker.is_alive()}
                self._stopping = False
                return not self._workers
