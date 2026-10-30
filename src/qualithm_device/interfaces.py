"""The three interfaces a board's adapters implement.

They exist for type checkers only: a board has no `typing`, so an adapter imports them under
`if TYPE_CHECKING:` and implements the methods by name.
"""

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Protocol

    class Session(Protocol):
        """An open MQTT session with the gateway."""

        #: The largest packet the gateway accepts, from CONNACK, or None when it didn't say.
        max_packet_size: "int | None"

        def publish(self, topic: str, payload: bytes) -> None:
            """Publish a payload to a device-relative topic."""
            ...

        def subscribe(self, topic_filter: str) -> None:
            """Subscribe to a device-relative topic filter."""
            ...

        def poll(self) -> "list[tuple[str, bytes]]":
            """Service the connection; return the messages received since the last poll."""
            ...

        def close(self) -> None:
            """Disconnect."""
            ...

    class Transport(Protocol):
        """The board's HTTPS client and MQTT client."""

        def request(
            self, method: str, url: str, headers: "dict[str, str]", body: "bytes | None"
        ) -> "tuple[int, bytes]":
            """Make an HTTPS request; return the status and body. Raise OSError when unreachable."""
            ...

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
            """Open an MQTT-over-TLS session. Raise OSError when the gateway refuses it."""
            ...

    class Crypto(Protocol):
        """Key generation and signing, for the certificate credential."""

        def generate_csr(self, device_id: str) -> "tuple[str, str]":
            """Make an ECDSA P-256 key and CSR for the device; return (key PEM, CSR PEM)."""
            ...

        def sign(self, private_key_pem: str, message: bytes) -> bytes:
            """Sign SHA-256 of the message with the key; return the raw `r || s` signature."""
            ...

    class Store(Protocol):
        """Where the credential persists across restarts, as a JSON-ready dict."""

        def load(self) -> "dict[str, object] | None":
            """The stored credential, or None when there is none yet."""
            ...

        def save(self, credential: "dict[str, object]") -> None:
            """Persist the credential, durably."""
            ...

        def clear(self) -> None:
            """Remove the stored credential."""
            ...
