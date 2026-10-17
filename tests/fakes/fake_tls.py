"""A fake of MicroPython's `tls`, patched over a board module's: the Unix port's own wins."""

PROTOCOL_TLS_CLIENT = 0
CERT_NONE = 0
CERT_REQUIRED = 2

contexts = []
#: Raised by the next handshake when set, as a certificate that doesn't verify would.
handshake_error = None


class SSLContext:
    """Records what a context trusts and presents, and marks the sockets it wraps."""

    def __init__(self, protocol):
        self.protocol = protocol
        self.verify_mode = CERT_NONE
        self.cadata = None
        self.cert_chain = None
        contexts.append(self)

    def load_verify_locations(self, cadata):
        self.cadata = cadata

    def load_cert_chain(self, cert, key):
        self.cert_chain = (cert, key)

    def wrap_socket(self, sock, server_hostname=None):
        if handshake_error is not None:
            raise handshake_error
        sock.wrapped = (self, server_hostname)
        return sock


def reset():
    global handshake_error  # noqa: PLW0603 -- module state a test scripts
    handshake_error = None
    contexts.clear()
