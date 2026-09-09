# Subjective testing agent

自然语言用例 → 人工确认的不可变计划 → MCP 工具驱动的自适应执行 → 截图证据 → 确定性结果和报告。

## 结构和边界

- `backend/app/domain/`：用例、计划、运行、工具目录、模型快照和事件的领域事实。
- `backend/app/planning/`：无工具的规划流程，只读取用例；`execution/`：Act/Judge 共用执行图。
- `backend/app/tools.py`：标准 stdio MCP 配置、会话和 LangChain 工具导入；不理解设备或工具参数语义。
- `backend/app/event_stream/`：从 LangGraph updates 投影持久消息事件，提交后发布 SSE。
- `frontend/`：计划编辑、实时运行、历史和报告。
- `device/atv_mcp/`：可信外部 ATV MCP，独立配置和安装，不参与宿主测试。
- `device/fake_mcp/`：独立的通用 MCP 场景/响应回放服务，不依赖 ATV。
- `mcp.json`：宿主连接配置；`data/`：忽略的运行数据库、checkpoint、证据和导出。

宿主及其测试不 import 外部包，不管理设备 ID、状态、探活或配置。第三方工具只需通过 MCP 提供 schema 和结果；不需要实现项目专属的 scopes、manifest 或设备接口。

## 安装和启动

Python 使用已有 Conda 环境 `llm-dev`，PowerShell 每个会话先设置 UTF-8：

```powershell
$env:PYTHONUTF8 = "1"
cd backend
conda run -n llm-dev python -m pip install -e ".[dev]"
conda run -n llm-dev python -m alembic upgrade head
conda run -n llm-dev python -m uvicorn app.main:app --port 8000
```

如果本机 `conda.bat` 入口失效，可用 `D:/SoftwareInstalled/miniconda3/Scripts/conda.exe` 替代命令名，仍使用同一 Conda 环境。

另一个终端启动前端：

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

复制 `.env.example` 为 `.env`，配置模型和 MCP。默认 scripted 模型只用于测试；没有显式执行场景时返回 blocked，不会猜测工具名称或伪造截图。

## 宿主配置

| 配置 | 默认值 / 作用 |
|---|---|
| `LLM_PROFILES` | 有序模型配置列表；保留密钥在本地 `.env` |
| `TEST_AGENT_PLANNING_MODEL_ID` | 规划模型 ID，未设时使用首个 profile |
| `TEST_AGENT_EXECUTION_MODEL_ID` | Act/Judge 共用模型 ID，未设时使用首个 profile |
| `TEST_AGENT_MCP_CONFIG_PATH` | 仓库根目录 `mcp.json`，相对路径从根目录解析 |
| `TEST_AGENT_TOOL_CALL_TIMEOUT_SECONDS` | 120 秒，通用工具调用与发现超时 |
| `TEST_AGENT_SCREENSHOT_HISTORY_ROUNDS` | 3，State 中保留图片字节的最近决策轮数 |
| `TEST_AGENT_MODEL_CALL_MAX_ATTEMPTS` | 3，模型传输错误有限重试 |
| `TEST_AGENT_MODEL_RESPONSE_MAX_ATTEMPTS` | 3，连续无效响应/工具错误上限 |

旧的 `ADB_*`、Act/Judge 模型路由和设备操作参数已移除。模型上下文预算仍由 profile 的 context window、输出保留、图片 token 估计和安全余量共同决定。

`mcp.json` 使用 `mcpServers`，每个连接支持 `command`、`args`、`env`、`cwd`，首版仅 stdio。`cwd` 相对于配置文件解析；命令直接启动，不经过 shell。MCP SDK 继承其标准进程环境，再应用显式 `env`。不同服务的工具加服务名前缀。

连接在一次运行内复用；运行开始冻结实际工具目录，运行中不热加载。Planning 和历史查询不连接 MCP；配置/连接/发现失败时创建运行接口返回 502。只启用此次运行需要的服务。

## 独立 ATV MCP

ATV 仅作为示例外部服务。需要使用时单独安装：

```powershell
$env:PYTHONUTF8 = "1"
conda run -n llm-dev python -m pip install -e ./device/atv_mcp
```

编辑 `device/atv_mcp/config.yaml` 的 `serial`，确保 `mcp.json` 中的 Python 命令指向安装该包的环境。宿主不读取 YAML，也不提供设备选择 API。

- `get_device_capabilities()`：按键值、说明和有效限制，供模型主动发现。
- `execute_operations(operations)`：按序执行 `press_key`、`screenshot`、`wait`、`text`，返回原始顺序的文本和图片。
- 两个工具参数校验通过后先探活；失败直接返回“设备断连”，不自动重连。
- 整队列预校验，禁止相邻截图。失败停止后缀，已完成动作不重放；截图只重试当前操作。
- 默认最多 16 项、队列 60 秒、操作 15 秒、截图 3 次、显式等待 100–10000ms，均在 YAML 调整。
- 按键/输入后默认等待 `post_action_wait_ms: 1000`；下一项是显式 wait 时不叠加。末尾动作也等待，截图和 wait 本身不附加等待。自动等待不消耗模型操作预算。

