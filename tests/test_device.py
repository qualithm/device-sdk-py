import json
import unittest

from device_fakes import FakeTransport, MemoryStore, Response, wire

from qualithm_device import (
    CapabilityError,
    Credential,
    CredentialError,
    GatewayError,
    PayloadTooLargeError,
    TooManyMetricsError,
    TopicError,
    format_iso,
    renew_due,
    resolve_api_url,
    resolve_broker,
)
from qualithm_device import connect as connect_with

URL = "https://api.example.com"
ZONE_API = "https://api.test-sg-sin-a.qualithm.com"
CLAIM = "/v1/provision/claim"
RENEW = "/v1/provision/credentials/renew"
JAN_1_2026 = 1767225600
JAN_21_2026 = 1768953600
FEB_1_2026 = 1769904000
MAR_1_2026 = 1772323200

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
        # The stored credential names no zone API, so it renews where it was configured to.
        self.assertEqual(transport.requests[0][1], URL + RENEW)

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


def urls(transport):
    """The URL of each HTTPS request, in order."""
    return [request[1] for request in transport.requests]


class ZoneApiTest(unittest.TestCase):
    def due(self, api_url, response=None):
        """A transport and store whose token, naming `api_url`, is due to renew on JAN_21_2026."""
        fixture = wire("renew")["token"]
        stored = dict(fixture["current"])
        if api_url is not None:
            stored["apiUrl"] = api_url
        if response is None:
            response = Response(fixture["status"], fixture["body"])
        return FakeTransport({RENEW: response}), MemoryStore(stored)

    def test_renews_at_the_stored_zone_api(self):
        transport, store = self.due(ZONE_API)
        device = connect(URL, transport, store, broker_host="gw", now=clock(JAN_21_2026))
        self.assertEqual(urls(transport), [ZONE_API + RENEW])
        self.assertEqual(device.credential.token, "qmd_new")
        # The renewal named no API, so the stored one stays.
        self.assertEqual(store.saves[-1]["apiUrl"], ZONE_API)

    def test_falls_back_to_the_provisioning_url_when_the_zone_api_does_not_answer(self):
        body = wire("renew")["token"]["body"]
        body["data"]["apiUrl"] = "https://api.sg-sin-a.qualithm.com"
        transport, store = self.due(ZONE_API, Response(201, body))
        transport.unreachable_hosts.add("api.test-sg-sin-a.qualithm.com")
        device = connect(URL, transport, store, broker_host="gw", now=clock(JAN_21_2026))
        self.assertEqual(urls(transport), [ZONE_API + RENEW, URL + RENEW])
        self.assertEqual(device.credential.token, "qmd_new")
        self.assertIsNone(device.renew_error)
        # The answer through the provisioning URL names the zone API to use from now on.
        self.assertEqual(store.saves[-1]["apiUrl"], "https://api.sg-sin-a.qualithm.com")

    def test_falls_back_to_the_provisioning_url_on_a_server_error_from_the_zone_api(self):
        # An edge in front of an unreachable zone answers with its own error page.
        for status in (500, 503, 530):
            transport, store = self.due(ZONE_API)
            transport.host_responses[("api.test-sg-sin-a.qualithm.com", RENEW)] = Response(
                status, raw=b"<html>error 1033</html>"
            )
            device = connect(URL, transport, store, broker_host="gw", now=clock(JAN_21_2026))
            self.assertEqual(urls(transport), [ZONE_API + RENEW, URL + RENEW])
            self.assertEqual(device.credential.token, "qmd_new")
            self.assertIsNone(device.renew_error)

    def test_keeps_the_provisioning_url_error_when_both_fail(self):
        transport, store = self.due(ZONE_API, Response(503, raw=b"<html>busy</html>"))
        transport.host_responses[("api.test-sg-sin-a.qualithm.com", RENEW)] = Response(
            530, raw=b"<html>error 1033</html>"
        )
        device = connect(URL, transport, store, broker_host="gw", now=clock(JAN_21_2026))
        self.assertEqual(urls(transport), [ZONE_API + RENEW, URL + RENEW])
        assert device.renew_error is not None
        self.assertEqual(device.renew_error.status, 503)
        self.assertEqual(store.saves, [])

    def test_tries_once_when_the_provisioning_url_spells_the_zone_api_another_way(self):
        transport, store = self.due(ZONE_API, Response(503, raw=b"<html>busy</html>"))
        spelled = "https://API.test-sg-sin-a.qualithm.com:443/"
        device = connect(spelled, transport, store, broker_host="gw", now=clock(JAN_21_2026))
        self.assertEqual(urls(transport), [ZONE_API + RENEW])
        assert device.renew_error is not None
        self.assertEqual(device.renew_error.status, 503)

    def test_keeps_a_client_error_from_the_zone_api(self):
        for response in (
            Response(429, wire("renew")["errors"][2]["body"]),
            Response(401, raw=b'{"error": {"code": "unauthorized"}}'),
        ):
            transport, store = self.due(ZONE_API, response)
            device = connect(URL, transport, store, broker_host="gw", now=clock(JAN_21_2026))
            self.assertEqual(urls(transport), [ZONE_API + RENEW])
            assert device.renew_error is not None
            self.assertEqual(device.renew_error.status, response.status)
            self.assertEqual(store.saves, [])

    def test_keeps_an_answer_missing_its_secret_from_the_zone_api(self):
        body = wire("renew")["token"]["body"]
        del body["data"]["secret"]
        transport, store = self.due(ZONE_API, Response(201, body))
        device = connect(URL, transport, store, broker_host="gw", now=clock(JAN_21_2026))
        # Answered, so not tried again at the provisioning URL.
        self.assertEqual(urls(transport), [ZONE_API + RENEW])
        assert device.renew_error is not None
        self.assertEqual(device.renew_error.status, 201)
        self.assertEqual(store.saves, [])
        self.assertEqual(transport.opened[0]["password"], "qmd_abc")

    def test_tries_an_unanswered_provisioning_url_once(self):
        for api_url in (None, URL):
            transport, store = self.due(api_url)
            transport.unreachable = True
            device = connect(URL + "/", transport, store, broker_host="gw", now=clock(JAN_21_2026))
            self.assertEqual(len(transport.requests), 1)
            assert device.renew_error is not None
            self.assertIsNone(device.renew_error.status)


