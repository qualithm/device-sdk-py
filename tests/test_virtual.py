import json
import os
import sys

import fake_gc
import fake_socket
import fake_time
from device_fakes import wire
from pico_case import NetCase, http
from umqtt import simple

from qualithm_device import CredentialError, virtual

URL = "https://api.example.com"
# MicroPython has no tempfile.
STORE_PATH = "/tmp/qualithm-device-test-virtual-credential.json"  # noqa: S108
SETUP_PATH = "/tmp/qualithm-device-test-virtual-setup.json"  # noqa: S108
SETUP = {"provisioningUrl": URL, "claimCode": "qmc_x.y"}


def write_setup(setup):
    with open(SETUP_PATH, "w") as file:
        file.write(setup if isinstance(setup, str) else json.dumps(setup))


def read_back():
    with open(SETUP_PATH) as file:
        return json.loads(file.read())


def claim_body():
    request = fake_socket.sockets[0].written
    return json.loads(request.split(b"\r\n\r\n", 1)[1])


class VirtualCase(NetCase):
    """Patches the virtual device's clock and memory, and clears its files."""

    def setUp(self):
        super().setUp()
        self._host = (virtual.gc, virtual.time)
        virtual.gc = fake_gc
        virtual.time = fake_time
        virtual._uptime[0] = 0
        virtual._uptime[1] = 0
        self.clear()

    def tearDown(self):
        virtual.gc, virtual.time = self._host
        self.clear()
        super().tearDown()

    def clear(self):
        virtual.FlashStore(STORE_PATH).clear()
        for path in (SETUP_PATH, SETUP_PATH + ".tmp"):
            try:  # noqa: SIM105 -- MicroPython has no contextlib
                os.remove(path)
            except OSError:
                pass

    def store(self):
        return virtual.FlashStore(STORE_PATH)


class SystemInfoTest(VirtualCase):
    def test_reads_the_vital_signs_with_no_wifi(self):
        fake_time.ticks = 5500

        info = virtual.system_info()

        major, minor, patch = sys.implementation.version[:3]
        self.assertEqual(info["uptime_s"], 5)
        self.assertEqual(info["free_memory_bytes"], fake_gc.free)
        self.assertEqual(info["firmware_version"], major * 10000 + minor * 100 + patch)
        self.assertFalse("wifi_rssi_dbm" in info)

    def test_counts_uptime_from_the_first_read_on_a_host(self):
        virtual._uptime[1] = virtual._NOT_STARTED
        fake_time.ticks = 9000000

        first = virtual.system_info()["uptime_s"]
        fake_time.ticks += 5000

        self.assertEqual((first, virtual.system_info()["uptime_s"]), (0, 5))

    def test_counts_uptime_from_the_connect(self):
        fake_socket.response = http(200, json.dumps(wire("claim")["cases"][1]["body"]).encode())
        fake_time.ticks = 7000
        virtual.connect(URL, store=self.store(), claim_code="qmc_x.y")
        fake_time.ticks = 9000

        self.assertEqual(virtual.system_info()["uptime_s"], 2)

    def test_swings_the_stand_in_temperature_around_25(self):
        readings = []
        for seconds in (0, 150, 300, 450):
            fake_time.ticks = seconds * 1000
            readings.append(virtual.system_info()["chip_temperature_c"])

        self.assertEqual(readings, [26.5, 25.0, 23.5, 25.0])

    def test_declares_a_sensor_for_every_reading(self):
        declared = sorted(str(sensor["key"]) for sensor in virtual.SYSTEM_SENSORS)

        self.assertEqual(declared, sorted(virtual.system_info()))


class ConnectTest(VirtualCase):
    def test_claims_as_virtual_and_publishes_system_info(self):
        claimed = wire("claim")["cases"][1]
        fake_socket.response = http(200, json.dumps(claimed["body"]).encode())

        device = virtual.connect(URL, store=self.store(), claim_code="qmc_x.y")
        device.publish_reading(virtual.system_info())

        self.assertEqual(claim_body(), {"code": "qmc_x.y", "virtual": True})
        self.assertEqual((self.store().load() or {})["token"], claimed["credential"]["token"])
        client = simple.MQTTClient.instances[0]
        topic, payload = client.published[0]
        self.assertEqual(topic, "capabilities")
        declared = json.loads(payload)["capabilities"]
        topic, payload = client.published[1]
        self.assertEqual(topic, "telemetry")
        self.assertEqual(
            sorted(entry["config"]["metric"] for entry in declared),
            sorted(json.loads(payload)["metrics"]),
        )

    def test_uses_the_default_transport_and_store(self):
        fake_socket.response = http(200, json.dumps(wire("claim")["cases"][1]["body"]).encode())
        saved = virtual.DEFAULT_STORE_PATH
        virtual.DEFAULT_STORE_PATH = STORE_PATH
        try:
            virtual.connect(URL, claim_code="qmc_x.y")
        finally:
            virtual.DEFAULT_STORE_PATH = saved

        self.assertIsNotNone(self.store().load())


class ReadSetupTest(VirtualCase):
    def test_reads_a_setup_with_no_wifi(self):
        write_setup(SETUP)

        self.assertEqual(virtual.read_setup(SETUP_PATH), SETUP)

    def test_names_what_is_wrong(self):
        for setup, reason in (
            ("not json", "isn't a JSON object"),
            ({"claimCode": "qmc_x.y"}, "no https provisioningUrl"),
            ({"provisioningUrl": "http://insecure"}, "no https provisioningUrl"),
            ({"provisioningUrl": URL, "claimCode": ""}, "isn't a string"),
        ):
            write_setup(setup)
            with self.assertRaises(virtual.SetupError) as caught:
                virtual.read_setup(SETUP_PATH)
            self.assertIn(reason, str(caught.exception))

    def test_points_at_the_deploy_command_when_missing(self):
        with self.assertRaises(virtual.SetupError) as caught:
            virtual.read_setup(SETUP_PATH)

        self.assertIn("qualithm deploy --virtual", str(caught.exception))


class StartTest(VirtualCase):
    def test_claims_and_forgets_the_claim_code(self):
        claimed = wire("claim")["cases"][1]
        fake_socket.response = http(200, json.dumps(claimed["body"]).encode())
        write_setup(SETUP)

        virtual.start(setup_path=SETUP_PATH, store=self.store())

        self.assertEqual(claim_body()["virtual"], True)
        self.assertEqual(read_back(), {"provisioningUrl": URL})

    def test_reconnects_with_the_stored_credential_after_the_claim(self):
        fake_socket.response = http(200, json.dumps(wire("claim")["cases"][1]["body"]).encode())
        write_setup(SETUP)
        virtual.start(setup_path=SETUP_PATH, store=self.store())
        requests = len(fake_socket.sockets)

        virtual.start(setup_path=SETUP_PATH, store=self.store())

        self.assertEqual(len(fake_socket.sockets), requests)
        self.assertEqual(len(simple.MQTTClient.instances), 2)

    def test_fails_with_no_claim_code_and_nothing_stored(self):
        write_setup({"provisioningUrl": URL})

        with self.assertRaises(CredentialError):
            virtual.start(setup_path=SETUP_PATH, store=self.store())
