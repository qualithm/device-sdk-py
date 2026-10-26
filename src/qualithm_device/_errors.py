"""The SDK's exceptions, one base and one subclass per failure."""


class QualithmDeviceError(Exception):
    """Base of every error the SDK raises."""


class ClaimError(QualithmDeviceError):
    """The provisioning endpoint was unreachable or rejected the claim code.

    Attributes:
        status: The HTTP status the endpoint returned, or None when unreachable.
    """

    def __init__(self, message: str, status: "int | None" = None) -> None:
        """Record the message and status."""
        super().__init__(message)
        self.status = status


class EnrollError(QualithmDeviceError):
    """Certificate enrollment failed.

    Attributes:
        status: The HTTP status the endpoint returned, or None when it wasn't reached.
    """

    def __init__(self, message: str, status: "int | None" = None) -> None:
        """Record the message and status."""
        super().__init__(message)
        self.status = status


class RenewError(QualithmDeviceError):
    """Credential renewal failed.

    Attributes:
        status: The HTTP status the endpoint returned, or None when it wasn't reached.
    """

    def __init__(self, message: str, status: "int | None" = None) -> None:
        """Record the message and status."""
        super().__init__(message)
        self.status = status


class CredentialError(QualithmDeviceError):
    """The stored credential is missing, malformed or expired."""


class GatewayError(QualithmDeviceError):
    """The device couldn't open or use its session with the gateway."""


class TopicError(QualithmDeviceError):
    """A topic, capability key or command payload breaks the wire grammar."""


class CapabilityError(QualithmDeviceError):
    """A capability declaration is missing, empty or invalid, or a command targets a sensor."""


class PayloadTooLargeError(QualithmDeviceError):
    """A publish exceeds the payload ceiling the gateway advertised.

    Attributes:
        max_bytes: The ceiling, in bytes.
        actual_bytes: The refused payload's size, in bytes.
    """

    def __init__(self, max_bytes: int, actual_bytes: int) -> None:
        """Record both sizes."""
        super().__init__(
            f"payload of {actual_bytes} bytes exceeds the gateway's {max_bytes}-byte limit"
        )
        self.max_bytes = max_bytes
        self.actual_bytes = actual_bytes
