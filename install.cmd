@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

echo [install] Checking installation prerequisites...
where python >nul 2>nul
if errorlevel 1 (
    echo [install] FAIL: Activate your Python environment and ensure python is on PATH.
    exit /b 1
)
call python --version
if errorlevel 1 (
    echo [install] FAIL: Cannot run Python. Activate or repair your Python environment.
    exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
    echo [install] FAIL: Install Node.js and add it to PATH first.
    exit /b 1
)
where npm.cmd >nul 2>nul
if errorlevel 1 (
    echo [install] FAIL: npm is unavailable. Repair the Node.js installation.
    exit /b 1
)

echo [install] Installing backend dependencies incrementally...
call python -m pip install -e "./backend[dev]"
if errorlevel 1 (
    echo [install] FAIL: Backend installation failed. See the package manager output above.
    exit /b 1
)
call check-dependencies.cmd backend
if errorlevel 1 (
    echo [install] FAIL: Backend post-install check failed.
    exit /b 1
)

echo [install] Installing frontend dependencies incrementally...
call npm.cmd --prefix frontend install --include=dev
if errorlevel 1 (
    echo [install] FAIL: Frontend installation failed. See the package manager output above.
    exit /b 1
)
call check-dependencies.cmd frontend
if errorlevel 1 (
    echo [install] FAIL: Frontend post-install check failed.
    exit /b 1
)
echo [install] Installation and basic checks completed. Start with: .\start.cmd
exit /b 0
