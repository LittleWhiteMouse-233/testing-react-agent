# Scripted local MCP

Independent stdio protocol replay service. It has no Android TV or host application dependency.

The service uses MCP SDK 2.3 public server callbacks. Scenarios may also declare `resources`, `resource_templates`, and `resource_contents` (URI to ordered standard text/blob resource contents). Set `tools` to an empty list for a resource-only server. Resource listings and reads have separate JSONL phases and never advance the business-tool replay index. `resource_disconnects` lists URIs whose reads terminate the process to exercise transport failure.

SDK 2's low-level callbacks do not validate the advertised tool schema. The service uses public `jsonschema.validate` before replay dispatch so invalid parameters return a normal tool error without consuming an operation.

Install with `python -m pip install -e .`, then start with:

```text
fake-mcp --scenario scenario.json --record calls.jsonl
```

The host can also launch the single service module directly from this project directory with a Python interpreter that has the declared dependencies:

```text
python src/fake_mcp_server.py --scenario scenario.json --record calls.jsonl
```

`src/fake_mcp_server.py` contains the scenario models, protocol replay and CLI. There is no device-control package. All paths are CLI arguments; no host deployment configuration is read. Application tests launch this entry in a separate process and never import the service implementation.

A scenario declares standard MCP tools and an ordered list of expected requests:

```json
{
  "tools": [{
    "name": "lookup",
    "description": "Look up a document",
    "inputSchema": {
      "type": "object",
      "properties": {"title": {"type": "string"}},
      "required": ["title"]
    }
  }],
  "calls": [{
    "name": "lookup",
    "arguments": {"title": "example"},
    "result": {"content": [{"type": "text", "text": "Found example"}]},
    "delay_seconds": 0,
    "disconnect": false
  }]
}
```

`result` is a standard MCP `CallToolResult`, including ordered text/image blocks, optional `structuredContent`, and `isError`. Images use `data` (base64) and `mimeType`. Delays keep a request pending for cancellation/timeout scenarios; `disconnect` exits the process before returning a result. Unexpected calls and exhausted scenarios return tool errors.

The JSONL record reports discovery, received arguments, response completion, cancellation and process lifecycle, with process ID and request index. It is the external observation interface for integration tests; consumers do not import service objects.

Each record also includes monotonic `time_seconds` for elapsed-time observations. Set a call's `cooperative_cancellation` to `false` to simulate a blocking external operation that cannot process cancellation during its delay; its late completion must not be treated as a confirmed host result. The scenario's optional `startup_delay_seconds` delays protocol startup to exercise initialization timeout and cancellation. Each newly started process reads the scenario file anew and continues at the request index derived from existing `started` records. Discovery and schema-rejected requests do not advance the sequence; every valid received call advances it once, including calls that time out or disconnect. Use a fresh record path to begin a new replay sequence.
