"""Fixture-only voice weather tests; no provider or audio hardware required."""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from magic_mirror.assistant.tools.weather import (
    FORECAST_URL, GEOCODE_URL, WeatherRateLimited, WeatherTools, WeatherUnavailable,
    _fetch_json, _temperature,
)
from magic_mirror.assistant.tools.registry import ToolDispatcher, ToolStatus, default_registry
from magic_mirror.config import Settings


def instant(zone="America/New_York", day=2, hour=9):
    return datetime(2026, 11, day, hour, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)


def fixture(zone="America/New_York", now=None):
    now = now or instant(zone)
    local = now.astimezone(ZoneInfo(zone))
    first_day = local.date()
    daily = {"time": [], "temperature_2m_max": [], "temperature_2m_min": [],
             "weather_code": [], "precipitation_probability_max": []}
    for i in range(8):
        day = first_day + timedelta(days=i)
        daily["time"].append(int(datetime(day.year, day.month, day.day,
                                          tzinfo=ZoneInfo(zone)).timestamp()))
        daily["temperature_2m_max"].append(15 + i)
        daily["temperature_2m_min"].append(2 + i)
        daily["weather_code"].append(61 if i else 0)
        daily["precipitation_probability_max"].append(80 if i else 20)
    hourly = {"time": [], "temperature_2m": [], "precipitation_probability": [], "weather_code": []}
    start = datetime(first_day.year, first_day.month, first_day.day,
                     tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    for i in range(193):
        stamp = start + timedelta(hours=i)
        hourly["time"].append(int(stamp.timestamp()))
        hourly["temperature_2m"].append(8 if stamp.astimezone(ZoneInfo(zone)).hour >= 18 else 13)
        hourly["precipitation_probability"].append(70 if stamp.astimezone(ZoneInfo(zone)).hour >= 12 else 10)
        hourly["weather_code"].append(61)
    observed = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
    return {"timezone": zone, "current": {"time": observed, "temperature_2m": 12,
            "apparent_temperature": 10, "relative_humidity_2m": 65,
            "weather_code": 61, "wind_speed_10m": 12, "wind_direction_10m": 225},
            "daily": daily, "hourly": hourly}


def service(*, zone="America/New_York", now=None, unit="celsius", location=None):
    now = now or instant(zone)
    calls = []
    payload = fixture(zone, now)
    locations = [{"name": "Auckland", "admin1": "Auckland", "country": "New Zealand",
                  "country_code": "NZ", "latitude": -36.85, "longitude": 174.76,
                  "population": 417910, "timezone": "Pacific/Auckland"},
                 {"name": "Auckland", "country": "United States", "latitude": 37.8,
                  "longitude": -122.2, "population": 5000, "timezone": "America/Los_Angeles"}]

    async def fetch(url, params):
        calls.append((url, params))
        return {"results": locations} if url == GEOCODE_URL else payload

    settings = Settings(weather_latitude=40.7, weather_longitude=-74,
                        weather_location_name="New York", weather_unit=unit,
                        tools_enabled=True)
    weather = WeatherTools(settings, fetch_json=fetch, now=lambda: now)
    return weather, calls, payload, locations


def run(coro):
    return asyncio.run(coro)


def test_current_conditions_default_location_and_units():
    weather, calls, _, _ = service()
    result = run(weather.current({}))
    assert result["availability"] == "available"
    assert result["kind"] == "current_model_estimate"
    assert (result["location"], result["timezone"], result["provider"]) == (
        "New York", "America/New_York", "Open-Meteo")
    assert result["temperature"] == {"fahrenheit": 53.6, "celsius": 12.0,
                                     "value": 12.0, "unit": "°C"}
    assert result["feels_like"] == {"fahrenheit": 50.0, "celsius": 10.0,
                                    "value": 10.0, "unit": "°C"}
    assert result["humidity_percent"] == 65
    assert result["wind_speed"] == 12
    assert result["wind_direction_degrees"] == 225
    assert result["precipitation_probability_percent"] == 10
    assert len(calls) == 1 and calls[0][0] == FORECAST_URL

    fahrenheit, calls_f, _, _ = service(unit="fahrenheit")
    result_f = run(fahrenheit.current({}))
    assert result_f["temperature"] == {"fahrenheit": 12.0, "celsius": -11.1,
                                       "value": 12.0, "unit": "°F"}
    assert result_f["feels_like"] == {"fahrenheit": 10.0, "celsius": -12.2,
                                      "value": 10.0, "unit": "°F"}
    assert result_f["wind_unit"] == "mph"
    assert len(calls_f) == 1
    assert calls_f[0][1]["temperature_unit"] == "fahrenheit"


@pytest.mark.parametrize("value,unit,expected_f,expected_c", [
    (20, "celsius", 68.0, 20.0),
    (11.1, "celsius", 52.0, 11.1),
    (0, "celsius", 32.0, 0.0),
    (-40, "celsius", -40.0, -40.0),
    (68, "fahrenheit", 68.0, 20.0),
    (32, "fahrenheit", 32.0, 0.0),
    (-4, "fahrenheit", -4.0, -20.0),
])
def test_temperature_conversion_uses_source_unit_once(value, unit, expected_f, expected_c):
    paired = _temperature(value, unit)
    assert paired["fahrenheit"] == expected_f
    assert paired["celsius"] == expected_c
    assert paired["value"] == (expected_f if unit == "fahrenheit" else expected_c)
    assert list(paired)[:2] == ["fahrenheit", "celsius"]


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf")])
def test_missing_or_invalid_temperature_never_gains_a_conversion(value):
    assert _temperature(value, "celsius") is None


@pytest.mark.parametrize("period,expected,low_f,low_c,high_f", [
    ("today", ["2026-11-02"], 35.6, 2, 59.0),
    ("tomorrow", ["2026-11-03"], 37.4, 3, 60.8),
    ("weekend", ["2026-11-07", "2026-11-08"], 46.4, 8, 68.0),
])
def test_daily_forecast_periods(period, expected, low_f, low_c, high_f):
    weather, _, _, _ = service()
    result = run(weather.forecast({"period": period}))
    assert result["kind"] == "forecast"
    assert [day["date"] for day in result["days"]] == expected
    assert result["days"][-1]["precipitation_probability_percent"] == (20 if period == "today" else 80)
    assert result["days"][-1]["low"] == {"fahrenheit": low_f, "celsius": low_c,
                                          "value": low_c, "unit": "°C"}
    assert result["days"][0]["high"]["fahrenheit"] == high_f


def test_hourly_afternoon_rain_and_tonight_low():
    weather, _, _, _ = service()
    afternoon = run(weather.forecast({"period": "afternoon"}))
    tonight = run(weather.forecast({"period": "tonight"}))
    assert afternoon["days"] == [] and afternoon["window"]["max_precipitation_probability_percent"] == 70
    assert afternoon["window"]["from"].startswith("2026-11-02T12:00")
    assert tonight["days"] == [] and tonight["window"]["min_temperature"] == {
        "fahrenheit": 46.4, "celsius": 8.0, "value": 8.0, "unit": "°C"}
    assert tonight["window"]["until"].startswith("2026-11-03T05:00")


def test_fahrenheit_source_pairs_forecast_high_low_and_hourly_minimum():
    weather, calls, _, _ = service(unit="fahrenheit")
    tomorrow = run(weather.forecast({"period": "tomorrow"}))
    day = tomorrow["days"][0]
    assert day["high"] == {"fahrenheit": 16.0, "celsius": -8.9,
                           "value": 16.0, "unit": "°F"}
    assert day["low"] == {"fahrenheit": 3.0, "celsius": -16.1,
                          "value": 3.0, "unit": "°F"}
    tonight = run(weather.forecast({"period": "tonight"}))
    assert tonight["window"]["min_temperature"] == {
        "fahrenheit": 8.0, "celsius": -13.3, "value": 8.0, "unit": "°F"}
    assert len(calls) == 1  # forecast windows share the same cached source response


def test_overseas_location_and_ambiguous_location():
    weather, calls, payload, places = service(zone="Pacific/Auckland", now=instant("Pacific/Auckland"))
    result = run(weather.forecast({"location": "Auckland", "period": "tomorrow"}))
    assert result["location"] == "Auckland, Auckland, New Zealand"
    assert result["timezone"] == "Pacific/Auckland"
    assert result["days"][0]["high"]["fahrenheit"] == 60.8
    assert result["days"][0]["high"]["celsius"] == 16.0
    assert len(calls) == 2
    assert calls[0][1]["name"] == "Auckland"
    assert calls[1][1]["latitude"] == -36.85

    places[0]["population"] = 10000
    second, _, _, _ = service()
    second.fetch_json = weather.fetch_json
    second._places.clear()
    ambiguous = run(second.current({"location": "Auckland"}))
    assert ambiguous["availability"] == "ambiguous"
    assert len(ambiguous["candidates"]) == 2
    qualified = run(second.current({"location": "Auckland, NZ"}))
    assert qualified["availability"] == "available"


def test_local_midnight_and_daylight_saving_fold():
    auckland = instant("Pacific/Auckland", day=2, hour=0)
    weather, _, _, _ = service(zone="Pacific/Auckland", now=auckland)
    assert auckland.date().isoformat() == "2026-11-01"
    assert run(weather.forecast({"period": "tomorrow"}))["days"][0]["date"] == "2026-11-03"
    # On the US fall-back night, the second 01:30 has a different UTC instant.
    folded = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1)
    eastern, _, _, _ = service(now=folded.astimezone(timezone.utc))
    overnight = run(eastern.forecast({"period": "tonight"}))
    assert overnight["window"]["from"].startswith("2026-11-01T01:00:00-05:00")
    assert overnight["window"]["until"].startswith("2026-11-01T05:00")


