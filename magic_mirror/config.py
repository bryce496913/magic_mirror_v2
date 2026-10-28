"""Environment configuration with safe development defaults."""

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """An environment setting is invalid."""


DASHBOARD_MODULES = ("weather", "news", "pihole", "system", "assistant")
LED_STATES = ("idle", "listening", "responding", "off")


def _led_pin(values: Mapping[str, str], name: str, default: int) -> int:
    try:
        pin = int(values.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a BCM GPIO number from 0 to 27") from exc
    if not 0 <= pin <= 27:
        raise ConfigurationError(f"{name} must be a BCM GPIO number from 0 to 27")
    return pin


def _bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name, "true" if default else "false").strip().lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")
    return value == "true"


def _audio_int(values: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(values.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be between {low} and {high}") from exc
    if not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low} and {high}")
    return value


def _audio_float(values: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(values.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be between {low} and {high}") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low} and {high}")
    return value


def _interval(values: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    try:
        interval = int(values.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer of at least {minimum} seconds") from exc
    if interval < minimum or interval > 86400:
        raise ConfigurationError(f"{name} must be between {minimum} and 86400 seconds")
    return interval


def _url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ConfigurationError(f"{name} must be an HTTP(S) URL without credentials or query parameters")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 5000
    log_level: str = "INFO"
    clock_format: str = "24"
    display_scale: float = 1.0
    enabled_modules: tuple[str, ...] = DASHBOARD_MODULES
    weather_latitude: float | None = None
    weather_longitude: float | None = None
    weather_unit: str = "celsius"
    news_feeds: tuple[str, ...] = ()
    pihole_url: str = "http://127.0.0.1"
    pihole_password_file: str = ""
    pihole_ca_cert: str = ""
    system_interval: int = 15
    weather_interval: int = 900
    news_interval: int = 1800
    pihole_interval: int = 60
    led_enabled: bool = False
    led_red_pin: int = 22
    led_green_pin: int = 27
    led_blue_pin: int = 24
    led_active_high: bool = True
    led_initial_state: str = "off"
    audio_enabled: bool = False
    audio_input_device: str = "USB PnP Sound Device"
    audio_output_device: str = "MAX98357A"
    audio_capture_rate: int = 48000
    audio_playback_rate: int = 48000
    audio_capture_channels: int = 1
    audio_playback_channels: int = 1
    audio_buffer_duration: float = 0.1
    audio_frame_format: str = "int16"
    audio_playback_gain: float = 1.0
    audio_max_playback_level: float = 1.0
    audio_timeout: float = 5.0

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

        raw_lat = values.get("MIRROR_WEATHER_LATITUDE", "").strip()
        raw_lon = values.get("MIRROR_WEATHER_LONGITUDE", "").strip()
        if bool(raw_lat) != bool(raw_lon):
            raise ConfigurationError("MIRROR_WEATHER_LATITUDE and MIRROR_WEATHER_LONGITUDE must be set together")
        latitude = longitude = None
        if raw_lat:
            try:
                latitude, longitude = float(raw_lat), float(raw_lon)
            except ValueError as exc:
                raise ConfigurationError("Weather coordinates must be numbers") from exc
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ConfigurationError("Weather coordinates are out of range")

        unit = values.get("MIRROR_WEATHER_UNIT", "celsius").strip().lower()
        if unit not in {"celsius", "fahrenheit"}:
            raise ConfigurationError("MIRROR_WEATHER_UNIT must be celsius or fahrenheit")
        feeds = tuple(_url(item.strip(), "MIRROR_NEWS_FEEDS") for item in
                      values.get("MIRROR_NEWS_FEEDS", "").split(",") if item.strip())
        if len(feeds) > 5 or len(feeds) != len(set(feeds)):
            raise ConfigurationError("MIRROR_NEWS_FEEDS must contain at most five unique URLs")
        pihole_url = _url(values.get("MIRROR_PIHOLE_URL", "http://127.0.0.1").strip(), "MIRROR_PIHOLE_URL")
        password_file = values.get("MIRROR_PIHOLE_PASSWORD_FILE", "").strip()
        if password_file and not os.path.isabs(password_file):
            raise ConfigurationError("MIRROR_PIHOLE_PASSWORD_FILE must be an absolute path")
        if password_file and urlparse(pihole_url).scheme != "https" and urlparse(pihole_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError("Pi-hole authentication requires HTTPS or a loopback URL")
        ca_cert = values.get("MIRROR_PIHOLE_CA_CERT", "").strip()
        if ca_cert:
            if not os.path.isabs(ca_cert):
                raise ConfigurationError("MIRROR_PIHOLE_CA_CERT must be an absolute path")
            if urlparse(pihole_url).scheme != "https":
                raise ConfigurationError("MIRROR_PIHOLE_CA_CERT requires an HTTPS Pi-hole URL")
            if not Path(ca_cert).is_file():
                raise ConfigurationError("MIRROR_PIHOLE_CA_CERT must point to a readable file")
            try:
                with open(ca_cert, "rb"):
                    pass
            except OSError as exc:
                raise ConfigurationError("MIRROR_PIHOLE_CA_CERT must point to a readable file") from exc
        led_pins = (_led_pin(values, "MIRROR_LED_RED_PIN", 22),
                    _led_pin(values, "MIRROR_LED_GREEN_PIN", 27),
                    _led_pin(values, "MIRROR_LED_BLUE_PIN", 24))
        if len(set(led_pins)) != 3:
            raise ConfigurationError("LED channels must use distinct BCM GPIO pins")
        led_initial_state = values.get("MIRROR_LED_INITIAL_STATE", "off").strip().lower()
        if led_initial_state not in LED_STATES:
            raise ConfigurationError("MIRROR_LED_INITIAL_STATE must be idle, listening, responding, or off")
        audio_input = values.get("MIRROR_AUDIO_INPUT_DEVICE", "USB PnP Sound Device").strip()
        audio_output = values.get("MIRROR_AUDIO_OUTPUT_DEVICE", "MAX98357A").strip()
        if any(not name or len(name) > 120 or any(char in name for char in "\r\n\x00")
               for name in (audio_input, audio_output)):
            raise ConfigurationError("Audio device selectors must be nonempty single-line names")
        audio_format = values.get("MIRROR_AUDIO_FRAME_FORMAT", "int16").strip().lower()
        if audio_format not in {"int16", "float32"}:
            raise ConfigurationError("MIRROR_AUDIO_FRAME_FORMAT must be int16 or float32")
        return cls(host=host, port=port, log_level=level, clock_format=clock_format,
                   display_scale=display_scale, enabled_modules=modules,
                   weather_latitude=latitude, weather_longitude=longitude, weather_unit=unit,
                   news_feeds=feeds, pihole_url=pihole_url, pihole_password_file=password_file,
                   pihole_ca_cert=ca_cert,
                   system_interval=_interval(values, "MIRROR_SYSTEM_INTERVAL", 15, 5),
                   weather_interval=_interval(values, "MIRROR_WEATHER_INTERVAL", 900, 300),
                   news_interval=_interval(values, "MIRROR_NEWS_INTERVAL", 1800, 300),
                   pihole_interval=_interval(values, "MIRROR_PIHOLE_INTERVAL", 60, 15),
                   led_enabled=_bool(values, "MIRROR_LED_ENABLED", False),
                   led_red_pin=led_pins[0], led_green_pin=led_pins[1], led_blue_pin=led_pins[2],
                   led_active_high=_bool(values, "MIRROR_LED_ACTIVE_HIGH", True),
                   led_initial_state=led_initial_state,
                   audio_enabled=_bool(values, "MIRROR_AUDIO_ENABLED", False),
                   audio_input_device=audio_input, audio_output_device=audio_output,
                   audio_capture_rate=_audio_int(values, "MIRROR_AUDIO_CAPTURE_RATE", 48000, 8000, 96000),
                   audio_playback_rate=_audio_int(values, "MIRROR_AUDIO_PLAYBACK_RATE", 48000, 8000, 96000),
                   audio_capture_channels=_audio_int(values, "MIRROR_AUDIO_CAPTURE_CHANNELS", 1, 1, 2),
                   audio_playback_channels=_audio_int(values, "MIRROR_AUDIO_PLAYBACK_CHANNELS", 1, 1, 2),
                   audio_buffer_duration=_audio_float(values, "MIRROR_AUDIO_BUFFER_DURATION", 0.1, 0.02, 1.0),
                   audio_frame_format=audio_format,
                   audio_playback_gain=_audio_float(values, "MIRROR_AUDIO_PLAYBACK_GAIN", 1.0, 0.0, 4.0),
                   audio_max_playback_level=_audio_float(values, "MIRROR_AUDIO_MAX_PLAYBACK_LEVEL", 1.0, 0.1, 1.0),
                   audio_timeout=_audio_float(values, "MIRROR_AUDIO_TIMEOUT", 5.0, 0.1, 30.0))


def configure_logging(level: str) -> None:
    """Use one predictable format for application and Flask logs."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
