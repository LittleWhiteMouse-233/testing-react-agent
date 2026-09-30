@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

pushd "%~dp0"
if errorlevel 1 exit /b 1

call "%~dp0check-dependencies.cmd"
if errorlevel 1 goto finish

call python "%~dp0backend\scripts\start_app.py" --cmd-entry

:finish
set "startup_exit_code=%errorlevel%"
popd
exit /b %startup_exit_code%
