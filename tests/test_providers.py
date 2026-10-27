"""Provider tests use only local fixtures and mocked read-only interfaces."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from magic_mirror.config import Settings
from magic_mirror.providers import ProviderUnavailable
from magic_mirror.providers.news import read_news
from magic_mirror.providers.pihole import PiHoleProvider
from magic_mirror.providers.system import read_system
from magic_mirror.providers.weather import read_weather


class Response:
    def __init__(self, payload=None, content=b"", status=200):
        self.payload, self.content, self.status_code = payload, content, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, _size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_system_without_temperature(monkeypatch):
    from magic_mirror.providers import system
    monkeypatch.setattr(system.psutil, "sensors_temperatures", lambda: {})
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(system.psutil, "cpu_percent", lambda interval: 12.5)
    monkeypatch.setattr(system.psutil, "virtual_memory", lambda: SimpleNamespace(percent=25, used=2 * 1024**3, total=8 * 1024**3))
    monkeypatch.setattr(system.psutil, "disk_usage", lambda _path: SimpleNamespace(percent=30))
    monkeypatch.setattr(system.psutil, "boot_time", lambda: 100)
    monkeypatch.setattr(system.time, "time", lambda: 3700)
    data = read_system()
    assert data["cpu_percent"] == 12.5
    assert data["cpu_temperature_c"] is None
    assert data["ram_used_gb"] == 2
    assert data["disk_percent"] == 30
    assert data["uptime_seconds"] == 3600


def test_weather_success_and_invalid_data(monkeypatch):
    from magic_mirror.providers import weather
    settings = Settings(weather_latitude=10, weather_longitude=20)
    payload = {"current": {"temperature_2m": 22, "apparent_temperature": 23, "weather_code": 2},
               "daily": {"temperature_2m_max": [25, 26], "temperature_2m_min": [15, 16],
                         "weather_code": [2, 3], "precipitation_probability_max": [20, 10]}}
    monkeypatch.setattr(weather.requests, "get", lambda *_args, **_kwargs: Response(payload))
    assert read_weather(settings)["description"] == "Partly cloudy"
    payload["current"]["temperature_2m"] = "bad"
    with pytest.raises(ProviderUnavailable):
        read_weather(settings)
    monkeypatch.setattr(weather.requests, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout()))
    with pytest.raises(requests.Timeout):
        read_weather(settings)


def test_news_deduplicates_and_ignores_malformed_feed(monkeypatch):
    from magic_mirror.providers import news
    good = b'<rss><channel><title>Example</title><item><title>One</title><pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate></item><item><title>one</title></item></channel></rss>'
    feeds = iter([Response(content=b'<rss><'), Response(content=good)])
    monkeypatch.setattr(news.requests, "get", lambda *_args, **_kwargs: next(feeds))
    items = read_news(Settings(news_feeds=("https://a.example/rss", "https://b.example/rss")))["headlines"]
    assert len(items) == 1
    assert items[0]["publisher"] == "Example"
    assert items[0]["published_at"] is not None


def test_news_timeout_is_unavailable(monkeypatch):
    from magic_mirror.providers import news
    monkeypatch.setattr(news.requests, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout()))
    with pytest.raises(ProviderUnavailable):
        read_news(Settings(news_feeds=("https://a.example/rss",)))


def test_pihole_v6_success_and_auth_failure(monkeypatch, tmp_path):
    from magic_mirror.providers import pihole
    password_file = tmp_path / "pihole-password"
    password_file.write_text("test-only-password\n")
    settings = Settings(pihole_password_file=str(password_file))
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"session": {"valid": True, "sid": "test-sid", "validity": 300}})

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/summary"):
            return Response({"queries": {"total": 100, "blocked": 25}})
        return Response({"blocking": "enabled"})

    monkeypatch.setattr(pihole.requests, "post", post)
    monkeypatch.setattr(pihole.requests, "get", get)
    data = PiHoleProvider().read(settings)
    assert data == {"blocking_enabled": True, "queries_total": 100,
                    "queries_blocked": 25, "blocked_percent": 25.0}
    assert calls[0][1]["json"] == {"password": "test-only-password"}
    assert calls[1][1]["headers"] == {"X-FTL-SID": "test-sid"}
    assert all("test-only-password" not in url for url, _ in calls)
    monkeypatch.setattr(pihole.requests, "post", lambda *_args, **_kwargs: Response(status=401))
    with pytest.raises(ProviderUnavailable, match="authentication failed"):
        PiHoleProvider().read(settings)


def test_pihole_connection_failure(monkeypatch):
    from magic_mirror.providers import pihole
    monkeypatch.setattr(pihole.requests, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError()))
    with pytest.raises(requests.ConnectionError):
        PiHoleProvider().read(Settings())
