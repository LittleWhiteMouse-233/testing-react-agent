# 用户脚本

安装、启动和人工诊断工具集中在本目录。开发时使用的 API 契约生成工具留在后端和前端各自的 `scripts/` 中。

| 入口 | 用途 |
|---|---|
| [根目录 start.cmd](../start.cmd) | 基础自检后统一启动应用 |
| [install.cmd](install.cmd) | 增量安装前后端依赖并执行基础自检 |
| [check-dependencies.cmd](check-dependencies.cmd) | 离线、只读的基础依赖自检 |
| [start_app.py](start_app.py) | 管理数据库迁移、前后端进程和关闭流程 |
| [inspect-mcp.cmd](inspect-mcp.cmd) | 使用 MCP Inspector 手动检查配置中的服务和工具 |
| [check_llm_capabilities.py](check_llm_capabilities.py) | 独立检测模型的文字、图片、工具和结构化输出能力 |

命令示例从仓库根目录开始；明确写出 `cd` 的后续命令按切换后的目录执行。
工具使用当前选定的 Python 环境，不会自动选择或切换环境。批处理入口和应用启动器用于 Windows。
LLM 检测依赖随应用一并安装；各 MCP 服务的依赖仍由服务自行准备。

## 安装和启动

以下示例均从仓库根目录开始，使用当前环境的 `python`，不要求特定环境名称。

### 1. 环境准备

#### 运行环境

