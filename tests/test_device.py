import json
import unittest

from device_fakes import FakeTransport, MemoryStore, Response, wire

from qualithm_device import (
    CapabilityError,
    Credential,
    CredentialError,
    GatewayError,
    PayloadTooLargeError,
    TopicError,
    renew_due,
    resolve_broker,
)
from qualithm_device import connect as connect_with

URL = "https://api.example.com"
JAN_1_2026 = 1767225600
JAN_21_2026 = 1768953600
FEB_1_2026 = 1769904000

TEMPERATURE = {"key": "temperature", "type": "sensor", "metric": "temperature", "unit": "celsius"}


def connect(*args, **kwargs):
    """connect() declaring one sensor unless the test passes its own capabilities."""
    if "capabilities" not in kwargs:
        kwargs["capabilities"] = [TEMPERATURE]
    return connect_with(*args, **kwargs)


def clock(seconds):
    return lambda: seconds


def claimed_transport(**kwargs):
    body = wire("claim")["cases"][1]["body"]
    return FakeTransport({"/v1/provision/claim": Response(200, body)}, **kwargs)


class ConnectTest(unittest.TestCase):
    def test_claims_connects_and_publishes_a_reading(self):
        transport = claimed_transport()
        store = MemoryStore()

        device = connect(URL, transport, store, claim_code="qmc_x.y", now=clock(JAN_1_2026))
        device.publish_reading({"temperature": 21.4})

        self.assertEqual(store.saves[-1]["token"], "qmd_abc")
        self.assertEqual(
            transport.opened[0],
            {
                "host": "gw.test-sg-sin-a.qualithm.com",
                "port": 8883,
                "client_id": "d",
                "username": "d",
                "password": "qmd_abc",
                "key_pem": None,
                "cert_pem": None,
                "keepalive_s": 60,
            },
        )
        topic, payload = transport.sessions[0].published[1]
        self.assertEqual(topic, "telemetry")
        self.assertEqual(
            json.loads(payload.decode()),
            {"ts": JAN_1_2026 * 1000, "metrics": {"temperature": 21.4}},
        )

    def test_reuses_the_stored_credential_instead_of_claiming(self):
        stored = wire("credential")["credentials"][0]["credential"]
        transport = FakeTransport()
        device = connect(
            URL, transport, MemoryStore(stored), broker_host="gw.example.com", now=clock(JAN_1_2026)
        )
        self.assertEqual(transport.requests, [])
        self.assertEqual(device.credential.device_id, stored["deviceId"])
        self.assertEqual(
            (transport.opened[0]["host"], transport.opened[0]["port"]), ("gw.example.com", 8883)
        )

    def test_connects_a_certificate_credential_over_mtls(self):
        stored = wire("renew")["cert"]["current"]
        transport = FakeTransport()
        connect(URL, transport, MemoryStore(stored), now=clock(JAN_1_2026))
        opened = transport.opened[0]
        self.assertEqual((opened["username"], opened["password"]), (None, None))
        self.assertEqual((opened["key_pem"], opened["cert_pem"]), ("old-key", "old-cert"))

    def test_needs_a_claim_code_on_first_boot(self):
        with self.assertRaises(CredentialError):
            connect(URL, FakeTransport(), MemoryStore())

    def test_refuses_an_expired_credential(self):
        stored = wire("renew")["token"]["current"]
        with self.assertRaises(CredentialError):
            connect(
                URL, FakeTransport(), MemoryStore(stored), broker_host="gw", now=clock(FEB_1_2026)
            )

    def test_renews_a_due_credential_before_connecting(self):
        fixture = wire("renew")["token"]
        transport = FakeTransport(
            {"/v1/provision/credentials/renew": Response(fixture["status"], fixture["body"])}
        )
        store = MemoryStore(fixture["current"])
        device = connect(URL, transport, store, broker_host="gw", now=clock(JAN_21_2026))
        self.assertEqual(store.saves[-1]["token"], "qmd_new")
        self.assertEqual(transport.opened[0]["password"], "qmd_new")
        self.assertIsNone(device.renew_error)

    def test_connects_on_the_old_credential_when_renewal_fails(self):
        fixture = wire("renew")
        transport = FakeTransport(
            {"/v1/provision/credentials/renew": Response(429, fixture["errors"][2]["body"])}
        )
        device = connect(
            URL,
            transport,
            MemoryStore(fixture["token"]["current"]),
            broker_host="gw",
            now=clock(JAN_21_2026),
        )
        self.assertEqual(transport.opened[0]["password"], "qmd_abc")
        assert device.renew_error is not None
        self.assertEqual(device.renew_error.status, 429)

    def test_a_certificate_needs_crypto_to_renew(self):
        stored = wire("renew")["cert"]["current"]
        device = connect(URL, FakeTransport(), MemoryStore(stored), now=clock(JAN_21_2026))
        self.assertIsNotNone(device.renew_error)

    def test_publishes_the_manifest_first_on_every_connect(self):
        fixture = wire("capabilities")
        stored = wire("credential")["credentials"][0]["credential"]
        transport = FakeTransport()
        for _ in range(2):
            connect(
                URL,
                transport,
                MemoryStore(stored),
                capabilities=fixture["declarations"],
                broker_host="gw",
                now=clock(JAN_1_2026),
            ).close()
        for session in transport.sessions:
            topic, payload = session.published[0]
            self.assertEqual(topic, "capabilities")
            self.assertEqual(json.loads(payload.decode()), fixture["manifest"])

    def test_requires_capabilities_before_claiming(self):
        for capabilities in (None, [], fixture_invalid()[1]):
            transport = claimed_transport()
            with self.assertRaises(CapabilityError):
                connect(
                    URL,
                    transport,
                    MemoryStore(),
                    capabilities=capabilities,
                    claim_code="qmc_x.y",
                    now=clock(JAN_1_2026),
                )
            self.assertEqual(transport.requests, [])
            self.assertEqual(transport.opened, [])

    def test_closes_the_session_when_the_manifest_is_refused(self):
        transport = claimed_transport(max_packet_size=8)
        with self.assertRaises(PayloadTooLargeError):
            connect(URL, transport, MemoryStore(), claim_code="qmc_x.y", now=clock(JAN_1_2026))
        self.assertTrue(transport.sessions[0].closed)

    def test_reports_a_refused_session(self):
        transport = claimed_transport()
        transport.refuse = True
        with self.assertRaises(GatewayError):
            connect(URL, transport, MemoryStore(), claim_code="qmc_x.y", now=clock(JAN_1_2026))


