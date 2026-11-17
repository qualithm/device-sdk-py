"""Claim, certificate enrollment and renewal against the platform's provisioning endpoints."""

import binascii
import json

from qualithm_device._credential import CERT, TOKEN, Credential, parse_api_url, parse_broker
from qualithm_device._errors import ClaimError, CredentialError, EnrollError, RenewError
from qualithm_device._time import format_iso

TYPE_CHECKING = False
if TYPE_CHECKING:
    from qualithm_device.interfaces import Crypto, Transport

    ErrorType = type[ClaimError] | type[EnrollError] | type[RenewError]

CLAIM_PATH = "/v1/provision/claim"
CERT_PATH = "/v1/provision/credentials/cert"
RENEW_PATH = "/v1/provision/credentials/renew"
#: The prefix of the bytes a device signs to renew a certificate.
RENEW_PROOF_TAG = "qualithm-renew-v1"
_OK_MIN = 200
_OK_MAX = 299


def _url(provisioning_url: str, path: str) -> str:
    return provisioning_url.rstrip("/") + path


def _basic(credential: Credential) -> str:
    pair = f"{credential.device_id}:{credential.token}".encode()
    return "Basic " + binascii.b2a_base64(pair).decode().strip()


def _post(  # noqa: PLR0913, PLR0917 -- the request's parts, and how to report a failure
    transport: "Transport",
    url: str,
    headers: "dict[str, str]",
    body: "dict[str, object] | None",
    error: "ErrorType",
    what: str,
) -> "tuple[int, dict[str, object]]":
    """POST JSON and return the status and the envelope's `data`, or raise `error` on failure."""
    # Built up rather than unpacked: MicroPython has no `**` in a dict display.
    sent = {"content-type": "application/json"}
    for key, value in headers.items():
        sent[key] = value
    payload = None if body is None else json.dumps(body).encode()
    try:
        status, raw = transport.request("POST", url, sent, payload)
    except OSError as err:
        raise error(f"failed to reach the provisioning endpoint: {err}") from err
    try:
        envelope = json.loads(raw.decode()) if raw else None
    except ValueError:
        envelope = None
    data = envelope.get("data") if isinstance(envelope, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    if not _OK_MIN <= status <= _OK_MAX or not isinstance(data, dict):
        message = envelope.get("message") if isinstance(envelope, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
        text = message if isinstance(message, str) else f"{what} failed with status {status}"
        raise error(text, status)
    return status, data  # pyright: ignore[reportUnknownVariableType] -- JSON


def _field(data: "dict[str, object]", key: str, error: "ErrorType", status: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        # With the answer's status, so a status of None still means the endpoint wasn't reached.
        raise error(f"provisioning response has no {key}", status)
    return value


def claim(  # noqa: PLR0913 -- the request's fields, plus the injected transport and clock
    transport: "Transport",
    provisioning_url: str,
    code: str,
    now: float,
    *,
    name: "str | None" = None,
    virtual: bool = False,
) -> Credential:
    """Exchange a single-use claim code for the device's credential.

    Args:
        transport: The board's HTTPS client.
        provisioning_url: The provisioning API's base URL, e.g. `https://api.qualithm.com`.
        code: The claim code, as issued (it starts with `qmc_`).
        now: The current Unix time, recorded as the credential's issue time.
        name: A human-friendly device name to set at claim.
        virtual: Declare the device virtual: it runs on a host, not a board. It counts as a
            device all the same.

    Returns:
        The credential, ready to store.

    Raises:
        ClaimError: The endpoint was unreachable or rejected the code.
    """
    body: dict[str, object] = {"code": code}
    if name is not None:
        body["name"] = name
    if virtual:
        body["virtual"] = True
    url = _url(provisioning_url, CLAIM_PATH)
    status, data = _post(transport, url, {}, body, ClaimError, "claim")
    issued = data.get("credential")
    kind = TOKEN
    expires_at = None
    if isinstance(issued, dict):
        issued_kind = issued.get("kind")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
        issued_expiry = issued.get("expiresAt")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
        kind = issued_kind if isinstance(issued_kind, str) else TOKEN
        expires_at = issued_expiry if isinstance(issued_expiry, str) else None
    return Credential(
        device_id=_field(data, "deviceId", ClaimError, status),
        team_id=_field(data, "teamId", ClaimError, status),
        scope_id=_field(data, "scopeId", ClaimError, status),
        kind=kind,
        token=_field(data, "secret", ClaimError, status),
        issued_at=format_iso(now),
        expires_at=expires_at,
        broker=parse_broker(data.get("broker")),
        api_url=parse_api_url(data.get("apiUrl")),
    )


def enroll_certificate(  # noqa: PLR0913 -- the request, plus the injected transport, crypto and clock
    transport: "Transport",
    crypto: "Crypto",
    provisioning_url: str,
    credential: Credential,
    now: float,
    *,
    label: "str | None" = None,
    expires_in_days: "int | None" = None,
) -> Credential:
    """Upgrade a token credential to an mTLS certificate credential.

    The device makes a key and CSR, and authenticates with its token as HTTP Basic auth.

    Args:
        transport: The board's HTTPS client.
        crypto: Makes the key and CSR.
        provisioning_url: The API's base URL: the provisioning URL, or the zone API
            `resolve_api_url` chooses for the credential. The provisioning URL forwards to the
            device's zone, so try it when the zone API gives no HTTP response or a server error
            (5xx).
        credential: The device's token credential.
        now: The current Unix time.
        label: A label for the issued certificate.
        expires_in_days: The certificate's lifetime, bounded by the platform's maximum.

    Returns:
        The certificate credential, ready to store.

    Raises:
        EnrollError: The credential has no token, or the endpoint was unreachable or refused.
    """
    if credential.kind != TOKEN or not credential.token:
        raise EnrollError("a token credential is required to enroll for a certificate")
    private_key_pem, csr_pem = crypto.generate_csr(credential.device_id)
    body: dict[str, object] = {"csrPem": csr_pem}
    if label is not None:
        body["label"] = label
    if expires_in_days is not None:
        body["expiresInDays"] = expires_in_days
    headers = {"authorization": _basic(credential)}
    url = _url(provisioning_url, CERT_PATH)
    status, data = _post(transport, url, headers, body, EnrollError, "enrollment")
    return Credential(
        device_id=credential.device_id,
        team_id=credential.team_id,
        scope_id=credential.scope_id,
        kind=CERT,
        private_key_pem=private_key_pem,
        certificate_pem=_field(data, "certificatePem", EnrollError, status),
        ca_certificate_pem=_field(data, "caCertificatePem", EnrollError, status),
        issued_at=format_iso(now),
        # The platform names the broker and API again at enrollment; keep the claimed ones if it
        # doesn't.
        broker=parse_broker(data.get("broker")) or credential.broker,
        api_url=parse_api_url(data.get("apiUrl")) or credential.api_url,
    )


def renew_proof(signed_at: str, csr_pem: str) -> bytes:
    """The bytes a certificate credential signs to renew: the tag, the time and the CSR.

    Args:
        signed_at: The ISO-8601 time sent with the request.
        csr_pem: The CSR, exactly as sent.

    Returns:
        The message to sign.
    """
    return f"{RENEW_PROOF_TAG}\n{signed_at}\n{csr_pem}".encode()


def renew(
    transport: "Transport",
    crypto: "Crypto",
    provisioning_url: str,
    credential: Credential,
    now: float,
) -> Credential:
    """Swap the credential for a fresh one of the same kind, before it expires.

    A token renews with its own Basic auth. A certificate renews with a CSR for a new key, signed
    by the current key, and needs no other secret. The presented credential stays valid until its
    own expiry, so a lost response can be retried.

    Args:
        transport: The board's HTTPS client.
        crypto: Makes the new key and signs the proof; used for a certificate only.
        provisioning_url: The API's base URL: the provisioning URL, or the zone API
            `resolve_api_url` chooses for the credential. The provisioning URL forwards to the
            device's zone, so try it when the zone API gives no HTTP response or a server error
            (5xx).
        credential: The current credential.
        now: The current Unix time.

    Returns:
        The renewed credential, ready to store.

    Raises:
        RenewError: The endpoint was unreachable or refused (an expired or revoked credential
            can't renew: claim the device again).
        CredentialError: The credential's kind has no renewal.
    """
    url = _url(provisioning_url, RENEW_PATH)
    if credential.kind == TOKEN:
        status, data = _post(
            transport, url, {"authorization": _basic(credential)}, None, RenewError, "renewal"
        )
        return Credential(
            device_id=credential.device_id,
            team_id=credential.team_id,
            scope_id=credential.scope_id,
            kind=TOKEN,
            token=_field(data, "secret", RenewError, status),
            issued_at=format_iso(now),
            expires_at=_renewed_expiry(data),
            # The gateway and API the device belongs on now, which may have moved since the last.
            broker=parse_broker(data.get("broker")) or credential.broker,
            api_url=parse_api_url(data.get("apiUrl")) or credential.api_url,
        )
    if credential.kind != CERT or not credential.private_key_pem or not credential.certificate_pem:
        raise CredentialError(f"a {credential.kind} credential has no renewal")
    private_key_pem, csr_pem = crypto.generate_csr(credential.device_id)
    signed_at = format_iso(now)
    signature = crypto.sign(credential.private_key_pem, renew_proof(signed_at, csr_pem))
    body: dict[str, object] = {
        "certificatePem": credential.certificate_pem,
        "csrPem": csr_pem,
        "signedAt": signed_at,
        "signature": binascii.b2a_base64(signature).decode().strip(),
    }
    status, data = _post(transport, url, {}, body, RenewError, "renewal")
    return Credential(
        device_id=credential.device_id,
        team_id=credential.team_id,
        scope_id=credential.scope_id,
        kind=CERT,
        private_key_pem=private_key_pem,
        certificate_pem=_field(data, "certificatePem", RenewError, status),
        ca_certificate_pem=_field(data, "caCertificatePem", RenewError, status),
        issued_at=signed_at,
        expires_at=_renewed_expiry(data),
        broker=parse_broker(data.get("broker")) or credential.broker,
        api_url=parse_api_url(data.get("apiUrl")) or credential.api_url,
    )


def _renewed_expiry(data: "dict[str, object]") -> "str | None":
    issued = data.get("credential")
    expiry = issued.get("expiresAt") if isinstance(issued, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    return expiry if isinstance(expiry, str) else None
