import json
import sys

import fake_gc
import fake_socket
import fake_time
import fake_tls
import machine
import network
import ntptime
from device_fakes import wire
from pico_case import STORE_PATH, PicoCase, http
from umqtt import simple

from qualithm_device import CapabilityError, CredentialError, connect, pico
from qualithm_device._roots import ROOTS

URL = "https://api.example.com"
JAN_21_2026 = 1768953600


class JoinWifiTest(PicoCase):
    def test_joins_and_sets_the_clock(self):
        pico.join_wifi("home", "secret")

        wlan = network.WLAN.instances[-1]
        self.assertTrue(wlan.is_active)
        self.assertEqual(wlan.interface, network.STA_IF)
        self.assertEqual(wlan.joined, ("home", "secret"))
        self.assertEqual(ntptime.calls, 1)

    def test_joins_wpa2_and_wpa3_networks_alike(self):
        pico.join_wifi("home", "secret")

        self.assertEqual(network.WLAN.instances[-1].security, network.WLAN.SEC_WPA2_WPA3)

    def test_keeps_the_driver_default_on_firmware_without_wpa3(self):
        saved = network.WLAN.SEC_WPA2_WPA3
        del network.WLAN.SEC_WPA2_WPA3
        try:
            pico.join_wifi("home", "secret")
        finally:
            network.WLAN.SEC_WPA2_WPA3 = saved

        wlan = network.WLAN.instances[-1]
        self.assertEqual(wlan.joined, ("home", "secret"))
        self.assertIsNone(wlan.security)

    def test_waits_while_the_network_connects(self):
        network.WLAN.statuses = [network.STAT_CONNECTING, network.STAT_CONNECTING, 3]

        pico.join_wifi("home", "secret")

        self.assertEqual(fake_time.slept, [0.2, 0.2])

    def test_skips_joining_when_already_connected(self):
        network.WLAN.connected = True

        pico.join_wifi("home", "secret")

        self.assertIsNone(network.WLAN.instances[-1].joined)
        self.assertEqual(ntptime.calls, 1)

    def test_names_why_the_network_refused(self):
        for status, reason in (
            (network.STAT_WRONG_PASSWORD, "wrong password"),
            (network.STAT_WRONG_PASSWORD, "WPA2 or WPA3 Personal"),
            (network.STAT_NO_AP_FOUND, "2.4 GHz"),
            (network.STAT_CONNECT_FAIL, "status -1"),
            (network.STAT_CONNECT_FAIL, "WPA2 or WPA3 Personal"),
        ):
            network.WLAN.statuses = [status]
            with self.assertRaises(pico.WifiError) as caught:
                pico.join_wifi("home", "secret")
            self.assertIn(reason, str(caught.exception))

    def test_gives_up_after_the_timeout(self):
        network.WLAN.statuses = [network.STAT_CONNECTING]

        with self.assertRaises(pico.WifiError) as caught:
            pico.join_wifi("home", "secret", timeout_s=1)

        self.assertIn("within 1 s", str(caught.exception))
        self.assertEqual(len(fake_time.slept), 5)

    def test_retries_ntp(self):
        ntptime.failures = 2

        pico.join_wifi("home", "secret")

        self.assertEqual(ntptime.calls, 3)

    def test_fails_when_ntp_never_answers(self):
        ntptime.failures = 3

        with self.assertRaises(pico.WifiError) as caught:
            pico.join_wifi("home", "secret")

        self.assertIn("clock", str(caught.exception))


