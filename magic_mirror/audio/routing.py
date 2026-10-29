"""Read-only verification of the PipeWire default physical input."""

import re
import subprocess

from .devices import DeviceError


_NODE_NAME = re.compile(r'^\s*\*?\s*node\.name\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


def pipewire_default_source(timeout):
    """Resolve the current WirePlumber default source without changing routing."""
    try:
        result = subprocess.run(["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"],
                                capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeviceError("Cannot verify PipeWire default input; check wpctl and the user session") from exc
    if result.returncode != 0:
        raise DeviceError("Cannot verify PipeWire default input; check wpctl and the user session")
    matches = _NODE_NAME.findall(result.stdout)
    if len(matches) != 1:
        raise DeviceError("PipeWire default input has no unambiguous node.name")
    return matches[0]