def fixture_invalid():
    return wire("capabilities")["invalid"]


class BrokerChoiceTest(unittest.TestCase):
    def test_prefers_the_configured_host_then_the_enrolled_one(self):
        credential = Credential.from_dict(wire("renew")["cert"]["current"])
        self.assertEqual(
            resolve_broker("gw.mine", None, credential).to_dict(), {"host": "gw.mine", "port": 8883}
        )
        self.assertEqual(resolve_broker(None, 9883, credential).port, 9883)
        self.assertEqual(resolve_broker(None, None, credential).host, "gw.sg-sin-a.qualithm.com")
        credential.broker = None
        with self.assertRaises(GatewayError):
            resolve_broker(None, None, credential)


class RenewDueTest(unittest.TestCase):
    def test_renews_two_thirds_through_the_lifetime(self):
        credential = Credential.from_dict(wire("renew")["token"]["current"])
        self.assertFalse(renew_due(credential, JAN_21_2026 - 1))
        self.assertTrue(renew_due(credential, JAN_21_2026))
        credential.expires_at = None
        self.assertFalse(renew_due(credential, FEB_1_2026))


class DeviceTest(unittest.TestCase):
    def setUp(self):
        self.transport = claimed_transport(max_packet_size=256)
        self.store = MemoryStore()
        self.device = connect(
            URL, self.transport, self.store, claim_code="qmc_x.y", now=clock(JAN_1_2026)
        )
        self.session = self.transport.sessions[0]
        self.session.published = []

    def test_publishes_text_and_bytes(self):
        self.device.publish("events/door", "open")
        self.device.publish("events/door", b"shut")
        self.assertEqual(
            self.session.published, [("events/door", b"open"), ("events/door", b"shut")]
        )

    def test_refuses_a_payload_over_the_gateway_ceiling(self):
        with self.assertRaises(PayloadTooLargeError) as caught:
            self.device.publish("telemetry", b"x" * 257)
        self.assertEqual((caught.exception.max_bytes, caught.exception.actual_bytes), (256, 257))

    def test_checks_topics_before_sending(self):
        with self.assertRaises(TopicError):
            self.device.publish("a/#", b"")
        with self.assertRaises(TopicError):
            self.device.subscribe("a/#/b")
        self.assertEqual(self.session.published, [])

    def test_dispatches_commands_and_returns_the_rest(self):
        seen = []
        self.device.on_command("power", seen.append)
        self.device.on_command("reboot", seen.append)
        self.assertEqual(self.session.subscribed, ["command/#"])
        self.session.inbound = [
            ("command/power", b'{"value":true}'),
            ("command/reboot", b"{}"),
            ("command/power", b"not json"),
            ("command/fan", b'{"value":1}'),
            ("events/other", b"x"),
        ]
        rest = self.device.poll()
        self.assertEqual(seen, [True, None])
        self.assertEqual(
            [topic for topic, _ in rest], ["command/power", "command/fan", "events/other"]
        )

    def test_rejects_an_invalid_command_key(self):
        with self.assertRaises(TopicError):
            self.device.on_command("bad key", print)

    def test_a_sensor_takes_no_commands(self):
        with self.assertRaises(CapabilityError):
            self.device.on_command("temperature", print)
        self.assertEqual(self.session.subscribed, [])

    def test_redeclares_and_publishes_the_new_manifest(self):
        self.device.declare_capabilities([{"key": "temperature", "type": "onoff"}])
        topic, payload = self.session.published[0]
        self.assertEqual(topic, "capabilities")
        self.assertEqual(
            json.loads(payload.decode()),
            {"capabilities": [{"key": "temperature", "type": "onoff", "config": {}}]},
        )
        self.device.on_command("temperature", print)
        self.assertEqual(self.session.subscribed, ["command/#"])

    def test_refuses_an_invalid_redeclaration(self):
        with self.assertRaises(CapabilityError):
            self.device.declare_capabilities([])
        self.assertEqual(self.session.published, [])
        with self.assertRaises(CapabilityError):
            self.device.on_command("temperature", print)

    def test_reports_a_dropped_session(self):
        self.session.fail = True
        for call in (
            lambda: self.device.publish("t", b""),
            lambda: self.device.subscribe("t"),
            self.device.poll,
        ):
            with self.assertRaises(GatewayError):
                call()

    def test_cannot_be_used_after_close(self):
        self.device.close()
        self.device.close()
        self.assertTrue(self.session.closed)
        with self.assertRaises(GatewayError):
            self.device.publish("telemetry", b"")


