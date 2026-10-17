"""`connect()` and the `Device` it returns: claim once, renew on time, keep one gateway session."""

import time

from qualithm_device._credential import CERT, Broker, Credential
from qualithm_device._errors import (
    CredentialError,
    GatewayError,
    PayloadTooLargeError,
    RenewError,
    TopicError,
)
from qualithm_device._provision import claim, renew
from qualithm_device._subject import (
    COMMAND_FILTER,
    TELEMETRY_TOPIC,
    check_publish_topic,
    check_topic_filter,
    command_key,
    decode_command,
    is_valid_capability_key,
    telemetry_payload,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable

    from qualithm_device.interfaces import Crypto, Session, Store, Transport

    CommandHandler = Callable[[object], None]

DEFAULT_PORT = 8883
DEFAULT_KEEPALIVE_S = 60
#: How far through its lifetime a credential renews: early enough to retry, late enough to be rare.
RENEW_AT_FRACTION = 2 / 3


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
    ) -> None:
        """Take over an open session; use `connect()` rather than calling this."""
        self.credential = credential
        self.renew_error: RenewError | None = None
        self._session: Session | None = session
        self._transport = transport
        self._crypto = crypto
        self._store = store
        self._provisioning_url = provisioning_url
        self._now = now
        self._handlers: dict[str, CommandHandler] = {}
        self._commands_subscribed = False

    def _open(self) -> "Session":
        if self._session is None:
            raise GatewayError("device is closed")
        return self._session

    def publish(self, topic: str, payload: "bytes | str") -> None:
        """Publish to a device-relative topic.

        Args:
            topic: The topic, e.g. `telemetry`.
            payload: The message body; a str is sent as UTF-8.

        Raises:
            TopicError: The topic breaks the grammar.
            PayloadTooLargeError: The payload exceeds the ceiling the gateway advertised.
            GatewayError: The device is closed, or the publish failed.
        """
        check_publish_topic(topic)
        body = payload.encode() if isinstance(payload, str) else payload
        session = self._open()
        limit = session.max_packet_size
        if limit is not None and len(body) > limit:
            raise PayloadTooLargeError(limit, len(body))
        try:
            session.publish(topic, body)
        except OSError as err:
            raise GatewayError(f"publish to {topic} failed: {err}") from err

    def publish_reading(self, metrics: "dict[str, float]", ts_ms: "int | None" = None) -> None:
        """Publish a reading the platform stores: each metric's name and number, at one time.

        Args:
            metrics: The readings, e.g. `{"temperature": 21.4}`.
            ts_ms: When they were taken, in Unix milliseconds; now by default.

        Raises:
            TopicError: There are no metrics, or a value isn't a number.
            PayloadTooLargeError: The reading exceeds the gateway's ceiling.
            GatewayError: The device is closed, or the publish failed.
        """
        when = int(self._now() * 1000) if ts_ms is None else ts_ms
        self.publish(TELEMETRY_TOPIC, telemetry_payload(metrics, when))

    def subscribe(self, topic_filter: str) -> None:
        """Subscribe to a device-relative topic filter.

        Args:
            topic_filter: The filter; `+` fills one level and `#` ends one.

        Raises:
            TopicError: The filter breaks the grammar.
            GatewayError: The device is closed, or the subscribe failed.
        """
        check_topic_filter(topic_filter)
        try:
            self._open().subscribe(topic_filter)
        except OSError as err:
            raise GatewayError(f"subscribe to {topic_filter} failed: {err}") from err

    def on_command(self, key: str, handler: "CommandHandler") -> None:
        """Call `handler` for each command addressed to a capability.

        The handler gets the command's value, or None for a trigger. The first registration
        subscribes `command/#`.

        Args:
            key: The capability key.
            handler: Called with the value from `poll()`.

        Raises:
            TopicError: The key is invalid.
            GatewayError: The device is closed, or the subscribe failed.
        """
        if not is_valid_capability_key(key):
            raise TopicError(f"invalid capability key: {key}")
        self._handlers[key] = handler
        if not self._commands_subscribed:
            self.subscribe(COMMAND_FILTER)
            self._commands_subscribed = True

    def poll(self) -> "list[tuple[str, bytes]]":
        """Service the session, run command handlers, and return every other message.

        A command with no handler, or whose payload doesn't decode, comes back with the rest.

        Returns:
            The (topic, payload) pairs received since the last poll that no handler took.

        Raises:
            GatewayError: The device is closed, or the session dropped.
        """
        try:
            messages = self._open().poll()
        except OSError as err:
            raise GatewayError(f"session dropped: {err}") from err
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
            handler(None if command.trigger else command.value)
        return rest

    def renew_if_due(self) -> bool:
        """Renew the credential once it is due, and store the new one.

        The open session keeps its credential; the next `connect()` uses the new one. A failure
        is kept in `renew_error` while the current credential is still valid, and retried later.

        Returns:
            True when a renewed credential was stored.
        """
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

    def close(self) -> None:
        """Disconnect. The device can't be used afterwards; `connect()` again to reconnect."""
        session = self._session
        self._session = None
        if session is not None:
            session.close()


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
    crypto: "Crypto | None" = None,
    claim_code: "str | None" = None,
    name: "str | None" = None,
    broker_host: "str | None" = None,
    broker_port: "int | None" = None,
    keepalive_s: int = DEFAULT_KEEPALIVE_S,
    now: "Callable[[], float] | None" = None,
) -> Device:
    """Claim once, renew when due, and open the gateway session.

    On first boot the claim code is exchanged and the credential stored; every later boot loads
    it and skips claiming, since a claim code works once.

    Args:
        provisioning_url: The provisioning API for the claim code's environment, e.g.
            `https://api.qualithm.com`.
        transport: The board's HTTPS and MQTT clients.
        store: Where the credential persists.
        crypto: Key generation and signing; needed only for a certificate credential.
        claim_code: The single-use claim code, read only when nothing is stored.
        name: A human-friendly device name set at claim.
        broker_host: The gateway to use instead of the one the platform assigned.
        broker_port: The gateway port, overriding either source's.
        keepalive_s: The MQTT keep-alive interval.
        now: The clock, in Unix seconds; `time.time` by default.

    Returns:
        The connected device.

    Raises:
        CredentialError: Nothing is stored and there's no claim code, or the stored credential
            is malformed or expired (claim the device again).
        ClaimError: The claim failed.
        GatewayError: No broker is known, or the gateway refused the session.
    """
    clock = time.time if now is None else now
    stored = store.load()
    if stored is not None:
        credential = Credential.from_dict(stored)
    elif claim_code:
        credential = claim(transport, provisioning_url, claim_code, clock(), name=name)
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
    is_cert = credential.kind == CERT
    try:
        session = transport.open(
            broker.host,
            broker.port,
            credential.device_id,
            None if is_cert else credential.device_id,
            None if is_cert else credential.token,
            credential.private_key_pem if is_cert else None,
            credential.certificate_pem if is_cert else None,
            keepalive_s,
        )
    except OSError as err:
        raise GatewayError(
            f"failed to connect to the gateway at {broker.host}:{broker.port}: {err}"
        ) from err
    device = Device(session, credential, transport, crypto, store, provisioning_url, clock)
    device.renew_error = renew_error
    return device
