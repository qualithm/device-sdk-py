import unittest

from device_fakes import FakeCrypto, FakeTransport, Response, wire

from qualithm_device import Credential, EnrollError, enroll_certificate

JAN_1_2026 = 1767225600


class EnrollTest(unittest.TestCase):
    def test_trades_the_token_for_a_certificate(self):
        fixture = wire("enroll")
        for case in fixture["cases"]:
            transport = FakeTransport(
                {"/v1/provision/credentials/cert": Response(case["status"], case["body"])}
            )
            crypto = FakeCrypto()
            claimed = Credential.from_dict(fixture["claimed"])
            credential = enroll_certificate(
                transport,
                crypto,
                "https://api.example.com",
                claimed,
                JAN_1_2026,
                label="lab",
                expires_in_days=30,
            )
            self.assertEqual(credential.kind, "cert")
            self.assertEqual(credential.private_key_pem, "key-1")
            self.assertEqual(credential.certificate_pem, "cert-pem")
            self.assertEqual(credential.ca_certificate_pem, "ca-pem")
            self.assertIsNone(credential.token)
            assert credential.broker is not None
            self.assertEqual(credential.broker.to_dict(), case["broker"])
            _, url, headers, sent = transport.requests[0]
            self.assertEqual(url, "https://api.example.com/v1/provision/credentials/cert")
            self.assertEqual(headers["authorization"], fixture["authorization"])
            self.assertEqual(sent, {"csrPem": "csr-for-d-1", "label": "lab", "expiresInDays": 30})

    def test_takes_the_zone_api_url_from_the_response_or_keeps_the_claimed_one(self):
        claimed_api = "https://api.test-sg-sin-a.qualithm.com"
        for named, want in (
            ("https://api.sg-sin-a.qualithm.com", "https://api.sg-sin-a.qualithm.com"),
            (None, claimed_api),
            ("http://localhost:3000", claimed_api),
        ):
            fixture = wire("enroll")
            body = fixture["cases"][0]["body"]
            if named is not None:
                body["data"]["apiUrl"] = named
            transport = FakeTransport({"/v1/provision/credentials/cert": Response(201, body)})
            claimed = Credential.from_dict(dict(fixture["claimed"], apiUrl=claimed_api))
            credential = enroll_certificate(
                transport, FakeCrypto(), claimed_api, claimed, JAN_1_2026
            )
            self.assertEqual(credential.api_url, want)
            self.assertEqual(credential.to_dict()["apiUrl"], want)

    def test_reports_the_platform_message(self):
        fixture = wire("enroll")
        for case in fixture["errors"]:
            raw = case["raw"].encode() if "raw" in case else None
            transport = FakeTransport(
                {"/v1/provision/credentials/cert": Response(case["status"], case.get("body"), raw)}
            )
            with self.assertRaises(EnrollError) as caught:
                enroll_certificate(
                    transport,
                    FakeCrypto(),
                    "https://x",
                    Credential.from_dict(fixture["claimed"]),
                    0,
                )
            self.assertEqual(str(caught.exception), case["message"])

    def test_rejects_a_response_missing_the_certificate_with_its_status(self):
        fixture = wire("enroll")
        body = fixture["cases"][0]["body"]
        del body["data"]["certificatePem"]
        transport = FakeTransport({"/v1/provision/credentials/cert": Response(201, body)})
        claimed = Credential.from_dict(fixture["claimed"])
        with self.assertRaises(EnrollError) as caught:
            enroll_certificate(transport, FakeCrypto(), "https://x", claimed, JAN_1_2026)
        self.assertEqual(str(caught.exception), "provisioning response has no certificatePem")
        self.assertEqual(caught.exception.status, 201)

    def test_needs_a_token_credential(self):
        current = Credential.from_dict(wire("renew")["cert"]["current"])
        with self.assertRaises(EnrollError):
            enroll_certificate(FakeTransport(), FakeCrypto(), "https://x", current, 0)

    def test_reports_an_unreachable_endpoint(self):
        transport = FakeTransport()
        transport.unreachable = True
        with self.assertRaises(EnrollError):
            enroll_certificate(
                transport,
                FakeCrypto(),
                "https://x",
                Credential.from_dict(wire("enroll")["claimed"]),
                0,
            )
