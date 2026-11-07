"""Bounded, read-only Open-Meteo queries for a live voice session.

The dashboard's two-day provider remains independent; only its weather-code
descriptions and configured coordinates/units are shared here.
"""

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ...config import Settings
from ...providers.weather import API_URL as FORECAST_URL, DESCRIPTIONS, dual_temperature

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
MAX_BODY = 256 * 1024
MAX_CACHE = 16
STALE_GRACE_SECONDS = 1800
LOCATION = re.compile(r"[\w\s.,'’\-]{2,80}\Z", re.UNICODE)
PERIODS = ("today", "tonight", "tomorrow", "afternoon", "weekend", "next_7_days")
# Every alias is pinned to its actual country and region. Never fuzzy-match
# arbitrary geocoder results or let a high-population namesake override it.
CITY_ALIASES = {
    "new york city": ("New York", "US", "New York"),
    "nyc": ("New York", "US", "New York"),
}


def _location_key(value: str) -> str:
    return " ".join(value.casefold().replace("’", "'").split())


def _qualifier_matches(item: dict[str, Any], qualifier: str) -> bool:
    key = _location_key(qualifier)
    country = _location_key(item.get("country") or "") if isinstance(item.get("country"), str) else ""
    code = _location_key(item.get("country_code") or "") if isinstance(item.get("country_code"), str) else ""
    region = _location_key(item.get("admin1") or "") if isinstance(item.get("admin1"), str) else ""
    if key in {"usa", "u.s.", "u.s.a.", "united states of america"}:
        return code == "us" or country == "united states"
    if key == "ny":
        return region == "new york" and (code == "us" or country == "united states")
    return key in {country, code, region}


class WeatherUnavailable(Exception):
    """Expected provider or response failure; details must not reach the model."""


class WeatherRateLimited(WeatherUnavailable):
    """The upstream API rejected the request for exceeding its limit."""


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    timezone: str | None = None


@dataclass(frozen=True)
class CacheEntry:
    place: Place
    payload: dict[str, Any]
    fetched_at: float
    retrieved_at: str


