@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not "%~2"=="" goto usage
if "%~1"=="backend" goto backend
if "%~1"=="frontend" goto frontend
if not "%~1"=="" goto usage

set "check_failed=0"
call "%~f0" backend
if errorlevel 1 set "check_failed=1"
call "%~f0" frontend
if errorlevel 1 set "check_failed=1"
if "%check_failed%"=="1" (
    echo Basic dependency check failed. See the failed stage above.
    exit /b 1
)
echo Basic dependency check passed. Application readiness has not been checked.
exit /b 0

:backend
echo [backend] Checking basic dependencies...
where conda >nul 2>nul
if errorlevel 1 (
    echo [backend] FAIL: Conda is unavailable. Install Conda and add it to PATH.
    exit /b 1
)
call conda run --no-capture-output -n llm-dev python --version
if errorlevel 1 (
    echo [backend] FAIL: Cannot run Python in llm-dev. Create or repair that Conda environment.
    exit /b 1
)
call conda run --no-capture-output -n llm-dev python -c "from importlib.metadata import version; version('android-tv-test-agent'); import alembic, uvicorn"
if errorlevel 1 (
    echo [backend] FAIL: Basic project dependencies are missing or invalid. Run install.cmd.
    exit /b 1
)
echo [backend] PASS: Basic dependency check passed.
exit /b 0

:frontend
echo [frontend] Checking basic dependencies...
where node >nul 2>nul
if errorlevel 1 (
    echo [frontend] FAIL: Node.js is unavailable. Install Node.js and add it to PATH.
    exit /b 1
)
where npm.cmd >nul 2>nul
if errorlevel 1 (
    echo [frontend] FAIL: npm is unavailable. Repair the Node.js installation.
    exit /b 1
)
set "dependency_log=%TEMP%\moka-dependencies-%RANDOM%-%RANDOM%.log"
call npm.cmd --prefix frontend ls --depth=0 --include=dev >"%dependency_log%" 2>&1
if errorlevel 1 (
    type "%dependency_log%"
    del "%dependency_log%"
    echo [frontend] FAIL: Direct dependencies are missing or invalid. Run install.cmd.
    exit /b 1
)
del "%dependency_log%"
node frontend\node_modules\vite\bin\vite.js --version
if errorlevel 1 (
    echo [frontend] FAIL: Vite cannot start. Run install.cmd and inspect the error above.
    exit /b 1
)
echo [frontend] PASS: Basic dependency check passed.
exit /b 0

:usage
echo Usage: check-dependencies.cmd [backend^|frontend]
exit /b 2
