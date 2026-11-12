"""`connect()` and the `Device` it returns: claim once, renew on time, keep one gateway session."""

import json
import time

from qualithm_device._backoff import reconnect_delay
from qualithm_device._capability import build_manifest, fits_declaration
from qualithm_device._credential import CERT, Broker, Credential
from qualithm_device._errors import (
    CapabilityError,
    CredentialError,
    GatewayError,
    GatewayUnavailableError,
    PayloadTooLargeError,
    RenewError,
    TooManyMetricsError,
    TopicError,
)
from qualithm_device._provision import claim, renew
from qualithm_device._subject import (
    COMMAND_FILTER,
    MANIFEST_TOPIC,
    TELEMETRY_TOPIC,
    check_publish_topic,
    check_topic_filter,
    command_key,
    decode_command,
    is_valid_capability_key,
    telemetry_payload,
)

TYPE_CHECKING = False

MAX_METRICS = 64
"""The most metrics one reading may carry: one event holds up to this many."""
if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from qualithm_device.interfaces import Crypto, Session, Store, Transport

    CommandHandler = Callable[[object], None]

DEFAULT_PORT = 8883
DEFAULT_KEEPALIVE_S = 60
#: How far through its lifetime a credential renews: early enough to retry, late enough to be rare.
RENEW_AT_FRACTION = 2 / 3
#: How old a credential must be before an unavailable gateway renews it early to look for a moved
#: one, so a gateway that is only down doesn't issue a credential on every connect.
MOVED_RENEW_AFTER_S = 3600


def resolve_broker(host: "str | None", port: "int | None", credential: Credential) -> Broker:
    """Choose the gateway: a configured host wins, otherwise the one the platform assigned.

    Args:
        host: The configured gateway host, or None to use the credential's.
        port: The configured port; overrides either source's.
        credential: The stored credential, carrying the broker from claim or enrollment.

    Returns:
        The gateway to connect to.

    Raises:
        GatewayError: Neither names a host.
    """
    if host:
        return Broker(host, port or DEFAULT_PORT)
    if credential.broker is not None:
        return Broker(credential.broker.host, port or credential.broker.port)
    raise GatewayError(
        "no broker host is configured and the stored credential has none from enrollment"
    )


def renew_due(credential: Credential, now: float) -> bool:
    """Whether a credential has lived long enough to renew.

    Args:
        credential: The credential.
        now: The current Unix time.

    Returns:
        True once `RENEW_AT_FRACTION` of its lifetime has passed; never for one that doesn't expire.
    """
    expires = credential.expires_at_s()
    if expires is None:
        return False
    from qualithm_device._time import parse_iso  # noqa: PLC0415 -- only for an expiring credential

    issued = parse_iso(credential.issued_at)
    return now >= issued + (expires - issued) * RENEW_AT_FRACTION


