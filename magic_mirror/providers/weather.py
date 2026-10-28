"""Open-Meteo current weather and short forecast."""

import math

import requests

from . import ProviderUnavailable

API_URL = "https://api.open-meteo.com/v1/forecast"
DESCRIPTIONS = {
    0: ("Clear", "☀︎"), 1: ("Mostly clear", "☀︎"), 2: ("Partly cloudy", "☁︎"),
    3: ("Cloudy", "☁︎"), 45: ("Fog", "≋"), 48: ("Fog", "≋"),
    51: ("Drizzle", "☂︎"), 53: ("Drizzle", "☂︎"), 55: ("Drizzle", "☂︎"),
    61: ("Rain", "☂︎"), 63: ("Rain", "☂︎"), 65: ("Rain", "☂︎"),
    71: ("Snow", "❄︎"), 73: ("Snow", "❄︎"), 75: ("Snow", "❄︎"),
    80: ("Showers", "☂︎"), 81: ("Showers", "☂︎"), 82: ("Showers", "☂︎"),
    95: ("Thunderstorm", "⚡︎"), 96: ("Thunderstorm", "⚡︎"), 99: ("Thunderstorm", "⚡︎"),
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProviderUnavailable("Weather data is invalid")
    return round(value, 1)


def dual_temperature(value, unit):
    """Return Celsius and Fahrenheit from one Open-Meteo reading."""
    if value is None:
        return None, None
    value = _number(value)
    if unit == "fahrenheit":
        return round((value - 32) * 5 / 9, 1), value
    return value, round(value * 9 / 5 + 32, 1)


def read_weather(settings):
    if settings.weather_latitude is None:
        raise ProviderUnavailable("Location not configured")
    response = requests.get(API_URL, params={
        "latitude": settings.weather_latitude, "longitude": settings.weather_longitude,
        "current": "temperature_2m,apparent_temperature,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
        "forecast_days": 2, "timezone": "auto", "temperature_unit": settings.weather_unit,
    }, timeout=(3, 5))
    response.raise_for_status()
    try:
        payload = response.json()
        current, daily = payload["current"], payload["daily"]
        code = int(_number(current["weather_code"]))
        tomorrow_code = int(_number(daily["weather_code"][1]))
        result = {
            "temperature": _number(current["temperature_2m"]),
            "description": DESCRIPTIONS.get(code, ("Conditions unavailable", ""))[0],
            "icon": DESCRIPTIONS.get(code, ("", ""))[1],
            "high": _number(daily["temperature_2m_max"][0]),
            "low": _number(daily["temperature_2m_min"][0]),
            "forecast": DESCRIPTIONS.get(tomorrow_code, ("Forecast unavailable", ""))[0],
            "unit": "°F" if settings.weather_unit == "fahrenheit" else "°C",
        }
        feels_like = current.get("apparent_temperature")
        result["feels_like"] = _number(feels_like) if feels_like is not None else None
        for field in ("temperature", "high", "low", "feels_like"):
            result[field + "_c"], result[field + "_f"] = dual_temperature(result[field], settings.weather_unit)
        chance = daily.get("precipitation_probability_max", [None])[0]
        result["precipitation_percent"] = _number(chance) if chance is not None else None
        return result
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProviderUnavailable("Weather data is invalid") from exc
