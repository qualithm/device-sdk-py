import unittest

from device_fakes import FakeTransport, Response, wire

from qualithm_device import ClaimError, claim

JAN_1_2026 = 1767225600


class ClaimTest(unittest.TestCase):
    def test_maps_each_wire_case_into_a_credential(self):
        fixture = wire("claim")
        for case in fixture["cases"]:
            transport = FakeTransport(
                {"/v1/provision/claim": Response(case["status"], case["body"])}
            )
            credential = claim(
                transport, "https://api.example.com/", fixture["request"]["code"], JAN_1_2026
            )
            want = dict(case["credential"], issuedAt="2026-01-01T00:00:00.000Z")
            self.assertEqual(credential.to_dict(), want)

    def test_keeps_the_zone_api_url_the_platform_names(self):
        body = wire("claim")["cases"][1]["body"]
        body["data"]["apiUrl"] = "https://api.test-sg-sin-a.qualithm.com/"
        transport = FakeTransport({"/v1/provision/claim": Response(200, body)})
        credential = claim(transport, "https://api.example.com", "qmc_x.y", JAN_1_2026)
        self.assertEqual(credential.api_url, "https://api.test-sg-sin-a.qualithm.com")
        self.assertEqual(credential.to_dict()["apiUrl"], "https://api.test-sg-sin-a.qualithm.com")

    def test_drops_a_zone_api_url_that_is_not_an_https_origin(self):
        for value in ("http://localhost:3000", "https://api.example.com/v1", None):
            body = wire("claim")["cases"][1]["body"]
            body["data"]["apiUrl"] = value
            transport = FakeTransport({"/v1/provision/claim": Response(200, body)})
            credential = claim(transport, "https://api.example.com", "qmc_x.y", JAN_1_2026)
            self.assertIsNone(credential.api_url)
            self.assertFalse("apiUrl" in credential.to_dict())

    def test_posts_the_code_and_name_to_the_claim_endpoint(self):
        body = wire("claim")["cases"][0]["body"]
        transport = FakeTransport({"/v1/provision/claim": Response(200, body)})
        claim(transport, "https://api.example.com/", "qmc_x.y", JAN_1_2026, name="kitchen")
        method, url, headers, sent = transport.requests[0]
        self.assertEqual((method, url), ("POST", "https://api.example.com/v1/provision/claim"))
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(sent, {"code": "qmc_x.y", "name": "kitchen"})

    def test_declares_a_virtual_device_at_claim(self):
        body = wire("claim")["cases"][0]["body"]
        transport = FakeTransport({"/v1/provision/claim": Response(200, body)})
        claim(transport, "https://api.example.com/", "qmc_x.y", JAN_1_2026, virtual=True)
        self.assertEqual(transport.requests[0][3], {"code": "qmc_x.y", "virtual": True})

    def test_reports_the_platform_message_and_status(self):
        for case in wire("claim")["errors"]:
            raw = case["raw"].encode() if "raw" in case else None
            transport = FakeTransport(
                {"/v1/provision/claim": Response(case["status"], case.get("body"), raw)}
            )
            with self.assertRaises(ClaimError) as caught:
                claim(transport, "https://api.example.com", "bad", JAN_1_2026)
            self.assertEqual(str(caught.exception), case["message"])
            self.assertEqual(caught.exception.status, case["status"])

    def test_reports_an_unreachable_endpoint(self):
        transport = FakeTransport()
        transport.unreachable = True
        with self.assertRaises(ClaimError) as caught:
            claim(transport, "https://api.example.com", "qmc_x.y", JAN_1_2026)
        self.assertIsNone(caught.exception.status)

    def test_rejects_a_response_missing_the_secret_with_its_status(self):
        body = {"data": {"deviceId": "d", "scopeId": "s", "teamId": "t"}}
        transport = FakeTransport({"/v1/provision/claim": Response(200, body)})
        with self.assertRaises(ClaimError) as caught:
            claim(transport, "https://api.example.com", "qmc_x.y", JAN_1_2026)
        self.assertEqual(str(caught.exception), "provisioning response has no secret")
        # The endpoint answered, so the status isn't the None of an unreachable one.
        self.assertEqual(caught.exception.status, 200)
