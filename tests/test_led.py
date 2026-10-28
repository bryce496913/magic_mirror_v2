"""RGB LED behavior is verified with devices that never access GPIO."""

import threading
from types import SimpleNamespace

import pytest

from magic_mirror import create_app
from magic_mirror.config import ConfigurationError, Settings
from magic_mirror.hardware.led import LedError, RGBLedController
from magic_mirror.hardware.led_test import run


class FakeLED:
    def __init__(self, pin, *, active_high, initial_value, events, fail_on=None):
        self.pin = pin
        self.active_high = active_high
        self.value = initial_value
        self.closed = False
        self.events = events
        self.fail_on = fail_on
        events.append(("init", pin, active_high, initial_value))

    def on(self):
        self.events.append(("on", self.pin))
        if self.fail_on == "on":
            raise OSError("simulated GPIO failure")
        self.value = True

    def off(self):
        self.events.append(("off", self.pin))
        if self.fail_on == "off":
            raise OSError("simulated GPIO failure")
        self.value = False

    def close(self):
        self.events.append(("close", self.pin))
        self.closed = True


def fake_factory(events, devices, *, fail_pin=None, fail_on=None):
    def make(pin, *, active_high, initial_value):
        if pin == fail_pin:
            raise OSError("simulated unavailable GPIO")
        device = FakeLED(pin, active_high=active_high, initial_value=initial_value,
                         events=events, fail_on=fail_on if pin == 27 else None)
        devices.append(device)
        return device
    return make


def test_default_pins_states_order_and_idempotence():
    events, devices = [], []
    led = RGBLedController(Settings(), device_factory=fake_factory(events, devices))
    assert led.state is None
    with led:
        assert events == [("init", 22, True, False), ("init", 27, True, False),
                          ("init", 24, True, False)]
        assert led.state == "off"
        assert led.set_state("idle")
        assert events[-3:] == [("on", 22), ("on", 27), ("on", 24)]
        count = len(events)
        assert led.set_state("idle") is False
        assert len(events) == count
        assert led.set_state("listening")
        assert events[-2:] == [("off", 22), ("off", 24)]
        assert [item.value for item in devices] == [False, True, False]
        assert led.set_state("responding")
        assert events[-2:] == [("off", 27), ("on", 24)]
        assert [item.value for item in devices] == [False, False, True]
        assert led.set_state("off")
        assert [item.value for item in devices] == [False, False, False]
    assert led.state is None
    assert all(device.closed and not device.value for device in devices)
    assert led.close() is False


def test_initial_state_and_active_low_start_from_off():
    events, devices = [], []
    settings = Settings(led_active_high=False, led_initial_state="idle")
    with RGBLedController(settings, device_factory=fake_factory(events, devices)) as led:
        assert events[:3] == [("init", 22, False, False), ("init", 27, False, False),
                              ("init", 24, False, False)]
        assert events[3:6] == [("on", 22), ("on", 27), ("on", 24)]
        assert led.state == "idle"
        assert all(device.value for device in devices)
    assert all(not device.value for device in devices)


@pytest.mark.parametrize("active_high,off_level,on_level", [
    (True, False, True), (False, True, False),
])
def test_gpiozero_mock_pin_polarity(active_high, off_level, on_level):
    from gpiozero import LED
    from gpiozero.pins.mock import MockFactory

    pin_factory = MockFactory()
    def make(pin, *, active_high, initial_value):
        return LED(pin, active_high=active_high, initial_value=initial_value,
                   pin_factory=pin_factory)

    try:
        with RGBLedController(Settings(led_active_high=active_high), device_factory=make) as led:
            assert all(pin_factory.pin(pin).state == off_level for pin in led.pins)
            led.set_state("idle")
            assert all(pin_factory.pin(pin).state == on_level for pin in led.pins)
        assert all(pin_factory.pin(pin).state == off_level for pin in (22, 27, 24))
    finally:
        pin_factory.close()


