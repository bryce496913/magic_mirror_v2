"""Private, bounded, read-only samples of the existing Pi services."""

import argparse
import fcntl
import json
import math
import os
import signal
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

import psutil


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / ".private" / "soak"
UNITS = {"dashboard": "magic-mirror-dashboard.service", "voice": "magic-mirror-voice.service"}
DEPENDENCIES = {"pihole": ("pihole-FTL.service", False),
                "pipewire": ("pipewire.service", True),
                "wireplumber": ("wireplumber.service", True)}
HEALTH_URL = "http://127.0.0.1:5000/health"
MAX_OUTPUT = 4096


class SoakError(RuntimeError):
    """A private log or monitoring state cannot be used safely."""


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _epoch(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SoakError("Soak directory must be owned by this user and mode 0700")
    return path


def _private_open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise SoakError("Soak files must be private regular files")
    return fd


def _command(args):
    try:
        result = subprocess.run(args, capture_output=True, timeout=2, check=False)
        if result.returncode != 0 or len(result.stdout) > MAX_OUTPUT:
            return None
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None


def _fields(raw):
    if raw is None:
        return {}
    fields = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ActiveState", "MainPID", "NRestarts"}:
            fields[key] = value.strip()
    return fields


def _integer(value, *, positive=False):
    try:
        number = int(value)
        return number if number >= (1 if positive else 0) else None
    except (TypeError, ValueError):
        return None


def _state(value):
    return value if value in {"active", "inactive", "failed", "activating", "deactivating"} else None


def _service(name, *, user, command=_command):
    args = ["systemctl"] + (["--user"] if user else []) + [
        "show", name, "--property=ActiveState,MainPID,NRestarts"]
    fields = _fields(command(args))
    return {"state": _state(fields.get("ActiveState")),
            "pid": _integer(fields.get("MainPID"), positive=True),
            "restarts": _integer(fields.get("NRestarts"))}


def _process(key, pid, now, previous, boot, *, factory=psutil.Process):
    empty = {"rss_bytes": None, "cpu_seconds": None, "cpu_percent": None, "uptime_seconds": None}
    if pid is None:
        return empty
    try:
        proc = factory(pid)
        cpu = proc.cpu_times()
        cpu_seconds = float(cpu.user + cpu.system)
        created = float(proc.create_time())
        rss = int(proc.memory_info().rss)
        if not math.isfinite(cpu_seconds) or not math.isfinite(created) or rss < 0:
            return empty
    except (OSError, ValueError, psutil.Error):
        return empty
    percent = None
    if previous and previous.get("boot_time") == boot:
        old = previous.get("services", {}).get(key, {})
        if old.get("pid") == pid:
            old_cpu = old.get("cpu_seconds")
            old_at = _epoch(previous.get("timestamp"))
            if isinstance(old_cpu, (int, float)) and old_at is not None and now > old_at:
                percent = round(max(0.0, 100 * (cpu_seconds - old_cpu) / (now - old_at)), 1)
    return {"rss_bytes": rss, "cpu_seconds": round(cpu_seconds, 3),
            "cpu_percent": percent, "uptime_seconds": round(max(0.0, now - created), 1)}


def _health(*, opener=None, clock=time.monotonic):
    opener = opener or build_opener(ProxyHandler({}))
    start = clock()
    try:
        with opener.open(HEALTH_URL, timeout=2) as response:
            raw = response.read(513)
            healthy = (response.status == 200 and len(raw) <= 512 and
                       json.loads(raw) == {"status": "healthy"})
            reason = None if healthy else "bad_response"
    except (HTTPError, URLError, TimeoutError, OSError):
        healthy, reason = False, "request_failed"
    except (ValueError, UnicodeError):
        healthy, reason = False, "invalid_json"
    return {"healthy": healthy, "response_ms": round(max(0.0, (clock() - start) * 1000), 1),
            "error": reason}


def collect(*, previous=None, now=None, command=_command, process_factory=psutil.Process,
            health=_health, system=psutil):
    """Use fixed allowlisted fields; never capture provider output or transcripts."""
    now = time.time() if now is None else now
    try:
        boot = round(float(system.boot_time()), 1)
    except (OSError, ValueError, psutil.Error):
        boot = None
    services = {}
    for key, unit in UNITS.items():
        item = _service(unit, user=True, command=command)
        item.update(_process(key, item["pid"], now, previous, boot, factory=process_factory))
        services[key] = item
    dependencies = {key: _service(unit, user=user, command=command)["state"]
                    for key, (unit, user) in DEPENDENCIES.items()}
    try:
        mem = system.virtual_memory()
        swap = system.swap_memory()
        load = os.getloadavg()
        resources = {"load_1m": round(load[0], 2), "load_5m": round(load[1], 2),
                     "available_bytes": int(mem.available), "swap_used_bytes": int(swap.used)}
    except (OSError, ValueError, psutil.Error):
        resources = {"load_1m": None, "load_5m": None,
                     "available_bytes": None, "swap_used_bytes": None}
    try:
        dashboard = health()
    except (OSError, ValueError, TimeoutError):
        dashboard = {"healthy": None, "response_ms": None, "error": "measurement_unavailable"}
    return {"timestamp": _utc(now), "boot_time": boot, "services": services,
            "dependencies": dependencies, "health": dashboard, "system": resources}


def _state_file(directory, *, duration, interval, now):
    path = directory / "state.json"
    value = {"started_at": _utc(now), "duration_seconds": duration, "interval_seconds": interval,
             "ended_at": None}
    try:
        fd = _private_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        fd = _private_open(path, os.O_RDONLY)
        if os.fstat(fd).st_size > 4096:
            os.close(fd)
            raise SoakError("Soak state exceeds the supported size")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            try:
                value = json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise SoakError("Soak state is malformed; preserve it for review") from exc
        _validate_state(value)
        if value["duration_seconds"] != duration or value["interval_seconds"] != interval:
            raise SoakError("Soak state is incompatible; preserve it for review")
        return value
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    return value


def _validate_state(value):
    if (not isinstance(value, dict) or _epoch(value.get("started_at")) is None or
            type(value.get("duration_seconds")) is not int or
            type(value.get("interval_seconds")) is not int or
            value["duration_seconds"] <= 0 or value["interval_seconds"] <= 0 or
            value.get("ended_at") is not None and _epoch(value["ended_at"]) is None):
        raise SoakError("Soak state is malformed; preserve it for review")
    return _epoch(value["started_at"]), value["duration_seconds"], value["interval_seconds"]


def _end_state(directory, state, when):
    state = {**state, "ended_at": _utc(when)}
    tmp = directory / "state.tmp"
    fd = _private_open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(state, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, directory / "state.json")


def append_sample(directory, sample, *, max_bytes):
    line = (json.dumps(sample, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(line) > max_bytes:
        raise SoakError("A diagnostic sample exceeds the configured log bound")
    path = directory / "samples.jsonl"
    fd = _private_open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    if os.fstat(fd).st_size + len(line) > max_bytes:
        os.close(fd)
        os.replace(path, directory / "samples.jsonl.1")
        fd = _private_open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    with os.fdopen(fd, "wb") as stream:
        stream.write(line)


def read_samples(directory):
    samples, malformed = [], 0
    for path in (directory / "samples.jsonl.1", directory / "samples.jsonl"):
        if not path.exists():
            continue
        fd = _private_open(path, os.O_RDONLY)
        if os.fstat(fd).st_size > 32 * 1024 * 1024:
            os.close(fd)
            raise SoakError("Soak log exceeds the maximum supported size")
        with os.fdopen(fd, "rb") as stream:
            for line in stream:
                if len(line) > 16384:
                    malformed += 1
                    continue
                try:
                    item = json.loads(line)
                    if (not isinstance(item, dict) or _epoch(item.get("timestamp")) is None or
                            any(not isinstance(item.get(key), dict)
                                for key in ("services", "health", "dependencies", "system")) or
                            any(not isinstance(item["services"].get(key), dict) for key in UNITS)):
                        raise ValueError
                    samples.append(item)
                except (ValueError, UnicodeError):
                    malformed += 1
    return samples, malformed


def _metric(values):
    safe = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0]
    return {"typical": round(median(safe), 1), "max": round(max(safe), 1)} if safe else None


def summarize(state, samples, *, malformed=0, now=None):
    now = time.time() if now is None else now
    start, duration, interval = _validate_state(state)
    samples = [item for item in samples if isinstance(item, dict) and
               _epoch(item.get("timestamp")) is not None and
               all(isinstance(item.get(key), dict)
                   for key in ("services", "health", "dependencies", "system")) and
               all(isinstance(item["services"].get(key), dict) for key in UNITS)]
    end = min(now, start + duration)
    elapsed = max(0, end - start)
    expected = min(math.ceil(duration / interval), math.ceil(elapsed / interval))
    slots = {int((_epoch(item.get("timestamp")) - start) // interval)
             for item in samples if _epoch(item.get("timestamp")) is not None and
             start <= _epoch(item.get("timestamp")) < end}
    missing = max(0, expected - len(slots))
    ordered = sorted((item for item in samples if _epoch(item.get("timestamp")) is not None),
                     key=lambda item: item["timestamp"])
    report = {"duration_hours": round(elapsed / 3600, 2), "planned_hours": round(duration / 3600, 2),
              "samples": len(ordered), "expected": expected, "missing": missing,
              "malformed": malformed, "services": {}, "health_failures": 0,
              "health_unknown": 0, "dependencies": {}, "assessment": "INCONCLUSIVE"}
    confirmed, uncertain = False, malformed > 0 or missing > 0 or elapsed < duration
    for key in UNITS:
        rows = [item.get("services", {}).get(key, {}) for item in ordered]
        inactive = sum(row.get("state") not in ("active", None) for row in rows)
        unknown = sum(row.get("state") is None for row in rows)
        restarts, pid_changes = 0, 0
        for old_sample, new_sample, old, new in zip(ordered, ordered[1:], rows, rows[1:]):
            if old_sample.get("boot_time") != new_sample.get("boot_time"):
                continue
            before, after = old.get("restarts"), new.get("restarts")
            if isinstance(before, int) and isinstance(after, int) and after > before:
                restarts += after - before
            if old.get("pid") and new.get("pid") and old["pid"] != new["pid"]:
                pid_changes += 1
        cpu_unknown = sum(
            new.get("cpu_percent") is None
            for old_sample, new_sample, old, new in zip(ordered, ordered[1:], rows, rows[1:])
            if old_sample.get("boot_time") == new_sample.get("boot_time")
            and old.get("pid") and old.get("pid") == new.get("pid"))
        rss = [row.get("rss_bytes") for row in rows
               if isinstance(row.get("rss_bytes"), int) and not isinstance(row.get("rss_bytes"), bool)]
        baseline = peak = trend = None
        leak = False
        if rss:
            width = max(1, len(rss) // 4)
            baseline = int(median(rss[:width]))
            peak = max(rss)
            trend = int(median(rss[-width:]) - baseline)
            leak = len(rss) >= 8 and trend > max(64 * 1024 * 1024, baseline * 0.25)
        report["services"][key] = {"inactive": inactive, "unknown": unknown,
                                    "automatic_restarts": restarts, "pid_changes": pid_changes,
                                    "restart_counter_unknown": sum(row.get("restarts") is None for row in rows),
                                    "cpu_percent": _metric(row.get("cpu_percent") for row in rows),
                                    "cpu_unknown": cpu_unknown,
                                    "rss_baseline_bytes": baseline, "rss_peak_bytes": peak,
                                    "rss_trend_bytes": trend, "rss_unknown": len(rows) - len(rss),
                                    "potential_leak": leak}
        confirmed |= inactive > 0 or restarts > 0
        uncertain |= (unknown > 0 or pid_changes > restarts or leak or len(rss) != len(rows) or
                      report["services"][key]["restart_counter_unknown"] > 0 or
                      report["services"][key]["cpu_percent"] is None or cpu_unknown > 0)
    for item in ordered:
        health = item.get("health", {}).get("healthy")
        report["health_failures"] += health is False
        report["health_unknown"] += health is not True and health is not False
    confirmed |= report["health_failures"] > 0
    uncertain |= report["health_unknown"] > 0
    for key in DEPENDENCIES:
        states = [item.get("dependencies", {}).get(key) for item in ordered]
        outage = sum(value not in ("active", None) for value in states)
        unknown = sum(value is None for value in states)
        report["dependencies"][key] = {"outages": outage, "unknown": unknown}
        confirmed |= outage > 0
        uncertain |= unknown > 0
    report["system_load"] = _metric(item.get("system", {}).get("load_1m") for item in ordered)
    report["available_memory"] = _metric(item["system"].get("available_bytes") for item in ordered)
    report["swap_used"] = _metric(item["system"].get("swap_used_bytes") for item in ordered)
    report["health_response_ms"] = _metric(item["health"].get("response_ms") for item in ordered)
    report["resource_unknown"] = sum(
        item["system"].get(key) is None for item in ordered
        for key in ("load_1m", "available_bytes", "swap_used_bytes"))
    report["response_time_unknown"] = sum(
        item["health"].get("response_ms") is None for item in ordered)
    uncertain |= (report["resource_unknown"] > 0 or report["response_time_unknown"] > 0 or
                  any(report[key] is None for key in
                      ("system_load", "available_memory", "swap_used", "health_response_ms")))
    report["assessment"] = "FAIL" if confirmed else "INCONCLUSIVE" if uncertain else "PASS"
    return report


def format_report(report):
    lines = [f"Soak duration: {report['duration_hours']} / {report['planned_hours']} hours",
             f"Samples: {report['samples']} collected, {report['expected']} expected, "
             f"{report['missing']} missing, {report['malformed']} malformed"]
    for key, row in report["services"].items():
        cpu = row["cpu_percent"] or {}
        lines.append(f"{key}: {row['automatic_restarts']} automatic restarts, "
                     f"{row['pid_changes']} PID changes, {row['inactive']} inactive, {row['unknown']} unknown; "
                     f"CPU typical/max {cpu.get('typical')}/{cpu.get('max')}%, "
                     f"{row['cpu_unknown']} unavailable intervals; "
                     f"RSS baseline/peak/trend {row['rss_baseline_bytes']}/{row['rss_peak_bytes']}/"
                     f"{row['rss_trend_bytes']} bytes, {row['rss_unknown']} unknown; "
                     f"potential leak={row['potential_leak']}")
    response = report["health_response_ms"] or {}
    lines.append(f"Dashboard health: {report['health_failures']} failures, {report['health_unknown']} unknown; "
                 f"response typical/max {response.get('typical')}/{response.get('max')} ms, "
                 f"{report['response_time_unknown']} unavailable timings")
    for key, row in report["dependencies"].items():
        lines.append(f"{key}: {row['outages']} outages, {row['unknown']} unknown")
    load = report["system_load"] or {}
    lines.append(f"System one-minute load typical/max: {load.get('typical')}/{load.get('max')}")
    available = report["available_memory"] or {}
    swap = report["swap_used"] or {}
    lines.append(f"Available memory typical/max: {available.get('typical')}/{available.get('max')} bytes; "
                 f"swap used typical/max: {swap.get('typical')}/{swap.get('max')} bytes; "
                 f"{report['resource_unknown']} unavailable resource measurements")
    lines.append(f"Assessment: {report['assessment']} (unexplained PID changes and potential leaks need review)")
    return "\n".join(lines)


def run(directory, *, duration, interval, max_bytes, clock=time.time, stopped=None, sampler=collect):
    directory = private_dir(directory)
    stopped = stopped or threading.Event()
    fd = _private_open(directory / "owner.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SoakError("Another soak monitor already owns this directory") from exc
        state = _state_file(directory, duration=duration, interval=interval, now=clock())
        started = _epoch(state["started_at"])
        deadline = started + duration
        previous = read_samples(directory)[0]
        previous = previous[-1] if previous else None
        while not stopped.is_set():
            now = clock()
            if now >= deadline:
                _end_state(directory, state, deadline)
                return
            slot = int(max(0, now - started) // interval)
            old_slot = int((_epoch(previous["timestamp"]) - started) // interval) if previous else -1
            if slot > old_slot:
                sample = sampler(previous=previous, now=now)
                append_sample(directory, sample, max_bytes=max_bytes)
                previous = sample
            next_at = min(deadline, started + (slot + 1) * interval)
            stopped.wait(max(0.01, next_at - clock()))
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private, read-only Magic Mirror reliability soak")
    parser.add_argument("action", choices=("run", "report", "status"))
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--hours", type=int, default=72)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--max-log-mib", type=int, default=4)
    args = parser.parse_args(argv)
    if not 1 <= args.hours <= 720 or not 30 <= args.interval_seconds <= 3600 or not 1 <= args.max_log_mib <= 32:
        parser.error("hours, interval or log limit outside safe bounds")
    try:
        directory = private_dir(args.directory)
        if args.action == "run":
            stopped = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stopped.set())
            signal.signal(signal.SIGINT, lambda *_: stopped.set())
            run(directory, duration=args.hours * 3600, interval=args.interval_seconds,
                max_bytes=args.max_log_mib * 1024 * 1024, stopped=stopped)
        else:
            path = directory / "state.json"
            fd = _private_open(path, os.O_RDONLY)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                state = json.load(stream)
            samples, malformed = read_samples(directory)
            report = summarize(state, samples, malformed=malformed)
            if args.action == "report":
                print(format_report(report))
            else:
                print(f"Soak started {state['started_at']}; "
                      f"ended {state.get('ended_at') or 'not yet'}; "
                      f"samples {report['samples']}; assessment {report['assessment']}")
    except (OSError, ValueError, SoakError):
        print("Soak data unavailable or unsafe; inspect private directory and monitoring service", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
