# Android TV 主观测试 Agent

基于 `spec3.md` 实现的 A+B 阶段验证系统。它把自然语言测试用例转换为可人工确认的任务计划，并通过截图—决策—动作循环控制 Android TV，保存证据、实时事件和确定性测试报告。

当前实现包括：

- 不可变 TestCase/PlanRevision/TestRun 快照；
- LangGraph 串行执行图、cycle 上限、全局 fail-fast 和带外取消；
- ADB、Fake、Replay DeviceController；
- OpenAI 兼容 RealLLMProvider 和确定性 ScriptedLLMProvider；
- SQLite WAL、Alembic、LangGraph SQLite checkpoint、幂等 StepEvent；
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

复制 `.env.example` 为 `.env`。默认 `LLM_MODE=scripted`，无需网络或密钥，`fake-tv` 始终可用。

真实模型模式：

```dotenv
LLM_MODE=real
LLM_BASE_URL=https://example.com/v1
LLM_API_KEY=...
LLM_MODEL=...
```

兼容端点必须支持：

- Chat Completions 图文输入；
- `response_format.type=json_schema`；
- strict JSON Schema 输出。

系统不会从非法自由文本中正则提取计划或动作。Structured Output 校验失败会重试两次，仍失败时规划返回错误，执行任务进入 BLOCKED。

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

首次建库或升级：

```powershell
cd backend
conda run -n llm-dev python -m alembic upgrade head
```

应用启动也会为全新开发目录创建当前表结构，但正式的结构版本以 Alembic 为准。

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

