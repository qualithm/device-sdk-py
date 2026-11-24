"""Check that the live API and gateway certificate chains end at a root the SDK pins.

Devices trust only `qualithm_device._roots.ROOTS`. If a host's chain moves to another CA, every
device on a frozen image fails to provision, renew or connect until it is reflashed, so this check
runs on a schedule and fails the moment a chain stops verifying against those roots.

Usage: `python scripts/check_live_roots.py HOST:PORT [HOST:PORT ...]`

A host with no DNS record, or one that refuses or times out, is reported as unreachable and
skipped: a test cluster that's torn down has no gateway. A chain that doesn't verify fails the
check, and so does a run where no host could be reached at all.
"""

import socket
import ssl
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qualithm_device._roots import ROOTS

TIMEOUT_MS = 10_000

VERIFIED = "verified"
UNREACHABLE = "unreachable"
MISMATCH = "mismatch"


def pinned_context() -> ssl.SSLContext:
    """Return a TLS client context that trusts only the pinned roots."""
    return ssl.create_default_context(cadata=ROOTS)


def probe(host: str, port: int, context: ssl.SSLContext) -> tuple[str, str]:
    """Connect to one host and say whether its chain ends at a pinned root.

    Args:
        host: the hostname, also sent as SNI and checked against the certificate.
        port: the TLS port.
        context: the context to verify with.

    Returns:
        The verdict (`verified`, `unreachable` or `mismatch`) and a one-line detail.
    """
    try:
        with (
            socket.create_connection((host, port), timeout=TIMEOUT_MS / 1000) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert() or {}
    except ssl.SSLCertVerificationError as error:
        return MISMATCH, error.verify_message or str(error)
    except (OSError, ssl.SSLError) as error:
        return UNREACHABLE, "%s: %s" % (type(error).__name__, error)
    issuer = dict(pair[0] for pair in cert.get("issuer", ()))
    return VERIFIED, "issued by %s" % issuer.get("commonName", "an unnamed CA")


def parse_target(target: str) -> tuple[str, int]:
    """Split a `HOST:PORT` argument.

    Args:
        target: the argument.

    Returns:
        The host and the port.

    Raises:
        ValueError: when the argument has no port or a port that isn't a number.
    """
    host, sep, port = target.rpartition(":")
    if not sep or not host or not port.isdigit():
        msg = "expected HOST:PORT, got %r" % target
        raise ValueError(msg)
    return host, int(port)


def run(
    targets: list[str],
    check: Callable[[str, int], tuple[str, str]],
    out: Callable[[str], None] = print,
) -> int:
    """Probe every target, report each verdict, and return the exit code.

    Args:
        targets: `HOST:PORT` arguments.
        check: probes one host and port, returning a verdict and detail.
        out: writes one report line.

    Returns:
        0 when every reachable chain verifies and at least one did; 1 otherwise.
    """
    verified = 0
    failed = 0
    for target in targets:
        host, port = parse_target(target)
        verdict, detail = check(host, port)
        out("%-11s %s:%d  %s" % (verdict, host, port, detail))
        if verdict == VERIFIED:
            verified += 1
        elif verdict == MISMATCH:
            failed += 1
    if failed:
        out("%d chain(s) no longer end at a pinned root (src/qualithm_device/_roots.py)" % failed)
        return 1
    if not verified:
        out("no host could be reached, so no chain was checked")
        return 1
    return 0


def main() -> int:
    """Run the check on the command-line targets."""
    if len(sys.argv) < 2:  # noqa: PLR2004
        print(__doc__)
        return 2
    context = pinned_context()
    return run(sys.argv[1:], lambda host, port: probe(host, port, context))


if __name__ == "__main__":
    sys.exit(main())
