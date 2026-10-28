"""Adapters for a virtual device: a project running on MicroPython's Unix port on a host.

A virtual device claims, connects and reports as a board does, and counts as a device. It
declares itself virtual at claim. `start()` reads the setup file `qualithm deploy --virtual`
writes in the working directory and connects, declaring `system_info`'s sensors:

    from qualithm_device import virtual

    device = virtual.start()
    device.publish_reading(virtual.system_info())

The host's network stands in for Wi-Fi and its clock is already set, so there is no join step.
`chip_temperature_c` is a stand-in: a slow swing around 25 °C, so a chart has something to show.
The credential is a file in the working directory, readable by anyone who can read that
directory.
"""

import gc
import json
import os
import sys
import time

from qualithm_device._device import DEFAULT_KEEPALIVE_S
from qualithm_device._device import connect as _connect
from qualithm_device._errors import QualithmDeviceError
from qualithm_device._micropython import FlashStore, Transport

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from qualithm_device._device import Device

__all__ = [
    "DEFAULT_SETUP_PATH",
    "DEFAULT_STORE_PATH",
    "SYSTEM_SENSORS",
    "FlashStore",
    "SetupError",
    "Transport",
    "connect",
    "read_setup",
    "start",
    "system_info",
]

#: Where the credential persists, relative to the working directory.
DEFAULT_STORE_PATH = "qualithm-credential.json"
#: Where `qualithm deploy --virtual` writes the provisioning URL and claim code.
DEFAULT_SETUP_PATH = "qualithm-setup.json"
_STAND_IN_C = 25.0
_STAND_IN_SWING_C = 1.5
_STAND_IN_PERIOD_S = 600

#: The sensors `system_info` reports, which `virtual.connect` declares on every virtual device.
SYSTEM_SENSORS: "tuple[dict[str, object], ...]" = (
    {"key": "uptime_s", "type": "sensor", "metric": "uptime_s", "unit": "seconds"},
    {"key": "free_memory_bytes", "type": "sensor", "metric": "free_memory_bytes", "unit": "bytes"},
    {
        "key": "chip_temperature_c",
        "type": "sensor",
        "metric": "chip_temperature_c",
        "unit": "celsius",
    },
    # Unitless: major * 10000 + minor * 100 + patch, so a dashboard shows which build is running.
    {"key": "firmware_version", "type": "sensor", "metric": "firmware_version"},
)


class SetupError(QualithmDeviceError):
    """The setup file `qualithm deploy --virtual` writes is missing or malformed."""


# [accumulated ms, ticks at the last read]: ticks_ms wraps, the uptime doesn't. A host's tick
# counter doesn't start at zero as a board's does, so the first read sets the start.
_NOT_STARTED = -1
_uptime = [0, _NOT_STARTED]


def _start_uptime() -> None:
    _uptime[0] = 0
    _uptime[1] = time.ticks_ms()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType] -- MicroPython's time


