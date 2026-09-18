$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

Push-Location $PSScriptRoot
try {
    & "$PSScriptRoot\check-dependencies.cmd"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    # Initialize Conda without depending on the user's PowerShell profile.
    $condaBase = (& conda info --base | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate Conda.' }
    $condaExecutable = Join-Path $condaBase 'Scripts\conda.exe'
    $condaHook = & $condaExecutable shell.powershell hook
    if ($LASTEXITCODE -ne 0) { throw 'Cannot initialize Conda for PowerShell.' }
    $condaHook | Out-String | Invoke-Expression
    conda activate llm-dev
    if ($LASTEXITCODE -ne 0) { throw 'Cannot activate llm-dev.' }

    & (Join-Path $env:CONDA_PREFIX 'python.exe') "$PSScriptRoot\backend\scripts\start_app.py"
    exit $LASTEXITCODE
}
catch {
    Write-Host "[startup] FAIL: $_" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
