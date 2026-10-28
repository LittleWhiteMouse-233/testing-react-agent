# ATV MCP：Android TV 控制服务

ATV MCP 通过 ADB 提供电视按键、文字输入、等待和截图能力，可由 TestPilot 或其他 MCP 客户端通过 stdio 调用。本服务独立维护设备配置和依赖；测试用例、计划及报告由[主应用](../../README.md)管理。

本包也提供可在独立 Python 脚本中使用的异步控制器，见[从 Python 控制电视](#从-python-控制电视)。

## 准备环境与设备

准备以下条件：

- Python 3.12 或以上的服务环境。
- 可执行的 ADB，加入 PATH 或在配置中填写绝对路径。
- 可从本机访问的电视 IP，电视已开启 TCP 5555 端口的网络 ADB，并授权本机连接。

服务按 IP 和固定端口 5555 形成目标地址，并在需要时连接，不要求手动执行 `adb connect`。配置只接受不带端口的 IP，不提供自定义端口发现或无线调试配对。

在本 README 所在目录安装本服务：

```powershell
$env:PYTHONUTF8 = "1"
python -m pip install -e .
```

依赖由本目录的 [pyproject.toml](pyproject.toml) 声明。

首次配置时复制模板；已有 `config.yaml` 时直接编辑：

```powershell
Copy-Item config.example.yaml config.yaml
```

将 `ip_address` 的示例地址 `192.0.2.1` 替换为电视实际 IP。`config.yaml` 是本地设备配置，由本目录的 Git 忽略规则保护。

### 设备与队列配置

完整模板见 [config.example.yaml](config.example.yaml)，所有字段在同一层：

| 字段 | 默认值 | 作用 |
|---|---|---|
| `ip_address` | 必填，模板值需替换 | 电视 IPv4 或 IPv6 地址，不含端口 |
| `adb_path` | `adb` | ADB 可执行程序名称或路径 |
| `operation_timeout_seconds` | `15` | 单项操作的超时秒数，必须大于零 |
| `capture_max_attempts` | `3` | 截图最多尝试次数，至少一次 |
| `queue_max_operations` | `16` | 单次队列最多操作数，至少一项 |
| `queue_timeout_seconds` | `60` | 整个队列的超时秒数，必须大于零 |
| `wait_min_ms` | `100` | 显式等待的最小毫秒数，可为零 |
| `wait_max_ms` | `10000` | 显式等待的最大毫秒数，不小于最小值 |
| `post_action_wait_ms` | `1000` | 按键或文字输入后的自动等待，设为零关闭 |

修改配置后，新启动的服务进程使用新值；运行中的进程保留启动时的配置。不要在需要保持同一设备与行为的测试期间修改此文件。

## 连接客户端

仓库默认的 [MCP 连接文件](../mcp.json)已指向本目录的源码入口：

```powershell
$env:PYTHONUTF8 = "1"
python src/atv_mcp_server.py --config config.yaml
```

安装包后也可以使用命令行入口：

```powershell
atv-mcp --config config.yaml
```

以上命令从本 README 所在目录执行。`--config` 必填，相对路径按服务工作目录解析；移动或独立安装后可传入配置文件绝对路径。

stdio 服务启动后等待 MCP 请求。客户端调用 `get_device_capabilities` 成功返回设备与能力信息，才表示完成了实际设备探测。通过应用接入时，解释器、工作目录和环境设置见[外部工具连接](../../README.md#外部工具连接)；手动连接与调用见 [MCP Inspector](../../user_scripts/README.md#检查-mcp-服务)。

## 使用工具

### 查询能力

`get_device_capabilities` 不需要参数，返回目标地址、允许的按键值及说明、实际生效的限制。客户端应先读取这些信息，再构造操作。

### 执行操作队列

`execute_operations` 接收 `operations` 列表，按声明顺序执行。例如，返回主页并截图：

```json
{
  "operations": [
    {"type": "press_key", "key": "HOME"},
    {"type": "screenshot"}
  ]
}
```

| 操作 | 参数 |
|---|---|
| `press_key` | `key`：能力查询和工具 schema 中列出的按键值 |
| `screenshot` | 无额外参数，返回 PNG 图片 |
| `wait` | `duration_ms`：配置范围内的等待毫秒数 |
| `text` | `text`：向当前焦点输入，长度 1～1000，不含控制字符 |

队列必须非空且不超过配置长度，连续两项不能都是截图。无效操作类型、按键或字段会在工具参数校验阶段被拒绝；队列长度、等待范围、文字控制字符和连续截图也会在执行前检查，校验失败不执行操作。

按键和文字输入完成后默认自动等待。紧随其后的显式 `wait` 会替代自动等待，即使它比默认等待更短；截图与等待操作本身不再附加自动等待。

### 连接、失败与取消

服务串行处理设备访问。能力查询以及设备命令会探测连接；目标不可用时尝试一次连接并再次检查，仍不可用则返回包含“设备断连”和诊断信息的错误。

整个队列预算包含初始探测和连接；单项操作中的连接检查、截图尝试和设备命令共用该项预算。自动等待受队列总预算约束。

执行错误会停止余下操作，以标准 MCP 错误标志返回已经产生的有序文字、图片和失败位置。参数层校验错误可能由框架直接返回，不一定带队列位置。按键和文字命令失败后不会自动重放，截图允许有限重试。

取消在操作之间和等待期间响应；当前设备命令可能继续到完成或超时。客户端没有确认收到结果时，先重新观察设备状态，再决定后续动作，避免重复未知结果的按键或输入。

## 从 Python 控制电视

安装本包后，可以从任意目录导入 `atv_adb_ctrl`，不需要通过 MCP。导入或创建控制器不会连接电视；设备操作时按需检查连接。

下面的示例回到主页，主动等待后保存截图。执行前替换示例 IP：

```python
import asyncio
from ipaddress import ip_address
from pathlib import Path

from atv_adb_ctrl import ATVController, ATVControlSettings, RemoteKey


async def main() -> None:
    controller = ATVController(ATVControlSettings(
        ip_address=ip_address("192.0.2.1"),
    ))
    await controller.press_key(RemoteKey.HOME)
    await controller.wait(1000)
    Path("screen.png").write_bytes(await controller.screenshot())


asyncio.run(main())
```

`ATVControlSettings` 使用上表中的设备字段：`ip_address`、`adb_path`、`operation_timeout_seconds` 和 `capture_max_attempts`。它不加载 YAML，也不依赖 MCP。

| 控制器接口 | 行为 |
|---|---|
| `serial` | 只读的 ADB 目标地址 |
| `await probe()` | 检查并按需连接设备 |
| `await run(*arguments)` | 检查设备后执行 ADB 参数，返回输出字节 |
| `await press_key(key)` | 按下 `RemoteKey` 枚举中的键 |
| `await input_text(text)` | 向当前焦点输入文字 |
| `await screenshot()` | 返回 PNG 字节，保留有限截图重试 |
| `await wait(duration_ms)` | 按毫秒等待 |

控制器异常由调用脚本处理。调用方负责顺序、并发、总超时和操作后的等待；控制器的按键与文字输入不会自动等待。MCP 层的队列长度、等待范围、连续截图限制和部分结果返回不应用于独立控制器。需要改变设备或设置时创建新控制器。

## 排查问题与维护

| 现象 | 检查方向 |
|---|---|
| 找不到 ADB | 确认服务环境的 PATH，或将 `adb_path` 改为可执行程序绝对路径 |
| 返回“设备断连” | 核对实际 IP、网络可达性、网络 ADB 端口和电视授权，查看返回的连接诊断 |
| 队列在执行前被拒绝 | 根据工具 schema 与能力查询检查操作类型、按键、数量及等待范围 |
| 截图失败 | 查看设备连接、PNG 获取和超时诊断；截图重试有次数与时间限制 |
| 画面尚未稳定就截图 | 调整自动等待，或在动作后显式安排合适的 `wait` |
| 队列中途停止 | 查看已返回的部分结果与失败位置，重新观察后再安排后续操作 |

ATV 是独立可信外部包，不通过应用自动化测试导入、启动或验证。维护时在服务环境准备 Pyright、服务依赖及 `types-PyYAML` 后，可从本目录执行静态检查：

```powershell
$env:PYTHONUTF8 = "1"
python -m pyright .
```

应用使用规则和最终结果的解释由[项目 README](../../README.md)维护。
