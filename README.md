# Subjective testing agent

自然语言用例 → 人工确认的不可变计划 → MCP 工具驱动的自适应执行 → 截图证据 → 确定性结果和报告。

## 结构和边界

- `backend/app/domain/`：用例、计划、运行、工具目录、模型快照和事件的领域事实。
- `backend/app/planning/`：无工具的规划流程，只读取用例；`execution/`：Act/Judge 共用执行图。
- `backend/app/tools.py`：标准 stdio MCP 配置、会话和 LangChain 工具导入；不理解设备或工具参数语义。
- `backend/app/event_stream/`：从 LangGraph updates 投影持久消息事件，提交后发布 SSE。
- `frontend/`：计划编辑、实时运行、历史和报告。
- `mcp_servers/atv_mcp/`：可信外部 ATV MCP，独立配置和安装，不参与宿主测试。
- `mcp_servers/fake_mcp/`：独立的通用 MCP 场景/响应回放服务，不依赖 ATV。
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
| `TEST_AGENT_TOOL_CALL_TIMEOUT_SECONDS` | 120 秒，通用工具调用与发现超时；可能包含会话重建 |
| `TEST_AGENT_TOOL_CLEANUP_TIMEOUT_SECONDS` | 30 秒，调用中止与会话收尾的独立等待上限 |
| `TEST_AGENT_SCREENSHOT_HISTORY_ROUNDS` | 3，State 保留原图 base64 的最近有截图决策轮数；过期图片替换为 artifact 引用 |
| `TEST_AGENT_MODEL_CALL_MAX_ATTEMPTS` | 3，模型传输错误有限重试 |
| `TEST_AGENT_MODEL_RESPONSE_MAX_ATTEMPTS` | 3，连续无效响应/工具错误上限 |

旧的 `ADB_*`、Act/Judge 模型路由和设备操作参数已移除。模型上下文预算仍由 profile 的 context window、输出保留、图片 token 估计和安全余量共同决定。

`mcp.json` 使用 `mcpServers`，每个连接支持 `command`、`args`、`env`、`cwd`，首版仅 stdio。`cwd` 相对于配置文件解析；命令直接启动，不经过 shell。MCP SDK 继承其标准进程环境，再应用显式 `env`。不同服务的工具加服务名前缀。

连接在一次运行内复用；运行开始冻结实际工具目录，运行中不热加载。Planning 和历史查询不连接 MCP；配置/连接/发现失败时创建运行接口返回 502。只启用此次运行需要的服务。

## 独立 ATV MCP

ATV 仅作为示例外部服务。需要使用时单独安装：

```powershell
$env:PYTHONUTF8 = "1"
conda run -n llm-dev python -m pip install -e ./mcp_servers/atv_mcp
```

编辑 `mcp_servers/atv_mcp/config.yaml` 的 `serial`，确保 `mcp.json` 中的 Python 命令指向安装该包的环境。宿主不读取 YAML，也不提供设备选择 API。

- `get_device_capabilities()`：按键值、说明和有效限制，供模型主动发现。
- `execute_operations(operations)`：按序执行 `press_key`、`screenshot`、`wait`、`text`，返回原始顺序的文本和图片。
- 两个工具参数校验通过后先探活；失败直接返回“设备断连”，不自动重连。
- 整队列预校验，禁止相邻截图。失败停止后缀，已完成动作不重放；截图只重试当前操作。
- 默认最多 16 项、队列 60 秒、操作 15 秒、截图 3 次、显式等待 100–10000ms，均在 YAML 调整。
- 按键/输入后默认等待 `post_action_wait_ms: 1000`；下一项是显式 wait 时不叠加。末尾动作也等待，截图和 wait 本身不附加等待。自动等待不消耗模型操作预算。

该包不运行专属测试，仅做静态检查；宿主测试不会安装或启动它。

## 执行、证据和历史

执行协议版本为 3。用户取消时仅发起一次调用中止，最多等待配置的 30 秒收尾；成功则 CANCELLED，失败或超时则 BLOCKED，故障不会被取消信号覆盖。产品取消请求使用运行事件，原生任务取消保持传播；工具包装器将图控制异常交回 LangGraph 处理。

