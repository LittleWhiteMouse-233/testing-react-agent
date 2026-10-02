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
应用依赖、LLM 检测工具的专用依赖、各 MCP 服务的依赖分别准备，安装应用不会安装后两者。

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

`check_llm_capabilities.py` 是独立的人工检测工具，不加载应用配置、不调用 MCP、
不使用真实设备，也不生成报告文件。真实请求会产生供应商调用费用。

### 安装与运行

以下示例使用 Conda 环境 `llm-dev`，均从仓库根目录运行；可替换成自己的工具环境：

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python -m pip install -r user_scripts/llm-capabilities-requirements.txt
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py --list
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py --base-url https://provider.example --model your-model
```

脚本专用依赖文件锁定了验证版本；安装会同步更新当前环境中的这些包，
不会修改应用的依赖声明。未安装 `langchain-anthropic` 时，Messages 检测会标明客户端缺失，
其他协议仍可运行。

运行时隐藏输入令牌。`--base-url` 是公共接口根地址，不包含 `/v1`、具体接口路径或查询参数；
网关公共前缀可以保留，例如 `https://provider.example/gateway`。
三个协议使用同一模型和令牌，分别访问根地址下的 `/v1/chat/completions`、
`/v1/responses`、`/v1/messages`。认证由各 LangChain 集成按其协议处理，
不自动尝试其他认证方式。网关不支持某一接口、模型名称或认证时，终端保留实际错误。

可筛选协议和场景，每个参数可以重复：

```powershell
conda run --no-capture-output -n llm-dev python user_scripts/check_llm_capabilities.py --base-url https://provider.example --model your-model --protocol chat --protocol responses --scenario tools_json --scenario tools_schema --timeout 60
```

协议名称为 `chat`、`responses`、`messages`。默认遍历全部协议和场景，
每次请求上限 2048 个输出 token、超时 60 秒，无自动重试。中途按 Ctrl+C 停止。
脚本主动关闭 LangSmith tracing，避免使用当前环境的 tracing 配置发送检测内容。

### 图片配置与输入

直接编辑脚本顶部的 `IMAGE_BASE64` 和 `IMAGE_MIME_TYPE`，例如：

```python
IMAGE_BASE64 = """这里粘贴数字图片的原始 Base64，可换行"""
IMAGE_MIME_TYPE = "image/png"
```

不要包含 `data:image/...;base64,` 前缀。支持 PNG、JPEG、WebP、GIF 的 MIME。
脚本仅验证 Base64 与 MIME 配置，图片能否被模型解码和识别由真实请求检测。
图片未填或配置无效时跳过图片组，纯文本组始终使用 `[12, 7, 25]`。
图片场景要求按从上到下、从左到右提取数字。

内置工具 `X(numbers)` 返回数字总和加 1，`Y(numbers)` 返回数字总和减 1。
纯文本组的结果分别为 45 和 43。提示词及模型可见工具描述不泄露算法。
模型默认自主选择工具；调用记录才是是否使用 X/Y 的依据，最终数值仅辅助核对。

### 场景与输出解读

| 场景 | 检测内容 |
|---|---|
| `understanding` | 描述输入、提取数字，无工具 |
| `prompt_json` | 仅提示词要求 JSON，正文按 Schema 检查 |
| `json_mode` | 原生 JSON 模式 |
| `json_schema` | 原生 JSON Schema，明确指定方法 |
| `tools` | 提供 X/Y，要求进行 X 处理并说明理解与依据 |
| `tools_json` | 原生工具 + JSON 模式正文 |
| `tools_schema` | 原生工具 + JSON Schema 正文 |
| `multi_tools` | 同轮进行 X 和 Y 处理 |
| `assistant_tail` | 无调用的非空 AI 回复放在历史末尾，再次请求 |
| `image_retention` | 删除历史图片、保留实际 AI 文字，再要求 X 处理，仅图片组 |

除图片回收外，每个场景均有纯文本和图片两组。Messages 没有独立 `json_object` 模式，
相应场景标明不支持，不自动改成 Schema。框架发出可能导致模式变化的 UserWarning 时停止该场景，
不会把回退后的结果记为原模式成功。

Chat Completions 的 `tools_json` 因 LangChain 所用解析接口的要求，必须绑定 `strict=True`
工具；Responses 的同一场景不要求它。两种 OpenAI 协议的 `tools_schema` 都启用 strict。
这些设置会在终端打印，比较时应同时考虑正文格式和工具 Schema 的差异。

工具场景最多两次模型调用：第一轮调用本地工具、第二轮观察结果使用。
没有调用时直接记录；第二轮仍要求调用时只展示，不继续执行。
图片回收最多三次调用（先观察，再处理，再使用工具结果）；每次都显式发送历史，
Responses 关闭存储和自动历史 ID 关联。若前置观察没有非空正文则跳过后续，绝不补写观察。

终端展示输入文字和角色、实际方法、原始内容块、工具参数和结果、框架解析结果、
正文格式检查、响应元信息、token 用量及耗时。图片只显示 MIME 和字节数；
令牌及配置的图片 Base64 会脱敏。格式完整不代表视觉理解或理由正确。
无正文但有工具调用会正常执行，不要求补写说明。

接口错误带 HTTP 状态；没有 HTTP 状态的异常单独标记，不能直接推断为供应商拒绝。
单场景失败不会终止其他检测。末尾汇总仅描述这次观察，不推断永久能力。
正常完成检测返回退出码 0（即使部分探测失败），命令行参数错误为 2，中断为 130。

框架依据：[ChatOpenAI structured output](https://reference.langchain.com/python/langchain-openai/chat_models/base/BaseChatOpenAI/with_structured_output)、
[ChatAnthropic](https://reference.langchain.com/python/langchain-anthropic)、
[Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)。

### 无网络验证

```powershell
$env:PYTHONUTF8 = "1"
conda run --no-capture-output -n llm-dev python -m pytest -q backend/tests/test_llm_capabilities.py
conda run --no-capture-output -n llm-dev python -m pyright user_scripts/check_llm_capabilities.py backend/tests/test_llm_capabilities.py
```

测试在 HTTP 传输边界返回模拟响应，仍使用真实 LangChain 集成进行序列化、解析及工具执行。
它们验证脚本行为，不代替真实模型检测。