- Python 3.12 或 3.13：使用自己选择的环境管理方式创建和激活 Python 环境；若终端已自动激活目标环境，无需重复激活。
- 应用配置：按[宿主配置说明](../README.md#宿主配置)准备模型和 MCP 连接配置。
- Node.js/npm：当前锁定的 Vite 要求 Node.js `^20.19.0 || >=22.12.0`，确保 `node` 和 `npm.cmd` 在 PATH 中可用。

#### 依赖安装与快捷脚本

快捷安装（PowerShell 示例）：

```powershell
.\user_scripts\install.cmd
```

也可直接执行安装命令：

```powershell
$env:PYTHONUTF8 = "1"
python -m pip install -e "./backend[dev]"
npm.cmd --prefix frontend install --include=dev
```

后端安装开发依赖，前端包含 Vite 等开发依赖。PowerShell 示例使用 `npm.cmd`，避免 `npm.ps1` 的执行策略限制。

安装脚本先检查当前 Python 和 Node/npm，再始终执行两端增量安装，由包管理器判断需要更新的依赖；每端安装后复用基础自检。不清空环境或 `node_modules`，安装中止后可重新运行；editable 注册可能重新生成。失败时保留包管理器诊断并指出阶段。

`install.cmd` 仅安装应用依赖；MCP 服务请按[各 MCP 服务文档](../README.md#mcp-服务文档)单独准备。

#### 基础自检与依赖排查

```powershell
.\user_scripts\check-dependencies.cmd
# 也可只检查一端
.\user_scripts\check-dependencies.cmd backend
.\user_scripts\check-dependencies.cmd frontend
```

`check-dependencies.cmd` 只做离线、只读的基础自检：检查基础工具、后端项目安装记录与 Alembic/Uvicorn 导入、前端直接依赖与 Vite 入口。无参数检查两端并汇总，也支持 `backend` 或 `frontend`；退出码 0 为通过、1 为失败、2 为参数错误。它不加载应用，不验证模型配置、凭据、数据库、端口、MCP、电视或完整依赖文件，不承担保证应用可用的责任。启动检查失败不会自动安装，按提示补齐前置工具或运行 `user_scripts/install.cmd`。

需要排查当前 Python 环境的依赖冲突时，可单独运行：

```powershell
$env:PYTHONUTF8 = "1"
python -m pip check
```

此检查不作为应用启动门槛。安装和基础自检是短时批处理，手动中断它们时可能出现 Windows 的批处理确认。

### 2. 启动应用

完成依赖安装并激活目标 Python 环境后，在仓库根目录运行：

```powershell
.\start.cmd
```

`start.cmd` 不受 PowerShell 脚本执行策略限制，也不会初始化 Conda 或切换环境，使用当前 PATH 中的 `python`。

在 CMD 或 Miniconda/Anaconda Prompt 中使用 Conda 的示例：

```cmd
conda activate llm-dev
start.cmd
```

#### Conda 无法激活环境

PowerShell 中能运行 `conda` 不代表能使用 `conda activate`：激活环境需要加载 Conda 的 PowerShell 初始化代码。未执行过 `conda init powershell`、未加载终端配置，或执行策略禁止加载 `Conda.psm1` 时，激活可能失败；仅能找到 `conda.bat` 也不代表当前 PowerShell 已切换环境。可用以下命令检查实际使用的 Python 路径：

```powershell
python -c "import sys; print(sys.executable)"
```

无需修改 PowerShell 执行策略或在当前终端激活环境，只要能识别 `conda`，即可在 PowerShell 中执行：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev .\start.cmd
```

`conda run` 为本次启动选择环境，无需在当前 PowerShell 中执行 `conda activate`；`start.cmd` 仍会完成依赖自检并调用 Python 启动器。`llm-dev` 只是环境名示例，请替换成已安装项目依赖的环境。直接运行 `start.cmd` 本身不会自动切换到 `llm-dev`。

#### 启动后会执行的操作与停止方式

入口自动设置 Python UTF-8，沿用终端已经激活的 Python 环境，并依次执行：

1. 运行基础依赖自检，失败则停止，不自动安装依赖。
2. 调用 Python 启动器执行数据库迁移，迁移失败不会启动服务。
3. 启动后端和前端。打开 http://localhost:5173 使用应用，后端监听 `127.0.0.1:8000`（单 worker）。

日志以 `backend` / `frontend` 区分；任一服务意外退出时另一个也会停止。端口被占用时直接报错，不会自动切换端口。

首次 Ctrl+C 立即显示停止提示，停止 Vite 并请求后端正常关闭；服务关闭流程不需要等待 Y/N 确认。后端最多等待三秒让已有 HTTP 请求结束，随后中断仍打开的 SSE，进入应用关闭流程，取消运行、保存终态并清理 MCP。三秒不包含业务收尾时间，正常运行的 SSE 不受影响。

关闭时页面可能收不到最终取消事件，重启后可查询持久化结果；取消收尾故障仍按现有规则记录为 BLOCKED，已经完成的运行不会被改写。再次 Ctrl+C 会明确提示并强制结束本次启动的进程树，结果可能未保存。

启动器收到停止信号后，会在退出、控制权返回 CMD 前解释批处理确认。若随后出现“终止批处理操作吗(Y/N)?”，建议输入 `N` 并回车，让脚本完成目录恢复和退出码传递；`Y` 会跳过剩余脚本收尾。两者都不会重新启动服务，也不会改变此前的关闭结果。此询问来自 CMD，不代表应用清理成功，应以前面的关闭日志为准；`Services stopped.` 只表示启动器已等待服务进程退出。第二次 Ctrl+C 强制退出后也会显示选项说明，但不打印正常停止提示。依赖自检失败时启动器尚未运行，不显示此说明。

### 3. 直接调用 Python 启动器

在依赖已准备好的 Windows 环境中，也可直接运行应用级启动器：

```powershell
$env:PYTHONUTF8 = "1"
python user_scripts/start_app.py
```

它执行数据库迁移、启动两端，并提供相同的日志转发和 Ctrl+C 关闭流程。
基础依赖自检由 `start.cmd` 调用；需要自检时先运行 `user_scripts/check-dependencies.cmd`。
`--cmd-entry` 用于根目录 CMD 入口的中断确认说明，直接运行 Python 时无需填写。

### 4. 前后端分别用命令行启动

完成上述环境准备后，也可直接启动各服务。后端终端先激活自己准备的应用 Python 环境；以下两个终端均从仓库根目录开始：

```powershell
# 后端终端
$env:PYTHONUTF8 = "1"
cd backend
python -m alembic upgrade head
python -m uvicorn app.main:app --port 8000
```

```powershell
# 前端终端
cd frontend
npm.cmd run dev
```

分别启动时需自行执行数据库迁移，并在两个终端分别停止服务。

## MCP 服务调试

在仓库根目录运行：

```powershell
conda activate llm-dev
.\user_scripts\inspect-mcp.cmd
```

已经在 `user_scripts/` 目录时，运行 `.\inspect-mcp.cmd` 即可。先安装满足 MCP Inspector 当前要求的 Node.js；脚本通过 `npx.cmd` 启动 `@modelcontextprotocol/inspector@latest`，跟随 npm 的 latest 发布版本，首次运行或更新时需要联网下载。

脚本只读加载仓库中的 `mcp_servers/mcp.json`，在 Inspector 网页中选择服务并连接，再调用工具查看输入和返回结果。添加或修改服务入口仍然编辑 `mcp.json`，无需每次在网页中重新配置。修改后重启 Inspector。

脚本将工作目录切换到 `mcp_servers/`，使配置中的相对 `cwd`（例如 `atv_mcp`）从这里解析，退出后恢复调用目录。脚本设置 `PYTHONUTF8=1` 并继承终端环境；`command: "python"` 使用当前 PATH 中的 Python。服务依赖需提前安装在所选环境中；使用独立环境的服务可在 `mcp.json` 中指定解释器绝对路径。

Inspector 会建立自己的 MCP 会话；它用于手动调试，不显示 Agent 已有会话的调用。结束调试时在终端按 Ctrl+C。

配置参数说明见 [Inspector 官方文档](https://github.com/modelcontextprotocol/inspector/blob/main/docs/mcp-server-configuration.md)。

## LLM 供应商能力检测

`check_llm_capabilities.py` 是独立的供应商适配性检测工具，不加载应用配置或业务代码，
不调用 MCP 或真实设备。真实检测会产生供应商调用费用。

### 安装与运行

依赖统一由 `backend/pyproject.toml` 声明，包含 LangChain OpenAI/Anthropic 集成和 LangGraph，
不再需要单独的 requirements 文件。以下命令从仓库根目录运行：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python -m pip install -e "./backend[dev]"
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py --list
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py
```

未提供的模型名称、公共根地址、API Key 会逐项提示输入；API Key 隐藏输入。
也可使用 `--model`、`--base-url`、`--api-key` 提供参数。检测不读取应用的模型配置。
`--list` 仅列出协议和 case，不要求凭据、不发起请求。

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py --base-url https://provider.example --model your-model --protocol chat responses --case case2.2 case4.2 --timeout 60
```

`--protocol` 和 `--case` 均接受列表，也可重复提供；去重后按规格顺序执行。
不指定筛选时运行全部三个协议和 11 个 case。旧 `--scenario` 已替换为 `--case`。

`--base-url` 是公共根地址，不包含 `/v1`、具体接口路径、凭据或查询参数；
网关公共前缀可以保留，例如 `https://provider.example/gateway`。
三个协议使用同一模型和令牌，分别访问根地址下的 `/v1/chat/completions`、
`/v1/responses`、`/v1/messages`，认证由各 LangChain 集成处理。

默认每次模型调用超时 60 秒、最多 2048 个输出 token，`max_retries=0`。
初始化、绑定、调用中的 `UserWarning` 直接作为异常记录，不重试、不降级。
Responses 关闭远端存储和自动历史 ID 关联；脚本关闭 LangSmith tracing。

### 预制素材与 case

脚本顶部集中定义数字序列 `NUMBERS = [12, 7, 25]`、显示这些数字的 PNG Base64、
识别要求和处理要求。识别要求明确回复必须包含文本，并先简短说明收到的内容，
即便本轮需要调用工具，也要先完成说明再调用工具。
可以修改 `IMAGE_BASE64`、`IMAGE_MIME_TYPE`，Base64 可换行，
但不能带 `data:` 前缀。支持 PNG、JPEG、WebP、GIF 的 MIME。

素材按 case 需要校验。空序列、非数字或非有限数字、空或非法 Base64 等配置错误记为 ERROR，
不发送模型请求，继续检测其他 case。本地校验 Base64 编码及非空内容，图像解码和识别能力由供应商检测。
工具 A/B 分别返回数字之和加一、减一，模型可见工具描述不透露算法。

| Case ID | 检测内容 |
|---|---|
| `case1` | 图片识别；自动检查正文非空，内容正确性人工核验 |
| `case1a` | 图片替换成占位文字后，保留真实 AI 回复并询问数字总数 |
| `case2.1` | 系统提示词要求 JSON |
| `case2.2` | 系统格式提示词 + 原生 json_mode |
| `case2.3` | 原生 json_schema |
| `case3` | 要求工具 B |
| `case3a` | 要求同时进行 A、B 处理 |
| `case4.1` | 同次回复包含 JSON 正文和工具 B，格式由提示词要求 |
| `case4.2` | 同次回复包含 JSON 正文和工具 B，使用 json_mode |
| `case4.3` | 同次回复包含 JSON 正文和工具 B，使用 json_schema |
| `case5` | 以预制 AI 消息结尾；观察 AI Tail 行为，只作人工检查 |

JSON 结构为 `{"description":"内容说明","num":[12,7,25],"sum":44}`。
结构校验仅检查结构与类型，不评分数字识别或求和是否正确。框架解析通过后，另外检查整段正文是否为纯 JSON：
两端空白不影响 PASS；前后说明、Markdown 代码围栏等非纯 JSON 输出记为 WARNING。
结构或工具校验失败仍为 FAIL，格式提醒与结构失败统一保存在 JSON 校验项中。Case2.1/2.2/4.1/4.2 使用
`PydanticOutputParser.get_format_instructions()` 生成系统格式要求，与应用 planning 相同。
解析沿用框架行为，不自行修复或重写原始正文。

工具用例绑定 A、B，不限制调用数量、不强制工具选择；用真实 ToolNode 检查参数与工具执行结果。
工具结果只写记录，不再回传模型。Case4 系列分别记录 JSON、工具的校验结果；
仅有工具调用而没有 JSON 正文时，JSON 校验失败。

每个普通 case 只调用模型一次。Case1a 在同一协议内有成功 Case1 记录时复用对话，
替换图片并新增一次调用；没有 Case1 记录时自行完成两次调用，全部属于 Case1a，
不会补选或生成 Case1 结果。原 Case1 记录保持不变。

已有 Case1 失败记录时 Case1a 为 NA；已有 Case3 失败记录时 Case3a/Case4 系列为 NA。
未选择 Case3 时，Case3a/Case4 系列独立执行。记录只在本次运行、同一协议内生效，不读取旧报告。

当前 Anthropic 集成的 json_mode 会发出降级警告，Case2.2/4.2 因而直接记录 ERROR，
不会向供应商发送这个请求或替换为 Schema 模式。OpenAI Case4.2 使用工具绑定与 json_object 组合，
其中 Chat Completions 按 SDK 解析要求启用 strict 工具；Case4.3 使用原生 Schema 与工具的公开绑定接口。

### 输出与结果

终端显示每个 case 的开始状态与简述、结束状态、记录文件位置，以及结束时的总览。
失败项会附上校验项、错误类别与异常类型，例如 `FAIL [json: validation/OutputParserException]`；
HTTP 错误同时显示实际状态码，例如 `ERROR [http/BadRequestError (HTTP 400)]`。
完整错误原文仍保存在 JSON 中。
格式提醒会显示为 `WARNING [json: WARNING validation/JSONDecodeError]`。

| 状态 | 含义 |
|---|---|
| PASS | 通过该 case 的自动检查，不表示所有内容正确或供应商永久支持该能力 |
| WARNING | JSON 可通过框架结构校验，但正文不是纯 JSON，包含额外文本或格式标记 |
| FAIL | 模型输出未通过结构、非空正文或工具调用校验 |
| ERROR | 素材配置、客户端、HTTP、连接、超时或工具执行异常 |
| NA | 被同协议中已有的失败 case 记录跳过 |
| MANUAL | Case5 已返回，需查看原始记录人工判断 |

每次运行在脚本目录下生成一个 `<安全化模型名>_llm_check_<微秒时间戳>/` 目录，
按 `<protocol>/<case_id>.json` 保存各 case，包含失败和跳过的 case。
整个运行目录已加入 Git 忽略规则，可按运行集中查看或删除。

记录包含 case ID 与简述（`case_description`）、调用输入、原始 AIMessage、工具调用、
ToolNode 结果、校验结果、耗时和原始错误。简述来自注册表，作为本次运行的说明快照保存。
每次调用输入分别快照保存，因此 Case1a 的图片替换可复核。绑定失败时也会保留准备好的输入，
这不代表已经发送了 HTTP 请求。框架返回 AIMessage 前发生错误时，不伪造缺失的模型回复。

`checks` 中某项的空诊断列表表示该项通过，缺少该项表示未检查；最终状态从记录推导。
每条诊断使用 `ProbeDiagnostic` 保存，`severity` 为 `error` 或 `warning`，默认是 `error`。
JSON 结构校验和全文检查统一由 `validate_json_response` 完成，格式提醒写入 `checks["json"]`，
保留全文解码异常的类型与原文；异常和校验失败优先于 WARNING。
异常按类型与 HTTP 状态分类，保留异常类型、原文和实际状态码，不根据文案猜测供应商能力，
也不把输出解析错误写成 HTTP 拒绝。记录不序列化客户端、API Key 参数或认证头。

检测正常结束退出码为 0，即使有部分 FAIL/ERROR/WARNING；参数错误为 2，Ctrl+C/输入中断为 130。
记录文件写入失败时展示原始错误并以 1 退出，停止后续检测。

### 无网络验证

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python -m pytest -q backend/tests/test_llm_capabilities.py
conda run --no-capture-output -n llm-dev python -m pyright user_scripts/check_llm_capabilities.py backend/tests/test_llm_capabilities.py
```

测试按上述 case 契约设计，使用真实 LangChain 客户端、Pydantic 解析器与 ToolNode，
仅在 HTTP 传输边界提供模拟响应，不访问供应商、凭据、MCP 或真实设备。
消息断言检查角色、内容块类型和历史保留关系，不依赖提示词或图片占位符的具体文案。