收尾失败或超时后，当前进程拒绝所有新运行：HTTP 409 / `run_resources_unavailable`。排查并清理残留子进程或连接后重启服务恢复；不提供解锁接口，不在迟到收尾成功后自动恢复，也不自动终止宿主进程。已持久化的运行终态不会被之后的资源清理改写。

Act 达成目标，Judge 验证行为；Judge 可主动准备条件和触发交互，但不得绕过待验证行为或修改成功标准。两者共用模型、工具和执行图。

图没有固定截图节点。每次模型回复计一个决策轮，包括能力查询、截图、纠错和结束请求；传输重试不另计。每轮只允许一个工具调用，终态使用宿主的 `finish_task(status, summary)`。

工具内容顺序保留。图片先解码保存原图，原 base64 连同 Artifact ID 进入 ToolMessage，不重新编码。工具完成后，在 `after_tools` 阶段选取最近 3 个有截图的已完成决策轮，同轮多图全部保留，无图轮不占窗口。窗口外图片在 State 原内容位置替换为带证据引用的占位文本；仅复制变化的消息，通过公开 `add_messages` reducer 按相同 message ID 替换，不直接修改输入 State。该产品边界只实现截图轮数策略，消息合并及 checkpoint 序列化仍由框架负责。模型请求基于回收后的 State 执行 token 预算与必要上下文检查；token 裁剪不删除 State 历史，窗口限制轮数而非图片字节总量，也不保证所有图片都能装入预算。

State 与 checkpoint 保留消息身份、顺序、文字和工具调用关系，只保留视觉窗口内图片的 base64，终止时不额外清空窗口。执行沿用 `durability="exit"`，图退出时保存回收后的 State；进程突然崩溃时不保证中间 checkpoint 恢复。图片回收前后公开消息均投影为相同 Artifact ID，现有消息去重保证 SSE/历史事件不重复追加；持久事件、历史原图与 JSON/HTML 导出保持完整。内存与最终快照的图片载荷随窗口内截图体积变化，文字历史仍随任务累积。

开发调试重建约定：过期图片使用 `type="text"`、固定文本 `[Screenshot was captured; image is outside the visual history window.]` 和 `extras.artifact_id`，占据原图片内容块位置。通过该 ID 调用 `ArtifactStore.load_content(artifact_id)` 获取 `(content, mime_type)`，以 `base64.b64encode(content).decode("ascii")` 重建 `{"type": "image", "id": artifact_id, "mime_type": mime_type, "base64": encoded}` 内容块。消息 ID、内容块顺序及持久事件提供历史定位；原图丢失时应报告缺失，不伪造内容。重建仅用于开发人员调试，不自动回填运行 State，也不提供独立调试命令。

通过/失败必须引用本任务实际保存的截图；是否需要操作后重新取证由模型判断。总轮次或连续纠错预算耗尽统一 BLOCKED。连续尝试默认 3 次（包含首次），参数错误、模型响应校验错误、工具业务错误和超时共用预算，成功调用后清零；同时耗尽采用 cycle_limit。全局 fail-fast、取消和最终 PASS/FAIL/BLOCKED/CANCELLED 聚合由程序决定。

通用工具边界对取消、超时和未知结果不进行自动重放。会话清理结束前保持单运行占用；未收到的结果明确为未确认。不同外部 MCP 的取消响应能力由其实现决定，宿主不伪造已完成操作回执。

正常调用在同一次运行内复用会话。工具调用超时后等待本地调用与受影响会话清理，将操作可能部分完成、结果未确认的配对错误返回给模型。预算允许且清理成功时模型可调整参数、重新观察或结束；下一次使用该服务才通过 SDK 初始化新会话。初始化沿用工具超时配置，初始化本身失败直接 BLOCKED / TOOL_FAILED，不无限重连，不重写启动时目录快照，也不探活设备。清理抛出基础设施异常时也直接阻塞并保留诊断；不忽略 SDK 异常来强行重建。配置秒数限定调用等待，截图保存与结果投影不在该计时范围内，协作清理耗时另计。

