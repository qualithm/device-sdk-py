"""Example: drive connect() with the three adapters written in memory."""

import json

# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class PrintingSession:
    """An MQTT session that prints what it would send."""

    max_packet_size: "int | None" = None

    def publish(self, topic: str, payload: bytes) -> None:
        """Print the publish."""
        print("publish", topic, payload.decode())

    def subscribe(self, topic_filter: str) -> None:
        """Print the subscription."""
        print("subscribe", topic_filter)

    def poll(self) -> "list[tuple[str, bytes]]":
        """Hand out one command, as the gateway would."""
        return [("command/power", b'{"value": true}')]

    def close(self) -> None:
        """Print the disconnect."""
        print("close")


class ScriptedTransport:
    """An HTTPS client that answers the claim, and an MQTT client that prints."""

    def request(
        self, method: str, url: str, headers: "dict[str, str]", body: "bytes | None"
    ) -> "tuple[int, bytes]":
        """Answer the claim as the platform does."""
        print(method, url)
        data = {
            "deviceId": "11111111-1111-1111-1111-111111111111",
            "teamId": "team-1",
            "spaceId": "space-1",
            "secret": "qmd_example",
            "broker": {"host": "gw.example.com", "port": 8883},
        }
        return 200, json.dumps({"data": data}).encode()

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
    ) -> PrintingSession:
        """Open a printing session."""
        print("connect", host, port, "as", client_id)
        return PrintingSession()


class MemoryStore:
    """A credential store held in memory."""

    def __init__(self) -> None:
        """Start empty, as on first boot."""
        self.credential: dict[str, object] | None = None

    def load(self) -> "dict[str, object] | None":
        """The stored credential."""
        return self.credential

    def save(self, credential: "dict[str, object]") -> None:
        """Keep the credential."""
        self.credential = credential

    def clear(self) -> None:
        """Forget the credential."""
        self.credential = None


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Claim, publish a reading, handle a command and disconnect."""
    from qualithm_device import QualithmDeviceError, connect  # noqa: PLC0415 -- after the adapters

    try:
        device = connect(
            "https://api.example.com",
            transport=ScriptedTransport(),
            store=MemoryStore(),
            capabilities=[
                {
                    "key": "temperature",
                    "type": "sensor",
                    "metric": "temperature",
                    "unit": "celsius",
                },
                {"key": "power", "type": "onoff"},
            ],
            claim_code="qmc_example",
        )
        device.publish_reading({"temperature": 21.4})
        device.on_command("power", lambda value: print("power ->", value))
        device.poll()
        device.close()
    except QualithmDeviceError as err:
        print(err)


if __name__ == "__main__":
    main()
