"""A host fake of MicroPython's `ntptime`: settime fails a scripted number of times first."""

failures = 0
calls = 0


def settime():
    global calls, failures  # noqa: PLW0603 -- module state a test scripts
    calls += 1
    if failures:
        failures -= 1
        raise OSError("ETIMEDOUT")


def reset():
    global calls, failures  # noqa: PLW0603 -- module state a test scripts
    calls = 0
    failures = 0
