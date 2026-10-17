"""A host fake of micropython-lib's `umqtt.simple`: a client that records and replays packets."""

PUBLISH = 0x30


class MQTTException(Exception):  # noqa: N818 -- umqtt's own name
    pass


class Sock:
    """The socket the real client opens, for the TLS context to wrap."""


class MQTTClient:
    """Records the CONNECT fields and every call; `inbound` holds messages check_msg delivers."""

    instances = []  # noqa: RUF012 -- state a test scripts
    #: The CONNACK return code the next connect() fails with, or None to accept.
    refuse: "int | None" = None

    def __init__(self, client_id, server, port=0, user=None, password=None, keepalive=0, ssl=None):  # noqa: PLR0917 -- umqtt's own signature
        self.client_id = client_id
        self.server = server
        self.port = port
        self.user = user
        self.pswd = password
        self.keepalive = keepalive
        self.ssl = ssl
        self.cb = None
        self.connected = None
        self.published = []
        self.subscribed = []
        self.pings = 0
        self.disconnected = False
        self.inbound = []
        #: Raised by the next publish, subscribe, ping, check_msg or disconnect when set.
        self.fail = None
        MQTTClient.instances.append(self)

    def set_callback(self, f):
        self.cb = f

    def connect(self, clean_session=True, timeout=None):
        if self.ssl is not None:
            self.ssl.wrap_socket(Sock(), server_hostname=self.server)
        if MQTTClient.refuse is not None:
            raise MQTTException(MQTTClient.refuse)
        self.connected = (clean_session, timeout)
        return 0

    def _check(self):
        if self.fail is not None:
            raise self.fail

    def publish(self, topic, msg, retain=False, qos=0):
        self._check()
        self.published.append((topic, msg))

    def subscribe(self, topic, qos=0):
        self._check()
        if self.cb is None:
            raise AssertionError("Subscribe callback is not set")
        self.subscribed.append(topic)

    def ping(self):
        self._check()
        self.pings += 1

    def check_msg(self):
        self._check()
        if not self.inbound:
            return None
        topic, msg = self.inbound.pop(0)
        assert self.cb is not None
        self.cb(topic, msg)
        return PUBLISH

    def disconnect(self):
        self.disconnected = True
        self._check()


def reset():
    MQTTClient.instances = []
    MQTTClient.refuse = None
