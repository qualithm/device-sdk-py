"""HTTPS, MQTT and the credential file over MicroPython's sockets and TLS.

Shared by the adapters that run MicroPython: a Pico on its flash, and a virtual device on the
host's Unix port.
"""

import json
import os
import socket
import time

import tls
from umqtt.simple import MQTTClient, MQTTException

from qualithm_device._errors import CredentialError
from qualithm_device._roots import ROOTS

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable

    from tls import SSLSocket as Stream

DEFAULT_STORE_PATH = "/qualithm-credential.json"
DEFAULT_TIMEOUT_S = 10
_HTTPS_PORT = 443


def _context(cadata: str, key_pem: "str | None", cert_pem: "str | None") -> "tls.SSLContext":
    context = tls.SSLContext(tls.PROTOCOL_TLS_CLIENT)
    context.verify_mode = tls.CERT_REQUIRED
    context.load_verify_locations(cadata)
    if key_pem is not None and cert_pem is not None:
        context.load_cert_chain(cert_pem, key_pem)
    return context


def _split_url(url: str) -> "tuple[str, int, str]":
    """The host, port and path of an https URL."""
    if not url.startswith("https://"):
        raise OSError(f"only https URLs are supported: {url}")
    rest = url[len("https://") :]
    slash = rest.find("/")
    authority, path = (rest, "/") if slash < 0 else (rest[:slash], rest[slash:])
    host, _, port = authority.partition(":")
    try:
        return host, int(port) if port else _HTTPS_PORT, path
    except ValueError:
        raise OSError(f"invalid port in {url}") from None


