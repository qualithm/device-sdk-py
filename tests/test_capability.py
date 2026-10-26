import unittest

from device_fakes import wire

from qualithm_device import CapabilityError
from qualithm_device._capability import build_manifest


class BuildManifestTest(unittest.TestCase):
    def test_builds_the_manifest_for_every_capability_type(self):
        fixture = wire("capabilities")
        self.assertEqual(build_manifest(fixture["declarations"]), fixture["manifest"])

    def test_omits_unset_optional_config(self):
        fixture = wire("capabilities")["optionalOmitted"]
        self.assertEqual(build_manifest(fixture["declarations"]), fixture["manifest"])

    def test_accepts_a_tuple(self):
        manifest = build_manifest(({"key": "reboot", "type": "trigger"},))
        self.assertEqual(
            manifest["capabilities"], [{"key": "reboot", "type": "trigger", "config": {}}]
        )

    def test_rejects_invalid_declarations(self):
        for declarations in wire("capabilities")["invalid"]:
            with self.assertRaises(CapabilityError):
                build_manifest(declarations)

    def test_rejects_a_list_that_is_not_a_list(self):
        with self.assertRaises(CapabilityError):
            build_manifest("power")  # pyright: ignore[reportArgumentType] -- an untyped caller

    def test_rejects_a_missing_list_and_non_finite_bounds(self):
        for declarations in (
            None,
            [{"key": "r", "type": "range", "min": float("nan"), "max": 1}],
            [{"key": "r", "type": "range", "min": 0, "max": float("inf")}],
            [{"key": "r", "type": "range", "min": 0, "max": 1, "step": float("inf")}],
        ):
            with self.assertRaises(CapabilityError):
                build_manifest(declarations)
