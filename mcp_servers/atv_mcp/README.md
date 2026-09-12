# ATV local MCP

Independent, trusted Android TV MCP service. The host connects through stdio and does not import this package or read its configuration. Host tests do not install, launch or test it.

Install this directory with `python -m pip install -e .`. Configure `serial` and ADB settings in `config.yaml`, then run:

```text
atv-mcp --config config.yaml
```

The package requires Python 3.12+ and an available ADB executable. Configuration paths are supplied explicitly, so the package can be moved or installed independently.

- `get_device_capabilities` returns available key values, descriptions and effective limits.
- `execute_operations` accepts ordered operations with a discriminated `type`: `press_key` (`key`), `screenshot`, `wait` (`duration_ms`), or `text` (`text`). Discover keys before choosing them.
- The entire queue is validated before a single liveness probe and execution. Consecutive screenshots are rejected. Disconnected targets produce a standard tool error containing “设备断连”; no reconnect is attempted.
- Key/text operations settle for `post_action_wait_ms` unless followed immediately by explicit `wait`. Screenshot/wait operations do not settle. An explicit wait replaces the default, including when shorter.
- Defaults: 16 operations, 60-second queue, 15-second operation, 3 screenshot attempts, explicit wait 100–10000 ms, automatic wait 1000 ms. YAML owns all limits; 0 disables automatic waiting.
- Execution is serialized. Errors stop the queue and return ordered partial text/images plus the failure position. Only screenshot acquisition may retry. Request cancellation is observed between bounded operations and during automatic waits; subprocess cleanup is bounded by the current command.

Only static checking is performed for this external package. Its behavior is not duplicated in the generic Fake MCP service.
