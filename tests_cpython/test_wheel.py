import unittest
from pathlib import Path

import qualithm_device
import qualithm_device.interfaces

PACKAGE = Path(qualithm_device.__file__).parent


class PackageTest(unittest.TestCase):
    def test_ships_the_type_marker_and_the_interfaces(self):
        self.assertTrue((PACKAGE / "py.typed").is_file())
        self.assertTrue((PACKAGE / "interfaces.py").is_file())

    def test_exports_the_hero_api(self):
        self.assertTrue(callable(qualithm_device.connect))
        self.assertFalse(qualithm_device.interfaces.TYPE_CHECKING)