async def _fetch_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call only fixed HTTPS endpoints; cap time, redirects, and response bytes."""
    if url not in (FORECAST_URL, GEOCODE_URL):
        raise WeatherUnavailable
    for attempt in range(2):
        try:
            async with asyncio.timeout(5):
                async with httpx.AsyncClient(timeout=httpx.Timeout(3, connect=1.5),
                                             follow_redirects=False) as client:
                    async with client.stream("GET", url, params=params) as response:
                        if response.status_code == 429:
                            raise WeatherRateLimited
                        if response.status_code in (500, 502, 503, 504) and attempt == 0:
                            await asyncio.sleep(0.2)
                            continue
                        if response.status_code != 200:
                            raise WeatherUnavailable
                        if int(response.headers.get("Content-Length", "0")) > MAX_BODY:
                            raise WeatherUnavailable
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            body.extend(chunk)
                            if len(body) > MAX_BODY:
                                raise WeatherUnavailable
                        value = json.loads(body)
                        if not isinstance(value, dict) or value.get("error"):
                            raise WeatherUnavailable
                        return value
        except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == 0:
                await asyncio.sleep(0.2)
                continue
            raise WeatherUnavailable from exc
        except (ValueError, httpx.RequestError) as exc:
            raise WeatherUnavailable from exc
    raise WeatherUnavailable


def _number(value: Any, *, low: float = -1000, high: float = 1000) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), 1) if low <= value <= high else None


def _coordinate(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if low <= value <= high else None


def _array_at(payload: dict[str, Any], key: str, index: int) -> Any:
    values = payload.get(key)
    return values[index] if isinstance(values, list) and index < len(values) else None


def _condition(value: Any) -> str | None:
    code = _number(value, low=0, high=99)
    return DESCRIPTIONS.get(int(code), (None, ""))[0] if code is not None else None


def _valid_temperature(value: Any, unit: str) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and
            math.isfinite(value) and -100 <= value <= (70 if unit == "celsius" else 160))


def _temperature(value: Any, unit: str) -> dict[str, Any] | None:
    if not _valid_temperature(value, unit):
        return None
    celsius, fahrenheit = dual_temperature(value, unit)
    # Keep the original source-unit fields for existing tool consumers. The
    # paired fields are derived once from the same response, Fahrenheit first.
    return {"fahrenheit": fahrenheit, "celsius": celsius,
            "value": fahrenheit if unit == "fahrenheit" else celsius,
            "unit": "°F" if unit == "fahrenheit" else "°C"}


def _probability(value: Any) -> float | None:
    return _number(value, low=0, high=100)


def _date(value: Any, zone: ZoneInfo) -> date | None:
    try:
        if isinstance(value, int):
            return datetime.fromtimestamp(value, timezone.utc).astimezone(zone).date()
        return date.fromisoformat(value) if isinstance(value, str) else None
    except (ValueError, OverflowError, OSError):
        return None


def _local_time(value: Any, zone: ZoneInfo) -> datetime | None:
    try:
        if isinstance(value, int):
            return datetime.fromtimestamp(value, timezone.utc).astimezone(zone)
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
        return moment.replace(tzinfo=zone) if moment and moment.tzinfo is None else None
    except (ValueError, OverflowError, OSError):
        return None


def _place_label(result: dict[str, Any]) -> str:
    return ", ".join(str(result[key]) for key in ("name", "admin1", "country") if result.get(key))[:120]


class WeatherTools:
    """Session-local cache; cancellation cannot publish a late tool result."""

    def __init__(self, settings: Settings, *, fetch_json: Callable = _fetch_json,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.settings = settings
        self.fetch_json = fetch_json
        self.clock = clock
        self.now = now
        self._places: dict[str, Place] = {}
        self._cache: dict[tuple[float, float, str], CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def _resolve(self, requested: str | None) -> Place | dict[str, Any]:
        if requested is not None and not isinstance(requested, str):
            return {"availability": "unavailable", "reason": "invalid_location"}
        if requested is None or not requested.strip():
            if self.settings.weather_latitude is None or self.settings.weather_longitude is None:
                return {"availability": "unavailable", "reason": "default_location_not_configured"}
            name = self.settings.weather_location_name or (
                f"Configured location ({self.settings.weather_latitude:.3f}, "
                f"{self.settings.weather_longitude:.3f})")
            return Place(name, self.settings.weather_latitude, self.settings.weather_longitude)
        if any(ord(ch) < 32 for ch in requested):
            return {"availability": "unavailable", "reason": "invalid_location"}
        requested = requested.strip()
        if (not LOCATION.fullmatch(requested) or not any(ch.isalpha() for ch in requested)
                or "_" in requested):
            return {"availability": "unavailable", "reason": "invalid_location"}
        if requested.casefold() in self._places:
            return self._places[requested.casefold()]
        parts = [part.strip() for part in requested.split(",")]
        if len(parts) > 3 or any(not part for part in parts):
            return {"availability": "unavailable", "reason": "invalid_location"}
        city, *qualifiers = parts
        alias = CITY_ALIASES.get(_location_key(city))
        geocode_name = alias[0] if alias else city
        response = await self.fetch_json(GEOCODE_URL, {"name": geocode_name, "count": 10,
                                                      "language": "en", "format": "json"})
        if not isinstance(response, dict):
            return {"availability": "unavailable", "reason": "invalid_provider_data"}
        results = response.get("results", [])
        if not isinstance(results, list) or not results:
            return {"availability": "unavailable", "reason": "location_not_found"}
        matches = [item for item in results if isinstance(item, dict) and
                   isinstance(item.get("name"), str) and
                   _location_key(item["name"]) == _location_key(geocode_name)]
        if alias:
            matches = [item for item in matches if
                       _qualifier_matches(item, alias[1]) and _qualifier_matches(item, alias[2])]
        for qualifier in qualifiers:
            matches = [item for item in matches if _qualifier_matches(item, qualifier)]
        if not matches:
            return {"availability": "unavailable", "reason": "location_not_found"}
        matches.sort(key=lambda x: _number(x.get("population"), low=0, high=1e9) or 0, reverse=True)
        first = matches[0]
        largest = _number(first.get("population"), low=0, high=1e9) or 0
        second = (_number(matches[1].get("population"), low=0, high=1e9) or 0) if len(matches) > 1 else 0
        if len(matches) > 1 and (largest < 50000 or largest < 5 * second):
            return {"availability": "ambiguous", "candidates": [_place_label(x) for x in matches[:4]]}
        lat = _coordinate(first.get("latitude"), -90, 90)
        lon = _coordinate(first.get("longitude"), -180, 180)
        if lat is None or lon is None:
            return {"availability": "unavailable", "reason": "location_not_found"}
        place = Place(_place_label(first), lat, lon, first.get("timezone"))
        if len(self._places) >= MAX_CACHE:
            self._places.pop(next(iter(self._places)))
        self._places[requested.casefold()] = place
        return place

    async def _load(self, requested: str | None) -> tuple[Place, dict[str, Any], str, str] | dict[str, Any]:
        place = await self._resolve(requested)
        if isinstance(place, dict):
            return place
        key = (place.latitude, place.longitude, self.settings.weather_unit)
        entry = self._cache.get(key)
        age = self.clock() - entry.fetched_at if entry else float("inf")
        if entry and 0 <= age < self.settings.voice_weather_cache_seconds:
            return place, entry.payload, "fresh", entry.retrieved_at
        params = {
            "latitude": place.latitude, "longitude": place.longitude, "timezone": "auto",
            "timeformat": "unixtime",
            "temperature_unit": self.settings.weather_unit,
            "wind_speed_unit": "mph" if self.settings.weather_unit == "fahrenheit" else "kmh",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
            "forecast_days": 8,
        }
        try:
            payload = await self.fetch_json(FORECAST_URL, params)
            if not isinstance(payload.get("timezone"), str) or not isinstance(payload.get("daily"), dict):
                raise WeatherUnavailable
            ZoneInfo(payload["timezone"])
        except (WeatherUnavailable, ZoneInfoNotFoundError):
            if entry and 0 <= age <= self.settings.voice_weather_cache_seconds + STALE_GRACE_SECONDS:
                return place, entry.payload, "stale", entry.retrieved_at
            raise
        if len(self._cache) >= MAX_CACHE:
            self._cache.pop(next(iter(self._cache)))
        retrieved_at = self.now().astimezone(timezone.utc).isoformat()
        self._cache[key] = CacheEntry(place, payload, self.clock(), retrieved_at)
        return place, payload, "fresh", retrieved_at

    async def _query(self, requested: str | None, period: str | None) -> dict[str, Any]:
        async with self._lock:
            try:
                loaded = await self._load(requested)
            except WeatherRateLimited:
                return {"availability": "unavailable", "reason": "provider_rate_limited"}
            except (WeatherUnavailable, httpx.RequestError):
                return {"availability": "unavailable", "reason": "provider_unavailable"}
            if isinstance(loaded, dict):
                return loaded
            place, payload, freshness, retrieved_at = loaded
            try:
                return self._normalize(place, payload, freshness, retrieved_at, period)
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                return {"availability": "unavailable", "reason": "invalid_provider_data"}

    async def current(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._query(arguments.get("location"), None)

    async def forecast(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._query(arguments.get("location"), arguments.get("period", "today"))

    def _normalize(self, place: Place, payload: dict[str, Any], freshness: str, retrieved_at: str,
                   period: str | None) -> dict[str, Any]:
        zone = ZoneInfo(payload["timezone"])
        local_now = self.now().astimezone(zone)
        base = {"availability": "available", "location": place.name,
                "timezone": payload["timezone"], "provider": "Open-Meteo",
                "freshness": freshness, "retrieved_at": retrieved_at,
                "kind": "current_model_estimate" if period is None else "forecast"}
        if period is None:
            current = payload.get("current")
            if not isinstance(current, dict):
                return {"availability": "unavailable", "reason": "current_data_missing"}
            measured = _local_time(current.get("time"), zone)
            if measured is None:
                return {"availability": "unavailable", "reason": "current_data_missing"}
            base.update({"time": measured.isoformat(),
                         "temperature": _temperature(current.get("temperature_2m"), self.settings.weather_unit),
                         "feels_like": _temperature(current.get("apparent_temperature"), self.settings.weather_unit),
                         "conditions": _condition(current.get("weather_code")),
                         "humidity_percent": _probability(current.get("relative_humidity_2m")),
                         "wind_speed": _number(current.get("wind_speed_10m"), low=0, high=500),
                         "wind_unit": "mph" if self.settings.weather_unit == "fahrenheit" else "km/h",
                         "wind_direction_degrees": _number(current.get("wind_direction_10m"), low=0, high=360)})
            if all(base[field] is None for field in (
                    "temperature", "feels_like", "conditions", "humidity_percent", "wind_speed")):
                return {"availability": "unavailable", "reason": "current_data_missing"}
            hourly = payload.get("hourly", {})
            if isinstance(hourly, dict) and isinstance(hourly.get("time"), list):
                for i, stamp in enumerate(hourly["time"]):
                    if stamp == current["time"]:
                        base["precipitation_probability_percent"] = _probability(_array_at(hourly, "precipitation_probability", i))
                        break
            return base
        daily = payload["daily"]
        days = daily.get("time")
        if not isinstance(days, list):
            return {"availability": "unavailable", "reason": "forecast_data_missing"}
        today = local_now.date()
        if period == "tomorrow":
            selected = {today + timedelta(days=1)}
        elif period == "weekend":
            if today.weekday() == 6:
                selected = {today}
            else:
                saturday = today + timedelta(days=(5 - today.weekday()) % 7)
                selected = {saturday, saturday + timedelta(days=1)}
        elif period == "tonight":
            evening = today - timedelta(days=1) if local_now.hour < 6 else today
            selected = {evening, evening + timedelta(days=1)}
        else:
            selected = {today + timedelta(days=i) for i in range(7)} if period == "next_7_days" else {today}
        periods = []
        for i, value in enumerate(days[:8]):
            day = _date(value, zone)
            if day not in selected:
                continue
            if period in ("tonight", "afternoon"):
                continue  # Whole-day high/low must not be represented as a short-window forecast.
            periods.append({"date": day.isoformat(),
                            "conditions": _condition(_array_at(daily, "weather_code", i)),
                            "high": _temperature(_array_at(daily, "temperature_2m_max", i), self.settings.weather_unit),
                            "low": _temperature(_array_at(daily, "temperature_2m_min", i), self.settings.weather_unit),
                            "precipitation_probability_percent": _probability(_array_at(daily, "precipitation_probability_max", i))})
        hourly = payload.get("hourly", {})
        relevant = []
        cutoff = local_now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        if isinstance(hourly, dict) and isinstance(hourly.get("time"), list):
            for i, stamp in enumerate(hourly["time"][:192]):
                moment = _local_time(stamp, zone)
                if moment is None or moment.astimezone(timezone.utc) < cutoff:
                    continue
                day = moment.date()
                if period == "afternoon" and day == today and 12 <= moment.hour < 18:
                    relevant.append((i, moment))
                elif period == "tonight" and ((day == evening and moment.hour >= 18) or
                                              (day == evening + timedelta(days=1) and moment.hour < 6)):
                    relevant.append((i, moment))
                elif period == "today" and day == today:
                    relevant.append((i, moment))
        if period in ("afternoon", "tonight", "today") and relevant:
            probs = [_probability(_array_at(hourly, "precipitation_probability", i)) for i, _ in relevant]
            temps = [_array_at(hourly, "temperature_2m", i) for i, _ in relevant]
            base["window"] = {"from": relevant[0][1].isoformat(), "until": relevant[-1][1].isoformat(),
                              "max_precipitation_probability_percent": max((x for x in probs if x is not None), default=None),
                              "min_temperature": _temperature(min((x for x in temps if _valid_temperature(x, self.settings.weather_unit)),
                                                                  default=None), self.settings.weather_unit)}
        base.update({"period": period, "days": periods[:7]})
        if not periods and "window" not in base:
            return {"availability": "unavailable", "reason": "forecast_period_missing",
                    "location": place.name, "timezone": payload["timezone"], "period": period}
        if not periods and base.get("window") and all(base["window"][field] is None for field in (
                "max_precipitation_probability_percent", "min_temperature")):
            return {"availability": "unavailable", "reason": "forecast_data_missing",
                    "location": place.name, "timezone": payload["timezone"], "period": period}
        if periods and not any(any(day[field] is not None for field in (
                "conditions", "high", "low", "precipitation_probability_percent")) for day in periods):
            return {"availability": "unavailable", "reason": "forecast_data_missing",
                    "location": place.name, "timezone": payload["timezone"], "period": period}
        return base
