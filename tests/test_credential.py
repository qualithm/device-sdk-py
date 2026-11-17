import unittest

from device_fakes import wire

from qualithm_device import Broker, Credential, CredentialError, parse_api_url, parse_broker

ZONE_API = "https://api.sg-sin-a.qualithm.com"


class ApiUrlTest(unittest.TestCase):
    def test_keeps_an_https_origin_lowercased_without_a_default_port_or_trailing_slash(self):
        for value, want in (
            (ZONE_API, ZONE_API),
            (ZONE_API + "/", ZONE_API),
            ("https://api.example.com:8443", "https://api.example.com:8443"),
            ("https://10.0.0.7:8443/", "https://10.0.0.7:8443"),
            ("https://10.0.0.7:443/", "https://10.0.0.7"),
            ("https://API-1.Example.com:08443", "https://api-1.example.com:8443"),
            ("https://API.example.com:443", "https://api.example.com"),
        ):
            self.assertEqual(parse_api_url(value), want)

    def test_ignores_anything_but_an_https_origin(self):
        for value in (
            None,
            443,
            {"host": "api.example.com"},
            "",
            "api.example.com",
            "http://localhost:3000",
            "HTTPS://api.example.com",
            "https://",
            "https:///",
            "https://:443",
            "https://api.example.com:",
            "https://api.example.com:0",
            "https://api.example.com:65536",
            "https://api.example.com:443443",
            "https://api.example.com:44a",
            "https://api.example.com:443:1",
            "https://user@api.example.com",
            "https://user:secret@api.example.com",
            "https://api.example.com/v1",
            "https://api.example.com//",
            "https://api.example.com?zone=a",
            "https://api.example.com#zone",
            "https://api example.com",
            "https://api.example.com\n",
            " https://api.example.com",
            "https://[::1]",
            "https://api.exämple.com",
        ):
            self.assertIsNone(parse_api_url(value))


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
            credential = Credential.from_dict(stored)
            # Stored before the platform named a zone API, so it has none.
            self.assertIsNone(credential.api_url)
            self.assertEqual(credential.to_dict(), stored)

    def test_round_trips_a_certificate_with_its_broker(self):
        stored = wire("renew")["cert"]["current"]
        credential = Credential.from_dict(stored)
        self.assertEqual(credential.broker, Broker("gw.sg-sin-a.qualithm.com", 8883))
        self.assertEqual(credential.to_dict(), stored)

    def test_round_trips_the_zone_api_url(self):
        stored = dict(wire("renew")["cert"]["current"], apiUrl=ZONE_API)
        credential = Credential.from_dict(stored)
        self.assertEqual(credential.api_url, ZONE_API)
        self.assertEqual(credential.to_dict(), stored)

    def test_loads_a_malformed_zone_api_url_as_none(self):
        token = wire("credential")["credentials"][0]["credential"]
        for value in ("http://localhost:3000", "https://api.example.com/v1", 443, ""):
            credential = Credential.from_dict(dict(token, apiUrl=value))
            self.assertIsNone(credential.api_url)
            self.assertEqual(credential.to_dict(), token)

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
