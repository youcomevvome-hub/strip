# Strip launcher - creates venv if needed, installs deps, runs server
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"
$py   = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating virtual environment at $venv ..." -ForegroundColor Cyan
    python -m venv $venv
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $root "backend\requirements.txt")
}

$envFile = Join-Path $root "backend\.env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $root "backend\.env.example") $envFile
    Write-Host "Created backend\.env from example (edit it later to add platform credentials)." -ForegroundColor Yellow
}

Set-Location (Join-Path $root "backend")
Write-Host ""
Write-Host "Strip is starting at http://localhost:8000" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor Green
Write-Host ""
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
