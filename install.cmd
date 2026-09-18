@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

echo [install] Checking installation prerequisites...
where conda >nul 2>nul
if errorlevel 1 (
    echo [install] FAIL: Install Conda and add it to PATH first.
    exit /b 1
)
call conda run --no-capture-output -n llm-dev python --version
if errorlevel 1 (
    echo [install] FAIL: Create or repair the llm-dev Conda environment first.
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
call conda run --no-capture-output -n llm-dev python -m pip install -e "./backend[dev]"
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
echo [install] Installation and basic checks completed. Start with: .\start.ps1
exit /b 0
