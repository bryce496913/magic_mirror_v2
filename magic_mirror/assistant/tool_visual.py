"""Versioned, bounded presentation data derived from read-only tool results."""

import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from .tools.search import _public_url

VERSION = 1
MAX_VISUAL_BYTES = 2200
KINDS = {"get_current_weather": "weather_current",
         "get_weather_forecast": "weather_forecast", "search_web": "web_search"}
SAFE_REASONS = {
    "default_location_not_configured": "Default location is not configured",
    "invalid_location": "Location is invalid",
    "location_not_found": "Location was not found",
    "ambiguous": "Location needs clarification",
    "provider_rate_limited": "Weather source is busy",
    "provider_unavailable": "Source is temporarily unavailable",
    "forecast_period_missing": "Forecast period is unavailable",
    "forecast_data_missing": "Forecast data is unavailable",
    "current_data_missing": "Current conditions are unavailable",
    "no_search_results": "No retrievable search results",
    "no_cited_results": "No cited search results",
    "authentication_failed": "Search authentication failed",
    "authentication_not_configured": "Search is not configured",
    "rate_limited": "Search source is busy",
    "provider_timeout": "Search timed out",
    "conversation_search_limit": "Conversation search limit reached",
    "daily_budget_exhausted": "Daily search budget reached",
    "search_disabled": "Search is disabled",
}
STATUS_DETAILS = {"timeout": "Request timed out", "provider_failure": "Source unavailable",
                  "tool_unavailable": "Tool is unavailable", "invalid_arguments": "Question needs clarification",
                  "cancelled": "Request cancelled"}


@dataclass(frozen=True)
class VisualSource:
    title: str
    url: str
    source_name: str
    publication_date: str = ""


@dataclass(frozen=True)
class ToolVisual:
    version: int
    turn_id: str
    call_id: str
    response_id: str
    kind: str
    status: str
    title: str = ""
    summary: str = ""
    detail: str = ""
    freshness: str = ""
    sources: tuple[VisualSource, ...] = ()


def _plain(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if not -1000 <= value <= 1000:
        return ""
    return f"{value:g}"


def _temp(value: Any) -> str:
    if not isinstance(value, dict) or value.get("unit") not in {"°C", "°F"}:
        return ""
    number = _number(value.get("value"))
    return number + value["unit"] if number else ""


def _chance(value: Any) -> str:
    number = _number(value)
    return number + "% rain chance" if number and 0 <= float(value) <= 100 else ""


def _published(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 10:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _sources(raw: Any) -> tuple[VisualSource, ...]:
    if not isinstance(raw, list):
        return ()
    results = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        url = _public_url(item.get("url"))
        if not url:
            continue
        name = _plain(item.get("source_name"), 60)
        title = _plain(item.get("title"), 70) or name
        if not title:
            continue
        published = _published(item.get("publication_date"))
        results.append(VisualSource(title, url, name, published))
    return tuple(results)


def loading(name: str, turn_id: str, call_id: str, response_id: str) -> ToolVisual | None:
    kind = KINDS.get(name)
    if not kind:
        return None
    return ToolVisual(VERSION, turn_id, call_id, response_id, kind, "loading",
                      "Checking weather" if kind.startswith("weather") else "Searching the web")


def completed(name: str, turn_id: str, call_id: str, response_id: str,
              status: str, data: dict[str, Any] | None) -> ToolVisual | None:
    base = loading(name, turn_id, call_id, response_id)
    if base is None:
        return None
    data = data if isinstance(data, dict) else {}
    if status != "success" or data.get("availability") != "available":
        reason = data.get("reason") if status == "success" else None
        if data.get("availability") == "ambiguous":
            reason = "ambiguous"
        return ToolVisual(VERSION, turn_id, call_id, response_id, base.kind,
                          "unavailable", base.title,
                          detail=SAFE_REASONS.get(reason, STATUS_DETAILS.get(status, "Information unavailable")))
    if base.kind == "weather_current":
        title = "Weather · " + (_plain(data.get("location"), 65) or "Location unavailable")
        summary = " · ".join(filter(None, (_plain(data.get("conditions"), 45),
                                           _temp(data.get("temperature")),
                                           "Feels " + _temp(data.get("feels_like")) if _temp(data.get("feels_like")) else "")))
        detail = " · ".join(filter(None, ("Current model estimate", _chance(data.get("precipitation_probability_percent")))))
    elif base.kind == "weather_forecast":
        period = _plain(data.get("period"), 18).replace("_", " ").title() or "Forecast"
        title = period + " · " + (_plain(data.get("location"), 55) or "Location unavailable")
        days = data.get("days") if isinstance(data.get("days"), list) else []
        day = days[0] if days and isinstance(days[0], dict) else {}
        window = data.get("window") if isinstance(data.get("window"), dict) else {}
        summary = " · ".join(filter(None, (_plain(day.get("conditions"), 45),
                                           "High " + _temp(day.get("high")) if _temp(day.get("high")) else "",
                                           "Low " + _temp(day.get("low")) if _temp(day.get("low")) else "",
                                           "Low " + _temp(window.get("min_temperature")) if _temp(window.get("min_temperature")) else "")))
        detail = " · ".join(filter(None, (_chance(window.get("max_precipitation_probability_percent")) or
                                           _chance(day.get("precipitation_probability_percent")),
                                           _plain(day.get("date"), 10))))
        if data.get("period") == "weekend" and len(days) > 1 and isinstance(days[1], dict):
            pieces = []
            for item in days[:2]:
                if not isinstance(item, dict):
                    continue
                piece = " · ".join(filter(None, (_plain(item.get("date"), 10),
                                                _plain(item.get("conditions"), 30),
                                                "H " + _temp(item.get("high")) if _temp(item.get("high")) else "",
                                                "L " + _temp(item.get("low")) if _temp(item.get("low")) else "")))
                if piece:
                    pieces.append(piece)
            summary = "; ".join(pieces)
            probabilities = [item.get("precipitation_probability_percent") for item in days[:2]
                             if isinstance(item, dict)]
            valid = [value for value in probabilities if isinstance(value, (int, float)) and
                     not isinstance(value, bool) and 0 <= value <= 100]
            detail = "Weekend max " + _chance(max(valid)) if valid else ""
    else:
        title = _plain(data.get("query"), 80) or "Web search"
        summary = _plain(data.get("summary"), 200)
        detail = "Cited web result"
    freshness = _plain(data.get("freshness"), 10) if base.kind.startswith("weather") else ""
    visual = ToolVisual(VERSION, turn_id, call_id, response_id, base.kind, "completed",
                        title[:80], summary[:200], detail[:100], freshness,
                        _sources(data.get("sources")) if base.kind == "web_search" else ())
    if len(json.dumps(asdict(visual), ensure_ascii=True).encode()) > MAX_VISUAL_BYTES:
        return ToolVisual(VERSION, turn_id, call_id, response_id, base.kind,
                          "unavailable", base.title, detail="Display summary is too large")
    return visual
