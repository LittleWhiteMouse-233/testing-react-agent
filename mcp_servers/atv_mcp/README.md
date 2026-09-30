# ATV local MCP

Independent, trusted Android TV MCP service. The host connects through stdio and does not import this package or read its configuration. Host tests do not install, launch or test it.

With the dependencies declared in `pyproject.toml` available, first copy `config.example.yaml` to `config.yaml` in this directory (PowerShell: `Copy-Item config.example.yaml config.yaml`). Do this only for initial setup; keep an existing local configuration. Configure `ip_address` and ADB settings in `config.yaml`, then run directly from this directory without installing the ATV package:

```text
python src/atv_mcp_server.py --config config.yaml
```

This is the launch mode used by the repository's default MCP configuration. Alternatively, install this directory with `python -m pip install -e .` and run:

```text
atv-mcp --config config.yaml
```

The package requires Python 3.12+ and an available ADB executable. Configuration paths are supplied explicitly, so the package can be moved or installed independently.

`config.example.yaml` is the version-controlled template; `config.yaml` is local device configuration covered by this directory's `.gitignore`. Replace the template's `192.0.2.1` address with the TV's IP address, without a port. The service derives the ADB serial using the default TCP port 5555 and establishes the connection itself; no manual `adb connect` is required. The TV must have network ADB enabled on that port and authorize this computer. IP-only configuration does not discover custom ports or perform wireless-debugging pairing.

- `get_device_capabilities` returns available key values, descriptions and effective limits.
- `execute_operations` accepts ordered operations with a discriminated `type`: `press_key` (`key`), `screenshot`, `wait` (`duration_ms`), or `text` (`text`). Valid keys are enumerated in the tool schema from the same `RemoteKey` used by the controller and capability discovery. An invalid key is rejected by FastMCP/Pydantic before entering the queue function; it returns a framework validation error rather than a queue-position error, and no operations execute.
- The entire queue is validated before the initial liveness probe and execution. Capability discovery and every device command (keys, text and each screenshot attempt) check `get-state`. An unavailable target triggers one `adb connect` followed by another `get-state`; failure produces a standard tool error containing “设备断连” and connection diagnostics. Consecutive screenshots are rejected.
- Key/text operations settle for `post_action_wait_ms` unless followed immediately by explicit `wait`. Screenshot/wait operations do not settle. An explicit wait replaces the default, including when shorter.
- Defaults: 16 operations, 60-second queue, 15-second operation, 3 screenshot attempts, explicit wait 100–10000 ms, automatic wait 1000 ms. YAML owns all limits; 0 disables automatic waiting.
- Execution is serialized. Errors stop the queue and return ordered partial text/images plus the failure position. Key/text commands are never replayed after a command failure; only screenshot acquisition may retry. Each ADB subprocess uses the operation timeout; checks, connection and execution within a queue operation share its operation budget. The queue timeout also includes its initial probe and connection attempt. Request cancellation is observed between bounded operations and during automatic waits; subprocess cleanup is bounded by the current command.

Exceptions with an empty message report their exception type in queue and screenshot diagnostics. Existing nonempty diagnostics and ordered partial results are retained.

## Use the controller from Python

`src/atv_adb_ctrl/` is the reusable control package. `adb.py` owns ADB connection checks, commands and subprocess cleanup; `controller.py` owns device operations; `keys.py` owns remote keys and Android keyevent codes. `src/atv_mcp_server.py` owns MCP configuration loading, tool registration and queue policy.

Install this project with `python -m pip install -e .` in your chosen service environment to import the control package from scripts in any directory. The same installation provides the MCP command and dependencies; the control package itself does not import MCP or load YAML. Importing the package or constructing a controller does not connect to the TV.

```python
import asyncio
from ipaddress import ip_address
from pathlib import Path

from atv_adb_ctrl import ATVController, ATVControlSettings, RemoteKey


async def main() -> None:
    controller = ATVController(ATVControlSettings(
        ip_address=ip_address("192.0.2.1"),  # Replace with the TV's address.
    ))
    await controller.press_key(RemoteKey.HOME)
    await controller.wait(1000)
    Path("screen.png").write_bytes(await controller.screenshot())


asyncio.run(main())
```

`probe()`, `run(*arguments)`, `press_key(key)`, `input_text(text)`, `screenshot()` and `wait(duration_ms)` are asynchronous. `serial` is a read-only property. Device commands probe and connect as needed; screenshots return PNG bytes and retain bounded retries. Failures propagate as Python exceptions. Settings are supplied at construction; create a new controller to change the target or command settings.

Standalone callers own operation sequencing, concurrency, overall deadlines and settling delays. Key/text operations do not automatically wait. MCP-only queue limits, wait bounds, cancellation shielding, consecutive-screenshot restrictions and partial-result reporting remain in the server; scripts do not need those policies to control a TV.

`ATVControlSettings` owns device settings. `ATVServerSettings` extends it with MCP queue settings while retaining the flat YAML format and existing defaults. Both belong to this external project; the host does not read either configuration model.

Only static checking and packaging inspection are performed for this external package. Its behavior is not duplicated in the generic Fake MCP service; application regression tests neither import nor launch ATV.

For static checking, prepare Pyright, the service dependencies and `types-PyYAML` in the service's Python environment, then run from this directory:

```text
python -m pyright .
```

These checks are separate from application dependency installation and verification and do not require launching the service.
