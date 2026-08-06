# Android TV 主观测试 Agent

基于 `PRD.md` 与 `spec3.md` 实现的 A+B 阶段验证系统。它把自然语言测试用例转换为可人工确认的任务计划，并通过通用 ReAct Task Agent 控制或观察 Android TV，保存证据、实时事件和确定性测试报告。

当前实现包括：

- 不可变 TestCase/PlanRevision/TestRun 快照；
- PlanningGraph 与通用 TaskAgentGraph；Act/Judge 共享循环并通过 Policy 隔离能力；
- Task 级多轮观察—决策—单工具调用、cycle 上限、全局 fail-fast 和带外取消；
- ADB、Fake、Replay DeviceController；
- planning/act/judge 独立路由的多模型注册表、OpenAI 兼容 Provider 和确定性 Scripted Provider；
- SQLite WAL、Alembic、每 Task 独立的 LangGraph SQLite checkpoint、强类型幂等事件；
- FastAPI REST、可补拉 SSE、JSON/HTML 报告；
- React、TypeScript、Ant Design 管理界面。

PDF、自动崩溃恢复入口、Skills、MCP、React Flow 和多运行并发属于阶段 C，不在当前版本内。

## 环境

- Conda 环境：`llm-dev`（Python 3.12）
- Node.js 22 或更高
- 可选：Android SDK Platform Tools/ADB

所有后端命令都从 `backend` 目录执行：

```powershell
cd backend
conda run -n llm-dev python -m pip install -e ".[dev]"
conda run -n llm-dev python -m alembic upgrade head
conda run -n llm-dev python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

后端必须使用单个 Uvicorn worker；当前版本的 RunRegistry 和 SSE EventBus 是进程内组件。

前端：

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

浏览器打开 `http://localhost:5173`。Vite 会把 `/api` 和 `/health` 代理到 `http://localhost:8000`。

## 配置

复制 `.env.example` 为 `.env`。应用级配置使用与设备类型无关的 `TEST_AGENT_` 前缀；模型和设备适配器配置分别使用 `LLM_`、`ADB_` 等技术域前缀。默认模型列表只有一个 scripted profile，无需网络或密钥，`fake-tv` 始终可用。

真实模型模式：

```dotenv
LLM_PROFILES=[{"id":"planner","mode":"real","base_url":"https://example.com/v1","api_key":"...","model":"planner-model","timeout_seconds":60},{"id":"vision","mode":"real","base_url":"https://example.com/v1","api_key":"...","model":"vision-tool-model","timeout_seconds":60}]
TEST_AGENT_PLANNING_MODEL_ID=planner
TEST_AGENT_ACT_MODEL_ID=vision
TEST_AGENT_JUDGE_MODEL_ID=vision
```

兼容端点必须支持：

- Chat Completions 图文输入；
- `response_format.type=json_schema`；
- strict JSON Schema 输出；
- 标准 tool calling，并支持禁用并行 tool calls。

`LLM_PROFILES` 的顺序稳定，未显式配置活动路由时使用首项。系统不会从非法自由文本中正则提取计划、动作或终态：规划 Structured Output 最多调用三次；Task Agent 每轮必须调用一个工具，终态统一调用 `finish_task`，格式、未知工具或参数错误在当前 cycle 内最多重试三次。

注册真实电视：

```dotenv
ADB_PATH=adb
ADB_SERIAL=192.168.1.10:5555
```

系统启动时会同时提供 `fake-tv` 和配置的 ADB serial。

## 数据

默认运行数据位于：

```text
data/
  app.db
  checkpoints.db
  artifacts/{run_id}/
```

截图先写临时文件，再原子重命名并记录 Artifact。数据库只保存相对路径和元数据。历史导出不会覆盖，每次 JSON/HTML 导出都创建新 Artifact。

本次执行核心重构直接重写了 `0001` 基线，不提供旧数据库、checkpoint 或事件协议兼容。已有开发数据需要先停止后端，再显式删除 `data/app.db`、`data/checkpoints.db` 和不再需要的 `data/artifacts/`，随后重新建库；应用本身不会自动删除数据。

首次建库或重置后建库：

```powershell
cd backend
conda run -n llm-dev python -m alembic upgrade head
```

应用启动也会为全新开发目录创建当前表结构，但正式的结构版本以 Alembic 为准。

## 执行架构

`RunExecutor` 只加载不可变 `RunSnapshot`、顺序启动 Task、选择快照中的 act/judge 模型、执行全局 fail-fast，并按固定规则聚合最终结果。每个 Task 由一次 `TaskAgentGraph` 调用完整执行，cycle、截图、模型消息、业务事件与证据都归 Agent 图管理。

每个 cycle 强制执行“取消/cycle 守卫 → 最新完整截图 → 模型决策 → 恰好一个工具调用”。`finish_task` 由图截获为终态；其他工具交给 LangGraph `ToolNode`。成功工具调用后重新截图；schema、运行异常或超时耗尽则携带 ToolMessage 直接让模型修复调用，不重新观察或累计图片上下文。

DeviceProvider 通过 `DeviceCapabilities` 声明已经装饰好的 `ToolEntry(BaseTool, scopes)`。全局 `CatalogToolProvider` 在应用初始化时装载并分类全部工具，graph 通过共享的 domain `Activity` 精确选择 act/judge 工具。Catalog 不包装工具、不修改 docstring/schema、不执行重试或标准化结果；节点超时由 LangGraph `TimeoutPolicy + RetryPolicy` 独立处理。MCP 尚未接入，未来适配器产出相同 capability 声明即可进入目录。

PASS/FAIL 自动绑定终态决策所在 cycle 的最新截图。checkpoint 只保存消息与 Artifact 引用，不保存截图 bytes/base64。API、SSE、报告和前端消费同一组点分隔的强类型事件。

## 测试与构建

后端：

```powershell
cd backend
$env:PYTHONDONTWRITEBYTECODE = "1"
conda run -n llm-dev python -m pytest -q
```

当前受管工作区可能禁止创建 Python bytecode，因此测试命令显式关闭 `.pyc` 写入；项目也关闭了 pytest cache provider。

前端：

```powershell
cd frontend
npm.cmd test
npm.cmd run typecheck
npm.cmd run build
```

默认测试不访问网络、不需要模型密钥或真实电视。真实 ADB/模型 smoke 测试应只在显式配置对应环境变量时运行。

## 核心 API

完整 OpenAPI 文档在后端启动后的 `/docs`。

```text
POST   /api/test-cases
GET    /api/test-cases
GET    /api/test-cases/{id}
POST   /api/test-cases/{id}/plans
GET    /api/test-cases/{id}/plans
POST   /api/plan-revisions/{id}/revisions

POST   /api/runs
GET    /api/runs
GET    /api/runs/{id}
POST   /api/runs/{id}/cancel
GET    /api/runs/{id}/events?after={sequence}
GET    /api/runs/{id}/stream?after={sequence}

GET    /api/runs/{id}/report
POST   /api/runs/{id}/exports
GET    /api/artifacts/{id}
GET    /api/devices
GET    /api/devices/{id}/health
```

创建运行时 `confirmed_assumptions` 必须与计划中的 assumptions 完全匹配。已有 pending/running TestRun 时，新请求返回 HTTP 409。
