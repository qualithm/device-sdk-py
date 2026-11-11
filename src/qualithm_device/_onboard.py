"""What every on-board adapter shares: the setup file, the system sensors and the clock.

`pico` and `esp32` re-export the public names, so each board module reads as a whole.
"""

import json
import os

import ntptime

from qualithm_device._errors import QualithmDeviceError

#: Where `qualithm deploy` writes the provisioning URL, Wi-Fi details and claim code.
DEFAULT_SETUP_PATH = "/qualithm-setup.json"
WIFI_TIMEOUT_S = 30
_CLOCK_ATTEMPTS = 3

#: The sensors `system_info` reports, which each board's `connect` declares.
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


def set_clock() -> None:
    """Set the clock from NTP, which TLS needs to check certificates.

    Raises:
        WifiError: NTP didn't answer after a few attempts.
    """
    for attempt in range(_CLOCK_ATTEMPTS):
        try:
            ntptime.settime()
        except OSError as err:
            if attempt == _CLOCK_ATTEMPTS - 1:
                raise WifiError(f"couldn't set the clock from NTP: {err}") from err
        else:
            return


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


def forget_claim_code(path: str, setup: "dict[str, object]") -> None:
    """Drop the spent, single-use claim code from the setup file, replacing it atomically.

    Args:
        path: Where the setup file lives.
        setup: Its contents, as `read_setup` returned them.
    """
    kept = {key: value for key, value in setup.items() if key != "claimCode"}
    with open(path + ".tmp", "w") as file:
        file.write(json.dumps(kept))
    os.rename(path + ".tmp", path)
