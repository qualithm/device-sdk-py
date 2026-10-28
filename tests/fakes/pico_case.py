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

from qualithm_device import _micropython, pico

# MicroPython has no tempfile.
STORE_PATH = "/tmp/qualithm-device-test-credential.json"  # noqa: S108


def http(status, body=b"", headers=b""):
    """The bytes of an HTTP/1.1 response with a Content-Length."""
    return b"HTTP/1.1 %d OK\r\n%sContent-Length: %d\r\n\r\n%s" % (status, headers, len(body), body)


class NetCase(unittest.TestCase):
    """Resets the network fakes and patches the shared transport's sockets, TLS and clock."""

    def setUp(self):
        for fake in (fake_gc, fake_socket, fake_time, fake_tls, simple):
            fake.reset()
        self._net = (_micropython.socket, _micropython.time, _micropython.tls)
        _micropython.socket = fake_socket
        _micropython.time = fake_time
        _micropython.tls = fake_tls

    def tearDown(self):
        _micropython.socket, _micropython.time, _micropython.tls = self._net


class PicoCase(NetCase):
    """Resets every fake, and patches the modules the Unix port's own builtins would shadow."""

    def setUp(self):
        super().setUp()
        for fake in (network, ntptime):
            fake.reset()
        machine.reset()
        self._board = (pico.gc, pico.time)
        pico.gc = fake_gc
        pico.time = fake_time
        pico.FlashStore(STORE_PATH).clear()

    def tearDown(self):
        pico.gc, pico.time = self._board
        pico.FlashStore(STORE_PATH).clear()
        super().tearDown()
