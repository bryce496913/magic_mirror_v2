"""User service setup tests; never contact a live systemd manager or audio device."""

import configparser
import importlib.util
import os
from pathlib import Path

import pytest

from magic_mirror.assistant import service_ready
from magic_mirror.audio.devices import DeviceError
from magic_mirror.config import Settings


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("manage_user_services", ROOT / "scripts" / "manage_user_services.py")
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)


def _unit(name):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(ROOT / "systemd" / "user" / name)
    return parser


def test_user_units_are_separate_bounded_and_secret_free():
    dashboard = _unit("magic-mirror-dashboard.service")
    voice = _unit("magic-mirror-voice.service")
    for unit in (dashboard, voice):
        service = unit["Service"]
        assert service["Type"] == "exec"
        assert service["WorkingDirectory"] == "/home/bryce/magic_mirror_v2"
        assert service["Restart"] == "on-failure"
        assert service["RestartSec"] == "15s"
        assert service["TimeoutStopSec"] == "45s"
        assert service["KillSignal"] == "SIGTERM"
        assert service["KillMode"] == "control-group"
        assert service["UMask"] == "0077"
        assert unit["Unit"]["StartLimitIntervalSec"] == "300"
        assert unit["Unit"]["StartLimitBurst"] == "5"
        assert unit["Install"]["WantedBy"] == "default.target"
        assert "User" not in service
        raw = "\n".join(unit[section][key] for section in unit.sections() for key in unit[section])
        for forbidden in ("OPENAI_API_KEY", "EnvironmentFile", "DISPLAY=", "WAYLAND_DISPLAY=",
                          "MIRROR_AUDIO_", "MIRROR_LED_", "MIRROR_PIHOLE_", "MIRROR_WAKE_MODEL_DIR",
                          "Pi-hole", "password", "secret", "sudo", "Restart=always"):
            assert forbidden not in raw
    assert dashboard["Service"]["ExecStart"] == "/home/bryce/magic_mirror_v2/scripts/run_pi.sh"
    assert "ExecStartPre" not in dashboard["Service"]
    assert all(key not in dashboard["Unit"] for key in ("Wants", "Requires", "After", "PartOf"))
    assert voice["Unit"]["Wants"] == "pipewire.service wireplumber.service"
    assert voice["Unit"]["After"] == "pipewire.service wireplumber.service"
    assert "magic-mirror-dashboard" not in str(dict(voice["Unit"]))
    assert voice["Service"]["ExecStartPre"].endswith(
        "python -m magic_mirror.assistant.service_ready --wait-seconds 60")
    assert voice["Service"]["ExecStart"].endswith(
        "python -m magic_mirror.assistant.wake_cli --start")
    assert voice["Service"]["Environment"] == "MIRROR_LOG_LEVEL=INFO"
    assert voice["Service"]["TimeoutStartSec"] == "90s"
    ignored = (ROOT / ".gitignore").read_text()
    assert ".env" in ignored and ".private/" in ignored


def test_audio_preflight_checks_both_devices_without_opening_streams(monkeypatch):
    checked = []

    class FakeAudio:
        def __init__(self, settings):
            assert settings.audio_input_device == "pipewire"

        def selected(self, direction):
            checked.append(direction)

    monkeypatch.setattr(service_ready, "AudioManager", FakeAudio)
    assert service_ready.audio_ready(Settings(audio_input_device="pipewire"))
    assert checked == ["input", "output"]

    def disconnected(_direction):
        raise DeviceError("Private source detail")

    FakeAudio.selected = lambda self, direction: disconnected(direction)
    assert not service_ready.audio_ready(Settings(audio_input_device="pipewire"))


def test_audio_preflight_wait_is_bounded_and_recovers_from_late_routing():
    now = [0.0]
    checks = []

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    def check(_settings):
        checks.append(now[0])
        return len(checks) == 3

    assert service_ready.wait_for_audio(Settings(), 5, check=check, clock=clock, sleep=sleep)
    assert checks == [0, 2, 4]
    now[:] = [0.0]
    assert not service_ready.wait_for_audio(Settings(), 3, check=lambda _: False,
                                            clock=clock, sleep=sleep)
    assert now[0] == 3


