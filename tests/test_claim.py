import unittest

from device_fakes import FakeTransport, Response, wire

from qualithm_device import ClaimError, claim

JAN_1_2026 = 1767225600


class ClaimTest(unittest.TestCase):
    def test_maps_each_wire_case_into_a_credential(self):
        fixture = wire("claim")
        for case in fixture["cases"]:
            transport = FakeTransport({"/provision/claim": Response(case["status"], case["body"])})
            credential = claim(
                transport, "https://api.example.com/", fixture["request"]["code"], JAN_1_2026
            )
            want = dict(case["credential"], issuedAt="2026-01-01T00:00:00.000Z")
            self.assertEqual(credential.to_dict(), want)

    def test_posts_the_code_and_name_to_the_claim_endpoint(self):
        body = wire("claim")["cases"][0]["body"]
        transport = FakeTransport({"/provision/claim": Response(200, body)})
        claim(transport, "https://api.example.com/", "qmc_x.y", JAN_1_2026, name="kitchen")
        method, url, headers, sent = transport.requests[0]
        self.assertEqual((method, url), ("POST", "https://api.example.com/provision/claim"))
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(sent, {"code": "qmc_x.y", "name": "kitchen"})

    def test_reports_the_platform_message_and_status(self):
        for case in wire("claim")["errors"]:
            raw = case["raw"].encode() if "raw" in case else None
            transport = FakeTransport(
                {"/provision/claim": Response(case["status"], case.get("body"), raw)}
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

    def test_rejects_a_response_missing_the_secret(self):
        body = {"data": {"deviceId": "d", "spaceId": "s", "teamId": "t"}}
        transport = FakeTransport({"/provision/claim": Response(200, body)})
        with self.assertRaises(ClaimError):
            claim(transport, "https://api.example.com", "qmc_x.y", JAN_1_2026)
