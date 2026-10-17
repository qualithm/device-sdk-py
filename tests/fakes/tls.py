"""The `tls` module CPython lacks, so a board module imports; tests patch in `fake_tls`."""

from fake_tls import CERT_NONE, CERT_REQUIRED, PROTOCOL_TLS_CLIENT, SSLContext  # noqa: F401
