import importlib.util
import socket
import ssl
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_roots.py"
_spec = importlib.util.spec_from_file_location("check_live_roots", _SCRIPT)
if _spec is None or _spec.loader is None:
    raise ImportError(str(_SCRIPT))
check_live_roots = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_live_roots)


def _fake(verdicts):
    def check(host, port):
        return verdicts[(host, port)]

    return check


class TestCheckLiveRoots(unittest.TestCase):
    def run_check(self, targets, verdicts):
        lines = []
        code = check_live_roots.run(targets, _fake(verdicts), lines.append)
        return code, lines

    def test_passes_when_every_reachable_chain_verifies(self):
        code, lines = self.run_check(
            ["api.example.com:443", "gw.example.com:8883"],
            {
                ("api.example.com", 443): ("verified", "issued by WE1"),
                ("gw.example.com", 8883): ("unreachable", "gaierror: no record"),
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("verified"))
        self.assertIn("gw.example.com:8883", lines[1])

    def test_fails_when_a_chain_does_not_end_at_a_pinned_root(self):
        code, lines = self.run_check(
            ["api.example.com:443", "gw.example.com:8883"],
            {
                ("api.example.com", 443): ("mismatch", "unable to get local issuer certificate"),
                ("gw.example.com", 8883): ("verified", "issued by R12"),
            },
        )
        self.assertEqual(code, 1)
        self.assertIn("1 chain(s) no longer end at a pinned root", lines[-1])

    def test_fails_when_no_host_is_reachable(self):
        code, lines = self.run_check(
            ["gw.example.com:8883"],
            {("gw.example.com", 8883): ("unreachable", "timed out")},
        )
        self.assertEqual(code, 1)
        self.assertIn("no host could be reached", lines[-1])

    def test_parse_target_splits_host_and_port(self):
        self.assertEqual(
            check_live_roots.parse_target("api.example.com:443"), ("api.example.com", 443)
        )

    def test_parse_target_rejects_a_missing_or_bad_port(self):
        for target in ("api.example.com", ":443", "api.example.com:https"):
            with self.assertRaises(ValueError):
                check_live_roots.parse_target(target)

    def test_pinned_context_trusts_only_the_pinned_roots(self):
        context = check_live_roots.pinned_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(
            len(context.get_ca_certs()), check_live_roots.ROOTS.count("BEGIN CERTIFICATE")
        )

    def test_probe_reports_a_refused_connection_as_unreachable(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        verdict, detail = check_live_roots.probe(
            "127.0.0.1", port, check_live_roots.pinned_context()
        )
        self.assertEqual(verdict, "unreachable")
        self.assertTrue(detail)


if __name__ == "__main__":
    unittest.main()
