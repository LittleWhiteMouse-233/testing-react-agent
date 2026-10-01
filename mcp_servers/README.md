# MCP 服务调试

在仓库根目录运行：

```powershell
conda activate llm-dev
.\mcp_servers\inspect-mcp.cmd
```

已经在本目录时，运行 `.\inspect-mcp.cmd` 即可。先安装满足 MCP Inspector 当前要求的 Node.js；脚本通过 `npx.cmd` 启动 `@modelcontextprotocol/inspector@latest`，跟随 npm 的 latest 发布版本，首次运行或更新时需要联网下载。

脚本只读加载同目录的 `mcp.json`，在 Inspector 网页中选择服务并连接，再调用工具查看输入和返回结果。添加或修改服务入口仍然编辑 `mcp.json`，无需每次在网页中重新配置。修改后重启 Inspector。

脚本将工作目录切换到 `mcp_servers/`，使配置中的相对 `cwd`（例如 `atv_mcp`）从这里解析，退出后恢复调用目录。脚本设置 `PYTHONUTF8=1` 并继承终端环境；`command: "python"` 使用当前 PATH 中的 Python。服务依赖需提前安装在所选环境中；使用独立环境的服务可在 `mcp.json` 中指定解释器绝对路径。

Inspector 会建立自己的 MCP 会话；它用于手动调试，不显示 Agent 已有会话的调用。结束调试时在终端按 Ctrl+C。

配置参数说明见 [Inspector 官方文档](https://github.com/modelcontextprotocol/inspector/blob/main/docs/mcp-server-configuration.md)。
