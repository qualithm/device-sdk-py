"""ISO-8601 timestamps as the platform writes them, without datetime (a board has none)."""

from qualithm_device._errors import CredentialError

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable

    from qualithm_device.interfaces import Clock

SECONDS_PER_DAY = 86_400

#: Seconds from 1970-01-01 to 2000-01-01, which a 2000-epoch port's `time.time()` lags Unix time by.
EPOCH_2000_OFFSET_S = 946_684_800
EPOCH_2000_YEAR = 2000


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's algorithm)."""
    year -= month <= 2  # noqa: PLR2004 -- March-based year
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1  # noqa: PLR2004
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def _civil_from_days(days: int) -> "tuple[int, int, int]":
    """The (year, month, day) a day count since 1970-01-01 falls on."""
    days += 719_468
    era = (days if days >= 0 else days - 146_096) // 146_097
    doe = days - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + (3 if mp < 10 else -9)  # noqa: PLR2004
    return yoe + era * 400 + (month <= 2), month, day  # noqa: PLR2004


def parse_iso(value: str) -> int:
    """Unix seconds for an ISO-8601 UTC timestamp.

    Accepts `2026-10-17T01:02:03Z`, with or without fractional seconds or the `Z`, and a space in
    place of the `T`, which covers every timestamp the platform returns.

    Args:
        value: The timestamp.

    Returns:
        The whole seconds since the Unix epoch.

    Raises:
        CredentialError: The value isn't a timestamp in that form.
    """
    text = value.rstrip("Z")
    plus = text.find("+")
    if plus != -1:
        if text[plus:] not in ("+00:00", "+0000"):
            raise CredentialError(f"timestamp is not UTC: {value}")
        text = text[:plus]
    try:
        date, clock = text.replace(" ", "T").split("T")
        year, month, day = (int(part) for part in date.split("-"))
        hours, minutes, seconds = clock.split(":")
        whole = int(seconds.split(".")[0])
        days = _days_from_civil(year, month, day)
        return days * SECONDS_PER_DAY + int(hours) * 3600 + int(minutes) * 60 + whole
    except ValueError:
        raise CredentialError(f"not an ISO-8601 timestamp: {value}") from None


def format_iso(seconds: float) -> str:
    """The ISO-8601 UTC timestamp, with milliseconds, that `Date.toISOString` writes.

    Args:
        seconds: Unix seconds; the fraction is kept to the millisecond.

    Returns:
        A timestamp such as `2026-10-17T01:02:03.456Z`.
    """
    millis = int(seconds * 1000)
    whole, ms = divmod(millis, 1000)
    days, rest = divmod(whole, SECONDS_PER_DAY)
    year, month, day = _civil_from_days(days)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (year, month, day, hours, minutes, secs, ms)


def unix_clock(clock: "Clock") -> "Callable[[], float]":
    """A clock in Unix seconds over a port's `time` module, whichever epoch the port counts from.

    MicroPython's `esp32` port counts `time.time()` from 2000-01-01; the `rp2` and Unix ports and
    CPython count from 1970. Readings and credential checks need Unix seconds on every port.

    Args:
        clock: The port's `time` module, or anything with its `time()` and `gmtime()`.

    Returns:
        A function that reads the current Unix time.
    """
    read = clock.time
    if clock.gmtime(0)[0] != EPOCH_2000_YEAR:
        return read
    return lambda: read() + EPOCH_2000_OFFSET_S
