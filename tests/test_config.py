import pytest

from magic_mirror.config import ConfigurationError, Settings


def test_defaults():
    assert Settings.from_env({}) == Settings()


def test_environment_overrides():
    assert Settings.from_env({"MIRROR_HOST": "localhost", "MIRROR_PORT": "5050", "MIRROR_LOG_LEVEL": "debug"}) == Settings(
        host="localhost", port=5050, log_level="DEBUG"
    )


def test_dashboard_configuration():
    settings = Settings.from_env({
        "MIRROR_CLOCK_FORMAT": "12", "MIRROR_DISPLAY_SCALE": "1.25",
        "MIRROR_MODULES": "weather,assistant",
    })
    assert settings.clock_format == "12"
    assert settings.display_scale == 1.25
    assert settings.enabled_modules == ("weather", "assistant")
    assert Settings.from_env({"MIRROR_MODULES": ""}).enabled_modules == ()


def test_live_module_configuration():
    settings = Settings.from_env({
        "MIRROR_WEATHER_LATITUDE": "40.7", "MIRROR_WEATHER_LONGITUDE": "-74.0",
        "MIRROR_WEATHER_UNIT": "fahrenheit", "MIRROR_NEWS_FEEDS": "https://example.com/rss",
        "MIRROR_SYSTEM_INTERVAL": "20", "MIRROR_PIHOLE_URL": "http://127.0.0.1",
        "MIRROR_PIHOLE_PASSWORD_FILE": "/tmp/local-password",
    })
    assert settings.weather_latitude == 40.7
    assert settings.weather_unit == "fahrenheit"
    assert settings.news_feeds == ("https://example.com/rss",)
    assert settings.system_interval == 20
    assert settings.pihole_password_file == "/tmp/local-password"


@pytest.mark.parametrize(
    "values",
    [
        {"MIRROR_HOST": ""},
        {"MIRROR_HOST": "bad host"},
        {"MIRROR_PORT": "abc"},
        {"MIRROR_PORT": "0"},
        {"MIRROR_PORT": "65536"},
        {"MIRROR_LOG_LEVEL": "VERBOSE"},
        {"MIRROR_CLOCK_FORMAT": "13"},
        {"MIRROR_DISPLAY_SCALE": "nan"},
        {"MIRROR_DISPLAY_SCALE": "0.5"},
        {"MIRROR_MODULES": "weather,weather"},
        {"MIRROR_MODULES": "weather,unknown"},
        {"MIRROR_WEATHER_LATITUDE": "40"},
        {"MIRROR_WEATHER_LATITUDE": "91", "MIRROR_WEATHER_LONGITUDE": "0"},
        {"MIRROR_WEATHER_LATITUDE": "nan", "MIRROR_WEATHER_LONGITUDE": "0"},
        {"MIRROR_WEATHER_UNIT": "kelvin"},
        {"MIRROR_NEWS_FEEDS": "https://user:password@example.com/rss"},
        {"MIRROR_NEWS_FEEDS": "file:///etc/passwd"},
        {"MIRROR_PIHOLE_PASSWORD_FILE": "relative-secret"},
        {"MIRROR_PIHOLE_URL": "http://192.0.2.1", "MIRROR_PIHOLE_PASSWORD_FILE": "/tmp/secret"},
        {"MIRROR_SYSTEM_INTERVAL": "1"},
        {"MIRROR_WEATHER_INTERVAL": "bad"},
    ],
)
def test_invalid_configuration(values):
    with pytest.raises(ConfigurationError):
        Settings.from_env(values)
