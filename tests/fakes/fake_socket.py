"""A fake of the `socket` module, patched over a board module's: streams answer from a script."""

SOCK_STREAM = 1

#: The bytes the next socket's server sends, then EOF.
response = b""
#: Raised by connect() when set.
unreachable = None
sockets = []


class Socket:
    """A stream socket that records what is written and reads from `response`."""

    def __init__(self, family, kind, proto):
        self.args = (family, kind, proto)
        self.address = None
        self.timeout = None
        self.wrapped = None
        self.written = b""
        self.closed = False
        self._pending = response
        sockets.append(self)

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        if unreachable is not None:
            raise unreachable
        self.address = address

    def write(self, data):
        self.written += data
        return len(data)

    def read(self, size):
        # A short read, as a TLS record boundary can give.
        size = min(size, 7)
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def readline(self):
        end = self._pending.find(b"\n")
        end = len(self._pending) if end < 0 else end + 1
        line, self._pending = self._pending[:end], self._pending[end:]
        return line

    def close(self):
        self.closed = True


def socket(family, kind, proto):
    return Socket(family, kind, proto)


def getaddrinfo(host, port, family, kind):
    return [(2, kind, 6, "", (host, port))]


def reset():
    global response, unreachable  # noqa: PLW0603 -- module state a test scripts
    response = b""
    unreachable = None
    sockets.clear()
