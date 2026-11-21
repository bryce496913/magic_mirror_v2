"""Bounded local tool execution; model-provided names and arguments are untrusted."""

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jsonschema import Draft202012Validator

from ...config import Settings
from .search import FRESHNESS, WebSearch
from .weather import PERIODS, WeatherTools

LOGGER = logging.getLogger(__name__)
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class Permission(StrEnum):
    READ_ONLY = "read_only"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    INVALID_ARGUMENTS = "invalid_arguments"
    TOOL_UNAVAILABLE = "tool_unavailable"
    TIMEOUT = "timeout"
    PROVIDER_FAILURE = "provider_failure"
    CANCELLED = "cancelled"


class ToolCancelled(Exception):
    """A provider ended its own read-only operation without a result."""


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    data: dict[str, Any] | None = None

    def json(self) -> str:
        value = {"status": self.status.value}
        if self.status == ToolStatus.SUCCESS and self.data is not None:
            value["data"] = self.data
        return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    enabled: bool
    permission: Permission
    executor: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    timeout_seconds: float
    max_input_bytes: int
    max_output_bytes: int


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if (not isinstance(spec.name, str) or not NAME.fullmatch(spec.name) or
                spec.name in self._tools or not isinstance(spec.description, str) or
                not spec.description or len(spec.description) > 240 or
                not isinstance(spec.enabled, bool) or spec.permission is not Permission.READ_ONLY or
                not inspect.iscoroutinefunction(spec.executor) or
                not 0 < spec.timeout_seconds <= 60 or
                not 1 <= spec.max_input_bytes <= 4096 or
                not 32 <= spec.max_output_bytes <= 16384):
            raise ValueError("Invalid or duplicate read-only tool specification")
        Draft202012Validator.check_schema(spec.parameters)
        if (spec.parameters.get("type") != "object" or
                spec.parameters.get("additionalProperties") is not False or
                self._contains_reference(spec.parameters)):
            raise ValueError("Tool parameters must be a closed local object schema")
        self._tools[spec.name] = spec

    @staticmethod
    def _contains_reference(value: Any) -> bool:
        if isinstance(value, dict):
            return (any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef")) or
                    any(ToolRegistry._contains_reference(item) for item in value.values()))
        if isinstance(value, list):
            return any(ToolRegistry._contains_reference(item) for item in value)
        return False

    def get(self, name: str) -> ToolSpec | None:
        if not isinstance(name, str):
            return None
        spec = self._tools.get(name)
        return spec if spec is not None and spec.enabled else None

    def declarations(self) -> list[dict[str, Any]]:
        return [{"type": "function", "name": spec.name, "description": spec.description,
                 "parameters": spec.parameters}
                for spec in self._tools.values() if spec.enabled]


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry, settings: Settings):
        self.registry = registry
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.tool_concurrency)

    async def execute(self, name: str, arguments: str) -> ToolResult:
        spec = self.registry.get(name)
        if spec is None:
            LOGGER.info("Tool validation rejected (unregistered_or_disabled)")
            return ToolResult(ToolStatus.TOOL_UNAVAILABLE)
        if not isinstance(arguments, str):
            LOGGER.info("Tool %s validation rejected (invalid_arguments)", spec.name)
            return ToolResult(ToolStatus.INVALID_ARGUMENTS)
        try:
            input_size = len(arguments.encode("utf-8"))
        except UnicodeError:
            LOGGER.info("Tool %s validation rejected (invalid_arguments)", spec.name)
            return ToolResult(ToolStatus.INVALID_ARGUMENTS)
        if input_size > spec.max_input_bytes:
            LOGGER.info("Tool %s validation rejected (input_limit)", spec.name)
            return ToolResult(ToolStatus.INVALID_ARGUMENTS)
        try:
            value = json.loads(arguments, object_pairs_hook=_no_duplicate_keys,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON constant")))
        except (ValueError, UnicodeError, RecursionError):
            LOGGER.info("Tool %s validation rejected (invalid_json)", spec.name)
            return ToolResult(ToolStatus.INVALID_ARGUMENTS)
        if not Draft202012Validator(spec.parameters).is_valid(value):
            LOGGER.info("Tool %s validation rejected (schema)", spec.name)
            return ToolResult(ToolStatus.INVALID_ARGUMENTS)
        LOGGER.info("Tool %s validation accepted", spec.name)
        try:
            async with asyncio.timeout(min(spec.timeout_seconds, self.settings.tool_timeout_seconds)):
                async with self._slots:
                    LOGGER.info("Tool %s execution started", spec.name)
                    output = await spec.executor(value)
            if not isinstance(output, dict):
                raise TypeError("Tool executor must return an object")
            result = ToolResult(ToolStatus.SUCCESS, output)
            if len(result.json().encode("utf-8")) > min(spec.max_output_bytes, self.settings.tool_max_output_bytes):
                LOGGER.info("Tool %s execution result (provider_failure)", spec.name)
                return ToolResult(ToolStatus.PROVIDER_FAILURE)
            LOGGER.info("Tool %s execution result (success)", spec.name)
            return result
        except TimeoutError:
            LOGGER.info("Tool %s execution result (timeout)", spec.name)
            return ToolResult(ToolStatus.TIMEOUT)
        except ToolCancelled:
            LOGGER.info("Tool %s execution result (cancelled)", spec.name)
            return ToolResult(ToolStatus.CANCELLED)
        except asyncio.CancelledError:
            LOGGER.info("Tool %s execution result (cancelled)", spec.name)
            raise
        except Exception as exc:
            # Only the allowlisted name and exception type are logged; never arguments or result data.
            LOGGER.error("Tool %s execution result (provider_failure: %s)", spec.name, type(exc).__name__)
            return ToolResult(ToolStatus.PROVIDER_FAILURE)


async def _capabilities(_arguments: dict[str, Any]) -> dict[str, Any]:
    return {"capabilities": ["local wake activation", "voice conversation", "read-only tool foundation"]}


def default_registry(settings: Settings) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="get_mirror_capabilities", description="List built-in Magic Mirror assistant capabilities.",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        enabled=settings.tools_enabled, permission=Permission.READ_ONLY, executor=_capabilities,
        timeout_seconds=2.0, max_input_bytes=128, max_output_bytes=512,
    ))
    weather = WeatherTools(settings)
    location = {"type": "string", "minLength": 2, "maxLength": 80,
                "description": "Optional city, or city and country/region. Omit for configured default."}
    registry.register(ToolSpec(
        name="get_current_weather",
        description="Get current Open-Meteo model conditions, temperature, humidity and wind for a location. Never describe these as measured observations.",
        parameters={"type": "object", "properties": {"location": location},
                    "required": [], "additionalProperties": False},
        enabled=settings.tools_enabled, permission=Permission.READ_ONLY,
        executor=weather.current, timeout_seconds=15.0,
        max_input_bytes=256, max_output_bytes=2048,
    ))
    registry.register(ToolSpec(
        name="get_weather_forecast",
        description="Get Open-Meteo forecast for today, this afternoon, tonight, tomorrow, the next weekend or seven days. Use precipitation probability for rain questions; state timeframe and location.",
        parameters={"type": "object", "properties": {
            "location": location, "period": {"type": "string", "enum": list(PERIODS)}},
            "required": ["period"], "additionalProperties": False},
        enabled=settings.tools_enabled, permission=Permission.READ_ONLY,
        executor=weather.forecast, timeout_seconds=15.0,
        max_input_bytes=256, max_output_bytes=2048,
    ))
    search = WebSearch(settings)
    registry.register(ToolSpec(
        name="search_web",
        description=("Search current public web information once for recent news, events, releases, or changing facts. "
                     "Use only when up-to-date sources are needed; cite sources and explain uncertainty."),
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "minLength": 3, "maxLength": 160},
            "freshness": {"type": "string", "enum": list(FRESHNESS)},
            "max_results": {"type": "integer", "minimum": 1, "maximum": settings.search_max_results}},
            "required": ["query"], "additionalProperties": False},
        enabled=settings.tools_enabled and settings.search_enabled,
        permission=Permission.READ_ONLY, executor=search.search,
        timeout_seconds=30.0, max_input_bytes=512, max_output_bytes=2048,
    ))
    return registry
