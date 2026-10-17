import unittest

from qualithm_device import RECONNECT_CAP_MS, RECONNECT_FLOOR_MS, reconnect_delay


class BackoffTest(unittest.TestCase):
    def test_doubles_the_ceiling_up_to_the_cap(self):
        top = 0.999999
        self.assertEqual(reconnect_delay(1, 1000, random=lambda: top), 999)
        self.assertEqual(reconnect_delay(2, 1000, random=lambda: top), 1999)
        self.assertEqual(reconnect_delay(3, 1000, random=lambda: 0.5), 2000)
        self.assertEqual(reconnect_delay(20, 1000, random=lambda: top), RECONNECT_CAP_MS - 1)

    def test_never_waits_less_than_the_floor(self):
        self.assertEqual(reconnect_delay(1, 1000, random=lambda: 0.0), RECONNECT_FLOOR_MS)
        self.assertEqual(reconnect_delay(0, 1000, random=lambda: 0.0), RECONNECT_FLOOR_MS)

    def test_draws_from_the_random_module_by_default(self):
        delay = reconnect_delay(1, 1000)
        self.assertTrue(RECONNECT_FLOOR_MS <= delay < 1000)