class RenewIfDueTest(unittest.TestCase):
    def connected(self, responses, now):
        fixture = wire("renew")["token"]
        transport = FakeTransport(responses)
        store = MemoryStore(fixture["current"])
        device = connect(URL, transport, store, broker_host="gw", now=clock(JAN_1_2026))
        device._now = clock(now)
        return device, store

    def test_stores_the_renewed_credential_once_due(self):
        fixture = wire("renew")["token"]
        device, store = self.connected(
            {"/v1/provision/credentials/renew": Response(201, fixture["body"])}, JAN_21_2026
        )
        self.assertTrue(device.renew_if_due())
        self.assertEqual(store.saves[-1]["token"], "qmd_new")
        self.assertEqual(device.credential.token, "qmd_new")

    def test_does_nothing_before_it_is_due(self):
        device, store = self.connected({}, JAN_1_2026)
        self.assertFalse(device.renew_if_due())
        self.assertEqual(store.saves, [])

    def test_keeps_the_failure_and_the_old_credential(self):
        device, store = self.connected(
            {"/v1/provision/credentials/renew": Response(500, raw=b"oops")}, JAN_21_2026
        )
        self.assertFalse(device.renew_if_due())
        assert device.renew_error is not None
        self.assertEqual(device.renew_error.status, 500)
        self.assertEqual(store.saves, [])