class RequestTest(PicoCase):
    def test_sends_one_request_over_verified_tls(self):
        fake_socket.response = http(200, b'{"data": {}}')

        status, body = pico.Transport(timeout_s=5).request(
            "POST", URL + "/v1/provision/claim", {"content-type": "application/json"}, b'{"a": 1}'
        )

        self.assertEqual((status, body), (200, b'{"data": {}}'))
        sock = fake_socket.sockets[0]
        self.assertEqual(sock.address, ("api.example.com", 443))
        self.assertEqual(sock.timeout, 5)
        self.assertTrue(sock.closed)
        context, hostname = sock.wrapped
        self.assertEqual(hostname, "api.example.com")
        self.assertEqual(context.verify_mode, fake_tls.CERT_REQUIRED)
        self.assertEqual(context.cadata, ROOTS)
        self.assertIsNone(context.cert_chain)
        self.assertEqual(
            sock.written,
            b"POST /v1/provision/claim HTTP/1.1\r\nHost: api.example.com\r\nConnection: close\r\n"
            b"content-type: application/json\r\nContent-Length: 8\r\n\r\n"
            b'{"a": 1}',
        )

    def test_takes_a_port_and_a_bare_host(self):
        fake_socket.response = http(204)

        status, body = pico.Transport(cadata="my-roots").request("GET", "https://h:8443", {}, None)

        self.assertEqual((status, body), (204, b""))
        self.assertEqual(fake_socket.sockets[0].address, ("h", 8443))
        self.assertTrue(fake_socket.sockets[0].written.startswith(b"GET / HTTP/1.1\r\n"))
        self.assertFalse(b"Content-Length" in fake_socket.sockets[0].written)
        self.assertEqual(fake_tls.contexts[0].cadata, "my-roots")

    def test_reads_a_chunked_body(self):
        fake_socket.response = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\nc;ext=1\r\n, chunked w!\r\n0\r\nTrailer: x\r\n\r\n"
        )

        self.assertEqual(
            pico.Transport().request("GET", URL, {}, None), (200, b"hello, chunked w!")
        )

    def test_reads_to_the_end_without_a_length(self):
        fake_socket.response = b"HTTP/1.1 200 OK\r\nServer: x\r\n\r\n" + b"z" * 1200

        self.assertEqual(pico.Transport().request("GET", URL, {}, None), (200, b"z" * 1200))

    def test_refuses_plain_http(self):
        with self.assertRaises(OSError):
            pico.Transport().request("GET", "http://api.example.com", {}, None)
        self.assertEqual(fake_socket.sockets, [])

    def test_raises_oserror_when_unreachable_or_unverified(self):
        fake_socket.unreachable = OSError("EHOSTUNREACH")
        with self.assertRaises(OSError):
            pico.Transport().request("GET", URL, {}, None)

        fake_socket.unreachable = None
        fake_tls.handshake_error = OSError("ECONNRESET")
        with self.assertRaises(OSError):
            pico.Transport().request("GET", URL, {}, None)

        # MicroPython raises a certificate that doesn't verify as a ValueError.
        fake_tls.handshake_error = ValueError("The certificate is not correctly signed")
        with self.assertRaises(OSError) as caught:
            pico.Transport().request("GET", URL, {}, None)
        self.assertIn("couldn't verify api.example.com", str(caught.exception))
        self.assertTrue(all(sock.closed for sock in fake_socket.sockets))

    def test_raises_oserror_on_a_bad_port(self):
        with self.assertRaises(OSError):
            pico.Transport().request("GET", "https://h:port/", {}, None)

    def test_raises_oserror_on_a_malformed_response(self):
        for response in (
            b"",
            b"SSH-2.0\r\n\r\n",
            b"HTTP/1.1 abc OK\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nshort",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
        ):
            fake_socket.response = response
            with self.assertRaises(OSError):
                pico.Transport().request("GET", URL, {}, None)


class OpenTest(PicoCase):
    def test_opens_a_token_session_over_verified_tls(self):
        session = pico.Transport(timeout_s=5).open(
            "gw.example.com", 8883, "d", "d", "qmd_abc", None, None, 60
        )

        client = simple.MQTTClient.instances[0]
        self.assertEqual(
            (client.client_id, client.server, client.port, client.user, client.pswd),
            ("d", "gw.example.com", 8883, "d", "qmd_abc"),
        )
        self.assertEqual(client.keepalive, 60)
        self.assertEqual(client.connected, (True, 5))
        self.assertEqual(client.ssl.verify_mode, fake_tls.CERT_REQUIRED)
        self.assertEqual(client.ssl.cadata, ROOTS)
        self.assertIsNone(session.max_packet_size)

    def test_presents_a_certificate(self):
        pico.Transport().open("gw", 8883, "d", None, None, "key", "cert", 60)

        self.assertEqual(simple.MQTTClient.instances[0].ssl.cert_chain, ("cert", "key"))

    def test_raises_oserror_when_the_gateway_does_not_verify(self):
        fake_tls.handshake_error = ValueError("The certificate is not correctly signed")

        with self.assertRaises(OSError) as caught:
            pico.Transport().open("gw", 8883, "d", "d", "t", None, None, 60)

        self.assertIn("couldn't verify the gateway", str(caught.exception))

    def test_raises_oserror_when_refused(self):
        simple.MQTTClient.refuse = 5

        with self.assertRaises(OSError) as caught:
            pico.Transport().open("gw", 8883, "d", "d", "bad", None, None, 60)

        self.assertIn("code 5", str(caught.exception))


