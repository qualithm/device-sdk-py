"""The device-relative MQTT subject grammar the gateway accepts.

A device publishes and subscribes on topics relative to its own identity; the gateway derives the
tenant-scoped subject from the verified credential, so a device never names its team or itself.
"""

import json

from qualithm_device._errors import TopicError

TELEMETRY_TOPIC = "telemetry"
MANIFEST_TOPIC = "capabilities"
COMMAND_PREFIX = "command/"
COMMAND_FILTER = "command/#"
MAX_KEY_LENGTH = 64
_KEY_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def is_valid_capability_key(key: str) -> bool:
    """Whether a capability key fits `^[a-z0-9][a-z0-9_-]{0,63}$`, case-insensitive.

    Args:
        key: The key.

    Returns:
        True when it fits.
    """
    if not key or len(key) > MAX_KEY_LENGTH or key[0] not in _KEY_CHARS:
        return False
    return all(char in _KEY_CHARS or char in "_-" for char in key[1:])


def _levels(topic: str) -> "list[str]":
    if not topic or topic.startswith("/") or topic.endswith("/") or "\x00" in topic:
        raise TopicError(f"topic must be device-relative, with no empty level: {topic!r}")
    levels = topic.split("/")
    if "" in levels:
        raise TopicError(f"topic has an empty level: {topic!r}")
    return levels


def check_publish_topic(topic: str) -> None:
    """Fail unless a topic can be published to: relative, no empty level, no wildcard.

    Args:
        topic: The device-relative topic.

    Raises:
        TopicError: The topic breaks the grammar.
    """
    for level in _levels(topic):
        if "+" in level or "#" in level:
            raise TopicError(f"a publish topic can't hold a wildcard: {topic!r}")


def check_topic_filter(topic_filter: str) -> None:
    """Fail unless a subscription filter is valid: `+` fills a level, `#` only ends one.

    Args:
        topic_filter: The device-relative filter.

    Raises:
        TopicError: The filter breaks the grammar.
    """
    levels = _levels(topic_filter)
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            raise TopicError(f"'#' must be the whole last level: {topic_filter!r}")
        if "+" in level and level != "+":
            raise TopicError(f"'+' must be a whole level: {topic_filter!r}")


def command_key(topic: str) -> "str | None":
    """The capability key a `command/<key>` topic addresses.

    Args:
        topic: An inbound topic.

    Returns:
        The key, or None when the topic isn't a command or the key is invalid.
    """
    if not topic.startswith(COMMAND_PREFIX):
        return None
    key = topic[len(COMMAND_PREFIX) :]
    return key if is_valid_capability_key(key) else None


class Command:
    """A decoded command (Decision qualithm/pm#747): a value to set, or a trigger.

    Attributes:
        trigger: True for a trigger, which carries no value.
        value: The value to set; None for a trigger.
    """

    def __init__(self, trigger: bool, value: object = None) -> None:
        """Hold the decoded command."""
        self.trigger = trigger
        self.value = value


def decode_command(payload: bytes) -> Command:
    """Decode a command payload: `{"value": ...}` sets a value, `{}` is a trigger.

    Args:
        payload: The message body.

    Returns:
        The command.

    Raises:
        TopicError: The payload isn't one of the two forms.
    """
    try:
        data = json.loads(payload.decode())
    except ValueError:
        raise TopicError("command payload is not valid JSON") from None
    if not isinstance(data, dict):
        raise TopicError("command payload must be a JSON object")
    keys = list(data)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType] -- JSON
    if not keys:
        return Command(trigger=True)
    if keys == ["value"]:
        return Command(trigger=False, value=data["value"])  # pyright: ignore[reportUnknownArgumentType] -- JSON
    raise TopicError('command payload must be {"value": ...} or {}')


def telemetry_payload(metrics: "dict[str, float]", ts_ms: int) -> bytes:
    """A reading as the platform stores it: `{"ts": <epoch ms>, "metrics": {name: number}}`.

    Args:
        metrics: Each metric's name and numeric value.
        ts_ms: The reading's time, in Unix milliseconds.

    Returns:
        The JSON payload for the `telemetry` topic.

    Raises:
        TopicError: There are no metrics, or a value isn't a number.
    """
    if not metrics:
        raise TopicError("a reading needs at least one metric")
    for name, value in metrics.items():
        if not name or isinstance(value, bool) or not isinstance(value, (int, float)):  # pyright: ignore[reportUnnecessaryIsInstance] -- callers aren't typed on a board
            raise TopicError(f"metric {name!r} must be a number")
    return json.dumps({"ts": ts_ms, "metrics": metrics}).encode()
