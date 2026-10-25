import pytest

from magic_mirror.config import ConfigurationError, Settings


def test_defaults():
    assert Settings.from_env({}) == Settings()


def test_environment_overrides():
    assert Settings.from_env({"MIRROR_HOST": "localhost", "MIRROR_PORT": "5050", "MIRROR_LOG_LEVEL": "debug"}) == Settings(
        host="localhost", port=5050, log_level="DEBUG"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"MIRROR_HOST": ""},
        {"MIRROR_HOST": "bad host"},
        {"MIRROR_PORT": "abc"},
        {"MIRROR_PORT": "0"},
        {"MIRROR_PORT": "65536"},
        {"MIRROR_LOG_LEVEL": "VERBOSE"},
    ],
)
def test_invalid_configuration(values):
    with pytest.raises(ConfigurationError):
        Settings.from_env(values)
