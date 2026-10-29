"""PortAudio device discovery without fallback to an unintended output."""

from dataclasses import dataclass


class DeviceError(RuntimeError):
    """The requested audio device cannot be selected safely."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    hostapi: str
    input_channels: int
    output_channels: int
    default_rate: float

    def label(self):
        return f"{self.name} ({self.hostapi}, PortAudio index {self.index})"


def list_devices(backend):
    """Return current PortAudio properties; indices are never persisted."""
    try:
        hostapis = backend.query_hostapis()
        devices = backend.query_devices()
        return [AudioDevice(index, str(item["name"]), str(hostapis[item["hostapi"]]["name"]),
                            int(item["max_input_channels"]), int(item["max_output_channels"]),
                            float(item["default_samplerate"]))
                for index, item in enumerate(devices)]
    except Exception as exc:
        raise DeviceError("Unable to enumerate PortAudio devices") from exc


def select_device(devices, selector, direction, channels):
    """Prefer one exact name, then one substring; allow HostAPI:name disambiguation."""
    hostapis = {item.hostapi.casefold() for item in devices}
    hostapi, separator, name = selector.partition(":")
    if not separator or hostapi.casefold() not in hostapis:
        hostapi, name = None, selector
    name = name.casefold().strip()
    candidates = [item for item in devices if
                  (hostapi is None or item.hostapi.casefold() == hostapi.casefold()) and
                  name in item.name.casefold()]
    exact = [item for item in candidates if item.name.casefold() == name]
    if exact:
        candidates = exact
    if not candidates:
        raise DeviceError(f"Preferred {direction} device not found; inspect the PortAudio device list")
    capable = [item for item in candidates if
               (item.input_channels if direction == "input" else item.output_channels) >= channels]
    if not capable:
        raise DeviceError(f"Preferred {direction} device lacks {channels} channel(s)")
    if len(capable) != 1:
        raise DeviceError(f"Preferred {direction} device is ambiguous; use HostAPI:name to narrow it")
    return capable[0]


def check_format(backend, device, direction, channels, rate, frame_format):
    check = backend.check_input_settings if direction == "input" else backend.check_output_settings
    try:
        check(device=device.index, channels=channels, samplerate=rate, dtype=frame_format)
    except Exception as exc:
        raise DeviceError(f"{device.label()} does not support {channels} channel(s) at {rate} Hz {frame_format}") from exc