工具执行使用单层 LangGraph 和标准 `ToolNode`。公开 `awrap_tool_call` 拦截器负责一次调用的等待超时、用户取消和协作清理，超时后尚未退出的 Task 保留引用以观察迟到异常；不再使用工具节点 `timeout`、`error_handler` 或兼容子图。只有等待到期且清理成功才允许有限纠错，工具自身抛出的 `TimeoutError` 仍作为执行失败阻塞。`after_tools` 统一保存证据、整理结构化内容、回收历史图片，然后才发布本轮工具消息；证据处理失败立即向 executor 传播，已保存原图不回滚，失败消息不作为可重试 ToolMessage 发布。

RunExecutor 在执行结束时确定并持久化唯一运行终态，正常返回即表示 TestRun 已完成。最终提交位于执行异常处理之外，写入失败直接传播，不再次报告或重试提交。RunService 随后关闭运行级 MCP 会话，清理失败只记录一次应用日志，不追加运行事件、不改变 verdict。资源清理期间仍保持单运行占用，清理失败或超时后入口继续锁定，但已完成运行的取消返回 false，应用关闭也不改写既有结果；SSE、历史和报告可在资源清理结束前呈现终态。MCP 正常返回的业务错误及参数错误通过工具消息传给模型和前端；executor 生命周期内的调用机制和证据处理故障通过 ExecutionErrorEvent 传到前端，并进入历史、报告和导出；异常文本不包含调用栈或局部变量。应用日志还用于 HTTP 通用 500 的服务端诊断，以及后台执行收尾/事件/终态写入失败或外部取消的最终兜底。若持久化通道失败，不伪造完成事件，后续启动沿用既有孤立运行恢复机制。

调用超时计入连续纠错预算。总决策轮数耗尽统一 BLOCKED / CYCLE_LIMIT，并在两种预算同时耗尽时优先采用该原因；仅工具连续纠错预算耗尽为 BLOCKED / TOOL_FAILED。断连、会话失效、重建失败和证据处理失败直接阻塞；取消不能覆盖调用机制或收尾故障。

RunService 用一个工作状态管理准备、执行和清理阶段；准备阶段尚无运行 ID。取消核对当前运行 ID，关闭则禁止新启动并等待清理。数据库创建已提交但启动调用方取消时，仍交给执行器生成取消终态。

图控制异常只在工具调用包装边界交回 LangGraph，不因产品取消而转换成工具失败。当前不支持运行中暂停、恢复或 drain，executor 不设置框架控制异常的特殊透传分支；逃逸出根图的异常按现有应用异常路径报告并收尾为 BLOCKED，根图中断后缺少任务完成结果也按同一路径处理。RunService 不识别框架控制异常，证据保存和内容投影不承担图控制职责。

## API 与数据升级

- `POST /api/test-cases/{id}/plans` 不需要请求体，规划不读取设备信息。
- `POST /api/runs` 接收 `test_plan_id` 和 `assumptions_confirmed: true`。
- 设备查询 API 和 device 字段已删除；运行保存一个 `execution_model` 和实际工具目录。
- 计划修订创建新版本，只允许 latest 启动；相同运行的事件只追加，重复导出创建新文件。
- `/api/runs/{id}/events` 和 `/stream` 提供同源历史与 SSE；`/api/artifacts/{id}` 下载证据。
- 执行协议为 `2`。开发期旧数据库、JSON、checkpoint 不兼容；旧数据目录应另行归档并使用新的空 `TEST_AGENT_DATA_DIR`，不执行数据迁移。

## 测试和静态检查

测试通过临时 mcp.json 启动 `mcp_servers/fake_mcp`，场景只声明标准工具、预期调用、响应、延迟和断连。通过 JSONL 调用记录观察进程及执行顺序，不访问外部服务内部对象，不复制 ATV 规则。

```powershell
$env:PYTHONUTF8 = "1"
cd backend
conda run -n llm-dev python -m pytest -q
conda run -n llm-dev python -m pyright
conda run -n llm-dev python -m pyright ../mcp_servers
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

Uvicorn 保持一个 worker，因为 RunService 的当前工作状态与 EventBus 是进程内对象。不要提交模型密钥、设备凭据、运行数据库、截图或导出。
