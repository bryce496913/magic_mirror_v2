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
from magic_mirror.providers.weather import dual_temperature, read_weather


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
    weather_data = read_weather(settings)
    assert weather_data["description"] == "Partly cloudy"
    assert weather_data["temperature"] == 22
    assert weather_data["unit"] == "°C"
    assert (weather_data["temperature_c"], weather_data["temperature_f"]) == (22, 71.6)
    assert (weather_data["high_c"], weather_data["high_f"]) == (25, 77)
    assert (weather_data["low_c"], weather_data["low_f"]) == (15, 59)
    assert (weather_data["feels_like_c"], weather_data["feels_like_f"]) == (23, 73.4)
    payload["current"]["temperature_2m"] = "bad"
    with pytest.raises(ProviderUnavailable):
        read_weather(settings)
    monkeypatch.setattr(weather.requests, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout()))
    with pytest.raises(requests.Timeout):
        read_weather(settings)


@pytest.mark.parametrize("value,unit,expected", [
    (11, "celsius", (11, 51.8)),
    (-10, "celsius", (-10, 14)),
    (-40, "celsius", (-40, -40)),
    (52, "fahrenheit", (11.1, 52)),
    (-4, "fahrenheit", (-20, -4)),
    (None, "celsius", (None, None)),
])
def test_dual_temperature_conversion(value, unit, expected):
    assert dual_temperature(value, unit) == expected


def test_fahrenheit_source_retains_original_fields(monkeypatch):
    from magic_mirror.providers import weather
    requests_made = []
    payload = {"current": {"temperature_2m": 52, "apparent_temperature": 41, "weather_code": 0},
               "daily": {"temperature_2m_max": [59, 60], "temperature_2m_min": [23, 24],
                         "weather_code": [0, 2]}}
    def get(*args, **kwargs):
        requests_made.append((args, kwargs))
        return Response(payload)

    monkeypatch.setattr(weather.requests, "get", get)
    result = read_weather(Settings(weather_latitude=10, weather_longitude=20, weather_unit="fahrenheit"))
    assert len(requests_made) == 1
    assert result["temperature"] == 52
    assert result["unit"] == "°F"
    assert (result["temperature_c"], result["temperature_f"]) == (11.1, 52)
    assert (result["low_c"], result["low_f"]) == (-5, 23)
    assert (result["feels_like_c"], result["feels_like_f"]) == (5, 41)


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
    assert all(kwargs["verify"] is True for _, kwargs in calls)
    assert all("test-only-password" not in url for url, _ in calls)
    monkeypatch.setattr(pihole.requests, "post", lambda *_args, **_kwargs: Response(status=401))
    with pytest.raises(ProviderUnavailable, match="authentication failed"):
        PiHoleProvider().read(settings)


def test_pihole_connection_failure(monkeypatch):
    from magic_mirror.providers import pihole
    monkeypatch.setattr(pihole.requests, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError()))
    with pytest.raises(requests.ConnectionError):
        PiHoleProvider().read(Settings())


def test_pihole_custom_ca_verifies_auth_and_both_reads(monkeypatch, tmp_path):
    from magic_mirror.providers import pihole
    password_file = tmp_path / "password"
    password_file.write_text("test-only-password")
    ca_cert = tmp_path / "local-ca.crt"
    ca_cert.write_text("test fixture only")
    settings = Settings.from_env({"MIRROR_PIHOLE_URL": "https://pi.hole",
                                  "MIRROR_PIHOLE_PASSWORD_FILE": str(password_file),
                                  "MIRROR_PIHOLE_CA_CERT": str(ca_cert)})
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
    assert PiHoleProvider().read(settings)["blocking_enabled"] is True
    assert [url for url, _ in calls] == ["https://pi.hole/api/auth",
                                        "https://pi.hole/api/stats/summary",
                                        "https://pi.hole/api/dns/blocking"]
    assert all(kwargs["verify"] == str(ca_cert) for _, kwargs in calls)
    assert all(kwargs["timeout"] == (3, 5) for _, kwargs in calls)


def test_pihole_certificate_failure_is_not_bypassed(monkeypatch, tmp_path):
    from magic_mirror.providers import pihole
    ca_cert = tmp_path / "local-ca.crt"
    ca_cert.write_text("test fixture only")
    def get(_url, **kwargs):
        assert kwargs["verify"] == str(ca_cert)
        raise requests.exceptions.SSLError("certificate verification failed")

    monkeypatch.setattr(pihole.requests, "get", get)
    with pytest.raises(requests.exceptions.SSLError, match="certificate verification failed"):
        PiHoleProvider().read(Settings(pihole_url="https://pi.hole", pihole_ca_cert=str(ca_cert)))


def test_pihole_ca_does_not_change_weather_or_news_trust(monkeypatch, tmp_path):
    from magic_mirror.providers import pihole, weather, news
    ca_cert = tmp_path / "local-ca.crt"
    ca_cert.write_text("test fixture only")
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/stats/summary"):
            return Response({"queries": {"total": 1, "blocked": 0}})
        if url.endswith("/api/dns/blocking"):
            return Response({"blocking": "enabled"})
        if url == weather.API_URL:
            return Response({"current": {"temperature_2m": 11, "weather_code": 0},
                             "daily": {"temperature_2m_max": [12, 13],
                                       "temperature_2m_min": [5, 6], "weather_code": [0, 0]}})
        return Response(content=b"<rss><channel><title>Test</title><item><title>One</title></item></channel></rss>")

    monkeypatch.setattr(pihole.requests, "get", get)
    PiHoleProvider().read(Settings(pihole_url="https://pi.hole", pihole_ca_cert=str(ca_cert)))
    read_weather(Settings(weather_latitude=10, weather_longitude=20))
    read_news(Settings(news_feeds=("https://example.com/rss",)))
    assert [kwargs.get("verify") for _, kwargs in calls] == [str(ca_cert), str(ca_cert), None, None]