@pytest.mark.parametrize("location", ["https://internal/", "localhost:5000", "../etc/passwd", "\nParis", "A", "_"])
def test_invalid_location_never_calls_network(location):
    weather, calls, _, _ = service()
    assert run(weather.current({"location": location}))["reason"] == "invalid_location"
    assert calls == []


def test_missing_forecast_fields_and_current_fields_are_not_invented():
    weather, _, payload, _ = service()
    payload["daily"].pop("temperature_2m_min")
    payload["current"].pop("relative_humidity_2m")
    result = run(weather.forecast({"period": "tomorrow"}))
    assert result["days"][0]["low"] is None
    assert run(weather.current({}))["humidity_percent"] is None
    payload["current"]["temperature_2m"] = None
    payload["current"]["apparent_temperature"] = None
    current = run(weather.current({}))
    assert current["temperature"] is None and current["feels_like"] is None
    payload["daily"].pop("time")
    weather._cache.clear()
    assert run(weather.forecast({"period": "tomorrow"}))["reason"] == "forecast_data_missing"


@pytest.mark.parametrize("unit", ["celsius", "fahrenheit"])
def test_seven_day_paired_temperatures_fit_tool_output_limit(unit):
    weather, calls, _, _ = service(unit=unit)
    registry = default_registry(weather.settings)
    spec = registry.get("get_weather_forecast")
    registered_weather = spec.executor.__self__
    registered_weather.fetch_json = weather.fetch_json
    registered_weather.now = weather.now
    result = run(ToolDispatcher(registry, weather.settings).execute(
        "get_weather_forecast", '{"period":"next_7_days"}'))
    assert result.status == ToolStatus.SUCCESS
    assert len(result.json().encode("utf-8")) <= spec.max_output_bytes
    assert len(result.data["days"]) == 7
    assert all(set(day["high"]) >= {"fahrenheit", "celsius"} and
               set(day["low"]) >= {"fahrenheit", "celsius"} for day in result.data["days"])
    assert len(calls) == 1 and calls[0][0] == FORECAST_URL