class Device:
    """A device with an open gateway session. Made by `connect()`.

    The session stays open on its own: a renewed credential reopens it, and a dropped one
    reconnects with `reconnect_delay` backoff, publishing the capabilities and subscribing again.

    Attributes:
        credential: The credential the session authenticated with.
        renew_error: The last renewal failure, kept while the old credential is still valid.
    """

    def __init__(  # noqa: PLR0913, PLR0917 -- what connect() resolved, handed over whole
        self,
        session: "Session",
        credential: Credential,
        transport: "Transport",
        crypto: "Crypto | None",
        store: "Store",
        provisioning_url: str,
        now: "Callable[[], float]",
        manifest: "dict[str, object]",
        *,
        broker: "Broker | None" = None,
        broker_host: "str | None" = None,
        broker_port: "int | None" = None,
        keepalive_s: int = DEFAULT_KEEPALIVE_S,
        sleep_ms: "Callable[[int], None] | None" = None,
    ) -> None:
        """Take over an open session; use `connect()` rather than calling this."""
        self.credential = credential
        self.renew_error: RenewError | None = None
        self._session: Session | None = session
        self._closed = False
        self._transport = transport
        self._crypto = crypto
        self._store = store
        self._provisioning_url = provisioning_url
        self._now = now
        self._broker = broker
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._keepalive_s = keepalive_s
        self._sleep_ms = _sleep_ms if sleep_ms is None else sleep_ms
        self._handlers: dict[str, CommandHandler] = {}
        self._commands_subscribed = False
        self._filters: list[str] = []
        self._manifest = manifest
        self._declared = _declarations(manifest)

    def _live(self) -> "Session":
        """The open session, reconnecting first when it dropped."""
        if self._closed:
            raise GatewayError("device is closed")
        session = self._session
        return self._reconnect() if session is None else session

    def _drop(self) -> None:
        """Forget a session that failed, closing what is left of it."""
        session = self._session
        self._session = None
        if session is not None:
            try:  # noqa: SIM105 -- MicroPython has no contextlib
                session.close()
            except OSError:
                pass

    def _reconnect(self) -> "Session":
        """Open a new session, retrying with backoff until the gateway accepts one.

        Raises:
            CredentialError: The credential expired: claim the device again.
            GatewayError: No broker is known.
            PayloadTooLargeError: The manifest exceeds the new session's ceiling.
        """
        attempt = 0
        followed = False
        while True:
            expires = self.credential.expires_at_s()
            if expires is not None and self._now() >= expires:
                raise CredentialError(
                    f"the credential expired at {self.credential.expires_at}: claim the device"
                    " again"
                )
            if self._renew():
                self._broker = resolve_broker(self._broker_host, self._broker_port, self.credential)
            broker = self._broker
            if broker is None:
                broker = resolve_broker(self._broker_host, self._broker_port, self.credential)
                self._broker = broker
            try:
                return self._resume(broker)
            except GatewayUnavailableError:
                moved = _renew_for_moved_gateway(
                    self.credential,
                    self._transport,
                    self._crypto,
                    self._provisioning_url,
                    self._now(),
                    broker,
                )
                if moved is not None:
                    self._store.save(moved.to_dict())
                    self.credential = moved
                    self.renew_error = None
                    self._broker = moved.broker
                    if not followed:
                        # Straight to the gateway renewal named, once.
                        followed = True
                        continue
            except OSError:
                pass
            attempt += 1
            self._sleep_ms(reconnect_delay(attempt))

    def _resume(self, broker: Broker) -> "Session":
        """Open a session at `broker`, then publish the capabilities and subscribe again."""
        session = _open_session(self._transport, broker, self.credential, self._keepalive_s)
        self._session = session
        try:
            _publish_manifest(session, self._manifest)
            for topic_filter in self._filters:
                session.subscribe(topic_filter)
        except (OSError, PayloadTooLargeError):
            self._drop()
            raise
        return session

    def publish(self, topic: str, payload: "bytes | str") -> None:
        """Publish to a device-relative topic, reconnecting first when the session dropped.

        Args:
            topic: The topic, e.g. `telemetry`.
            payload: The message body; a str is sent as UTF-8.

        Raises:
            TopicError: The topic breaks the grammar.
            PayloadTooLargeError: The payload exceeds the ceiling the gateway advertised.
            CredentialError: The session dropped and the credential has expired.
            GatewayError: The device is closed, or the publish failed on a fresh session too.
        """
        check_publish_topic(topic)
        body = payload.encode() if isinstance(payload, str) else payload
        for retry in (False, True):
            session = self._live()
            limit = session.max_packet_size
            if limit is not None and len(body) > limit:
                raise PayloadTooLargeError(limit, len(body))
            try:
                session.publish(topic, body)
            except OSError as err:
                self._drop()
                if retry:
                    raise GatewayError(f"publish to {topic} failed: {err}") from err
            else:
                return

    def publish_reading(self, metrics: "dict[str, float]", ts_ms: "int | None" = None) -> None:
        """Publish a reading the platform stores: each metric's name and number, at one time.

        Args:
            metrics: The readings, e.g. `{"temperature": 21.4}`.
            ts_ms: When they were taken, in Unix milliseconds; now by default.

        Raises:
            TopicError: There are no metrics, or a value isn't a number.
            TooManyMetricsError: The reading carries more than 64 metrics.
            PayloadTooLargeError: The reading exceeds the gateway's ceiling.
            CredentialError: The session dropped and the credential has expired.
            GatewayError: The device is closed, or the publish failed on a fresh session too.
        """
        if len(metrics) > MAX_METRICS:
            raise TooManyMetricsError(MAX_METRICS, len(metrics))
        when = int(self._now() * 1000) if ts_ms is None else ts_ms
        self.publish(TELEMETRY_TOPIC, telemetry_payload(metrics, when))

    def subscribe(self, topic_filter: str) -> None:
        """Subscribe to a device-relative topic filter, kept across reconnects.

        Args:
            topic_filter: The filter; `+` fills one level and `#` ends one.

        Raises:
            TopicError: The filter breaks the grammar.
            CredentialError: The session dropped and the credential has expired.
            GatewayError: The device is closed, or the subscribe failed on a fresh session too.
        """
        check_topic_filter(topic_filter)
        if topic_filter in self._filters:
            return
        for retry in (False, True):
            session = self._live()
            try:
                session.subscribe(topic_filter)
            except OSError as err:
                self._drop()
                if retry:
                    raise GatewayError(f"subscribe to {topic_filter} failed: {err}") from err
            else:
                self._filters.append(topic_filter)
                return

    def on_command(self, key: str, handler: "CommandHandler") -> None:
        """Call `handler` for each command addressed to a capability.

        The handler gets the command's value, or None for a trigger. The first registration
        subscribes `command/#`.

        Args:
            key: The capability key.
            handler: Called with the value from `poll()`.

        Raises:
            CapabilityError: The key is invalid, or is a declared sensor, which is read-only.
            GatewayError: The device is closed, or the subscribe failed.
        """
        if not is_valid_capability_key(key):
            raise CapabilityError(f"invalid capability key: {key}")
        if _declared(self._declared, key, "sensor"):
            raise CapabilityError(f"capability {key} is a sensor and takes no commands")
        self._handlers[key] = handler
        if not self._commands_subscribed:
            self.subscribe(COMMAND_FILTER)
            self._commands_subscribed = True

    def declare_capabilities(self, capabilities: "Sequence[Mapping[str, object]]") -> None:
        """Replace the device's capabilities and publish the new manifest now.

        A reconnect publishes the new set again; a later `connect()` publishes the capabilities
        passed to it, so pass the same set there.

        Args:
            capabilities: At least one capability declaration, each with a unique key.

        Raises:
            CapabilityError: The list is empty or a declaration is invalid.
            PayloadTooLargeError: The manifest exceeds the gateway's ceiling.
            GatewayError: The device is closed, or the publish failed.
        """
        manifest = build_manifest(capabilities)
        self.publish(MANIFEST_TOPIC, json.dumps(manifest))
        self._manifest = manifest
        self._declared = _declarations(manifest)

    def poll(self) -> "list[tuple[str, bytes]]":
        """Service the session, run command handlers, and return every other message.

        A dropped session reconnects first, waiting `reconnect_delay` between refused attempts.
        A command with no handler, whose payload doesn't decode, or whose value doesn't fit the
        declared capability comes back with the rest; its handler isn't called.

        Returns:
            The (topic, payload) pairs received since the last poll that no handler took.

        Raises:
            CredentialError: The session dropped and the credential has expired.
            GatewayError: The device is closed, or a fresh session dropped too.
        """
        messages: list[tuple[str, bytes]] = []
        for retry in (False, True):
            session = self._live()
            try:
                messages = session.poll()
            except OSError as err:
                self._drop()
                if retry:
                    raise GatewayError(f"session dropped: {err}") from err
            else:
                break
        rest: list[tuple[str, bytes]] = []
        for topic, payload in messages:
            key = command_key(topic)
            handler = None if key is None else self._handlers.get(key)
            if handler is None:
                rest.append((topic, payload))
                continue
            try:
                command = decode_command(payload)
            except TopicError:
                rest.append((topic, payload))
                continue
            entry = self._declared.get(key)  # pyright: ignore[reportArgumentType] -- a handler's key is a str
            if entry is not None and not fits_declaration(entry, command):
                rest.append((topic, payload))
                continue
            handler(None if command.trigger else command.value)
        return rest

    def _renew(self) -> bool:
        """Renew the credential once it is due and store the new one; keep a failure."""
        renewed = _try_renew(
            self.credential, self._transport, self._crypto, self._provisioning_url, self._now()
        )
        if isinstance(renewed, RenewError):
            self.renew_error = renewed
            return False
        if renewed is None:
            return False
        self._store.save(renewed.to_dict())
        self.credential = renewed
        self.renew_error = None
        return True

    def renew_if_due(self) -> bool:
        """Renew the credential once it is due, store it, and reopen the session on it.

        The session reopens before the old credential expires, so the gateway never drops it
        for an expired one. A failure is kept in `renew_error` while the current credential is
        still valid, and retried later.

        Returns:
            True when a renewed credential was stored.

        Raises:
            GatewayError: No broker is known for the renewed credential.
        """
        if not self._renew():
            return False
        self._broker = resolve_broker(self._broker_host, self._broker_port, self.credential)
        if not self._closed:
            self._drop()
            self._reconnect()
        return True

    def close(self) -> None:
        """Disconnect. The device can't be used afterwards; `connect()` again to reconnect."""
        self._closed = True
        self._drop()