JAN_2_2026 = JAN_1_2026 + 86400
HOME = {"host": "gw.test-de-fra-a.qualithm.com", "port": 8883}
REFUSING = {"host": "gw.test-sg-sin-a.qualithm.com", "port": 8883}


def renewal_naming(broker):
    """The token renewal fixture's response, naming `broker` as the device's gateway."""
    fixture = wire("renew")["token"]
    body = json.loads(json.dumps(fixture["body"]))
    if broker is not None:
        body["data"]["broker"] = broker
    return fixture, Response(fixture["status"], body)


class MovedGatewayTest(unittest.TestCase):
    def moved_transport(self, broker=HOME):
        fixture, response = renewal_naming(broker)
        transport = FakeTransport({RENEW: response})
        transport.unavailable_hosts.add(REFUSING["host"])
        stored = dict(fixture["current"])
        stored["broker"] = REFUSING
        return transport, MemoryStore(stored)

    def test_follows_a_refusal_at_once_right_after_its_claim(self):
        claimed = wire("claim")["cases"][2]["body"]  # a credential that expires, so it renews
        claimed["data"]["broker"] = REFUSING
        claimed["data"]["apiUrl"] = ZONE_API
        _, renewal = renewal_naming(HOME)
        transport = FakeTransport({CLAIM: Response(200, claimed), RENEW: renewal})
        transport.unavailable_hosts.add(REFUSING["host"])
        store = MemoryStore()
        device = connect(URL, transport, store, claim_code="qmc_x.y", now=clock(JAN_1_2026))
        # One renewal, at the zone API the claim named, within the same second as the claim.
        self.assertEqual(urls(transport), [URL + CLAIM, ZONE_API + RENEW])
        self.assertEqual(
            [opened["host"] for opened in transport.opened], [REFUSING["host"], HOME["host"]]
        )
        self.assertEqual(transport.opened[-1]["password"], "qmd_new")
        self.assertEqual(store.saves[-1]["broker"], HOME)
        self.assertEqual(store.saves[-1]["issuedAt"], format_iso(JAN_1_2026))
        assert device.credential.broker is not None
        self.assertEqual(device.credential.broker.to_dict(), HOME)

    def test_renews_early_and_connects_to_the_gateway_it_names(self):
        transport, store = self.moved_transport()
        device = connect(URL, transport, store, now=clock(JAN_2_2026))
        self.assertEqual(transport.opened[-1]["host"], HOME["host"])
        self.assertEqual(transport.opened[-1]["password"], "qmd_new")
        self.assertEqual(store.saves[-1]["broker"], HOME)
        assert device.credential.broker is not None
        self.assertEqual(device.credential.broker.to_dict(), HOME)
        self.assertIsNone(device.renew_error)

    def test_follows_it_past_a_configured_host(self):
        transport, store = self.moved_transport()
        connect(
            URL,
            transport,
            store,
            broker_host="gw.test-sg-sin-a.qualithm.com",
            now=clock(JAN_2_2026),
        )
        self.assertEqual(transport.opened[-1]["host"], HOME["host"])

    def test_reports_the_refusal_when_the_gateway_has_not_moved(self):
        transport, store = self.moved_transport(REFUSING)
        with self.assertRaises(GatewayError):
            connect(URL, transport, store, now=clock(JAN_2_2026))
        self.assertEqual(len(transport.opened), 1)
        # Stored all the same, so the next boot waits before renewing early again.
        self.assertEqual(store.saves[-1]["token"], "qmd_new")
        self.assertEqual(store.saves[-1]["issuedAt"], format_iso(JAN_2_2026))

    def test_reports_the_refusal_when_renewal_names_no_gateway(self):
        transport, store = self.moved_transport(None)
        with self.assertRaises(GatewayError):
            connect(URL, transport, store, now=clock(JAN_2_2026))

    def test_does_not_renew_early_within_15_minutes_of_the_last_renewal(self):
        transport, store = self.moved_transport()
        with self.assertRaises(GatewayError):
            connect(URL, transport, store, now=clock(JAN_1_2026 + 899))
        self.assertEqual(transport.requests, [])

    def test_renews_early_once_15_minutes_have_passed(self):
        transport, store = self.moved_transport()
        connect(URL, transport, store, now=clock(JAN_1_2026 + 900))
        self.assertEqual(urls(transport), [URL + RENEW])
        self.assertEqual(transport.opened[-1]["host"], HOME["host"])

    def test_renews_early_at_most_4_times_an_hour_across_connects(self):
        # The gateway stays down, and each renewal names it again.
        transport, store = self.moved_transport(REFUSING)
        for minute in range(60):
            with self.assertRaises(GatewayError):
                connect(URL, transport, store, now=clock(JAN_2_2026 + minute * 60))
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(
            [save["issuedAt"] for save in store.saves],
            [format_iso(JAN_2_2026 + offset) for offset in (0, 900, 1800, 2700)],
        )

    def test_reports_the_refusal_when_renewal_fails(self):
        transport, store = self.moved_transport()
        transport.responses["/v1/provision/credentials/renew"] = Response(
            429, wire("renew")["errors"][2]["body"]
        )
        with self.assertRaises(GatewayError):
            connect(URL, transport, store, now=clock(JAN_2_2026))

    def test_a_certificate_needs_crypto_to_renew_early(self):
        stored = dict(wire("renew")["cert"]["current"])
        transport = FakeTransport()
        transport.unavailable_hosts.add(stored["broker"]["host"])
        with self.assertRaises(GatewayError):
            connect(URL, transport, MemoryStore(stored), now=clock(JAN_2_2026))
        self.assertEqual(transport.requests, [])

    def test_reports_a_failure_at_the_new_gateway(self):
        transport, store = self.moved_transport()
        transport.unavailable_hosts.add(HOME["host"])
        with self.assertRaises(GatewayError):
            connect(URL, transport, store, now=clock(JAN_2_2026))
        self.assertEqual(store.saves[-1]["broker"], HOME)


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


