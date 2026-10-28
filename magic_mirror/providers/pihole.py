"""Read-only Pi-hole v6 API client. No passwords or SIDs leave this process."""

import time
from pathlib import Path

import requests

from . import ProviderUnavailable


class PiHoleProvider:
    def __init__(self):
        self._sid = None
        self._expires = 0.0

    def _authenticate(self, settings):
        if not settings.pihole_password_file:
            return None
        try:
            password = Path(settings.pihole_password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProviderUnavailable("Pi-hole authentication unavailable") from exc
        if not password:
            raise ProviderUnavailable("Pi-hole authentication unavailable")
        response = requests.post(settings.pihole_url + "/api/auth", json={"password": password},
                                 timeout=(3, 5), verify=settings.pihole_ca_cert or True)
        if response.status_code in {401, 403}:
            raise ProviderUnavailable("Pi-hole authentication failed")
        response.raise_for_status()
        try:
            session = response.json()["session"]
            if not session["valid"] or not session["sid"]:
                raise ValueError
            self._sid = session["sid"]
            self._expires = time.monotonic() + max(0, int(session.get("validity", 300)) - 30)
            return self._sid
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderUnavailable("Pi-hole authentication failed") from exc

    def read(self, settings):
        if self._sid and time.monotonic() >= self._expires:
            self._sid = None
        if settings.pihole_password_file and not self._sid:
            self._authenticate(settings)
        headers = {"X-FTL-SID": self._sid} if self._sid else {}
        summary = requests.get(settings.pihole_url + "/api/stats/summary", headers=headers,
                               timeout=(3, 5), verify=settings.pihole_ca_cert or True)
        if summary.status_code in {401, 403}:
            self._sid = None
            raise ProviderUnavailable("Pi-hole authentication required")
        summary.raise_for_status()
        blocking = requests.get(settings.pihole_url + "/api/dns/blocking", headers=headers,
                                timeout=(3, 5), verify=settings.pihole_ca_cert or True)
        if blocking.status_code in {401, 403}:
            self._sid = None
            raise ProviderUnavailable("Pi-hole authentication required")
        blocking.raise_for_status()
        try:
            queries = summary.json()["queries"]
            blocking_state = blocking.json()["blocking"]
            total, blocked = int(queries["total"]), int(queries["blocked"])
            if blocking_state not in {"enabled", "disabled"} or total < 0 or blocked < 0 or blocked > total:
                raise ValueError
            return {"blocking_enabled": blocking_state == "enabled", "queries_total": total,
                    "queries_blocked": blocked,
                    "blocked_percent": round(100 * blocked / total, 1) if total else 0.0}
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderUnavailable("Pi-hole data is invalid") from exc