该包不运行专属测试，仅做静态检查；宿主测试不会安装或启动它。

## 执行、证据和历史

Act 达成目标，Judge 验证行为；Judge 可主动准备条件和触发交互，但不得绕过待验证行为或修改成功标准。两者共用模型、工具和执行图。

图没有固定截图节点。每次模型回复计一个决策轮，包括能力查询、截图、纠错和结束请求；传输重试不另计。每轮只允许一个工具调用，终态使用宿主的 `finish_task(status, summary)`。

工具内容顺序保留。图片先解码保存原图，原 base64 连同 Artifact ID 进入 ToolMessage，不重新编码。默认最近 3 个决策轮保留全部图片，无图片轮也计数；更早的图片块替换为占位文本，保留动作、模型视觉描述和工具调用配对。

原始工具消息先投影为持久事件，旧图清理通过 add_messages 同 ID 更新 State，不重写已保存事件。清理缩小后续 checkpoint 的 State；不回收已有 checkpoint 历史。SSE/前端仅使用 Artifact ID，历史图与 JSON/HTML 导出不受窗口清理影响。

通过/失败必须引用本任务实际保存的截图；是否需要操作后重新取证由模型判断。轮数耗尽时有截图则 FAILED，无截图则 BLOCKED。全局 fail-fast、取消和最终 PASS/FAIL/BLOCKED/CANCELLED 聚合由程序决定。

通用工具边界对取消、超时和未知结果不进行自动重放。会话清理结束前保持单运行占用；未收到的结果明确为未确认。不同外部 MCP 的取消响应能力由其实现决定，宿主不伪造已完成操作回执。

## API 与数据升级

- `POST /api/test-cases/{id}/plans` 不需要请求体，规划不读取设备信息。
- `POST /api/runs` 接收 `test_plan_id` 和 `assumptions_confirmed: true`。
- 设备查询 API 和 device 字段已删除；运行保存一个 `execution_model` 和实际工具目录。
- 计划修订创建新版本，只允许 latest 启动；相同运行的事件只追加，重复导出创建新文件。
- `/api/runs/{id}/events` 和 `/stream` 提供同源历史与 SSE；`/api/artifacts/{id}` 下载证据。
- 执行协议为 `2`。开发期旧数据库、JSON、checkpoint 不兼容；旧数据目录应另行归档并使用新的空 `TEST_AGENT_DATA_DIR`，不执行数据迁移。

## 测试和静态检查

测试通过临时 mcp.json 启动 `device/fake_mcp`，场景只声明标准工具、预期调用、响应、延迟和断连。通过 JSONL 调用记录观察进程及执行顺序，不访问外部服务内部对象，不复制 ATV 规则。

```powershell
$env:PYTHONUTF8 = "1"
cd backend
conda run -n llm-dev python -m pytest -q
conda run -n llm-dev python -m pyright
conda run -n llm-dev python -m pyright ../device
conda run -n llm-dev python scripts/export_openapi.py ../frontend/src/api/openapi.json
cd ../frontend
npm.cmd run generate:api
npm.cmd test
npm.cmd run typecheck
npm.cmd run check:api
npm.cmd run build
```

静态检查独立包时需要其声明的依赖和 PyYAML 类型存根；这不要求启动 ATV。自动化不使用网络、真实模型密钥或电视。核心验证包括通用工具闭环、取消/超时、图片窗口、事件顺序、证据归属、计划版本、并发限制和导出。

MCP 适配器和 SDK 分别固定为 0.3.2、1.30.0。应用使用[官方适配器](https://github.com/langchain-ai/langchain-mcp-adapters/tree/v0.3.2)、公开 session、ToolNode wrapper、消息 reducer 和 checkpointer API。产品代码只补充框架不拥有的证据存储、轮数预算、必需任务上下文和视觉窗口；不读取私有 checkpoint 表、不复制消息协议。结构化返回值保留为消息文本，并清除嵌套的重复图片字节。

该版本 SDK 的公开工具调用接口不暴露请求 ID，宿主通过取消调用等待、退出会话并关闭 stdio 子进程完成清理，不猜测协议 ID 或访问私有计数器。MCP [取消规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)允许取消与返回结果竞态；没有收到的结果始终标为未确认。

Uvicorn 保持一个 worker，因为 ActiveRunRegistry 与 EventBus 是进程内对象。不要提交模型密钥、设备凭据、运行数据库、截图或导出。
