# 安装、启停与诊断

本目录提供 TestPilot 的 Windows 安装和启动入口，以及独立的 MCP、模型服务诊断工具。产品操作和应用配置见[项目 README](../README.md)。

除明确切换目录的命令外，本文示例均从仓库根目录开始。批处理使用当前 PATH 中的 Python，不创建或自动切换环境。直接调用 Python 的示例使用已有 Conda 环境 `llm-dev`；使用其他环境时替换该名称。

## 安装应用

### 准备运行环境

| 环境 | 要求 |
|---|---|
| 操作系统 | Windows，统一安装与启动入口依赖 Windows 终端 |
| Python | 3.12 或 3.13，准备并选择好应用使用的环境 |
| Node.js | 20 系列至少 20.19.0，或版本不低于 22.12.0；使 `node` 和 `npm.cmd` 在 PATH 中可用 |
| 安装网络 | Python 和 npm 包源可访问；运行模型、工具时另需对应服务可达 |

PowerShell 中使用 `npm.cmd` 可以避开 `npm.ps1` 的脚本执行策略限制。

### 安装依赖

已激活应用 Python 环境时运行：

```powershell
.\user_scripts\install.cmd
```

脚本依次检查 Python、Node.js 和 npm，增量安装后端及其开发依赖、前端及其开发依赖，并在各阶段完成基础检查。安装失败会标出阶段并保留包管理器诊断；补齐问题后可以重新运行，不需要先清空环境或 `node_modules`。

需要明确指定 Conda 环境时，可通过 `conda run` 安装，无需先在 PowerShell 激活：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev .\user_scripts\install.cmd
```

安装只准备应用依赖。接下来完成[应用配置](../README.md#配置应用)，并按所选 [ATV](../mcp_servers/atv_mcp/README.md) 或 [Fake MCP](../mcp_servers/fake_mcp/README.md) 文档准备服务自身的环境。

需要逐条安装、排查包管理器输出时，可以使用安装脚本对应的命令：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python -m pip install -e "./backend[dev]"
npm.cmd --prefix frontend install --include=dev
```

### 基础依赖检查

```powershell
.\user_scripts\check-dependencies.cmd
```

只检查一端时，追加 `backend` 或 `frontend`，例如：

```powershell
.\user_scripts\check-dependencies.cmd backend
```

检查离线、只读，覆盖基础工具、后端安装记录和 Alembic/Uvicorn 导入、前端直接依赖与 Vite 入口。退出码为：`0` 通过、`1` 失败、`2` 参数错误。

通过只说明基础依赖可用。模型配置、凭据、数据库、端口、MCP 和电视需分别验证；检查不会加载应用或自动安装缺失包。排查当前 Python 环境的包冲突时，可另行执行：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python -m pip check
```

## 启动与停止

### 统一启动

完成依赖与应用配置，选择正确的 Python 环境后运行：

```powershell
.\start.cmd
```

PowerShell 无法使用 `conda activate`，或需要为本次启动明确选择环境时，改用：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev .\start.cmd
```

入口设置 Python UTF-8 输出，先运行基础检查，再执行数据库迁移，最后启动前后端。检查或迁移失败时不会继续启动服务。

| 服务 | 地址 |
|---|---|
| 应用页面 | http://localhost:5173 |
| 后端 | http://127.0.0.1:8000 |
| 后端健康检查 | http://127.0.0.1:8000/health |

启动日志用 `backend`、`frontend` 区分输出。应在日志显示两端已就绪后打开页面；启动器打印访问地址本身不等于服务已经可用。端口冲突会直接报错，不自动换端口；任一服务意外退出时，启动器也会停止配套服务。

保持一个应用实例，Uvicorn 使用一个 worker；活动运行和实时事件状态由该进程统一管理。

### 正常停止与强制停止

在启动终端第一次按 **Ctrl+C**，启动器立即提示停止，关闭前端并请求后端正常退出。后端最多等待 3 秒让已有 HTTP 请求结束，之后中断仍保持的实时连接，进入运行取消和保存流程。3 秒不包含业务收尾时间，应继续等待。

