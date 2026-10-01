@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
set "PYTHONUTF8=1"

rem Inherit the selected service environment; resolve relative cwd from mcp_servers.
call npx.cmd -y @modelcontextprotocol/inspector@latest --config "mcp.json"
set "inspector_exit_code=%ERRORLEVEL%"
popd
exit /b %inspector_exit_code%
