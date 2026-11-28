"""Display-safe, correlated tool snapshots without audio, GPIO, or HTTP."""

import asyncio
from dataclasses import replace

from magic_mirror import create_app
from magic_mirror.assistant.display import AssistantStateServer, AssistantStateStore, read_snapshot
from magic_mirror.assistant.events import ConversationEvent, ConversationState, PlaybackAction, PlaybackEvent
from magic_mirror.assistant.tool_visual import VisualSource, completed, loading
from magic_mirror.assistant.wake import WakeState
from magic_mirror.config import Settings
from magic_mirror.data import DashboardData


def event(kind, *, item=None, response=None, status=None):
    return ConversationEvent(kind, ConversationState.PROCESSING, "session",
                             response_id=response, item_id=item, status=status)


def active_store():
    store = AssistantStateStore()
    store.on_state(WakeState.LISTENING)
    store.on_event(event("input_audio_buffer.speech_started", item="user-1"))
    store.on_event(event("response.created", response="tool-response"))
    return store


def test_contextual_weather_current_and_forecast_are_compact_and_independent():
    data = {"availability": "available", "location": "Auckland, New Zealand",
            "conditions": "Rain", "temperature": {"value": 11, "unit": "°C"},
            "feels_like": {"value": 8, "unit": "°C"},
            "precipitation_probability_percent": 70, "freshness": "stale"}
    visual = completed("get_current_weather", "user-1", "call-1", "tool-response", "success", data)
    assert visual.kind == "weather_current" and visual.status == "completed"
    assert visual.title == "Weather · Auckland, New Zealand"
    assert "11°C" in visual.summary and "70% rain chance" in visual.detail
    assert visual.freshness == "stale"

    forecast = completed("get_weather_forecast", "user-1", "call-2", "tool-response", "success",
                         {"availability": "available", "location": "Auckland", "period": "tonight",
                          "days": [], "window": {"min_temperature": {"value": -3, "unit": "°C"},
                                                  "max_precipitation_probability_percent": 55}})
    assert forecast.kind == "weather_forecast"
    assert "Low -3°C" in forecast.summary and "55% rain chance" in forecast.detail
    weekend = completed("get_weather_forecast", "user-1", "call-3", "tool-response", "success",
                        {"availability": "available", "location": "Auckland", "period": "weekend",
                         "days": [{"date": "2026-11-07", "high": {"value": 18, "unit": "°C"},
                                   "precipitation_probability_percent": 20},
                                  {"date": "2026-11-08", "low": {"value": 4, "unit": "°C"},
                                   "precipitation_probability_percent": 80}]})
    assert "2026-11-07" in weekend.summary and "2026-11-08" in weekend.summary
    assert "80% rain chance" in weekend.detail

    settings = Settings(weather_latitude=40.7, weather_longitude=-74.0,
                        enabled_modules=("weather", "news", "pihole", "system", "assistant"))
    dashboard = DashboardData(settings, providers={})
    app = create_app(settings, dashboard)
    page = app.test_client().get("/")
    assert page.status_code == 200
    for name in settings.enabled_modules:
        assert f'id="module-{name}"'.encode() in page.data
    assert settings.weather_latitude == 40.7 and settings.weather_longitude == -74.0


def test_missing_fields_unavailable_and_bounded_malicious_text():
    visual = completed("get_current_weather", "user-1", "call-1", "r", "success",
                       {"availability": "available", "location": "<img src=x onerror=alert(1)>" * 30,
                        "conditions": None, "temperature": None})
    assert len(visual.title) <= 80 and visual.summary == ""
    assert "<img" in visual.title  # Text data; frontend inserts with textContent.
    unavailable = completed("get_weather_forecast", "user-1", "call-1", "r", "timeout", None)
    assert unavailable.status == "unavailable" and unavailable.detail == "Request timed out"
    ambiguous = completed("get_weather_forecast", "user-1", "call-1", "r", "success",
                          {"availability": "ambiguous"})
    assert ambiguous.detail == "Location needs clarification"


def test_search_summary_source_dates_and_url_validation():
    text = "<script>alert(1)</script>" * 30
    visual = completed("search_web", "user-1", "search-1", "r", "success", {
        "availability": "available", "query": "Latest release", "summary": text,
        "sources": [
            {"title": "<b>Official</b>", "url": "https://www.raspberrypi.com/news/release/",
             "source_name": "www.raspberrypi.com", "publication_date": "2026-11-02"},
            {"title": "Private", "url": "http://127.0.0.1/admin", "source_name": "localhost"},
            {"title": "Other", "url": "https://example.org/other", "source_name": "example.org",
             "publication_date": "not-a-date"},
        ]})
    assert visual.kind == "web_search" and visual.title == "Latest release"
    assert len(visual.summary) == 200
    assert len(visual.sources) == 2
    assert visual.sources[0].title == "<b>Official</b>"
    assert visual.sources[0].publication_date == "2026-11-02"
    assert visual.sources[1].publication_date == ""


