import threading
import time

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
    data.stop()
    assert data._thread is None
    assert data.snapshot()["system"]["status"] == "ok"
