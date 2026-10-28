import threading
import pytest

from magic_mirror import create_app
from magic_mirror.config import Settings
from magic_mirror.data import DashboardData


def test_stale_cache_and_independent_failures():
    healthy = True

    def weather():
        if not healthy:
            raise TimeoutError
        return {"temperature": 20}

    def news():
        raise ConnectionError

    data = DashboardData(Settings(enabled_modules=("weather", "news")),
                         providers={"weather": (weather, 30), "news": (news, 30)})
    data.refresh_one("weather")
    healthy = False
    data.refresh_one("weather")
    data.refresh_one("news")
    snapshots = data.snapshot()
    assert snapshots["weather"]["status"] == "stale"
    assert snapshots["weather"]["data"] == {"temperature": 20}
    assert snapshots["weather"]["updated_at"]
    assert snapshots["news"]["status"] == "unavailable"
    assert snapshots["news"]["data"] is None
    response = create_app(Settings(), data).test_client().get("/api/dashboard")
    assert response.status_code == 200
    assert response.json["modules"] == snapshots
    assert response.content_type == "application/json"
    assert "news_feeds" not in response.json


def test_untrusted_news_is_json_data_only():
    settings = Settings(enabled_modules=("news",), news_feeds=("https://example.com/rss",))
    data = DashboardData(settings, providers={"news": (lambda: {"headlines": [
        {"title": "<script>alert(1)</script>", "publisher": "Example", "published_at": None}]}, 1800)})
    data.refresh_one("news")
    client = create_app(settings, data).test_client()
    assert client.get("/api/dashboard").json["modules"]["news"]["data"]["headlines"][0]["title"] == "<script>alert(1)</script>"
    assert b"<script>alert(1)</script>" not in client.get("/").data


def test_factory_does_not_refresh_and_coordinator_stops():
    called = threading.Event()

    def provider():
        called.set()
        return {"value": 1}

    settings = Settings(enabled_modules=("system",))
    data = DashboardData(settings, providers={"system": (provider, 15)})
    app = create_app(settings, data)
    assert app.test_client().get("/").status_code == 200
    assert not called.is_set()
    data.start()
    assert called.wait(timeout=2)
    assert data.stop()
    assert data._workers == {}
    assert data.snapshot()["system"]["status"] == "ok"


def test_slow_weather_does_not_delay_system_refresh():
    weather_started = threading.Event()
    release_weather = threading.Event()
    system_refreshed = threading.Event()

    def weather():
        weather_started.set()
        assert release_weather.wait(timeout=2)
        return {"temperature": 20}

    def system():
        system_refreshed.set()
        return {"cpu_percent": 15}

    settings = Settings(enabled_modules=("weather", "system"))
    data = DashboardData(settings, providers={"weather": (weather, 30), "system": (system, 15)})
    try:
        assert data.start()
        assert weather_started.wait(timeout=2)
        assert system_refreshed.wait(timeout=2)
        assert data.snapshot()["system"]["status"] == "ok"
        assert data.snapshot()["weather"]["status"] == "unavailable"
    finally:
        release_weather.set()
        assert data.stop()


def test_no_duplicate_refresh_for_one_provider():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def provider():
        calls.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return {"value": 1}

    data = DashboardData(Settings(enabled_modules=("weather",)),
                         providers={"weather": (provider, 0)})
    try:
        assert data.start()
        assert entered.wait(timeout=2)
        assert data.start() is False
        assert len(data._workers) == 1
        assert len(calls) == 1
    finally:
        assert data.stop(grace=0) is False
        release.set()
        assert data.stop(grace=2)
    assert len(calls) == 1


def test_shutdown_retains_active_worker_until_it_exits():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def provider():
        calls.append(1)
        entered.set()
        assert release.wait(timeout=2)
        return {"value": 1}

    data = DashboardData(Settings(enabled_modules=("weather",)),
                         providers={"weather": (provider, 0)})
    assert data.start()
    assert entered.wait(timeout=2)
    try:
        assert data.stop(grace=0) is False
        assert data._workers["weather"].is_alive()
        with pytest.raises(RuntimeError, match="shutting down"):
            data.start()
    finally:
        release.set()
        assert data.stop(grace=2)
    assert data.refresh_one("weather") is False
    assert data._workers == {}
    assert len(calls) == 1


def test_repeated_start_and_stop_are_safe():
    first = threading.Event()
    second = threading.Event()
    calls = []

    def provider():
        calls.append(1)
        (first if len(calls) == 1 else second).set()
        return {"value": len(calls)}

    data = DashboardData(Settings(enabled_modules=("system",)),
                         providers={"system": (provider, 30)})
    assert data.start()
    assert data.start() is False
    assert first.wait(timeout=2)
    assert data.stop()
    assert data.stop()
    assert data.start()
    assert second.wait(timeout=2)
    assert data.stop()
    assert len(calls) == 2


def test_stop_waits_for_active_worker_without_blocking_its_exit():
    entered = threading.Event()
    release = threading.Event()
    outcome = []

    def provider():
        entered.set()
        assert release.wait(timeout=2)
        return {"value": 1}

    data = DashboardData(Settings(enabled_modules=("weather",)),
                         providers={"weather": (provider, 30)})
    assert data.start()
    assert entered.wait(timeout=2)
    stopper = threading.Thread(target=lambda: outcome.append(data.stop(grace=2)))
    stopper.start()
    assert data._stop.wait(timeout=2)
    with pytest.raises(RuntimeError, match="shutting down"):
        data.start()
    release.set()
    stopper.join(timeout=2)
    assert not stopper.is_alive()
    assert outcome == [True]
    assert data._workers == {}
