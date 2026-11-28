"""Reliability soak tests use fake systemd, processes, HTTP, and time."""

import configparser
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from magic_mirror import soak


def sample(at, *, pid=100, restarts=0, active="active", health=True, rss=100_000_000,
           cpu=2.0, pipewire="active"):
    row = {"state": active, "pid": pid, "restarts": restarts, "rss_bytes": rss,
           "cpu_seconds": 10.0, "cpu_percent": cpu, "uptime_seconds": 100.0}
    return {"timestamp": soak._utc(at), "boot_time": 10.0,
            "services": {"dashboard": dict(row), "voice": dict(row)},
            "dependencies": {"pihole": "active", "pipewire": pipewire,
                             "wireplumber": "active"},
            "health": {"healthy": health, "response_ms": 12.0, "error": None},
            "system": {"load_1m": 0.5, "load_5m": 0.4,
                       "available_bytes": 2_000_000_000, "swap_used_bytes": 0}}


def test_service_status_parsing_and_missing_process():
    def command(args):
        assert args[:2] == ["systemctl", "--user"]
        return "ActiveState=active\nMainPID=456\nNRestarts=2\n"

    assert soak._service("magic-mirror-voice.service", user=True, command=command) == {
        "state": "active", "pid": 456, "restarts": 2}
    assert soak._service("missing", user=False, command=lambda _: None) == {
        "state": None, "pid": None, "restarts": None}
    assert soak._service("bad", user=False, command=lambda _:
                         "ActiveState=surprise\nMainPID=-1\nNRestarts=oops\n") == {
        "state": None, "pid": None, "restarts": None}

    class MissingProcess:
        def __init__(self, _pid):
            raise OSError("gone")

    assert all(value is None for value in soak._process(
        "voice", 456, 100, None, 10, factory=MissingProcess).values())


def test_process_cpu_and_uptime_are_correlated_only_with_same_pid_and_boot():
    class Process:
        def __init__(self, pid):
            assert pid == 456

        def cpu_times(self):
            return SimpleNamespace(user=7.0, system=3.0)

        def create_time(self):
            return 20.0

        def memory_info(self):
            return SimpleNamespace(rss=123_456)

    previous = sample(90, pid=456)
    previous["services"]["voice"]["cpu_seconds"] = 8.0
    metrics = soak._process("voice", 456, 100, previous, 10.0, factory=Process)
    assert metrics == {"rss_bytes": 123_456, "cpu_seconds": 10.0,
                       "cpu_percent": 20.0, "uptime_seconds": 80.0}
    assert soak._process("voice", 456, 100, previous, 11.0, factory=Process)["cpu_percent"] is None
    assert soak._process("voice", 456, 100, sample(90, pid=789), 10.0,
                         factory=Process)["cpu_percent"] is None


def test_health_timeout_invalid_data_and_bounded_response():
    class Response:
        status = 200

        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, limit):
            assert limit == 513
            return self.data

    class Opener:
        def __init__(self, data):
            self.data = data

        def open(self, url, timeout):
            assert url == soak.HEALTH_URL and timeout == 2
            if self.data is None:
                raise TimeoutError
            return Response(self.data)

    for data, expected in ((b'{"status":"healthy"}', True),
                           (b'{"status":"unhealthy"}', False), (b"bad-json", False),
                           (b"x" * 513, False), (None, False)):
        result = soak._health(opener=Opener(data), clock=lambda: 1.0)
        assert result["healthy"] is expected
        assert result["response_ms"] == 0
        assert set(result) == {"healthy", "response_ms", "error"}


