import unittest

from qualithm_device import CredentialError, format_iso, parse_iso
from qualithm_device._time import EPOCH_2000_OFFSET_S, unix_clock

JAN_1_2026 = 1767225600


class TimeTest(unittest.TestCase):
    def test_parses_the_platform_forms(self):
        for text in (
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00.000Z",
            "2026-01-01T00:00:00.999Z",
            "2026-01-01 00:00:00",
            "2026-01-01T00:00:00+00:00",
        ):
            self.assertEqual(parse_iso(text), JAN_1_2026)

    def test_parses_across_months_and_leap_days(self):
        self.assertEqual(parse_iso("2024-02-29T12:30:15Z"), 1709209815)
        self.assertEqual(parse_iso("1970-01-01T00:00:00Z"), 0)
        self.assertEqual(parse_iso("1969-12-31T23:59:59Z"), -1)

    def test_formats_as_to_iso_string_does(self):
        self.assertEqual(format_iso(JAN_1_2026), "2026-01-01T00:00:00.000Z")
        self.assertEqual(format_iso(1709209815.25), "2024-02-29T12:30:15.250Z")

    def test_round_trips(self):
        for seconds in (0, 951782400, 1709209815, 4102444800):
            self.assertEqual(parse_iso(format_iso(seconds)), seconds)

    def test_rejects_what_it_cannot_read(self):
        for text in ("yesterday", "2026-01-01", "2026-01-01T00:00:00+02:00"):
            with self.assertRaises(CredentialError):
                parse_iso(text)


class Clock:
    """A port's `time` module whose clock counts from `epoch`."""

    def __init__(self, epoch, seconds):
        self.epoch = epoch
        self.seconds = seconds

    def time(self):
        return self.seconds

    def gmtime(self, secs):
        return (self.epoch, 1, 1, 0, 0, secs, 0, 0)


class UnixClockTest(unittest.TestCase):
    def test_adds_the_offset_on_a_2000_epoch_port(self):
        clock = Clock(2000, JAN_1_2026 - EPOCH_2000_OFFSET_S)

        now = unix_clock(clock)

        self.assertEqual(now(), JAN_1_2026)
        clock.seconds += 5
        self.assertEqual(now(), JAN_1_2026 + 5)

    def test_reads_a_1970_epoch_port_as_it_is(self):
        clock = Clock(1970, JAN_1_2026)

        self.assertEqual(unix_clock(clock)(), JAN_1_2026)

    def test_the_offset_is_2000_01_01(self):
        self.assertEqual(format_iso(EPOCH_2000_OFFSET_S), "2000-01-01T00:00:00.000Z")