def system_info() -> "dict[str, float]":
    """The virtual device's vital signs, as a reading for `Device.publish_reading`.

    Returns:
        `uptime_s` since the device connected (or since the first call, without a connect),
        `free_memory_bytes`, a stand-in `chip_temperature_c`, and `firmware_version` as
        major * 10000 + minor * 100 + patch.
    """
    if _uptime[1] == _NOT_STARTED:
        _start_uptime()
    ticks = time.ticks_ms()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType] -- MicroPython's time
    elapsed_ms = int(time.ticks_diff(ticks, _uptime[1]))  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType] -- MicroPython's time
    _uptime[0] += elapsed_ms
    _uptime[1] = ticks
    uptime_s = _uptime[0] // 1000
    # A triangle wave from -1 to 1 over the period: no math module needed.
    phase = (uptime_s % _STAND_IN_PERIOD_S) / _STAND_IN_PERIOD_S
    swing = 4 * abs(phase - 0.5) - 1
    gc.collect()
    major, minor, patch = sys.implementation.version[:3]
    return {
        "uptime_s": uptime_s,
        "free_memory_bytes": gc.mem_free(),  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType] -- MicroPython's gc
        "chip_temperature_c": round(_STAND_IN_C + _STAND_IN_SWING_C * swing, 1),
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
    """Connect a virtual device, declaring `SYSTEM_SENSORS` and any capabilities of its own.

    Args:
        provisioning_url: The provisioning API for the claim code's environment, e.g.
            `https://api.qualithm.com`.
        capabilities: The device's own capability declarations, declared after `SYSTEM_SENSORS`.
        claim_code: The single-use claim code, read only when nothing is stored.
        name: A human-friendly device name set at claim.
        transport: The HTTPS and MQTT clients; a `Transport()` by default.
        store: Where the credential persists; a `FlashStore(DEFAULT_STORE_PATH)` by default.
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
    _start_uptime()
    return _connect(
        provisioning_url,
        Transport() if transport is None else transport,
        FlashStore(DEFAULT_STORE_PATH) if store is None else store,
        capabilities=list(SYSTEM_SENSORS) + list(capabilities),
        claim_code=claim_code,
        name=name,
        broker_host=broker_host,
        broker_port=broker_port,
        keepalive_s=keepalive_s,
        now=time.time,
        virtual=True,
    )


def read_setup(path: str = DEFAULT_SETUP_PATH) -> "dict[str, object]":
    """The setup file `qualithm deploy --virtual` writes.

    Args:
        path: Where it lives.

    Returns:
        `provisioningUrl` and, before the first claim, `claimCode`.

    Raises:
        SetupError: The file is missing, isn't JSON, or lacks a field.
    """
    try:
        with open(path) as file:
            text = file.read()
    except OSError:
        raise SetupError(
            f"no setup file at {path}: run `qualithm deploy --virtual` to write one, or call"
            " connect() with the details"
        ) from None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise SetupError(f"the setup file at {path} isn't a JSON object")
    url = data.get("provisioningUrl")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    if not isinstance(url, str) or not url.startswith("https://"):
        raise SetupError(f"the setup file at {path} has no https provisioningUrl")
    code = data.get("claimCode")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    if code is not None and (not isinstance(code, str) or not code):
        raise SetupError(f"the setup file at {path} has a claimCode that isn't a string")
    return data  # pyright: ignore[reportUnknownVariableType] -- JSON


def _forget_claim_code(path: str, setup: "dict[str, object]") -> None:
    # The code is single-use; drop it once spent, replacing the file atomically.
    kept = {key: value for key, value in setup.items() if key != "claimCode"}
    with open(path + ".tmp", "w") as file:
        file.write(json.dumps(kept))
    os.rename(path + ".tmp", path)


def start(  # noqa: PLR0913 -- connect()'s options, by keyword
    *,
    capabilities: "Sequence[Mapping[str, object]]" = (),
    name: "str | None" = None,
    setup_path: str = DEFAULT_SETUP_PATH,
    transport: "Transport | None" = None,
    store: "FlashStore | None" = None,
    broker_host: "str | None" = None,
    broker_port: "int | None" = None,
    keepalive_s: int = DEFAULT_KEEPALIVE_S,
) -> "Device":
    """Connect with the setup file `qualithm deploy --virtual` wrote.

    The first start claims the device with the file's claim code, then removes the code from the
    file; later starts reconnect with the stored credential.

    Args:
        capabilities: The device's own capability declarations, declared after `SYSTEM_SENSORS`.
        name: A human-friendly device name set at claim.
        setup_path: Where the setup file lives.
        transport: The HTTPS and MQTT clients; a `Transport()` by default.
        store: Where the credential persists; a `FlashStore(DEFAULT_STORE_PATH)` by default.
        broker_host: The gateway to use instead of the one the platform assigned.
        broker_port: The gateway port, overriding either source's.
        keepalive_s: The MQTT keep-alive interval.

    Returns:
        The connected device.

    Raises:
        SetupError: The setup file is missing or malformed.
        CredentialError: Nothing is stored and the setup file has no claim code.
        ClaimError: The claim failed.
        GatewayError: The gateway refused the session.
    """
    setup = read_setup(setup_path)
    code = setup.get("claimCode")
    device = connect(
        str(setup["provisioningUrl"]),
        capabilities=capabilities,
        claim_code=code if isinstance(code, str) else None,
        name=name,
        transport=transport,
        store=store,
        broker_host=broker_host,
        broker_port=broker_port,
        keepalive_s=keepalive_s,
    )
    if code is not None:
        _forget_claim_code(setup_path, setup)
    return device
