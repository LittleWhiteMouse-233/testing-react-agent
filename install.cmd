@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

call check-dependencies.cmd backend
if not errorlevel 1 (
    echo Backend dependencies are already installed. Skipping.
    goto frontend
)

echo Installing missing backend dependencies...
call conda run --no-capture-output -n llm-dev python -m pip install -e "./backend[dev]"
if errorlevel 1 exit /b %errorlevel%
call check-dependencies.cmd backend
if errorlevel 1 exit /b %errorlevel%

:frontend
call check-dependencies.cmd frontend
if not errorlevel 1 (
    echo Frontend dependencies are already installed. Skipping.
    exit /b 0
)

echo Installing missing frontend dependencies...
call npm.cmd --prefix frontend install --include=dev
if errorlevel 1 exit /b %errorlevel%
call check-dependencies.cmd frontend
exit /b %errorlevel%