def test_collector_uses_fixed_units_and_no_raw_output():
    calls = []

    def command(args):
        calls.append(args)
        return "ActiveState=active\nMainPID=0\nNRestarts=0\nPRIVATE=must-not-appear\n"

    fake_system = SimpleNamespace(boot_time=lambda: 20.0,
                                  virtual_memory=lambda: SimpleNamespace(available=123),
                                  swap_memory=lambda: SimpleNamespace(used=4))
    result = soak.collect(now=100, command=command, system=fake_system,
                          health=lambda: {"healthy": True, "response_ms": 2.0, "error": None})
    assert len(calls) == 5
    assert calls[0][1] == "--user" and "--user" not in calls[2]
    assert result["services"]["voice"]["pid"] is None
    assert result["dependencies"] == {key: "active" for key in soak.DEPENDENCIES}
    assert result["system"]["available_bytes"] == 123
    assert "PRIVATE" not in json.dumps(result)


def test_private_log_rotation_and_malformed_rows(tmp_path):
    directory = soak.private_dir(tmp_path / "private")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    first = sample(100)
    line_size = len((json.dumps(first, separators=(",", ":")) + "\n").encode())
    for index in range(3):
        soak.append_sample(directory, sample(100 + 30 * index), max_bytes=line_size + 1)
    paths = [directory / "samples.jsonl", directory / "samples.jsonl.1"]
    assert all(path.stat().st_size <= line_size + 1 for path in paths)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    rows, malformed = soak.read_samples(directory)
    assert len(rows) == 2 and malformed == 0  # oldest rotation was discarded
    with paths[0].open("ab") as stream:
        stream.write(b"not-json\n")
    assert soak.read_samples(directory)[1] == 1
    with pytest.raises(soak.SoakError, match="exceeds"):
        soak.append_sample(directory, first, max_bytes=50)


def test_private_files_reject_symlinks_and_open_permissions(tmp_path):
    directory = soak.private_dir(tmp_path / "private")
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (directory / "samples.jsonl").symlink_to(outside)
    with pytest.raises(OSError):
        soak.append_sample(directory, sample(100), max_bytes=4096)
    assert outside.read_text() == "untouched"
    (directory / "samples.jsonl").unlink()
    directory.chmod(0o755)
    with pytest.raises(soak.SoakError, match="0700"):
        soak.private_dir(directory)


def test_duration_expiration_stops_collecting_and_resume_does_not_reset_clock(tmp_path):
    now = [100.0]
    observed = []

    class ClockEvent:
        def is_set(self):
            return False

        def wait(self, seconds):
            now[0] += seconds

    def sampler(*, previous, now):
        observed.append(now)
        return sample(now)

    directory = tmp_path / "private"
    soak.run(directory, duration=60, interval=30, max_bytes=4096,
             clock=lambda: now[0], stopped=ClockEvent(), sampler=sampler)
    assert observed == [100, 130]
    rows, malformed = soak.read_samples(directory)
    assert len(rows) == 2 and malformed == 0
    state = soak._state_file(directory, duration=60, interval=30, now=999)
    assert state["started_at"] == soak._utc(100) and state["ended_at"] == soak._utc(160)
    now[0] = 999
    soak.run(directory, duration=60, interval=30, max_bytes=4096,
             clock=lambda: now[0], stopped=ClockEvent(), sampler=sampler)
    assert observed == [100, 130]


def test_interrupted_run_resumes_original_deadline_without_duplicate_slot(tmp_path):
    now = [100.0]
    stopped = SimpleNamespace(is_set=lambda: now[0] >= 100,
                              wait=lambda _seconds: None)
    observed = []

    def sampler(*, previous, now):
        observed.append(now)
        return sample(now)

    directory = tmp_path / "private"
    # An already-stopped monitor creates state without starting a sample.
    soak.run(directory, duration=90, interval=30, max_bytes=4096,
             clock=lambda: now[0], stopped=stopped, sampler=sampler)
    now[0] = 131

    class ClockEvent:
        def is_set(self):
            return False

        def wait(self, seconds):
            now[0] += seconds

    soak.run(directory, duration=90, interval=30, max_bytes=4096,
             clock=lambda: now[0], stopped=ClockEvent(), sampler=sampler)
    assert observed == [131, 160]
    state = soak._state_file(directory, duration=90, interval=30, now=999)
    assert state["started_at"] == soak._utc(100)
    assert state["ended_at"] == soak._utc(190)


