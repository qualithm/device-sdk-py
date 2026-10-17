"""The device credential and the broker it names, in device-sdk-js's wire shape."""

from qualithm_device._errors import CredentialError
from qualithm_device._time import parse_iso

#: The ports a TCP endpoint can name.
MAX_PORT = 65_535

TOKEN = "token"  # noqa: S105 -- a credential kind, not a secret
CERT = "cert"


class Broker:
    """A gateway the device connects to over MQTT/TLS.

    Args:
        host: The gateway host, e.g. `gw.sg-sin-a.qualithm.com`.
        port: Its TLS port.
    """

    def __init__(self, host: str, port: int) -> None:
        """Hold the endpoint."""
        self.host = host
        self.port = port

    def __eq__(self, other: object) -> bool:
        """Equal when host and port match."""
        return isinstance(other, Broker) and (self.host, self.port) == (other.host, other.port)

    def __hash__(self) -> int:
        """Hash by host and port."""
        return hash((self.host, self.port))

    def to_dict(self) -> "dict[str, object]":
        """The endpoint as JSON-ready data."""
        return {"host": self.host, "port": self.port}


def parse_broker(value: object) -> "Broker | None":
    """Read the broker a provisioning response carries.

    An older platform omits it and a malformed one isn't trusted; either leaves the device on its
    configured broker.

    Args:
        value: The response's `broker` field.

    Returns:
        The broker, or None when absent or malformed.
    """
    if not isinstance(value, dict):
        return None
    host = value.get("host")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    port = value.get("port")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    if not isinstance(host, str) or not host:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= MAX_PORT:
        return None
    return Broker(host, port)


def _string(data: "dict[str, object]", key: str, required: bool = True) -> "str | None":
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise CredentialError(f"stored credential has no {key}")
    return value


class Credential:
    """A device credential, persisted so a restart reconnects without claiming again.

    The token kind carries `token`; the cert kind carries `private_key_pem`, `certificate_pem` and
    `ca_certificate_pem`. Times are ISO-8601 strings, as the platform and device-sdk-js write them.
    """

    def __init__(  # noqa: PLR0913 -- one argument per wire field
        self,
        device_id: str,
        team_id: str,
        space_id: str,
        kind: str,
        issued_at: str,
        *,
        token: "str | None" = None,
        private_key_pem: "str | None" = None,
        certificate_pem: "str | None" = None,
        ca_certificate_pem: "str | None" = None,
        expires_at: "str | None" = None,
        broker: "Broker | None" = None,
    ) -> None:
        """Hold the credential's fields."""
        self.device_id = device_id
        self.team_id = team_id
        self.space_id = space_id
        self.kind = kind
        self.issued_at = issued_at
        self.token = token
        self.private_key_pem = private_key_pem
        self.certificate_pem = certificate_pem
        self.ca_certificate_pem = ca_certificate_pem
        self.expires_at = expires_at
        self.broker = broker

    def to_dict(self) -> "dict[str, object]":
        """The credential in device-sdk-js's stored shape, unset fields left out."""
        data: dict[str, object] = {
            "deviceId": self.device_id,
            "teamId": self.team_id,
            "spaceId": self.space_id,
            "kind": self.kind,
            "issuedAt": self.issued_at,
        }
        optional = (
            ("token", self.token),
            ("privateKeyPem", self.private_key_pem),
            ("certificatePem", self.certificate_pem),
            ("caCertificatePem", self.ca_certificate_pem),
            ("expiresAt", self.expires_at),
        )
        for key, value in optional:
            if value is not None:
                data[key] = value
        if self.broker is not None:
            data["broker"] = self.broker.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: "dict[str, object]") -> "Credential":
        """Read a stored credential.

        Args:
            data: The credential as the store returned it.

        Returns:
            The credential.

        Raises:
            CredentialError: A field is missing, or a kind's secret material is.
        """
        kind = _string(data, "kind") or TOKEN
        credential = cls(
            device_id=_string(data, "deviceId") or "",
            team_id=_string(data, "teamId") or "",
            space_id=_string(data, "spaceId") or "",
            kind=kind,
            issued_at=_string(data, "issuedAt") or "",
            token=_string(data, "token", required=False),
            private_key_pem=_string(data, "privateKeyPem", required=False),
            certificate_pem=_string(data, "certificatePem", required=False),
            ca_certificate_pem=_string(data, "caCertificatePem", required=False),
            expires_at=_string(data, "expiresAt", required=False),
            broker=parse_broker(data.get("broker")),
        )
        credential.check()
        return credential

    def check(self) -> None:
        """Fail unless the credential carries its kind's secret material.

        Raises:
            CredentialError: The kind is unknown, or its material is missing.
        """
        if self.kind == TOKEN:
            if not self.token:
                raise CredentialError("token credential is missing its secret")
        elif self.kind == CERT:
            if not self.private_key_pem or not self.certificate_pem:
                raise CredentialError("certificate credential is missing its key or certificate")
        else:
            raise CredentialError(f"unknown credential kind: {self.kind}")

    def expires_at_s(self) -> "int | None":
        """When the credential expires, in Unix seconds, or None when it never does."""
        return None if self.expires_at is None else parse_iso(self.expires_at)
