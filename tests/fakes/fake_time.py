"""A fake of MicroPython's `time`, patched over a board module's: a clock a test moves by hand."""

PERIOD = 1 << 30

ticks = 0
seconds = 1767225600
#: Each sleep, in seconds.
slept = []


def ticks_ms():
    return ticks


def ticks_add(base, delta):
    return (base + delta) % PERIOD


def ticks_diff(end, start):
    return ((end - start + PERIOD // 2) % PERIOD) - PERIOD // 2


def sleep(s):
    global ticks, seconds  # noqa: PLW0603 -- module state a test scripts
    slept.append(s)
    seconds += s
    ticks = ticks_add(ticks, int(s * 1000))


def time():
    return seconds


def reset():
    global ticks, seconds  # noqa: PLW0603 -- module state a test scripts
    ticks = 0
    seconds = 1767225600
    slept.clear()
