import unittest

from device_fakes import FakeCrypto, FakeTransport, Response, wire

from qualithm_device import Credential, CredentialError, RenewError, renew, renew_proof

JAN_21_2026 = 1768953600
OLD_API = "https://api.test-sg-sin-a.qualithm.com"
NEW_API = "https://api.sg-sin-a.qualithm.com"


class RenewTest(unittest.TestCase):
    def renewed(self, kind, named):
        """Renew the `kind` fixture, stored with OLD_API, when the response names `named`."""
        case = wire("renew")[kind]
        if named is not None:
            case["body"]["data"]["apiUrl"] = named
        transport = FakeTransport(
            {"/v1/provision/credentials/renew": Response(case["status"], case["body"])}
        )
        current = Credential.from_dict(dict(case["current"], apiUrl=OLD_API))
        return renew(transport, FakeCrypto(), OLD_API, current, JAN_21_2026)

    def test_takes_the_zone_api_url_the_renewal_names(self):
        for kind in ("token", "cert"):
            self.assertEqual(self.renewed(kind, NEW_API + "/").api_url, NEW_API)

    def test_keeps_the_zone_api_url_when_the_renewal_names_none(self):
        for kind in ("token", "cert"):
            self.assertEqual(self.renewed(kind, None).api_url, OLD_API)
            self.assertEqual(self.renewed(kind, "http://localhost:3000").api_url, OLD_API)

    def test_renews_a_token_with_its_own_basic_auth(self):
        fixture = wire("renew")["token"]
        transport = FakeTransport(
            {"/v1/provision/credentials/renew": Response(fixture["status"], fixture["body"])}
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
        self.assertEqual(url, "https://x/v1/provision/credentials/renew")
        self.assertEqual(headers["authorization"], fixture["authorization"])
        self.assertIsNone(sent)

    def test_renews_a_certificate_with_a_signed_csr_for_a_new_key(self):
        fixture = wire("renew")
        cert = fixture["cert"]
        transport = FakeTransport(
            {"/v1/provision/credentials/renew": Response(cert["status"], cert["body"])}
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
                {"/v1/provision/credentials/renew": Response(case["status"], case["body"])}
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

    def test_rejects_a_response_missing_the_new_credential_with_its_status(self):
        for kind, field in (("token", "secret"), ("cert", "certificatePem")):
            case = wire("renew")[kind]
            del case["body"]["data"][field]
            transport = FakeTransport(
                {"/v1/provision/credentials/renew": Response(case["status"], case["body"])}
            )
            current = Credential.from_dict(case["current"])
            with self.assertRaises(RenewError) as caught:
                renew(transport, FakeCrypto(), "https://x", current, JAN_21_2026)
            self.assertEqual(str(caught.exception), "provisioning response has no " + field)
            # The endpoint answered, so the status isn't the None of an unreachable one.
            self.assertEqual(caught.exception.status, case["status"])

    def test_refuses_a_kind_with_no_renewal(self):
        credential = Credential.from_dict(wire("renew")["token"]["current"])
        credential.kind = "psk"
        with self.assertRaises(CredentialError):
            renew(FakeTransport(), FakeCrypto(), "https://x", credential, 0)
