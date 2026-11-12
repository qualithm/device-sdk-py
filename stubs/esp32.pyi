# The part of MicroPython 1.29.0's esp32 the ESP32 adapters use, typed: the board stub leaves it
# untyped. The S2, S3, C3 and C6 have it; the original ESP32 has raw_temperature instead.

def mcu_temperature() -> int: ...
