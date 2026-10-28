# Device SDK

[![CI](https://github.com/qualithm/device-sdk-py/actions/workflows/ci.yaml/badge.svg)](https://github.com/qualithm/device-sdk-py/actions/workflows/ci.yaml)
[![codecov](https://codecov.io/gh/qualithm/device-sdk-py/graph/badge.svg)](https://codecov.io/gh/qualithm/device-sdk-py)
[![PyPI](https://img.shields.io/pypi/v/qualithm-device)](https://pypi.org/project/qualithm-device/)

Device SDK for MicroPython and Python devices connecting to Qualithm. One `connect()` call claims
the device once, stores its credential, renews it before it expires, opens its gateway session, and
declares what the device can do.

## Features

- **Claim once.** The single-use claim code is exchanged on first boot; every later boot loads the
  stored credential.
- **Renewal on time.** A token or certificate credential renews two thirds of the way through its
  lifetime, and a failed renewal keeps the device on its current credential.
- **Capabilities declared on every connect.** Dashboards and automations list what a device
  declares, so every device declares at least one capability.
- **Certificate enrollment.** A token credential upgrades to an mTLS certificate.
- **The gateway's subject grammar.** Device-relative topics, capability commands and readings are
  checked before they go on the wire.
- **Board-agnostic.** The core runs on MicroPython and CPython. It reaches the network, crypto and
  storage only through three small adapters the board supplies.
- **Pico 2 W adapters included.** `qualithm_device.pico` joins Wi-Fi, verifies every TLS
  certificate, and stores the credential in flash.

## Installation

On a board running MicroPython 1.29.0:

```python
import mip

mip.install("github:qualithm/device-sdk-py@0.3.0")
```

On CPython:

```bash
uv add "qualithm-device @ git+https://github.com/qualithm/device-sdk-py"
```

## Run it on a Pico 2 W

You need a Pico 2 W, a USB cable, a 2.4 GHz Wi-Fi network, and a claim code: the single-use code
the platform gives you when you add a device.

1. Flash MicroPython 1.29.0: hold BOOTSEL while plugging the board in, then copy the Pico 2 W `.uf2`
   from [micropython.org/download/RPI_PICO2_W](https://micropython.org/download/RPI_PICO2_W/) onto
   the `RP2350` drive that appears.
2. Join Wi-Fi and install the SDK with [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html):

   ```bash
   uvx mpremote exec "import network; w = network.WLAN(network.STA_IF); w.active(True); w.connect('<your-ssid>', '<your-password>')"
   uvx mpremote exec "import mip; mip.install('github:qualithm/device-sdk-py@0.3.0')"
   ```

3. Write the setup file the board reads on boot, with your network and claim code filled in:

   ```bash
   uvx mpremote exec "import json; open('/qualithm-setup.json', 'w').write(json.dumps({'provisioningUrl': 'https://api.qualithm.com', 'wifi': {'ssid': '<your-ssid>', 'passphrase': '<your-password>'}, 'claimCode': '<your-claim-code>'}))"
   ```

4. Save this as `main.py`:

   ```python
   import time

   from qualithm_device import pico

   device = pico.start()
   while True:
       device.publish_reading(pico.system_info())
       device.poll()
       device.renew_if_due()
       time.sleep(60)
   ```

5. Copy it to the board and reset it:

   ```bash
   uvx mpremote cp main.py :main.py + reset
   ```

`pico.start` joins the network in `/qualithm-setup.json` and connects. On first boot the board
claims itself, stores its credential in flash, and removes the spent claim code from the setup
file; on a later boot it reuses the stored credential. Every minute it sends its uptime, free
memory, chip temperature, Wi-Fi signal strength and firmware version. `pico.start` declares each of
these as a sensor, so they appear on a dashboard.

The board's LED shows where it is:

| LED                                  | State                                       |
| ------------------------------------ | ------------------------------------------- |
| Slow pulse, one second on, one off   | Joining Wi-Fi and reaching the gateway      |
| Q in Morse (long, long, short, long) | Just claimed                                |
| Steady                               | Connected                                   |
| Fast flash, twice a second           | Needs attention: read the error on the REPL |
| Off                                  | Not running, or unpowered                   |

Pass `light=None` to leave the LED to your own code. To show your own state later, pass a
`pico.StatusLight()` as `light` and call its `show()` with one of `ui.LIGHT_STEPS`.

To pass the details in code instead, call `pico.join_wifi(ssid, password)` and then
`pico.connect(provisioning_url, claim_code=...)`.

`join_wifi` sets the clock from NTP, since the board checks each server's certificate and its
dates. A Pico connects with a token credential: it has no on-board key generation for a
certificate.

## Run it as a virtual device

A virtual device runs your project on MicroPython's Unix port on your computer, with no board. It
claims, connects and reports as a Pico does, and counts as a device on your plan. You need a claim
code.

1. Install MicroPython 1.29.0 and the SDK:

   ```bash
   brew install micropython
   micropython -m mip install github:qualithm/device-sdk-py@0.3.0
   ```

2. Write the setup file in your project's directory, with your claim code filled in:

   ```bash
   echo '{"provisioningUrl": "https://api.qualithm.com", "claimCode": "<your-claim-code>"}' > qualithm-setup.json
   ```

3. Save this as `main.py`. It runs unchanged on a Pico 2 W:

   ```python
   import time

   from qualithm_device import board

   device = board.start()
   while True:
       device.publish_reading(board.system_info())
       device.poll()
       device.renew_if_due()
       time.sleep(60)
   ```

4. Run it:

   ```bash
   micropython main.py
   ```

`board.start` uses the `pico` adapters on a Pico and the `virtual` adapters anywhere else. The
virtual device tells the platform it is virtual when it claims. It stores its credential in
`qualithm-credential.json`, next to the setup file. Anyone who can read that directory can read
the credential. Every minute it sends its uptime, free memory, firmware version and a stand-in chip
temperature that swings around 25 °C.

## Quick Start

```python
from qualithm_device import connect

device = connect(
    "https://api.qualithm.com",
    transport=transport,  # the board's HTTPS and MQTT clients
    store=store,  # where the credential persists
    capabilities=[
        {"key": "temperature", "type": "sensor", "metric": "temperature", "unit": "celsius"},
    ],
    claim_code="<your-claim-code>",
)
device.publish_reading({"temperature": 21.4})
```

`transport` and `store` are the board's adapters, described under Usage.

## Usage

### Adapters

The core holds the protocol; the board supplies three adapters, typed in
`qualithm_device.interfaces`:

| Adapter     | Methods                                                         |
| ----------- | --------------------------------------------------------------- |
| `transport` | `request(method, url, headers, body)` and `open(...)`           |
| `crypto`    | `generate_csr(device_id)` and `sign(private_key_pem, message)`  |
| `store`     | `load()`, `save(credential)` and `clear()`                      |

`qualithm_device.pico` holds the Pico 2 W and Pico W adapters: `Transport`, `FlashStore`,
`join_wifi` and `system_info`, and `connect`, which uses them and declares `system_info`'s sensors.

`transport.open` returns a session with `publish`, `subscribe`, `poll`, `close` and
`max_packet_size`. `crypto` is needed only for a certificate credential.

### Capabilities

Every device declares its capabilities at `connect()`: at least one, each with a unique `key`.
`connect()` raises `CapabilityError` for a missing, empty or invalid list, before it claims. Each
connect publishes the declarations to the reserved `capabilities` topic. Dashboards and automations
list only what a device declares.

| `type`    | Fields                                   | Commands carry      |
| --------- | ---------------------------------------- | ------------------- |
| `sensor`  | `metric`, optional `unit`                | nothing (read-only) |
| `onoff`   | none                                     | `True` or `False`   |
| `range`   | `min`, `max`, optional `step` and `unit` | a number            |
| `enum`    | `options`, at least one string           | one of the options  |
| `trigger` | none                                     | `None`              |

A sensor's `metric` names the reading it reports, as `publish_reading` sends it. On a Pico, pass
your own sensors to `pico.connect` and it declares them after the board's:

```python
device = pico.connect(
    "https://api.qualithm.com",
    claim_code="<your-claim-code>",
    capabilities=[
        {"key": "soil_moisture", "type": "sensor", "metric": "soil_moisture", "unit": "percent"},
    ],
)
device.publish_reading({"soil_moisture": 41.5})
```

`device.declare_capabilities([...])` replaces the set and publishes it at once. The next
`connect()` publishes the list passed to it.

### Commands

```python
device.on_command("power", lambda value: print("power", value))
while True:
    for topic, payload in device.poll():
        print(topic, payload)
```

A handler gets the command's value, or `None` for a trigger. Declare the key as a control first: a
sensor's key raises `CapabilityError`.

### Renewal and reconnecting

Call `device.renew_if_due()` from the main loop. A renewed credential is stored and used by the next
`connect()`. When the session drops, `poll()` raises `GatewayError`. Close the device, wait
`reconnect_delay(attempt)` milliseconds, and call `connect()` again.

### Error Handling

Every error subclasses `QualithmDeviceError`:

| Error                  | Raised when                                                            |
| ---------------------- | ---------------------------------------------------------------------- |
| `CapabilityError`      | no capability is declared, one is invalid, or a command hits a sensor  |
| `ClaimError`           | the claim endpoint is unreachable or rejects the code                  |
| `CredentialError`      | nothing is stored and there's no claim code, or the credential expired |
| `EnrollError`          | certificate enrollment fails                                           |
| `RenewError`           | renewal fails; kept in `device.renew_error`, never raised by `connect` |
| `GatewayError`         | no broker is known, or the session is refused or drops                 |
| `TopicError`           | a topic, capability key, command payload or reading breaks the grammar |
| `PayloadTooLargeError` | a payload exceeds the ceiling the gateway advertised                   |

## API Reference

The public API is what `src/qualithm_device/__init__.py` exports, documented in its docstrings:

```bash
uv run python -m pydoc qualithm_device
```

## Examples

See the [`examples/`](examples/) directory:

| Example                                       | Description                                       |
| --------------------------------------------- | ------------------------------------------------- |
| [`custom_adapters.py`](examples/custom_adapters.py) | The three adapters, written in memory, driving `connect()` |

```bash
uv run python examples/custom_adapters.py
```

## Development

### Prerequisites

- [uv](https://docs.astral.sh/uv/), which installs the Python this project needs
- `git`, `make` and a C compiler, to build the pinned MicroPython Unix port and `mpy-cross` once

### Setup

```bash
uv sync
git config core.hooksPath .githooks
```

### Testing

`tests/` runs on CPython under coverage and on MicroPython's Unix port; `tests_cpython/` runs on
CPython only. The pre-commit hook runs both, as CI does:

```bash
uv run --no-project --with pyyaml env -u VIRTUAL_ENV python .claude/checks/ci-jobs.py --skip-ci-only lint typecheck coverage micropython
```

### Releasing

Set the same `version` in `pyproject.toml` and `package.json`, rewrite the manifest, and commit:

```bash
python3 .github/micropython-package manifest
```

Merging the version to `main` tags it, and the tag creates the GitHub Release `mip` installs from.

## Minimum Supported Python Version

Python 3.11, as `requires-python` in `pyproject.toml` declares.

## Supported MicroPython Version

MicroPython 1.29.0, the version CI pins for the Unix port, `mpy-cross` and the board stubs.

## License

Apache-2.0
