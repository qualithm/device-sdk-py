"""Adapters for a Raspberry Pi Pico 2 W or Pico W on MicroPython (Decisions qualithm/pm#2292, 2293).

Join Wi-Fi, then `connect()` over `Transport` and `FlashStore`, declaring `system_info`'s sensors:

    from qualithm_device import pico

    wlan = pico.join_wifi("<your-ssid>", "<your-password>")
    device = pico.connect("https://api.qualithm.com", claim_code="<your-claim-code>")
    device.publish_reading(pico.system_info(wlan))

Every TLS connection requires a certificate that chains to the bundled roots and names the host.
A Pico has no on-board key generation or signing, so it connects with a token credential and
`connect()` takes no `crypto`.
"""

import gc
import json
import os
import socket
import sys
import time

import machine
import network
import ntptime
import tls
from umqtt.simple import MQTTClient, MQTTException

from qualithm_device._device import DEFAULT_KEEPALIVE_S
from qualithm_device._device import connect as _connect
from qualithm_device._errors import CredentialError, QualithmDeviceError
from qualithm_device._roots import ROOTS

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from tls import SSLSocket as Stream

    from qualithm_device._device import Device

DEFAULT_STORE_PATH = "/qualithm-credential.json"
DEFAULT_TIMEOUT_S = 10
WIFI_TIMEOUT_S = 30
_CLOCK_ATTEMPTS = 3
_POLL_S = 0.2
_HTTPS_PORT = 443
_TEMPERATURE_CHANNEL = 4
# RP2040 and RP2350 datasheets: 0.706 V at 27 °C, falling 1.721 mV per degree.
_SENSOR_VOLTS_AT_27C = 0.706
_SENSOR_VOLTS_PER_C = 0.001721
_ADC_VOLTS = 3.3
_ADC_FULL_SCALE = 65535


#: The sensors `system_info` reports, which `pico.connect` declares on every board.
SYSTEM_SENSORS: "tuple[dict[str, object], ...]" = (
    {"key": "uptime_s", "type": "sensor", "metric": "uptime_s", "unit": "seconds"},
    {"key": "free_memory_bytes", "type": "sensor", "metric": "free_memory_bytes", "unit": "bytes"},
    {
        "key": "chip_temperature_c",
        "type": "sensor",
        "metric": "chip_temperature_c",
        "unit": "celsius",
    },
    {"key": "wifi_rssi_dbm", "type": "sensor", "metric": "wifi_rssi_dbm", "unit": "dBm"},
    # Unitless: major * 10000 + minor * 100 + patch, so a dashboard shows which build is running.
    {"key": "firmware_version", "type": "sensor", "metric": "firmware_version"},
)


class WifiError(QualithmDeviceError):
    """The board couldn't join the Wi-Fi network or set its clock."""


def join_wifi(ssid: str, password: str, *, timeout_s: int = WIFI_TIMEOUT_S) -> "network.WLAN":
    """Join a Wi-Fi network and set the clock from NTP, which TLS needs to check certificates.

    Args:
        ssid: The network name. A Pico joins 2.4 GHz networks only.
        password: The network password.
        timeout_s: How long to wait for the network.

    Returns:
        The connected station interface, for `system_info`.

    Raises:
        WifiError: The network refused the board, didn't answer in time, or NTP didn't.
    """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        deadline = time.time() + timeout_s
        while not wlan.isconnected():
            status = wlan.status()
            if status < 0:
                raise WifiError(f"couldn't join {ssid}: {_wifi_reason(status)}")
            if time.time() >= deadline:
                raise WifiError(f"couldn't join {ssid} within {timeout_s} s")
            time.sleep(_POLL_S)
    _set_clock()
    return wlan


def _wifi_reason(status: int) -> str:
    if status == network.STAT_WRONG_PASSWORD:
        return "wrong password"
    if status == network.STAT_NO_AP_FOUND:
        return "no network with that name answered (a Pico joins 2.4 GHz networks only)"
    return f"the connection failed (status {status})"


