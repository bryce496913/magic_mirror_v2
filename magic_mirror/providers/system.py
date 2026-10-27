"""Local Raspberry Pi metrics from read-only system interfaces."""

import time

import psutil


def read_system():
    temperature = None
    try:
        sensors = psutil.sensors_temperatures()
        for entries in sensors.values():
            if entries:
                temperature = entries[0].current
                break
    except (AttributeError, OSError, RuntimeError):
        pass
    if temperature is None:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as sensor:
                temperature = int(sensor.read().strip()) / 1000
        except (OSError, ValueError):
            pass

    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_percent": round(psutil.cpu_percent(interval=0.1), 1),
        "cpu_temperature_c": round(temperature, 1) if temperature is not None else None,
        "ram_percent": round(memory.percent, 1),
        "ram_used_gb": round(memory.used / 1024**3, 1),
        "ram_total_gb": round(memory.total / 1024**3, 1),
        "disk_percent": round(disk.percent, 1),
        "uptime_seconds": max(0, int(time.time() - psutil.boot_time())),
    }
