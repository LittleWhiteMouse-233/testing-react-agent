# Subjective testing agent

自然语言用例 → 人工确认的不可变计划 → MCP 工具驱动的自适应执行与观察 → 确定性结果和报告。

## 用例编辑与归档

规划页“编辑用例”同时修改名称和描述，一次保存，编号不变；两者不可为空，名称最多 200 字符。保存不会修改已有计划或报告，需重新确认计划后才能启动；重新规划读取最新用例内容。右侧“任务序列”提供计划版本选择，历史版本只读，不能修订、重新规划或执行。

用例卡片的橙黄色收纳图标用于归档。归档后规划页及直接规划 API 不再提供该用例，历史运行、筛选、统计、截图和报告导出仍然保留。历史列表显示当前用例名称，报告原始用例来自运行绑定计划的快照；归档用例不能重跑，本次不提供恢复入口。

生成计划期间不能修改或归档用例。点击运行、请求被服务端接受后即占用用例，连接 MCP、执行及资源释放期间均不能归档；启动失败并安全清理后解除占用。有运行时仍可修改用例内容，不影响该运行的冻结计划。

`backend/app/test_case_lock.py` 是单 worker 下的统一用例同步边界。归档在数据库写入前立即占用用例，期间其他直接访问、编辑、规划和运行请求返回 `409 test_case_busy`；完成、失败或取消后释放。归档提交后由数据库标志阻止规划访问，历史查询始终可用。

新增接口：`PATCH /api/test-cases/{id}` 接收 `{name, source_text}`；`POST /api/test-cases/{id}/archive` 执行归档；`GET /api/runs/test-cases` 提供包含已归档用例的历史筛选。主用例列表始终只查询未归档记录。

开发阶段使用新的数据库初始化定义，不迁移旧运行数据。清空 `data` 由开发者自行执行，应用不会主动删除历史数据。

## 结构和边界