def _publish_manifest(session: "Session", manifest: "dict[str, object]") -> None:
    """Publish the capability manifest on `session`.

    Raises:
        PayloadTooLargeError: It exceeds the session's ceiling.
        OSError: The publish failed.
    """
    body = json.dumps(manifest).encode()
    limit = session.max_packet_size
    if limit is not None and len(body) > limit:
        raise PayloadTooLargeError(limit, len(body))
    session.publish(MANIFEST_TOPIC, body)


def _sleep_ms(ms: int) -> None:
    """Wait `ms` milliseconds."""
    time.sleep(ms / 1000)


def _declarations(manifest: "dict[str, object]") -> "dict[str, dict[str, object]]":
    """A manifest's entries by capability key."""
    entries: list[dict[str, object]] = manifest["capabilities"]  # pyright: ignore[reportAssignmentType] -- build_manifest's shape
    return {entry["key"]: entry for entry in entries}  # pyright: ignore[reportReturnType] -- keys are str


def _declared(declared: "dict[str, dict[str, object]]", key: str, kind: str) -> bool:
    """Whether `key` is declared with type `kind`."""
    entry = declared.get(key)
    return entry is not None and entry["type"] == kind


def _open_session(
    transport: "Transport", broker: Broker, credential: Credential, keepalive_s: int
) -> "Session":
    """Open the gateway session, presenting `credential`."""
    is_cert = credential.kind == CERT
    return transport.open(
        broker.host,
        broker.port,
        credential.device_id,
        None if is_cert else credential.device_id,
        None if is_cert else credential.token,
        credential.private_key_pem if is_cert else None,
        credential.certificate_pem if is_cert else None,
        keepalive_s,
    )