class SessionTest(PicoCase):
    def open(self, keepalive_s=60):
        self.now = [0]
        transport = pico.Transport(now=lambda: self.now[0])
        session = transport.open("gw", 8883, "d", "d", "t", None, None, keepalive_s)
        return session, simple.MQTTClient.instances[-1]

    def test_publishes_and_subscribes(self):
        session, client = self.open()

        session.publish("telemetry", b"{}")
        session.subscribe("command/#")

        self.assertEqual(client.published, [("telemetry", b"{}")])
        self.assertEqual(client.subscribed, ["command/#"])

    def test_polls_every_waiting_message(self):
        session, client = self.open()
        client.inbound = [(b"command/power", b'{"value": true}'), (b"other", bytearray(b"x"))]

        self.assertEqual(session.poll(), [("command/power", b'{"value": true}'), ("other", b"x")])
        self.assertEqual(session.poll(), [])

    def test_pings_once_half_the_keepalive_is_spent(self):
        session, client = self.open(keepalive_s=60)

        self.now[0] = 29
        session.poll()
        self.assertEqual(client.pings, 0)
        self.now[0] = 30
        session.poll()
        self.assertEqual(client.pings, 1)
        self.now[0] = 50
        session.publish("telemetry", b"{}")
        self.now[0] = 79
        session.poll()
        self.assertEqual(client.pings, 1)

    def test_never_pings_without_a_keepalive(self):
        session, client = self.open(keepalive_s=0)

        self.now[0] = 10000
        session.poll()

        self.assertEqual(client.pings, 0)

    def test_raises_oserror_when_the_link_fails(self):
        session, client = self.open()
        for failure in (OSError(-1), IndexError("empty read"), AssertionError(), ValueError()):
            client.fail = failure
            with self.assertRaises(OSError):
                session.poll()
        client.fail = simple.MQTTException(0x80)
        with self.assertRaises(OSError):
            session.subscribe("command/#")

    def test_closes_a_dropped_session_quietly(self):
        session, client = self.open()
        client.fail = OSError(-1)

        session.close()

        self.assertTrue(client.disconnected)


class FlashStoreTest(PicoCase):
    def test_holds_nothing_at_first(self):
        self.assertIsNone(pico.FlashStore(STORE_PATH).load())

    def test_saves_and_loads(self):
        store = pico.FlashStore(STORE_PATH)

        store.save({"token": "a"})
        store.save({"token": "b"})

        self.assertEqual(pico.FlashStore(STORE_PATH).load(), {"token": "b"})
        with self.assertRaises(OSError):
            open(STORE_PATH + ".tmp").close()

    def test_keeps_the_last_credential_when_a_save_is_cut_short(self):
        store = pico.FlashStore(STORE_PATH)
        store.save({"token": "a"})
        with open(STORE_PATH + ".tmp", "w") as half_written:
            half_written.write('{"tok')

        self.assertEqual(store.load(), {"token": "a"})
        store.save({"token": "b"})
        self.assertEqual(store.load(), {"token": "b"})

    def test_clears(self):
        store = pico.FlashStore(STORE_PATH)
        store.save({"token": "a"})

        store.clear()
        store.clear()

        self.assertIsNone(store.load())

    def test_refuses_an_unreadable_file(self):
        for text in ("{not json", "[1]"):
            with open(STORE_PATH, "w") as file:
                file.write(text)
            with self.assertRaises(CredentialError):
                pico.FlashStore(STORE_PATH).load()


class SystemInfoTest(PicoCase):
    def test_reads_the_vital_signs(self):
        pico._uptime[0] = 0
        pico._uptime[1] = 0
        fake_time.ticks = 5500
        machine.ADC.reading = 14020
        network.WLAN.rssi = -48

        info = pico.system_info(pico.join_wifi("home", "secret"))

        major, minor, patch = sys.implementation.version[:3]
        self.assertEqual(info["uptime_s"], 5)
        self.assertEqual(info["chip_temperature_c"], 27.0)
        self.assertEqual(machine.ADC.channels, [4])
        self.assertEqual(info["wifi_rssi_dbm"], -48)
        self.assertEqual(info["firmware_version"], major * 10000 + minor * 100 + patch)
        self.assertEqual(info["free_memory_bytes"], fake_gc.free)
        self.assertEqual(fake_gc.collections, 1)

    def test_counts_uptime_across_the_tick_wrap(self):
        pico._uptime[0] = 0
        pico._uptime[1] = 0
        wlan = pico.join_wifi("home", "secret")
        info = {}
        for _ in range(3):
            fake_time.ticks = fake_time.ticks_add(fake_time.ticks, 400000000)
            info = pico.system_info(wlan)

        self.assertTrue(fake_time.ticks < 400000000)
        self.assertEqual(info["uptime_s"], 1200000)


