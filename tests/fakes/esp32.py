"""A host fake of MicroPython's `esp32`: the chip temperature sensor a test sets."""

#: The whole degrees `mcu_temperature` reads.
temperature = 0
#: How many times it was read.
reads = 0


def mcu_temperature():
    global reads  # noqa: PLW0603 -- module state a test scripts
    reads += 1
    return temperature


def reset():
    global reads, temperature  # noqa: PLW0603 -- module state a test scripts
    reads = 0
    temperature = 0