def _gateway_error(broker: Broker, err: OSError) -> GatewayError:
    """The error for a session the gateway at `broker` didn't open."""
    return GatewayError(f"failed to connect to the gateway at {broker.host}:{broker.port}: {err}")


def _renew_for_moved_gateway(  # noqa: PLR0913, PLR0917 -- what connect() holds, handed over whole
    credential: Credential,
    transport: "Transport",
    crypto: "Crypto | None",
    provisioning_url: str,
    now: float,
    tried: Broker,
) -> "Credential | None":
    """Renew early after an unavailable gateway, to learn whether the device's gateway moved.

    Returns:
        The renewed credential when it names another gateway, or None when the credential is too
        new to renew early, renewal failed, or the gateway hasn't moved.
    """
    from qualithm_device._time import parse_iso  # noqa: PLC0415 -- only after a refusal

    if now < parse_iso(credential.issued_at) + MOVED_RENEW_AFTER_S:
        return None
    if credential.kind == CERT and crypto is None:
        return None
    try:
        renewed = renew(transport, crypto, provisioning_url, credential, now)  # pyright: ignore[reportArgumentType] -- crypto is only read for a certificate
    except RenewError:
        return None
    moved = renewed.broker
    if moved is None or (moved.host == tried.host and moved.port == tried.port):
        return None
    return renewed


def _try_renew(
    credential: Credential,
    transport: "Transport",
    crypto: "Crypto | None",
    provisioning_url: str,
    now: float,
) -> "Credential | RenewError | None":
    """The renewed credential, the renewal's error, or None when renewal isn't due or possible."""
    if not renew_due(credential, now):
        return None
    if credential.kind == CERT and crypto is None:
        return RenewError("a certificate credential needs crypto to renew")
    try:
        return renew(transport, crypto, provisioning_url, credential, now)  # pyright: ignore[reportArgumentType] -- crypto is only read for a certificate
    except RenewError as err:
        return err


