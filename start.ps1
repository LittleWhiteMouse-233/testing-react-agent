$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Push-Location $PSScriptRoot
try {
    & "$PSScriptRoot\check-dependencies.cmd"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    & python "$PSScriptRoot\backend\scripts\start_app.py"
    exit $LASTEXITCODE
}
catch {
    Write-Host "[startup] FAIL: $_" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
