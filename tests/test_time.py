import unittest

from qualithm_device import CredentialError, format_iso, parse_iso

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
