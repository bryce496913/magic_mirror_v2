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


def test_voice_weather_configuration():
    settings = Settings.from_env({"MIRROR_WEATHER_LOCATION_NAME": "Auckland, New Zealand",
                                  "MIRROR_VOICE_WEATHER_CACHE_SECONDS": "900"})
    assert settings.weather_location_name == "Auckland, New Zealand"
    assert settings.voice_weather_cache_seconds == 900
    for values in ({"MIRROR_WEATHER_LOCATION_NAME": "bad\nname"},
                   {"MIRROR_VOICE_WEATHER_CACHE_SECONDS": "5"}):
        with pytest.raises(ConfigurationError):
            Settings.from_env(values)


def test_search_is_opt_in_with_bounded_cost_configuration():
    default = Settings.from_env({})
    assert not default.search_enabled
    assert default.search_daily_budget == 10
    configured = Settings.from_env({"MIRROR_SEARCH_ENABLED": "true",
        "MIRROR_SEARCH_MAX_PER_CONVERSATION": "3", "MIRROR_SEARCH_MAX_RESULTS": "2",
        "MIRROR_SEARCH_TIMEOUT_SECONDS": "9", "MIRROR_SEARCH_CACHE_SECONDS": "120",
        "MIRROR_SEARCH_DAILY_BUDGET": "5", "MIRROR_SEARCH_ESTIMATED_USD_PER_REQUEST": "0.02"})
    assert configured.search_enabled and configured.search_max_per_conversation == 3
    assert configured.search_max_results == 2 and configured.search_timeout_seconds == 9
    assert configured.search_cache_seconds == 120 and configured.search_daily_budget == 5
    assert configured.search_estimated_usd_per_request == 0.02
    for values in ({"MIRROR_SEARCH_ENABLED": "yes"},
                   {"MIRROR_SEARCH_MAX_PER_CONVERSATION": "0"},
                   {"MIRROR_SEARCH_MAX_RESULTS": "4"},
                   {"MIRROR_SEARCH_TIMEOUT_SECONDS": "0"},
                   {"MIRROR_SEARCH_CACHE_SECONDS": "-1"},
                   {"MIRROR_SEARCH_DAILY_BUDGET": "1001"},
                   {"MIRROR_SEARCH_ESTIMATED_USD_PER_REQUEST": "-1"}):
        with pytest.raises(ConfigurationError):
            Settings.from_env(values)


def test_pihole_ca_certificate_path(tmp_path):
    ca_cert = tmp_path / "local-ca.crt"
    ca_cert.write_text("test fixture only")
    settings = Settings.from_env({"MIRROR_PIHOLE_URL": "https://pi.hole",
                                  "MIRROR_PIHOLE_CA_CERT": str(ca_cert)})
    assert settings.pihole_ca_cert == str(ca_cert)


@pytest.mark.parametrize("ca_path", ["relative-ca.crt", "/nonexistent/mirror-ca.crt"])
def test_invalid_pihole_ca_certificate_path(ca_path):
    with pytest.raises(ConfigurationError, match="MIRROR_PIHOLE_CA_CERT"):
        Settings.from_env({"MIRROR_PIHOLE_URL": "https://pi.hole",
                           "MIRROR_PIHOLE_CA_CERT": ca_path})


def test_pihole_ca_certificate_must_be_readable_https_file(tmp_path, monkeypatch):
    with pytest.raises(ConfigurationError, match="MIRROR_PIHOLE_CA_CERT"):
        Settings.from_env({"MIRROR_PIHOLE_URL": "https://pi.hole",
                           "MIRROR_PIHOLE_CA_CERT": str(tmp_path)})
    ca_cert = tmp_path / "local-ca.crt"
    ca_cert.write_text("test fixture only")
    with pytest.raises(ConfigurationError, match="requires an HTTPS"):
        Settings.from_env({"MIRROR_PIHOLE_CA_CERT": str(ca_cert)})
    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()))
        with pytest.raises(ConfigurationError, match="readable file"):
            Settings.from_env({"MIRROR_PIHOLE_URL": "https://pi.hole",
                               "MIRROR_PIHOLE_CA_CERT": str(ca_cert)})


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
