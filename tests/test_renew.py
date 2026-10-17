import unittest

from device_fakes import FakeCrypto, FakeTransport, Response, wire

from qualithm_device import Credential, CredentialError, RenewError, renew, renew_proof

JAN_21_2026 = 1768953600


class RenewTest(unittest.TestCase):
    def test_renews_a_token_with_its_own_basic_auth(self):
        fixture = wire("renew")["token"]
        transport = FakeTransport(
            {"/provision/credentials/renew": Response(fixture["status"], fixture["body"])}
        )
        renewed = renew(
            transport,
            FakeCrypto(),
            "https://x",
            Credential.from_dict(fixture["current"]),
            JAN_21_2026,
        )
        self.assertEqual(renewed.token, "qmd_new")
        self.assertEqual(renewed.expires_at, "2026-03-02T00:00:00.000Z")
        self.assertEqual(renewed.issued_at, "2026-01-21T00:00:00.000Z")
        _, url, headers, sent = transport.requests[0]
        self.assertEqual(url, "https://x/provision/credentials/renew")
        self.assertEqual(headers["authorization"], fixture["authorization"])
        self.assertIsNone(sent)

    def test_renews_a_certificate_with_a_signed_csr_for_a_new_key(self):
        fixture = wire("renew")
        cert = fixture["cert"]
        transport = FakeTransport(
            {"/provision/credentials/renew": Response(cert["status"], cert["body"])}
        )
        crypto = FakeCrypto()
        renewed = renew(
            transport, crypto, "https://x", Credential.from_dict(cert["current"]), JAN_21_2026
        )
        self.assertEqual(renewed.private_key_pem, "key-1")
        self.assertEqual(renewed.certificate_pem, "new-cert")
        self.assertEqual(renewed.expires_at, "2026-03-02T00:00:00.000Z")
        _, _, headers, sent = transport.requests[0]
        self.assertFalse("authorization" in headers)
        signed_at = "2026-01-21T00:00:00.000Z"
        self.assertEqual(
            sent,
            {
                "certificatePem": "old-cert",
                "csrPem": "csr-for-d-1",
                "signedAt": signed_at,
                "signature": "AQJzaWduYXR1cmU=",
            },
        )
        key, message = crypto.signed[0]
        self.assertEqual(key, "old-key")
        self.assertEqual(message, renew_proof(signed_at, "csr-for-d-1"))
        self.assertEqual(
            message, (fixture["proofTag"] + "\n" + signed_at + "\ncsr-for-d-1").encode()
        )

    def test_reports_the_platform_message(self):
        fixture = wire("renew")
        for case in fixture["errors"]:
            transport = FakeTransport(
                {"/provision/credentials/renew": Response(case["status"], case["body"])}
            )
            with self.assertRaises(RenewError) as caught:
                renew(
                    transport,
                    FakeCrypto(),
                    "https://x",
                    Credential.from_dict(fixture["token"]["current"]),
                    0,
                )
            self.assertEqual(str(caught.exception), case["message"])
            self.assertEqual(caught.exception.status, case["status"])

    def test_refuses_a_kind_with_no_renewal(self):
        credential = Credential.from_dict(wire("renew")["token"]["current"])
        credential.kind = "psk"
        with self.assertRaises(CredentialError):
            renew(FakeTransport(), FakeCrypto(), "https://x", credential, 0)
