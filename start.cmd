@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

call check-dependencies.cmd
if errorlevel 1 (
    echo Dependencies are missing or invalid. Run install.cmd, then start.cmd again.
    exit /b 1
)

echo Updating the database...
pushd backend
call conda run --no-capture-output -n llm-dev python -m alembic upgrade head
set "migration_exit_code=%errorlevel%"
popd
if not "%migration_exit_code%"=="0" exit /b %migration_exit_code%

echo Starting the app at http://localhost:5173
echo Press Ctrl+C to stop both services.
call npm.cmd --prefix frontend run start:app
exit /b %errorlevel%