关闭时页面可能收不到最终通知，重启后以保存的运行记录为准。业务取消与故障的结果含义见[执行与结果](../README.md#执行与结果)。

再次按 Ctrl+C 会强制结束本次启动的进程树，结果可能尚未保存。`Services stopped.` 仅表示服务进程已退出，应结合前面的日志判断业务收尾是否成功。

通过 CMD 批处理启动时，控制权返回后可能出现“终止批处理操作吗(Y/N)?”。建议输入 `N` 并回车，使脚本完成目录恢复与退出码传递；`Y` 会跳过剩余批处理收尾。这个终端提示不表示应用清理成功，也不会重新启动服务。安装和基础检查被手动中断时也可能出现批处理确认。

### 其他启动方式

依赖已经准备好时，可以直接使用 Python 启动器：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python user_scripts/start_app.py
```

它执行迁移、启动两端并管理关闭，但不调用基础依赖检查。`--cmd-entry` 供根目录批处理使用，直接调用时无需填写。

调试前后端时也可以分别启动。以下两个终端均从仓库根目录开始，停止时分别按 Ctrl+C：

```powershell
# 后端终端：先完成数据库迁移
$env:PYTHONUTF8 = "1"
cd backend
conda run --no-capture-output -n llm-dev python -m alembic upgrade head
conda run --no-capture-output -n llm-dev python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --timeout-graceful-shutdown 3 --log-config logging.json
```

```powershell
# 前端终端
cd frontend
npm.cmd run dev -- --host localhost --port 5173 --strictPort
```

## 排查安装与启动

| 现象 | 检查与处理 |
|---|---|
| 找不到 Python，或提示后端未安装 | 确认实际选中的环境，在该环境重新运行安装入口 |
| PowerShell 能运行 Conda，但激活失败 | 使用上面的 `conda run` 方式；能找到 Conda 不等于当前环境已切换 |
| 前端依赖检查失败 | 查看 npm 输出与 Node.js 版本，重新运行安装入口 |
| 迁移失败或配置校验失败 | 查看终端中最先报告的错误，检查[本地配置](../README.md#配置应用)和实际使用的数据位置 |
| 端口被占用 | 停止占用相应端口的旧服务，再启动应用 |
| 基础检查通过，但创建运行失败 | 检查错误指向的准备阶段，并使用下面的 MCP 或模型诊断工具 |

需要确认 Conda 环境中的解释器时：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python -c "import sys; print(sys.executable)"
```

## 检查 MCP 服务

选好服务使用的 Python 环境，在仓库根目录运行：

```powershell
.\user_scripts\inspect-mcp.cmd
```

该入口通过 `npx.cmd` 启动 `@modelcontextprotocol/inspector@latest`，首次使用或版本更新时可能联网下载。在 Inspector 网页中选择服务、连接，再查看工具输入并手动调用。结束时在终端按 Ctrl+C。

脚本固定读取 [mcp_servers/mcp.json](../mcp_servers/mcp.json)，并将工作目录切换到 `mcp_servers/`，不会改读 `.env` 中自定义的连接文件。它继承终端环境并设置 Python UTF-8；连接文件的通用参数规则见[外部工具连接](../README.md#外部工具连接)。修改文件后重启 Inspector。

Inspector 建立独立会话，不展示 Agent 已有会话中的调用。手动执行的工具会作用于该服务的真实目标，操作前检查设备与参数。

## 检查模型服务

[check_llm_capabilities.py](check_llm_capabilities.py) 独立检测服务的图片、JSON 和工具能力，不读取应用配置，不调用 MCP 或电视。实际检测会向供应商发送请求并可能产生调用费用；只列出检测项不会发送请求。

### 运行与筛选

依赖随应用安装。先查看可用检测项：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py --list
```

执行检测时，模型名、公共根地址和 API Key 未通过参数提供的部分会逐项询问，密钥隐藏输入：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py
```

也可以选择部分协议和检测项：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py --base-url https://provider.example --model your-model --protocol chat responses --case case2.2 case4.2 --timeout 60
```

| 参数 | 含义 |
|---|---|
| `--base-url` | 公共根地址，不含 `/v1`、具体接口路径、凭据或查询参数；可保留网关前缀 |
| `--model` | 待检测的模型名 |
| `--api-key` | 显式提供密钥；也可省略并使用隐藏交互输入 |
| `--protocol` | `chat`、`responses`、`messages`，支持多值及重复参数 |
| `--case` | 下表中的检测项，支持多值及重复参数 |
| `--timeout` | 每次模型调用超时，默认 60 秒，必须为正的有限值 |
| `--list` | 只列出协议与检测项 |

未筛选时按固定顺序运行三个协议、每个协议 11 项。三个协议使用相同模型与密钥，分别访问根地址下的 `/v1/chat/completions`、`/v1/responses`、`/v1/messages`。检测地址的填写方式与[应用模型配置](../README.md#模型服务)的 API 基地址不同。

每次调用输出上限为 2048 token，不自动重试或降级；客户端 `UserWarning` 按异常记录。Responses 关闭远端存储和自动历史 ID 关联，检测过程关闭 LangSmith tracing。

### 检测内容

| 检测项 | 内容 |
|---|---|
| `case1` | 图片识别，自动检查非空正文，内容人工复核 |
| `case1a` | 用占位文字替换图片后，保留真实历史回复并继续询问 |
| `case2.1` | 用提示词要求 JSON |
| `case2.2` | 原生 JSON 模式 |
| `case2.3` | 原生 JSON Schema |
| `case3` | 请求工具 B |
| `case3a` | 请求同次回复调用工具 A、B |
| `case4.1` | 同次回复包含 JSON 正文和工具 B，格式由提示词要求 |
| `case4.2` | 同次回复包含 JSON 正文和工具 B，使用 JSON 模式 |
| `case4.3` | 同次回复包含 JSON 正文和工具 B，使用 JSON Schema |
| `case5` | 以预制 AI 回复结尾，观察续接行为，人工判断 |

默认素材为显示 `[12, 7, 25]` 的图片，JSON 结构包含 `description`、`num` 和 `sum`。自动检查不评分数字识别或计算是否正确，应查看原始回复人工核验。需要更换素材时，修改脚本顶部的数字序列、图片 Base64 和 MIME；支持 PNG、JPEG、WebP、GIF，Base64 不带 `data:` 前缀。

JSON 同时检查结构和完整正文：结构合格但附带说明或 Markdown 围栏时记为 WARNING；结构不合格仍为 FAIL。工具项检查调用及执行结果，结果只写记录，不再传回模型。

普通检测项只请求模型一次。`case1a` 复用同协议中成功的 `case1` 对话，再新增一次调用；未选择 `case1` 时自行完成所需的两次调用。同一轮检测、同协议中已有 `case1` 失败记录时跳过 `case1a`；已有 `case3` 失败记录时跳过 `case3a` 和 `case4` 系列。未选择前序项时，所选项独立执行。

### 读取结果

| 状态 | 解释 |
|---|---|
| PASS | 通过该项自动检查，内容正确性仍需相应复核 |
| WARNING | JSON 结构合格，但正文含额外文本或格式标记 |
| FAIL | 正文、结构或工具调用不符合该项要求 |
| ERROR | 配置、客户端、网络、HTTP、超时或工具执行异常 |
| NA | 被同协议中已有的失败前序项跳过 |
| MANUAL | 已获得回复，需要人工判断 |

终端显示检测项简述、状态、诊断摘要和文件位置，最后输出总览。每次检测在本目录生成独立的 `<安全化模型名>_llm_check_<微秒时间戳>/`，按 `<protocol>/<case_id>.json` 保存输入、原始回复、调用、工具结果、校验和错误，包括失败与跳过项。

记录不序列化 API Key 参数或认证头。框架未返回的模型回复保持缺失；准备好的输入不等于请求已经发出。HTTP、解析和连接错误分别记录，不根据错误文案推断服务能力。

检测正常完成的退出码为 `0`，即使存在 FAIL、ERROR 或 WARNING；参数错误为 `2`，人工中断为 `130`，记录写入或隐藏输入等本地 I/O 失败为 `1`。记录写入失败会停止后续检测。运行记录默认由 Git 忽略，可按整次检测目录整理或删除。

诊断覆盖的能力应结合[应用实际模型要求](../README.md#模型服务)理解；通过额外协议或组合检测，不代表应用使用了该协议或调用组合。