def test_audio_preflight_failure_does_not_log_credentials(monkeypatch, capsys):
    class LocalSettings:
        @staticmethod
        def from_env():
            return Settings(wake_enabled=True, openai_api_key="private-test-only-key")

    monkeypatch.setattr(service_ready, "Settings", LocalSettings)
    monkeypatch.setattr(service_ready, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(service_ready, "wait_for_audio", lambda *args, **kwargs: False)
    assert service_ready.main(["--wait-seconds", "0"]) == 1
    output = capsys.readouterr()
    assert "route unavailable" in output.err
    assert "private-test-only-key" not in output.err + output.out


def test_install_is_inert_then_activate_and_rollback_are_explicit(tmp_path, monkeypatch):
    destination = tmp_path / "user"
    project = tmp_path / "project"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    (project / ".env").write_text("# local test placeholder\n")
    (project / ".env").chmod(0o600)
    calls = []

    def systemctl(*args):
        calls.append(args)

    services.install_units(destination, systemctl=systemctl)
    assert calls == [("daemon-reload",)]
    assert sorted(path.name for path in destination.iterdir()) == sorted(services.UNITS.values())
    monkeypatch.setattr(services, "_managed_pids", lambda names: set())
    services.activate_units(destination, "both", project=project, systemctl=systemctl,
                            instances=lambda **_: {"dashboard": [], "voice": []})
    assert calls[-2:] == [("enable", "--now", services.UNITS["dashboard"]),
                          ("enable", "--now", services.UNITS["voice"])]
    services.rollback_units(destination, "both", systemctl=systemctl)
    assert calls[-3:] == [("disable", "--now", services.UNITS["dashboard"]),
                          ("disable", "--now", services.UNITS["voice"]), ("daemon-reload",)]
    assert list(destination.iterdir()) == []


def test_activation_refuses_manual_owner_or_exposed_env(tmp_path, monkeypatch):
    destination = tmp_path / "user"
    project = tmp_path / "project"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    (project / ".env").write_text("# local test placeholder\n")
    (project / ".env").chmod(0o600)
    calls = []
    services.install_units(destination, systemctl=lambda *args: None)
    monkeypatch.setattr(services, "_managed_pids", lambda names: set())
    with pytest.raises(services.ServiceSetupError, match="Manual voice process"):
        services.activate_units(destination, "voice", project=project,
                                systemctl=lambda *args: calls.append(args),
                                instances=lambda **_: {"dashboard": [], "voice": [1234]})
    assert calls == []
    (project / ".env").chmod(0o644)
    with pytest.raises(services.ServiceSetupError, match="private"):
        services.activate_units(destination, "voice", project=project,
                                systemctl=lambda *args: calls.append(args))
    assert calls == []


def test_manual_process_detection_is_scoped_to_checkout_and_excludes_managed(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, module, cwd in ((100, "magic_mirror", services.PROJECT),
                             (101, "magic_mirror.assistant.wake_cli", services.PROJECT),
                             (102, "magic_mirror", tmp_path),
                             (103, "", services.PROJECT),
                             (104, "magic_mirror.assistant.voice_cli", services.PROJECT)):
        entry = proc / str(pid)
        entry.mkdir()
        (entry / "cwd").symlink_to(cwd)
        command = (f"python\0-m\0{module}\0--start\0" if module else
                   "bash\0./scripts/run_pi.sh\0")
        (entry / "cmdline").write_bytes(command.encode())
    found = services.manual_instances(proc=proc, uid=os.getuid(), managed={100})
    assert sorted(found["dashboard"]) == [103]
    assert sorted(found["voice"]) == [101, 104]


def test_install_refuses_unrelated_existing_unit(tmp_path):
    target = tmp_path / services.UNITS["voice"]
    target.write_text("[Service]\nExecStart=/bin/true\n")
    calls = []
    with pytest.raises(services.ServiceSetupError, match="differs"):
        services.install_units(tmp_path, systemctl=lambda *args: calls.append(args))
    assert calls == []
    assert target.read_text().startswith("[Service]")
    with pytest.raises(services.ServiceSetupError, match="not a recognized"):
        services.rollback_units(tmp_path, "voice", systemctl=lambda *args: calls.append(args))
    assert calls == []


def test_installing_one_service_does_not_install_or_start_the_other(tmp_path):
    calls = []
    services.install_units(tmp_path, "dashboard", systemctl=lambda *args: calls.append(args))
    assert sorted(path.name for path in tmp_path.iterdir()) == [services.UNITS["dashboard"]]
    assert calls == [("daemon-reload",)]