def test_cache_reuse_failure_stale_and_expiration():
    weather, calls, _, _ = service()
    elapsed = [1000.0]
    weather.clock = lambda: elapsed[0]
    first = run(weather.current({}))
    assert first["freshness"] == "fresh"
    elapsed[0] += 20
    assert run(weather.forecast({"period": "tomorrow"}))["freshness"] == "fresh"
    assert len(calls) == 1

    async def outage(_url, _params):
        raise WeatherUnavailable

    weather.fetch_json = outage
    elapsed[0] += 601
    assert run(weather.current({}))["freshness"] == "stale"
    elapsed[0] += 2200
    assert run(weather.current({}))["reason"] == "provider_unavailable"


@pytest.mark.parametrize("error,reason", [(WeatherRateLimited, "provider_rate_limited"),
                                           (WeatherUnavailable, "provider_unavailable")])
def test_rate_limit_and_provider_outage(error, reason):
    weather, _, _, _ = service()

    async def broken(_url, _params):
        raise error

    weather.fetch_json = broken
    assert run(weather.current({}))["reason"] == reason


def test_timeout_and_idle_registry_make_no_provider_call(monkeypatch):
    async def scenario():
        weather, calls, _, _ = service()

        async def timeout(_url, _params):
            raise httpx.ReadTimeout("mock timeout")

        weather.fetch_json = timeout
        assert (await weather.current({}))["reason"] == "provider_unavailable"
        assert calls == []

    asyncio.run(scenario())

    def unexpected(*_args, **_kwargs):
        raise AssertionError("No network should open while waiting for wake")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected)
    assert len(default_registry(Settings(tools_enabled=True)).declarations()) == 3


def test_cancellation_releases_request_and_cannot_publish_late_result():
    async def scenario():
        weather, _, payload, _ = service()
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(_url, _params):
            entered.set()
            await release.wait()
            return payload

        weather.fetch_json = slow
        task = asyncio.create_task(weather.current({}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert weather._cache == {}

    asyncio.run(scenario())


def test_http_redirect_size_and_rate_limit(monkeypatch):
    async def scenario():
        status = [302]

        def handler(request):
            assert request.url.host == "api.open-meteo.com"
            if status[0] == 302:
                return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})
            if status[0] == 200:
                return httpx.Response(200, content=b"x" * (256 * 1024 + 1))
            return httpx.Response(429)

        real_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
        with pytest.raises(WeatherUnavailable):
            await _fetch_json(FORECAST_URL, {"latitude": 1})
        status[0] = 200
        with pytest.raises(WeatherUnavailable):
            await _fetch_json(FORECAST_URL, {"latitude": 1})
        status[0] = 429
        with pytest.raises(WeatherRateLimited):
            await _fetch_json(FORECAST_URL, {"latitude": 1})
        with pytest.raises(WeatherUnavailable):
            await _fetch_json("http://127.0.0.1/private", {})

    asyncio.run(scenario())