def test_report_restarts_missing_samples_outages_leak_and_unknown():
    state = {"started_at": soak._utc(100), "duration_seconds": 120,
             "interval_seconds": 30, "ended_at": soak._utc(220)}
    rows = [sample(100 + i * 30) for i in range(4)]
    complete = soak.summarize(state, rows, now=220)
    assert complete["assessment"] == "PASS"
    assert complete["samples"] == 4 and complete["missing"] == 0
    assert "Assessment: PASS" in soak.format_report(complete)

    missing = soak.summarize(state, rows[:2] + rows[3:], now=220)
    assert missing["missing"] == 1 and missing["assessment"] == "INCONCLUSIVE"
    restarted = [dict(row) for row in rows]
    restarted[2] = sample(160, pid=101, restarts=1)
    restarted[3] = sample(190, pid=101, restarts=1)
    failure = soak.summarize(state, restarted, now=220)
    assert failure["services"]["voice"]["automatic_restarts"] == 1
    assert failure["services"]["voice"]["pid_changes"] == 1
    assert failure["assessment"] == "FAIL"

    rows[1]["health"]["healthy"] = False
    rows[2]["dependencies"]["pipewire"] = "failed"
    outage = soak.summarize(state, rows, now=220)
    assert outage["health_failures"] == 1
    assert outage["dependencies"]["pipewire"]["outages"] == 1
    assert outage["assessment"] == "FAIL"

    unknown = [sample(100 + i * 30) for i in range(4)]
    unknown[1]["services"]["voice"]["state"] = None
    unknown[1]["services"]["voice"]["rss_bytes"] = None
    assert soak.summarize(state, unknown, now=220)["assessment"] == "INCONCLUSIVE"
    unavailable = [sample(100 + i * 30) for i in range(4)]
    unavailable[1]["services"]["voice"]["cpu_percent"] = None
    unavailable[2]["system"]["available_bytes"] = None
    partial = soak.summarize(state, unavailable, now=220)
    assert partial["services"]["voice"]["cpu_unknown"] == 1
    assert partial["resource_unknown"] == 1
    assert partial["assessment"] == "INCONCLUSIVE"
    with pytest.raises(soak.SoakError, match="malformed"):
        soak.summarize({"bad": "state"}, unknown, now=220)

    long_state = {"started_at": soak._utc(100), "duration_seconds": 240,
                  "interval_seconds": 30, "ended_at": soak._utc(340)}
    growing = [sample(100 + i * 30, rss=(100 + 25 * i) * 1024 * 1024) for i in range(8)]
    trend = soak.summarize(long_state, growing, now=340)
    assert trend["services"]["voice"]["potential_leak"]
    assert trend["assessment"] == "INCONCLUSIVE"


def test_malformed_log_is_counted_without_echoing_contents(tmp_path):
    directory = soak.private_dir(tmp_path / "private")
    path = directory / "samples.jsonl"
    path.write_text('SECRET text\n{"timestamp":"bad"}\n')
    path.chmod(0o600)
    rows, malformed = soak.read_samples(directory)
    assert rows == [] and malformed == 2


def test_user_unit_is_opt_in_and_never_controls_production_services():
    path = Path(__file__).resolve().parents[1] / "systemd/user/magic-mirror-soak.service"
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(path)
    assert unit["Service"]["Type"] == "exec"
    assert unit["Service"]["Restart"] == "on-failure"
    assert unit["Service"]["StandardInput"] == "null"
    assert "magic_mirror.soak run --hours 72 --interval-seconds 300" in unit["Service"]["ExecStart"]
    assert unit["Install"]["WantedBy"] == "default.target"
    text = path.read_text()
    for forbidden in ("ExecStartPre", "ExecStop", "Requires=magic-mirror-",
                      "OPENAI_API_KEY", "EnvironmentFile", "sudo", "Restart=always"):
        assert forbidden not in text
