import json
import os
import sys

import esp32 as fake_esp32
import fake_gc
import fake_socket
import fake_time
import network
import ntptime
from device_fakes import wire
from pico_case import STORE_PATH, Esp32Case, http
from umqtt import simple

from qualithm_device import CredentialError, esp32, pico
from qualithm_device._time import EPOCH_2000_OFFSET_S

URL = "https://api.example.com"
JAN_1_2026 = 1767225600
SETUP_PATH = "/tmp/qualithm-device-test-esp32-setup.json"  # noqa: S108 -- MicroPython has no tempfile
SETUP = {
    "provisioningUrl": URL,
    "wifi": {"ssid": "home", "passphrase": "secret"},
    "claimCode": "qmc_x.y",
}


class Light:
    """A status light that records each state it was asked to show."""

    def __init__(self):
        self.states = []

    def show(self, state):
        self.states.append(state)


def claim_response():
    claimed = wire("claim")["cases"][1]
    fake_socket.response = http(200, json.dumps(claimed["body"]).encode())
    return claimed


class JoinWifiTest(Esp32Case):
    def test_joins_with_the_ports_default_security_and_sets_the_clock(self):
        esp32.join_wifi("home", "secret")

        wlan = network.WLAN.instances[-1]
        self.assertTrue(wlan.is_active)
        self.assertEqual(wlan.interface, network.STA_IF)
        self.assertEqual(wlan.joined, ("home", "secret"))
        self.assertIsNone(wlan.security)
        self.assertEqual(ntptime.calls, 1)

    def test_waits_while_the_network_connects(self):
        network.WLAN.statuses = [network.STAT_IDLE, network.STAT_CONNECTING, network.STAT_GOT_IP]

        esp32.join_wifi("home", "secret")

        self.assertEqual(fake_time.slept, [0.2, 0.2])

    def test_skips_joining_when_already_connected(self):
        network.WLAN.connected = True

        esp32.join_wifi("home", "secret")

        self.assertIsNone(network.WLAN.instances[-1].joined)
        self.assertEqual(ntptime.calls, 1)

    def test_names_why_the_network_refused(self):
        for status, reason in (
            (network.STAT_WRONG_PASSWORD, "wrong password"),
            (network.STAT_NO_AP_FOUND_W_COMPATIBLE_SECURITY, "WPA2 or WPA3 Personal"),
            (network.STAT_NO_AP_FOUND, "2.4 GHz"),
            (network.STAT_CONNECT_FAIL, "reason -1"),
        ):
            network.WLAN.statuses = [status]
            with self.assertRaises(esp32.WifiError) as caught:
                esp32.join_wifi("home", "secret")
            self.assertIn(reason, str(caught.exception))

    def test_gives_up_after_the_timeout(self):
        network.WLAN.statuses = [network.STAT_CONNECTING]

        with self.assertRaises(esp32.WifiError) as caught:
            esp32.join_wifi("home", "secret", timeout_s=1)

        self.assertIn("within 1 s", str(caught.exception))
        self.assertEqual(len(fake_time.slept), 5)

    def test_fails_when_ntp_never_answers(self):
        ntptime.failures = 3

        with self.assertRaises(esp32.WifiError) as caught:
            esp32.join_wifi("home", "secret")

        self.assertIn("clock", str(caught.exception))

    def test_shares_the_picos_errors(self):
        self.assertIs(esp32.WifiError, pico.WifiError)
        self.assertIs(esp32.SetupError, pico.SetupError)
        self.assertIs(esp32.SYSTEM_SENSORS, pico.SYSTEM_SENSORS)


class SystemInfoTest(Esp32Case):
    def test_reads_the_vital_signs(self):
        esp32._uptime[0] = 0
        esp32._uptime[1] = 0
        fake_time.ticks = 7250
        fake_esp32.temperature = 41
        network.WLAN.rssi = -55

        info = esp32.system_info(esp32.join_wifi("home", "secret"))

        major, minor, patch = sys.implementation.version[:3]
        self.assertEqual(info["uptime_s"], 7)
        self.assertEqual(info["chip_temperature_c"], 41)
        self.assertEqual(fake_esp32.reads, 1)
        self.assertEqual(info["wifi_rssi_dbm"], -55)
        self.assertEqual(info["firmware_version"], major * 10000 + minor * 100 + patch)
        self.assertEqual(info["free_memory_bytes"], fake_gc.free)
        self.assertEqual(fake_gc.collections, 1)

    def test_reads_the_station_when_nothing_was_joined(self):
        esp32._station[:] = []
        network.WLAN.connected = True
        network.WLAN.rssi = -60

        self.assertEqual(esp32.system_info()["wifi_rssi_dbm"], -60)

    def test_leaves_out_the_signal_strength_while_disconnected(self):
        esp32._station[:] = []

        info = esp32.system_info()

        self.assertFalse("wifi_rssi_dbm" in info)
        self.assertEqual(info["chip_temperature_c"], fake_esp32.temperature)


