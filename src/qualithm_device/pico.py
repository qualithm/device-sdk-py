"""Adapters for a Raspberry Pi Pico 2 W or Pico W on MicroPython (Decisions qualithm/pm#2292, 2293).

`start()` reads the setup file `qualithm deploy` writes, joins Wi-Fi and connects, declaring
`system_info`'s sensors:

    from qualithm_device import pico

    device = pico.start()
    device.publish_reading(pico.system_info())

`join_wifi()` and `connect()` do the same steps with the details passed in.

The board's LED shows where it is (Decision qualithm/pm#2727): a slow pulse while it joins Wi-Fi
and reaches the gateway, Q once after a claim, steady once connected, and a fast flash when it needs
attention. Pass `light=None` to leave the LED alone, or a `StatusLight` to keep showing states:

    light = pico.StatusLight()
    device = pico.start(light=light)
    light.show("steady-red")

Every TLS connection requires a certificate that chains to the bundled roots and names the host.
A Pico has no on-board key generation or signing, so it connects with a token credential and
`connect()` takes no `crypto`.
"""

import gc
import json
import os
import sys
import time

import machine
import network
import ntptime

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
from qualithm_device.ui import light_at, light_state

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from qualithm_device._device import Device

__all__ = [
    "DEFAULT_SETUP_PATH",
    "DEFAULT_STORE_PATH",
    "DEFAULT_TIMEOUT_S",
    "LIGHT_TICK_MS",
    "SYSTEM_SENSORS",
    "WIFI_TIMEOUT_S",
    "FlashStore",
    "Session",
    "SetupError",
    "StatusLight",
    "Transport",
    "WifiError",
    "connect",
    "join_wifi",
    "read_setup",
    "start",
    "system_info",
]

#: Where `qualithm deploy` writes the provisioning URL, Wi-Fi details and claim code.
DEFAULT_SETUP_PATH = "/qualithm-setup.json"
WIFI_TIMEOUT_S = 30
_CLOCK_ATTEMPTS = 3
_POLL_S = 0.2
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


class SetupError(QualithmDeviceError):
    """The setup file `qualithm deploy` writes is missing or malformed."""


#: The status light's tick, in milliseconds: fine enough for the rhythm's 140 ms unit.
LIGHT_TICK_MS = 20


class StatusLight:
    """The board's status light, playing the device light states from a timer.

    A state comes from `ui.LIGHT_STEPS`: `steady`, `q-once`, `slow-pulse`, `steady-red` or `off`.
    """

    def __init__(
        self,
        pin: "str | machine.Pin" = "LED",
        *,
        mono: bool = True,
        reduced: bool = False,
        timer: "machine.Timer | None" = None,
    ) -> None:
        """Take the LED and start the timer, dark.

        Args:
            pin: The LED's pin name, or the pin; the Pico W's on-board LED by default.
            mono: Whether the LED shows one colour only, as the Pico's does.
            reduced: Whether to show the still form of a state that moves.
            timer: The timer that plays the states; a new software timer by default.
        """
        self._pin = machine.Pin(pin, machine.Pin.OUT) if isinstance(pin, str) else pin
        self._mono = mono
        self._reduced = reduced
        self._state = "off"
        self._began = time.ticks_ms()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType] -- MicroPython's time
        self._lit: "bool | None" = None
        self._timer = machine.Timer(-1) if timer is None else timer
        self._pin.value(0)
        self._timer.init(period=LIGHT_TICK_MS, mode=machine.Timer.PERIODIC, callback=self._tick)

    @property
    def state(self) -> str:
        """The state the light was last asked to show."""
        return self._state

    def show(self, state: str) -> None:
        """Play a state from its start.

        Args:
            state: A key of `ui.LIGHT_STEPS`.

        Raises:
            ValueError: When there is no light state of that name.
        """
        light_state(state)
        self._state = state
        self._began = time.ticks_ms()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType] -- MicroPython's time
        self._tick()

    def close(self) -> None:
        """Stop the timer and turn the LED off."""
        self._timer.deinit()
        self._state = "off"
        self._pin.value(0)
        self._lit = False

    def _tick(self, _timer: object = None) -> None:
        elapsed = time.ticks_diff(time.ticks_ms(), self._began)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType] -- MicroPython's time
        lit, _ = light_at(self._state, max(0, elapsed), self._reduced, self._mono)  # pyright: ignore[reportUnknownArgumentType] -- MicroPython's time
        if lit != self._lit:
            self._pin.value(1 if lit else 0)
            self._lit = lit


def _light(light: "StatusLight | str | None") -> "StatusLight | None":
    return StatusLight(light) if isinstance(light, str) else light


# The station interface the last `join_wifi` connected, for `system_info`.
_station: "list[network.WLAN]" = []


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
        # WPA2/WPA3 mixed joins WPA2-only, mixed and WPA3-only networks; firmware without the
        # constant keeps the driver's default, WPA2.
        security = getattr(network.WLAN, "SEC_WPA2_WPA3", None)
        if security is None:
            wlan.connect(ssid, password)
        else:
            wlan.connect(ssid, password, security=security)  # pyright: ignore[reportCallIssue] -- the stubs lag the cyw43 driver, which takes it
        deadline = time.time() + timeout_s
        while not wlan.isconnected():
            status = wlan.status()
            if status < 0:
                raise WifiError(f"couldn't join {ssid}: {_wifi_reason(status)}")
            if time.time() >= deadline:
                raise WifiError(f"couldn't join {ssid} within {timeout_s} s")
            time.sleep(_POLL_S)
    _set_clock()
    _station[:] = [wlan]
    return wlan


