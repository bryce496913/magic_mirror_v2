"""Environment configuration with safe development defaults."""

import logging
import os
from dataclasses import dataclass
from typing import Mapping


class ConfigurationError(ValueError):
    """An environment setting is invalid."""


DASHBOARD_MODULES = ("weather", "news", "pihole", "system", "assistant")


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 5000
    log_level: str = "INFO"
    clock_format: str = "24"
    display_scale: float = 1.0
    enabled_modules: tuple[str, ...] = DASHBOARD_MODULES

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if environ is None else environ
        host = values.get("MIRROR_HOST", "127.0.0.1").strip()
        if not host or any(char.isspace() for char in host):
            raise ConfigurationError("MIRROR_HOST must be a nonempty address")

        raw_port = values.get("MIRROR_PORT", "5000")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigurationError("MIRROR_PORT must be an integer from 1 to 65535") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("MIRROR_PORT must be an integer from 1 to 65535")

        level = values.get("MIRROR_LOG_LEVEL", "INFO").strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("MIRROR_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

        clock_format = values.get("MIRROR_CLOCK_FORMAT", "24").strip()
        if clock_format not in {"12", "24"}:
            raise ConfigurationError("MIRROR_CLOCK_FORMAT must be 12 or 24")

        try:
            display_scale = float(values.get("MIRROR_DISPLAY_SCALE", "1.0"))
        except ValueError as exc:
            raise ConfigurationError("MIRROR_DISPLAY_SCALE must be between 0.75 and 1.5") from exc
        if not 0.75 <= display_scale <= 1.5:
            raise ConfigurationError("MIRROR_DISPLAY_SCALE must be between 0.75 and 1.5")

        raw_modules = values.get("MIRROR_MODULES", ",".join(DASHBOARD_MODULES))
        modules = tuple(item.strip().lower() for item in raw_modules.split(",") if item.strip())
        if len(modules) != len(set(modules)) or any(item not in DASHBOARD_MODULES for item in modules):
            raise ConfigurationError("MIRROR_MODULES must be a comma-separated list of unique weather, news, pihole, system, assistant names")
        return cls(host=host, port=port, log_level=level, clock_format=clock_format,
                   display_scale=display_scale, enabled_modules=modules)


def configure_logging(level: str) -> None:
    """Use one predictable format for application and Flask logs."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