def test_default_backend_shares_one_lgpio_factory(monkeypatch):
    import gpiozero
    from gpiozero.pins import lgpio

    events, devices, factories = [], [], []
    class FakePinFactory:
        def close(self):
            events.append(("factory-close",))
    def make(pin, *, active_high, initial_value, pin_factory):
        factories.append(pin_factory)
        device = FakeLED(pin, active_high=active_high, initial_value=initial_value,
                         events=events)
        devices.append(device)
        return device

    monkeypatch.setattr(lgpio, "LGPIOFactory", FakePinFactory)
    monkeypatch.setattr(gpiozero, "LED", make)
    with RGBLedController(Settings()) as led:
        assert led.pins == (22, 27, 24)
        assert len(factories) == 3 and factories[0] is factories[1] is factories[2]
    assert events[-1] == ("factory-close",)
    assert all(device.closed for device in devices)


def test_show_individual_channels_and_invalid_state():
    events, devices = [], []
    with RGBLedController(Settings(), device_factory=fake_factory(events, devices)) as led:
        for name, expected in (("red", [True, False, False]),
                               ("green", [False, True, False]),
                               ("blue", [False, False, True])):
            led.show_channel(name)
            assert [device.value for device in devices] == expected
        before = list(events)
        with pytest.raises(ValueError, match="LED state"):
            led.set_state("unknown")
        with pytest.raises(ValueError, match="test channel"):
            led.show_channel("yellow")
        assert events == before


def test_partial_initialization_and_unavailable_gpio_release_devices():
    events, devices = [], []
    led = RGBLedController(Settings(), device_factory=fake_factory(events, devices, fail_pin=27))
    with pytest.raises(LedError, match="GPIO initialization failed"):
        led.open()
    assert len(devices) == 1
    assert devices[0].closed and not devices[0].value
    assert led.state is None
    assert led.close() is False


def test_write_failure_turns_off_and_releases_every_pin():
    events, devices = [], []
    led = RGBLedController(Settings(), device_factory=fake_factory(events, devices, fail_on="on"))
    led.open()
    with pytest.raises(LedError, match="write failed"):
        led.set_state("idle")
    assert led.state is None
    assert all(device.closed and not device.value for device in devices)
    assert led.close() is False
    with pytest.raises(LedError, match="not open"):
        led.set_state("off")


def test_initial_write_failure_releases_every_pin():
    events, devices = [], []
    led = RGBLedController(Settings(led_initial_state="idle"),
                           device_factory=fake_factory(events, devices, fail_on="on"))
    with pytest.raises(LedError, match="write failed"):
        led.open()
    assert all(device.closed and not device.value for device in devices)


def test_cleanup_releases_other_pins_if_one_off_fails():
    events, devices = [], []
    led = RGBLedController(Settings(), device_factory=fake_factory(events, devices, fail_on="off"))
    led.open()
    with pytest.raises(LedError, match="cleanup failed"):
        led.close()
    assert all(device.closed for device in devices)
    assert led.close() is False


def test_concurrent_state_changes_are_serialized():
    events, devices = [], []
    entered, second_started, release = threading.Event(), threading.Event(), threading.Event()
    make = fake_factory(events, devices)

    def factory(pin, **kwargs):
        device = make(pin, **kwargs)
        if pin == 22:
            original_on = device.on
            def on():
                entered.set()
                assert release.wait(timeout=2)
                original_on()
            device.on = on
        return device

    led = RGBLedController(Settings(), device_factory=factory)
    with led:
        first = threading.Thread(target=lambda: led.set_state("idle"))
        def followup():
            second_started.set()
            led.set_state("listening")
        second = threading.Thread(target=followup)
        first.start()
        assert entered.wait(timeout=2)
        second.start()
        assert second_started.wait(timeout=2)
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive() and not second.is_alive()
        assert led.state == "listening"
        assert [device.value for device in devices] == [False, True, False]


def test_cli_interruption_cleans_up_without_flask():
    events, devices = [], []
    def controller_factory(settings):
        return RGBLedController(settings, device_factory=fake_factory(events, devices))
    def interrupt(_duration):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(Settings(), duration=0.1, sleep=interrupt, controller_factory=controller_factory)
    assert all(device.closed and not device.value for device in devices)


