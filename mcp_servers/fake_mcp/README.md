# Fake MCP：可回放的协议服务

Fake MCP 按预先编写的场景公开 MCP 工具和静态资源，并返回可预测的结果。它适合验证工具发现、调用顺序、图片返回、超时、取消和连接故障，不需要电视、模型凭据或外部网络。

本服务作为独立 stdio 进程运行。使用 TestPilot 的流程见[项目 README](../../README.md)，应用通过协议连接服务；应用测试也使用独立进程，不导入本服务实现。

## 安装与启动

准备 Python 3.12 或以上的服务环境，在本 README 所在目录执行：

```powershell
$env:PYTHONUTF8 = "1"
python -m pip install -e .
```

依赖由本服务的 [pyproject.toml](pyproject.toml) 声明，可单独选择服务环境。

先将下节示例保存为本目录下的 `scenario.json`，然后运行：

```powershell
fake-mcp --scenario scenario.json --record calls.jsonl
```

已有声明依赖时，也可使用源码入口：

```powershell
$env:PYTHONUTF8 = "1"
python src/fake_mcp_server.py --scenario scenario.json --record calls.jsonl
```

服务启动后等待 MCP 客户端请求，通过普通终端输入文字不会调用工具。`--scenario` 和 `--record` 都是必填参数，相对路径按进程工作目录解析；记录文件的父目录需预先存在。

接入应用时，按[外部工具连接](../../README.md#外部工具连接)配置本服务的解释器、工作目录及上述源码入口参数。为同一回放序列固定记录文件路径；手动检查工具可使用 [MCP Inspector](../../user_scripts/README.md#检查-mcp-服务)。

## 编写工具场景

以下场景公开一个 `lookup` 工具，预期收到一次查询：

```json
{
  "tools": [
    {
      "name": "lookup",
      "description": "Look up a document by title",
      "inputSchema": {
        "type": "object",
        "properties": {
          "title": {"type": "string"}
        },
        "required": ["title"],
        "additionalProperties": false
      }
    }
  ],
  "calls": [
    {
      "name": "lookup",
      "arguments": {"title": "example"},
      "result": {
        "content": [
          {"type": "text", "text": "Found example"}
        ]
      }
    }
  ]
}
```

在客户端调用 `lookup({"title": "example"})` 后，应得到文本 `Found example`，记录中出现对应的 `started` 和 `completed`。

`tools` 声明标准 MCP 工具，`calls` 按顺序声明预期调用。每项的 `name`、`arguments` 和 `result` 分别是工具名、预期参数和标准 MCP `CallToolResult`：

- 文本块使用 `type: "text"` 和 `text`。
- 图片块使用 `type: "image"`、Base64 字符串 `data` 和 `mimeType`。
- `structuredContent` 可返回结构化内容，`isError: true` 表示工具业务错误。
- 同一返回中的内容块按声明顺序保留。

调用前先按公开的 `inputSchema` 校验参数。未知工具和 schema 不合格的请求返回工具错误，不消耗回放步骤。通过校验的请求会消耗下一步：名称或参数与预期不一致时返回 `Unexpected call at index ...`；用尽后返回 `Replay exhausted`。这些已接收请求仍会推进位置。

## 延迟与故障

| 场景字段 | 默认值 | 作用 |
|---|---|---|
| 顶层 `startup_delay_seconds` | `0` | 延迟协议启动，用于准备阶段的超时与取消 |
| 调用项 `delay_seconds` | `0` | 延迟该次工具回复 |
| 调用项 `disconnect` | `false` | 接收该次调用后直接退出进程，不返回结果 |
| 调用项 `cooperative_cancellation` | `true` | 延迟期间可处理取消；设为 `false` 模拟延迟期间无法响应取消的操作 |
| 顶层 `resource_disconnects` | `[]` | 读取所列 URI 时直接退出进程 |

迟到的服务端完成记录只证明服务端行为；客户端是否接收结果，应从客户端自己的记录判断。

## 提供静态资源

场景可以同时提供工具和资源，也可以将 `tools`、`calls` 设为空列表，创建仅提供资源的服务。例如：

```json
{
  "tools": [],
  "calls": [],
  "resources": [
    {
      "uri": "resource://guide",
      "name": "guide",
      "mimeType": "text/plain"
    }
  ],
  "resource_contents": {
    "resource://guide": [
      {
        "uri": "resource://guide",
        "mimeType": "text/plain",
        "text": "Use actual observations to evaluate the task."
      }
    ]
  }
}
```

| 字段 | 内容 |
|---|---|
| `resources` | 标准 MCP 资源条目列表 |
| `resource_templates` | 标准 MCP 资源地址模板列表，可包含 `uriTemplate`、`name` 等字段 |
| `resource_contents` | URI 到有序资源内容列表的映射；文字内容使用 `text`，二进制内容使用 Base64 `blob`，并提供 `uri` 和 `mimeType` |
| `resource_disconnects` | 读取时触发断连的 URI 列表 |

资源列表和实际读取内容分别声明。未提供内容的 URI 会返回“Resource not found”；模板不会自动生成内容。资源发现、模板查询和资源读取不消耗工具回放步骤。

为 TestPilot 提供 Skill 时，入口 URI、包范围和附件发现规则见[技能资料与静态资源](../../skills/README.md#通过-mcp-提供资料)。

## 回放位置与调用记录

`--record` 指定 JSONL 文件，每行包含阶段 `phase`、进程号 `pid`、单调时间 `time_seconds` 及该阶段事实。工具请求的 `index` 从零开始；单调时间可用于比较耗时。

| 阶段 | 记录内容 |
|---|---|
| `opened`、`closed` | 服务打开和正常收尾；进程直接退出时可能没有关闭记录 |
| `discovery` | 工具发现 |
| `started` | 已接收的工具名、参数和回放位置 |
| `completed` | 对应工具结果准备完成 |
| `cancelled` | 服务处理了该次调用取消 |
| `disconnected` | 调用项触发主动断连 |
| `resource.list`、`resource.read` | 资源发现或读取，读取记录包含 URI |

每个新进程重新读取场景，通过已有记录中 `started` 的数量继续回放。因此，发现过程不会重置位置，已开始后超时或断连的调用也不会被自动重放。

需要从头开始时，选择新的记录文件路径。不要让多个并发回放共享一个记录文件；该文件用于同一串行回放序列跨会话延续。

## 排查问题

| 现象 | 检查方向 |
|---|---|
| 服务启动失败 | 核对当前 Python 环境、声明依赖、场景 JSON 格式及文件路径 |
| 没有工具结果 | 确认客户端已发起 MCP 调用，并检查延迟、断连和记录文件 |
| 预期调用不匹配 | 对照 `started` 中的参数与下一项场景，检查是否复用了旧记录文件 |
| 刚开始就提示回放耗尽 | 使用新记录路径，或检查场景是否覆盖所有实际收到的工具请求 |
| 资源能列出但读不到 | 在 `resource_contents` 中为实际请求的完整 URI 提供内容 |

应用侧的自动化验证入口见[开发入口](../../README.md#文档与开发入口)。