def test_loading_completion_new_turn_and_late_call_are_correlated():
    store = active_store()
    load = loading("search_web", "user-1", "call-1", "tool-response")
    store.on_tool_visual(load)
    assert store.snapshot().tool.status == "loading"
    wrong = completed("search_web", "user-1", "other-call", "tool-response", "success",
                      {"availability": "available", "query": "wrong", "summary": "wrong"})
    store.on_tool_visual(wrong)
    assert store.snapshot().tool.status == "loading"
    good = completed("search_web", "user-1", "call-1", "tool-response", "success",
                     {"availability": "available", "query": "current", "summary": "current"})
    store.on_tool_visual(good)
    assert store.snapshot().tool.status == "completed"
    store.on_event(event("input_audio_buffer.speech_started", item="user-2"))
    assert store.snapshot().tool is None
    store.on_tool_visual(good)
    assert store.snapshot().tool is None
    store.on_event(event("response.created", response="new-tool-response"))
    store.on_tool_visual(loading("get_current_weather", "user-2", "call-2", "new-tool-response"))
    store.on_tool_visual(None)  # Manual cancellation clears the processing indicator.
    assert store.snapshot().tool is None
    store.on_state(WakeState.IDLE)
    assert store.snapshot().tool is None


def test_interruption_failure_and_snapshot_size_limit():
    store = active_store()
    store.on_tool_visual(loading("search_web", "user-1", "call-1", "tool-response"))
    store.on_tool_visual(completed("search_web", "user-1", "call-1", "tool-response", "success",
                                   {"availability": "available", "query": "news", "summary": "A [1]."}))
    store.on_event(event("response.created", response="spoken-response"))
    store.on_playback(PlaybackEvent(PlaybackAction.INTERRUPTED, "spoken-response"))
    assert store.snapshot().tool is None
    store.on_event(event("response.created", response="tool-2"))
    store.on_tool_visual(loading("get_current_weather", "user-1", "call-2", "tool-2"))
    store.on_event(event("response.done", response="tool-2", status="failed"))
    assert store.snapshot().tool is None
    oversized = replace(loading("search_web", "user-1", "call-3", "tool-2"), summary="x" * 4000)
    store.on_tool_visual(oversized)
    assert store.snapshot().tool is None
    unsafe = replace(loading("search_web", "user-1", "call-3", "tool-2"),
                     sources=(VisualSource("Local", "http://127.0.0.1/admin", "localhost"),))
    store.on_tool_visual(unsafe)
    assert store.snapshot().tool is None


def test_browser_refresh_reads_same_private_snapshot_and_disconnect_is_safe(tmp_path, monkeypatch):
    async def scenario():
        private = tmp_path / ".private"
        path = private / "assistant.sock"
        store = active_store()
        store.on_tool_visual(loading("get_current_weather", "user-1", "call-1", "tool-response"))
        server = AssistantStateServer(store, path)
        await server.start()
        try:
            first = await asyncio.to_thread(read_snapshot, path)
            second = await asyncio.to_thread(read_snapshot, path)
            assert first == second and first["tool"]["status"] == "loading"
            monkeypatch.setattr("magic_mirror.dashboard.routes.read_snapshot", lambda: read_snapshot(path))
            app = create_app(Settings(wake_enabled=True, enabled_modules=("assistant",)))
            client = app.test_client()
            one = await asyncio.to_thread(client.get, "/api/assistant")
            two = await asyncio.to_thread(client.get, "/api/assistant")
            assert one.status_code == two.status_code == 200
            assert one.json["tool"]["call_id"] == two.json["tool"]["call_id"] == "call-1"
            assert one.headers["Cache-Control"] == "no-store, private"
            denied = await asyncio.to_thread(client.get, "/api/assistant",
                                             environ_overrides={"REMOTE_ADDR": "192.0.2.2"})
            assert denied.status_code == 403
            store.user_text = "x" * 10000
            oversized = await asyncio.to_thread(read_snapshot, path)
            assert oversized["state"] == "error" and "tool" not in oversized
        finally:
            await server.close()
        assert not path.exists()
        disconnected = await asyncio.to_thread(client.get, "/api/assistant")
        assert disconnected.json["state"] == "unavailable"

    asyncio.run(scenario())
