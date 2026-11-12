"""Adapters for an ESP32-S3, ESP32-C3 or ESP32-C6 on MicroPython's `esp32` port.

The same calls as `pico`: `start()` reads the setup file `qualithm deploy` writes, joins Wi-Fi and
connects, declaring `system_info`'s sensors:

    from qualithm_device import esp32

    device = esp32.start()
    device.publish_reading(esp32.system_info())

`join_wifi()` and `connect()` do the same steps with the details passed in.

The reference boards' LEDs differ, so the adapters leave them alone unless a `light` is passed:
any object with the `show(state)` of `pico.StatusLight`, such as a `pico.StatusLight` on a
single-colour LED's pin.

Every TLS connection requires a certificate that chains to the bundled roots and names the host.
The board connects with a token credential and `connect()` takes no `crypto`.
"""

import gc
import sys
import time

import esp32
import network

from qualithm_device._device import DEFAULT_KEEPALIVE_S
from qualithm_device._device import connect as _connect
from qualithm_device._errors import QualithmDeviceError
from qualithm_device._micropython import (
    DEFAULT_STORE_PATH,
    DEFAULT_TIMEOUT_S,
    FlashStore,
    Session,
    Transport,
)
from qualithm_device._onboard import (
    DEFAULT_SETUP_PATH,
    SYSTEM_SENSORS,
    WIFI_TIMEOUT_S,
    SetupError,
    WifiError,
    forget_claim_code,
    read_setup,
    set_clock,
)
from qualithm_device._time import unix_clock

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from qualithm_device._device import Device
    from qualithm_device.interfaces import Light

__all__ = [
    "DEFAULT_SETUP_PATH",
    "DEFAULT_STORE_PATH",
    "DEFAULT_TIMEOUT_S",
    "SYSTEM_SENSORS",
    "WIFI_TIMEOUT_S",
    "FlashStore",
    "Session",
    "SetupError",
    "Transport",
    "WifiError",
    "connect",
    "join_wifi",
    "read_setup",
    "start",
    "system_info",
]

_POLL_S = 0.2


# The station interface the last `join_wifi` connected, for `system_info`.
_station: "list[network.WLAN]" = []


