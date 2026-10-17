import unittest

from device_fakes import wire

from qualithm_device import Broker, Credential, CredentialError, parse_broker


class BrokerTest(unittest.TestCase):
    def test_reads_the_wire_cases(self):
        cases = wire("broker")
        for case in cases["valid"]:
            broker = parse_broker(case["value"])
            assert broker is not None
            self.assertEqual(broker.to_dict(), case["broker"])
        for value in cases["invalid"]:
            self.assertIsNone(parse_broker(value))

    def test_compares_by_endpoint(self):
        self.assertEqual(Broker("gw", 1), Broker("gw", 1))
        self.assertNotEqual(Broker("gw", 1), Broker("gw", 2))
        self.assertNotEqual(Broker("gw", 1), ("gw", 1))
        self.assertEqual(hash(Broker("gw", 1)), hash(Broker("gw", 1)))


class CredentialTest(unittest.TestCase):
    def test_round_trips_device_sdk_js_stored_credentials(self):
        for case in wire("credential")["credentials"]:
            stored = case["credential"]
            self.assertEqual(Credential.from_dict(stored).to_dict(), stored)

    def test_round_trips_a_certificate_with_its_broker(self):
        stored = wire("renew")["cert"]["current"]
        credential = Credential.from_dict(stored)
        self.assertEqual(credential.broker, Broker("gw.sg-sin-a.qualithm.com", 8883))
        self.assertEqual(credential.to_dict(), stored)

    def test_reads_the_expiry(self):
        stored = wire("credential")["credentials"][1]["credential"]
        self.assertEqual(Credential.from_dict(stored).expires_at_s(), 1893456000)
        self.assertIsNone(
            Credential.from_dict(wire("credential")["credentials"][0]["credential"]).expires_at_s()
        )

    def test_rejects_a_credential_missing_what_its_kind_needs(self):
        token = wire("credential")["credentials"][0]["credential"]
        broken = [
            {key: value for key, value in token.items() if key != "deviceId"},
            {key: value for key, value in token.items() if key != "token"},
            dict(token, kind="cert"),
            dict(token, kind="psk"),
            dict(token, deviceId=""),
        ]
        for stored in broken:
            with self.assertRaises(CredentialError):
                Credential.from_dict(stored)