class ConnectTest(PicoCase):
    """connect() end to end over the Pico adapters, with only the network faked."""

    def test_claims_stores_and_publishes_system_info(self):
        claimed = wire("claim")["cases"][1]
        fake_socket.response = http(200, json.dumps(claimed["body"]).encode())
        wlan = pico.join_wifi("home", "secret")

        device = pico.connect(URL, store=pico.FlashStore(STORE_PATH), claim_code="qmc_x.y")
        device.publish_reading(pico.system_info(wlan))

        stored = pico.FlashStore(STORE_PATH).load() or {}
        self.assertEqual(stored["token"], claimed["credential"]["token"])
        self.assertEqual(stored["broker"], claimed["credential"]["broker"])
        client = simple.MQTTClient.instances[0]
        self.assertEqual(
            (client.server, client.user, client.pswd),
            ("gw.test-sg-sin-a.qualithm.com", "d", "qmd_abc"),
        )
        topic, payload = client.published[0]
        self.assertEqual(topic, "capabilities")
        declared = json.loads(payload)["capabilities"]
        topic, payload = client.published[1]
        self.assertEqual(topic, "telemetry")
        self.assertEqual(
            sorted(entry["config"]["metric"] for entry in declared),
            sorted(json.loads(payload)["metrics"]),
        )

    def test_declares_the_system_sensors_with_their_units(self):
        fake_socket.response = http(200, json.dumps(wire("claim")["cases"][1]["body"]).encode())
        own = {
            "key": "soil_moisture",
            "type": "sensor",
            "metric": "soil_moisture",
            "unit": "percent",
        }

        pico.connect(
            URL, store=pico.FlashStore(STORE_PATH), claim_code="qmc_x.y", capabilities=[own]
        )

        topic, payload = simple.MQTTClient.instances[0].published[0]
        self.assertEqual(topic, "capabilities")
        self.assertEqual(
            json.loads(payload)["capabilities"],
            [
                {
                    "key": "uptime_s",
                    "type": "sensor",
                    "config": {"metric": "uptime_s", "unit": "seconds"},
                },
                {
                    "key": "free_memory_bytes",
                    "type": "sensor",
                    "config": {"metric": "free_memory_bytes", "unit": "bytes"},
                },
                {
                    "key": "chip_temperature_c",
                    "type": "sensor",
                    "config": {"metric": "chip_temperature_c", "unit": "celsius"},
                },
                {
                    "key": "wifi_rssi_dbm",
                    "type": "sensor",
                    "config": {"metric": "wifi_rssi_dbm", "unit": "dBm"},
                },
                {
                    "key": "firmware_version",
                    "type": "sensor",
                    "config": {"metric": "firmware_version"},
                },
                {
                    "key": "soil_moisture",
                    "type": "sensor",
                    "config": {"metric": "soil_moisture", "unit": "percent"},
                },
            ],
        )

    def test_refuses_a_capability_that_reuses_a_system_sensor_key(self):
        with self.assertRaises(CapabilityError):
            pico.connect(URL, capabilities=[{"key": "uptime_s", "type": "trigger"}])
        self.assertEqual(simple.MQTTClient.instances, [])

    def test_renewal_replaces_the_stored_token(self):
        fixture = wire("renew")["token"]
        store = pico.FlashStore(STORE_PATH)
        store.save(fixture["current"])
        fake_socket.response = http(fixture["status"], json.dumps(fixture["body"]).encode())

        device = connect(
            URL,
            pico.Transport(),
            store,
            capabilities=pico.SYSTEM_SENSORS,
            broker_host="gw",
            now=lambda: JAN_21_2026,
        )

        self.assertIn(
            b"authorization: " + fixture["authorization"].encode(), fake_socket.sockets[0].written
        )
        self.assertEqual((store.load() or {})["token"], "qmd_new")
        self.assertEqual(device.credential.token, "qmd_new")
        self.assertEqual(simple.MQTTClient.instances[0].pswd, "qmd_new")

    def test_an_interrupted_renewal_leaves_a_working_token(self):
        fixture = wire("renew")["token"]
        store = pico.FlashStore(STORE_PATH)
        store.save(fixture["current"])
        fake_socket.response = b'HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n{"da'

        device = connect(
            URL,
            pico.Transport(),
            store,
            capabilities=pico.SYSTEM_SENSORS,
            broker_host="gw",
            now=lambda: JAN_21_2026,
        )

        self.assertIsNotNone(device.renew_error)
        self.assertEqual(store.load(), fixture["current"])
        self.assertEqual(simple.MQTTClient.instances[0].pswd, "qmd_abc")
