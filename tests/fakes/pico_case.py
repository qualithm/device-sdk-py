"""A TestCase that swaps the board modules `qualithm_device.pico` reaches for host fakes."""

import unittest

import fake_gc
import fake_socket
import fake_time
import fake_tls
import machine
import network
import ntptime
from umqtt import simple

from qualithm_device import pico

# MicroPython has no tempfile.
STORE_PATH = "/tmp/qualithm-device-test-credential.json"  # noqa: S108


def http(status, body=b"", headers=b""):
    """The bytes of an HTTP/1.1 response with a Content-Length."""
    return b"HTTP/1.1 %d OK\r\n%sContent-Length: %d\r\n\r\n%s" % (status, headers, len(body), body)


class PicoCase(unittest.TestCase):
    """Resets every fake, and patches the modules the Unix port's own builtins would shadow."""

    def setUp(self):
        for fake in (fake_gc, fake_socket, fake_time, fake_tls, network, ntptime, simple):
            fake.reset()
        machine.ADC.reading = 0
        machine.ADC.channels = []
        self._board = (pico.gc, pico.socket, pico.time, pico.tls)
        pico.gc = fake_gc
        pico.socket = fake_socket
        pico.time = fake_time
        pico.tls = fake_tls
        pico.FlashStore(STORE_PATH).clear()

    def tearDown(self):
        pico.gc, pico.socket, pico.time, pico.tls = self._board
        pico.FlashStore(STORE_PATH).clear()
