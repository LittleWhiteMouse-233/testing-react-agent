# ATV local MCP

Independent, trusted Android TV MCP service. The host connects through stdio and does not import this package or read its configuration. Host tests do not install, launch or test it.

With the dependencies declared in `pyproject.toml` available, configure `ip_address` and ADB settings in `config.yaml`, then run directly from this directory without installing the ATV package:

```text
python -m src.atv_mcp --config config.yaml
```

This is the launch mode used by the repository's default MCP configuration. Alternatively, install this directory with `python -m pip install -e .` and run:

```text
atv-mcp --config config.yaml
```

The package requires Python 3.12+ and an available ADB executable. Configuration paths are supplied explicitly, so the package can be moved or installed independently.

Set `ip_address: "192.168.31.102"` to the TV's IP address, without a port. The service derives the ADB serial using the default TCP port 5555 and establishes the connection itself; no manual `adb connect` is required. The TV must have network ADB enabled on that port and authorize this computer. IP-only configuration does not discover custom ports or perform wireless-debugging pairing.

- `get_device_capabilities` returns available key values, descriptions and effective limits.
- `execute_operations` accepts ordered operations with a discriminated `type`: `press_key` (`key`), `screenshot`, `wait` (`duration_ms`), or `text` (`text`). Discover keys before choosing them.
- The entire queue is validated before the initial liveness probe and execution. Capability discovery and every device command (keys, text and each screenshot attempt) check `get-state`. An unavailable target triggers one `adb connect` followed by another `get-state`; failure produces a standard tool error containing “设备断连” and connection diagnostics. Consecutive screenshots are rejected.
- Key/text operations settle for `post_action_wait_ms` unless followed immediately by explicit `wait`. Screenshot/wait operations do not settle. An explicit wait replaces the default, including when shorter.
- Defaults: 16 operations, 60-second queue, 15-second operation, 3 screenshot attempts, explicit wait 100–10000 ms, automatic wait 1000 ms. YAML owns all limits; 0 disables automatic waiting.
- Execution is serialized. Errors stop the queue and return ordered partial text/images plus the failure position. Key/text commands are never replayed after a command failure; only screenshot acquisition may retry. Each ADB subprocess uses the operation timeout; checks, connection and execution within a queue operation share its operation budget. The queue timeout also includes its initial probe and connection attempt. Request cancellation is observed between bounded operations and during automatic waits; subprocess cleanup is bounded by the current command.

Only static checking is performed for this external package. Its behavior is not duplicated in the generic Fake MCP service.
