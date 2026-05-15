Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptRoot
$VenvPython = Join-Path $RepoRoot "venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Missing repo venv Python: $VenvPython" -ForegroundColor Red
    Write-Host "Create the venv and install the project first." -ForegroundColor Yellow
    exit 1
}

Push-Location $RepoRoot
try {
    & $VenvPython -m adminbot.app.main @Args
} finally {
    Pop-Location
}
exit $LASTEXITCODE