def test_disabled_mode_and_dashboard_routes_do_not_claim_gpio():
    settings = Settings(led_enabled=False)
    events, devices = [], []
    led = RGBLedController(settings, device_factory=fake_factory(events, devices))
    app = create_app(settings, led=None)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/health").json == {"status": "healthy"}
    assert app.extensions["magic_mirror_led"] is None
    assert events == []
    with pytest.raises(LedError, match="not open"):
        led.set_state("idle")


@pytest.mark.parametrize("enabled", [False, True])
def test_flask_lifecycle_owns_led_only_when_enabled(monkeypatch, enabled):
    from magic_mirror import __main__ as entry
    events = []
    settings = Settings(led_enabled=enabled)

    class FakeData:
        def start(self):
            events.append("data-start")
        def stop(self):
            events.append("data-stop")
            return True

    class FakeLed:
        def __init__(self, _settings):
            events.append("led-created")
        def open(self):
            events.append("led-open")
        def close(self):
            events.append("led-close")

    class FakeApp:
        def run(self, **_kwargs):
            events.append("flask-run")
            raise KeyboardInterrupt

    monkeypatch.setattr(entry, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entry, "Settings", SimpleNamespace(from_env=lambda: settings))
    monkeypatch.setattr(entry, "configure_logging", lambda _level: None)
    monkeypatch.setattr(entry, "DashboardData", lambda _settings: FakeData())
    monkeypatch.setattr(entry, "RGBLedController", FakeLed)
    monkeypatch.setattr(entry, "create_app", lambda _settings, _data, led: FakeApp())
    monkeypatch.setattr(entry.signal, "signal", lambda *_args: None)
    assert entry.main() == 0
    if enabled:
        assert events == ["led-created", "led-open", "data-start", "flask-run",
                          "data-stop", "led-close"]
    else:
        assert events == ["data-start", "flask-run", "data-stop"]


def test_led_startup_failure_stops_data_and_releases_pins(monkeypatch):
    from magic_mirror import __main__ as entry
    events = []

    class FakeLed:
        def __init__(self, _settings):
            pass
        def open(self):
            events.append("led-open")
            raise LedError("unavailable")
        def close(self):
            events.append("led-close")

    class FakeData:
        def start(self):
            events.append("data-start")
        def stop(self):
            events.append("data-stop")
            return True

    monkeypatch.setattr(entry, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entry, "Settings", SimpleNamespace(from_env=lambda: Settings(led_enabled=True)))
    monkeypatch.setattr(entry, "configure_logging", lambda _level: None)
    monkeypatch.setattr(entry, "DashboardData", lambda _settings: FakeData())
    monkeypatch.setattr(entry, "RGBLedController", FakeLed)
    monkeypatch.setattr(entry, "create_app", lambda _settings, _data, led: object())
    monkeypatch.setattr(entry.signal, "signal", lambda *_args: None)
    assert entry.main() == 1
    assert events == ["led-open", "data-stop", "led-close"]


@pytest.mark.parametrize("values", [
    {"MIRROR_LED_ENABLED": "maybe"},
    {"MIRROR_LED_ACTIVE_HIGH": "yes"},
    {"MIRROR_LED_RED_PIN": "xyz"},
    {"MIRROR_LED_BLUE_PIN": "28"},
    {"MIRROR_LED_GREEN_PIN": "22"},
    {"MIRROR_LED_INITIAL_STATE": "blinking"},
])
def test_invalid_led_settings(values):
    with pytest.raises(ConfigurationError):
        Settings.from_env(values)


def test_led_settings_override_defaults():
    settings = Settings.from_env({"MIRROR_LED_ENABLED": "true", "MIRROR_LED_RED_PIN": "5",
                                  "MIRROR_LED_GREEN_PIN": "6", "MIRROR_LED_BLUE_PIN": "13",
                                  "MIRROR_LED_ACTIVE_HIGH": "false",
                                  "MIRROR_LED_INITIAL_STATE": "listening"})
    assert (settings.led_enabled, settings.led_red_pin, settings.led_green_pin,
            settings.led_blue_pin, settings.led_active_high, settings.led_initial_state) == (
                True, 5, 6, 13, False, "listening")
