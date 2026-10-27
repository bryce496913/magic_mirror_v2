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
    ],
)
def test_invalid_configuration(values):
    with pytest.raises(ConfigurationError):
        Settings.from_env(values)
