import json
import unittest

from device_fakes import wire

from qualithm_device import (
    TopicError,
    check_publish_topic,
    check_topic_filter,
    command_key,
    decode_command,
    is_valid_capability_key,
    telemetry_payload,
)


class CapabilityKeyTest(unittest.TestCase):
    def test_accepts_and_rejects_the_wire_keys(self):
        fixture = wire("command")
        for key in fixture["validKeys"]:
            self.assertTrue(is_valid_capability_key(key), key)
        for key in fixture["invalidKeys"]:
            self.assertFalse(is_valid_capability_key(key), key)
        self.assertTrue(is_valid_capability_key("k" * 64))


class CommandTest(unittest.TestCase):
    def test_decodes_the_wire_payloads(self):
        fixture = wire("command")
        for case in fixture["decoded"]:
            command = decode_command(case["payload"].encode())
            self.assertEqual(command.trigger, case["trigger"])
            self.assertEqual(command.value, case.get("value"))
        for payload in fixture["rejected"]:
            with self.assertRaises(TopicError):
                decode_command(payload.encode())

    def test_reads_the_key_a_command_topic_addresses(self):
        self.assertEqual(command_key("command/power"), "power")
        self.assertIsNone(command_key("command/bad key"))
        self.assertIsNone(command_key("telemetry"))


class TopicTest(unittest.TestCase):
    def test_accepts_device_relative_topics(self):
        for topic in ("telemetry", "capabilities", "events/door"):
            check_publish_topic(topic)
        for topic_filter in ("command/#", "events/+/open", "#"):
            check_topic_filter(topic_filter)

    def test_rejects_what_the_gateway_would(self):
        for topic in ("", "/telemetry", "telemetry/", "a//b", "a/+", "a/#", "a\x00b"):
            with self.assertRaises(TopicError):
                check_publish_topic(topic)
        for topic_filter in ("a/#/b", "a/b#", "a/+b", "a//#"):
            with self.assertRaises(TopicError):
                check_topic_filter(topic_filter)


class TelemetryTest(unittest.TestCase):
    def test_builds_the_stored_reading(self):
        payload = telemetry_payload({"temperature": 21.4, "count": 3}, 1767225600000)
        self.assertEqual(
            json.loads(payload.decode()),
            {"ts": 1767225600000, "metrics": {"temperature": 21.4, "count": 3}},
        )

    def test_rejects_a_reading_the_platform_wouldnt_store(self):
        for metrics in ({}, {"on": True}, {"label": "x"}, {"": 1}):
            with self.assertRaises(TopicError):
                telemetry_payload(metrics, 0)  # pyright: ignore[reportArgumentType] -- the wrong types under test
