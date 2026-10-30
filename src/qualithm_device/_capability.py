"""Capability declarations and the manifest every connect publishes.

A declaration is a dict, one per capability:

- `{"key": "power", "type": "onoff"}`
- `{"key": "brightness", "type": "range", "min": 0, "max": 100}`, with optional `step` and `unit`
- `{"key": "mode", "type": "enum", "options": ["auto", "manual"]}`
- `{"key": "reboot", "type": "trigger"}`
- `{"key": "temperature", "type": "sensor", "metric": "temperature"}`, with an optional `unit`
"""

from qualithm_device._errors import CapabilityError
from qualithm_device._subject import is_valid_capability_key

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from qualithm_device._subject import Command

#: Each capability type, and the declaration fields it takes besides `key` and `type`.
_FIELDS = {
    "onoff": (),
    "range": ("min", "max", "step", "unit"),
    "enum": ("options",),
    "trigger": (),
    "sensor": ("metric", "unit"),
}


def _is_finite(value: object) -> bool:
    # MicroPython's core has no math module: inf - inf and nan - nan are both nan, never 0.
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value - value == 0


def _optional_unit(
    key: str, declaration: "Mapping[str, object]", config: "dict[str, object]"
) -> None:
    if "unit" in declaration:
        unit = declaration["unit"]
        if not isinstance(unit, str):
            raise CapabilityError(f"capability {key} unit must be a string")
        config["unit"] = unit


def _range_config(key: str, declaration: "Mapping[str, object]") -> "dict[str, object]":
    low = declaration.get("min")
    high = declaration.get("max")
    if not _is_finite(low) or not _is_finite(high):
        raise CapabilityError(f"range capability {key} requires finite min and max")
    if low >= high:  # pyright: ignore[reportOperatorIssue] -- both are numbers, checked above
        raise CapabilityError(f"range capability {key} requires min < max")
    config: dict[str, object] = {"min": low, "max": high}
    if "step" in declaration:
        step = declaration["step"]
        if not _is_finite(step) or step <= 0:  # pyright: ignore[reportOperatorIssue] -- a number, checked first
            raise CapabilityError(f"range capability {key} step must be a positive number")
        config["step"] = step
    _optional_unit(key, declaration, config)
    return config


def _enum_config(key: str, declaration: "Mapping[str, object]") -> "dict[str, object]":
    options = declaration.get("options")
    if (
        not isinstance(options, (list, tuple))
        or not options
        or not all(isinstance(option, str) and option for option in options)  # pyright: ignore[reportUnknownVariableType] -- checked per item
    ):
        raise CapabilityError(
            f"enum capability {key} requires at least one non-empty string option"
        )
    return {"options": list(options)}  # pyright: ignore[reportUnknownArgumentType] -- strings, checked above


def _sensor_config(key: str, declaration: "Mapping[str, object]") -> "dict[str, object]":
    metric = declaration.get("metric")
    if not isinstance(metric, str) or not metric:
        raise CapabilityError(f"sensor capability {key} requires a metric name")
    config: dict[str, object] = {"metric": metric}
    _optional_unit(key, declaration, config)
    return config


def _entry(fields: "Mapping[str, object]") -> "dict[str, object]":
    if not isinstance(fields, dict):  # pyright: ignore[reportUnnecessaryIsInstance] -- callers aren't typed on a board
        raise CapabilityError("a capability declaration must be a dict")
    key = fields.get("key")
    if not isinstance(key, str) or not is_valid_capability_key(key):
        raise CapabilityError(f"invalid capability key: {key}")
    kind = fields.get("type")
    if kind not in _FIELDS:
        raise CapabilityError(f"capability {key} has an unknown type: {kind}")
    allowed = _FIELDS[kind]  # pyright: ignore[reportArgumentType] -- a known type, checked above
    for name in fields:
        if name not in ("key", "type") and name not in allowed:
            raise CapabilityError(f"capability {key} of type {kind} takes no {name}")
    if kind == "range":
        config = _range_config(key, fields)
    elif kind == "enum":
        config = _enum_config(key, fields)
    elif kind == "sensor":
        config = _sensor_config(key, fields)
    else:
        config = {}
    return {"key": key, "type": kind, "config": config}


def build_manifest(declarations: "Sequence[Mapping[str, object]] | None") -> "dict[str, object]":
    """Check declarations and build the manifest for the reserved `capabilities` topic.

    Args:
        declarations: At least one capability declaration, each with a unique key.

    Returns:
        `{"capabilities": [{"key": ..., "type": ..., "config": {...}}]}`.

    Raises:
        CapabilityError: The list is missing or empty, or a declaration is invalid.
    """
    if not isinstance(declarations, (list, tuple)) or not declarations:
        raise CapabilityError("a device must declare at least one capability at connect")
    capabilities: list[dict[str, object]] = []
    seen: list[object] = []
    for declaration in declarations:
        entry = _entry(declaration)
        if entry["key"] in seen:
            raise CapabilityError(f"duplicate capability key: {entry['key']}")
        seen.append(entry["key"])
        capabilities.append(entry)
    return {"capabilities": capabilities}


def fits_declaration(entry: "Mapping[str, object]", command: "Command") -> bool:
    """Whether a decoded command fits the capability as declared, as device-sdk-js checks it.

    The platform checks commands when it queues them, so a misfit arrived out of band.

    Args:
        entry: The capability's manifest entry, from `build_manifest`.
        command: The decoded command.

    Returns:
        True for a trigger with no value, a boolean for `onoff`, a finite number within
        `min`..`max` for `range`, or a declared option for `enum`; False otherwise, and for a
        sensor.
    """
    kind = entry["type"]
    if kind == "trigger":
        return command.trigger
    if command.trigger:
        return False
    config: dict[str, object] = entry["config"]  # pyright: ignore[reportAssignmentType] -- build_manifest's shape
    return _fits_value(kind, config, command.value)


def _fits_value(kind: object, config: "dict[str, object]", value: object) -> bool:
    if kind == "onoff":
        return isinstance(value, bool)
    if kind == "enum":
        options = config["options"]
        return isinstance(value, str) and isinstance(options, list) and value in options
    if kind != "range" or not _is_finite(value):
        return False
    low = config["min"]
    high = config["max"]
    return (
        isinstance(value, (int, float))
        and isinstance(low, (int, float))
        and isinstance(high, (int, float))
        and low <= value <= high
    )