def _read_exact(stream: "Stream", size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise OSError("the connection closed mid-response")
        data += chunk
    return data


def _read_chunked(stream: "Stream") -> bytes:
    body = b""
    while True:
        size = int(stream.readline().split(b";")[0].strip(), 16)
        if size == 0:
            while stream.readline() not in (b"", b"\r\n"):
                pass
            return body
        body += _read_exact(stream, size)
        stream.readline()


def _read_to_end(stream: "Stream") -> bytes:
    body = b""
    while True:
        chunk = stream.read(512)
        if not chunk:
            return body
        body += chunk


def _read_response(stream: "Stream") -> "tuple[int, bytes]":
    status_line = stream.readline().split(None, 2)
    if len(status_line) < 2 or not status_line[0].startswith(b"HTTP/"):  # noqa: PLR2004 -- version and code
        raise OSError("the server's response has no status line")
    status = int(status_line[1])
    length = None
    chunked = False
    while True:
        line = stream.readline()
        if line in (b"", b"\r\n"):
            break
        name, _, value = line.partition(b":")
        name = name.strip().lower()
        if name == b"content-length":
            length = int(value.strip())
        elif name == b"transfer-encoding" and b"chunked" in value.lower():
            chunked = True
    if chunked:
        return status, _read_chunked(stream)
    if length is not None:
        return status, _read_exact(stream, length)
    return status, _read_to_end(stream)


class Session:
    """An MQTT 3.1.1 session over `umqtt.simple`. Made by `Transport.open`.

    Attributes:
        max_packet_size: Always None: an MQTT 3.1.1 CONNACK doesn't carry the gateway's ceiling.
    """

    def __init__(self, client: MQTTClient, keepalive_s: int, now: "Callable[[], float]") -> None:
        """Take over a connected client; use `Transport.open` rather than calling this."""
        self.max_packet_size: int | None = None
        self._client = client
        self._keepalive_s = keepalive_s
        self._now = now
        self._last_sent = now()
        self._inbound: list[tuple[str, bytes]] = []
        client.set_callback(self._deliver)

    def _deliver(self, topic: bytes, payload: bytes) -> None:
        self._inbound.append((topic.decode(), bytes(payload)))

    def publish(self, topic: str, payload: bytes) -> None:
        """Publish at QoS 0.

        Args:
            topic: The device-relative topic.
            payload: The message body.

        Raises:
            OSError: The connection failed.
        """
        _mqtt(self._client.publish, topic, payload)
        self._last_sent = self._now()

    def subscribe(self, topic_filter: str) -> None:
        """Subscribe at QoS 0 and wait for the gateway's acknowledgement.

        Args:
            topic_filter: The device-relative filter.

        Raises:
            OSError: The connection failed, or the gateway refused the subscription.
        """
        _mqtt(self._client.subscribe, topic_filter)
        self._last_sent = self._now()

    def poll(self) -> "list[tuple[str, bytes]]":
        """Ping when the keep-alive is half spent, then read every message waiting.

        Returns:
            The (topic, payload) pairs received since the last poll.

        Raises:
            OSError: The connection dropped.
        """
        if self._keepalive_s and self._now() - self._last_sent >= self._keepalive_s / 2:
            _mqtt(self._client.ping)
            self._last_sent = self._now()
        while _mqtt(self._client.check_msg) is not None:
            pass
        messages = self._inbound
        self._inbound = []
        return messages

    def close(self) -> None:
        """Disconnect; a connection that already dropped is closed all the same."""
        try:  # noqa: SIM105 -- MicroPython has no contextlib
            self._client.disconnect()
        except OSError:
            pass


def _mqtt(call: "Callable[..., object]", *args: object) -> object:
    """Run a umqtt call, raising its protocol failures as the OSError the core expects."""
    try:
        return call(*args)
    except MQTTException as err:
        raise OSError(f"the gateway refused the request (code {err.args[0]})") from err
    except ValueError as err:
        # MicroPython raises a certificate that doesn't verify as a ValueError.
        raise OSError(f"couldn't verify the gateway: {err}") from err
    except (AssertionError, IndexError) as err:
        raise OSError("the gateway sent a malformed packet") from err


class Transport:
    """HTTPS and MQTT over the board's TLS, verifying every certificate."""

    def __init__(
        self,
        *,
        cadata: str = ROOTS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        now: "Callable[[], float] | None" = None,
    ) -> None:
        """Set what the connections trust and how long they wait.

        Args:
            cadata: The PEM roots a server's chain must end at; the platform's by default.
            timeout_s: How long a connection waits on the network.
            now: The clock, in Unix seconds, for MQTT keep-alive; `time.time` by default.
        """
        self._cadata = cadata
        self._timeout_s = timeout_s
        self._now = time.time if now is None else now

    def request(
        self, method: str, url: str, headers: "dict[str, str]", body: "bytes | None"
    ) -> "tuple[int, bytes]":
        """Make one HTTPS request on its own connection.

        Args:
            method: The HTTP method.
            url: An https URL.
            headers: The request headers.
            body: The request body, or None.

        Returns:
            The status and body.

        Raises:
            OSError: The URL isn't https, the server is unreachable or its certificate doesn't
                verify, or the response is malformed.
        """
        host, port, path = _split_url(url)
        family, kind, proto, _, address = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0]
        raw = socket.socket(family, kind, proto)
        try:
            raw.settimeout(self._timeout_s)
            raw.connect(address)
            stream = _context(self._cadata, None, None).wrap_socket(raw, server_hostname=host)
        except OSError:
            raw.close()
            raise
        except ValueError as err:
            # MicroPython raises a certificate that doesn't verify as a ValueError.
            raw.close()
            raise OSError(f"couldn't verify {host}: {err}") from err
        try:
            head = "%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n" % (method, path, host)
            for name, value in headers.items():
                head += "%s: %s\r\n" % (name, value)
            if body is not None:
                head += "Content-Length: %d\r\n" % len(body)
            stream.write((head + "\r\n").encode())
            if body:
                stream.write(body)
            return _read_response(stream)
        except ValueError as err:
            raise OSError(f"malformed response from {host}: {err}") from err
        finally:
            stream.close()

    def open(  # noqa: PLR0913, PLR0917 -- one argument per CONNECT field
        self,
        host: str,
        port: int,
        client_id: str,
        username: "str | None",
        password: "str | None",
        key_pem: "str | None",
        cert_pem: "str | None",
        keepalive_s: int,
    ) -> Session:
        """Open an MQTT 3.1.1 session over verified TLS.

        Args:
            host: The gateway host.
            port: The gateway port.
            client_id: The MQTT client id.
            username: The token credential's username, or None.
            password: The token credential's password, or None.
            key_pem: A certificate credential's private key, or None.
            cert_pem: A certificate credential's certificate, or None.
            keepalive_s: The keep-alive interval.

        Returns:
            The open session.

        Raises:
            OSError: The gateway is unreachable, its certificate doesn't verify, or it refused
                the connection.
        """
        client = MQTTClient(
            client_id,
            host,
            port=port,
            user=username,
            password=password,
            keepalive=keepalive_s,
            ssl=_context(self._cadata, key_pem, cert_pem),
        )
        _mqtt(client.connect, True, self._timeout_s)
        return Session(client, keepalive_s, self._now)


class FlashStore:
    """The credential as a JSON file on the board's flash, replaced atomically.

    A save writes a temporary file and renames it over the old one, which LittleFS (the Pico's
    filesystem) does atomically: power lost mid-save leaves the previous credential in place.
    """

    def __init__(self, path: str = DEFAULT_STORE_PATH) -> None:
        """Choose the file.

        Args:
            path: Where the credential lives.
        """
        self._path = path
        self._temp = path + ".tmp"

    def load(self) -> "dict[str, object] | None":
        """The stored credential.

        Returns:
            The credential, or None when nothing is stored yet.

        Raises:
            CredentialError: The file isn't a JSON object.
        """
        try:
            with open(self._path) as file:
                text = file.read()
        except OSError:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise CredentialError(f"the stored credential at {self._path} is unreadable")
        return data  # pyright: ignore[reportUnknownVariableType] -- JSON

    def save(self, credential: "dict[str, object]") -> None:
        """Replace the stored credential.

        Args:
            credential: The credential, as `Credential.to_dict()` makes it.
        """
        with open(self._temp, "w") as file:
            file.write(json.dumps(credential))
        os.rename(self._temp, self._path)

    def clear(self) -> None:
        """Remove the stored credential, and any half-written one."""
        for path in (self._path, self._temp):
            try:  # noqa: SIM105 -- MicroPython has no contextlib
                os.remove(path)
            except OSError:
                pass