def _set_clock() -> None:
    for attempt in range(_CLOCK_ATTEMPTS):
        try:
            ntptime.settime()
        except OSError as err:
            if attempt == _CLOCK_ATTEMPTS - 1:
                raise WifiError(f"couldn't set the clock from NTP: {err}") from err
        else:
            return


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


# [accumulated ms, ticks at the last read]: ticks_ms wraps every 12 days, the uptime doesn't.
_uptime = [0, 0]


def system_info(wlan: "network.WLAN") -> "dict[str, float]":
    """The board's vital signs, as a reading for `Device.publish_reading`.

    Call it at least every few days: the uptime counts across the tick counter's wrap only then.

    Args:
        wlan: The station interface `join_wifi` returned.

    Returns:
        `uptime_s`, `free_memory_bytes`, `chip_temperature_c`, `wifi_rssi_dbm`, and
        `firmware_version` as major * 10000 + minor * 100 + patch (12900 for 1.29.0).
    """
    ticks = time.ticks_ms()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType] -- MicroPython's time
    _uptime[0] += time.ticks_diff(ticks, _uptime[1])  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType] -- MicroPython's time
    _uptime[1] = ticks
    gc.collect()
    volts = machine.ADC(_TEMPERATURE_CHANNEL).read_u16() * _ADC_VOLTS / _ADC_FULL_SCALE
    major, minor, patch = sys.implementation.version[:3]
    return {
        "uptime_s": _uptime[0] // 1000,
        "free_memory_bytes": gc.mem_free(),  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType] -- MicroPython's gc
        "chip_temperature_c": round(27 - (volts - _SENSOR_VOLTS_AT_27C) / _SENSOR_VOLTS_PER_C, 1),
        "wifi_rssi_dbm": wlan.status("rssi"),
        "firmware_version": major * 10000 + minor * 100 + patch,
    }


def connect(  # noqa: PLR0913 -- the core connect()'s options, by keyword
    provisioning_url: str,
    *,
    capabilities: "Sequence[Mapping[str, object]]" = (),
    claim_code: "str | None" = None,
    name: "str | None" = None,
    transport: "Transport | None" = None,
    store: "FlashStore | None" = None,
    broker_host: "str | None" = None,
    broker_port: "int | None" = None,
    keepalive_s: int = DEFAULT_KEEPALIVE_S,
) -> "Device":
    """Connect a Pico, declaring `SYSTEM_SENSORS` and any capabilities of its own.

    Args:
        provisioning_url: The provisioning API for the claim code's environment, e.g.
            `https://api.qualithm.com`.
        capabilities: The board's own capability declarations, declared after `SYSTEM_SENSORS`.
        claim_code: The single-use claim code, read only when nothing is stored.
        name: A human-friendly device name set at claim.
        transport: The HTTPS and MQTT clients; a `Transport()` by default.
        store: Where the credential persists; a `FlashStore()` by default.
        broker_host: The gateway to use instead of the one the platform assigned.
        broker_port: The gateway port, overriding either source's.
        keepalive_s: The MQTT keep-alive interval.

    Returns:
        The connected device.

    Raises:
        CapabilityError: A declaration is invalid, or reuses a system sensor's key.
        CredentialError: Nothing is stored and there's no claim code, or the stored credential
            is malformed or expired (claim the device again).
        ClaimError: The claim failed.
        GatewayError: No broker is known, the gateway refused the session, or the manifest
            publish failed.
    """
    return _connect(
        provisioning_url,
        Transport() if transport is None else transport,
        FlashStore() if store is None else store,
        capabilities=list(SYSTEM_SENSORS) + list(capabilities),
        claim_code=claim_code,
        name=name,
        broker_host=broker_host,
        broker_port=broker_port,
        keepalive_s=keepalive_s,
        now=time.time,
    )
