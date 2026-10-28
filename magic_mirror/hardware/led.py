"""Exclusive, thread-safe RGB LED owner. GPIO is acquired only by open()."""

from threading import RLock


class LedError(RuntimeError):
    """The LED cannot be used or safely controlled."""


PATTERNS = {
    "idle": (True, True, True),
    "listening": (False, True, False),
    "responding": (False, False, True),
    "off": (False, False, False),
}
CHANNELS = ("red", "green", "blue")
OFF = PATTERNS["off"]


class RGBLedController:
    """Own three gpiozero LEDs, or injected channel devices in hardware-free tests.

    A single LGPIOFactory is shared by all channels. No device or pin factory is
    created until open() is called. The application must have exactly one owner.
    """

    def __init__(self, settings, device_factory=None):
        self.pins = (settings.led_red_pin, settings.led_green_pin, settings.led_blue_pin)
        self.active_high = settings.led_active_high
        self.initial_state = settings.led_initial_state
        if len(set(self.pins)) != 3 or any(isinstance(pin, bool) or not isinstance(pin, int)
                                            or not 0 <= pin <= 27 for pin in self.pins):
            raise LedError("RGB LED requires three distinct BCM GPIO pins from 0 to 27")
        if self.initial_state not in PATTERNS:
            raise LedError("Invalid initial RGB LED state")
        self._device_factory = device_factory
        self._devices = ()
        self._factory_close = None
        self._active = OFF
        self._state = None
        self._lock = RLock()

    @property
    def state(self):
        with self._lock:
            return self._state

    def open(self):
        """Acquire GPIO with all channels initially off, then apply initial state."""
        with self._lock:
            if self._devices:
                return self
            created = []
            try:
                if self._device_factory is None:
                    from gpiozero import LED
                    from gpiozero.pins.lgpio import LGPIOFactory

                    pin_factory = LGPIOFactory()
                    self._factory_close = pin_factory.close

                    def make(pin):
                        return LED(pin, active_high=self.active_high, initial_value=False,
                                   pin_factory=pin_factory)
                else:
                    def make(pin):
                        return self._device_factory(pin, active_high=self.active_high,
                                                    initial_value=False)

                for pin in self.pins:
                    created.append(make(pin))
            except BaseException as exc:
                self._release_unlocked(created)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise LedError("RGB LED GPIO initialization failed; check pin access and lgpio") from exc
            self._devices = tuple(created)
            self._active = OFF
            self._state = "off"
            self.set_state(self.initial_state)
            return self

    def _release_unlocked(self, devices):
        failed = False
        for device in devices:
            try:
                device.off()
            except Exception:
                failed = True
        for device in devices:
            try:
                device.close()
            except Exception:
                failed = True
        if self._factory_close is not None:
            try:
                self._factory_close()
            except Exception:
                failed = True
        self._factory_close = None
        self._devices = ()
        self._active = OFF
        self._state = None
        return failed

    def _apply_unlocked(self, pattern, state):
        if self._state == state and self._active == pattern:
            return False
        try:
            for device, was_on, want_on in zip(self._devices, self._active, pattern):
                if was_on and not want_on:
                    device.off()
            for device, was_on, want_on in zip(self._devices, self._active, pattern):
                if want_on and not was_on:
                    device.on()
        except BaseException as exc:
            self._release_unlocked(self._devices)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise LedError("RGB LED write failed; GPIO resources released") from exc
        self._active = pattern
        self._state = state
        return True

    def set_state(self, state):
        """Apply idle, listening, responding, or off without duplicate writes."""
        if not isinstance(state, str) or state not in PATTERNS:
            raise ValueError("LED state must be idle, listening, responding, or off")
        with self._lock:
            if not self._devices:
                raise LedError("RGB LED is not open or hardware is disabled")
            return self._apply_unlocked(PATTERNS[state], state)

    def show_channel(self, channel):
        """Light one channel for the standalone hardware test CLI."""
        if not isinstance(channel, str) or channel not in CHANNELS:
            raise ValueError("LED test channel must be red, green, or blue")
        with self._lock:
            if not self._devices:
                raise LedError("RGB LED is not open or hardware is disabled")
            pattern = tuple(name == channel for name in CHANNELS)
            return self._apply_unlocked(pattern, f"test:{channel}")

    def close(self):
        """Turn every channel off and release all pins, even after a partial fault."""
        with self._lock:
            if not self._devices and self._factory_close is None:
                return False
            failed = self._release_unlocked(self._devices)
            if failed:
                raise LedError("RGB LED cleanup failed; check GPIO hardware")
            return True

    def __enter__(self):
        return self.open()

    def __exit__(self, _type, _value, _traceback):
        self.close()
        return False
