@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

if "%~1"=="backend" goto backend
if "%~1"=="frontend" goto frontend

call "%~f0" backend
if errorlevel 1 exit /b %errorlevel%
call "%~f0" frontend
exit /b %errorlevel%

:backend
echo Checking backend dependencies...
call conda run --no-capture-output -n llm-dev python -c "from importlib.metadata import version; version('android-tv-test-agent'); import alembic, uvicorn, pytest, pyright, httpx, pytest_asyncio"
if errorlevel 1 exit /b %errorlevel%
call conda run --no-capture-output -n llm-dev python -m pip check
exit /b %errorlevel%

:frontend
echo Checking frontend dependencies...
call npm.cmd --prefix frontend ls --all --include=dev >nul
if errorlevel 1 exit /b %errorlevel%
call frontend\node_modules\.bin\vite.cmd --version
if errorlevel 1 exit /b %errorlevel%
call frontend\node_modules\.bin\concurrently.cmd --version
exit /b %errorlevel%
