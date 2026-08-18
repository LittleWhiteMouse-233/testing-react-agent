# Android TV 主观测试 Agent

这是一个面向内部测试人员的 Android TV 主观测试系统：把自然语言用例转换为可人工确认的语义任务计划，再由截图驱动的 Task Agent 自适应执行或判定，最终生成可追溯的确定性结果和证据报告。

当前交付范围是 PRD P0/P1：一台设备对应一次运行、全局同时只执行一个 TestRun。PDF、通用 DAG、多运行调度、进程中断后的自动续跑、Skills/MCP 管理平台属于未来范围。

## 当前架构

核心事实沿一条单向链路流动：

```text
TestCase
  -> immutable TestPlan(version + ordered TestTask)
  -> TestRun(snapshot) + TaskRun
  -> LangChain Message
  -> RunMessage -> message.appended
  -> TestRunDetail -> TestRunReport -> JSON/HTML Artifact
```

- `backend/app/domain/planning/` 与 `domain/execution/` 保存两条 Agent 过程的信息家族；`domain/resources/` 保存 Device/LLM/Tool 的框架无关领域表示，根级保留 shared-kernel ID/activity 与异常。
- `backend/app/planning/` 与 `execution/` 分别拥有应用入口和 LangGraph 实现；execution 同时拥有后台编排与活跃运行取消生命周期。
- `backend/app/device/`、`llm/`、`tools/` 保存 provider、`BaseTool` 和瞬时截图 bytes 等集成对象。
- `backend/app/event_stream/` 保存 Message 投影、持久事件 writer 与进程内提交通知 bus。
- `backend/app/persistence/` 保存 SQLAlchemy Row、集中 JSON adapter、Row mapper 和查询/事务边界。
- `backend/app/artifacts.py` 与 `reporting.py` 分别拥有文件存储和报告组合/导出边界。
- `backend/app/api/` 只定义 command request、分页、错误及路由；同形且安全的 domain/read model 直接作为响应。
- `frontend/src/api/schema.d.ts` 由已提交的 OpenAPI 合同生成；`openapi-fetch` 与 `openapi-react-query` 约束页面的 method/path/params/body/response，页面保留 snake_case wire 字段。

结构治理、信息家族与整改证据详见 [review.DataStructureRefactoring.md](review.DataStructureRefactoring.md)。产品语义以 [PRD.md](PRD.md) 为准，运行设计以 [spec3.md](spec3.md) 为准。

## 环境与启动

- Conda 环境：`llm-dev`（Python 3.12）
- Node.js 22 或更高
- 可选：Android SDK Platform Tools / ADB

后端命令从 `backend` 执行。PowerShell 中应在第一次 `conda run` 前启用 UTF-8：

```powershell
cd backend
$env:PYTHONUTF8 = "1"
conda run -n llm-dev python -m pip install -e ".[dev]"
conda run -n llm-dev python -m alembic upgrade head
conda run -n llm-dev python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

后端必须使用单个 Uvicorn worker；ActiveRunRegistry 与 SSE EventBus 是进程内组件。

前端单独启动：

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

打开 `http://localhost:5173`。Vite 会把 `/api` 与 `/health` 代理到 `http://localhost:8000`。

默认前后端同源，不需要前端环境变量。若分开部署，复制 `frontend/.env.example` 为 `frontend/.env`，并把 `VITE_API_ORIGIN` 设置为后端 Origin（例如 `http://localhost:8000`）；该值不包含 `/api` 路径。

## 配置

复制 `.env.example` 为 `.env`。默认使用无需网络或凭据的 scripted model，并始终注册 `fake-tv`。

真实 OpenAI-compatible 模型配置示例：

```dotenv
LLM_PROFILES=[{"id":"planner","mode":"real","base_url":"https://example.com/v1","api_key":"...","model":"planner-model","timeout_seconds":60,"context_window_tokens":32768,"max_output_tokens":2048,"characters_per_token":1.5,"tokens_per_image":1024,"context_safety_margin_tokens":1024},{"id":"vision","mode":"real","base_url":"https://example.com/v1","api_key":"...","model":"vision-model","timeout_seconds":60,"context_window_tokens":131072,"max_output_tokens":4096,"characters_per_token":1.5,"tokens_per_image":1024,"context_safety_margin_tokens":2048}]
TEST_AGENT_PLANNING_MODEL_ID=planner
TEST_AGENT_ACT_MODEL_ID=vision
TEST_AGENT_JUDGE_MODEL_ID=vision
```

模型端点需要支持多模态输入、structured output 和标准 tool calling。每个 profile 必须按实际模型校准 context window、输出预留、中文字符/token 比例、单张图片预算和安全余量；Task Agent 使用 LangChain 公共近似计数并把工具 schema 纳入窗口裁剪。Task Agent 每次有效响应必须恰好包含一个 tool call；任务通过正常的 `finish_task` 调用结束。

注册真实电视：

```dotenv
ADB_PATH=adb
ADB_SERIAL=192.168.1.10:5555
```

