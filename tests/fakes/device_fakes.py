"""In-memory fakes of the three interfaces, for tests on the host and the Unix port."""

import json


def wire(name):
    """Load a wire fixture from tests/wire/."""
    with open("tests/wire/" + name + ".json") as file:
        return json.load(file)


class Response:
    """A scripted HTTP response."""

    def __init__(self, status, body=None, raw=None):
        self.status = status
        self.raw = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())


class FakeSession:
    """An MQTT session that records publishes and subscriptions, and hands out queued messages."""

    def __init__(self, max_packet_size=None):
        self.max_packet_size = max_packet_size
        self.published = []
        self.subscribed = []
        self.inbound = []
        self.closed = False
        self.fail = False

    def publish(self, topic, payload):
        if self.fail:
            raise OSError("link down")
        self.published.append((topic, payload))

    def subscribe(self, topic_filter):
        if self.fail:
            raise OSError("link down")
        self.subscribed.append(topic_filter)

    def poll(self):
        if self.fail:
            raise OSError("link down")
        messages = self.inbound
        self.inbound = []
        return messages

    def close(self):
        self.closed = True


class FakeTransport:
    """An HTTPS client answering by path from a script; an MQTT client opening FakeSessions."""

    def __init__(self, responses=None, max_packet_size=None):
        self.responses = responses or {}
        self.requests = []
        self.opened = []
        self.sessions = []
        self.max_packet_size = max_packet_size
        self.unreachable = False
        self.refuse = False

    def request(self, method, url, headers, body):
        self.requests.append(
            (method, url, headers, None if body is None else json.loads(body.decode()))
        )
        if self.unreachable:
            raise OSError("ECONNREFUSED")
        path = url[url.index("/provision") :]
        response = self.responses[path]
        return response.status, response.raw

    def open(self, host, port, client_id, username, password, key_pem, cert_pem, keepalive_s):  # noqa: PLR0917
        if self.refuse:
            raise OSError("connection refused")
        self.opened.append(
            {
                "host": host,
                "port": port,
                "client_id": client_id,
                "username": username,
                "password": password,
                "key_pem": key_pem,
                "cert_pem": cert_pem,
                "keepalive_s": keepalive_s,
            }
        )
        session = FakeSession(self.max_packet_size)
        self.sessions.append(session)
        return session


class FakeCrypto:
    """Key generation and signing that record their inputs and return predictable values."""

    def __init__(self):
        self.generated = 0
        self.signed = []

    def generate_csr(self, device_id):
        self.generated += 1
        return "key-%d" % self.generated, "csr-for-%s-%d" % (device_id, self.generated)

    def sign(self, private_key_pem, message):
        self.signed.append((private_key_pem, message))
        return b"\x01\x02signature"


class MemoryStore:
    """A credential store held in memory, recording each save."""

    def __init__(self, credential=None):
        self.credential = credential
        self.saves = []

    def load(self):
        return self.credential

    def save(self, credential):
        self.credential = credential
        self.saves.append(credential)

    def clear(self):
        self.credential = None
