"""The adapters for wherever the code runs: a board's own, or a virtual device's on a host.

One project runs on a Pico 2 W, an ESP32 and as a virtual device with no change:

    from qualithm_device import board

    device = board.start()
    device.publish_reading(board.system_info())
"""

import sys

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from qualithm_device._device import Device

# MicroPython's port name on each board; anything else is a host.
_PICO_PLATFORM = "rp2"
_ESP32_PLATFORM = "esp32"
#: The platform the adapters are chosen for; a test sets it to choose the other.
platform = [sys.platform]


def on_board() -> bool:
    """Whether the code runs on a board rather than as a virtual device.

    Returns:
        True on a Pico or an ESP32, False on a host.
    """
    return platform[0] in (_PICO_PLATFORM, _ESP32_PLATFORM)


def start(
    *,
    capabilities: "Sequence[Mapping[str, object]]" = (),
    name: "str | None" = None,
) -> "Device":
    """Connect with the setup file `qualithm deploy` wrote, through `pico`, `esp32` or `virtual`.

    Args:
        capabilities: The device's own capability declarations, declared after the system
            sensors.
        name: A human-friendly device name set at claim.

    Returns:
        The connected device.

    Raises:
        SetupError: The setup file is missing or malformed (the adapter module's own).
        WifiError: On a board, it couldn't join the network or set its clock.
        CredentialError: Nothing is stored and the setup file has no claim code.
        ClaimError: The claim failed.
        GatewayError: The gateway refused the session.
    """
    if platform[0] == _PICO_PLATFORM:
        from qualithm_device import pico  # noqa: PLC0415 -- a host has no board modules

        return pico.start(capabilities=capabilities, name=name)
    if platform[0] == _ESP32_PLATFORM:
        from qualithm_device import esp32  # noqa: PLC0415 -- a host has no board modules

        return esp32.start(capabilities=capabilities, name=name)
    from qualithm_device import virtual  # noqa: PLC0415 -- chosen at run time, as pico is

    return virtual.start(capabilities=capabilities, name=name)


def system_info() -> "dict[str, float]":
    """The device's vital signs, as a reading for `Device.publish_reading`.

    Returns:
        The reading `pico.system_info`, `esp32.system_info` or `virtual.system_info` makes.
    """
    if platform[0] == _PICO_PLATFORM:
        from qualithm_device import pico  # noqa: PLC0415 -- a host has no board modules

        return pico.system_info()
    if platform[0] == _ESP32_PLATFORM:
        from qualithm_device import esp32  # noqa: PLC0415 -- a host has no board modules

        return esp32.system_info()
    from qualithm_device import virtual  # noqa: PLC0415 -- chosen at run time, as pico is

    return virtual.system_info()