重试配置分别控制幂等截图、模型调用异常和无效模型响应。可能改变设备状态且结果未知的工具不会被自动重试。

## 数据与不兼容基线

默认运行数据位于：

```text
data/
  app.db
  checkpoints.db
  artifacts/{owner_id}/{artifact_id}.{extension}
```

业务数据库只有七张表：`test_cases`、`test_plans`、`test_tasks`、`test_runs`、`task_runs`、`run_events`、`artifacts`。截图和导出 bytes 以本地文件为权威，数据库只保存受约束的 owner、类型、相对路径、大小和哈希。

本次重构直接重写了 Alembic `0001`。旧数据库、checkpoint、事件、截图和导出均不兼容，应用不会自动迁移或删除它们。需要重置时，先停止后端，人工移走或删除 `data/app.db`、`data/checkpoints.db` 与不再需要的 `data/artifacts/`，再执行：

```powershell
cd backend
$env:PYTHONUTF8 = "1"
conda run -n llm-dev python -m alembic upgrade head
```

## 执行与事件

TestPlan 不可变。人工编辑生成新的 `manual_revision` TestPlan 和全新的 TestTask ID；重新规划生成 `replanning` TestPlan。只有同一 TestCase 的 latest TestPlan 可以继续修订或创建 TestRun。

每个 Task Agent cycle 为：取消/上限守卫 → 截图 → 模型决策 → 校验 → 一个工具调用。截图 bytes 获取后，同时执行一次 base64 编码和一次 Artifact 文件写入。完整原始 Message 与 base64 保留在 Graph State/checkpoint 中；模型窗口仅通过公开 `filter_messages`/`trim_messages` 选择已有消息，不写占位符、不重读文件、不重复编码。

运行过程以 Message 为第一事实：

- LangGraph `astream(version="v2", stream_mode=["updates", "custom"])` 的 `updates.messages` 是 `message.appended` 的唯一来源。
- 公开 `RunMessage` 保留原文本、合法/无效 AI tool call 和 ToolMessage，只把图片 base64 投影为 Artifact ID。
- `custom` 只补充 Message 没有表达的校验失败和工具开始事实。
- 生命周期、错误、跳过和终态由少量补充事件表达，不复制 Task、Result、verdict 或设备事实。
- 不订阅 `messages` token mode，不定义、聚合、持久化或展示 token/chunk。

REST、SSE、在线报告和导出共同使用 `StoredRunEvent`。SSE 发送 `retry: 1000`、`id: sequence` 和 `data: StoredRunEvent`；首次连接用 `after` 补拉，浏览器重连自动发送 `Last-Event-ID`，后端取两者较大值。前端用 OpenAPI 生成的 Ajv standalone 校验器验证实时事件，普通 REST JSON 只使用静态生成类型。每个 Task checkpoint thread 使用 `task-run:{task_run_id}`；执行协议基线是 `"1"`。

## 核心 API

完整合同见运行中的 `/docs` 或 `frontend/src/api/openapi.json`。

```text
POST   /api/test-cases
GET    /api/test-cases
GET    /api/test-cases/{test_case_id}
POST   /api/test-cases/{test_case_id}/plans
GET    /api/test-cases/{test_case_id}/plans
POST   /api/test-plans/{test_plan_id}/revisions

POST   /api/runs
GET    /api/runs
GET    /api/runs/{test_run_id}
POST   /api/runs/{test_run_id}/cancel
GET    /api/runs/{test_run_id}/events?after={sequence}
GET    /api/runs/{test_run_id}/stream?after={sequence}
GET    /api/runs/{test_run_id}/report
POST   /api/runs/{test_run_id}/exports

GET    /api/artifacts/{artifact_id}
GET    /api/devices
GET    /api/devices/{device_id}
```

创建运行的 request 是 `test_plan_id + device_id + assumptions_confirmed: true`。nullable 字段始终显式输出 `null`；错误统一为 `{code, message}`。

## OpenAPI 与验证

后端合同变化后，必须先导出 OpenAPI，再生成前端类型与 SSE 校验器：

```powershell
cd backend
$env:PYTHONUTF8 = "1"
conda run -n llm-dev python scripts/export_openapi.py ../frontend/src/api/openapi.json
cd ../frontend
npm.cmd run generate:api
```

`generate:api` 会更新 `schema.d.ts` 和 `src/api/generated/validateStoredRunEvent.*`。`check:api` 只检查这些生成物是否与 `openapi.json` 同步，不会改写或自动修复文件；检查失败时重新执行上述导出、生成顺序。

本项目不配置 GitHub Actions 或远程合并门禁。提交前应在本地执行完整验收：

```powershell
cd backend
$env:PYTHONUTF8 = "1"
conda run -n llm-dev python -m pytest -q
conda run -n llm-dev python -m pyright
conda run -n llm-dev python -m alembic check

cd ../frontend
npm.cmd run check:api
npm.cmd test
npm.cmd run typecheck
npm.cmd run build
```

自动化测试默认不访问网络，不需要真实模型凭据或真实电视。
