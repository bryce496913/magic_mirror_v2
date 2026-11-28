#!/usr/bin/env python3
"""Explicit install, activation, and rollback of the Pi user's systemd units."""

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path


PROJECT = Path("/home/bryce/magic_mirror_v2")
UNITS = {
    "dashboard": "magic-mirror-dashboard.service",
    "voice": "magic-mirror-voice.service",
}
SOURCE = Path(__file__).resolve().parents[1] / "systemd" / "user"


class ServiceSetupError(RuntimeError):
    """A local setup condition requires explicit human correction."""


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def _is_active(name: str) -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", name],
                          check=False).returncode == 0


def _is_enabled(name: str) -> bool:
    return subprocess.run(["systemctl", "--user", "is-enabled", "--quiet", name],
                          check=False).returncode == 0


def _unit_directory() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _selected(target: str) -> dict[str, str]:
    return UNITS if target == "both" else {target: UNITS[target]}


def _matching_unit(path: Path, name: str) -> bool:
    if path.name != name or path.is_symlink() or not path.is_file() or name not in UNITS.values():
        return False
    try:
        return path.read_bytes() == (SOURCE / name).read_bytes()
    except OSError:
        return False


def install_units(destination: Path, target: str = "both", *, systemctl=_systemctl) -> None:
    """Copy only; never enable or start during installation."""
    names = _selected(target)
    for name in names.values():
        target = destination / name
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or target.read_bytes() != (SOURCE / name).read_bytes():
                raise ServiceSetupError(f"Existing {name} differs; review it before installation")
    destination.mkdir(parents=True, exist_ok=True)
    for name in names.values():
        target = destination / name
        if not target.exists():
            target.write_bytes((SOURCE / name).read_bytes())
            target.chmod(0o644)
    try:
        systemctl("daemon-reload")
    except subprocess.CalledProcessError as exc:
        raise ServiceSetupError(
            "User unit files were copied but daemon-reload failed; check the user systemd manager "
            "and run `systemctl --user daemon-reload`. No service was enabled or started") from exc


