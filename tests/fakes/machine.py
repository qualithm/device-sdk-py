"""A host fake of MicroPython's `machine`: the ADC the chip's temperature sensor sits on."""


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