def join_wifi(ssid: str, password: str, *, timeout_s: int = WIFI_TIMEOUT_S) -> "network.WLAN":
    """Join a Wi-Fi network and set the clock from NTP, which TLS needs to check certificates.

    The port joins WPA2 and WPA3 Personal networks with no security argument.

    Args:
        ssid: The network name. An ESP32-S3 or C3 joins 2.4 GHz networks only; a C6 also joins
            Wi-Fi 6 on 2.4 GHz.
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
        # The port reports a failed join as the driver's disconnect reason in place of a status.
        waiting = (network.STAT_IDLE, network.STAT_CONNECTING, network.STAT_GOT_IP)
        deadline = time.time() + timeout_s
        while not wlan.isconnected():
            status = wlan.status()
            if status not in waiting:
                raise WifiError(f"couldn't join {ssid}: {_wifi_reason(status)}")
            if time.time() >= deadline:
                raise WifiError(f"couldn't join {ssid} within {timeout_s} s")
            time.sleep(_POLL_S)
    set_clock()
    _station[:] = [wlan]
    return wlan


def _wifi_reason(status: int) -> str:
    if status == network.STAT_WRONG_PASSWORD:
        return "wrong password"
    if status == getattr(network, "STAT_NO_AP_FOUND_W_COMPATIBLE_SECURITY", None):
        return (
            "the network's security mode isn't WPA2 or WPA3 Personal"
            " (enterprise networks aren't supported)"
        )
    if status == network.STAT_NO_AP_FOUND:
        return "no network with that name answered (the board joins 2.4 GHz networks only)"
    return (
        f"the connection failed (reason {status}); check the password and that the network"
        " uses WPA2 or WPA3 Personal"
    )


# [accumulated ms, ticks at the last read]: ticks_ms wraps every 12 days, the uptime doesn't.
_uptime = [0, 0]


def system_info(wlan: "network.WLAN | None" = None) -> "dict[str, float]":
    """The board's vital signs, as a reading for `Device.publish_reading`.

    Call it at least every few days: the uptime counts across the tick counter's wrap only then.

    Args:
        wlan: The station interface; the one `join_wifi` or `start` last joined by default.

    Returns:
        `uptime_s`, `free_memory_bytes`, `chip_temperature_c` (whole degrees, as the chip's
        sensor reads), `wifi_rssi_dbm` while the station is connected, and `firmware_version`
        as major * 10000 + minor * 100 + patch (12900 for 1.29.0).
    """
    if wlan is None:
        wlan = _station[0] if _station else network.WLAN(network.STA_IF)
    ticks = time.ticks_ms()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType] -- MicroPython's time
    _uptime[0] += time.ticks_diff(ticks, _uptime[1])  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType] -- MicroPython's time
    _uptime[1] = ticks
    gc.collect()
    major, minor, patch = sys.implementation.version[:3]
    info: "dict[str, float]" = {
        "uptime_s": _uptime[0] // 1000,
        "free_memory_bytes": gc.mem_free(),  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType] -- MicroPython's gc
        "chip_temperature_c": esp32.mcu_temperature(),
        "firmware_version": major * 10000 + minor * 100 + patch,
    }
    # The port raises for the signal strength while the station is down, as after a dropped link.
    if wlan.isconnected():
        info["wifi_rssi_dbm"] = wlan.status("rssi")
    return info


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
    light: "Light | None" = None,
) -> "Device":
    """Connect the board, declaring `SYSTEM_SENSORS` and any capabilities of its own.

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
        light: A status light to show the connect's progress on, or None to leave the LEDs alone.

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
    store = FlashStore() if store is None else store
    if light is not None:
        light.show("slow-pulse")
    try:
        claiming = claim_code is not None and store.load() is None
        device = _connect(
            provisioning_url,
            Transport() if transport is None else transport,
            store,
            capabilities=list(SYSTEM_SENSORS) + list(capabilities),
            claim_code=claim_code,
            name=name,
            broker_host=broker_host,
            broker_port=broker_port,
            keepalive_s=keepalive_s,
            now=unix_clock(time),
        )
    except (QualithmDeviceError, OSError):
        if light is not None:
            light.show("steady-red")
        raise
    if light is not None:
        light.show("q-once" if claiming else "steady")
    return device


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
    light: "Light | None" = None,
) -> "Device":
    """Join Wi-Fi and connect with the setup file `qualithm deploy` wrote.

    The first start claims the board with the file's claim code, then removes the code from the
    file; later starts reconnect with the stored credential.

    Args:
        capabilities: The board's own capability declarations, declared after `SYSTEM_SENSORS`.
        name: A human-friendly device name set at claim.
        setup_path: Where the setup file lives.
        transport: The HTTPS and MQTT clients; a `Transport()` by default.
        store: Where the credential persists; a `FlashStore()` by default.
        broker_host: The gateway to use instead of the one the platform assigned.
        broker_port: The gateway port, overriding either source's.
        keepalive_s: The MQTT keep-alive interval.
        light: A status light to show progress on, or None to leave the LEDs alone.

    Returns:
        The connected device. `system_info()` reads the Wi-Fi interface it joined.

    Raises:
        SetupError: The setup file is missing or malformed.
        WifiError: The board couldn't join the network or set its clock.
        CredentialError: Nothing is stored and the setup file has no claim code.
        ClaimError: The claim failed.
        GatewayError: The gateway refused the session.
    """
    if light is not None:
        light.show("slow-pulse")
    try:
        setup = read_setup(setup_path)
        wifi = setup["wifi"]
        join_wifi(wifi["ssid"], wifi["passphrase"])  # pyright: ignore[reportIndexIssue, reportUnknownArgumentType] -- checked by read_setup
    except (QualithmDeviceError, OSError):
        if light is not None:
            light.show("steady-red")
        raise
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
        light=light,
    )
    if code is not None:
        forget_claim_code(setup_path, setup)
    return device