def _managed_pids(names: dict[str, str]) -> set[int]:
    managed = set()
    for name in names.values():
        result = subprocess.run(["systemctl", "--user", "show", "--property=MainPID",
                                 "--value", name], capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip().isdigit():
            managed.add(int(result.stdout.strip()))
    return managed


def manual_instances(*, proc: Path = Path("/proc"), uid: int | None = None,
                     managed: set[int] | None = None) -> dict[str, list[int]]:
    """Conservatively find same-user foreground launches of the application."""
    uid = os.getuid() if uid is None else uid
    managed = managed or set()
    found: dict[str, list[int]] = {kind: [] for kind in UNITS}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in managed:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        try:
            cwd = Path(os.readlink(entry / "cwd"))
        except OSError:
            cwd = None  # module invocations remain detectable without cwd access
        for index, arg in enumerate(argv[:-1]):
            if arg == "-m" and argv[index + 1] == "magic_mirror":
                found["dashboard"].append(pid)
            elif (arg == "-m" and argv[index + 1] in {
                    "magic_mirror.assistant.wake_cli", "magic_mirror.assistant.voice_cli"}
                    and "--start" in argv):
                found["voice"].append(pid)
        if any(arg == str(PROJECT / "scripts" / "run_pi.sh") or
               (arg.endswith("/scripts/run_pi.sh") and cwd == PROJECT) for arg in argv):
            found["dashboard"].append(pid)
    return found


def _verify_installed(destination: Path, names: dict[str, str], *, allow_missing: bool = False) -> None:
    for name in names.values():
        path = destination / name
        if allow_missing and not path.exists() and not path.is_symlink():
            continue
        if not _matching_unit(path, name):
            raise ServiceSetupError(f"{name} is missing or not a recognized Magic Mirror unit")


def activate_units(destination: Path, target: str, *, project: Path = PROJECT, systemctl=_systemctl,
                   instances=manual_instances, is_active=_is_active,
                   is_enabled=_is_enabled) -> None:
    """Explicit enablement, guarded against duplicate manual processes."""
    names = _selected(target)
    _verify_installed(destination, names)
    private_env = project / ".env"
    try:
        info = private_env.stat()
    except OSError as exc:
        raise ServiceSetupError("Local .env is missing; preserve the existing private configuration") from exc
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not mode & stat.S_IRUSR or mode & 0o077:
        raise ServiceSetupError("Local .env must be owned by this user, readable, and private")
    if not (project / ".venv" / "bin" / "python").is_file():
        raise ServiceSetupError("Project .venv is missing")
    found = instances(managed=_managed_pids(names))
    for kind in names:
        if found[kind]:
            raise ServiceSetupError(f"Manual {kind} process still running (PID {found[kind][0]}); stop it first")
    for name in names.values():
        was_active = is_active(name)
        was_enabled = is_enabled(name)
        try:
            # Starting first leaves the existing enablement unchanged if startup fails.
            systemctl("start", name)
        except subprocess.CalledProcessError as exc:
            raise ServiceSetupError(
                f"{name} failed to start; check `systemctl --user status {name}` and "
                f"`journalctl --user -u {name} -n 50`. Earlier services may be running; "
                "inspect both before retrying or rolling back") from exc
        try:
            systemctl("enable", name)
        except subprocess.CalledProcessError as exc:
            cleanup = []
            if not was_active:
                try:
                    systemctl("stop", name)
                    cleanup.append("Stopped the newly started unit.")
                except subprocess.CalledProcessError:
                    cleanup.append("Could not stop the newly started unit; inspect its status immediately.")
            else:
                cleanup.append("The previously active unit was left running.")
            if not was_enabled:
                try:
                    systemctl("disable", name)
                    cleanup.append("Removed any partial boot enablement.")
                except subprocess.CalledProcessError:
                    cleanup.append("Could not clear partial boot enablement; inspect is-enabled immediately.")
            raise ServiceSetupError(
                f"{name} started but could not be enabled. {' '.join(cleanup)} Check "
                f"`systemctl --user status {name}` and `journalctl --user -u {name} -n 50`; "
                "earlier services may be running") from exc


def rollback_units(destination: Path, target: str, *, systemctl=_systemctl) -> None:
    """Stop and disable selected services, then remove only recognized units."""
    names = _selected(target)
    _verify_installed(destination, names, allow_missing=True)
    present = {kind: name for kind, name in names.items()
               if (destination / name).exists() or (destination / name).is_symlink()}
    for name in present.values():
        try:
            systemctl("disable", "--now", name)
        except subprocess.CalledProcessError as exc:
            raise ServiceSetupError(
                f"Could not stop/disable {name}; no unit files were removed. "
                "Earlier units may already be stopped. Check `systemctl --user status` and "
                "the user journal, then retry rollback") from exc
    # Recheck after systemctl calls so a replaced unit cannot be deleted.
    _verify_installed(destination, present)
    failed = []
    for name in present.values():
        try:
            (destination / name).unlink()
        except OSError:
            failed.append(name)
    try:
        systemctl("daemon-reload")
    except subprocess.CalledProcessError as exc:
        raise ServiceSetupError(
            "Unit files may have been removed but daemon-reload failed; run "
            "`systemctl --user daemon-reload` and inspect unit status") from exc
    if failed:
        raise ServiceSetupError(
            "Could not remove " + ", ".join(failed) + "; services were disabled. "
            "Check user-unit directory permissions and retry rollback")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Magic Mirror user-level systemd services")
    parser.add_argument("action", choices=("install", "activate", "rollback"))
    parser.add_argument("target", nargs="?", choices=(*UNITS, "both"), default="both")
    args = parser.parse_args(argv)
    if os.getuid() != 1000 or Path.home() != Path("/home/bryce") or SOURCE.parents[1] != PROJECT:
        print("Run as bryce (UID 1000) from /home/bryce/magic_mirror_v2", file=sys.stderr)
        return 1
    try:
        destination = _unit_directory()
        if args.action == "install":
            install_units(destination, args.target)
            print("User units installed but not enabled or started")
        elif args.action == "activate":
            activate_units(destination, args.target)
            print("Selected user services enabled and started")
        else:
            rollback_units(destination, args.target)
            print("Selected user services stopped, disabled, and removed")
    except (ServiceSetupError, subprocess.CalledProcessError) as exc:
        print(f"User service setup failed: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("User service setup failed: check local file permissions and the user systemd manager; "
              "inspect service status before retrying or rolling back", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
