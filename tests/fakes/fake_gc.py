"""A fake of MicroPython's `gc`, patched over a board module's: CPython's has no mem_free."""

free = 123456
collections = 0


def collect():
    global collections  # noqa: PLW0603 -- module state a test scripts
    collections += 1


def mem_free():
    return free


def reset():
    global collections  # noqa: PLW0603 -- module state a test scripts
    collections = 0