class ConnectTest(Esp32Case):
    """connect() end to end over the esp32 adapters, with only the network faked."""

    def test_claims_stores_and_publishes_system_info(self):
        claimed = claim_response()
        wlan = esp32.join_wifi("home", "secret")
        light = Light()

        device = esp32.connect(
            URL, store=esp32.FlashStore(STORE_PATH), claim_code="qmc_x.y", light=light
        )
        device.publish_reading(esp32.system_info(wlan))

        stored = esp32.FlashStore(STORE_PATH).load() or {}
        self.assertEqual(stored["token"], claimed["credential"]["token"])
        topic, payload = simple.MQTTClient.instances[0].published[0]
        self.assertEqual(topic, "capabilities")
        declared = json.loads(payload)["capabilities"]
        topic, payload = simple.MQTTClient.instances[0].published[1]
        self.assertEqual(topic, "telemetry")
        self.assertEqual(
            sorted(entry["config"]["metric"] for entry in declared),
            sorted(json.loads(payload)["metrics"]),
        )
        self.assertEqual(light.states, ["slow-pulse", "q-once"])

    def test_stamps_readings_in_unix_time_on_the_2000_epoch_port(self):
        claim_response()
        fake_time.epoch = 2000
        fake_time.seconds = JAN_1_2026 - EPOCH_2000_OFFSET_S

        device = esp32.connect(URL, store=esp32.FlashStore(STORE_PATH), claim_code="qmc_x.y")
        device.publish_reading({"t": 1})

        _, payload = simple.MQTTClient.instances[0].published[-1]
        self.assertEqual(json.loads(payload)["ts"], JAN_1_2026 * 1000)

    def test_shows_steady_when_it_reconnects(self):
        claim_response()
        esp32.connect(URL, store=esp32.FlashStore(STORE_PATH), claim_code="qmc_x.y")
        light = Light()

        esp32.connect(URL, store=esp32.FlashStore(STORE_PATH), claim_code="qmc_x.y", light=light)

        self.assertEqual(light.states, ["slow-pulse", "steady"])

    def test_shows_attention_when_the_connect_fails(self):
        light = Light()

        with self.assertRaises(CredentialError):
            esp32.connect(URL, store=esp32.FlashStore(STORE_PATH), light=light)

        self.assertEqual(light.states, ["slow-pulse", "steady-red"])


class StartTest(Esp32Case):
    """start() from the setup file `qualithm deploy` writes."""

    def tearDown(self):
        super().tearDown()
        for path in (SETUP_PATH, SETUP_PATH + ".tmp"):
            try:  # noqa: SIM105 -- MicroPython has no contextlib
                os.remove(path)
            except OSError:
                pass

    def write_setup(self, setup):
        with open(SETUP_PATH, "w") as file:
            file.write(json.dumps(setup))

    def read_back(self):
        with open(SETUP_PATH) as file:
            return json.loads(file.read())

    def test_joins_claims_and_forgets_the_claim_code(self):
        claimed = claim_response()
        self.write_setup(SETUP)
        network.WLAN.rssi = -51

        device = esp32.start(setup_path=SETUP_PATH, store=esp32.FlashStore(STORE_PATH))
        device.publish_reading(esp32.system_info())

        self.assertEqual(network.WLAN.instances[-1].joined, ("home", "secret"))
        stored = esp32.FlashStore(STORE_PATH).load() or {}
        self.assertEqual(stored["token"], claimed["credential"]["token"])
        self.assertEqual(
            self.read_back(),
            {"provisioningUrl": URL, "wifi": {"ssid": "home", "passphrase": "secret"}},
        )
        _, payload = simple.MQTTClient.instances[0].published[1]
        self.assertEqual(json.loads(payload)["metrics"]["wifi_rssi_dbm"], -51)

    def test_keeps_the_claim_code_and_shows_attention_when_wifi_fails(self):
        self.write_setup(SETUP)
        network.WLAN.statuses = [network.STAT_WRONG_PASSWORD]
        light = Light()

        with self.assertRaises(esp32.WifiError):
            esp32.start(setup_path=SETUP_PATH, store=esp32.FlashStore(STORE_PATH), light=light)

        self.assertEqual(self.read_back()["claimCode"], "qmc_x.y")
        self.assertEqual(light.states, ["slow-pulse", "steady-red"])

    def test_names_a_missing_setup_file(self):
        with self.assertRaises(esp32.SetupError) as caught:
            esp32.start(setup_path=SETUP_PATH, store=esp32.FlashStore(STORE_PATH))
        self.assertIn("run `qualithm deploy`", str(caught.exception))