- `backend/app/domain/`：用例、计划、运行、工具目录、模型快照和事件的领域事实。
- `backend/app/planning/`：无工具的规划流程，读取原始用例、最新计划与本轮额外输入；`execution/`：Act/Judge 共用执行图。
- `backend/app/mcp.py`：标准 stdio MCP 配置、工作目录和框架客户端；`tools.py` 通过官方适配器导入业务工具，`skill.py` 统一管理 Skill 与静态资源。
- `backend/app/event_stream/`：共享有界 EventBus；执行事件经 RunEventWriter 提交后通知，规划事件仅在连接中发送。慢订阅队列溢出会断开，执行流可从数据库补拉，规划流不保留历史。
- `frontend/`：计划编辑、实时运行、历史和报告。
- `backend/scripts/` 和 `frontend/scripts/`：开发用契约生成工具。
- `user_scripts/`：安装、启动和独立能力检测工具，见[用户脚本文档](user_scripts/README.md)。
- `mcp_servers/`：独立 MCP 服务，使用说明见下方[服务文档](#mcp-服务文档)。
- `mcp_servers/mcp.json`：宿主连接配置；`data/`：忽略的运行数据库、checkpoint、运行记录和导出。

宿主及其测试不 import 外部包，不管理设备 ID、状态、探活或配置。第三方工具只需通过 MCP 提供 schema 和结果；不需要实现项目专属的 scopes、manifest 或设备接口。

MCP 服务独立管理运行环境、依赖和配置；应用仅负责协议接入。安装和使用方式见各服务文档。

## 启动应用

完成依赖安装和[宿主配置](#宿主配置)，并激活目标 Python 环境后，在仓库根目录运行统一启动入口：

```powershell
.\start.cmd
```

启动后打开 http://localhost:5173 使用应用。环境准备、安装与依赖自检、其他启动方式、停止与排障，以及 MCP/LLM 诊断工具的详细说明见[用户脚本文档](user_scripts/README.md)。

## 宿主配置

首次使用时，复制 `.env.example` 为 `.env`，并复制 `models.example.toml` 为 `models.toml`。在 `models.toml` 中配置各模型的连接、API key 和参数，在 `.env` 中选择规划与执行模型并配置 MCP。模型配置变更后需重启。

默认 scripted 模型只用于测试；没有显式执行场景时返回 blocked，不会猜测工具名称或伪造截图。

| 配置 | 默认值 / 作用 |
|---|---|
| `TEST_AGENT_MODEL_CONFIG_PATH` | 默认 `./models.toml`；模型配置文件，相对路径从仓库根目录解析 |
| `TEST_AGENT_PLANNING_MODEL_ID` | 规划模型 ID，未设或留空时使用首个 profile |
| `TEST_AGENT_EXECUTION_MODEL_ID` | Act/Judge 共用模型 ID，未设或留空时使用首个 profile |
| `TEST_AGENT_MCP_CONFIG_PATH` | 默认 `mcp_servers/mcp.json`，相对路径从仓库根目录解析 |
| `TEST_AGENT_TOOL_CALL_TIMEOUT_SECONDS` | 120 秒，完整运行准备过程（加载 MCP、发现工具、发现资源）及每次工具节点执行的超时；节点执行包含会话创建、调用与退出，图片保存不计入 |
| `TEST_AGENT_IMAGE_HISTORY_ROUNDS` | 3，State 保留原图 base64 的最近有图片决策轮数；过期图片替换为 artifact 引用 |
| `TEST_AGENT_EXECUTION_MODEL_CALL_MAX_ATTEMPTS` | 3，执行阶段模型传输错误有限重试 |
| `TEST_AGENT_EXECUTION_MODEL_RESPONSE_MAX_ATTEMPTS` | 3，执行阶段连续无效响应/工具错误上限 |
| `TEST_AGENT_ACT_MAX_CYCLES` | 30，act 任务总决策轮次上限，范围 1～100 |
| `TEST_AGENT_JUDGE_MAX_CYCLES` | 10，judge 任务总决策轮次上限，范围 1～100 |

模型参数统一放在 `models.toml` 的有序 `[[profiles]]` 中，每个 profile 独立填写 `api_key`、`base_url`、`model`、`mode` 和所需预算参数；省略的参数采用代码默认值，完整示例见 `models.example.toml`。真实模型必须设置 `mode = "real"`，并按服务实际能力填写上下文窗口与输出上限。

规划模型需支持 JSON 模式（`response_format.type = "json_object"`）。宿主通过 LangChain `json_mode` 请求 JSON，在系统提示中提供计划 schema，并由 Pydantic 校验结果；不要求服务支持原生 `json_schema` 严格输出。无效计划仍最多尝试三次，持续无效时不保存计划版本。

`.env` 不再使用 `LLM_PROFILES`，模型选择示例：

```dotenv
TEST_AGENT_MODEL_CONFIG_PATH=./models.toml
TEST_AGENT_PLANNING_MODEL_ID=planner
TEST_AGENT_EXECUTION_MODEL_ID=vision
```

两个 ID 必须存在于模型文件中；Act/Judge 共用 execution 模型。配置在启动时读取，修改后重启后端。文件缺失、TOML 格式错误、空列表、重复 ID、未知字段或无效预算会阻止启动。`models.toml` 已加入 Git 忽略；真实密钥只写入本地文件，提交示例文件时保留占位符。

旧的 `ADB_*`、Act/Judge 模型路由和设备操作参数已移除。模型上下文预算仍由 profile 的 context window、输出保留、图片 token 估计和安全余量共同决定。

`mcp_servers/mcp.json` 使用 `mcpServers`，每个连接支持 `command`、`args`、`env`、`cwd`，首版仅 stdio。`cwd` 相对于配置文件解析；命令直接启动，不经过 shell。MCP SDK 继承其标准进程环境，再应用显式 `env`。不同服务的工具加服务名前缀。

按服务文档设置启动命令和工作目录。`command: "python"` 使用启动进程 PATH 中的 Python；通过 `start.cmd` 启动时继承终端已激活或 `conda run` 为本次启动选择的环境。服务使用独立 Python 环境时，将 `command` 设为该环境解释器的绝对路径。

宿主通过官方适配器发现工具，适配器和 MCP SDK 为每次工具调用创建、关闭会话；stdio 服务会随调用重新启动，因此服务不应依赖跨调用的进程内状态。运行开始冻结连接配置和实际工具目录，运行中不热加载。Planning 和历史查询不连接 MCP；配置/连接/发现失败时创建运行接口返回 502。只启用此次运行需要的服务。

## MCP 服务文档

- [ATV MCP](mcp_servers/atv_mcp/README.md)
- [Fake MCP](mcp_servers/fake_mcp/README.md)

## 执行、运行记录和历史

执行协议版本为 8。用户取消通过运行取消标志在节点安全边界生效：等待当前调用返回或达到框架超时，保存已收到的图片后结束为 CANCELLED，不再请求模型或执行下一次工具调用。调用机制、结果处理或图片保存的未处理故障仍为 BLOCKED，不被取消标志覆盖。应用关闭请求同样的安全边界停止，并等待当前运行结束。

工具参数错误和 MCP 正常返回的业务错误通过框架错误消息交给模型；原生工具节点超时由公开错误处理器转换为关联原调用 ID 的错误消息，提示操作可能部分执行、结果未确认。随后沿用纠错预算，由模型决定重新观察、继续或结束；工具不自动重放。会话退出交由框架与 SDK 处理，宿主不额外等待独立清理流程；迟到结果不进入 State、事件或运行记录。模型传输错误仍有限重试，不另计决策轮。未处理异常向上传播到 executor，记录执行错误并结束为 BLOCKED。运行工作任务结束后释放占用，不设置永久入口锁定；已保存的终态不被后续取消改写。

Act 达成目标，Judge 验证行为；Judge 可主动准备条件和触发交互，但不得绕过待验证行为或修改成功标准。两者共用模型、工具和执行图。

图没有固定截图节点。逻辑决策轮开始即消耗一轮，包括能力查询、截图、纠错和结束请求；首次模型请求前在同一事务中保存任务轮数与 `cycle.started`。传输重试不另计，请求最终失败仍保留已开始的轮次。开始前取消或已到轮次上限不增加轮次；开始事件提交期间取消则保留已提交轮次，不再请求模型。开始事件写入失败立即阻塞，不调用模型。每轮只允许一个工具调用，终态使用宿主的 `finish_task(status, summary, memory=None)`。

`memory` 可保留供同 Run 后续任务使用的实际观察值、对象和位置、字段单位及图片用途和 artifact ID；缺省、null、空字符串和纯空白统一为无记忆，其余内容保留原文。规划在采集任务目标和成功标准中明确保留要求，在消费任务中明确来源、比较字段与一致性规则。执行器只将已保存且含记忆的前序 TaskRun 投影为 `memory_task_runs`（来源任务 ID 和 `result.memory`）供全部后续任务使用，不传完整消息。新 Run 从空记忆开始；必要观察不足时由 Agent 结束为 BLOCKED。

每张工具图片之前会插入 `[Image artifact <artifact_id>]` 文字，供模型记忆和调用内置 `read_image_artifact(artifact_id)`。读取工具根据当前 TaskRun 查询所属 Run，仅允许读取同 Run 的图片原文件；业务检查失败交给模型纠正，数据库或文件故障沿用执行异常路径。成功重读保留原 ID 和 TaskRun 归属、不重复保存，其余处理与普通图片相同。重读是历史图片，参考图仍是参考图，不等同于当前目标的新观察。窗口过期后仍可按 ID 重读，所有调用沿用轮次和 token 预算。

运行页、在线报告和 HTML 导出在来源任务结果中展示“跨任务记忆”，保留原文换行并转义；JSON 保存完整结果。运行页沿用 `task.finished` SSE 通知触发详情查询，从 `detail.task_runs[].result.memory` 更新内容；`events_after` 只限制事件，不限制任务结果。

工具内容顺序保留。图片先解码保存原图，原 base64 连同 Artifact ID 进入 ToolMessage，不重新编码。工具完成后，在 `after_tools` 阶段选取最近 3 个有图片的已完成决策轮，同轮多图全部保留，无图轮不占窗口。窗口外图片在 State 原内容位置替换为带artifact 引用的占位文本；仅复制变化的消息，通过公开 `add_messages` reducer 按相同 message ID 替换，不直接修改输入 State。该产品边界只实现图片轮数策略，消息合并及 checkpoint 序列化仍由框架负责。模型请求基于回收后的 State 执行 token 预算与必要上下文检查；token 裁剪不删除 State 历史，窗口限制轮数而非图片字节总量，也不保证所有图片都能装入预算。

`TaskAgentContext` 仅包含当前任务、TaskRun ID、前序任务结果和运行快照中的轮次上限。`TaskAgentInput` 嵌套该上下文，另外携带外部工具、运行快照中的完整资源目录及模型 profile ID。Factory 按该 ID 从共享的 ModelProvider 取得客户端，将外部工具与 `finish_task` 整合，并通过 `top_level_skills` 筛选初始可见的 Skill；Runtime 接收整合后的工具和筛选后的 Skill 列表，不持有完整 Factory 输入或资源目录。相同的完整工具集合供 Runtime、ToolNode 和模型上下文预算使用。

应用装配直接实例化 `PromptCatalog()`，由初始化方法按 `AgentActivity` 和 `TestTaskType` 的枚举值加载同名 `.txt` 文件，以名称索引提示词定义；`prompt_directory` 参数可指定目录，默认使用提示词模块所在目录。`prompt_for_activity` 选择活动提示词（规划使用 `planning.txt`）；`prompt_for_task_type` 优先选择任务类型专用提示词，缺失时回退到 `execution.txt`，两者不拼接。文件不存在时对应定义为 `None`，消费者使用空文本；文件存在但内容为空时仍有定义，并覆盖通用 execution 提示词。其它读取或编码错误正常报错。`PromptDefinition.fingerprint` 是文件字节内容的 SHA-256 前 12 位十六进制指纹，缺失文件不生成指纹。

`TaskAgentFactory` 和 `RunService` 共用同一个 catalog，分别用于执行和通过 `prompt_fingerprints_by_task_type` 冻结实际选择的指纹；回退时记录 execution 的指纹，最终没有提示词时记录 `null`。`PlanningGraph` 接收 catalog 选出的 planning 提示词，规划快照以 `planning_prompt_fingerprint` 保存其指纹或 `null`。运行期间不热更新 catalog 或替换模型客户端，同源的模型配置和提示词指纹无需在任务构建时重复比较，完整模型快照和提示词指纹继续保存在运行环境中供追溯。

`backend/app/execution/model_context.py` 并列提供 `ModelContextWindow` 和 `ModelImageWindow`。`TaskAgentFactory` 为每个任务分别构造并注入运行时；两个对象仅绑定各自的策略参数，每次接收当前消息计算结果，不保存消息历史。`ModelImageWindow` 的图片窗口轮数由应用配置在装配 Factory 时注入，不进入任务输入或运行环境快照；它在 `after_tools` 中生成过期图片的同 ID 替换消息，State 合并仍由 reducer 负责。`ModelContextWindow` 绑定所选客户端的模型快照及与模型和 ToolNode 共用的完整工具集合（含 `finish_task`），在请求模型前负责 token 估算、输入预算判断与消息选择。输入预算为模型窗口减去输出预留与安全余量；必要任务、Skill、最后一条消息或工具调用配对无法保留时抛出预算异常，由执行器按既有规则结束为 BLOCKED。普通历史图片可随 token 预算从模型请求中裁掉，不额外强制保留最后一条含图消息；请求裁剪不修改 State、已保存原图和历史记录。

State 与 checkpoint 保留消息身份、顺序、文字和工具调用关系，只保留图片窗口内图片的 base64，终止时不额外清空窗口。执行沿用 `durability="exit"`，图退出时保存回收后的 State；进程突然崩溃时不保证中间 checkpoint 恢复。图片回收前后公开消息均投影为相同 Artifact ID，现有消息去重保证 SSE/历史事件不重复追加；持久事件、历史原图与 JSON/HTML 导出保持完整。内存与最终快照的图片载荷随窗口内图片体积变化，文字历史仍随任务累积。

开发调试重建约定：过期图片使用 `type="text"`、固定文本 `[Image was saved; image is outside the image history window.]` 和 `extras.artifact_id`，占据原图片内容块位置。通过该 ID 调用 `ArtifactStore.load_content(artifact_id)` 获取 `(content, mime_type)`，以 `base64.b64encode(content).decode("ascii")` 重建 `{"type": "image", "id": artifact_id, "mime_type": mime_type, "base64": encoded}` 内容块。消息 ID、内容块顺序及持久事件提供历史定位；原图丢失时应报告缺失，不伪造内容。重建仅用于开发人员调试，不自动回填运行 State，也不提供独立调试命令。

任务总轮次仅由 `.env` 中的 `TEST_AGENT_ACT_MAX_CYCLES` 和 `TEST_AGENT_JUDGE_MAX_CYCLES` 按类型统一提供；规划生成和人工编辑均不提供轮次设置。修改配置后重启后端，对新运行生效。创建运行时冻结上限到运行环境快照，执行、运行页和报告均使用该快照。

图片和文本均为普通运行数据，结束任务不要求截图或关联图片。模型主动调用 `finish_task(status, summary, memory=None)` 时，无论 passed、failed 或 blocked，摘要都须说明支持该结论的观察内容、判断依据及其与成功标准的关系；提示词要求观察尽可能来自真实截图、日志和待测物状态，参考示例不能当作实际观察。程序验证状态及非空摘要，不根据图片数量决定结论。总轮次或连续纠错预算耗尽统一 BLOCKED。连续尝试默认 3 次（包含首次），参数错误、模型响应校验错误、工具业务错误和超时共用预算，成功调用后清零；同时耗尽采用 cycle_limit。全局 fail-fast、取消和最终 PASS/FAIL/BLOCKED/CANCELLED 聚合由程序决定。

工具执行使用单层 LangGraph 和默认 `ToolNode`；节点 `timeout` 覆盖适配器连接、调用和会话退出，仅处理 `NodeTimeoutError` 的公开 `error_handler` 提供超时反馈。工具自身抛出的 `TimeoutError` 等未处理异常仍作为执行失败阻塞。图片保存与结果投影位于独立的 `after_tools` 节点，不计入调用超时；该节点统一保存运行记录、整理结构化内容、回收历史图片，然后才发布本轮工具消息。图片处理失败立即向 executor 传播，已保存原图不回滚，失败消息不作为可纠错 ToolMessage 发布。

执行器仅消费图的 `updates` 流，投影并持久化公开 Message。executor 每次构建 TaskAgent 时注入独立的 `TaskAgentEventRecorder`，由节点等待其记录轮次开始、模型校验失败与工具开始事件。记录对象统一绑定运行和任务归属，复用既有事件事务、去重及提交后广播，不修改框架内部实现。

RunExecutor 在执行结束时确定并持久化唯一运行终态，正常返回即表示 TestRun 已完成。最终提交位于执行异常处理之外，写入失败直接传播，不再次报告或重试提交。MCP 正常返回的业务错误及参数错误通过工具消息传给模型和前端；executor 生命周期内的调用机制和图片处理故障通过 ExecutionErrorEvent 传到前端，并进入历史、报告和导出；异常文本不包含调用栈或局部变量。应用日志还用于 HTTP 通用 500 的服务端诊断，以及后台事件/终态写入失败或外部取消的最终兜底。若持久化通道失败，不伪造完成事件，后续启动沿用既有孤立运行恢复机制。

调用超时计入连续纠错预算。总决策轮数耗尽统一 BLOCKED / CYCLE_LIMIT，并在两种预算同时耗尽时优先采用该原因；仅工具连续纠错预算耗尽为 BLOCKED / TOOL_FAILED。断连等未处理通信异常和图片处理失败直接阻塞；取消不能覆盖实际执行或图片存储故障。

RunService 用一个工作状态管理准备与执行阶段；准备阶段尚无运行 ID。容器级 MCPClientProvider 每次运行重新加载配置和客户端，工具 Provider 与资源 Provider 平行依赖它；RunService 在一次整体超时内依次加载 MCP、发现工具和发现资源，按失败阶段统一报告错误，再合并执行工具。运行期间保持这批客户端不变，会话仍由 SDK 管理。取消核对当前运行 ID，关闭则禁止新启动并等待活动工作结束。工作任务结束后释放运行槽和用例占用。数据库创建已提交但启动调用方取消时，仍交给执行器生成取消终态。

当前不支持运行中暂停、恢复或 drain。逃逸出根图的异常由 executor 报告并结束为 BLOCKED；根图中断后缺少任务完成结果也按同一路径处理。

## API 与数据升级

- 执行协议为 `8`：结果包含可选 `memory`，普通图片类型为 `image`，图片统计字段为 `image_count`；图片窗口配置使用 `TEST_AGENT_IMAGE_HISTORY_ROUNDS`，资源相对引用参数为 `base_uri`。开发期直接使用新的建库定义，不兼容旧运行数据。
- 运行环境按任务类型保存 `max_cycles_by_task_type` 和 `prompt_fingerprints_by_task_type`；两个映射的键都必须完整覆盖 `TestTaskType`，轮次范围为 1～100，指纹为非空字符串或 `null`。`null` 表示没有提示词，显式空文件则记录空内容的指纹；报告将 `null` 展示为“未定义”。执行与报告按任务类型索引快照；轮次环境变量仍为 `TEST_AGENT_ACT_MAX_CYCLES` 和 `TEST_AGENT_JUDGE_MAX_CYCLES`，在应用装配时转换为映射。
- `POST /api/test-cases/{id}/plans` 接受请求体 `{"planning_request_id":"客户端生成的 UUID","user_input":"本轮补充或修改要求"}`。关联 ID 必填且不持久化；首次规划的额外输入可不填写，已有计划时必须提供非空白输入，基于最新已保存计划（含人工修订）重新规划。缺少额外输入返回 `422 / planning_user_input_required`；生成期间参考版本变化返回 `409 / test_plan_not_latest`，不保存结果或自动重试。规划不读取设备信息。
- `GET /api/test-cases/{id}/planning/stream` 订阅临时规划进度。后端注册后发送命名 `ready` 控制事件，前端收到后才 POST；后续 data 为 OpenAPI 的 PlanningEvent，按 case 路由、按 planning_request_id 筛选。Graph 使用公开 custom stream 提供模型尝试进度，服务补充请求归属、准备输入及保存阶段；校验通过不等于保存成功。
- 规划进度在中栏粘滞弹窗中按日志追加，连接及请求期间禁止关闭、冲突操作和应用内导航；失败可查看并关闭，成功更新计划后自动清空。日志只在组件内存，关闭、离开或刷新后丢弃。初次订阅 10 秒未就绪不会提交规划；断线重连不补拉，POST 网络错误显示结果未确认，不自动重提。SSE 断线不取消规划。同 case 操作互斥，不同 case 独立。
- `POST /api/runs` 接收 `test_plan_id` 和 `assumptions_confirmed: true`。
- 设备查询 API 和 device 字段已删除；运行保存一个 `execution_model` 和实际工具目录。
- 计划修订创建新版本，只允许 latest 启动；相同运行的事件只追加，重复导出创建新文件。
- `GET /api/runs/{id}` 统一返回 `TestRunReport`（`detail`、`events`、`artifacts`），供执行页和历史详情使用。独立 `/report` 和 `/events` 查询已删除；`/stream` 从 `after` 或 `Last-Event-ID` 游标重放同源持久事件，`/api/artifacts/{id}` 读取运行记录与导出文件。
- 运行详情支持非负整数 `events_after`（默认 `0`）：只返回序号大于该值的事件，按序号升序；无新增事件返回空数组，详情和附件仍完整且来自同一次读取快照。页面首次全量读取，后续使用已成功缓存的 HTTP 历史末尾序号补齐增量，不以 SSE 最大序号推进游标。JSON／HTML 导出始终包含完整事件历史。
- `GET /api/test-cases` 支持 `search`、`limit`、`offset`；返回最新计划版本或 `null`。搜索按 ID、名称、正文作字面子串匹配。
- `GET /api/runs` 支持 `search`、`test_case_id`、重复的 `status` 参数、`verdict`、`created_from`、`created_before`、`limit`、`offset`，返回关联用例、计划任务数、任务状态计数及去重图片数。`GET /api/runs/statistics` 使用同一筛选字段，返回分页前的运行数、结果分布、平均时长与每日趋势。
- 时间参数必须携带时区，区间左闭右开；每日统计采用 `Asia/Shanghai`，平均时长只计算有开始和结束时间的已结束运行。没有有效耗时样本返回 `null`。查询只读取现有事实，不连接 MCP、不写库；原领域模型、业务命令及 JSON／HTML 导出内容不变。
- 执行协议为 `8`，支持跨任务记忆和同 Run 图片重读，沿用 Skill 渐进式读取、资源目录快照、任务内正文保留、框架管理会话和节点边界取消语义。HTTP 契约与前端生成类型同步更新；开发阶段不为旧运行快照或 checkpoint 增加兼容处理与数据迁移，也不主动删除本地数据。

## TestPilot 界面

- **规划**：左侧用例库、中间原始用例与规划输入、右侧语义任务及最近五次运行。计划编辑保存新版本；历史版本只读。确认前置假设后才可启动最新计划，重新规划必须填写本轮输入。
- **执行**：任务队列、最新图片、运行冻结的工具／模型／提示词版本，以及按保存顺序展示的消息时间线。循环计数是预算使用量，不代表完成百分比。取消提交后等待服务端终态。
- **历史**：默认最近 30 个上海自然日；筛选及分页保存在 URL。结果标签计数使用基础筛选范围，选中标签仅筛选列表。每日时长曲线采用独立刻度，悬停查看实际时长。
- **详情**：从列表打开弹窗，关闭后保留原筛选及页码；`/runs/{id}/report` 是可刷新、可直接打开的前端路由。详情展示原计划、结果原因、消息及全部图片，展示支持结束结论的观察与判断说明。重新执行回到该用例的最新计划确认页。
- 仅展示已有生产者提供的信息：资源面板包含 Skill 与静态资源的冻结目录；不提供设备／节点激活状态、预计耗时、虚构断言结果、重连统计、用例导入或设置入口。导出保留 JSON 和 HTML 报告及资源元数据。

## 测试和静态检查

应用集成测试通过独立 stdio 进程验证 MCP 接入，测试服务的使用说明见[服务文档](#mcp-服务文档)。

```powershell
$env:PYTHONUTF8 = "1"
cd backend
python -m pytest -q
python -m pyright
python scripts/export_openapi.py ../frontend/src/api/openapi.json
cd ../frontend
npm.cmd run generate:api
npm.cmd test
npm.cmd run typecheck
npm.cmd run check:api
npm.cmd run build
```

上述应用检查应在已安装应用开发依赖的 Python 环境中执行。自动化不使用网络、真实模型密钥或电视。核心验证包括通用工具闭环、取消/超时、图片窗口、事件顺序、图片归属、计划版本、并发限制和导出。

应用固定使用 `langchain[mcp]==1.4.6`、FastMCP 4.1.1 和 MCP SDK 2.3.0，通过[官方 langchain.mcp 接口](https://docs.langchain.com/oss/python/migrate/langchain-mcp-adapters)适配业务工具，并使用 `bind_tools()`、默认 `ToolNode`、公开节点 `timeout` / `error_handler`、消息 reducer 和 checkpointer API。`langchain.mcp` 当前标记为 beta，因此固定依赖版本。产品代码补充超时反馈、运行记录存储、轮数预算、必需任务/Skill 上下文和图片窗口；不读取私有 checkpoint 表、不复制消息协议。结构化返回值保留为消息文本，并清除嵌套的重复图片字节。

已有环境升级时，先在该 Python 环境执行 `python -m pip uninstall -y langchain-mcp-adapters`，再安装本版依赖，避免旧独立 adapter 的 MCP 1.x 约束残留。

本地 Skill 放在 `skills/base/` 或 `skills/external/`，也可通过 `TEST_AGENT_SKILLS_DIR` 指定根目录；用法见 [Skill 说明](skills/README.md)。`app/mcp.py` 管理配置和客户端，`app/tools.py` 导入业务工具，`app/skill.py` 统一包边界、摘要、列表和读取。初始只披露顶层摘要，嵌套 Skill 按需访问。三个目录资源工具是 `load_skill`、`read_resource` 和 `list_resources`；`read_resource` 可用 `base_uri` 解析正文中的相对引用。TaskAgent 另内置 `read_image_artifact` 重读已保存的运行图片。所有调用计入原有执行限制，新增图片统一存储，重读沿用原 ID，保留消息引用并遵循同一图片窗口。运行快照只保存目录元数据，实际读取内容记录在消息中；当前不兼容旧版本运行快照。

会话和协议取消由适配器与 SDK 管理，宿主不维护请求 ID、会话重连或子进程清理状态。MCP [取消规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)允许取消与返回结果竞态；没有收到的结果始终标为未确认。

Uvicorn 保持一个 worker，因为 RunService 的当前工作状态与 EventBus 是进程内对象。不要提交模型密钥、设备凭据、运行数据库、截图或导出。
