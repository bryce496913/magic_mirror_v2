"""Environment configuration with safe development defaults."""

import logging
import os
from dataclasses import dataclass
from typing import Mapping


class ConfigurationError(ValueError):
    """An environment setting is invalid."""


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 5000
    log_level: str = "INFO"

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
        return cls(host=host, port=port, log_level=level)


def configure_logging(level: str) -> None:
    """Use one predictable format for application and Flask logs."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
