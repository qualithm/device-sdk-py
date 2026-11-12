import json
import unittest

from device_fakes import FakeSession, FakeTransport, MemoryStore

from qualithm_device import (
    RECONNECT_FLOOR_MS,
    CredentialError,
    GatewayError,
    GatewayUnavailableError,
    connect,
)
from qualithm_device._time import format_iso

URL = "https://api.example.com"
T0 = 1767225600
#: An 11-minute credential, as the dev proof mints.
LIFETIME_S = 660
TEMPERATURE = {"key": "temperature", "type": "sensor", "metric": "temperature", "unit": "celsius"}
POWER = {"key": "power", "type": "onoff"}


class Clock:
    """A clock a test moves by hand."""

    def __init__(self, seconds):
        self.seconds = seconds

    def __call__(self):
        return self.seconds


class GatewaySession(FakeSession):
    """A session the gateway drops once the credential it opened with has expired."""

    def __init__(self, gateway, password):
        super().__init__()
        self.gateway = gateway
        self.password = password

    def check(self):
        if self.gateway.clock() >= self.gateway.expiry[self.password]:
            self.fail = True

    def publish(self, topic, payload):
        self.check()
        super().publish(topic, payload)

    def poll(self):
        self.check()
        return super().poll()


class Gateway(FakeTransport):
    """A platform that renews tokens for another lifetime and refuses expired ones."""

    def __init__(self, clock, *, renews=True):
        super().__init__()
        self.clock = clock
        self.renews = renews
        self.expiry = {"qmd_0": T0 + LIFETIME_S}
        self.refusals = 0
        #: The gateway a renewal names, if any.
        self.moved_to = ""
        #: Open sessions that take the manifest, then fail.
        self.flaky = False

    def request(self, method, url, headers, body):
        self.requests.append((method, url, headers, body))
        if not self.renews:
            return 500, b"oops"
        secret = "qmd_%d" % len(self.expiry)
        expires = self.clock() + LIFETIME_S
        self.expiry[secret] = expires
        body = {
            "success": True,
            "data": {
                "credential": {"id": "c", "kind": "token", "expiresAt": format_iso(expires)},
                "secret": secret,
            },
        }
        if self.moved_to:
            body["data"]["broker"] = {"host": self.moved_to, "port": 8883}
        return 201, json.dumps(body).encode()

    def open(self, host, port, client_id, username, password, key_pem, cert_pem, keepalive_s):  # noqa: PLR0917
        if self.refusals:
            self.refusals -= 1
            raise OSError("connection refused")
        if host in self.unavailable_hosts:
            raise GatewayUnavailableError("the gateway is unavailable (code 3)")
        if self.clock() >= self.expiry[password]:
            raise OSError("the gateway refused the request (code 5)")
        self.opened.append({"host": host, "password": password})
        session = (FlakySession if self.flaky else GatewaySession)(self, password)
        self.sessions.append(session)
        return session


class FlakySession(GatewaySession):
    """A session that takes the manifest and fails everything after it."""

    def publish(self, topic, payload):
        if topic != "capabilities":
            raise OSError("link down")
        super().publish(topic, payload)

    def poll(self):
        raise OSError("link down")

    def subscribe(self, topic_filter):
        raise OSError("link down")


def credential():
    return {
        "deviceId": "d",
        "teamId": "t",
        "scopeId": "s",
        "kind": "token",
        "token": "qmd_0",
        "issuedAt": format_iso(T0),
        "expiresAt": format_iso(T0 + LIFETIME_S),
    }


class QuickStartTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(T0)
        self.gateway = Gateway(self.clock)
        self.slept = []
        self.device = self.started(self.gateway)

    def started(self, gateway):
        device = connect(
            URL,
            gateway,
            MemoryStore(credential()),
            capabilities=[TEMPERATURE, POWER],
            broker_host="gw",
            now=self.clock,
        )
        device._sleep_ms = self.sleep
        return device

    def sleep(self, ms):
        self.slept.append(ms)
        if len(self.slept) > 10:
            raise AssertionError("reconnect never succeeded")

    def run_loop(self, minutes, each_minute=None):
        """The README quick start's loop, unchanged, a reading a minute."""
        for minute in range(minutes):
            if each_minute is not None:
                each_minute(minute)
            self.device.publish_reading({"temperature": 21.0})
            self.device.poll()
            self.device.renew_if_due()
            self.clock.seconds += 60

    def topics(self, session):
        return [topic for topic, _ in session.published]

    def readings(self):
        return sum(self.topics(session).count("telemetry") for session in self.gateway.sessions)

    def test_keeps_publishing_across_renewal_and_expiry(self):
        self.run_loop(40)
        self.assertEqual(self.readings(), 40)
        # Each renewal reopened the session on the new token before the old one expired.
        passwords = [opened["password"] for opened in self.gateway.opened]
        self.assertEqual(passwords, ["qmd_0", "qmd_1", "qmd_2", "qmd_3", "qmd_4"])
        self.assertTrue(all(session.closed for session in self.gateway.sessions[:-1]))
        self.assertEqual(self.slept, [])
        for session in self.gateway.sessions:
            self.assertEqual(self.topics(session)[0], "capabilities")

    def test_reconnects_a_dropped_session_and_subscribes_again(self):
        seen = []
        self.device.on_command("power", seen.append)
        self.device.subscribe("command/#")

        def drop(minute):
            if minute == 3:
                self.gateway.sessions[-1].fail = True
                self.gateway.refusals = 2

        self.run_loop(6, drop)
        self.assertEqual(self.readings(), 6)
        self.assertEqual(len(self.slept), 2)
        self.assertTrue(RECONNECT_FLOOR_MS <= self.slept[0] < 1000)
        self.assertTrue(RECONNECT_FLOOR_MS <= self.slept[1] < 2000)
        fresh = self.gateway.sessions[-1]
        self.assertEqual(len(self.gateway.sessions), 2)
        self.assertEqual(self.topics(fresh)[0], "capabilities")
        self.assertEqual(fresh.subscribed, ["command/#"])
        fresh.inbound = [("command/power", b'{"value":true}')]
        self.assertEqual(self.device.poll(), [])
        self.assertEqual(seen, [True])

    def test_republishes_the_redeclared_capabilities(self):
        self.device.declare_capabilities([POWER])
        self.gateway.sessions[-1].fail = True
        self.device.poll()
        _, manifest = self.gateway.sessions[-1].published[0]
        self.assertEqual(json.loads(manifest.decode())["capabilities"][0]["key"], "power")

    def test_raises_once_the_credential_expired_with_no_renewal(self):
        self.gateway.renews = False
        with self.assertRaises(CredentialError):
            self.run_loop(20)
        # Every reading before the expiry went out, then the gateway dropped the session.
        self.assertEqual(self.readings(), 11)
        self.assertIsNotNone(self.device.renew_error)
        self.assertEqual(len(self.gateway.sessions), 1)

    def test_reports_a_fresh_session_that_fails_too(self):
        self.gateway.flaky = True
        self.gateway.sessions[-1].fail = True
        for call in (
            lambda: self.device.publish("telemetry", b""),
            lambda: self.device.subscribe("t"),
            self.device.poll,
        ):
            with self.assertRaises(GatewayError):
                call()
        self.assertEqual(self.slept, [])

    def test_a_closed_device_does_not_reconnect(self):
        self.device.close()
        self.assertFalse(self.device.renew_if_due())
        self.clock.seconds += LIFETIME_S * 2 // 3
        self.assertTrue(self.device.renew_if_due())
        self.assertEqual(len(self.gateway.sessions), 1)
        with self.assertRaises(GatewayError):
            self.device.poll()

    def test_follows_a_moved_gateway_on_reconnect(self):
        self.clock.seconds += 3600  # old enough to renew early
        self.gateway.expiry["qmd_0"] = T0 + 7200
        self.device.credential.expires_at = format_iso(T0 + 7200)
        self.gateway.moved_to = "gw2"
        self.gateway.unavailable_hosts = {"gw"}
        self.gateway.sessions[-1].fail = True
        self.device.poll()
        self.assertEqual(self.gateway.opened[-1]["host"], "gw2")
        self.assertEqual(self.device.credential.token, "qmd_1")
        self.assertEqual(self.slept, [])


if __name__ == "__main__":
    unittest.main()