def _wifi_reason(status: int) -> str:
    if status == network.STAT_WRONG_PASSWORD:
        return (
            "wrong password, or the network's security mode isn't WPA2 or WPA3 Personal"
            " (enterprise networks aren't supported)"
        )
    if status == network.STAT_NO_AP_FOUND:
        return "no network with that name answered (a Pico joins 2.4 GHz networks only)"
    return (
        f"the connection failed (status {status}); check the password and that the network"
        " uses WPA2 or WPA3 Personal"
    )


def _set_clock() -> None:
    for attempt in range(_CLOCK_ATTEMPTS):
        try:
            ntptime.settime()
        except OSError as err:
            if attempt == _CLOCK_ATTEMPTS - 1:
                raise WifiError(f"couldn't set the clock from NTP: {err}") from err
        else:
            return


# [accumulated ms, ticks at the last read]: ticks_ms wraps every 12 days, the uptime doesn't.
_uptime = [0, 0]


def system_info(wlan: "network.WLAN | None" = None) -> "dict[str, float]":
    """The board's vital signs, as a reading for `Device.publish_reading`.

    Call it at least every few days: the uptime counts across the tick counter's wrap only then.

    Args:
        wlan: The station interface; the one `join_wifi` or `start` last joined by default.

    Returns:
        `uptime_s`, `free_memory_bytes`, `chip_temperature_c`, `wifi_rssi_dbm`, and
        `firmware_version` as major * 10000 + minor * 100 + patch (12900 for 1.29.0).
    """
    if wlan is None:
        wlan = _station[0] if _station else network.WLAN(network.STA_IF)
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
    light: "StatusLight | str | None" = "LED",
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
        light: The status light: a `StatusLight`, an LED pin name, or None to leave the LED alone.
            It pulses while connecting, plays Q once after a claim, then stays steady, and shows
            attention when the connect fails.

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
    status = _light(light)
    store = FlashStore() if store is None else store
    if status is not None:
        status.show("slow-pulse")
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
            now=time.time,
        )
    except (QualithmDeviceError, OSError):
        if status is not None:
            status.show("steady-red")
        raise
    if status is not None:
        status.show("q-once" if claiming else "steady")
    return device


def read_setup(path: str = DEFAULT_SETUP_PATH) -> "dict[str, object]":
    """The setup file `qualithm deploy` writes.

    Args:
        path: Where it lives.

    Returns:
        `provisioningUrl`, `wifi` (`ssid`, `passphrase`) and, before the first claim, `claimCode`.

    Raises:
        SetupError: The file is missing, isn't JSON, or lacks a field.
    """
    try:
        with open(path) as file:
            text = file.read()
    except OSError:
        raise SetupError(
            f"no setup file at {path}: run `qualithm deploy` to write one, or call"
            " join_wifi() and connect() with the details"
        ) from None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise SetupError(f"the setup file at {path} isn't a JSON object")
    url = data.get("provisioningUrl")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    wifi = data.get("wifi")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    if not isinstance(url, str) or not url.startswith("https://"):
        raise SetupError(f"the setup file at {path} has no https provisioningUrl")
    if (
        not isinstance(wifi, dict)
        or not isinstance(wifi.get("ssid"), str)  # pyright: ignore[reportUnknownMemberType] -- JSON
        or not wifi.get("ssid")  # pyright: ignore[reportUnknownMemberType] -- JSON
        or not isinstance(wifi.get("passphrase"), str)  # pyright: ignore[reportUnknownMemberType] -- JSON
    ):
        raise SetupError(f"the setup file at {path} has no wifi ssid and passphrase")
    code = data.get("claimCode")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- JSON
    if code is not None and (not isinstance(code, str) or not code):
        raise SetupError(f"the setup file at {path} has a claimCode that isn't a string")
    return data  # pyright: ignore[reportUnknownVariableType] -- JSON


def _forget_claim_code(path: str, setup: "dict[str, object]") -> None:
    # The code is single-use; drop it once spent, replacing the file atomically as FlashStore does.
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
    light: "StatusLight | str | None" = "LED",
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
        light: The status light: a `StatusLight`, an LED pin name, or None to leave the LED alone.
            It pulses from the start, plays Q once after a claim, then stays steady, and shows
            attention when any step fails.

    Returns:
        The connected device. `system_info()` reads the Wi-Fi interface it joined.

    Raises:
        SetupError: The setup file is missing or malformed.
        WifiError: The board couldn't join the network or set its clock.
        CredentialError: Nothing is stored and the setup file has no claim code.
        ClaimError: The claim failed.
        GatewayError: The gateway refused the session.
    """
    status = _light(light)
    if status is not None:
        status.show("slow-pulse")
    try:
        setup = read_setup(setup_path)
        wifi = setup["wifi"]
        join_wifi(wifi["ssid"], wifi["passphrase"])  # pyright: ignore[reportIndexIssue, reportUnknownArgumentType] -- checked by read_setup
    except (QualithmDeviceError, OSError):
        if status is not None:
            status.show("steady-red")
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
        light=status,
    )
    if code is not None:
        _forget_claim_code(setup_path, setup)
    return device