def connect(  # noqa: PLR0913 -- the hero API takes every option by keyword
    provisioning_url: str,
    transport: "Transport",
    store: "Store",
    *,
    capabilities: "Sequence[Mapping[str, object]] | None" = None,
    crypto: "Crypto | None" = None,
    claim_code: "str | None" = None,
    name: "str | None" = None,
    broker_host: "str | None" = None,
    broker_port: "int | None" = None,
    keepalive_s: int = DEFAULT_KEEPALIVE_S,
    now: "Callable[[], float] | None" = None,
    virtual: bool = False,
) -> Device:
    """Claim once, renew when due, and open the gateway session.

    On first boot the claim code is exchanged and the credential stored; every later boot loads
    it and skips claiming, since a claim code works once. Every connect publishes the capability
    manifest to the reserved `capabilities` topic, so dashboards and automations list what the
    device declares.

    Args:
        provisioning_url: The provisioning API for the claim code's environment, e.g.
            `https://api.qualithm.com`.
        transport: The board's HTTPS and MQTT clients.
        store: Where the credential persists.
        capabilities: What the device can do or report, at least one declaration, e.g.
            `[{"key": "temperature", "type": "sensor", "metric": "temperature",
            "unit": "celsius"}]`. Checked before anything else.
        crypto: Key generation and signing; needed only for a certificate credential.
        claim_code: The single-use claim code, read only when nothing is stored.
        name: A human-friendly device name set at claim.
        broker_host: The gateway to use instead of the one the platform assigned.
        broker_port: The gateway port, overriding either source's.
        keepalive_s: The MQTT keep-alive interval.
        now: The clock, in Unix seconds; `time.time` by default.
        virtual: Declare the device virtual at claim: it runs on a host, not a board.

    Returns:
        The connected device.

    Raises:
        CapabilityError: No capabilities are declared, or a declaration is invalid.
        CredentialError: Nothing is stored and there's no claim code, or the stored credential
            is malformed or expired (claim the device again).
        ClaimError: The claim failed.
        GatewayError: No broker is known, the gateway refused the session, or the manifest
            publish failed.
        PayloadTooLargeError: The manifest exceeds the gateway's ceiling.
    """
    # Before claiming, so a bad declaration never spends the single-use claim code.
    manifest = build_manifest(capabilities)
    clock = time.time if now is None else now
    stored = store.load()
    if stored is not None:
        credential = Credential.from_dict(stored)
    elif claim_code:
        credential = claim(
            transport, provisioning_url, claim_code, clock(), name=name, virtual=virtual
        )
        store.save(credential.to_dict())
    else:
        raise CredentialError("no stored credential and no claim code provided")

    expires = credential.expires_at_s()
    if expires is not None and clock() >= expires:
        raise CredentialError(
            f"the stored credential expired at {credential.expires_at}: claim the device again"
        )
    renewed = _try_renew(credential, transport, crypto, provisioning_url, clock())
    renew_error = renewed if isinstance(renewed, RenewError) else None
    if isinstance(renewed, Credential):
        store.save(renewed.to_dict())
        credential = renewed

    broker = resolve_broker(broker_host, broker_port, credential)
    try:
        session = _open_session(transport, broker, credential, keepalive_s)
    except GatewayUnavailableError as err:
        # How a gateway redirects an MQTT 3.1.1 client: renewal names where the device belongs.
        moved = _renew_for_moved_gateway(
            credential, transport, crypto, provisioning_url, clock(), broker
        )
        if moved is None or moved.broker is None:
            raise _gateway_error(broker, err) from err
        store.save(moved.to_dict())
        credential = moved
        broker = moved.broker
        renew_error = None
        try:
            session = _open_session(transport, broker, credential, keepalive_s)
        except OSError as retry_err:
            raise _gateway_error(broker, retry_err) from retry_err
    except OSError as err:
        raise _gateway_error(broker, err) from err
    device = Device(
        session,
        credential,
        transport,
        crypto,
        store,
        provisioning_url,
        clock,
        manifest,
        broker=broker,
        broker_host=broker_host,
        broker_port=broker_port,
        keepalive_s=keepalive_s,
    )
    device.renew_error = renew_error
    try:
        _publish_manifest(session, manifest)
    except OSError as err:
        device.close()
        raise GatewayError(f"publish to {MANIFEST_TOPIC} failed: {err}") from err
    except PayloadTooLargeError:
        device.close()
        raise
    return device
