"""A host fake of MicroPython's `machine`: the temperature sensor's ADC, the LED's pin, a timer."""


class ADC:
    """An ADC channel whose reading a test sets."""

    #: The raw 16-bit reading every channel returns.
    reading = 0
    #: The channels read, in order.
    channels = []  # noqa: RUF012 -- state a test scripts

    def __init__(self, channel):
        self.channel = channel

    def read_u16(self):
        ADC.channels.append(self.channel)
        return ADC.reading


class Pin:
    """An output pin that records each value written."""

    OUT = 1
    #: Every pin made, in order.
    instances = []  # noqa: RUF012 -- state a test scripts

    def __init__(self, name, mode=None):
        self.name = name
        self.mode = mode
        self.values = []
        Pin.instances.append(self)

    def value(self, v):
        self.values.append(v)


class Timer:
    """A timer a test fires by hand with `fire()`."""

    PERIODIC = 1
    #: Every timer made, in order.
    instances = []  # noqa: RUF012 -- state a test scripts

    def __init__(self, *args):
        self.callback = None
        self.period = None
        self.stopped = False
        Timer.instances.append(self)

    def init(self, *, period, mode, callback):
        self.period = period
        self.mode = mode
        self.callback = callback

    def deinit(self):
        self.stopped = True

    def fire(self):
        if self.callback is not None:
            self.callback(self)


def reset():
    ADC.reading = 0
    ADC.channels = []
    Pin.instances = []
    Timer.instances = []
