from pico_case import PicoCase

from qualithm_device import board, esp32, pico, virtual


class Recorder:
    """Stands in for an adapter function and records how it was called."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class BoardCase(PicoCase):
    """Swaps each adapter's start and system_info for recorders, and restores the platform."""

    def setUp(self):
        super().setUp()
        self._saved = (
            board.platform[0],
            pico.start,
            pico.system_info,
            esp32.start,
            esp32.system_info,
            virtual.start,
            virtual.system_info,
        )
        self.esp32_start = Recorder("esp32 device")
        esp32.start = self.esp32_start
        esp32.system_info = Recorder({"on": "esp32"})
        self.pico_start = Recorder("pico device")
        self.virtual_start = Recorder("virtual device")
        pico.start = self.pico_start
        pico.system_info = Recorder({"on": "pico"})
        virtual.start = self.virtual_start
        virtual.system_info = Recorder({"on": "host"})

    def tearDown(self):
        (
            board.platform[0],
            pico.start,
            pico.system_info,
            esp32.start,
            esp32.system_info,
            virtual.start,
            virtual.system_info,
        ) = self._saved
        super().tearDown()


class BoardTest(BoardCase):
    def test_uses_the_pico_adapters_on_a_pico(self):
        board.platform[0] = "rp2"

        self.assertTrue(board.on_board())
        self.assertEqual(board.start(name="kitchen"), "pico device")
        self.assertEqual(self.pico_start.calls, [{"capabilities": (), "name": "kitchen"}])
        self.assertEqual(board.system_info(), {"on": "pico"})

    def test_uses_the_esp32_adapters_on_an_esp32(self):
        board.platform[0] = "esp32"

        self.assertTrue(board.on_board())
        self.assertEqual(board.start(name="porch"), "esp32 device")
        self.assertEqual(self.esp32_start.calls, [{"capabilities": (), "name": "porch"}])
        self.assertEqual(self.pico_start.calls, [])
        self.assertEqual(board.system_info(), {"on": "esp32"})

    def test_uses_the_virtual_adapters_on_a_host(self):
        for host in ("linux", "darwin"):
            board.platform[0] = host

            self.assertFalse(board.on_board())
            self.assertEqual(board.start(), "virtual device")
            self.assertEqual(board.system_info(), {"on": "host"})

        self.assertEqual(len(self.virtual_start.calls), 2)
