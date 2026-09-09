# Scripted local MCP

Independent stdio protocol replay service. It has no Android TV or host application dependency.

Install with `python -m pip install -e .`, then start with:

```text
fake-mcp --scenario scenario.json --record calls.jsonl
```

The host can also launch `src/fake_mcp/__main__.py` directly with a Python interpreter that has the declared dependencies. All paths are CLI arguments; no host deployment configuration is read.

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
