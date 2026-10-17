"""Reconnect backoff: exponential growth with full jitter, as device-sdk-js waits.

A fleet the gateway refuses spreads its retries out instead of retrying in lockstep.
"""

#: The longest wait between reconnect attempts.
RECONNECT_CAP_MS = 30_000
#: The shortest wait, so a zero draw never hot-loops.
RECONNECT_FLOOR_MS = 100

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable


def reconnect_delay(
    attempt: int,
    base_ms: int = 1000,
    cap_ms: int = RECONNECT_CAP_MS,
    random: "Callable[[], float] | None" = None,
) -> int:
    """The wait before the next reconnect after `attempt` consecutive failures.

    A uniform draw from `[0, min(cap, base * 2 ** (attempt - 1)))`, at least the floor. Reset the
    attempt count once a connect is accepted.

    Args:
        attempt: Consecutive failed attempts so far, 1 for the first.
        base_ms: The ceiling of the first wait.
        cap_ms: The largest ceiling.
        random: A source of uniform draws in `[0, 1)`; the `random` module's by default.

    Returns:
        The wait, in milliseconds.
    """
    if random is None:
        import random as stdlib  # noqa: PLC0415 -- only when no source is given

        random = stdlib.random
    ceiling = min(cap_ms, base_ms * 2 ** max(0, attempt - 1))
    return max(RECONNECT_FLOOR_MS, int(random() * ceiling))