class ApiChoiceTest(unittest.TestCase):
    def test_prefers_the_stored_zone_api_to_the_configured_url(self):
        credential = Credential.from_dict(wire("renew")["token"]["current"])
        self.assertEqual(resolve_api_url(URL, credential), URL)
        credential.api_url = ZONE_API
        self.assertEqual(resolve_api_url(URL, credential), ZONE_API)


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
        self.device.publish("command/door", "open")
        self.device.publish("command/door", b"shut")
        self.assertEqual(
            self.session.published, [("command/door", b"open"), ("command/door", b"shut")]
        )

    def test_refuses_a_topic_outside_the_contract_before_sending(self):
        with self.assertRaises(TopicError):
            self.device.publish("events/door", b"open")
        self.assertEqual(self.session.published, [])

    def test_refuses_a_payload_over_the_gateway_ceiling(self):
        with self.assertRaises(PayloadTooLargeError) as caught:
            self.device.publish("telemetry", b"x" * 257)
        self.assertEqual((caught.exception.max_bytes, caught.exception.actual_bytes), (256, 257))

    def test_refuses_a_reading_over_64_metrics_before_sending(self):
        # Its own device: 64 metrics outgrow the 256-byte ceiling set up above.
        transport = claimed_transport()
        device = connect(URL, transport, MemoryStore(), claim_code="qmc_x.y", now=clock(JAN_1_2026))
        session = transport.sessions[0]
        session.published = []
        with self.assertRaises(TooManyMetricsError) as caught:
            device.publish_reading({"m" + str(i): i for i in range(65)})
        self.assertEqual((caught.exception.max_metrics, caught.exception.actual_metrics), (64, 65))
        self.assertEqual(session.published, [])
        device.publish_reading({"m" + str(i): i for i in range(64)})
        topic, payload = session.published[0]
        self.assertEqual(topic, "telemetry")
        self.assertEqual(len(json.loads(payload.decode())["metrics"]), 64)

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
        with self.assertRaises(CapabilityError):
            self.device.on_command("bad key", print)

    def test_skips_a_command_that_does_not_fit_its_declaration(self):
        self.device.declare_capabilities(
            [
                {"key": "p", "type": "onoff"},
                {"key": "l", "type": "range", "min": 0, "max": 10},
                {"key": "m", "type": "enum", "options": ["auto", "eco"]},
                {"key": "r", "type": "trigger"},
            ]
        )
        seen = []
        for key in ("p", "l", "m", "r"):
            self.device.on_command(key, lambda value, key=key: seen.append((key, value)))
        fits = [
            ("command/p", b'{"value":false}'),
            ("command/l", b'{"value":10}'),
            ("command/m", b'{"value":"auto"}'),
            ("command/r", b"{}"),
        ]
        misfits = [
            ("command/p", b'{"value":1}'),
            ("command/p", b"{}"),
            ("command/l", b'{"value":11}'),
            ("command/l", b'{"value":true}'),
            ("command/l", b'{"value":"5"}'),
            ("command/m", b'{"value":"off"}'),
            ("command/m", b'{"value":1}'),
            ("command/r", b'{"value":1}'),
        ]
        self.session.inbound = fits + misfits
        rest = self.device.poll()
        self.assertEqual(seen, [("p", False), ("l", 10), ("m", "auto"), ("r", None)])
        self.assertEqual(rest, misfits)

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

    def test_renews_at_the_zone_api_its_last_renewal_named(self):
        fixture = wire("renew")["token"]
        fixture["body"]["data"]["apiUrl"] = ZONE_API
        device, store = self.connected({RENEW: Response(201, fixture["body"])}, JAN_21_2026)
        self.assertTrue(device.renew_if_due())
        self.assertEqual(store.saves[-1]["apiUrl"], ZONE_API)
        device._now = clock(MAR_1_2026)
        self.assertTrue(device.renew_if_due())
        self.assertEqual(urls(device._transport), [URL + RENEW, ZONE_API + RENEW])

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
